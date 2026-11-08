"""Branded documents (the brand pack, brandpack.py): decks and one-pagers.

Brand rules applied:
- decks on the master template (page-coloured slides); one-pagers on a white page; headings in the ink colour;
  body and mono fonts;
- the primary colour for structure (rules, table headers); the accent colour as the single accent per page;
- the wordmark from the brand pack, never retyped.

Every deck and one-pager is checked against the sales fact register (facts.py) when one is wired: the result
says whether it is ready for a customer, and a sources slide lists the fact ids used.

In-process `brand` tools:
  make_deck(title, slides, subtitle?, language?)   a .pptx in the outbox (send_file / email_file / SharePoint)
  make_one_pager(title, sections, language?)       a .docx one-pager
"""

from __future__ import annotations

import io
import json
import os
import re
from pathlib import Path
from .brandpack import brand
from .org import org, localise

_P = brand().palette
PAGE, INK, PRIMARY, ACCENT, MUTED, TINT = _P.page, _P.ink, _P.primary, _P.accent, _P.muted, _P.tint
HEADING_FONT, BODY_FONT, MONO_FONT = brand().fonts.heading, brand().fonts.body, brand().fonts.mono
FACT_ID_RE = re.compile(r"\[(F-\d{4,})\]")

TOOLS = [
    {"name": "make_deck",
     "description": ("Build a {org}-branded PowerPoint deck. slides: [{title, bullets?: [..], notes?, kind?: "
                     "'bullets' | 'two_column' (left, right) | 'table' (columns, rows) | 'statement' (text)}]. Put "
                     "the fact id after each claim, e.g. 'Energised in 14–18 months [F-0001]'; ids are moved to a "
                     "sources slide. Benefits first, one idea per slide, short bullets. The result says whether "
                     "every claim traces to an approved fact (ready for a customer) or what to fix."),
     "inputSchema": {"type": "object", "properties": {
         "title": {"type": "string"}, "subtitle": {"type": "string"}, "language": {"type": "string"},
         "slides": {"type": "array", "items": {"type": "object"}}}, "required": ["title", "slides"]}},
    {"name": "make_one_pager",
     "description": ("Build a {org}-branded one-page Word document: sections [{heading, text?, bullets?}]. Same fact "
                     "id rules and check as make_deck."),
     "inputSchema": {"type": "object", "properties": {
         "title": {"type": "string"}, "language": {"type": "string"},
         "sections": {"type": "array", "items": {"type": "object"}}}, "required": ["title", "sections"]}},
]
TOOLS = localise(TOOLS)


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")[:60] or "document"


def _strip_ids(text: str, used: list[str]) -> str:
    for fid in FACT_ID_RE.findall(text or ""):
        if fid not in used:
            used.append(fid)
    return re.sub(r"\s*\[F-\d{4,}\]", "", text or "").strip()


def all_text(items) -> str:
    """Every string in a slide/section spec, for the claim check."""
    if isinstance(items, str):
        return items
    if isinstance(items, dict):
        return "\n".join(all_text(v) for k, v in items.items() if k != "kind")
    if isinstance(items, list):
        return "\n".join(all_text(v) for v in items)
    return ""


def _strip_all(value, used: list[str]):
    """Every string in a slide spec with its fact ids moved out (they go on the sources slide)."""
    if isinstance(value, str):
        return _strip_ids(value, used)
    if isinstance(value, list):
        return [_strip_all(v, used) for v in value]
    if isinstance(value, dict):
        return {k: (_strip_all(v, used) if k != "kind" else v) for k, v in value.items()}
    return value


def build_deck(title: str, slides: list[dict], subtitle: str | None = None) -> tuple[bytes, list[str]]:
    """The pre-sales agent's make_deck on the brand's master deck (deck.py): bullets, two_column, table and statement
    slides, plus a sources slide listing the fact ids used."""
    from .deck import build_deck as build
    used: list[str] = []
    spec_slides = []
    for spec in slides:
        spec = _strip_all(spec, used)
        kind = spec.get("kind") or ("table" if spec.get("rows") else "bullets")
        spec["kind"] = kind
        spec_slides.append(spec)
    deck = {"title": _strip_ids(title, used), "subtitle": _strip_ids(subtitle, used) if subtitle else None,
            "slides": spec_slides}
    if used:
        deck["slides"].append({"kind": "bullets", "title": "Sources",
                               "bullets": [f"{fid}: {org().name} sales fact register" for fid in used]})
    return build(deck), used


