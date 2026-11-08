"""Files the agents create from text they write (files.py `create_file`).

  .docx   from Markdown: headings (#, ##, ###), paragraphs, **bold**, *italic*, `code` and [links](https://...),
          bullet and numbered lists (indent two spaces to nest), tables (| a | b |), > quotes, ``` code blocks,
          figures and a page break line (<!-- pagebreak -->), as a real Word document (Word styles, so branded
          templates can restyle it). Figures are numbered and captioned from the alt text:
            ```svg ... ```         an SVG drawn inline (vector-art skill)
            ![Site layout](ref)    an image: an .svg or picture made earlier, sent in, or a SharePoint link
          SVG goes in as vectors (Word 2016+ / 365) with a PNG fallback (vector.py).
  .docx   in the brand's document style (docbuild.py) with a cover and title block from optional front matter
  .xlsx   from Markdown tables (a sheet per "## name"), CSV, or a JSON workbook spec (sheets.py), branded
  .pptx   from Markdown or a JSON slide spec, on the brand's master deck layouts (deck.py)
  .svg    SVG markup, checked and made safe (vector.clean_svg)
  .png    rendered from SVG markup
  .md .txt .csv .json .html .xml .yaml   the text as UTF-8

Anything else is refused: an agent should never produce a binary it can't build properly.
"""

from __future__ import annotations

import csv
import io
import re
from .brandpack import brand

TEXT_TYPES = {"md", "txt", "csv", "json", "html", "htm", "xml", "yaml", "yml", "ics"}
MAX_FILE_BYTES = 10 * 1024 * 1024
_RUN = re.compile(r"(\[[^\]]+\]\(https?://[^)\s]+\)|\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`)")
_LINK = re.compile(r"^\[([^\]]+)\]\((https?://[^)\s]+)\)$")
IMAGE_LINE = re.compile(r"^!\[([^\]]*)\]\(\s*([^)]+?)\s*\)$")
PAGE_BREAK = re.compile(r"^(<!--\s*pagebreak\s*-->|\\pagebreak|\\newpage)$", re.I)
FIGURE_WIDTH_CM = 16.0


def image_refs(markdown: str) -> list[str]:
    """The image references (![..](ref)) in a Markdown document, for the caller to fetch before building it."""
    return [m.group(2) for line in (markdown or "").splitlines() if (m := IMAGE_LINE.match(line.strip()))]
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _add_hyperlink(paragraph, text: str, url: str) -> None:
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    rid = paragraph.part.relate_to(url, RT.HYPERLINK, is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), rid)
    run = OxmlElement("w:r")
    props = OxmlElement("w:rPr")
    style = OxmlElement("w:rStyle")
    style.set(qn("w:val"), "Hyperlink")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "0563C1")
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    props.extend([style, color, underline])
    t = OxmlElement("w:t")
    t.text = text
    t.set(qn("xml:space"), "preserve")
    run.extend([props, t])
    link.append(run)
    paragraph._p.append(link)


def _add_runs(paragraph, text: str) -> None:
    for part in _RUN.split(text):
        if not part:
            continue
        if link := _LINK.match(part):
            _add_hyperlink(paragraph, link.group(1), link.group(2))
        elif part.startswith("**") and part.endswith("**"):
            paragraph.add_run(part[2:-2]).bold = True
        elif part.startswith("`") and part.endswith("`"):
            run = paragraph.add_run(part[1:-1])
            run.font.name = brand().fonts.mono
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            paragraph.add_run(part[1:-1]).italic = True
        else:
            paragraph.add_run(part)


def _clear(cell) -> None:
    """Empty a new table cell without leaving an empty run behind (cell.text = "" leaves one)."""
    for run in list(cell.paragraphs[0].runs):
        run._r.getparent().remove(run._r)


def _style(doc, name: str, fallback: str | None = None):
    try:
        return doc.styles[name]
    except KeyError:
        return doc.styles[fallback] if fallback else None


def add_figure(doc, data: bytes, name: str, caption: str, number: int, width_cm: float | None = None) -> None:
    """A centred picture (SVG as vectors, else the raster as it is) and, with a caption, 'Figure n: caption'."""
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    para = doc.add_paragraph()
    para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    insert_picture(para.add_run(), data, name, width_cm)
    if caption:
        add_caption(doc.add_paragraph(), number, caption)


