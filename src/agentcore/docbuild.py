"""Word documents in the brand's style, and edits to any Word document (files.py `create_file`, `edit_file`).

A branded document starts from the brand pack's Word template (brandpack.py): its page, styles, theme fonts and
colours, table style, first-page wordmark and footer (document title, classification, page number) are kept as they
are, and the cover and title block below go on top. A letter (front matter `type: Letter`) starts from the brand
pack's letterhead. Without the template files (tests, an old image) the style below is built in code.

The style is an engineering document format (the organisation's document procedure and the brand pack):
A4, white page (documents are printed and marked up, so no coloured ground), the wordmark, a small tracked eyebrow
(the document type), a heading-font title and body-font subtitle, a title block (document number, revision, date,
status, author, reviewers) with mono values, heading-font headings, body text in the ink colour, the primary colour
for structure (table headers and rules), and a footer with the
document's name, its classification and "Page n of m" on every page.

The Markdown a .docx is built from (filemaker.docx_from_markdown) may start with front matter:

    ---
    type: Project report            the eyebrow, e.g. Engineering procedure, Working notes, Minutes
    title: PRJ-01 programme baseline
    subtitle: Concept phase, first baseline for review
    number: PRJ-GEN-RPT-nnnn        the document controller allocates numbers; never invent one (use nnnn)
    revision: A
    date: 14 October 2026
    status: Draft for review
    author: Ada (AI) for J. Smith
    reviewers: B. Jones (check), J. Smith (approve)
    scope: What this covers and what it doesn't (shown as the scope note under the title block)
    classification: Confidential    the footer's word (default: the template's "Commercial in confidence")
    style: plain                    no brand styling (a document for an outside template)
    ---

A letter on the letterhead:

    ---
    type: Letter
    date: 16 October 2026
    to: Jane Citizen; Project Director; Example Pty Ltd; 1 Example St, Exampletown   (lines, split on ;)
    dear: Jane                      (the salutation's name; default the first line of `to`)
    subject: Subject line states the point, in sentence case
    from_name: Sam Taylor
    from_role: Chief Technology Officer
    from_email: sam.taylor@example.com
    from_phone: +61 4xx xxx xxx      (the letterhead's contact lines; left out when not given)
    sign_off: Kind regards
    ---
    Body paragraphs in Markdown.

Without front matter the first "# heading" becomes the title, and the title block shows only the date.

Edits (`edit_docx`) work on any .docx, branded or not, and keep everything they don't touch. With
track_changes they are Word tracked changes by the agent (accept or reject in Word); comments are real Word
comments anchored to the text they're about.
"""

from __future__ import annotations

import copy
import io
import os
import re
from datetime import date
from pathlib import Path
from .brandpack import brand

_P = brand().palette
PAGE, INK, PRIMARY, ACCENT, MUTED, TINT, RULE = _P.page, _P.ink, _P.primary, _P.accent, _P.muted, _P.tint, \
    _P.rule_doc or _P.rule