def build_one_pager(title: str, sections: list[dict]) -> tuple[bytes, list[str]]:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Inches, Pt, RGBColor

    used: list[str] = []
    doc = Document()
    # white page, like every branded Word document (they are printed and marked up)
    for sec in doc.sections:
        sec.left_margin = sec.right_margin = Inches(0.8)
        sec.top_margin = sec.bottom_margin = Inches(0.7)
    style = doc.styles["Normal"]
    style.font.name, style.font.size = BODY_FONT, Pt(11)
    style.font.color.rgb = RGBColor.from_string(INK)
    wordmark = brand().file(brand().wordmark)
    if wordmark and wordmark.exists():
        doc.add_picture(str(wordmark), height=Inches(0.35))
        doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.RIGHT
    h = doc.add_paragraph()
    r = h.add_run(_strip_ids(title, used))
    r.font.name, r.font.size, r.font.color.rgb = HEADING_FONT, Pt(24), RGBColor.from_string(INK)
    for sec in sections:
        hp = doc.add_paragraph()
        hr = hp.add_run(_strip_ids(sec.get("heading") or "", used))
        hr.font.name, hr.font.size, hr.font.color.rgb = HEADING_FONT, Pt(14), RGBColor.from_string(PRIMARY)
        if sec.get("text"):
            doc.add_paragraph(_strip_ids(sec["text"], used))
        for b in sec.get("bullets") or []:
            doc.add_paragraph(_strip_ids(b, used), style="List Bullet")
    if used:
        p = doc.add_paragraph()
        rr = p.add_run("Sources: " + ", ".join(used) + f" ({org().name} sales fact register)")
        rr.font.size, rr.font.color.rgb = Pt(8), RGBColor.from_string(MUTED)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue(), used


class BrandTools:
    in_process = True

    def __init__(self, files=None, facts=None):
        self.files = files  # FileTools: outbox
        self.facts = facts  # FactTools: the claim check

    async def list_tools(self) -> list[dict]:
        return TOOLS

    async def call_tool(self, name: str, args: dict, ctx=None) -> str:
        import asyncio
        conv = getattr(ctx, "conversation_id", None) or ""
        if not self.files:
            return json.dumps({"error": "files aren't available here"})
        if name == "make_deck":
            if not args.get("slides"):
                return json.dumps({"error": "give the slides"})
            data, used = await asyncio.to_thread(build_deck, args.get("title", ""), args["slides"], args.get("subtitle"))
            filename, body = _slug(args.get("title", "deck")) + ".pptx", all_text([args.get("title"), args.get("subtitle"),
                                                                                    args["slides"]])
        elif name == "make_one_pager":
            if not args.get("sections"):
                return json.dumps({"error": "give the sections"})
            data, used = await asyncio.to_thread(build_one_pager, args.get("title", ""), args["sections"])
            filename, body = _slug(args.get("title", "one-pager")) + ".docx", all_text([args.get("title"), args["sections"]])
        else:
            raise ValueError(f"unknown tool {name}")
        fid = await self.files._put_outbox(conv, filename, data)
        out = {"file": fid, "name": filename, "size": len(data), "fact_ids": used}
        if self.facts:
            check = await self.facts.check(body)
            out["claim_check"] = check
            out["customer_ready"] = check["ok"]
            if not check["ok"]:
                out["next"] = "not ready for a customer: " + check["next"]
        else:
            out["customer_ready"] = False
            out["next"] = "no fact register here: internal use only"
        if args.get("language") and args["language"].lower() not in ("en", "english", "en-au"):
            out["translation_note"] = ("machine-translated: have a native speaker check it before it goes out")
        return json.dumps(out)
