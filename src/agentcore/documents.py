"""Turn documents the agents are given into text a model can read.

The openclaw MCP `sharepoint_get` tool returns a file's bytes (base64) or a short-lived download URL;
a model can't read a .docx or .pdf that way. The tool gateway passes such results through
`readable_tool_result`, which swaps the bytes for extracted text (Word, PDF, Excel, PowerPoint,
plain text), capped so one large document can't flood the model's context.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re

import httpx

log = logging.getLogger(__name__)
MAX_TEXT_CHARS = 120_000
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
READABLE_TOOLS = {"openclaw__sharepoint_get"}


def _docx(data: bytes) -> str:
    import docx
    d = docx.Document(io.BytesIO(data))
    out: list[str] = []
    for block in d.element.body.iterchildren():
        tag = block.tag.rsplit("}", 1)[-1]
        if tag == "p":
            text = "".join(t.text or "" for t in block.iter() if t.tag.endswith("}t"))
            if text.strip():
                out.append(text)
        elif tag == "tbl":
            table = docx.table.Table(block, d)
            for row in table.rows:
                out.append(" | ".join(c.text.strip() for c in row.cells))
    for section in d.sections:  # headers carry document numbers and revisions
        for p in section.header.paragraphs:
            if p.text.strip():
                out.insert(0, f"[header] {p.text.strip()}")
    return "\n".join(out)


def _pdf(data: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return "\n".join(f"[page {i + 1}]\n{page.extract_text() or ''}" for i, page in enumerate(reader.pages))


def _xlsx(data: bytes) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    out: list[str] = []
    for ws in wb.worksheets:
        out.append(f"[sheet] {ws.title}")
        for row in ws.iter_rows(values_only=True):
            if any(v is not None for v in row):
                out.append(" | ".join("" if v is None else str(v) for v in row))
    return "\n".join(out)


def _pptx_shape(shape, out: list[str]) -> None:
    """Text of one shape: text frames, tables (rows as 'a | b'), groups (recursively) and charts (title + data)."""
    if hasattr(shape, "shapes"):  # a group
        for inner in shape.shapes:
            _pptx_shape(inner, out)
        return
    if getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip():
        out.append(shape.text_frame.text)
    if getattr(shape, "has_table", False):
        for row in shape.table.rows:
            out.append(" | ".join(c.text.replace("\n", " ").strip() for c in row.cells))
    if getattr(shape, "has_chart", False):
        try:
            chart = shape.chart
            if chart.has_title and chart.chart_title.has_text_frame:
                out.append(f"[chart] {chart.chart_title.text_frame.text}")
            plot = chart.plots[0]
            out.append("categories: " + ", ".join(str(c) for c in plot.categories))
            for series in plot.series:
                out.append(f"{series.name}: " + ", ".join("" if v is None else f"{v:g}" for v in series.values))
        except Exception:  # odd chart XML: the caption above is still useful
            pass


def _pptx(data: bytes) -> str:
    from pptx import Presentation
    prs = Presentation(io.BytesIO(data))
    out: list[str] = []
    for i, slide in enumerate(prs.slides, 1):
        out.append(f"[slide {i}]")
        for shape in slide.shapes:
            _pptx_shape(shape, out)
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
            out.append(f"[notes] {slide.notes_slide.notes_text_frame.text}")
    return "\n".join(out)


def document_text(data: bytes, name: str) -> str:
    """Plain text for a document, by file extension. Raises ValueError for unsupported types."""
    ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    if ext in ("docx", "docm"):
        return _docx(data)
    if ext == "pdf":
        return _pdf(data)
    if ext in ("xlsx", "xlsm"):
        return _xlsx(data)
    if ext == "pptx":
        return _pptx(data)
    if ext == "dxf":  # CAD drawing: title block, notes, dimensions, layers (dxf.py), in a capped child process
        from .dxf import isolated
        return isolated("text", data, name)
    if ext in ("txt", "md", "csv", "json", "xml", "yaml", "yml", "html", "htm"):
        return data.decode("utf-8", errors="replace")
    raise ValueError(f"can't read .{ext or '?'} files as text")


_FIELD_RE = re.compile(r"^\*\*([a-z_]+):\*\*[ \t]*(.*)$", re.MULTILINE)


def _parse_markdown_result(text: str) -> dict | None:
    """The openclaw MCP returns '**field:** value' lines, then '**content_base64:**' and the bytes."""
    if "**name:**" not in text:
        return None
    body: dict = {}
    head, _, b64 = text.partition("**content_base64:**")
    for key, value in _FIELD_RE.findall(head):
        body[key] = value.strip()
    if b64.strip():
        body["content_base64"] = "".join(b64.split())
    if body.get("size"):
        body["size"] = int(re.sub(r"\D", "", body["size"]) or 0)
    return body


def _as_markdown(body: dict) -> str:
    lines = [f"**{k}:** {v}" for k, v in body.items() if k not in ("text",)]
    if "text" in body:
        lines.append("\n**text:**\n" + body["text"])
    return "\n".join(lines)


def readable_tool_result(tool: str, text: str, http: httpx.Client | None = None) -> str:
    """For file-fetching tools, replace file bytes with extracted text; anything else is returned unchanged."""
    return readable_tool_result_with_images(tool, text, http)[0]


def readable_tool_result_with_images(tool: str, text: str, http: httpx.Client | None = None) -> tuple[str, list]:
    """Like readable_tool_result, plus the picture itself when the file is an image (images.py).
    For PDFs and Office files the text comes back; view_attachment(url=...) shows their pages and pictures."""
    if tool not in READABLE_TOOLS:
        return text, []
    markdown = False
    try:
        body = json.loads(text)
    except (TypeError, ValueError):
        body = _parse_markdown_result(text or "")
        markdown = body is not None
    if not isinstance(body, dict):
        return text, []
    name = body.get("name") or body.get("path") or ""
    data = None
    if body.get("content_base64"):
        data = base64.b64decode(body.pop("content_base64"))
    elif body.get("download_url") and (body.get("size") or 0) <= MAX_DOWNLOAD_BYTES:
        try:
            client = http or httpx.Client(timeout=60, follow_redirects=True)
            r = client.get(body["download_url"])
            r.raise_for_status()
            data = r.content
        except Exception as exc:
            body["text_error"] = f"could not download: {exc}"[:200]
    body.pop("download_url", None)  # a pre-authenticated link: don't hand it to the model
    from .images import is_image, visual
    if data is not None and is_image(name):
        try:
            shown, _ = visual(data, name)
            body["text"] = "(an image: shown to you below)"
            return (_as_markdown(body) if markdown else json.dumps(body)), shown
        except ValueError as exc:
            body["text_error"] = str(exc)[:200]
            data = None
    if data is not None:
        try:
            extracted = document_text(data, name)
            body["text"] = extracted[:MAX_TEXT_CHARS]
            if len(extracted) > MAX_TEXT_CHARS:
                body["text_truncated"] = f"first {MAX_TEXT_CHARS} of {len(extracted)} characters"
        except Exception as exc:
            body["text_error"] = str(exc)[:200]
    if data is not None and name.lower().endswith((".pdf", ".docx", ".pptx", ".xlsx")):
        body["visual"] = ("drawings, diagrams and pictures aren't in this text: view_attachment(url=<the link>, "
                          "pages=...) shows the pages (PDF) or embedded pictures")
    return (_as_markdown(body) if markdown else json.dumps(body)), []
