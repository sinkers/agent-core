"""Vector art (vector.py) and Word documents with figures (filemaker.py, files.insert_image)."""

import io
import json
import zipfile

import pytest

from agentcore.filemaker import image_refs, insert_into_document, make_file
from agentcore.files import FileTools
from agentcore.store import MemoryStore
from agentcore.tools import CallContext
from agentcore.vector import clean_svg, svg_size, svg_to_png

SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 450"><rect x="20" y="20" width="200" height="80" '
       'fill="#2A5A3D"/><text x="120" y="60" text-anchor="middle" font-family="Calibri" font-size="16" '
       'fill="#fff">33 kV busbar</text></svg>')
BLANK = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 450"><rect x="20" y="20" width="200" height="80" fill="#2A5A3D"/></svg>'


def parts(data: bytes) -> dict[str, bytes]:
    z = zipfile.ZipFile(io.BytesIO(data))
    return {n: z.read(n) for n in z.namelist()}


# ---- vector.py -------------------------------------------------------------------------------------------------

def test_clean_svg_strips_active_content_and_needs_a_size():
    dirty = ('<svg viewBox="0 0 10 10"><script>alert(1)</script><foreignObject/><rect onclick="x()" width="5" '
             'height="5"/><a href="https://evil.example"><text>x</text></a><use href="#r"/></svg>')
    out = clean_svg(dirty)
    assert "script" not in out and "foreignObject" not in out and "onclick" not in out and "evil" not in out
    assert 'href="#r"' in out and out.startswith("<svg") and "http://www.w3.org/2000/svg" in out
    for bad, why in [("<svg>", "well-formed"), ("<svg xmlns='http://www.w3.org/2000/svg'/>", "viewBox"),
                     ("<div/>", "root"), ('<!DOCTYPE svg [<!ENTITY x "y">]><svg/>', "DOCTYPE"), ("", "empty")]:
        with pytest.raises(ValueError, match=why):
            clean_svg(bad)
    assert clean_svg("```svg\n" + SVG + "\n```").startswith("<svg")


def test_svg_renders_with_text_even_without_the_named_font():
    assert svg_size(SVG) == (800, 450)
    with_text, without = svg_to_png(clean_svg(SVG), 400), svg_to_png(clean_svg(BLANK), 400)
    assert with_text.startswith(b"\x89PNG") and len(with_text) > len(without)  # Calibri missing: DejaVu stands in
    from PIL import Image
    assert Image.open(io.BytesIO(with_text)).size == (400, 225)


# ---- create_file -------------------------------------------------------------------------------------------------

def test_svg_and_png_files():
    assert make_file("layout.svg", SVG).startswith(b"<svg")
    assert make_file("layout.png", SVG).startswith(b"\x89PNG")
    with pytest.raises(ValueError):
        make_file("layout.png", "not svg")


def test_docx_with_inline_svg_figure_links_quote_code_and_page_break():
    md = ("# Brief\n\nSee [Acme](https://acme.example) and Figure 1.\n\n```svg Site layout\n" + SVG + "\n```\n\n"
          "> The site must have 2 MW firm.\n\n```\nTX-1 33/0.415 kV\n```\n\n<!-- pagebreak -->\n\n- top\n  - nested\n")
    data = make_file("brief.docx", md)
    p = parts(data)
    doc = p["word/document.xml"].decode()
    assert any(n.endswith(".svg") for n in p) and any(n.endswith(".png") for n in p)
    assert "svgBlip" in doc and "SEQ Figure" in doc and "Site layout" in doc
    assert "w:hyperlink" in doc and b"https://acme.example" in p["word/_rels/document.xml.rels"]
    assert 'w:type="page"' in doc and "TX-1 33/0.415 kV" in doc
    from docx import Document
    styles = {par.style.name for par in Document(io.BytesIO(data)).paragraphs}
    assert {"Caption", "Quote", "List Bullet 2"} <= styles


def test_docx_image_reference_and_missing_one():
    assert image_refs("x\n![Layout](outbox:abc)\n![](plan.png)") == ["outbox:abc", "plan.png"]
    png = svg_to_png(clean_svg(SVG), 300)
    data = make_file("a.docx", "![Layout](outbox:abc)\n\n![Missing](nope.png)", {"outbox:abc": ("l.png", png)})
    doc = parts(data)["word/document.xml"].decode()
    assert "Layout" in doc and "Image not available: Missing" in doc and "<pic:pic" in doc


