"""DXF drawings (ASCII or binary) for review: the drawing's text, and pictures of it.

A DXF is the drawing's data, so two things come out of it:

  text      what a drawing review checks (title block, numbering, references, sheet consistency, stated
            requirements): the header (units, extents, who saved it), the sheets (layouts), the title block
            (block references with attributes: drawing number, revision, title, drawn/checked/approved, dates),
            every note (TEXT/MTEXT) by sheet and layer, dimensions with their values, layers, external
            references and images, and what the drawing contains (entity counts)
  pictures  the model space and each sheet rendered as images (white background, colours kept), for the model
            to look at like a PDF page

DWG (AutoCAD's own binary format) isn't readable here: ask for a DXF export or a PDF print.
"""

from __future__ import annotations

import io
import re
import threading
from collections import Counter, defaultdict

_DRAW_LOCK = threading.Lock()  # matplotlib's pyplot isn't thread-safe; renders run in worker threads

MAX_NOTES = 400          # notes listed in the text
MAX_NOTE_CHARS = 300
MAX_ENTITIES_TO_DRAW = 150_000

UNITS = {0: "unitless", 1: "inches", 2: "feet", 4: "millimetres", 5: "centimetres", 6: "metres", 14: "decimetres"}


# Big drawings are opened in a child process with its own memory cap, so a drawing too large to handle fails
# with a message instead of the whole agent being killed for running out of memory (a 43 MB site plan once took
# a reviewer agent's pod past its 1 GiB limit three times). The child exits after each drawing, so its memory is
# returned straight away.
CHILD_MEMORY_BYTES = int(3.2 * 1024 ** 3)
CHILD_TIMEOUT_S = 300
_OUT_OF_MEMORY = re.compile(r"failed to map segment|cannot allocate memory|out of memory", re.I)


def _limit_memory(limit: int) -> None:
    import resource
    try:
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ValueError, OSError):  # not allowed here (some sandboxes): run without the cap
        pass


def isolated(fn_name: str, *args):
    """Run text(...) or render(...) in a capped child process; ValueError with a plain reason if it fails."""
    import concurrent.futures
    import multiprocessing
    ctx = multiprocessing.get_context("spawn")
    with concurrent.futures.ProcessPoolExecutor(max_workers=1, mp_context=ctx, initializer=_limit_memory,
                                                initargs=(CHILD_MEMORY_BYTES,)) as pool:
        future = pool.submit(globals()[fn_name], *args)
        try:
            return future.result(timeout=CHILD_TIMEOUT_S)
        except MemoryError:
            raise ValueError(f"that drawing is too big to open here (it needs more than "
                             f"{CHILD_MEMORY_BYTES // 1024 ** 3} GB): send a PDF print, or the DXF of just the "
                             "sheets to review") from None
        except (ImportError, OSError) as exc:
            # Under the cap, running out of memory can surface while a native library is loaded (numpy's
            # "failed to map segment from shared object", "cannot allocate memory"): the same as a MemoryError
            if not _OUT_OF_MEMORY.search(str(exc)):
                raise
            raise ValueError("opening that drawing ran out of memory: send a PDF print, or the DXF of just the "
                             "sheets to review") from None
        except concurrent.futures.process.BrokenProcessPool:
            raise ValueError("opening that drawing ran out of memory: send a PDF print, or the DXF of just the "
                             "sheets to review") from None
        except concurrent.futures.TimeoutError:
            for p in pool._processes.values():  # a stuck render: stop it
                p.kill()
            raise ValueError(f"that drawing took over {CHILD_TIMEOUT_S // 60} minutes to open: send a PDF print "
                             "instead") from None


def load(data: bytes):
    """An ezdxf document from DXF bytes (ASCII or binary), recovering from small errors as AutoCAD does."""
    from ezdxf import recover
    from ezdxf.lldxf.const import DXFStructureError

    if data[:4] in (b"AC10", b"AC1."):  # DWG files start "AC10xx"
        raise ValueError("that's a DWG file, which I can't read: export it as DXF (or print it to PDF) and send that")
    try:
        if data.startswith(b"AutoCAD Binary DXF"):  # recover.read() only detects binary DXF from a file
            import os
            import tempfile
            with tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, "drawing.dxf")
                with open(path, "wb") as f:
                    f.write(data)
                import ezdxf
                doc = ezdxf.readfile(path)  # recover can't read binary DXF; the plain reader can
                auditor = doc.audit()
        else:
            doc, auditor = recover.read(io.BytesIO(data))
    except (DXFStructureError, OSError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"I couldn't read that DXF ({exc}); if it opens in CAD, re-save it as ASCII DXF") from None
    return doc, auditor