def add_caption(paragraph, number: int, caption: str) -> None:
    """'Figure n: caption' with n as a Word SEQ field, so Word renumbers figures added or moved later."""
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    paragraph.style = _style(paragraph.part.document, "Caption", "Normal")
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    caption = re.sub(r"^figure\s*\d*\s*[:.-]?\s*", "", caption, flags=re.I)
    paragraph.add_run("Figure ")

    def fld_char(kind: str) -> None:
        el = OxmlElement("w:fldChar")
        el.set(qn("w:fldCharType"), kind)
        paragraph.add_run()._r.append(el)

    fld_char("begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = " SEQ Figure \\* ARABIC "
    paragraph.add_run()._r.append(instr)
    fld_char("separate")
    paragraph.add_run(str(number))  # what shows until Word updates fields
    fld_char("end")
    paragraph.add_run(f": {caption}")


def insert_picture(run, data: bytes, name: str, width_cm: float | None = None) -> None:
    from docx.shared import Cm

    from .vector import add_svg_picture, clean_svg

    if name.lower().endswith(".svg") or data.lstrip()[:5] in (b"<svg ", b"<?xml"):
        add_svg_picture(run, clean_svg(data.decode("utf-8", "replace")), width_cm, FIGURE_WIDTH_CM)
        return
    from PIL import Image

    with Image.open(io.BytesIO(data)) as im:
        px_w, dpi = im.width, (im.info.get("dpi") or (96, 96))[0] or 96
        if (im.format or "").upper() not in ("PNG", "JPEG", "GIF", "BMP", "TIFF"):
            buf = io.BytesIO()
            im.convert("RGBA").save(buf, "PNG")
            data = buf.getvalue()
    natural = px_w / dpi * 2.54
    run.add_picture(io.BytesIO(data), width=Cm(min(width_cm or natural, FIGURE_WIDTH_CM)))


def docx_from_markdown(text: str, images: dict[str, tuple[str, bytes]] | None = None, plain: bool = False) -> bytes:
    """`images` maps each ![..](ref) in the text to (file name, bytes) (image_refs; the caller fetches them).
    In the brand's document style (docbuild.py) unless `plain` or the front matter says `style: plain`."""
    from docx import Document
    from docx.enum.text import WD_BREAK

    from .docbuild import finish_branded_document, is_letter, new_document, split_front_matter

    meta, text = split_front_matter(text or "")
    branded = not plain and meta.get("style", "").lower() != "plain"
    doc, templated = new_document(meta) if branded else (Document(), False)
    lines = (text or "").replace("\r\n", "\n").split("\n")
    h1s = [ln for ln in lines if re.match(r"^#\s+\S", ln.strip())]
    # On the branded cover the title isn't a heading: when the title is the only "# " (or comes from the front
    # matter and there's no "# " at all), "## Section" becomes Heading 1, as engineering documents number them.
    shift = 1 if branded and not is_letter(meta) and (len(h1s) <= (0 if meta.get("title") else 1)) else 0
    figures = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        if stripped.startswith("```"):  # a fenced block: an SVG figure, or code
            info = stripped[3:].strip()
            lang = info.lower()
            body = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            i += 1
            caption = ""
            if lang.startswith("svg"):
                caption = info[3:].strip(" :")  # ```svg Single-line diagram
                figures += 1
                add_figure(doc, "\n".join(body).encode(), "figure.svg", caption, figures)
                continue
            for code_line in body or [""]:
                run = doc.add_paragraph(style=_style(doc, "No Spacing", "Normal")).add_run(code_line or " ")
                run.font.name = brand().fonts.mono
            continue
        if m := IMAGE_LINE.match(stripped):
            alt, ref = m.group(1).strip(), m.group(2)
            if images and ref in images:
                figures += 1
                name, data = images[ref]
                add_figure(doc, data, name, alt, figures)
            else:
                doc.add_paragraph(f"[Image not available: {alt or ref}]")
            i += 1
            continue
        if PAGE_BREAK.match(stripped):
            doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
            i += 1
            continue
        if stripped.startswith(">"):
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip().lstrip(">").strip())
                i += 1
            _add_runs(doc.add_paragraph(style=_style(doc, "Quote", "Normal")), " ".join(quote))
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m and branded and len(m.group(1)) == 1 and not meta.get("title"):
            meta["title"] = m.group(2).strip()  # the first "# heading" is the document's title (cover)
            i += 1
            continue
        if m:
            level = max(1, len(m.group(1)) - shift)
            doc.add_heading(m.group(2).strip(), level=min(level, 4))
            i += 1
            continue
        if stripped.startswith("|") and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            header = _cells(stripped)
            rows = []
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            table = doc.add_table(rows=1 + len(rows), cols=len(header))
            table.style = "Table Grid"
            for c, value in enumerate(header):
                cell = table.rows[0].cells[c]
                _clear(cell)
                _add_runs(cell.paragraphs[0], f"**{value}**" if value else "")
            for r, row in enumerate(rows, start=1):
                for c in range(len(header)):
                    cell = table.rows[r].cells[c]
                    _clear(cell)
                    _add_runs(cell.paragraphs[0], row[c] if c < len(row) else "")
            continue
        nested = " 2" if len(line) - len(line.lstrip()) >= 2 else ""
        m = re.match(r"^[-*+]\s+(.*)$", stripped)
        if m:
            _add_runs(doc.add_paragraph(style=_style(doc, "List Bullet" + nested, "List Bullet")), m.group(1))
            i += 1
            continue
        m = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if m:
            _add_runs(doc.add_paragraph(style=_style(doc, "List Number" + nested, "List Number")), m.group(1))
            i += 1
            continue
        para = [stripped]  # a paragraph runs until a blank line or another block
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(r"^(#{1,6}\s|[-*+]\s|\d+[.)]\s|\||```|>|!\[)",
                                                                    lines[i].strip()):
            para.append(lines[i].strip())
            i += 1
        _add_runs(doc.add_paragraph(), " ".join(para))
    if branded:
        finish_branded_document(doc, meta, templated)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def rows_from_text(text: str) -> list[list[str]]:
    """A Markdown table or CSV as rows."""
    lines = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
    if lines and lines[0].strip().startswith("|"):
        return [_cells(ln) for ln in lines if not _TABLE_SEP.match(ln)]
    return [row for row in csv.reader(io.StringIO("\n".join(lines)))]