# ---- insert_into_document ----------------------------------------------------------------------------------------

def base_docx() -> bytes:
    return make_file("r.docx", "# Report\n\n## 1 Scope\n\nText.\n\n## 2 Site\n\n[Figure: site layout]\n\nAfter.\n")


def test_insert_replaces_a_placeholder_and_numbers_the_caption():
    data, where = insert_into_document(base_docx(), "r.docx", SVG.encode(), "f.svg", "[Figure: site layout]",
                                       "Site layout")
    assert "in place of" in where
    from docx import Document
    d = Document(io.BytesIO(data))
    texts = [par.text for par in d.paragraphs]
    assert "[Figure: site layout]" not in texts and "Figure 1: Site layout" in texts
    assert texts.index("Figure 1: Site layout") < texts.index("After.")
    assert "svgBlip" in parts(data)["word/document.xml"].decode()


def test_insert_after_a_heading_and_unknown_anchor_lists_headings():
    data, where = insert_into_document(base_docx(), "r.docx", SVG.encode(), "f.svg", "1 Scope", "")
    assert "after '1 Scope'" in where
    with pytest.raises(ValueError, match="Headings: Report; 1 Scope; 2 Site"):
        insert_into_document(base_docx(), "r.docx", SVG.encode(), "f.svg", "Appendix Z", "")


def test_insert_into_a_deck_adds_a_slide():
    from pptx import Presentation
    prs = Presentation()
    prs.slides.add_slide(prs.slide_layouts[0])
    buf = io.BytesIO()
    prs.save(buf)
    data, where = insert_into_document(buf.getvalue(), "deck.pptx", SVG.encode(), "f.svg", "", "Single-line diagram")
    assert where == "on a new slide 2"
    out = Presentation(io.BytesIO(data))
    assert len(out.slides) == 2 and "svgBlip" in parts(data)["ppt/slides/slide2.xml"].decode()
    with pytest.raises(ValueError):
        insert_into_document(b"x", "notes.txt", SVG.encode(), "f.svg")


# ---- the files tools ----------------------------------------------------------------------------------------------

async def test_create_file_previews_drawings_and_insert_image_makes_a_new_copy():
    tools = FileTools(MemoryStore())
    ctx = CallContext("chat-1", "aad-alex")
    out = await tools.call_tool("create_file", {"filename": "layout.svg", "content": SVG}, ctx)
    assert isinstance(out, dict) and out["images"][0][0] == "png" and out["images"][0][1].startswith(b"\x89PNG")
    svg_ref = json.loads(out["text"])["file"]

    doc = json.loads(await tools.call_tool("create_file", {
        "filename": "brief.docx", "content": f"# Brief\n\n## Site\n\n![Site layout]({svg_ref})\n"}, ctx))
    assert doc["figures"] == 1

    bad = json.loads(await tools.call_tool("create_file", {"filename": "b.docx", "content": "![x](outbox:nope)"}, ctx))
    assert "image 'outbox:nope'" in bad["error"]

    placed = json.loads(await tools.call_tool("insert_image", {
        "document": doc["file"], "svg": SVG, "after": "Site", "caption": "Single-line diagram",
        "filename": "brief-v2.docx"}, ctx))
    assert placed["name"] == "brief-v2.docx" and placed["file"].startswith("outbox:") and "after" in placed["placed"]
    missing = json.loads(await tools.call_tool("insert_image", {"document": doc["file"]}, ctx))
    assert "image" in missing["error"]


def test_inserted_figures_are_numbered_by_position():
    first, _ = insert_into_document(base_docx(), "r.docx", SVG.encode(), "f.svg", "2 Site", "Site layout")
    second, _ = insert_into_document(first, "r.docx", SVG.encode(), "f.svg", "1 Scope", "Scope sketch")
    from docx import Document
    texts = [p.text for p in Document(io.BytesIO(second)).paragraphs]
    assert texts.index("Figure 1: Scope sketch") < texts.index("Figure 2: Site layout")
