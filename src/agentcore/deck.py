"""Branded PowerPoint decks: built on the brand pack's master deck, and edits to any deck.

Every new deck starts from the brand pack's deck template (brandpack.py): its example slides are removed and
slides are added from its named layouts, so the result carries the brand's masters (wordmark, page colour,
heading-font titles, body text, mono figures, footer, slide numbers).

  build_deck(spec, images?) -> bytes            spec: {title, subtitle?, author?, date?, slides: [{kind, ...}]}
  markdown_to_deck(md) -> spec                   the Markdown a model writes (or JSON) -> spec
  edit_deck(data, ops, images?) -> (bytes, [change])   edits to any .pptx (branded or not)
  deck_outline(data) -> [{slide, layout, title, text, table, chart, picture}]

Slide kinds: bullets, two_column, section, statement, image, comparison, table, numbers, process, timeline, chart,
closing.
A title may mark its accent phrase with *asterisks*: it is set in the italic accent colour, as in the master deck.
"""

from __future__ import annotations

import copy
import csv
import io
import json
import os
import re
from datetime import date
from pathlib import Path
from .brandpack import brand
from .org import org

_P = brand().palette
INK, PRIMARY, ACCENT, MUTED, RULE = _P.ink, _P.primary, _P.accent, _P.muted, _P.rule
HEADING_FONT, BODY_FONT, MONO_FONT = brand().fonts.heading, brand().fonts.body, brand().fonts.mono
MAX_BULLETS, MAX_ROWS = 7, 9

# The brand's deck layouts by role (brand.yaml deck_layouts). A deck template that has the bullets and table layouts
# is built on its own layouts; otherwise slides are plain (python-pptx's default template).
_L = brand().deck_layouts
L_TITLE, L_BULLETS, L_SECTION, L_IMAGE = (_L.get("title", "Title Slide"), _L.get("bullets", ""), _L.get("section", ""),
                                          _L.get("image", ""))
L_COMPARE, L_TABLE, L_NUMBERS = _L.get("compare", ""), _L.get("table", ""), _L.get("numbers", "")
L_PROCESS, L_TIMELINE, L_BAR, L_LINE, L_CLOSING = (_L.get("process", ""), _L.get("timeline", ""), _L.get("bar", ""),
                                                  _L.get("line", ""), _L.get("closing", ""))


def template_path() -> Path | None:
    """The brand's deck template, or None (plain python-pptx slides)."""
    return brand().file(brand().templates.deck)


# ---- small helpers ---------------------------------------------------------------------------------------------

def _rgb(h):
    from pptx.dml.color import RGBColor
    return RGBColor.from_string(h)


def _set_runs(paragraph, text: str, accent: bool = True) -> None:
    """Text into a paragraph; *phrase* becomes an italic accent-colour run (the master deck's title accent)."""
    parts = re.split(r"(\*[^*]+\*)", text) if accent else [text]
    for part in parts:
        if not part:
            continue
        run = paragraph.add_run()
        if accent and part.startswith("*") and part.endswith("*") and len(part) > 2:
            run.text = part[1:-1]
            run.font.italic = True
            run.font.color.rgb = _rgb(ACCENT)
        else:
            run.text = part


def _fill(ph, text, accent: bool = False) -> None:
    tf = ph.text_frame
    lines = str(text).split("\n")
    tf.text = ""
    for i, line in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        _set_runs(p, line, accent)


def _fit_title(ph, text: str, steps) -> None:
    """Shrink a long title so it stays in its box: steps = [(max chars, points), ...]; short titles keep the layout's size."""
    from pptx.util import Pt
    n = len(re.sub(r"\*", "", text))
    for limit, size in steps:
        if n <= limit:
            return
        for p in ph.text_frame.paragraphs:
            for r in p.runs:
                r.font.size = Pt(size)


def _widen(ph, left_in: float, width_in: float) -> None:
    """Move/widen a placeholder that inherits its position from the layout, keeping its top and height."""
    from pptx.util import Inches
    top, height = ph.top, ph.height  # inherited values, read before the slide gets its own xfrm
    ph.left, ph.top, ph.width, ph.height = Inches(left_in), top, Inches(width_in), height


def _remove(shape) -> None:
    el = shape._element
    el.getparent().remove(el)


def _is_number(v) -> bool:
    return bool(re.fullmatch(r"[\s$€£A-Z]{0,4}[-+]?[\d.,]+\s*(%|[kKmMbB]|MW|kW|MVA|kV|h|yr|wk|weeks|months)?\s*", str(v)))


def _bullet(paragraph, level: int) -> None:
    from pptx.oxml.ns import qn
    from lxml import etree
    pPr = paragraph._p.get_or_add_pPr()
    indent = 320040
    pPr.set("marL", str(indent * (level + 1)))
    pPr.set("indent", str(-indent))
    for tag in ("a:buNone", "a:buChar", "a:buAutoNum"):
        for el in pPr.findall(qn(tag)):
            pPr.remove(el)
    bu = etree.SubElement(pPr, qn("a:buChar"))
    bu.set("char", "•" if level == 0 else "–")


def _picture_bytes(name: str, data: bytes) -> bytes:
    if name.lower().endswith(".svg"):
        from .vector import svg_to_png
        return svg_to_png(data.decode(), 1600)
    return data


# ---- the builder ---------------------------------------------------------------------------------------------