def xlsx_from_text(text: str) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    rows = rows_from_text(text)
    if not rows:
        raise ValueError("an .xlsx needs a table: a Markdown table or CSV lines")
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append([_number(v) for v in r])
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for col in ws.columns:
        width = max(len(str(c.value or "")) for c in col)
        ws.column_dimensions[col[0].column_letter].width = min(max(10, width + 2), 60)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _number(v: str):
    s = (v or "").strip().replace(",", "")
    try:
        return int(s) if re.fullmatch(r"-?\d+", s) else float(s) if re.fullmatch(r"-?\d+\.\d+", s) else v
    except ValueError:
        return v


def make_file(filename: str, content: str, images: dict[str, tuple[str, bytes]] | None = None) -> bytes:
    """Bytes for `filename` built from `content`. Raises ValueError for types it can't build."""
    name = (filename or "").strip()
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ValueError("give a plain file name such as 'PRJ-plan.docx'")
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext == "docx":
        data = docx_from_markdown(content, images)
    elif ext == "svg":
        from .vector import clean_svg
        data = clean_svg(content).encode("utf-8")
    elif ext == "png":
        from .vector import clean_svg, svg_to_png
        data = svg_to_png(clean_svg(content))
    elif ext == "xlsx":
        from .sheets import xlsx_from_content
        data = xlsx_from_content(content)
    elif ext == "pptx":
        from .deck import build_deck, markdown_to_deck
        data = build_deck(markdown_to_deck(content), images)
    elif ext in TEXT_TYPES:
        data = (content or "").encode("utf-8")
    else:
        raise ValueError(f"I can create .docx, .xlsx, .pptx, .svg, .png (from SVG) and text files "
                         f"({', '.join(sorted(TEXT_TYPES))}), not .{ext or '?'}")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("that file would be over 10 MB")
    return data