def _layouts(doc) -> list:
    """Model space first, then the paper-space sheets in tab order (empty sheets, like the default Layout1, left
    out so the numbering matches what's on the drawing)."""
    sheets = [doc.layouts.get(n) for n in doc.layouts.names_in_taborder() if n.lower() != "model"]
    return [doc.modelspace()] + [s for s in sheets if len(s) > 0]


def _text_of(e) -> str:
    kind = e.dxftype()
    try:
        if kind == "MTEXT":
            return e.plain_text().strip()
        if kind in ("TEXT", "ATTRIB", "ATTDEF"):
            return (e.dxf.text or "").strip()
    except Exception:  # odd encodings or formatting codes: skip the entity, not the drawing
        return ""
    return ""


def text(data: bytes, name: str = "drawing.dxf") -> str:
    doc, auditor = load(data)
    h = doc.header
    out = [f"DXF drawing {name}: AutoCAD version {doc.acad_release} ({doc.dxfversion}), "
           f"units {UNITS.get(h.get('$INSUNITS', 0), h.get('$INSUNITS'))}"]
    if h.get("$LASTSAVEDBY"):
        out.append(f"Last saved by: {h.get('$LASTSAVEDBY')}")
    try:
        lo, hi = h.get("$EXTMIN"), h.get("$EXTMAX")
        if lo and hi and hi[0] > lo[0]:
            out.append(f"Model extents: {hi[0] - lo[0]:.0f} x {hi[1] - lo[1]:.0f} drawing units")
    except (TypeError, IndexError):
        pass
    if auditor.has_errors or auditor.has_fixes:
        out.append(f"File problems repaired on reading: {len(auditor.fixes)} fixes, {len(auditor.errors)} errors")
    layouts = _layouts(doc)
    out.append("Sheets (layouts): " + ", ".join(l.name for l in layouts))

    title_blocks, notes, dims = [], [], []
    kinds: Counter = Counter()
    by_layer: Counter = Counter()
    for layout in layouts:
        for e in layout:
            kinds[e.dxftype()] += 1
            by_layer[e.dxf.get("layer", "0")] += 1
            if e.dxftype() == "INSERT" and e.attribs:
                attrs = {a.dxf.tag: (a.dxf.text or "").strip() for a in e.attribs if (a.dxf.text or "").strip()}
                if attrs:
                    title_blocks.append((layout.name, e.dxf.name, attrs))
            elif e.dxftype() in ("TEXT", "MTEXT"):
                t = _text_of(e)
                if t:
                    notes.append((layout.name, e.dxf.get("layer", "0"), t))
            elif e.dxftype() == "DIMENSION":
                try:
                    value = e.dxf.get("text") or ""
                    measured = e.get_measurement()
                    measured = f"{measured:.2f}" if isinstance(measured, (int, float)) else str(measured)
                except Exception:
                    measured = "?"
                dims.append((layout.name, measured, value if value not in ("", "<>") else ""))

    sheet_blocks = [t for t in title_blocks if t[0].lower() != "model"]
    parts = [t for t in title_blocks if t[0].lower() == "model"]
    if sheet_blocks:  # on the sheets these are the title blocks (and revision boxes): every one, in full
        out.append("\nTitle blocks on the sheets (block: TAG = value):")
        for sheet, block, attrs in sheet_blocks[:60]:
            out.append(f"- [{sheet}] {block}: " + "; ".join(f"{k} = {v}" for k, v in attrs.items()))
    else:
        out.append("\nNo attributed block on any sheet: the title block may be plain text (see the notes) or missing.")
    if parts:  # in the model space they are mostly symbols with ratings: group them so nothing is crowded out
        groups: dict = defaultdict(Counter)
        for _, block, attrs in parts:
            groups[block]["; ".join(f"{k} = {v}" for k, v in attrs.items())] += 1
        out.append(f"\nComponents in the model space ({len(parts)} attributed symbols; block: values x count):")
        lines = 0
        for block, values in sorted(groups.items(), key=lambda g: -sum(g[1].values())):
            shown = values.most_common(12)
            out.append(f"- {block} ({sum(values.values())}): " + " | ".join(f"{v} x{n}" if n > 1 else v for v, n in shown)
                       + (f" | ... {len(values) - len(shown)} more" if len(values) > len(shown) else ""))
            lines += 1
            if lines >= 60:
                out.append(f"- ... {len(groups) - lines} more symbol types")
                break
    if notes:
        out.append(f"\nNotes and text ({len(notes)}; [sheet / layer] text):")
        seen = set()
        for sheet, layer, t in notes:
            key = (sheet, t)
            if key in seen:
                continue
            seen.add(key)
            out.append(f"- [{sheet} / {layer}] {t[:MAX_NOTE_CHARS]}")
            if len(seen) >= MAX_NOTES:
                out.append(f"- ... {len(notes) - len(seen)} more")
                break
    if dims:
        out.append(f"\nDimensions ({len(dims)}; measured value, and any text that overrides it):")
        for sheet, measured, override in dims[:150]:
            out.append(f"- [{sheet}] {measured}" + (f"  shown as \"{override}\"" if override else ""))
    xrefs = [b.name for b in doc.blocks if getattr(b, "is_xref", False)]
    images = [d.dxf.get("filename") for d in doc.objects.query("IMAGEDEF")]
    if xrefs:
        out.append("\nExternal references (other drawings this one relies on): " + ", ".join(xrefs))
    if images:
        out.append("Linked images: " + ", ".join(str(i) for i in images if i))
    layers = sorted(doc.layers, key=lambda l: l.dxf.name)
    out.append("\nLayers (entities; off/frozen marked): " + ", ".join(
        f"{l.dxf.name} ({by_layer.get(l.dxf.name, 0)}{', off' if l.is_off() else ''}{', frozen' if l.is_frozen() else ''})"
        for l in layers[:120]))
    out.append("Contents: " + ", ".join(f"{k} {v}" for k, v in kinds.most_common(25)))
    return "\n".join(out)


