"""Vector art: SVG the agents draw themselves (vector-art skill), checked, rendered, and placed in documents.

  clean_svg(text)              parse and make safe: one <svg> root with a size; no scripts, event handlers,
                               foreignObject, entities or external links (an .svg file may be opened in a browser)
  svg_to_png(svg, width_px)    render with resvg (no system libraries). The image has no system fonts, so the
                               fonts matplotlib ships (DejaVu) stand in for any family that isn't there
  add_svg_picture(run, svg)    a picture in a Word run: the SVG itself, which Word 2016+ / 365 draws as vectors,
                               with a PNG fallback for older readers (the Office `svgBlip` extension)
  add_svg_to_slide(slide, svg) the same on a PowerPoint slide
"""

from __future__ import annotations

import io
import os
import re
import xml.etree.ElementTree as ET

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
SVG_BLIP_EXT = "{96DAC541-7B7A-43D3-8B79-37D633B846F1}"  # Office 2016 SVG picture extension
ASVG_NS = "http://schemas.microsoft.com/office/drawing/2016/SVG/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
IMAGE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
MAX_SVG_BYTES = 2 * 1024 * 1024
DROP_TAGS = {"script", "foreignObject", "iframe", "embed", "object"}
_UNITS = {"px": 1.0, "pt": 4 / 3, "pc": 16.0, "mm": 96 / 25.4, "cm": 96 / 2.54, "in": 96.0, "": 1.0}

ET.register_namespace("", SVG_NS)
ET.register_namespace("xlink", XLINK_NS)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _length(value: str | None) -> float | None:
    m = re.fullmatch(r"\s*([0-9.]+)\s*(px|pt|pc|mm|cm|in)?\s*", value or "")
    return float(m.group(1)) * _UNITS[m.group(2) or ""] if m else None


def clean_svg(text: str) -> str:
    """Safe, standalone SVG markup, or ValueError saying what's wrong (so the agent can fix its drawing)."""
    text = (text or "").strip()
    if text.startswith("```"):  # pasted from a fenced block
        text = re.sub(r"^```\w*\s*|\s*```$", "", text)
    if not text:
        raise ValueError("the SVG is empty")
    if len(text.encode()) > MAX_SVG_BYTES:
        raise ValueError("the SVG is over 2 MB: simplify it (fewer points, reuse shapes with <use>)")
    if re.search(r"<!DOCTYPE|<!ENTITY", text, re.I):
        raise ValueError("no DOCTYPE or ENTITY declarations in the SVG")
    if "<svg" in text and "xmlns=" not in text.split(">", 1)[0] + ">":
        text = text.replace("<svg", f'<svg xmlns="{SVG_NS}"', 1)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"the SVG isn't well-formed XML ({exc}); check tags are closed and & is written &amp;")
    if _local(root.tag) != "svg":
        raise ValueError("the root element must be <svg>")
    if not root.get("viewBox") and not (_length(root.get("width")) and _length(root.get("height"))):
        raise ValueError('give the <svg> a viewBox, e.g. viewBox="0 0 800 450"')
    for parent in list(root.iter()):
        for child in list(parent):
            if _local(child.tag) in DROP_TAGS:
                parent.remove(child)
    for el in root.iter():
        for attr in list(el.attrib):
            name = _local(attr).lower()
            value = el.attrib[attr].strip().lower()
            if name.startswith("on") or (name == "href" and not value.startswith(("#", "data:image/"))):
                del el.attrib[attr]
    return ET.tostring(root, encoding="unicode")


def svg_size(svg: str) -> tuple[float, float]:
    """(width, height) in CSS pixels: width/height if given, else the viewBox."""
    root = ET.fromstring(svg)
    w, h = _length(root.get("width")), _length(root.get("height"))
    vb = [float(v) for v in re.split(r"[\s,]+", (root.get("viewBox") or "").strip()) if v] or None
    if vb and len(vb) == 4:
        w = w or (h * vb[2] / vb[3] if h else vb[2])
        h = h or (w * vb[3] / vb[2] if vb[2] else vb[3])
    if not w or not h:
        raise ValueError("couldn't work out the SVG's size: give it a viewBox")
    return w, h


def _font_dirs() -> list[str]:
    import matplotlib

    return [os.path.join(os.path.dirname(matplotlib.__file__), "mpl-data", "fonts", "ttf")]


def svg_to_png(svg: str, width_px: int = 1600, background: str | None = "#ffffff") -> bytes:
    import resvg_py

    w, h = svg_size(svg)
    width_px = max(16, min(int(width_px), 4000))
    height_px = max(1, round(width_px * h / w))
    fonts = dict(skip_system_fonts=True, font_dirs=_font_dirs(), font_family="DejaVu Sans",
                 sans_serif_family="DejaVu Sans", serif_family="DejaVu Serif", monospace_family="DejaVu Sans Mono")
    return bytes(resvg_py.svg_to_bytes(svg_string=svg, width=width_px, height=height_px, background=background, **fonts))


def _relate_svg(owner_part, svg: str) -> str:
    """Add the SVG as an image part next to `owner_part` (a Word story part or a slide); its relationship id."""
    package = owner_part.package
    if type(owner_part).__module__.startswith("docx"):
        from docx.opc.part import Part
        part = Part(package.next_partname("/word/media/svg%d.svg"), "image/svg+xml", svg.encode(), package)
    else:
        from pptx.opc.package import Part
        part = Part(package.next_partname("/ppt/media/svg%d.svg"), "image/svg+xml", package, svg.encode())
    return owner_part.relate_to(part, IMAGE_REL)


def _add_svg_blip(blip, rid: str) -> None:
    from lxml import etree

    ext_lst = blip.find(f"{{{A_NS}}}extLst")
    if ext_lst is None:
        ext_lst = etree.SubElement(blip, f"{{{A_NS}}}extLst")
    ext = etree.SubElement(ext_lst, f"{{{A_NS}}}ext", uri=SVG_BLIP_EXT)
    svg_blip = etree.SubElement(ext, f"{{{ASVG_NS}}}svgBlip", nsmap={"asvg": ASVG_NS})
    svg_blip.set(f"{{{R_NS}}}embed", rid)


def add_svg_picture(run, svg: str, width_cm: float | None = None, max_width_cm: float = 16.0):
    """Put `svg` in a python-docx run: vectors in current Word, PNG for anything older."""
    from docx.shared import Cm

    w, h = svg_size(svg)
    width = min(width_cm or w * 2.54 / 96, max_width_cm)
    shape = run.add_picture(io.BytesIO(svg_to_png(svg)), width=Cm(width))
    blip = shape._inline.find(f".//{{{A_NS}}}blip")
    _add_svg_blip(blip, _relate_svg(run.part, svg))
    return shape


def add_svg_to_slide(slide, svg: str, left, top, width=None, height=None):
    """Put `svg` on a python-pptx slide (vector in current PowerPoint, PNG fallback)."""
    pic = slide.shapes.add_picture(io.BytesIO(svg_to_png(svg)), left, top, width, height)
    blip = pic._element.find(f".//{{{A_NS}}}blip")
    _add_svg_blip(blip, _relate_svg(slide.part, svg))
    return pic