HEADING_FONT, BODY_FONT, MONO_FONT = brand().fonts.heading, brand().fonts.body, brand().fonts.mono
FRONT_MATTER = re.compile(r"\A\s*---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.S)
META_FIELDS = ("type", "title", "subtitle", "number", "revision", "date", "status", "author", "reviewers",
               "project", "scope", "classification", "style", "to", "dear", "subject", "from_name", "from_role",
               "from_email", "from_phone", "sign_off")
WORD_TEMPLATE, LETTERHEAD = brand().templates.word, brand().templates.letter
BLOCK_ROWS = (("number", "Document number"), ("revision", "Revision"), ("date", "Date"), ("status", "Status"),
              ("project", "Project"), ("author", "Author"), ("reviewers", "Reviewers / approver"))
NUMERIC = re.compile(r"^[\s$A€£-]*[-+(]?[\d.,]+[)%]?\s*(k|m|mw|kw|kva|mva|kv|v|a|h|d|wk|weeks?|days?|%|x)?$", re.I)


def template_file(name: str) -> Path:
    """A file in the brand pack (a path that doesn't exist when the brand has none)."""
    return brand().file(name) or Path("/nonexistent") / (name or "none")


def is_letter(meta: dict) -> bool:
    return (meta.get("type") or "").strip().lower() == "letter"


def new_document(meta: dict):
    """A new branded document: the brand's Word template (or the letterhead for a letter) with its example content
    removed, so its page, styles, header and footer are kept. Falls back to a blank document styled in code.
    Returns (doc, templated)."""
    from docx import Document
    path = template_file(LETTERHEAD if is_letter(meta) else WORD_TEMPLATE)
    if not path.exists():
        doc = Document()
        apply_brand_styles(doc)
        return doc, False
    data = path.read_bytes()
    if path.suffix == ".dotx":  # a Word template: open it as a document (only the content type differs)
        import zipfile
        src, buf = zipfile.ZipFile(io.BytesIO(data)), io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
            for item in src.infolist():
                blob = src.read(item.filename)
                if item.filename == "[Content_Types].xml":
                    blob = blob.replace(b"wordprocessingml.template.main+xml", b"wordprocessingml.document.main+xml")
                out.writestr(item, blob)
        data = buf.getvalue()
    doc = Document(io.BytesIO(data))
    body = doc.element.body
    for el in list(body):
        if _local(el.tag) != "sectPr":
            body.remove(el)
    core = doc.core_properties
    core.title = core.subject = core.keywords = ""
    return doc, True


def text_width_cm(doc) -> float:
    sec = doc.sections[0]
    return round((sec.page_width - sec.left_margin - sec.right_margin) / 360000, 2)


def wordmark() -> Path:
    return brand().file(brand().wordmark) or Path("/nonexistent/wordmark.png")


# ---- front matter -----------------------------------------------------------------------------------------
def split_front_matter(text: str) -> tuple[dict, str]:
    """(meta, body). Unknown keys are kept (lower-cased); values are plain strings."""
    m = FRONT_MATTER.match(text or "")
    if not m:
        return {}, text or ""
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.lstrip().startswith("#"):
            k, v = line.split(":", 1)
            meta[k.strip().lower()] = v.strip().strip('"').strip("'")
    return meta, (text or "")[m.end():]


# ---- low-level XML helpers --------------------------------------------------------------------------------
# Child order the WordprocessingML schema requires (ECMA-376); Word calls a file with children out of order
# "unreadable content", so every element added here goes through _place.
ORDER = {
    "rPr": "rStyle rFonts b bCs i iCs caps smallCaps strike dstrike outline shadow emboss imprint noProof snapToGrid "
           "vanish webHidden color spacing w kern position sz szCs highlight u effect bdr shd fitText vertAlign rtl cs "
           "em lang eastAsianLayout specVanish oMath",
    "pPr": "pStyle keepNext keepLines pageBreakBefore framePr widowControl numPr suppressLineNumbers pBdr shd tabs "
           "suppressAutoHyphens kinsoku wordWrap overflowPunct topLinePunct autoSpaceDE autoSpaceDN bidi "
           "adjustRightInd snapToGrid spacing ind contextualSpacing mirrorIndents suppressOverlap jc textDirection "
           "textAlignment textboxTightWrap outlineLvl divId cnfStyle rPr sectPr pPrChange",
    "tcPr": "cnfStyle tcW gridSpan hMerge vMerge tcBorders shd noWrap tcMar textDirection tcFitText vAlign hideMark",
    "tblPr": "tblStyle tblpPr tblOverlap bidiVisual tblStyleRowBandSize tblStyleColBandSize tblW jc tblCellSpacing "
             "tblInd tblBorders shd tblLayout tblCellMar tblLook tblCaption tblDescription",
    "trPr": "cnfStyle divId gridBefore gridAfter wBefore wAfter cantSplit trHeight tblHeader tblCellSpacing jc hidden",
}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _place(parent, el):
    """Put `el` into `parent` in schema order, replacing an existing child of the same name."""
    seq = ORDER.get(_local(parent.tag), "").split()
    name = _local(el.tag)
    for old in [c for c in parent if _local(c.tag) == name]:
        parent.remove(old)
    if name in seq:
        later = set(seq[seq.index(name) + 1:])
        for i, child in enumerate(parent):
            if _local(child.tag) in later:
                parent.insert(i, el)
                return el
    parent.append(el)
    return el


def _el(tag: str, **attrs):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    el = OxmlElement(tag)
    for k, v in attrs.items():
        el.set(qn(k.replace("_", ":", 1)), str(v))
    return el


def _set_cell_border(cell, **edges):
    """edges: top/bottom/left/right = (size in eighths of a point, colour), or None for no line."""
    borders = _el("w:tcBorders")
    for edge in ("top", "left", "bottom", "right"):
        spec = edges.get(edge)
        borders.append(_el(f"w:{edge}", w_val="nil") if spec is None else
                       _el(f"w:{edge}", w_val="single", w_sz=spec[0], w_space=0, w_color=spec[1]))
    _place(cell._tc.get_or_add_tcPr(), borders)


def _cell_margins(table, top=60, bottom=60, left=0, right=120):
    from docx.oxml.ns import qn
    tbl_pr = table._tbl.tblPr
    mar = _place(tbl_pr, _el("w:tblCellMar"))
    for edge, v in (("top", top), ("left", left), ("bottom", bottom), ("right", right)):
        mar.append(_el(f"w:{edge}", w_w=v, w_type="dxa"))


def _no_table_borders(table):
    from docx.oxml.ns import qn
    borders = _el("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        borders.append(_el(f"w:{edge}", w_val="nil"))
    _place(table._tbl.tblPr, borders)


def _tracking(run, twentieths: int):
    """Letter-spacing (the brand's tracked small caps)."""
    _place(run._r.get_or_add_rPr(), _el("w:spacing", w_val=twentieths))


def _field(paragraph, instr: str, shown: str = "1"):
    for kind, text in (("begin", None), ("instr", instr), ("separate", None), ("text", shown), ("end", None)):
        run = paragraph.add_run()
        if kind == "instr":
            el = _el("w:instrText")
            from docx.oxml.ns import qn
            el.set(qn("xml:space"), "preserve")
            el.text = f" {text} "
            run._r.append(el)
        elif kind == "text":
            run.text = text
        else:
            run._r.append(_el("w:fldChar", w_fldCharType=kind))
    return paragraph


def _font(f, name=None, size=None, color=None, bold=None, italic=None):
    """Set a run's or style's font. A named font replaces the theme font slots, which otherwise win."""
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor
    if name:
        f.name = name
        r_fonts = f._element.get_or_add_rPr().get_or_add_rFonts()
        for slot in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
            r_fonts.set(qn(slot), name)
        for theme in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
            if r_fonts.get(qn(theme)) is not None:
                del r_fonts.attrib[qn(theme)]
    if size:
        f.size = Pt(size)
    if color:
        f.color.rgb = RGBColor.from_string(color)
    if bold is not None:
        f.bold = bold
    if italic is not None:
        f.italic = italic


# ---- the brand's document style -----------------------------------------------------------------------------
def apply_brand_styles(doc) -> None:
    """Page, styles and colours of the brand's document format (no content)."""
    from docx.enum.style import WD_STYLE_TYPE
    from docx.shared import Cm, Pt

    for sec in doc.sections:
        sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
        sec.left_margin = sec.right_margin = Cm(2.3)
        sec.top_margin, sec.bottom_margin = Cm(2.0), Cm(2.0)
        sec.header_distance = sec.footer_distance = Cm(1.0)
    # White page: documents print and are marked up, so no coloured ground

    styles = doc.styles
    normal = styles["Normal"]
    _font(normal.font, BODY_FONT, 10.5, INK)
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.12
    for name, size, before, after in (("Heading 1", 17, 18, 6), ("Heading 2", 13, 12, 4), ("Heading 3", 11, 10, 3)):
        st = styles[name]
        if name == "Heading 3":
            _font(st.font, BODY_FONT, size, PRIMARY, bold=True, italic=False)
        else:
            _font(st.font, HEADING_FONT, size, INK, bold=False, italic=False)
        st.paragraph_format.space_before, st.paragraph_format.space_after = Pt(before), Pt(after)
        st.paragraph_format.keep_with_next = True
    for name in ("Heading 4",):
        _font(styles[name].font, BODY_FONT, 10.5, INK, bold=True, italic=True)
    _font(styles["Title"].font, HEADING_FONT, 26, INK, bold=False)
    styles["Title"].paragraph_format.space_after = Pt(4)
    _border_none(styles["Title"])
    _font(styles["Subtitle"].font, BODY_FONT, 12, MUTED, italic=False)
    styles["Subtitle"].paragraph_format.space_after = Pt(16)
    _font(styles["Caption"].font, BODY_FONT, 9, MUTED, bold=False, italic=True)
    quote = styles["Quote"]
    _font(quote.font, HEADING_FONT, 11, INK, italic=True)
    quote.paragraph_format.left_indent = Cm(0.6)
    bdr = _el("w:pBdr")
    bdr.append(_el("w:left", w_val="single", w_sz=18, w_space=8, w_color=PRIMARY))
    _place(quote.element.get_or_add_pPr(), bdr)
    for name in ("List Bullet", "List Bullet 2", "List Number", "List Number 2"):
        try:
            styles[name].paragraph_format.space_after = Pt(3)
        except KeyError:
            pass
    try:
        hyper = styles["Hyperlink"]
    except KeyError:
        hyper = styles.add_style("Hyperlink", WD_STYLE_TYPE.CHARACTER)
    _font(hyper.font, color=PRIMARY)
    hyper.font.underline = True


def _border_none(style):
    from docx.oxml.ns import qn
    p_pr = style.element.get_or_add_pPr()
    old = p_pr.find(qn("w:pBdr"))
    if old is not None:
        p_pr.remove(old)


TEXT_WIDTH_CM = 16.4


def set_column_widths(table, widths_cm: list[float]) -> None:
    """Fixed column widths (Word and LibreOffice both honour these; autofit would spread columns evenly)."""
    from docx.shared import Cm
    table.autofit = False
    grid = table._tbl.tblGrid
    for i, gc in enumerate(grid.findall("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}gridCol")):
        if i < len(widths_cm):
            gc.set("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}w", str(int(widths_cm[i] * 567)))
    for row in table.rows:
        for i, cell in enumerate(row.cells):
            if i < len(widths_cm):
                cell.width = Cm(widths_cm[i])


def fit_columns(table, total_cm: float = TEXT_WIDTH_CM) -> None:
    """Share the text width by how much each column holds: short codes stay narrow, descriptions get room."""
    cols = len(table.columns)
    if not cols:
        return
    need = [0.0] * cols
    for row in table.rows:
        for i, cell in enumerate(row.cells[:cols]):
            longest_word = max((len(w) for w in cell.text.split()), default=0)
            need[i] = max(need[i], min(len(cell.text), 45) ** 0.75, longest_word * 0.55)
    floor = 1.6
    weights = [max(n, 1.0) for n in need]
    widths = [max(floor, total_cm * w / sum(weights)) for w in weights]
    scale = total_cm / sum(widths)
    set_column_widths(table, [w * scale for w in widths])


def style_table(table, header: bool = True, width_cm: float = TEXT_WIDTH_CM) -> None:
    """Branded table: no vertical lines; a primary-colour rule under the header and thin rules between rows; header
    in small tracked primary-colour capitals; figures in the mono font. With the brand's template, its table style
    draws all of that; only widths, the repeating header and mono figures are set here."""
    from docx.shared import Pt
    try:
        if not brand().table_style:
            raise KeyError("no table style")
        table.style = table._parent.part.document.styles[brand().table_style]
        templated = True
    except (KeyError, AttributeError):
        templated = False
    if templated:
        fit_columns(table, width_cm)
        for r, row in enumerate(table.rows):
            for cell in row.cells:
                for p in cell.paragraphs:
                    for run in p.runs:
                        if not (header and r == 0) and NUMERIC.match(run.text or "") and (run.text or "").strip():
                            _font(run.font, MONO_FONT)
            if header and r == 0:
                _place(row._tr.get_or_add_trPr(), _el("w:tblHeader"))  # repeats on each page
        return
    _no_table_borders(table)
    _cell_margins(table)
    fit_columns(table, width_cm)
    for r, row in enumerate(table.rows):
        for cell in row.cells:
            is_head = header and r == 0
            _set_cell_border(cell, top=None, left=None, right=None,
                             bottom=(8, PRIMARY) if is_head else (4, RULE))
            for p in cell.paragraphs:
                p.paragraph_format.space_after = Pt(0)
                for run in p.runs:
                    if is_head:
                        run.text = run.text.upper()
                        _font(run.font, BODY_FONT, 7.5, PRIMARY, bold=True)
                        _tracking(run, 20)
                    elif NUMERIC.match(run.text or "") and (run.text or "").strip():
                        _font(run.font, MONO_FONT, 9.5)
                    else:
                        _font(run.font, size=9.5)
        if header and r == 0:
            from docx.oxml.ns import qn
            _place(row._tr.get_or_add_trPr(), _el("w:tblHeader"))  # repeats on each page


def add_cover(doc, meta: dict, templated: bool = False) -> None:
    """Wordmark, eyebrow, title, subtitle and the title block, at the top of the body. With the brand's template the
    wordmark is already in its first-page header, so it isn't added again."""
    from docx.shared import Cm, Pt

    body = doc.element.body
    first = body[0] if len(body) else None
    added = []

    def para(style=None):
        p = doc.add_paragraph(style=style)
        added.append(p._p)
        return p

    mark = wordmark()
    width = text_width_cm(doc)
    if mark.exists() and not templated:
        p = para()
        p.add_run().add_picture(str(mark), height=Cm(0.75))
        p.paragraph_format.space_after = Pt(26)
    if meta.get("type"):
        p = para()
        run = p.add_run(meta["type"].upper())
        _font(run.font, BODY_FONT, 7.5, PRIMARY, bold=True)
        _tracking(run, 30)
        p.paragraph_format.space_after = Pt(4)
    title = para("Title")
    title.add_run(meta.get("title") or "Untitled")
    if meta.get("subtitle"):
        para("Subtitle").add_run(meta["subtitle"])
    rows = [(label, meta[key]) for key, label in BLOCK_ROWS if meta.get(key)]
    if rows:
        table = doc.add_table(rows=len(rows), cols=2)
        added.append(table._tbl)
        _no_table_borders(table)
        _cell_margins(table, top=50, bottom=50)
        set_column_widths(table, [3.8, width - 3.8])
        for (label, value), row in zip(rows, table.rows):
            lab = row.cells[0].paragraphs[0].add_run(label.upper())
            _font(lab.font, BODY_FONT, 7, PRIMARY, bold=True)
            _tracking(lab, 20)
            val = row.cells[1].paragraphs[0].add_run(value)
            mono = label in ("Document number", "Date") or (label == "Revision" and len(value) <= 3)
            _font(val.font, MONO_FONT if mono else BODY_FONT, 9.5 if mono else 10, INK)
            for cell in row.cells:
                cell.paragraphs[0].paragraph_format.space_after = Pt(0)
                _set_cell_border(cell, top=(4, RULE), bottom=(4, RULE), left=None, right=None)
        spacer = para()
        spacer.paragraph_format.space_after = Pt(10)
    if meta.get("scope"):  # the organisation's house style: a scope note says what is and isn't covered
        head = para()
        _font(head.add_run("Scope note").font, HEADING_FONT, 12, INK)
        head.paragraph_format.space_after = Pt(3)
        para().add_run(meta["scope"])
    if first is not None:  # the cover goes before anything already in the body
        for el in added:
            first.addprevious(el)


def add_header_footer(doc, meta: dict) -> None:
    from docx.enum.text import WD_TAB_ALIGNMENT
    from docx.shared import Cm

    label_bits = [b for b in (brand().name, meta.get("number"), meta.get("title")) if b]
    if meta.get("revision"):
        label_bits.append(f"Rev {meta['revision']}")
    classification = meta.get("classification") or brand().classification
    for sec in doc.sections:
        sec.different_first_page_header_footer = True
        width = sec.page_width - sec.left_margin - sec.right_margin
        for footer in (sec.footer, sec.first_page_footer):
            p = footer.paragraphs[0]
            p.text = ""
            p.style = doc.styles["Normal"]  # the Footer style's centre tab would catch the page number
            p.paragraph_format.space_after = Cm(0)
            p.paragraph_format.tab_stops.add_tab_stop(width, WD_TAB_ALIGNMENT.RIGHT)
            run = p.add_run(" · ".join(label_bits[:3]) + f" · {classification}")
            _font(run.font, BODY_FONT, 7.5, MUTED)
            p.add_run("\t")
            start = len(p.runs)
            p.add_run("Page ")
            _field(p, "PAGE")
            p.add_run(" of ")
            _field(p, "NUMPAGES")
            for run in p.runs[start:]:
                _font(run.font, BODY_FONT, 7.5, MUTED)
            _set_paragraph_top_rule(p)
        hp = sec.header.paragraphs[0]  # pages 2 onwards: a quiet running header
        hp.text = ""
        hp.style = doc.styles["Normal"]
        hp.paragraph_format.space_after = Cm(0)
        hp.paragraph_format.tab_stops.add_tab_stop(width, WD_TAB_ALIGNMENT.RIGHT)
        run = hp.add_run(brand().name)
        _font(run.font, BODY_FONT, 8, INK, bold=True)
        right = " · ".join(b for b in (meta.get("number"), f"Rev {meta['revision']}" if meta.get("revision") else None,
                                         meta.get("date")) if b)
        if right:
            hp.add_run("\t")
            r2 = hp.add_run(right)
            _font(r2.font, MONO_FONT, 7.5, MUTED)
        sec.header_distance = Cm(1.0)


def fill_template_footer(doc, meta: dict) -> None:
    """The template's footer: its document-title field gets the title (the field is bound to the document's Title
    property, which Word and LibreOffice show in its place), and its classification words are replaced when the
    front matter gives one. Page numbers are the template's own."""
    from docx.oxml.ns import qn
    label = meta.get("title") or ""
    for sec in doc.sections:
        for part in {id(x): x for x in (sec.footer, sec.first_page_footer, sec.even_page_footer)}.values():
            if part.is_linked_to_previous and part is not sec.footer:
                continue
            for p in part.paragraphs:
                for sdt in p._p.iter(qn("w:sdt")):
                    texts = list(sdt.iter(qn("w:t")))
                    if texts and label:
                        texts[0].text = label
                        for t in texts[1:]:
                            t.text = ""
                if meta.get("classification"):
                    for t in p._p.iter(qn("w:t")):
                        if (t.text or "").strip().lower() == brand().classification.lower():
                            t.text = meta["classification"]


def fill_letterhead(doc, meta: dict) -> None:
    """The letterhead's contact lines (first-page header): the sender's email and phone, or removed."""
    from docx.oxml.ns import qn
    ph = brand().letterhead_placeholders
    swaps = {v: meta.get(f"from_{k}") for k, v in ph.items() if v}  # the letterhead's placeholder lines
    for sec in doc.sections:
        for p in list(sec.first_page_header._element.iter(qn("w:p"))):  # the contact lines sit in a table cell
            runs = list(p.iter(qn("w:t")))
            text = "".join(t.text or "" for t in runs).strip()
            if text in swaps:
                if swaps[text]:
                    runs[0].text = swaps[text]
                    for t in runs[1:]:
                        t.text = ""
                else:
                    p.getparent().remove(p)


def add_letter_parts(doc, meta: dict) -> None:
    """Date, recipient, salutation and subject before the body; sign-off after it (the letterhead's own styles)."""
    from docx.shared import Pt
    body = doc.element.body
    first = body[0] if len(body) else None
    added = []

    def para(text="", style=None, space_before=None):
        try:
            p = doc.add_paragraph(text, style=style)
        except KeyError:
            p = doc.add_paragraph(text)
        if space_before is not None:
            p.paragraph_format.space_before = Pt(space_before)
        added.append(p._p)
        return p

    para(meta.get("date") or date.today().strftime("%-d %B %Y"), "Meta")
    lines = [x.strip() for x in (meta.get("to") or "").split(";") if x.strip()]
    for k, line in enumerate(lines):
        p = para(line, space_before=12 if k == 0 else None)
        p.paragraph_format.space_after = Pt(0)
    dear = meta.get("dear") or (lines[0].split()[0] if lines else "")
    para(f"Dear {dear}," if dear else "Dear Sir or Madam,", space_before=18)
    if meta.get("subject") or meta.get("title"):
        para(meta.get("subject") or meta.get("title"), "Heading 2")
    if first is not None:
        for el in added:
            first.addprevious(el)
    para(f"{meta.get('sign_off') or 'Kind regards'},", space_before=12)
    if meta.get("from_name"):
        para(meta["from_name"], "Signature name", space_before=24)
    if meta.get("from_role"):
        para(f"{meta['from_role']} · {brand().name}")


def _set_paragraph_top_rule(p):
    bdr = _el("w:pBdr")
    bdr.append(_el("w:top", w_val="single", w_sz=4, w_space=6, w_color=RULE))
    _place(p._p.get_or_add_pPr(), bdr)


def finish_branded_document(doc, meta: dict, templated: bool = False) -> None:
    """Called by filemaker.docx_from_markdown after the body is built."""
    meta = dict(meta)
    meta.setdefault("date", date.today().strftime("%-d %B %Y"))
    width = text_width_cm(doc)
    for table in doc.tables:
        style_table(table, width_cm=width)
    from docx.oxml.ns import qn
    for link in doc.element.body.iter(qn("w:hyperlink")):  # links in the primary colour, not Word blue
        for color in link.iter(qn("w:color")):
            color.set(qn("w:val"), PRIMARY)
    if templated and is_letter(meta):
        add_letter_parts(doc, meta)
        fill_letterhead(doc, meta)
    elif templated:
        add_cover(doc, meta, templated=True)
        fill_template_footer(doc, meta)
    else:
        add_cover(doc, meta)
        add_header_footer(doc, meta)
    core = doc.core_properties
    core.title = meta.get("title") or ""
    core.subject = meta.get("subtitle") or ""
    core.author = meta.get("author") or brand().name
    core.keywords = " ".join(b for b in (meta.get("number"), meta.get("revision")) if b)


# ---- outline (what's in a document, for editing) ----------------------------------------------------------
def outline(data: bytes) -> dict:
    from docx import Document
    doc = Document(io.BytesIO(data))
    heads, tables = [], []
    for p in doc.paragraphs:
        name = p.style.name if p.style is not None else ""
        if name.startswith("Heading") or name == "Title":
            level = 0 if name == "Title" else int(re.sub(r"\D", "", name) or 1)
            heads.append({"level": level, "text": p.text.strip()[:120]})
    for i, t in enumerate(doc.tables, 1):
        first = [c.text.strip()[:30] for c in t.rows[0].cells] if t.rows else []
        tables.append({"table": i, "rows": len(t.rows), "columns": len(t.columns), "first_row": first})
    comments = []
    try:
        comments = [{"id": c.comment_id, "author": c.author, "text": c.text[:200]} for c in doc.comments]
    except Exception:
        pass
    return {"kind": "Word document", "paragraphs": len(doc.paragraphs), "headings": heads, "tables": tables,
            "comments": comments, "tracked_changes": _count_tracked(doc)}


def _count_tracked(doc) -> int:
    from docx.oxml.ns import qn
    body = doc.element.body
    return sum(1 for _ in body.iter(qn("w:ins"))) + sum(1 for _ in body.iter(qn("w:del")))


# ---- editing --------------------------------------------------------------------------------------------
class _Tracker:
    """Word tracked-change markup by one author."""

    def __init__(self, author: str):
        self.author, self.next_id = author, 9000
        from datetime import datetime, timezone
        self.when = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _wrap(self, tag: str):
        self.next_id += 1
        return _el(tag, w_id=self.next_id, w_author=self.author, w_date=self.when)

    def delete_run(self, r):
        """Turn a run into a deleted run (its w:t become w:delText)."""
        from docx.oxml.ns import qn
        for t in r.findall(qn("w:t")):
            t.tag = qn("w:delText")
        wrap = self._wrap("w:del")
        r.addprevious(wrap)
        wrap.append(r)
        return wrap

    def insert_run(self, r):
        wrap = self._wrap("w:ins")
        r.addprevious(wrap)
        wrap.append(r)
        return wrap

    def mark_paragraph(self, p_el, kind: str):
        """The whole paragraph inserted or deleted (runs and the paragraph mark)."""
        from docx.oxml.ns import qn
        for r in list(p_el.findall(qn("w:r"))):
            self.delete_run(r) if kind == "del" else self.insert_run(r)
        p_pr = p_el.find(qn("w:pPr"))
        if p_pr is None:
            p_pr = _el("w:pPr")
            p_el.insert(0, p_pr)
        r_pr = p_pr.find(qn("w:rPr"))
        if r_pr is None:
            r_pr = _place(p_pr, _el("w:rPr"))
        r_pr.insert(0, self._wrap("w:" + kind))  # ins/del lead a paragraph mark's properties


def _paragraphs_everywhere(doc):
    """Body paragraphs (incl. inside tables), then headers and footers."""
    def walk(container):
        for p in container.paragraphs:
            yield p
        for t in getattr(container, "tables", []):
            for row in t.rows:
                for cell in row.cells:
                    yield from walk(cell)
    yield from walk(doc)
    for sec in doc.sections:
        for part in (sec.header, sec.footer, sec.first_page_header, sec.first_page_footer):
            if part is not None and not part.is_linked_to_previous:
                yield from walk(part)


def _replace_in_paragraph(p, find: str, replace: str, tracker: _Tracker | None) -> int:
    """Replace `find` in a paragraph. Within one run the run's formatting is kept; across runs the matched text
    is rebuilt in the first run's formatting. Returns the count."""
    count = 0
    while True:
        runs = p.runs
        text = "".join(r.text for r in runs)
        at = text.find(find)
        if at < 0 or not find:
            return count
        # locate the runs covering [at, at+len)
        pos, first, last = 0, None, None
        offs = []
        for i, r in enumerate(runs):
            offs.append(pos)
            if first is None and pos + len(r.text) > at:
                first = i
            if pos + len(r.text) >= at + len(find):
                last = i
                break
            pos += len(r.text)
        if first is None or last is None:
            return count
        start_in_first = at - offs[first]
        end_in_last = at + len(find) - offs[last]
        if tracker is None:
            if first == last:
                r = runs[first]
                r.text = r.text[:start_in_first] + replace + r.text[end_in_last:]
            else:
                runs[first].text = runs[first].text[:start_in_first] + replace
                for r in runs[first + 1:last]:
                    r.text = ""
                runs[last].text = runs[last].text[end_in_last:]
        else:
            _tracked_replace(p, runs, first, last, start_in_first, end_in_last, replace, tracker)
        count += 1
        if count > 500:
            return count


def _split_run(r, at: int):
    """Split a run's text at `at`; returns (left_el, right_el) sharing its formatting."""
    right = copy.deepcopy(r._r)
    text = r.text
    r.text = text[:at]
    r._r.addnext(right)
    from docx.text.run import Run
    Run(right, r._parent).text = text[at:]
    return r._r, right


def _tracked_replace(p, runs, first, last, start, end, replace, tracker):
    from docx.text.run import Run
    # isolate the matched text into whole runs: split the first and the last at the match edges
    if first == last:
        r = runs[first]
        _, mid = _split_run(r, start)
        mid_run = Run(mid, r._parent)
        _, after = _split_run(mid_run, end - start)
        doomed = [mid]
    else:
        _, mid_first = _split_run(runs[first], start)
        last_run = runs[last]
        left_last, _ = _split_run(last_run, end)
        doomed = [mid_first] + [runs[i]._r for i in range(first + 1, last)] + [left_last]
    new = copy.deepcopy(doomed[0])
    Run(new, p).text = replace
    for el in doomed:
        tracker.delete_run(el)
    if replace:
        # the new run goes after the last deleted one
        last_del = doomed[-1].getparent()
        last_del.addnext(new)
        tracker.insert_run(new)


def _body_blocks(doc):
    """Top-level paragraphs and tables of the body, in order."""
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    from docx.oxml.ns import qn
    for el in doc.element.body.iterchildren():
        if el.tag == qn("w:p"):
            yield Paragraph(el, doc)
        elif el.tag == qn("w:tbl"):
            yield Table(el, doc)


def _heading_level(p) -> int | None:
    name = p.style.name if getattr(p, "style", None) is not None else ""
    if name == "Title":
        return 0
    if name.startswith("Heading"):
        digits = re.sub(r"\D", "", name)
        return int(digits) if digits else 1
    return None


def _find_heading(doc, heading: str):
    want = heading.strip().lower()
    blocks = list(_body_blocks(doc))
    paras = [b for b in blocks if hasattr(b, "style") and _heading_level(b) is not None]
    hit = next((p for p in paras if p.text.strip().lower() == want), None) or next(
        (p for p in paras if want in p.text.strip().lower()), None)
    if hit is None:
        raise ValueError(f"no heading {heading!r}. Headings: " +
                         "; ".join(p.text.strip() for p in paras[:40]) or "none")
    return hit, blocks


def _section_after(doc, heading: str):
    """(heading paragraph, the body elements under it up to the next heading at its level or higher)."""
    hit, blocks = _find_heading(doc, heading)
    level = _heading_level(hit)
    i = next(k for k, b in enumerate(blocks) if getattr(b, "_p", None) is hit._p)
    out = []
    for b in blocks[i + 1:]:
        lv = _heading_level(b) if hasattr(b, "style") and not hasattr(b, "rows") else None
        if lv is not None and lv <= level:
            break
        if b._element.tag.endswith("sectPr"):
            break
        out.append(b)
    return hit, out


def _markdown_elements(doc, markdown: str):
    """Body elements for `markdown`, built with the same converter, restyled to `doc`'s style names."""
    from docx import Document

    from .filemaker import docx_from_markdown
    built = Document(io.BytesIO(docx_from_markdown(markdown, None, plain=True)))
    els = [copy.deepcopy(el) for el in built.element.body.iterchildren() if not el.tag.endswith("sectPr")]
    return els


def _insert_elements_after(anchor_el, els) -> None:
    for el in els:
        anchor_el.addnext(el)
        anchor_el = el


def edit_docx(data: bytes, ops: list[dict], author: str = "", track_changes: bool = False) -> tuple[bytes, list[str]]:
    """Apply edit operations to a .docx. Returns (new bytes, what changed). Raises ValueError on a bad op."""
    from .org import agent
    author = author or agent().display_name
    from docx import Document
    doc = Document(io.BytesIO(data))
    tracker = _Tracker(author) if track_changes else None
    done: list[str] = []
    for n, op in enumerate(ops or [], 1):
        kind = (op.get("op") or "").strip()
        if kind == "replace_text":
            find, rep = op.get("find") or "", op.get("replace", "")
            if not find:
                raise ValueError(f"edit {n}: replace_text needs `find`")
            count = 0
            for p in _paragraphs_everywhere(doc):
                if find in p.text:
                    count += _replace_in_paragraph(p, find, rep, tracker)
                    if count and not op.get("all", True):
                        break
            if not count:
                raise ValueError(f"edit {n}: {find!r} isn't in the document (check spelling, or that it isn't split "
                                 f"by a line break)")
            done.append(f"replaced {find!r} → {rep!r} ({count}×)")
        elif kind in ("insert", "insert_markdown"):
            md = op.get("markdown") or op.get("text") or ""
            if not md.strip():
                raise ValueError(f"edit {n}: insert needs `markdown`")
            els = _markdown_elements(doc, md)
            if op.get("after_heading") or op.get("section"):
                hit, under = _section_after(doc, op.get("after_heading") or op.get("section"))
                anchor = under[-1]._element if under and op.get("at", "end") == "end" else hit._p
                where = f"at the end of section {hit.text.strip()!r}" if under and op.get("at", "end") == "end" \
                    else f"after heading {hit.text.strip()!r}"
            elif op.get("after"):
                want = op["after"].lower()
                p = next((p for p in doc.paragraphs if want in p.text.lower()), None)
                if p is None:
                    raise ValueError(f"edit {n}: no paragraph contains {op['after']!r}")
                anchor, where = p._p, f"after {p.text.strip()[:60]!r}"
            else:
                body = doc.element.body
                last = [el for el in body.iterchildren() if not el.tag.endswith("sectPr")]
                anchor, where = last[-1], "at the end"
            _insert_elements_after(anchor, els)
            if tracker:
                for el in els:
                    for p_el in ([el] if el.tag.endswith("}p") else el.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p")):
                        tracker.mark_paragraph(p_el, "ins")
            done.append(f"inserted {len(els)} block(s) {where}")
        elif kind in ("replace_section", "delete_section"):
            hit, under = _section_after(doc, op.get("heading") or "")
            for b in under:
                if tracker:
                    for p_el in b._element.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
                        tracker.mark_paragraph(p_el, "del")
                else:
                    b._element.getparent().remove(b._element)
            if kind == "delete_section" and op.get("include_heading", True):
                if tracker:
                    tracker.mark_paragraph(hit._p, "del")
                else:
                    hit._p.getparent().remove(hit._p)
                done.append(f"deleted section {hit.text.strip()!r} ({len(under)} block(s))")
                continue
            if kind == "replace_section":
                md = op.get("markdown") or ""
                els = _markdown_elements(doc, md) if md.strip() else []
                anchor = under[-1]._element if (tracker and under) else hit._p
                _insert_elements_after(anchor, els)
                if tracker:
                    for el in els:
                        for p_el in ([el] if el.tag.endswith("}p") else el.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p")):
                            tracker.mark_paragraph(p_el, "ins")
                done.append(f"rewrote section {hit.text.strip()!r}")
            else:
                done.append(f"emptied section {hit.text.strip()!r}")
        elif kind in ("set_table_cell", "add_table_row"):
            ti = int(op.get("table") or 0)
            if not 1 <= ti <= len(doc.tables):
                raise ValueError(f"edit {n}: there are {len(doc.tables)} tables; `table` is 1-based")
            table = doc.tables[ti - 1]
            if kind == "set_table_cell":
                r, c = int(op.get("row") or 0), int(op.get("col") or 0)
                if not (1 <= r <= len(table.rows) and 1 <= c <= len(table.columns)):
                    raise ValueError(f"edit {n}: table {ti} is {len(table.rows)} rows × {len(table.columns)} columns "
                                     f"(1-based)")
                cell = table.cell(r - 1, c - 1)
                old = cell.text
                para = cell.paragraphs[0]
                if tracker:
                    for run in list(para.runs):
                        tracker.delete_run(run._r)
                    new = para.add_run(str(op.get("text", "")))
                    tracker.insert_run(new._r)
                else:
                    keep = para.runs[0] if para.runs else None
                    for run in para.runs[1:]:
                        run._r.getparent().remove(run._r)
                    for extra in cell.paragraphs[1:]:
                        extra._p.getparent().remove(extra._p)
                    if keep is not None:
                        keep.text = str(op.get("text", ""))
                    else:
                        para.add_run(str(op.get("text", "")))
                done.append(f"table {ti} row {r} col {c}: {old[:40]!r} → {str(op.get('text', ''))[:40]!r}")
            else:
                values = op.get("values") or []
                src = table.rows[-1]._tr
                new_tr = copy.deepcopy(src)
                src.addnext(new_tr)
                from docx.table import _Row
                row = _Row(new_tr, table)
                for c, cell in enumerate(row.cells):
                    para = cell.paragraphs[0]
                    for extra in cell.paragraphs[1:]:
                        extra._p.getparent().remove(extra._p)
                    runs = para.runs
                    for run in runs[1:]:
                        run._r.getparent().remove(run._r)
                    text = str(values[c]) if c < len(values) else ""
                    if runs:
                        runs[0].text = text
                    else:
                        para.add_run(text)
                    if tracker:
                        for run in para.runs:
                            tracker.insert_run(run._r)
                done.append(f"table {ti}: added a row ({', '.join(str(v) for v in values)[:60]})")
        elif kind == "comment":
            on, text = op.get("on") or "", op.get("text") or ""
            if not on or not text:
                raise ValueError(f"edit {n}: comment needs `on` (text in the document) and `text`")
            target = next((p for p in _paragraphs_everywhere(doc) if on in p.text), None)
            if target is None:
                raise ValueError(f"edit {n}: {on!r} isn't in the document")
            runs = _isolate(target, on)
            doc.add_comment(runs, text=text, author=author, initials="".join(w[0] for w in author.split()[:2]).upper())
            done.append(f"comment on {on[:40]!r}")
        else:
            raise ValueError(f"edit {n}: unknown op {kind!r}. Ops: replace_text, insert, replace_section, "
                             f"delete_section, set_table_cell, add_table_row, comment")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue(), done


def _isolate(p, text: str):
    """The runs that exactly cover `text` in paragraph p (splitting runs at the edges)."""
    from docx.text.run import Run
    runs = p.runs
    full = "".join(r.text for r in runs)
    at = full.find(text)
    pos, first, last, offs = 0, None, None, []
    for i, r in enumerate(runs):
        offs.append(pos)
        if first is None and pos + len(r.text) > at:
            first = i
        if pos + len(r.text) >= at + len(text):
            last = i
            break
        pos += len(r.text)
    start, end = at - offs[first], at + len(text) - offs[last]
    if first == last:
        _, mid = _split_run(runs[first], start)
        mid_run = Run(mid, p)
        _split_run(mid_run, end - start)
        return [mid_run]
    _, mf = _split_run(runs[first], start)
    ll, _ = _split_run(runs[last], end)
    return [Run(mf, p)] + runs[first + 1:last] + [Run(ll, p)]