def _frame(ax, layout, max_edge: int) -> None:
    """Zoom to what's drawn (plus the paper, for a sheet) with a small margin, and size the picture to that shape
    with its long edge `max_edge` pixels, so small text on a big sheet stays legible."""
    from ezdxf import bbox
    def drawn(e) -> bool:  # the sheet's own overall viewport (id 1) and stray points aren't part of the drawing
        return e.dxftype() != "POINT" and not (e.dxftype() == "VIEWPORT" and e.dxf.get("id", 0) == 1)
    box = bbox.extents((e for e in layout if drawn(e)), fast=True)
    if not box.has_data:
        box = bbox.extents(layout, fast=True)
    if not box.has_data:
        return None
    (x0, y0, _), (x1, y1, _) = box.extmin, box.extmax
    if layout.name.lower() == "model":
        core = _core_window(layout)
        if core and (core[2] - core[0]) * (core[3] - core[1]) < 0.25 * (x1 - x0) * (y1 - y0):
            x0, y0, x1, y1 = core  # far-off survey lines or strays would otherwise shrink the drawing to a corner
            ax._agent_zoomed = True
    if layout.name.lower() != "model":
        lmin, lmax = layout.dxf_layout.dxf.limmin, layout.dxf_layout.dxf.limmax
        if lmax[0] > lmin[0]:
            main = bbox.extents((e for e in layout.query("INSERT VIEWPORT") if drawn(e)), fast=True)  # title block, viewports
            tol = 0.05 * max(lmax[0] - lmin[0], lmax[1] - lmin[1])
            on_paper = main.has_data and (main.extmin[0] >= lmin[0] - tol and main.extmin[1] >= lmin[1] - tol
                                          and main.extmax[0] <= lmax[0] + tol and main.extmax[1] <= lmax[1] + tol)
            if on_paper:  # a normal sheet: show the paper, ignore stray marks far off it
                x0, y0, x1, y1 = lmin[0], lmin[1], lmax[0], lmax[1]
            else:         # things placed off the paper: show them too
                x0, y0, x1, y1 = min(x0, lmin[0]), min(y0, lmin[1]), max(x1, lmax[0]), max(y1, lmax[1])
    pad = 0.03 * max(x1 - x0, y1 - y0, 1)
    x0, y0, x1, y1 = x0 - pad, y0 - pad, x1 + pad, y1 + pad
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.set_aspect("auto")  # the figure itself gets the drawing's shape, so nothing is stretched
    w, h = x1 - x0, max(y1 - y0, 1e-9)
    scale = max_edge / max(w, h)
    ax.figure.set_size_inches(max(w * scale, 64) / 100, max(h * scale, 64) / 100)
    return x0, y0, x1, y1


