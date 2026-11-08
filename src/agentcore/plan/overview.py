"""The one-page view of a plan: workstreams (stages) as bars with their durations, the gates as a band across the
top with a line through every row, and the key milestones on their workstream's row, on a month (or quarter)
timeline.

  overview_data(s, level=1) -> {name, start, finish, workstreams, gates, milestones, ticks}
  overview_png(data) / overview_svg(data) -> bytes   (matplotlib; the PNG is 2x for slides and documents)
  overview_pptx(data) -> bytes                        a branded deck: the overview drawn as native, editable shapes on
                                                      the master template, then a gate and milestone table
The published viewer draws the same data interactively (render.py: the Overview tab).

Workstreams are the plan's tasks at outline `level` (1 = the stages or sub-plans), plus any deeper task marked
`key: true`. Gates (model.Task.gate, "G0".."G7") are always shown, as gates: in the method each closes its stage.
Mid-stage gateways (Task.gateway) are always shown, on their row. Other milestones: those marked `key: true`, or if
none are marked, those at the workstream level or one below; at most MAX_MILESTONES, earliest first, keeping the
last.

Labels never overlap: each row's milestone labels are put in lanes above and below the bar (`lanes`), and the row
grows to fit them; gate labels get lanes in the top band the same way.
"""

from __future__ import annotations

import io
import math
from datetime import date, timedelta

from .schedule import Schedule
from ..brandpack import brand

_P = brand().palette
INK, PRIMARY, ACCENT, MUTED, RULE, TINT = _P.ink, _P.primary, _P.accent, _P.muted, _P.rule, _P.tint
HEADING_FONT, BODY_FONT, MONO_FONT = brand().fonts.heading, brand().fonts.body, brand().fonts.mono
MAX_MILESTONES = 12
MAX_WORKSTREAMS = 14
MAX_LANES_PER_SIDE = 3


def _weeks(a: date, b: date) -> float:
    return round(((b - a).days + 1) / 7, 1)


def _span(days: int, a: date, b: date) -> str:
    """'12 wk (58 d)', or months past half a year."""
    weeks = _weeks(a, b)
    if weeks >= 26:
        return f"{round(((b - a).days + 1) / 30.44, 1):g} mo ({days} d)"
    return f"{weeks:g} wk ({days} d)"


def overview_data(s: Schedule, level: int = 1) -> dict:
    plan = s.plan
    wbs = plan.wbs()
    level = max(1, int(level or 1))

    def ancestor_at(tid: str) -> str | None:
        t = plan.tasks[tid]
        while t and plan.outline(t.id) > level:
            t = plan.tasks[t.parent] if t.parent else None
        return t.id if t and plan.outline(t.id) == level else None

    def critical(tid: str) -> bool:
        t = plan.tasks[tid]
        return any(plan.tasks[d].critical for d in plan.descendants(tid)) if t.summary else t.critical

    rows: list[dict] = []
    for tid in plan.order:
        t = plan.tasks[tid]
        if t.milestone or not (plan.outline(tid) == level or (t.key and plan.outline(tid) > level)):
            continue
        a, b = s.start_date(t), s.finish_date(t)
        leaves = plan.descendants(tid) if t.summary else [tid]
        days = max(plan.tasks[d].lev_ef for d in leaves) - min(plan.tasks[d].lev_es for d in leaves)
        rows.append({"id": tid, "wbs": wbs[tid], "name": t.name, "start": a.isoformat(), "finish": b.isoformat(),
                     "days": days, "span": _span(days, a, b), "critical": critical(tid),
                     "sub": plan.outline(tid) > level, "url": t.url, "subplan": t.subplan,
                     "tasks": len([d for d in leaves if not plan.tasks[d].milestone])})
    rows = rows[:MAX_WORKSTREAMS]
    row_of = {r["id"]: i for i, r in enumerate(rows)}

    def entry(t, kind):
        home = ancestor_at(t.id)
        return {"id": t.id, "name": t.name, "date": s.start_date(t).isoformat(), "kind": kind,
                "code": t.gate or "", "row": row_of.get(home), "workstream": plan.tasks[home].name if home else "",
                "critical": t.critical, "float": t.float_days,
                "deadline": t.deadline.isoformat() if t.deadline else None,
                "late": bool(t.deadline and s.start_date(t) > t.deadline)}

    stones = [plan.tasks[i] for i in plan.order if plan.tasks[i].milestone]
    gates = sorted((entry(t, "gate") for t in stones if t.gate), key=lambda m: m["date"])
    gateways = [entry(t, "gateway") for t in stones if t.gateway and not t.gate]
    others = [t for t in stones if not t.gate and not t.gateway]
    chosen = [t for t in others if t.key] or [t for t in others if plan.outline(t.id) <= level + 1] or others
    chosen.sort(key=lambda t: (s.start_date(t), plan.order.index(t.id)))
    if len(chosen) > MAX_MILESTONES:
        chosen = chosen[:MAX_MILESTONES - 1] + [chosen[-1]]
    milestones = sorted(gateways + [entry(t, "milestone") for t in chosen], key=lambda m: m["date"])
    start = s.calendar.start
    finish = s.finish_date_of(s.lev_finish)
    return {"name": plan.name, "start": start.isoformat(), "finish": finish.isoformat(),
            "duration": _span(s.lev_finish, start, finish),
            "status": plan.status_date.isoformat() if plan.status_date else None, "level": level,
            "workstreams": rows, "gates": gates, "milestones": milestones, "ticks": _ticks(start, finish)}


