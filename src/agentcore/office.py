"""Page previews of Word, Excel and PowerPoint files, rendered through LibreOffice (headless) to PDF and then to
images (images.visual). The agents use it to look at what they made before sending it, the way a person would,
and view_attachment uses it to show a document's pages rather than only the pictures inside it.

LibreOffice is in the agent image (Dockerfile), with open fonts metric-compatible with Georgia, Calibri and
Consolas (fonts/30-metric-substitutes.conf), so line breaks and page breaks match Word closely. Each conversion runs
in its own temporary profile under /tmp (the root filesystem is read-only), at most two at a time, and is killed
after two minutes.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import threading
from pathlib import Path

OFFICE_EXTS = {"docx", "docm", "doc", "xlsx", "xlsm", "xls", "pptx", "ppt", "odt", "ods", "odp", "rtf"}
TIMEOUT_S = 120
_SLOTS = threading.Semaphore(2)


def available() -> bool:
    return shutil.which("soffice") is not None


def to_pdf(data: bytes, name: str) -> bytes:
    """The document as PDF. Raises ValueError when it can't be converted."""
    soffice = shutil.which("soffice")
    if not soffice:
        raise ValueError("page previews aren't available here (no LibreOffice)")
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in OFFICE_EXTS:
        raise ValueError(f"I can render Word, Excel and PowerPoint files, not .{ext or '?'}")
    with _SLOTS, tempfile.TemporaryDirectory(prefix="office-") as tmp:
        src = Path(tmp) / f"doc.{ext}"
        src.write_bytes(data)
        cmd = [soffice, f"-env:UserInstallation=file://{tmp}/profile", "--headless", "--norestore", "--nologo",
               "--nodefault", "--nolockcheck", "--convert-to", "pdf", "--outdir", tmp, str(src)]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                                env={**os.environ, "HOME": tmp})
        try:
            proc.wait(timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)  # soffice starts soffice.bin: kill the whole group
            proc.wait()
            raise ValueError("rendering took too long (over two minutes)") from None
        pdf = Path(tmp) / "doc.pdf"
        if not pdf.exists() or pdf.stat().st_size == 0:
            raise ValueError("LibreOffice couldn't open that file to render it")
        return pdf.read_bytes()


def render_pages(data: bytes, name: str, pages=None) -> tuple[list[tuple[str, bytes, str]], dict]:
    """([(format, image bytes, label)], info) for the pages wanted (default the first five), like a PDF in
    images.visual. Slides are labelled "slide n", spreadsheets "sheet page n"."""
    from .images import visual
    pdf = to_pdf(data, name)
    shown, info = visual(pdf, "rendered.pdf", pages)
    ext = name.rsplit(".", 1)[-1].lower()
    word = "slide" if ext in ("pptx", "ppt", "odp") else "sheet page" if ext in ("xlsx", "xlsm", "xls", "ods") else "page"
    relabelled = [(fmt, img, f"{name} {word} {label.rsplit(' ', 1)[-1]}") for fmt, img, label in shown]
    kind = {"slide": "slides", "sheet page": "spreadsheet pages", "page": "pages"}[word]
    return relabelled, {"kind": f"{kind} (rendered)", "pages": info.get("pages"), "shown": info.get("shown")}