class _Builder:
    """Adds slides to a presentation. With the brand's layouts present it uses them; otherwise plain fallbacks."""

    def __init__(self, prs, title: str, images: dict | None = None):
        self.prs, self.title, self.images = prs, title, images or {}
        self.layouts = {lay.name: lay for lay in prs.slide_layouts}
        self.branded = L_BULLETS in self.layouts and L_TABLE in self.layouts
        self.sections = 0
        self.section = ""
        self.added: list = []

    # placeholders --------------------------------------------------------------------------------------------
    def _new(self, layout_name: str):
        from pptx.enum.shapes import PP_PLACEHOLDER
        layout = self.layouts[layout_name]
        s = self.prs.slides.add_slide(layout)
        # python-pptx doesn't clone footer and slide-number placeholders: copy them from the layout
        for ph in layout.placeholders:
            kind = ph.placeholder_format.type
            if kind in (PP_PLACEHOLDER.FOOTER, PP_PLACEHOLDER.SLIDE_NUMBER):
                el = copy.deepcopy(ph._element)
                s.shapes._spTree.append(el)
        for ph in s.placeholders:
            if ph.placeholder_format.type == PP_PLACEHOLDER.FOOTER:
                runs = ph.text_frame.paragraphs[0].runs
                _widen(ph, 4.0, 8.58)  # right-aligned: room for a long deck title on one line
                if runs:  # keep the layout's run formatting (body font 12, tracked, muted colour)
                    runs[0].text = f"{brand().name} · {self.title}"
                    for extra in runs[1:]:
                        extra.text = ""
                else:
                    ph.text_frame.text = f"{brand().name} · {self.title}"
        self.added.append(s)
        return s

    @staticmethod
    def _ph(s, idx):
        for ph in s.placeholders:
            if ph.placeholder_format.idx == idx:
                return ph
        return None

    def _finish(self, s, used: set[int], notes: str | None = None, section: str | None = None) -> None:
        """Fill the eyebrow, then remove every placeholder left empty (they show 'Click to add…' prompts)."""
        from pptx.enum.shapes import PP_PLACEHOLDER
        eyebrow = (section if section is not None else self.section) or ""
        for ph in list(s.placeholders):
            f = ph.placeholder_format
            if f.idx in used or f.type in (PP_PLACEHOLDER.FOOTER, PP_PLACEHOLDER.SLIDE_NUMBER):
                continue
            prompt = ""
            try:
                prompt = s.slide_layout.placeholders.get(idx=f.idx).text_frame.text
            except (AttributeError, KeyError):
                pass
            if "SECTION TITLE" in prompt.upper() and eyebrow:
                _fill(ph, eyebrow.upper())
                continue
            _remove(ph)
        if notes:
            s.notes_slide.notes_text_frame.text = str(notes)

    # kinds ---------------------------------------------------------------------------------------------------
    def title_slide(self, spec: dict):
        if not self.branded:
            return self._plain(spec.get("title") or self.title, [spec.get("subtitle") or ""])
        s = self._new(L_TITLE)
        used = set()
        for idx, val, acc in ((0, spec.get("title") or self.title, True), (1, spec.get("subtitle"), False),
                              (11, spec.get("author"), False),
                              (12, spec.get("date") or date.today().strftime("%B %Y"), False)):
            if val and (ph := self._ph(s, idx)):
                _fill(ph, val, acc)
                used.add(idx)
                if idx == 0:
                    if len(val) > 12:  # the layout's half-width title box only holds a word or two at full size
                        _widen(ph, 0.76, 11.0)
                    _fit_title(ph, val, [(12, 44), (34, 40), (60, 34), (90, 28), (10_000, 24)])
                elif idx == 1 and len(val) > 50:
                    _widen(ph, 0.76, 11.0)
                elif idx in (11, 12):
                    _widen(ph, 0.77, 6.2)
        self._finish(s, used, section="")
        return s

    def add(self, spec: dict) -> None:
        kind = (spec.get("kind") or "").lower() or self._guess(spec)
        if spec.get("section"):
            self.section = spec["section"]
        fn = getattr(self, f"k_{kind}", None)
        if not fn:
            raise ValueError(f"unknown slide kind {kind!r}: use bullets, two_column, section, statement, image, "
                             "comparison, table, numbers, process, timeline, chart or closing")
        fn(spec)

    @staticmethod
    def _guess(spec: dict) -> str:
        for key, kind in (("rows", "table"), ("columns", "table"), ("series", "chart"), ("steps", "process"),
                          ("left", "comparison"), ("image", "image"), ("items", "numbers")):
            if spec.get(key):
                return kind
        return "bullets"

    def _titled(self, layout: str, spec: dict, title: str | None = None):
        s = self._new(layout)
        used = set()
        t = title if title is not None else spec.get("title") or ""
        if t and (ph := self._ph(s, 0)):
            _fill(ph, t, True)
            _fit_title(ph, t, [(55, 26), (75, 22), (10_000, 18)])
            used.add(0)
        return s, used

    def _textbox(self, s, items, left, top, width, height, size=20):
        """Bullets in the body font and ink colour: `size` pt at level 1, size-4 at level 2 (muted), 8 pt after each."""
        from pptx.util import Inches, Pt
        tf = s.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height)).text_frame
        tf.word_wrap = True
        for i, item in enumerate(items):
            text, level = (item.get("text", ""), int(item.get("level", 0))) if isinstance(item, dict) else (str(item), 0)
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            _set_runs(p, text, accent=False)
            _bullet(p, min(level, 2))
            p.space_after = Pt(8)
            for r in p.runs:
                r.font.name = BODY_FONT
                r.font.size = Pt(size if level == 0 else size - 4)
                r.font.color.rgb = _rgb(INK if level == 0 else MUTED)
        return tf

    def _plain(self, title: str, lines: list, table=None, chart=None, picture=None):
        """A slide in an unbranded deck: its own title layout plus text, table, chart or picture."""
        from pptx.util import Inches
        layout = next((lay for name in ("Title Only", "Title and Content", "Title Slide")
                       for lay in self.prs.slide_layouts if lay.name == name), self.prs.slide_layouts[0])
        s = self.prs.slides.add_slide(layout)
        self.added.append(s)
        if s.shapes.title is not None:
            s.shapes.title.text = re.sub(r"\*([^*]+)\*", r"\1", title or "")
        for ph in list(s.placeholders):
            if s.shapes.title is not None and ph.shape_id == s.shapes.title.shape_id:
                continue
            _remove(ph)
        w = self.prs.slide_width / 914400
        h = self.prs.slide_height / 914400
        top = 1.6
        if table:
            self._table_shape(s.shapes.add_table(len(table[1]) + 1, len(table[0]), Inches(0.6), Inches(top),
                                                 Inches(w - 1.2), Inches(0.4)).table, *table)
        elif chart:
            self._chart(s.shapes, chart, Inches(0.6), Inches(top), Inches(w - 1.2), Inches(h - top - 0.6))
        elif picture:
            s.shapes.add_picture(io.BytesIO(picture), Inches(0.6), Inches(top), height=Inches(h - top - 0.6))
        if [x for x in lines if x]:
            self._textbox(s, [x for x in lines if x], 0.6, top if not (table or chart or picture) else h - 1.4,
                          w - 1.2, 1.2 if (table or chart or picture) else h - top - 0.6, size=18)
        return s

    def k_bullets(self, spec):
        bullets = spec.get("bullets") or ([spec["text"]] if spec.get("text") else [])
        chunks = [bullets[i:i + MAX_BULLETS] for i in range(0, len(bullets), MAX_BULLETS)] or [[]]
        for n, chunk in enumerate(chunks):
            title = spec.get("title") or ""
            title = f"{title} (cont.)" if n else title
            if not self.branded:
                s = self._plain(title, chunk)
            else:
                s, used = self._titled(L_BULLETS, spec, title)
                # 7 bullets at 20 pt fit the 4.2 in box; long bullets that wrap shrink to 18 pt
                long = sum(len(b.get("text", "") if isinstance(b, dict) else str(b)) > 90 for b in chunk)
                self._textbox(s, chunk, 0.76, 2.45, 11.81, 4.2, size=18 if len(chunk) + long > 8 else 20)
                self._finish(s, used, spec.get("notes") if n == 0 else None)

    def k_section(self, spec):
        self.sections += 1
        self.section = re.sub(r"\*", "", spec.get("title") or "")
        if not self.branded:
            self._plain(spec.get("title") or "", [spec.get("subtitle") or ""])
            return
        s, used = self._titled(L_SECTION, spec)
        for idx, val in ((1, spec.get("subtitle")), (13, spec.get("number") or f"{self.sections:02d}")):
            if val and (ph := self._ph(s, idx)):
                _fill(ph, val)
                used.add(idx)
        self._finish(s, used, spec.get("notes"), section="")

    def k_two_column(self, spec):
        left, right = spec.get("left") or [], spec.get("right") or []
        if not self.branded:
            self._plain(spec.get("title") or "", [*left, *right])
            return
        s, used = self._titled(L_BULLETS, spec)
        heads = bool(spec.get("left_title") or spec.get("right_title"))
        for x, items, head in ((0.76, left, spec.get("left_title")), (6.86, right, spec.get("right_title"))):
            top = 2.45 + (0.55 if heads and not head else 0)
            if head:
                from pptx.util import Inches, Pt
                tf = s.shapes.add_textbox(Inches(x), Inches(top), Inches(5.7), Inches(0.4)).text_frame
                r = tf.paragraphs[0].add_run()
                r.text = str(head).upper()
                r.font.name, r.font.size, r.font.color.rgb = MONO_FONT, Pt(12), _rgb(PRIMARY)
                top += 0.55
            self._textbox(s, items, x, top, 5.7, 4.2 - (top - 2.45), size=20 if len(items) <= 6 else 18)
        self._finish(s, used, spec.get("notes"))

    def k_statement(self, spec):
        text = spec.get("text") or spec.get("title") or ""
        if not self.branded:
            self._plain(text, [])
            return
        s, used = self._titled(L_SECTION, spec, text)
        self._finish(s, used, spec.get("notes"), section="")

    def k_image(self, spec):
        ref = spec.get("image") or ""
        found = self.images.get(ref)
        data = _picture_bytes(found[0], found[1]) if found else None
        body = spec.get("body") or "\n".join(str(b) for b in spec.get("bullets") or [])
        if not data and self.branded:  # no picture: a plain bullets slide, not an empty picture frame
            lines = [x for x in body.split("\n") if x.strip()] or [spec.get("caption") or ""]
            self.k_bullets({**spec, "kind": "bullets", "bullets": [x for x in lines if x]})
            return
        if not self.branded:
            self._plain(spec.get("title") or "", [body] if body else [], picture=data)
            return
        s, used = self._titled(L_IMAGE, spec)
        if body and (ph := self._ph(s, 13)):
            _fill(ph, body)
            used.add(13)
        if data and (ph := self._ph(s, 10)):
            box = (ph.left, ph.top, ph.width, ph.height)
            pic = ph.insert_picture(io.BytesIO(data))
            pic.left, pic.top, pic.width, pic.height = box  # explicit, so every renderer places it the same
            used.add(10)
        self._finish(s, used, spec.get("notes"))

    def k_comparison(self, spec):
        left, right = spec.get("left") or {}, spec.get("right") or {}
        if not self.branded:
            lines = [f"{c.get('label', '')}: {c.get('headline', '')} {c.get('body', '')}".strip() for c in (left, right)]
            self._plain(spec.get("title") or "", lines)
            return
        s, used = self._titled(L_COMPARE, spec)
        for side, (a, b, c) in ((left, (18, 19, 20)), (right, (21, 22, 23))):
            for idx, val in ((a, (side.get("label") or "").upper()), (b, side.get("headline")), (c, side.get("body"))):
                if val and (ph := self._ph(s, idx)):
                    _fill(ph, val)
                    used.add(idx)
        self._finish(s, used, spec.get("notes"))

    @staticmethod
    def _table_shape(table, columns, rows):
        from pptx.util import Pt
        from pptx.oxml.ns import qn
        from lxml import etree
        tbl = table._tbl
        tblPr = tbl.tblPr
        for el in tblPr.findall(qn("a:tableStyleId")):
            tblPr.remove(el)
        tblPr.set("bandRow", "0")
        tblPr.set("firstRow", "1")

        def style(cell, text, header):
            cell.text = str(text)
            cell.fill.background()
            cell.margin_left = 0
            tcPr = cell._tc.get_or_add_tcPr()
            for tag in ("a:lnL", "a:lnR", "a:lnT", "a:lnB"):
                for el in tcPr.findall(qn(tag)):
                    tcPr.remove(el)
            for tag in ("a:lnL", "a:lnR"):
                ln = etree.Element(qn(tag), w="0")
                etree.SubElement(ln, qn("a:noFill"))
                tcPr.insert(0, ln)
            ln = etree.Element(qn("a:lnB"), w="9525" if header else "3175")
            fill = etree.SubElement(ln, qn("a:solidFill"))
            etree.SubElement(fill, qn("a:srgbClr"), val=INK if header else RULE)
            tcPr.insert(2, ln)
            for p in cell.text_frame.paragraphs:
                for r in p.runs:
                    if header:
                        r.font.name, r.font.size, r.font.color.rgb = MONO_FONT, Pt(12), _rgb(PRIMARY)
                        r.text = r.text.upper()
                    else:
                        r.font.name = MONO_FONT if _is_number(text) else BODY_FONT
                        r.font.size, r.font.color.rgb = Pt(14), _rgb(INK)

        for j, c in enumerate(columns):
            style(table.cell(0, j), c, True)
        for i, row in enumerate(rows, 1):
            for j in range(len(columns)):
                style(table.cell(i, j), row[j] if j < len(row) else "", False)

    def k_table(self, spec):
        columns = [str(c) for c in spec.get("columns") or []]
        rows = [list(map(str, r)) for r in spec.get("rows") or []]
        if not columns and rows:
            columns, rows = rows[0], rows[1:]
        if not columns:
            raise ValueError("a table slide needs columns")
        chunks = [rows[i:i + MAX_ROWS] for i in range(0, len(rows), MAX_ROWS)] or [[]]
        for n, chunk in enumerate(chunks):
            title = (spec.get("title") or "") + (" (cont.)" if n else "")
            if not self.branded:
                self._plain(title, [], table=(columns, chunk))
                continue
            s, used = self._titled(L_TABLE, spec, title)
            ph = self._ph(s, 13)
            frame = ph.insert_table(len(chunk) + 1, len(columns))
            self._table_shape(frame.table, columns, chunk)
            used.add(13)
            self._finish(s, used, spec.get("notes") if n == 0 else None)

    def _groups(self, spec, key, size, layout, slots, fields):
        items = spec.get(key) or []
        if not items:
            raise ValueError(f"a {spec.get('kind')} slide needs {key}")
        for n in range(0, len(items), size):
            chunk = items[n:n + size]
            title = (spec.get("title") or "") + (" (cont.)" if n else "")
            if not self.branded:
                self._plain(title, [" · ".join(str(it.get(f, "")) for f in fields if it.get(f)) for it in chunk])
                continue
            s, used = self._titled(layout, spec, title)
            for k, (it, idxs) in enumerate(zip(chunk, slots)):
                for f, idx in zip(fields, idxs):
                    val = it.get(f)
                    if f == "label" and not val and key == "steps":
                        val = f"STEP {n + k + 1:02d}"
                    if val not in (None, "") and (ph := self._ph(s, idx)):
                        _fill(ph, val)
                        used.add(idx)
            self._finish(s, used, spec.get("notes") if n == 0 else None)

    def k_numbers(self, spec):
        self._groups(spec, "items", 3, L_NUMBERS, [(13, 14), (15, 16), (17, 18)], ("value", "label"))

    def k_process(self, spec):
        self._groups(spec, "steps", 3, L_PROCESS, [(13, 19, 14), (20, 22, 21), (23, 25, 24)],
                     ("label", "heading", "body"))

    def k_timeline(self, spec):
        start = len(self.added)
        self._groups(spec, "items", 4, L_TIMELINE, [(13, 19, 14), (26, 28, 27), (29, 31, 30), (32, 34, 33)],
                     ("date", "heading", "body"))
        if not self.branded:
            return
        from pptx.enum.shapes import MSO_SHAPE
        from pptx.util import Inches
        items = spec.get("items") or []
        for n, s in enumerate(self.added[start:]):  # the master deck's dots on the line; `current` in the accent
            for k, x in enumerate((0.78, 3.80, 6.80, 9.81)[:len(items[n * 4:n * 4 + 4])]):
                dot = s.shapes.add_shape(MSO_SHAPE.OVAL, Inches(x), Inches(3.32), Inches(0.24), Inches(0.24))
                dot.fill.solid()
                dot.fill.fore_color.rgb = _rgb(ACCENT if items[n * 4 + k].get("current") else PRIMARY)
                dot.line.fill.background()

    @staticmethod
    def _chart_data(spec):
        from pptx.chart.data import CategoryChartData
        cats = [str(c) for c in spec.get("categories") or []]
        series = spec.get("series") or []
        if not cats or not series:
            raise ValueError("a chart slide needs categories and series [{name, values}]")
        data = CategoryChartData()
        data.categories = cats
        for se in series:
            vals = [float(v) if v not in (None, "") else None for v in se.get("values") or []]
            data.add_series(str(se.get("name") or "Series"), vals[:len(cats)])
        return data

    def _style_chart(self, chart, line: bool):
        from pptx.util import Pt
        chart.font.name, chart.font.size = BODY_FONT, Pt(11)
        chart.font.color.rgb = _rgb(MUTED)
        chart.has_title = False
        plot = chart.plots[0]
        n = len(plot.series)
        chart.has_legend = n > 1
        if chart.has_legend:
            from pptx.enum.chart import XL_LEGEND_POSITION
            chart.legend.position, chart.legend.include_in_layout = XL_LEGEND_POSITION.TOP, False
        for k, se in enumerate(plot.series):
            colour = ACCENT if (k == 0 if line else k == n - 1) else MUTED
            if line:
                se.format.line.color.rgb = _rgb(colour)
                se.format.line.width = Pt(2.25)
                se.smooth = False
            else:
                se.format.fill.solid()
                se.format.fill.fore_color.rgb = _rgb(MUTED)
                if n == 1 and len(se.points):  # one series: the last bar is the highlight, as in the master deck
                    pt = se.points[len(list(se.values)) - 1]
                    pt.format.fill.solid()
                    pt.format.fill.fore_color.rgb = _rgb(ACCENT)
                elif k == n - 1:
                    se.format.fill.fore_color.rgb = _rgb(ACCENT)
        if not line:
            plot.has_data_labels = True
            plot.data_labels.font.size = Pt(10)
            plot.data_labels.font.color.rgb = _rgb(INK)
            plot.gap_width = 80
        va = chart.value_axis
        va.has_major_gridlines = True
        va.major_gridlines.format.line.color.rgb = _rgb(RULE)
        va.format.line.fill.background()

    def _chart(self, shapes, spec, x, y, cx, cy):
        from pptx.enum.chart import XL_CHART_TYPE
        line = (spec.get("chart") or "bar").lower().startswith("line")
        kind = XL_CHART_TYPE.LINE_MARKERS if line else XL_CHART_TYPE.COLUMN_CLUSTERED
        frame = shapes.add_chart(kind, x, y, cx, cy, self._chart_data(spec))
        self._style_chart(frame.chart, line)

    def k_chart(self, spec):
        from pptx.enum.chart import XL_CHART_TYPE
        line = (spec.get("chart") or "bar").lower().startswith("line")
        if not self.branded:
            self._plain(spec.get("title") or "", [spec.get("body") or ""], chart=spec)
            return
        s, used = self._titled(L_LINE if line else L_BAR, spec)
        if spec.get("body") and (ph := self._ph(s, 14)):
            _fill(ph, spec["body"])
            used.add(14)
        ph = self._ph(s, 13)
        kind = XL_CHART_TYPE.LINE_MARKERS if line else XL_CHART_TYPE.COLUMN_CLUSTERED
        frame = ph.insert_chart(kind, self._chart_data(spec))
        self._style_chart(frame.chart, line)
        used.add(13)
        self._finish(s, used, spec.get("notes"))

    def k_closing(self, spec):
        text = spec.get("text") or spec.get("title") or "Thank you."
        if not self.branded:
            self._plain(text, [spec.get("presenter") or ""])
            return
        s = self._new(L_CLOSING)
        used = set()
        for idx, val, acc in ((13, text, True), (14, spec.get("presenter"), False), (19, spec.get("role"), False),
                              (16, spec.get("email"), False), (18, spec.get("phone"), False),
                              (17, spec.get("web") or ((brand().website or org().website) if spec.get("email") else None), False)):
            if val and (ph := self._ph(s, idx)):
                _fill(ph, val, acc)
                used.add(idx)
                if idx in (16, 17, 18):
                    _widen(ph, 8.64, 3.93)
        self._finish(s, used, spec.get("notes"), section="")