def _ticks(a: date, b: date) -> list[dict]:
    """Month starts (quarters past 18 months) between a and b, with labels."""
    months = (b.year - a.year) * 12 + b.month - a.month + 1
    step = 3 if months > 18 else 1
    d = date(a.year, a.month, 1)
    if step == 3:
        d = date(a.year, 1 + 3 * ((a.month - 1) // 3), 1)
    out = []
    while d <= b + timedelta(days=1):
        label = (f"Q{(d.month - 1) // 3 + 1} {d:%y}" if step == 3 else
                 (d.strftime("%b %y") if d.month == 1 or not out else d.strftime("%b")))
        out.append({"date": d.isoformat(), "label": label})
        m = d.month - 1 + step
        d = date(d.year + m // 12, m % 12 + 1, 1)
    return out


def _frac(data: dict):
    a, b = date.fromisoformat(data["start"]), date.fromisoformat(data["finish"])
    total = max((b - a).days + 1, 1)
    return lambda iso, end=False: ((date.fromisoformat(iso) - a).days + (1 if end else 0)) / total


def lanes(items: list[tuple[float, float]], max_lanes: int = 2 * MAX_LANES_PER_SIDE) -> list[int | None]:
    """Lanes for labels centred at x with width w (same units), so none overlap. Greedy, left to right: each label
    takes the first lane whose last label ends before it starts. None when every lane is taken (the label is left
    off; the viewer still shows it on hover)."""
    order = sorted(range(len(items)), key=lambda i: items[i][0])
    ends: list[float] = []
    out: list[int | None] = [None] * len(items)
    for i in order:
        x, w = items[i]
        for k in range(max_lanes):
            if k >= len(ends):
                ends.append(-math.inf)
            if x - w / 2 >= ends[k]:
                ends[k] = x + w / 2
                out[i] = k
                break
    return out


def _label(m: dict) -> tuple[str, str]:
    """(name line, date line) of a milestone or gate label."""
    name = m["name"]
    if m["kind"] == "gate" and m["code"] and name.upper().startswith(m["code"]):
        name = name[len(m["code"]):].strip(" -–—:")
    if len(name) > 34:
        name = name[:32].rstrip() + "…"
    return name, date.fromisoformat(m["date"]).strftime("%-d %b %y")


def layout(data: dict, char_frac: float) -> dict:
    """Lanes for the static renderers. char_frac: one label character's width as a fraction of the timeline.
    Each label is centred over its marker but kept inside the timeline (`gate_x`, `milestone_x`: where to centre
    it). Row milestone lanes alternate above (even) and below (odd) the bar; gate labels stack upwards in the band;
    gate badges that would touch go on a second badge row."""
    f = _frac(data)
    width = lambda m: (max(len(_label(m)[0]), len(_label(m)[1])) + 2) * char_frac  # noqa: E731
    centre = lambda x, w: min(max(x, w / 2), 1 - w / 2)  # noqa: E731
    gx = [centre(f(g["date"], end=True), width(g)) for g in data["gates"]]
    gate_level = lanes([(x, width(g) + 0.008) for x, g in zip(gx, data["gates"])], 6)
    badge_row = [ln or 0 for ln in lanes([(f(g["date"], end=True), 7 * char_frac) for g in data["gates"]], 2)]
    top_row = any(m["row"] is None for m in data["milestones"])
    keys = ([None] if top_row else []) + list(range(len(data["workstreams"])))
    mx = {i: centre(f(m["date"], end=True), width(m)) for i, m in enumerate(data["milestones"])}
    per_row: dict = {}
    for i, m in enumerate(data["milestones"]):
        per_row.setdefault(m["row"], []).append(i)
    lane_of: dict[int, int | None] = {}
    heights = []
    for key in keys:
        members = per_row.get(key, [])
        got = lanes([(mx[i], width(data["milestones"][i]) + 0.006) for i in members])
        lane_of.update(zip(members, got))
        used = [ln for ln in got if ln is not None]
        heights.append({"above": max([ln // 2 + 1 for ln in used if ln % 2 == 0], default=0),
                        "below": max([ln // 2 + 1 for ln in used if ln % 2 == 1], default=0)})
    band_levels = max([ln for ln in gate_level if ln is not None], default=-1) + 1
    return {"top_row": top_row, "heights": heights, "milestone_lane": lane_of, "gate_level": gate_level,
            "band_levels": band_levels, "badge_row": badge_row, "badge_rows": max(badge_row, default=0) + 1,
            "gate_x": gx, "milestone_x": mx}


# ---- PNG / SVG (matplotlib) -------------------------------------------------------------------------------------
def _figure(data: dict):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    family = next((n for n in (BODY_FONT, "Carlito", "DejaVu Sans")
                   if any(fm.name == n for fm in font_manager.fontManager.ttflist)), "DejaVu Sans")
    plt.rcParams.update({"font.family": family, "font.size": 10, "svg.fonttype": "none"})
    W_IN, LEFT, RIGHT = 13.33, 0.22, 0.985
    em = {BODY_FONT: 0.5, "Carlito": 0.5}.get(family, 0.6)  # average character width, in em
    L = layout(data, char_frac=em * 7.5 / 72 * 100 / (W_IN * (RIGHT - LEFT) * 100))
    BAR, LBL, GLBL, UNIT_IN = 0.9, 0.62, 0.62, 0.42  # row units; one unit is UNIT_IN inches
    y = 0.75 + L["band_levels"] * GLBL  # tick labels, then gate labels stacked above the badges
    band_y = y + 0.2
    y = band_y + 0.45 + (L["badge_rows"] - 1) * 0.42
    row_y = []
    for h in L["heights"]:
        y += h["above"] * LBL
        row_y.append(y + BAR / 2)
        y += BAR + h["below"] * LBL
    bottom = y + 0.1
    fig_h = UNIT_IN * bottom + 0.7
    fig, ax = plt.subplots(figsize=(W_IN, max(3.0, fig_h)), dpi=100)
    f = _frac(data)
    for i, t in enumerate(data["ticks"]):
        x = f(t["date"])
        if x < 0 and i == 0:
            ax.text(0.004, 0.3, t["label"], color="#" + MUTED, fontsize=9, va="center")
        elif 0 <= x <= 1:
            ax.plot([x, x], [0.5, bottom], color="#" + RULE, lw=0.6, zorder=0)
            ax.text(x + 0.004, 0.3, t["label"], color="#" + MUTED, fontsize=9, va="center")
    if data.get("status") and 0 <= f(data["status"]) <= 1:
        x = f(data["status"])
        ax.plot([x, x], [band_y, bottom], color="#" + ACCENT, lw=1, ls="--", zorder=1)
    if data["gates"]:
        ax.text(-0.012, band_y, "Gates", ha="right", va="center", color="#" + PRIMARY, fontsize=10, fontweight="bold")
    for g, lvl, brow in zip(data["gates"], L["gate_level"], L["badge_row"]):
        x = f(g["date"], end=True)
        colour = "#" + (ACCENT if g["late"] else PRIMARY)
        ax.plot([x, x], [band_y, bottom], color=colour, lw=1.1, ls=(0, (4, 3)), zorder=1, alpha=0.85)
        ax.text(x, band_y + brow * 0.42, f" {g['code'] or 'G'} ", ha="center", va="center", color="white", fontsize=8,
                fontweight="bold", zorder=5, bbox={"boxstyle": "round,pad=0.25", "fc": colour, "ec": "none"})
        if lvl is not None:
            name, when = _label(g)
            ax.text(L["gate_x"][data["gates"].index(g)], band_y - 0.32 - lvl * GLBL, f"{name}\n{when}", ha="center",
                    va="bottom", fontsize=7.5,
                    color="#" + INK, zorder=5, linespacing=1.0)
    names = (["Key milestones"] if L["top_row"] else []) + [r["name"] for r in data["workstreams"]]
    subs = ([False] if L["top_row"] else []) + [r["sub"] for r in data["workstreams"]]
    for i, (name, sub) in enumerate(zip(names, subs)):
        ax.text(-0.012, row_y[i], ("   " if sub else "") + (name if len(name) <= 44 else name[:42] + "…"),
                ha="right", va="center", color="#" + INK, fontsize=10 if not sub else 9,
                fontweight="bold" if (L["top_row"] and i == 0) else "normal")
    off = 1 if L["top_row"] else 0
    for i, r in enumerate(data["workstreams"]):
        yy = row_y[i + off]
        x0, x1 = f(r["start"]), f(r["finish"], end=True)
        ax.barh(yy, x1 - x0, left=x0, height=0.42 if not r["sub"] else 0.28,
                color="#" + (ACCENT if r["critical"] else PRIMARY), alpha=0.92 if not r["sub"] else 0.6, zorder=2)
        marked = any(m["row"] == i for m in data["milestones"])  # diamonds on the bar: the text goes after it
        inside = x1 - x0 > 0.13 and not marked
        ax.text(x0 + 0.006 if inside else x1 + 0.006, yy, r["span"], va="center",
                color="white" if inside else "#" + MUTED, fontsize=8.5, zorder=3)
    for i, m in enumerate(data["milestones"]):
        yy = row_y[(m["row"] + off) if m["row"] is not None else 0]
        x = f(m["date"], end=True)
        if m["kind"] == "gateway":
            ax.scatter([x], [yy], marker="s", s=55, facecolors="white", edgecolors="#" + PRIMARY, linewidths=1.6,
                       zorder=4)
        else:
            ax.scatter([x], [yy], marker="D", s=55, color="#" + (ACCENT if m["late"] else INK), zorder=4,
                       edgecolors="white", linewidths=0.8)
        ln = L["milestone_lane"].get(i)
        if ln is None:
            continue
        side, depth = (1, ln // 2) if ln % 2 == 0 else (-1, ln // 2)
        name, when = _label(m)
        ax.text(L["milestone_x"][i], yy - side * (0.3 + depth * LBL), f"{name}\n{when}", ha="center",
                va="bottom" if side > 0 else "top",
                fontsize=7.5, color="#" + INK, zorder=5, linespacing=1.0)
    ax.set_xlim(-0.01, 1.02)
    ax.set_ylim(bottom, 0)
    ax.axis("off")
    ax.set_title(f"{data['name']}  ·  {date.fromisoformat(data['start']):%-d %b %Y} – "
                 f"{date.fromisoformat(data['finish']):%-d %b %Y}  ·  {data['duration']}",
                 loc="left", fontsize=13, color="#" + INK, pad=10)
    fig.subplots_adjust(left=LEFT, right=RIGHT, top=1 - 0.55 / fig.get_figheight(), bottom=0.02)
    return fig, plt


def overview_png(data: dict) -> bytes:
    fig, plt = _figure(data)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=200, facecolor="white")
    plt.close(fig)
    return buf.getvalue()


def overview_svg(data: dict) -> bytes:
    fig, plt = _figure(data)
    buf = io.BytesIO()
    fig.savefig(buf, format="svg", facecolor="white")
    plt.close(fig)
    return buf.getvalue()


# ---- PowerPoint (native shapes) ---------------------------------------------------------------------------------
def overview_pptx(data: dict) -> bytes:
    """A branded deck: title slide, the overview as native shapes (editable in PowerPoint), and the gates and
    milestones as a table."""
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
    from pptx.util import Inches, Pt

    from ..deck import build_deck
    a, b = date.fromisoformat(data["start"]), date.fromisoformat(data["finish"])
    every = sorted(data["gates"] + data["milestones"], key=lambda m: m["date"])
    rows_md = [[(m["code"] + " " if m["kind"] == "gate" and not m["name"].upper().startswith(m["code"]) else "")
                + m["name"], {"gate": "Gate", "gateway": "Mid-stage gateway"}.get(m["kind"], "Milestone"),
                f"{date.fromisoformat(m['date']):%a %-d %b %Y}", m["workstream"] or "Programme",
                ("late: deadline " + m["deadline"]) if m["late"] else (m["deadline"] and "on time") or ""]
               for m in every]
    spec = {"title": data["name"], "subtitle": f"{a:%-d %b %Y} – {b:%-d %b %Y} · {data['duration']}",
            "slides": [{"kind": "table", "title": "Gates and *milestones*",
                        "columns": ["Gate or milestone", "Type", "Date", "Workstream", "Deadline"],
                        "rows": rows_md or [["(none)", "", "", "", ""]]}]}
    prs = Presentation(io.BytesIO(build_deck(spec)))
    want = brand().deck_layouts.get("bullets", "")  # the brand's title-only content layout
    layout_ = next((lo for lo in prs.slide_layouts if lo.name == want), None) or \
        next((lo for lo in prs.slide_layouts if lo.name == "Title Only"), prs.slide_layouts[-1])
    slide = prs.slides.add_slide(layout_)
    slide.shapes.title.text = "Stages, gates and milestones"
    for ph in list(slide.placeholders):  # no empty "click to add text" boxes under the chart
        if ph.placeholder_format.idx != 0:
            ph._element.getparent().remove(ph._element)
    ids = prs.slides._sldIdLst
    ids.insert(1, ids[-1])

    def rgb(h):
        return RGBColor.from_string(h)

    def text(x, y, w, h, s, size=10, colour=INK, bold=False, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.MIDDLE):
        box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
        tf = box.text_frame
        tf.word_wrap, tf.vertical_anchor = True, anchor
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        for i, line in enumerate(s.split("\n")):
            para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            para.alignment = align
            run = para.add_run()
            run.text = line
            run.font.size, run.font.bold, run.font.color.rgb = Pt(size), bold, rgb(colour)
        return box

    def label_box(x, w=1.6):
        lx = min(max(x - w / 2, x0), right - w)
        return lx, (PP_ALIGN.RIGHT if lx < x - w / 2 else PP_ALIGN.LEFT if lx > x - w / 2 else PP_ALIGN.CENTER)

    left, label_w, right, top, bottom = 0.76, 2.9, 12.57, 2.25, 6.9
    x0 = left + label_w + 0.15
    width = right - x0
    L = layout(data, char_frac=0.058 / width)  # ~0.058 in a character at 7.5 pt (Calibri, with margin)
    LBL, BAR, GLBL = 0.3, 0.42, 0.3
    need = 0.3 + L["band_levels"] * GLBL + 0.45 + sum(h["above"] * LBL + BAR + h["below"] * LBL for h in L["heights"])
    k = min(1.0, (bottom - top) / need)
    LBL, BAR, GLBL = LBL * k, BAR * k, GLBL * k
    fsz = max(6.5, 7.5 * k)
    f = _frac(data)
    X = lambda frac: x0 + width * frac  # noqa: E731
    band_y = top + 0.3 + L["band_levels"] * GLBL + 0.12
    y = band_y + 0.3 + (L["badge_rows"] - 1) * 0.24
    row_y = []
    for h in L["heights"]:
        y += h["above"] * LBL
        row_y.append(y + BAR / 2)
        y += BAR + h["below"] * LBL
    grid_bottom = y
    for i, t in enumerate(data["ticks"]):
        x = f(t["date"])
        if x < 0 and i == 0:
            text(X(0) + 0.03, top, 1.0, 0.25, t["label"], size=9, colour=MUTED)
        elif 0 <= x <= 1:
            ln = slide.shapes.add_connector(1, Inches(X(x)), Inches(top + 0.28), Inches(X(x)), Inches(grid_bottom))
            ln.line.color.rgb, ln.line.width = rgb(RULE), Pt(0.5)
            text(X(x) + 0.03, top, 1.0, 0.25, t["label"], size=9, colour=MUTED)
    if data.get("status") and 0 <= f(data["status"]) <= 1:
        x = X(f(data["status"]))
        ln = slide.shapes.add_connector(1, Inches(x), Inches(band_y), Inches(x), Inches(grid_bottom))
        ln.line.color.rgb, ln.line.width, ln.line.dash_style = rgb(ACCENT), Pt(1), 4
    if data["gates"]:
        text(left, band_y - 0.15, label_w, 0.3, "Gates", size=11, bold=True, colour=PRIMARY, align=PP_ALIGN.RIGHT)
    for g, lvl, brow in zip(data["gates"], L["gate_level"], L["badge_row"]):
        x = X(f(g["date"], end=True))
        colour = ACCENT if g["late"] else PRIMARY
        ln = slide.shapes.add_connector(1, Inches(x), Inches(band_y), Inches(x), Inches(grid_bottom))
        ln.line.color.rgb, ln.line.width, ln.line.dash_style = rgb(colour), Pt(1), 7
        badge = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x - 0.17), Inches(band_y - 0.1 + brow * 0.24),
                                       Inches(0.34), Inches(0.2))
        badge.fill.solid()
        badge.fill.fore_color.rgb = rgb(colour)
        badge.line.fill.background()
        tf = badge.text_frame
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        tf.paragraphs[0].alignment = PP_ALIGN.CENTER
        run = tf.paragraphs[0].add_run()
        run.text = g["code"] or "G"
        run.font.size, run.font.bold, run.font.color.rgb = Pt(7.5), True, rgb("FFFFFF")
        if lvl is not None:
            name, when = _label(g)
            lx, align = label_box(x)
            text(lx, band_y - 0.14 - (lvl + 1) * GLBL, 1.6, GLBL, f"{name}\n{when}", size=fsz, align=align,
                 anchor=MSO_ANCHOR.BOTTOM)
    names = (["Key milestones"] if L["top_row"] else []) + [r["name"] for r in data["workstreams"]]
    subs = ([False] if L["top_row"] else []) + [r["sub"] for r in data["workstreams"]]
    for i, (name, sub) in enumerate(zip(names, subs)):
        text(left, row_y[i] - 0.2, label_w, 0.4, name, size=10.5 if not sub else 9, align=PP_ALIGN.RIGHT,
             colour=INK if not sub else MUTED, bold=bool(L["top_row"] and i == 0))
    off = 1 if L["top_row"] else 0
    for i, r in enumerate(data["workstreams"]):
        yy = row_y[i + off]
        xa, xb = X(f(r["start"])), X(f(r["finish"], end=True))
        h = BAR * (0.55 if not r["sub"] else 0.38)
        bar = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(xa), Inches(yy - h / 2),
                                     Inches(max(xb - xa, 0.05)), Inches(h))
        bar.fill.solid()
        bar.fill.fore_color.rgb = rgb(ACCENT if r["critical"] else PRIMARY)
        bar.line.fill.background()
        bar.adjustments[0] = 0.25
        if r.get("url"):
            bar.click_action.hyperlink.address = r["url"]
        inside = xb - xa > 1.25 and not any(m["row"] == i for m in data["milestones"])
        text(xa + 0.06 if inside else xb + 0.06, yy - 0.13, 1.6, 0.26, r["span"], size=9,
             colour="FFFFFF" if inside else MUTED)
    for i, m in enumerate(data["milestones"]):
        yy = row_y[(m["row"] + off) if m["row"] is not None else 0]
        x = X(f(m["date"], end=True))
        d = 0.16
        if m["kind"] == "gateway":
            mk = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x - d / 2), Inches(yy - d / 2), Inches(d), Inches(d))
            mk.fill.solid()
            mk.fill.fore_color.rgb = rgb("FFFFFF")
            mk.line.color.rgb, mk.line.width = rgb(PRIMARY), Pt(1.5)
        else:
            mk = slide.shapes.add_shape(MSO_SHAPE.DIAMOND, Inches(x - d / 2), Inches(yy - d / 2), Inches(d), Inches(d))
            mk.fill.solid()
            mk.fill.fore_color.rgb = rgb(ACCENT if m["late"] else INK)
            mk.line.color.rgb = rgb("FFFFFF")
        ln = L["milestone_lane"].get(i)
        if ln is None:
            continue
        side, depth = (1, ln // 2) if ln % 2 == 0 else (-1, ln // 2)
        name, when = _label(m)
        lx, align = label_box(x)
        ly = yy - 0.1 - (depth + 1) * LBL if side > 0 else yy + 0.1 + depth * LBL
        text(lx, ly, 1.6, LBL, f"{name}\n{when}", size=fsz, align=align,
             anchor=MSO_ANCHOR.BOTTOM if side > 0 else MSO_ANCHOR.TOP)
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()
