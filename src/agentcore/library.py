"""Reference library: legislation, codes and guides the agent reads offline (in-process `library` tools).

  list_references()                         what's in the library: each document's id, edition, licence, retrieved date
  search_references(query, doc?, limit?)    ranked sections (SQLite FTS5, BM25, Porter stemming), with a snippet
  read_reference(doc, section, offset?)     one section by its number ("17", "s 17", "reg 291", "C2D2", "Part 4",
                                            "3.2") or title words, with the citation line to quote

The library is the repo's `knowledge/` folder, baked into the image like the skills (KNOWLEDGE_DIR, default
/app/knowledge). Each document is a folder: `source.yaml` (title, short name, edition, source URL, licence,
attribution, retrieved date) and Markdown files whose headings carry the clause number first, e.g.
`### 17 Payment claims`. A document whose `status` is `not_loaded` is listed (with how to get it) but has no text.

Only material whose licence allows a copy is held here (knowledge/README.md). Text is read-only and is never
changed by an agent. The index is built in memory on first use, in a worker thread.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from .org import localise

log = logging.getLogger(__name__)

MAX_READ_CHARS = 12_000
SNIPPET_TOKENS = 40
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
# Words people put before a number: "s 17", "section 17", "reg. 291", "cl 34.2", "clause C2D2".
PREFIX = re.compile(r"^(?:s|ss|sec|section|sections|r|reg|regs|regulation|regulations|cl|clause|clauses|"
                    r"item|para|paragraph)(?:\.\s*|\s+)", re.I)

TOOLS = [
    {"name": "list_references",
     "description": ("List the reference library you hold offline: {library_scope}. Each entry gives the "
                     "document id, edition or version date, licence and the date it was retrieved. Use this library "
                     "before the web for anything it covers."),
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "search_references",
     "description": ("Search the offline reference library and return the best-matching sections, each with its "
                     "document, section heading and a snippet. `doc` limits the search to one document id (see "
                     "list_references). Then read the section with read_reference before relying on it, and cite "
                     "the document, edition and section number."),
     "inputSchema": {"type": "object", "required": ["query"], "properties": {
         "query": {"type": "string", "description": "words to find, e.g. 'payment schedule time' or 'high risk construction work'"},
         "doc": {"type": "string", "description": "a document id to search only that document"},
         "limit": {"type": "integer", "minimum": 1, "maximum": 20}}}},
    {"name": "read_reference",
     "description": ("Read one section of a reference document by its number or title: an Act's section ('17' or "
                     "'s 17'), a regulation ('reg 291'), an NCC clause ('C2D2'), a part ('Part 4'), a code of practice "
                     "heading ('3.2'), or words from the heading. Returns the text with the citation line to quote. "
                     "Long sections come in pages: pass `offset` from the previous reply to continue."),
     "inputSchema": {"type": "object", "required": ["doc", "section"], "properties": {
         "doc": {"type": "string", "description": "document id from list_references"},
         "section": {"type": "string"},
         "offset": {"type": "integer", "minimum": 0}}}},
]
TOOLS = localise(TOOLS)


@dataclass
class Section:
    doc: str
    level: int
    heading: str
    trail: str          # the headings above it, e.g. "Part 3 > Division 2"
    body: str           # its own text, up to the next heading
    full_end: int = 0   # index (exclusive) of the last section that belongs under it


@dataclass
class Document:
    id: str
    meta: dict
    sections: list[Section] = field(default_factory=list)

    @property
    def short(self) -> str:
        return str(self.meta.get("short") or self.meta.get("title") or self.id)

    def cite(self, heading: str) -> str:
        bits = [f"{self.short}, {heading}"]
        if self.meta.get("edition"):
            bits.append(str(self.meta["edition"]))
        if self.meta.get("retrieved"):
            bits.append(f"retrieved {self.meta['retrieved']}")
        line = " — ".join([bits[0], "; ".join(bits[1:])]) if len(bits) > 1 else bits[0]
        src = self.meta.get("source_url")
        return line + (f" ({src})" if src else "")


def _key(text: str) -> str:
    """The lookup key of a heading or a requested section: its leading number, lower-case."""
    t = PREFIX.sub("", text.strip())
    m = re.match(r"^(?:(part|division|subdivision|chapter|schedule|volume|appendix|annexure|specification|spec)\s+)?"
                 r"([A-Z]{0,2}\d+[A-Z0-9.\-]*|[A-Z](?![A-Z]))", t, flags=re.I)
    if not m:
        return ""
    kind = (m.group(1) or "").lower()
    kind = "specification" if kind == "spec" else kind
    return ((kind + " " if kind else "") + m.group(2).rstrip(".-")).lower()


def parse_markdown(doc: str, text: str) -> list[Section]:
    sections: list[Section] = []
    stack: list[tuple[int, str]] = []
    body: list[str] = []
    current: Section | None = None
    for line in text.splitlines():
        m = HEADING.match(line)
        if m:
            if current is not None:
                current.body = "\n".join(body).strip()
            elif body and "".join(body).strip():  # text before the first heading
                sections.append(Section(doc, 7, "(preamble)", "", "\n".join(body).strip()))
            level, heading = len(m.group(1)), m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            current = Section(doc, level, heading, " > ".join(h for _, h in stack), "")
            stack.append((level, heading))
            sections.append(current)
            body = []
        else:
            body.append(line)
    if current is not None:
        current.body = "\n".join(body).strip()
    elif body and "".join(body).strip():
        sections.append(Section(doc, 7, "(preamble)", "", "\n".join(body).strip()))
    return sections


def default_dir() -> Path:
    if os.environ.get("KNOWLEDGE_DIR"):
        return Path(os.environ["KNOWLEDGE_DIR"])
    here = Path(__file__).resolve()
    repo = here.parents[5] / "knowledge" if len(here.parents) > 5 else None
    return repo if repo and repo.is_dir() else Path("/app/knowledge")


class ReferenceLibrary:
    """The documents under one folder, with a full-text index built on first use."""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root else default_dir()
        self.docs: dict[str, Document] = {}
        self._db: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def _load(self) -> None:
        if self._db is not None:
            return
        docs: dict[str, Document] = {}
        if self.root.is_dir():
            for meta_path in sorted(self.root.glob("*/source.yaml")):
                folder = meta_path.parent
                try:
                    meta = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
                except yaml.YAMLError as exc:
                    log.error("library: bad %s: %s", meta_path, exc)
                    continue
                doc = Document(str(meta.get("id") or folder.name), meta)
                for md in sorted(folder.glob("*.md")):
                    doc.sections += parse_markdown(doc.id, md.read_text(encoding="utf-8"))
                for i, s in enumerate(doc.sections):  # where each section's subtree ends
                    j = i + 1
                    while j < len(doc.sections) and doc.sections[j].level > s.level:
                        j += 1
                    s.full_end = j
                docs[doc.id] = doc
        else:
            log.warning("library: %s not found; the reference library is empty", self.root)
        db = sqlite3.connect(":memory:", check_same_thread=False)
        db.execute("CREATE VIRTUAL TABLE s USING fts5(doc UNINDEXED, idx UNINDEXED, heading, trail, body,"
                   " tokenize='porter unicode61')")
        db.executemany("INSERT INTO s VALUES (?,?,?,?,?)",
                       ((d.id, i, sec.heading, sec.trail, sec.body)
                        for d in docs.values() for i, sec in enumerate(d.sections) if sec.body or sec.level >= 3))
        db.commit()
        self.docs, self._db = docs, db
        log.info("library: %d documents, %d sections", len(docs), sum(len(d.sections) for d in docs.values()))

    def ensure(self) -> None:
        with self._lock:
            self._load()

    # ---- tools ----
    def list(self) -> dict:
        self.ensure()
        out = []
        for d in self.docs.values():
            m = d.meta
            entry = {"id": d.id, "short": d.short, "title": m.get("title"), "edition": m.get("edition"),
                     "jurisdiction": m.get("jurisdiction"), "licence": m.get("licence"),
                     "retrieved": str(m.get("retrieved") or ""), "sections": sum(1 for s in d.sections if s.level >= 3)}
            if m.get("status") == "not_loaded" or not d.sections:
                entry["status"] = "not loaded: no text held"
                if m.get("how_to_get"):
                    entry["how_to_get"] = m["how_to_get"]
            if m.get("notes"):
                entry["notes"] = m["notes"]
            out.append(entry)
        return {"documents": out, "note": "Cite the short name, section number and edition. The library is a copy "
                                          "as at the retrieved date: check the source for later amendments when it matters."}

    @staticmethod
    def _match_query(query: str, mode: str) -> str:
        phrases = re.findall(r'"([^"]+)"', query)
        rest = re.sub(r'"[^"]+"', " ", query)
        words = [w for w in re.findall(r"\w+", rest.lower()) if w not in STOPWORDS]
        terms = [f'"{p.replace(chr(34), " ")}"' for p in phrases] + [f'"{w}"' for w in words]
        return f" {mode} ".join(terms)

    def search(self, query: str, doc: str | None = None, limit: int = 8) -> dict:
        self.ensure()
        query = (query or "").strip()
        if not query:
            raise ValueError("say what to search for")
        if doc and doc not in self.docs:
            raise ValueError(f"no document {doc!r}; ids: {', '.join(sorted(self.docs))}")
        limit = max(1, min(int(limit or 8), 20))
        rows: list = []
        seen: set[tuple[str, int]] = set()
        for mode in ("AND", "OR"):
            q = self._match_query(query, mode)
            if not q:
                break
            sql = ("SELECT doc, idx, snippet(s, 4, '**', '**', ' … ', ?) FROM s WHERE s MATCH ?"
                   + (" AND doc = ?" if doc else "") + " ORDER BY bm25(s, 0, 0, 6.0, 2.0, 1.0) LIMIT ?")
            params = [SNIPPET_TOKENS, q] + ([doc] if doc else []) + [limit]
            with self._lock:
                try:
                    found = self._db.execute(sql, params).fetchall()
                except sqlite3.OperationalError as exc:
                    raise ValueError(f"couldn't search for that: {exc}") from exc
            for r in found:
                if (r[0], r[1]) not in seen:
                    seen.add((r[0], r[1]))
                    rows.append(r)
            if len(rows) >= limit:
                break
        results = []
        for doc_id, idx, snip in rows[:limit]:
            d = self.docs[doc_id]
            sec = d.sections[int(idx)]
            results.append({"doc": doc_id, "short": d.short, "section": sec.heading, "within": sec.trail,
                            "snippet": snip, "read": {"doc": doc_id, "section": _key(sec.heading) or sec.heading}})
        return {"query": query, "results": results,
                **({} if results else {"note": "Nothing in the library matched. Try other words, or the web."})}

    def find(self, doc_id: str, wanted: str) -> tuple[Document, int] | tuple[Document, list[str]]:
        self.ensure()
        d = self.docs.get(doc_id)
        if d is None:
            raise ValueError(f"no document {doc_id!r}; ids: {', '.join(sorted(self.docs))}")
        if not d.sections:
            how = d.meta.get("how_to_get")
            raise ValueError(f"{d.short} is listed but no text is held yet." + (f" {how}" if how else ""))
        key = _key(wanted)
        if key:
            hits = [i for i, s in enumerate(d.sections) if _key(s.heading) == key]
            if hits:  # prefer the deepest (a clause over a part with the same number)
                return d, max(hits, key=lambda i: (d.sections[i].level, -i))
        words = [w for w in re.findall(r"\w+", wanted.lower()) if w not in STOPWORDS]
        if words:
            scored = [(sum(w in s.heading.lower() for w in words), -i) for i, s in enumerate(d.sections)]
            best = max(scored, default=(0, 0))
            if best[0] == len(words):
                return d, -best[1]
        near = [s.heading for s in d.sections if key and _key(s.heading).startswith(key.split(".")[0])][:10]
        if not near and words:
            near = [s.heading for s in d.sections if any(w in s.heading.lower() for w in words)][:10]
        return d, near

    def read(self, doc_id: str, wanted: str, offset: int = 0) -> dict:
        d, hit = self.find(doc_id, (wanted or "").strip())
        if isinstance(hit, list):
            return {"error": f"no section {wanted!r} in {d.short}",
                    **({"did_you_mean": hit} if hit else {"hint": "search_references with doc set finds it by words"})}
        s = d.sections[hit]
        parts = []
        for sub in d.sections[hit:s.full_end]:
            parts.append(f"{'#' * min(sub.level, 6)} {sub.heading}\n\n{sub.body}".rstrip())
        text = "\n\n".join(parts)
        offset = max(0, int(offset or 0))
        page = text[offset:offset + MAX_READ_CHARS]
        out = {"doc": d.id, "section": s.heading, "within": s.trail, "cite": d.cite(s.heading),
               "licence": d.meta.get("licence"), "text": page}
        if d.meta.get("attribution"):
            out["attribution"] = d.meta["attribution"]
        if offset + MAX_READ_CHARS < len(text):
            out["more"] = {"next_offset": offset + MAX_READ_CHARS, "total_chars": len(text)}
        return out


STOPWORDS = {"the", "a", "an", "of", "to", "in", "and", "or", "for", "on", "is", "are", "be", "by", "with", "what",
             "when", "how", "does", "do", "under", "about", "as", "at", "it", "that", "this", "which"}


class LibraryTools:
    """The `library` in-process MCP server. Read-only; no store, no network."""

    in_process = True

    def __init__(self, library: ReferenceLibrary | None = None):
        self.library = library or ReferenceLibrary()

    async def list_tools(self) -> list[dict]:
        return TOOLS

    async def call_tool(self, name: str, args: dict, ctx=None) -> str:
        try:
            if name == "list_references":
                out = await asyncio.to_thread(self.library.list)
            elif name == "search_references":
                out = await asyncio.to_thread(self.library.search, args.get("query", ""), args.get("doc") or None,
                                              args.get("limit") or 8)
            elif name == "read_reference":
                out = await asyncio.to_thread(self.library.read, args.get("doc", ""), str(args.get("section", "")),
                                              args.get("offset") or 0)
            else:
                raise ValueError(f"unknown tool {name}")
        except ValueError as exc:
            out = {"error": str(exc)}
        return json.dumps(out, ensure_ascii=False)