def _clear_slides(prs) -> None:
    lst = prs.slides._sldIdLst
    for sld in list(lst):
        prs.part.drop_rel(sld.rId)
        lst.remove(sld)


def build_deck(spec: dict, images: dict[str, tuple[str, bytes]] | None = None) -> bytes:
    """A new branded deck. spec: {title, subtitle?, author?, date?, slides: [{kind, title?, section?, notes?, ...}]}."""
    from pptx import Presentation
    if not isinstance(spec, dict) or not (spec.get("title") or spec.get("slides")):
        raise ValueError("a deck needs a title and slides")
    tpl = template_path()
    prs = Presentation(str(tpl)) if tpl else Presentation()
    _clear_slides(prs)
    b = _Builder(prs, re.sub(r"\*", "", spec.get("title") or "Presentation"), images)
    b.title_slide(spec)
    for slide in spec.get("slides") or []:
        b.add(slide)
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def image_refs_in_deck(content: str) -> list[str]:
    """The image refs a .pptx's content needs resolved (outbox:…, a file name, a SharePoint link), in order, once
    each: `![caption](ref)` lines in Markdown, or slides[].image in a JSON spec."""
    text = (content or "").strip()
    refs: list[str] = []
    if text.startswith("{"):
        try:
            spec = json.loads(text)
        except ValueError:
            return []
        candidates = [str(sl.get("image")) for sl in (spec.get("slides") or []) if isinstance(sl, dict) and sl.get("image")]
    else:
        candidates = [m.group(2).strip() for line in text.split("\n") if (m := _IMAGE.match(line.strip()))]
    for ref in candidates:
        if ref not in refs:
            refs.append(ref)
    return refs