def _core_window(layout, grid: int = 48, mass: float = 0.9):
    """Where the drawing really is: grid the model space's objects (by their reference points), start at the
    busiest cell and grow through neighbouring cells until they hold `mass` of the objects in that cluster.
    Far-off survey lines, old copies and separate symbol clusters then fall outside the frame."""
    pts = []
    for e in layout:
        d = e.dxf
        p = None
        for attr in ("insert", "start", "center", "location", "defpoint", "vtx0"):  # vtx0: 3DFACE / SOLID
            if d.hasattr(attr):
                p = d.get(attr)
                break
        if p is None and e.dxftype() == "LWPOLYLINE":
            p = next(iter(e.vertices()), None)
        if p is None and e.dxftype() == "POLYLINE":
            p = next((v.dxf.location for v in e.vertices), None)
        if p is not None:
            pts.append((p[0], p[1]))
    if len(pts) < 50:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    if x1 <= x0 or y1 <= y0:
        return None
    cw, ch = (x1 - x0) / grid, (y1 - y0) / grid
    cells: Counter = Counter((min(int((x - x0) / cw), grid - 1), min(int((y - y0) / ch), grid - 1)) for x, y in pts)
    start = max(cells, key=cells.get)
    seen, frontier, total = {start}, [start], 0
    cluster = []
    while frontier:  # the connected block of occupied cells around the busiest one
        c = frontier.pop()
        cluster.append(c)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                n = (c[0] + dx, c[1] + dy)
                if n not in seen and cells.get(n):
                    seen.add(n)
                    frontier.append(n)
    cluster.sort(key=lambda c: -cells[c])
    need, kept = mass * sum(cells[c] for c in cluster), []
    for c in cluster:
        kept.append(c)
        total += cells[c]
        if total >= need:
            break
    gx0, gx1 = min(c[0] for c in kept), max(c[0] for c in kept) + 1
    gy0, gy1 = min(c[1] for c in kept), max(c[1] for c in kept) + 1
    return x0 + gx0 * cw, y0 + gy0 * ch, x0 + gx1 * cw, y0 + gy1 * ch


def _windows(layout) -> list[tuple[tuple[float, float, float, float], tuple[float, float, float, float]]]:
    """For each viewport on a sheet that looks into the model space: (its rectangle on the paper, the model-space
    window it shows), both as (x0, y0, x1, y1). The first viewport (id 1) is the sheet itself and is skipped."""
    out = []
    for vp in layout.query("VIEWPORT"):
        d = vp.dxf
        if d.get("id", 0) == 1 or d.get("status", 0) < 1 or not vp.is_top_view:
            continue
        w, h = d.get("width", 0), d.get("height", 0)
        vh = d.get("view_height", 0)
        if w <= 0 or h <= 0 or vh <= 0:
            continue
        (px, py, _), (mx, my, _) = d.center, d.view_center_point
        vw = vh * w / h
        out.append(((px - w / 2, py - h / 2, px + w / 2, py + h / 2), (mx - vw / 2, my - vh / 2, mx + vw / 2, my + vh / 2)))
    return out

def render_and_text(data: bytes, name: str, wanted: list[int] | None = None, max_edge: int = 1568):
    """Pictures and, with them, the drawing's text (title block, notes, dimensions): one parse of a big file."""
    drawn, info = render(data, name, wanted, max_edge)
    info["drawing_text"] = text(data, name)[:30_000]
    return drawn, info