def insert_into_document(doc_data: bytes, doc_name: str, img_data: bytes, img_name: str, after: str = "",
                         caption: str = "", width_cm: float | None = None) -> tuple[bytes, str]:
    """(new bytes, where it went) with the picture placed in an existing .docx or on a new slide in a .pptx."""
    ext = doc_name.rsplit(".", 1)[-1].lower() if "." in doc_name else ""
    if ext == "pptx":
        return _insert_into_deck(doc_data, img_data, img_name, caption)
    if ext != "docx":
        raise ValueError(f"I can put pictures into .docx and .pptx files, not .{ext or '?'}")
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.text.paragraph import Paragraph

    doc = Document(io.BytesIO(doc_data))
    paragraphs = doc.paragraphs
    anchor, replace = None, False
    if after:
        wanted = after.lower()
        anchor = next((p for p in paragraphs if p.text.strip().lower() == wanted), None)
        replace = anchor is not None and after.startswith("[")
        anchor = anchor or next((p for p in paragraphs if wanted in p.text.lower()), None)
        if anchor is None:
            headings = [p.text.strip() for p in paragraphs if p.style.name.startswith(("Heading", "Title"))]
            raise ValueError(f"no paragraph contains {after!r}. Headings: {'; '.join(headings[:30]) or 'none'}")

    def new_paragraph_after(par):
        el = OxmlElement("w:p")
        par._p.addnext(el)
        return Paragraph(el, par._parent)

    if anchor is None:
        pic_par = doc.add_paragraph()
        where = "at the end"
    elif replace:
        for run in list(anchor.runs):
            run._r.getparent().remove(run._r)
        pic_par = anchor
        where = f"in place of {after!r}"
    else:
        pic_par = new_paragraph_after(anchor)
        where = f"after {anchor.text.strip()[:80]!r}"
    pic_par.alignment = WD_ALIGN_PARAGRAPH.CENTER
    insert_picture(pic_par.add_run(), img_data, img_name, width_cm)
    if caption:
        add_caption(new_paragraph_after(pic_par), 0, caption)
        renumber_figures(doc)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue(), where


def renumber_figures(doc) -> None:
    """Set every 'Figure n' caption's shown number in document order (Word would on a field update, but a
    document opened in a viewer, or before fields update, must already read right)."""
    from docx.oxml.ns import qn

    n, pending = 0, False
    for el in doc.element.body.iter(qn("w:instrText"), qn("w:t")):
        if el.tag == qn("w:instrText") and "SEQ Figure" in (el.text or ""):
            n, pending = n + 1, True
        elif el.tag == qn("w:t") and pending:
            el.text, pending = str(n), False


def _insert_into_deck(deck: bytes, img_data: bytes, img_name: str, caption: str) -> tuple[bytes, str]:
    from pptx import Presentation
    from pptx.util import Emu

    from .vector import add_svg_to_slide, clean_svg, svg_size

    prs = Presentation(io.BytesIO(deck))
    layouts = list(prs.slide_layouts)
    layout = next((lay for lay in layouts if lay.name.lower() in ("title only", "blank")), layouts[-1])
    slide = prs.slides.add_slide(layout)
    sw, sh = prs.slide_width, prs.slide_height
    top = int(sh * 0.18)
    if slide.shapes.title is not None:
        slide.shapes.title.text = caption or ""
    elif caption:
        box = slide.shapes.add_textbox(int(sw * 0.05), int(sh * 0.04), int(sw * 0.9), int(sh * 0.1))
        box.text_frame.text = caption
    else:
        top = int(sh * 0.05)
    box_w, box_h = int(sw * 0.9), int(sh - top - sh * 0.05)
    is_svg = img_name.lower().endswith(".svg")
    if is_svg:
        svg = clean_svg(img_data.decode("utf-8", "replace"))
        w, h = svg_size(svg)
    else:
        from PIL import Image
        with Image.open(io.BytesIO(img_data)) as im:
            w, h = im.size
    scale = min(box_w / w, box_h / h)
    pw, ph = int(w * scale), int(h * scale)
    left, top = int((sw - pw) / 2), int(top + (box_h - ph) / 2)
    if is_svg:
        add_svg_to_slide(slide, svg, Emu(left), Emu(top), Emu(pw), Emu(ph))
    else:
        slide.shapes.add_picture(io.BytesIO(img_data), Emu(left), Emu(top), Emu(pw), Emu(ph))
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue(), f"on a new slide {len(prs.slides)}"