# ---- Markdown -> spec --------------------------------------------------------------------------------------------

_TABLE_SEP = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_IMAGE = re.compile(r"^!\[([^\]]*)\]\(([^)]+)\)\s*$")
_NUMBER = re.compile(r"^(?:[-*]\s+)?\*\*([^*]+)\*\*\s*[:–—-]?\s*(.+)$")


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _slide_from_block(title: str, lines: list[str]) -> dict:
    notes, body, bullets, table, chart, image, quote, numbers = [], [], [], [], None, None, [], []
    i = 0
    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if not s:
            i += 1
            continue
        if s.lower().startswith("notes:"):
            notes.append(s[6:].strip())
            notes.extend(x.strip() for x in lines[i + 1:] if x.strip())
            break
        if s.startswith("```"):
            info = s[3:].strip().lower()
            block = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                block.append(lines[i])
                i += 1
            i += 1
            if info.startswith("chart"):
                rows = [r for r in csv.reader(io.StringIO("\n".join(block).strip())) if r]
                if rows and len(rows[0]) > 1:
                    chart = {"chart": "line" if "line" in info else "bar", "categories": [r[0] for r in rows[1:]],
                             "series": [{"name": rows[0][k], "values": [r[k] if k < len(r) else None for r in rows[1:]]}
                                        for k in range(1, len(rows[0]))]}
            else:
                body.append("\n".join(block))
            continue
        if s.startswith("|") and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1].strip()):
            table = [_cells(s)]
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                table.append(_cells(lines[i]))
                i += 1
            continue
        if m := _IMAGE.match(s):
            image = {"caption": m.group(1), "ref": m.group(2).strip()}
        elif s.startswith(">"):
            quote.append(s.lstrip("> ").strip())
        elif m := _NUMBER.match(s):
            numbers.append({"value": m.group(1).strip(), "label": m.group(2).strip()})
            bullets.append(re.sub(r"\*\*", "", s.lstrip("-* ").strip()))
        elif re.match(r"^\s*([-*+]|\d+[.)])\s+", line):
            indent = len(line) - len(line.lstrip())
            bullets.append({"text": re.sub(r"^([-*+]|\d+[.)])\s+", "", s), "level": min(indent // 2, 2)})
        else:
            body.append(s)
        i += 1
    spec: dict = {"title": title}
    if chart:
        spec.update(kind="chart", body=" ".join(body), **chart)
    elif table:
        spec.update(kind="table", columns=table[0], rows=table[1:])
    elif image:
        spec.update(kind="image", image=image["ref"],
                    body="\n".join(body + [b["text"] if isinstance(b, dict) else b for b in bullets]))
        spec["title"] = title or image["caption"]
    elif 2 <= len(numbers) <= 3 and len(numbers) == len(bullets) and not body:
        spec.update(kind="numbers", items=numbers)
    elif quote and not bullets and not body:
        spec.update(kind="section", subtitle=" ".join(quote))
        if not title:
            spec["title"] = quote[0]
            spec.pop("subtitle")
    else:
        spec.update(kind="bullets", bullets=[*body, *bullets] if body and bullets else (bullets or body))
    if notes:
        spec["notes"] = " ".join(n for n in notes if n)
    return spec


def markdown_to_deck(md: str) -> dict:
    """The Markdown (or JSON) a model writes -> a build_deck spec."""
    text = (md or "").replace("\r\n", "\n").strip()
    if text.startswith("{"):
        spec = json.loads(text)
        if not isinstance(spec, dict):
            raise ValueError("JSON deck must be an object {title, slides}")
        return spec
    lines = text.split("\n")
    spec: dict = {"title": "", "slides": []}
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and re.match(r"^#\s+", lines[i]):
        spec["title"] = lines[i].lstrip("# ").strip()
        i += 1
        if i < len(lines) and lines[i].strip() and not lines[i].strip().startswith(("#", "-", "|", "---")):
            spec["subtitle"] = lines[i].strip()
            i += 1
    blocks: list[tuple[str, list[str]]] = []
    cur_title, cur = "", []
    in_fence = False
    for line in lines[i:]:
        s = line.strip()
        if s.startswith("```"):
            in_fence = not in_fence
        if not in_fence and (re.fullmatch(r"-{3,}|\*{3,}", s) or re.match(r"^##\s+", s)):
            if cur_title or any(x.strip() for x in cur):
                blocks.append((cur_title, cur))
            cur_title, cur = (s.lstrip("# ").strip() if s.startswith("##") else ""), []
            continue
        if not in_fence and re.match(r"^###\s+", s) and not cur_title:
            cur_title = s.lstrip("# ").strip()
            continue
        cur.append(line)
    if cur_title or any(x.strip() for x in cur):
        blocks.append((cur_title, cur))
    for title, body in blocks:
        slide = _slide_from_block(title, body)
        if title.lower() in ("thank you", "thank you.", "questions", "questions?") and slide.get("kind") == "bullets" \
                and not slide.get("bullets"):
            slide = {"kind": "closing", "text": title}
        spec["slides"].append(slide)
    if not spec["title"]:
        spec["title"] = next((s.get("title") for s in spec["slides"] if s.get("title")), "Presentation")
    return spec


# ---- editing any deck --------------------------------------------------------------------------------------------

def _iter_text_frames(shapes):
    for sh in shapes:
        if sh.shape_type == 6:  # group
            yield from _iter_text_frames(sh.shapes)
            continue
        if getattr(sh, "has_text_frame", False) and sh.has_text_frame:
            yield sh.text_frame
        if getattr(sh, "has_table", False) and sh.has_table:
            for row in sh.table.rows:
                for cell in row.cells:
                    yield cell.text_frame


def _replace_in_frame(tf, find: str, repl: str) -> int:
    count = 0
    for p in tf.paragraphs:
        runs = p.runs
        hit = False
        for r in runs:
            if find in r.text:
                count += r.text.count(find)
                r.text = r.text.replace(find, repl)
                hit = True
        full = "".join(r.text for r in runs)
        if not hit and find in full and runs:
            count += full.count(find)
            runs[0].text = full.replace(find, repl)
            for r in runs[1:]:
                r.text = ""
    return count


def _title_of(slide) -> str:
    try:
        if slide.shapes.title is not None and slide.shapes.title.text_frame.text.strip():
            return slide.shapes.title.text_frame.text.strip()
    except (AttributeError, KeyError):
        pass
    for tf in _iter_text_frames(slide.shapes):
        if tf.text.strip():
            return tf.text.strip().split("\n")[0][:80]
    return ""


def deck_outline(data: bytes) -> list[dict]:
    from pptx import Presentation
    prs = Presentation(io.BytesIO(data))
    out = []
    for n, s in enumerate(prs.slides, 1):
        texts = [tf.text.strip() for tf in _iter_text_frames(s.shapes) if tf.text.strip()]
        out.append({"slide": n, "layout": s.slide_layout.name, "title": _title_of(s),
                    "text": " / ".join(texts)[:300],
                    "table": sum(1 for sh in s.shapes if getattr(sh, "has_table", False) and sh.has_table),
                    "chart": any(getattr(sh, "has_chart", False) and sh.has_chart for sh in s.shapes),
                    "picture": any(sh._element.tag.endswith("}pic") for sh in s.shapes)})  # incl. filled placeholders
    return out


def _slide(prs, n) -> object:
    slides = list(prs.slides)
    try:
        k = int(n)
    except (TypeError, ValueError):
        k = 0
    if not 1 <= k <= len(slides):
        titles = "; ".join(f"{i}: {_title_of(s)[:40]}" for i, s in enumerate(slides, 1))
        raise ValueError(f"no slide {n!r}: the deck has {len(slides)} slides ({titles})")
    return slides[k - 1]


def _body_frame(slide):
    """The main text of a slide: the biggest body placeholder or text box that isn't the title, footer or eyebrow."""
    from pptx.enum.shapes import PP_PLACEHOLDER
    title = slide.shapes.title
    best, area = None, 0
    for sh in slide.shapes:
        if not getattr(sh, "has_text_frame", False) or not sh.has_text_frame:
            continue
        if title is not None and sh.shape_id == title.shape_id:
            continue
        if sh.is_placeholder and sh.placeholder_format.type in (PP_PLACEHOLDER.FOOTER, PP_PLACEHOLDER.SLIDE_NUMBER,
                                                                 PP_PLACEHOLDER.DATE, PP_PLACEHOLDER.TITLE,
                                                                 PP_PLACEHOLDER.CENTER_TITLE):
            continue
        a = (sh.width or 0) * (sh.height or 0)
        if a > area:
            best, area = sh, a
    return best


def _deck_title(prs) -> str:
    for s in prs.slides:
        for tf in _iter_text_frames(s.shapes):
            m = re.match(rf"^{re.escape(brand().name)} · (.+)$", tf.text.strip())
            if m and "PRESENTATION TITLE" not in m.group(1):
                return m.group(1)
    slides = list(prs.slides)
    return _title_of(slides[0]) if slides else "Presentation"


def edit_deck(data: bytes, ops: list[dict], images: dict[str, tuple[str, bytes]] | None = None) -> tuple[bytes, list[str]]:
    """Apply ops to any .pptx. Slides are 1-based. Returns (new bytes, what changed)."""
    from pptx import Presentation
    from pptx.util import Pt
    prs = Presentation(io.BytesIO(data))
    changes: list[str] = []
    if not ops:
        raise ValueError("no edits given")
    for op in ops:
        kind = (op.get("op") or "").lower()
        if kind == "replace_text":
            find, repl = op.get("find") or "", op.get("replace", "")
            if not find:
                raise ValueError("replace_text needs find")
            count = 0
            for s in prs.slides:
                for tf in _iter_text_frames(s.shapes):
                    count += _replace_in_frame(tf, find, str(repl))
                if s.has_notes_slide:
                    count += _replace_in_frame(s.notes_slide.notes_text_frame, find, str(repl))
            if not count:
                raise ValueError(f"{find!r} isn't in the deck")
            changes.append(f"replaced {find!r} with {repl!r} ({count}×)")
        elif kind == "set_title":
            s = _slide(prs, op.get("slide"))
            if s.shapes.title is None:
                raise ValueError(f"slide {op.get('slide')} has no title placeholder")
            tf = s.shapes.title.text_frame
            tf.text = ""
            _set_runs(tf.paragraphs[0], str(op.get("text", "")))
            changes.append(f"slide {op['slide']}: title set")
        elif kind == "set_bullets":
            s = _slide(prs, op.get("slide"))
            sh = _body_frame(s)
            bullets = op.get("bullets") or []
            if sh is None:
                b = _Builder(prs, _deck_title(prs))
                b._textbox(s, bullets, 0.76, 2.45, prs.slide_width / 914400 - 1.5, 4.2)
            else:
                tf = sh.text_frame
                first_rpr = None
                if tf.paragraphs and tf.paragraphs[0].runs:
                    first_rpr = copy.deepcopy(tf.paragraphs[0].runs[0]._r.find(
                        "{http://schemas.openxmlformats.org/drawingml/2006/main}rPr"))
                tf.text = ""
                for i, item in enumerate(bullets):
                    text, level = (item.get("text", ""), int(item.get("level", 0))) if isinstance(item, dict) \
                        else (str(item), 0)
                    p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
                    p.level = min(level, 4)
                    r = p.add_run()
                    r.text = text
                    if first_rpr is not None:
                        r._r.insert(0, copy.deepcopy(first_rpr))
                    if not sh.is_placeholder:
                        _bullet(p, min(level, 2))
            changes.append(f"slide {op['slide']}: {len(bullets)} bullets set")
        elif kind == "set_notes":
            s = _slide(prs, op.get("slide"))
            s.notes_slide.notes_text_frame.text = str(op.get("text", ""))
            changes.append(f"slide {op['slide']}: notes set")
        elif kind == "delete_slide":
            _slide(prs, op.get("slide"))
            lst = prs.slides._sldIdLst
            el = list(lst)[int(op["slide"]) - 1]
            prs.part.drop_rel(el.rId)
            lst.remove(el)
            changes.append(f"slide {op['slide']} deleted")
        elif kind == "move_slide":
            _slide(prs, op.get("slide"))
            lst = prs.slides._sldIdLst
            items = list(lst)
            to = int(op.get("to") or 0)
            if not 1 <= to <= len(items):
                raise ValueError(f"'to' must be 1–{len(items)}")
            el = items[int(op["slide"]) - 1]
            lst.remove(el)
            lst.insert(to - 1, el)
            changes.append(f"slide {op['slide']} moved to {to}")
        elif kind == "add_slides":
            slides = op.get("slides") or []
            if not slides:
                raise ValueError("add_slides needs slides")
            b = _Builder(prs, _deck_title(prs), images)
            for spec in slides:
                b.add(spec)
            after = op.get("after")
            if after is not None:
                total = len(prs.slides)
                pos = int(after)
                if not 0 <= pos <= total - len(b.added):
                    raise ValueError(f"'after' must be 0–{total - len(b.added)}")
                lst = prs.slides._sldIdLst
                new = list(lst)[-len(b.added):]
                for k, el in enumerate(new):
                    lst.remove(el)
                    lst.insert(pos + k, el)
            changes.append(f"{len(b.added)} slide(s) added" + (f" after slide {after}" if after is not None else ""))
        elif kind == "set_table_cell":
            s = _slide(prs, op.get("slide"))
            tables = [sh.table for sh in s.shapes if getattr(sh, "has_table", False) and sh.has_table]
            k = int(op.get("table") or 1)
            if not 1 <= k <= len(tables):
                raise ValueError(f"slide {op['slide']} has {len(tables)} table(s)")
            t = tables[k - 1]
            r, c = int(op.get("row") or 0), int(op.get("col") or 0)
            if not (1 <= r <= len(t.rows) and 1 <= c <= len(t.columns)):
                raise ValueError(f"the table is {len(t.rows)} rows × {len(t.columns)} columns (1-based, header is row 1)")
            cell = t.cell(r - 1, c - 1)
            p = cell.text_frame.paragraphs[0]
            if p.runs:
                p.runs[0].text = str(op.get("text", ""))
                for extra in p.runs[1:]:
                    extra.text = ""
                for extra in cell.text_frame.paragraphs[1:]:
                    extra._p.getparent().remove(extra._p)
            else:
                cell.text = str(op.get("text", ""))
                for run in cell.text_frame.paragraphs[0].runs:
                    run.font.size = Pt(13)
            changes.append(f"slide {op['slide']}: table {k} cell ({r},{c}) set")
        else:
            raise ValueError(f"unknown edit {kind!r}: use replace_text, set_title, set_bullets, set_notes, "
                             "delete_slide, move_slide, add_slides or set_table_cell")
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue(), changes