def render(data: bytes, name: str, wanted: list[int] | None = None, max_edge: int = 1568) -> tuple[list[tuple[bytes, str]], dict]:
    """PNG pictures of the model space (1) and each sheet (2, 3, ...): [(png, label)], info."""
    with _DRAW_LOCK:
        return _render(data, name, wanted, max_edge)


def _render(data: bytes, name: str, wanted: list[int] | None, max_edge: int) -> tuple[list[tuple[bytes, str]], dict]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from ezdxf.addons.drawing import Frontend, RenderContext
    from ezdxf.addons.drawing.config import BackgroundPolicy, ColorPolicy, Configuration
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

    doc, _ = load(data)
    layouts = _layouts(doc)
    drawable = [i for i, l in enumerate(layouts, 1) if len(l) > 0]
    wanted = [n for n in (wanted or drawable[:5]) if n in drawable][:5]
    config = Configuration(background_policy=BackgroundPolicy.WHITE, color_policy=ColorPolicy.COLOR)  # colour 7 draws black on white
    from PIL import Image

    out = []
    model: dict = {}  # the model space drawn once, then cut to each viewport's window (see below)

    def model_window(window, size) -> "Image.Image":
        if "ax" not in model:
            fig_m = plt.figure(dpi=100)
            ax_m = fig_m.add_axes((0, 0, 1, 1))
            Frontend(RenderContext(doc), MatplotlibBackend(ax_m), config=config).draw_layout(layouts[0], finalize=True)
            model.update(fig=fig_m, ax=ax_m)
        (wx0, wy0, wx1, wy1), (pw, ph) = window, size
        model["ax"].set_xlim(wx0, wx1)
        model["ax"].set_ylim(wy0, wy1)
        model["ax"].set_aspect("auto")
        model["fig"].set_size_inches(max(pw, 8) / 100, max(ph, 8) / 100)
        buf = io.BytesIO()
        model["fig"].savefig(buf, format="png", dpi=100, facecolor="white")
        return Image.open(buf).convert("RGB")

    try:
        for n in wanted:
            layout = layouts[n - 1]
            if len(layout) > MAX_ENTITIES_TO_DRAW:
                continue
            fig = plt.figure(figsize=(max_edge / 100, max_edge / 100 * 0.71), dpi=100)
            ax = fig.add_axes((0, 0, 1, 1))
            try:
                Frontend(RenderContext(doc), MatplotlibBackend(ax), config=config).draw_layout(layout, finalize=True)
                limits = _frame(ax, layout, max_edge)  # everything on the sheet, including anything off the paper
                buf = io.BytesIO()
                fig.savefig(buf, format="png", dpi=100, facecolor="white")
            finally:
                plt.close(fig)
            png = buf.getvalue()
            windows = _windows(layout) if n > 1 and limits else []
            if windows and len(layouts[0]) <= MAX_ENTITIES_TO_DRAW:
                # ezdxf leaves viewports empty on some drawings, so draw what each one shows ourselves and put it
                # in its place on the sheet
                sheet = Image.open(io.BytesIO(png)).convert("RGB")
                x0, y0, x1, y1 = limits
                sx, sy = sheet.width / (x1 - x0), sheet.height / (y1 - y0)
                for (vx0, vy0, vx1, vy1), window in windows:
                    left, top = round((vx0 - x0) * sx), round((y1 - vy1) * sy)
                    size = (max(1, round((vx1 - vx0) * sx)), max(1, round((vy1 - vy0) * sy)))
                    sheet.paste(model_window(window, size).resize(size), (left, top))
                fixed = io.BytesIO()
                sheet.save(fixed, "PNG", optimize=True)
                png = fixed.getvalue()
            label = f"{name} {'model space' if n == 1 else 'sheet ' + layout.name}"
            if n == 1 and getattr(ax, "_agent_zoomed", False):
                label += (" (zoomed to the main part of the drawing; other parts of the model space are outside the "
                          "frame: the sheets show what each one covers)")
            out.append((png, label))
    finally:
        if "fig" in model:
            plt.close(model["fig"])
    return out, {"kind": "dxf", "views": [("1 model space" if i == 1 else f"{i} sheet {layouts[i - 1].name}")
                                          for i in drawable], "shown": wanted}
