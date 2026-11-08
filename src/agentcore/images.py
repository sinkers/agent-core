"""Images the agents are shown: screenshots, photos, diagrams sent in Teams or email, or opened from SharePoint.

The models see them directly (Bedrock Converse image blocks, in the tool result that fetched them). This module
turns whatever arrives into something the model accepts: PNG/JPEG/GIF/WebP only, the long edge at most MAX_EDGE
pixels (what the model resolves anyway; bigger only costs tokens), and under MAX_BYTES (Bedrock's per-image limit
is 3.75 MB). Animated GIFs use their first frame. Other formats (HEIC, TIFF, BMP) are converted to PNG when Pillow
can read them, otherwise refused with a clear reason.
"""

from __future__ import annotations

import io

IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff", "heic", "heif"}
MAX_EDGE = 1568
MAX_BYTES = 3_500_000


def is_image(name: str, content_type: str | None = None) -> bool:
    ext = (name or "").lower().rsplit(".", 1)[-1] if "." in (name or "") else ""
    return ext in IMAGE_EXTS or (content_type or "").lower().startswith("image/")


def prepare_image(data: bytes) -> tuple[str, bytes, dict]:
    """(format for Bedrock, bytes, info) for an image, downscaled and re-encoded as needed.
    Raises ValueError when it isn't an image Pillow can read."""
    from PIL import Image, UnidentifiedImageError

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError(f"I can't open that image ({type(exc).__name__}); send it as PNG or JPEG") from None
    original = img.size
    fmt = (img.format or "").lower()
    if getattr(img, "is_animated", False):
        img.seek(0)
    resized = max(img.size) > MAX_EDGE
    if resized:
        img.thumbnail((MAX_EDGE, MAX_EDGE))
    if not resized and fmt in ("png", "jpeg", "gif", "webp") and len(data) <= MAX_BYTES:
        return fmt, data, {"width": original[0], "height": original[1], "resized": False}
    out = io.BytesIO()
    if img.mode in ("RGBA", "LA", "P") and fmt != "jpeg":
        img.save(out, "PNG", optimize=True)
        fmt_out = "png"
    else:
        img.convert("RGB").save(out, "JPEG", quality=88)
        fmt_out = "jpeg"
    if out.tell() > MAX_BYTES:  # a huge PNG photo: JPEG it
        out = io.BytesIO()
        img.convert("RGB").save(out, "JPEG", quality=80)
        fmt_out = "jpeg"
    return fmt_out, out.getvalue(), {"width": original[0], "height": original[1], "resized": resized,
                                     "shown_as": f"{img.size[0]}x{img.size[1]}"}


MAX_VIEW = 5  # images per tool call: enough for a drawing set's key sheets without flooding the context
MIN_EMBEDDED_BYTES = 8_000  # smaller pictures inside documents are logos, bullets and icons
RASTER_IN_DOCS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff")


def sniff_ext(data: bytes) -> str:
    """The file type from its first bytes ("" if unknown): Teams sends pasted images named "image/*"."""
    head = data[:16]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:2] == b"BM":
        return "bmp"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"mif1", b"heif", b"hevc"):
        return "heic"
    if head.startswith(b"%PDF"):
        return "pdf"
    return ""


def _ext(name: str) -> str:
    return (name or "").lower().rsplit(".", 1)[-1] if "." in (name or "") else ""


def _pages_wanted(pages, total: int) -> list[int]:
    """1-based page numbers from [3, 5] or "2-4,7" (default: the first MAX_VIEW), within 1..total."""
    if not pages:
        return list(range(1, min(total, MAX_VIEW) + 1))
    nums: list[int] = []
    for part in (pages if isinstance(pages, list) else str(pages).split(",")):
        part = str(part).strip()
        if "-" in part:
            a, _, b = part.partition("-")
            nums += range(int(a), int(b) + 1)
        elif part:
            nums.append(int(part))
    return [n for n in dict.fromkeys(nums) if 1 <= n <= total][:MAX_VIEW]


def visual(data: bytes, name: str, pages=None, pictures_only: bool = False) -> tuple[list[tuple[str, bytes, str]], dict]:
    """What a file looks like: [(format, bytes, label)] plus info (total pages/images, which were shown).

      image                 itself
      PDF                   pages rendered at the model's resolution (pages: [1, 3] or "2-4"; default the first 5)
      .docx .pptx .xlsx     its pages or slides as they look (office.py, LibreOffice); without LibreOffice, or
                            with pictures_only, the pictures embedded in it, in document order
      .dxf                  the model space (1) and each sheet (2, 3, ...) drawn (dxf.py)
    """
    ext = _ext(name)
    if ext in IMAGE_EXTS:
        fmt, out, info = prepare_image(data)
        return [(fmt, out, name)], {"kind": "image", **info}
    if ext == "pdf":
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(data)
        total = len(pdf)
        shown = _pages_wanted(pages, total)
        result = []
        for n in shown:
            page = pdf[n - 1]
            w, h = page.get_size()
            scale = min(4.0, MAX_EDGE / max(w, h))  # long edge ~MAX_EDGE pixels: drawings stay legible
            pil = page.render(scale=scale).to_pil()
            buf = io.BytesIO()
            pil.save(buf, "PNG", optimize=True)
            fmt, out, _ = prepare_image(buf.getvalue())
            result.append((fmt, out, f"{name} page {n}"))
        return result, {"kind": "pdf", "pages": total, "shown": shown}
    if ext == "dxf":
        from .dxf import isolated
        wanted = _pages_wanted(pages, 200) if pages else None
        # one capped child process (dxf.py) draws the views and reads the drawing's text with them: a SharePoint
        # DXF has no other way to its text (sharepoint_get's text stops at 10 MB and doesn't read DXF)
        drawn, info = isolated("render_and_text", data, name, wanted, MAX_EDGE)
        result = []
        for png, label in drawn:
            fmt, out, _ = prepare_image(png)
            result.append((fmt, out, label))
        return result, info
    if ext in ("docx", "docm", "pptx", "xlsx", "xlsm") and not pictures_only:
        from . import office  # the pages as they look (LibreOffice), when it's here
        if office.available():
            try:
                return office.render_pages(data, name, pages)
            except ValueError:
                pass  # fall back to the pictures inside it
    if ext in ("docx", "docm", "pptx", "xlsx", "xlsm"):
        import zipfile
        z = zipfile.ZipFile(io.BytesIO(data))
        media = [i for i in z.infolist() if "/media/" in i.filename and i.filename.lower().endswith(RASTER_IN_DOCS)
                 and i.file_size >= MIN_EMBEDDED_BYTES]
        media.sort(key=lambda i: [int(t) if t.isdigit() else t for t in __import__("re").split(r"(\d+)", i.filename)])
        shown = _pages_wanted(pages, len(media))
        result = []
        for n in shown:
            try:
                fmt, out, _ = prepare_image(z.read(media[n - 1]))
                result.append((fmt, out, f"{name} picture {n}"))
            except ValueError:
                continue
        return result, {"kind": "embedded pictures", "pictures": len(media), "shown": shown}
    if ext == "dwg":
        raise ValueError("I can't open DWG files: export the drawing as DXF (or print it to PDF) and send that")
    raise ValueError(f"I can show images, PDFs, DXF drawings and the pictures inside Word, PowerPoint and Excel "
                     f"files, not .{ext or '?'}")
