"""Views of a scheduled plan: a self-contained HTML viewer (MS Project-style) and Mermaid.

The viewer needs no external scripts, so it works as an email attachment, in SharePoint or offline. Tabs:
  Gantt       task table (WBS, name, owner, duration, start, finish, float, predecessors) beside the timeline:
              summaries (collapsible), milestones, critical path, dependency arrows, baseline, levelling delay,
              deadlines, today and status lines; levelled/unlevelled, critical-only, zoom day/week/month
  Milestones  dates, float, deadline met or missed
  Resources   weekly load per person (over 100% red), levelled or unlevelled, and each person's tasks
  Health      the schedule checks (health.py)
"""

from __future__ import annotations

import html
import json
import re
from datetime import date, timedelta

from .health import check
from .schedule import Schedule
from ..org import localise


def plan_data(s: Schedule) -> dict:
    plan, cal = s.plan, s.calendar
    wbs = plan.wbs()
    rows = []
    for tid in plan.order:
        t = plan.tasks[tid]
        preds = ", ".join(f"{wbs[link.pred]}{link.type if link.type != 'FS' or link.lag else ''}"
                          f"{('+' if link.lag > 0 else '') + str(link.lag) + 'd' if link.lag else ''}" for link in t.links)
        row = {"id": tid, "wbs": wbs[tid], "name": t.name, "level": plan.outline(tid), "summary": t.summary,
               "milestone": t.milestone, "critical": t.critical, "parent": t.parent, "float": t.float_days,
               "preds": preds, "pred_ids": [link.pred for link in t.links],
               "pred_links": [{"id": link.pred, "type": link.type} for link in t.links], "pct": t.percent_complete,
               "owners": ", ".join(f"{plan.resources[a.resource].name}" + (f" {a.units:.0%}" if a.units != 1 else "")
                                   for a in t.assignments),
               "jira": (plan.jira_base + t.jira) if t.jira else None, "jira_key": t.jira, "notes": t.notes,
               "document": t.document, "deadline": t.deadline.isoformat() if t.deadline else None,
               "url": t.url, "subplan": t.subplan, "gate": t.gate, "gateway": t.gateway,
               "baseline": [t.baseline_start.isoformat() if t.baseline_start else None,
                            t.baseline_finish.isoformat() if t.baseline_finish else None]}
        for key, lev in (("u", False), ("l", True)):
            es, ef = (t.lev_es, t.lev_ef) if lev else (t.es, t.ef)
            row[key] = {"start": s.start_date(t, lev).isoformat(), "finish": s.finish_date(t, lev).isoformat(),
                        "days": ef - es, "delay": (t.lev_es - t.es) if lev and not t.summary else 0}
        rows.append(row)
    first = cal.start
    last = s.finish_date_of(max(s.finish, s.lev_finish))
    weeks, d = [], first - timedelta(days=first.weekday())
    while d <= last:
        weeks.append(d)
        d += timedelta(days=7)
    resources = []
    for r in plan.resources.values():
        if r.kind != "work":
            continue
        loads = {}
        for key, load in (("u", s.load), ("l", s.lev_load)):
            per_week = []
            for w in weeks:
                idx = [cal.index_of(w + timedelta(days=k)) for k in range(5) if cal.is_working(w + timedelta(days=k))]
                idx = [i for i in idx if i >= 0]
                used = sum(load.get(r.id, {}).get(i, 0.0) for i in set(idx))
                cap = len(set(idx)) * r.max_units
                per_week.append(round(used / cap, 2) if cap else 0)
            loads[key] = per_week
        tasks = [row["id"] for row, t in zip(rows, [plan.tasks[i] for i in plan.order])
                 if any(a.resource == r.id for a in t.assignments)]
        resources.append({"id": r.id, "name": r.name, "role": r.role, "max": r.max_units, "load": loads,
                          "tasks": tasks, "leave": [[a.isoformat(), b.isoformat()] for a, b in r.leave if a and b]})
    return {"name": plan.name, "start": first.isoformat(), "end": last.isoformat(),
            "status": plan.status_date.isoformat() if plan.status_date else None, "today": date.today().isoformat(),
            "finish": {"u": s.finish_date_of(s.finish).isoformat(), "l": s.finish_date_of(s.lev_finish).isoformat()},
            "holidays": {k.isoformat(): v for k, v in cal.non_working_between(first, last + timedelta(days=30)).items()},
            "weeks": [w.isoformat() for w in weeks], "tasks": rows, "resources": resources, "health": check(s),
            "overview": _overviews(s), "method": _method_site()}


def _overviews(s: Schedule) -> dict:
    """The Overview tab's data: stage level, and one level down when that shows more."""
    from .overview import overview_data
    one = overview_data(s, 1)
    two = overview_data(s, 2)
    return {"1": one, **({"2": two} if len(two["workstreams"]) > len(one["workstreams"]) else {})}


def _method_site() -> str | None:
    try:
        from ..method import site_base
        return site_base()
    except Exception:
        return None


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#f6f7f9;--panel:#fff;--ink:#18202b;--muted:#5f6b7a;--line:#e2e6ec;--soft:#eef1f5;--bar:#1B4B8A;--crit:#c62828;
--sum:#2b2f36;--ms:#F5A623;--base:#b8c0cc;--ok:#1f8a4c;--warn:#b7791f;--grid:#f0f2f5;--hol:#eceff3;--dep:#4a5a70;--gate:#2A5A3D}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#0f1318;--panel:#161b22;--ink:#e6edf3;--muted:#8b98a8;
--line:#2a313b;--soft:#1d242d;--bar:#6ea8ff;--crit:#ff7b72;--sum:#c9d1d9;--base:#4a5563;--ok:#4cc38a;--warn:#e3b341;--grid:#1a2028;--hol:#212833;--dep:#9aa7b8;--gate:#5fb383}}
:root[data-theme=dark]{--bg:#0f1318;--panel:#161b22;--ink:#e6edf3;--muted:#8b98a8;
--line:#2a313b;--soft:#1d242d;--bar:#6ea8ff;--crit:#ff7b72;--sum:#c9d1d9;--base:#4a5563;--ok:#4cc38a;--warn:#e3b341;--grid:#1a2028;--hol:#212833;--dep:#9aa7b8;--gate:#5fb383}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:13px/1.4 system-ui,-apple-system,"Segoe UI",sans-serif}
header{background:var(--panel);border-bottom:1px solid var(--line);padding:10px 16px;display:flex;flex-wrap:wrap;gap:8px 18px;align-items:baseline}
h1{font-size:16px;margin:0}.dl{margin-left:auto;color:var(--bar)}.kpi{color:var(--muted)}.kpi b{color:var(--ink)}
nav{display:flex;gap:4px;padding:8px 16px;flex-wrap:wrap;align-items:center}
nav button,.ctl button,.ctl select{font:inherit;padding:4px 10px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--ink);cursor:pointer}
nav button.on,.ctl button.on{background:var(--bar);color:#fff;border-color:var(--bar)}
.ctl{display:flex;gap:6px;flex-wrap:wrap;margin-left:auto;align-items:center}
main{padding:0 16px 30px}.tab{display:none}.tab.on{display:block}
.gantt{display:grid;grid-template-columns:minmax(320px,46%) 1fr;border:1px solid var(--line);background:var(--panel);border-radius:8px;overflow:hidden}
.tbl{overflow:auto;border-right:1px solid var(--line)}.tl{overflow:auto}
table{border-collapse:collapse;width:100%;white-space:nowrap}th,td{padding:0 6px;height:26px;border-bottom:1px solid var(--grid);text-align:left}
th{position:sticky;top:0;background:var(--soft);height:40px;font-weight:600;z-index:1}
td.n{max-width:340px;overflow:hidden;text-overflow:ellipsis}td.o{max-width:130px;overflow:hidden;text-overflow:ellipsis}tr.sum td{font-weight:600;background:var(--soft)}tr.l1.sum td{box-shadow:inset 0 1px var(--line)}tr.crit td.n{color:var(--crit)}
tr:hover td{background:var(--soft)}.car{cursor:pointer;display:inline-block;width:14px;color:var(--muted)}
.num{text-align:right;font-variant-numeric:tabular-nums}a{color:var(--bar)}
svg text{fill:var(--ink);font-size:11px}.lbl{fill:var(--muted)}
.cards{display:grid;gap:8px}.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.sev-error{border-left:4px solid var(--crit)}.sev-warning{border-left:4px solid var(--warn)}.sev-info{border-left:4px solid var(--base)}
.heat td{text-align:center;font-variant-numeric:tabular-nums;min-width:44px;height:28px}
#detail{position:fixed;right:16px;bottom:16px;max-width:420px;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px;box-shadow:0 6px 24px rgba(0,0,0,.18);display:none}
.ov{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px 6px;overflow:hidden}
.ovbar{display:flex;gap:6px;align-items:center;margin:0 0 8px;flex-wrap:wrap}.ovbar .kpi{margin-left:6px}
.ovbar button{font:inherit;padding:3px 9px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--ink);cursor:pointer}
.ovbar button.on{background:var(--bar);color:#fff;border-color:var(--bar)}
.ovh{cursor:pointer}.ovrow:hover rect:first-child{fill:var(--soft)}.badge{fill:#fff!important;font-size:10px;font-weight:700}
.gatehead{fill:var(--gate)!important;font-weight:700}.inbar{fill:#fff!important}
#tip{position:fixed;z-index:5;pointer-events:none;max-width:340px;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:8px 10px;box-shadow:0 6px 20px rgba(0,0,0,.18);display:none}
.gb{display:inline-block;background:var(--gate);color:#fff;border-radius:3px;padding:0 4px;font-size:10px;font-weight:700;line-height:15px}
.gw{display:inline-block;width:9px;height:9px;border:2px solid var(--gate);margin-right:3px;vertical-align:-1px}
tr.flash td{background:#fff3c4!important;color:#18202b}
.chip{display:inline-block;padding:0 7px;border-radius:9px;font-size:11px;line-height:18px;border:1px solid var(--line);white-space:nowrap}
.c-done{background:var(--ok);color:#fff;border-color:var(--ok)}.c-rev{background:var(--soft)}.c-none{color:var(--muted)}
.c-late{background:var(--crit);color:#fff;border-color:var(--crit)}.c-risk{background:var(--warn);color:#fff;border-color:var(--warn)}
.dstage{margin:12px 0 4px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}.dstage h3{margin:0;font-size:14px}
.dtab td{white-space:normal;vertical-align:top;padding:5px 6px;height:auto}.dtab td.id{font-family:Consolas,monospace;white-space:nowrap}
.dctl{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:6px}.dctl input,.dctl select{font:inherit;padding:3px 8px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--ink)}
@media print{nav,.ctl,#detail,#tip,.ovbar{display:none}.tab{display:block!important}.gantt{display:block}.tl,.tbl{overflow:visible}}
@media (max-width:700px){.gantt{grid-template-columns:1fr}.tbl{max-height:none}}
</style></head><body>
<header><h1 id="t"></h1><span class="kpi" id="k"></span>__DOWNLOAD__</header>
<nav><button data-tab="o" class="on">Overview</button><button data-tab="g">Gantt</button><button data-tab="d" id="dtabbtn" style="display:none">Deliverables</button><button data-tab="m">Milestones</button><button data-tab="r">Resources</button><button data-tab="h">Health</button>
<div class="ctl"><button id="lev" class="on" title="Show the resource-levelled schedule">Levelled</button><button id="crit">Critical only</button>
<button id="deps" class="on" title="Show dependency lines">Dependencies</button><button id="fold" title="Show only the stages (top level)">Stages</button><button id="unfold" title="Show every task">Expand all</button><select id="zoom" aria-label="Zoom"><option value="18">Day</option><option value="5" selected>Week</option><option value="1.6">Month</option></select>
<button id="theme" title="Light/dark">◐</button></div></nav>
<main><section class="tab on" id="o"><div class="ov"><div class="ovbar" id="ovbar"></div><div id="ovc"></div></div></section>
<section class="tab" id="g"><div class="gantt"><div class="tbl" id="tbl"></div><div class="tl" id="tl"></div></div></section>
<section class="tab" id="d"></section><section class="tab" id="m"></section><section class="tab" id="r"></section><section class="tab" id="h"></section></main>
<div id="detail"></div><div id="tip"></div>
<script>
"use strict";
const P = __DATA__;
const st = {lev: true, crit: false, deps: true, px: 5, closed: new Set()};
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const D = s => new Date(s + "T00:00:00Z"), DAY = 864e5;
const fmt = s => s ? D(s).toLocaleDateString(undefined, {day: "2-digit", month: "short", year: "2-digit", timeZone: "UTC"}) : "";
const k = () => st.lev ? "l" : "u";
$("t").textContent = P.name;
function kpis() {
  const errs = P.health.filter(h => h.severity === "error").length, warns = P.health.filter(h => h.severity === "warning").length;
  $("k").innerHTML = `Start <b>${fmt(P.start)}</b> · Finish <b>${fmt(P.finish[k()])}</b> ${st.lev ? "(levelled)" : "(unlevelled)"}` +
    (P.finish.l !== P.finish.u ? ` · levelling adds <b>${Math.round((D(P.finish.l) - D(P.finish.u)) / DAY)}</b> days` : "") +
    ` · ${P.tasks.filter(t => !t.summary).length} tasks · ${P.tasks.filter(t => t.critical).length} critical` +
    ` · health: <b style="color:var(--crit)">${errs}</b> errors, <b style="color:var(--warn)">${warns}</b> warnings` +
    (P.status ? ` · status date ${fmt(P.status)}` : "");
}
function visible() {
  const hidden = new Set();
  P.tasks.forEach(t => { if (t.parent && (st.closed.has(t.parent) || hidden.has(t.parent))) hidden.add(t.id); });
  return P.tasks.filter(t => !hidden.has(t.id) && (!st.crit || t.critical || t.summary || t.milestone));
}
function gantt() {
  const rows = visible(), RH = 26, HEAD = 40, x0 = D(P.start).getTime() - 7 * DAY, x1 = D(P.end).getTime() + 21 * DAY;
  const W = Math.max(600, (x1 - x0) / DAY * st.px), H = HEAD + rows.length * RH + 4, X = s => (D(s).getTime() - x0) / DAY * st.px;
  $("tbl").innerHTML = `<table><tr><th>WBS</th><th>Task</th><th>Owner</th><th class="num">Dur</th><th>Start</th><th>Finish</th><th class="num">Float</th><th>Pred</th></tr>` +
    rows.map(t => { const s = t[k()]; return `<tr data-id="${t.id}" class="l${t.level} ${t.summary ? "sum" : ""} ${t.critical ? "crit" : ""}">
      <td>${esc(t.wbs)}</td><td class="n" style="padding-left:${6 + (t.level - 1) * 22}px">${t.summary ? `<span class="car" data-c="${t.id}">${st.closed.has(t.id) ? "▸" : "▾"}</span>` : `<span class="car"></span>`}${t.gate ? `<span class="gb">${esc(t.gate)}</span> ` : t.gateway ? `<span class="gw"></span>` : t.milestone ? "◆ " : ""}${esc(t.name)}${t.url ? ` <a href="${esc(t.url)}" target="_blank" rel="noopener" title="${t.subplan ? "Open this sub-plan" : "Open link"}">↗</a>` : ""}</td>
      <td class="o" title="${esc(t.owners)}">${esc(t.owners)}</td><td class="num">${t.milestone ? "0" : s.days}d</td><td>${fmt(s.start)}</td><td>${fmt(s.finish)}</td>
      <td class="num">${t.summary ? "" : t.float + "d"}</td><td>${esc(t.preds)}</td></tr>`; }).join("") + `</table>`;
  let svg = `<svg width="${W}" height="${H}" xmlns="http://www.w3.org/2000/svg">`;
  for (let d = x0; d <= x1; d += DAY) {  // weekends and holidays
    const iso = new Date(d).toISOString().slice(0, 10), wd = new Date(d).getUTCDay(), x = (d - x0) / DAY * st.px;
    if (wd === 0 || wd === 6 || P.holidays[iso]) svg += `<rect x="${x}" y="${HEAD}" width="${st.px}" height="${H}" fill="var(--hol)"><title>${esc(P.holidays[iso] || "")}</title></rect>`;
    if (new Date(d).getUTCDate() === 1) svg += `<line x1="${x}" x2="${x}" y1="0" y2="${H}" stroke="var(--line)"/><text x="${x + 3}" y="14" class="lbl">${new Date(d).toLocaleDateString(undefined, {month: "short", year: "numeric", timeZone: "UTC"})}</text>`;
    if (wd === 1 && st.px >= 4) svg += `<text x="${x + 2}" y="32" class="lbl">${new Date(d).getUTCDate()}</text><line x1="${x}" x2="${x}" y1="20" y2="${H}" stroke="var(--grid)"/>`;
  }
  const ypos = {};
  rows.forEach((t, i) => {
    const s = t[k()], y = HEAD + i * RH, xs = X(s.start), xe = X(s.finish) + st.px; ypos[t.id] = y + RH / 2;
    const col = t.critical ? "var(--crit)" : "var(--bar)";
    if (t.baseline[0] && t.baseline[1]) svg += `<rect x="${X(t.baseline[0])}" y="${y + 18}" width="${Math.max(2, X(t.baseline[1]) + st.px - X(t.baseline[0]))}" height="4" fill="var(--base)"><title>Baseline ${fmt(t.baseline[0])} – ${fmt(t.baseline[1])}</title></rect>`;
    if (t.milestone && t.gate) { const cx = X(s.start) + st.px; svg += `<g><rect x="${cx - 13}" y="${y + 5}" width="26" height="16" rx="3" fill="var(--gate)"/><text x="${cx}" y="${y + 17}" text-anchor="middle" class="badge">${esc(t.gate)}</text><title>${esc(t.name)} ${fmt(s.start)}</title></g><text x="${cx + 16}" y="${y + 17}">${esc(t.name)} ${fmt(s.start)}</text>`; }
    else if (t.milestone && t.gateway) { const cx = X(s.start) + st.px; svg += `<rect x="${cx - 6}" y="${y + 7}" width="12" height="12" fill="var(--panel)" stroke="var(--gate)" stroke-width="2"><title>${esc(t.name)} ${fmt(s.start)} (mid-stage gateway)</title></rect><text x="${cx + 11}" y="${y + 17}">${esc(t.name)} ${fmt(s.start)}</text>`; }
    else if (t.milestone) { const cx = X(s.start) + st.px; svg += `<path d="M${cx} ${y + 5} l8 8 -8 8 -8 -8z" fill="var(--ms)" stroke="${col}"><title>${esc(t.name)} ${fmt(s.start)}</title></path><text x="${cx + 11}" y="${y + 17}">${esc(t.name)} ${fmt(s.start)}</text>`; }
    else if (t.summary) svg += `<path d="M${xs} ${y + 8} H${xe} v10 l-4 -4 H${xs + 4} l-4 4z" fill="var(--sum)"><title>${esc(t.name)}</title></path>`;
    else {
      if (s.delay > 0) svg += `<line x1="${X(t.u.start)}" x2="${xs}" y1="${y + 13}" y2="${y + 13}" stroke="var(--muted)" stroke-dasharray="2 3"><title>Levelling delay ${s.delay} working days</title></line>`;
      svg += `<rect data-id="${t.id}" x="${xs}" y="${y + 6}" width="${Math.max(3, xe - xs)}" height="14" rx="3" fill="${col}" opacity="0.88"><title>${esc(t.name)}: ${fmt(s.start)} – ${fmt(s.finish)} (${s.days}d, float ${t.float}d)</title></rect>`;
      if (t.pct) svg += `<rect x="${xs}" y="${y + 11}" width="${(xe - xs) * t.pct / 100}" height="4" fill="#fff" opacity="0.7"/>`;
      if (st.px >= 4) svg += `<text x="${xe + 4}" y="${y + 17}" class="lbl">${esc(t.owners)}</text>`;
    }
    if (t.deadline) { const xd = X(t.deadline) + st.px, miss = D(s.finish) > D(t.deadline); svg += `<path d="M${xd} ${y + 4} v18 M${xd} ${y + 4} l-5 -4 h10z" stroke="${miss ? "var(--crit)" : "var(--ok)"}" fill="${miss ? "var(--crit)" : "var(--ok)"}"><title>Deadline ${fmt(t.deadline)}${miss ? " (missed)" : ""}</title></path>`; }
  });
  // Dependency lines, MS Project style: out of the predecessor's start (S) or finish (F), into the successor's start
  // (from the left, or dropping onto the bar when there's room) or finish (from the right). Critical links are red.
  if (st.deps) { const lines = []; rows.forEach(t => (t.pred_links || []).forEach(({id: pid, type}) => {
    const p = P.tasks.find(x => x.id === pid); if (!p || ypos[pid] === undefined) return;
    const ps = p[k()], ts = t[k()], y1 = ypos[pid], y2 = ypos[t.id], down = y2 > y1 ? 1 : -1;
    const edge = (task, s, which) => task.milestone ? X(s.start) + st.px + (which === "S" ? -8 : 8) : (which === "S" ? X(s.start) : X(s.finish) + st.px);
    const x1 = edge(p, ps, type[0]), x2 = edge(t, ts, type[1]), out = type[0] === "S" ? x1 - 6 : x1 + 6;
    const col = p.critical && t.critical ? "var(--crit)" : "var(--dep)", top = y2 - down * (t.milestone ? 9 : 7);
    let d, head;
    if (type[1] === "S" && x2 + 3 >= out && type[0] === "F") {  // room: across, then down onto the bar
      const xd = Math.max(out, x2 + 3); d = `M${x1} ${y1} H${xd} V${top}`;
      head = `M${xd} ${top} l-3 ${-down * 5} h6z`;
    } else if (type[1] === "S") {  // overlap or from a start: out, along the row gap, back, into the start
      const gap = y2 - down * RH / 2, xin = x2 - 7; d = `M${x1} ${y1} H${out} V${gap} H${xin} V${y2} H${x2 - 1}`;
      head = `M${x2 - 1} ${y2} l-5 -3 v6z`;
    } else {  // into a finish: from the right
      const xr = Math.max(out, x2 + 7); d = `M${x1} ${y1} H${xr} V${y2} H${x2 + 1}`;
      head = `M${x2 + 1} ${y2} l5 -3 v6z`;
    }
    lines.push(`<g class="dep"><path d="${d}" fill="none" stroke="${col}" stroke-width="1.2"/><path d="${head}" fill="${col}"/><title>${esc(p.wbs)} ${esc(p.name)} → ${esc(t.wbs)} ${esc(t.name)} (${type})</title></g>`);
  })); svg += lines.join(""); }
  for (const [name, val, colr] of [["Today", P.today, "var(--warn)"], ["Status", P.status, "var(--ok)"]]) if (val) {
    const x = X(val); svg += `<line x1="${x}" x2="${x}" y1="${HEAD - 6}" y2="${H}" stroke="${colr}" stroke-dasharray="4 3"/><text x="${x + 3}" y="${HEAD - 8}" style="fill:${colr}">${name}</text>`;
  }
  $("tl").innerHTML = svg + `</svg>`;
  const first = rows.find(t => !t.summary);
  if (first && !gantt.scrolled) { $("tl").scrollLeft = Math.max(0, X(first[k()].start) - 40); gantt.scrolled = true; }
}
function milestones() {
  const ms = P.tasks.filter(t => t.milestone);
  $("m").innerHTML = ms.length ? `<div class="card"><table><tr><th>Milestone</th><th>Date</th><th>Unlevelled</th><th class="num">Float</th><th>Deadline</th><th>Jira</th></tr>` +
    ms.map(t => { const miss = t.deadline && D(t.l.start) > D(t.deadline); return `<tr data-id="${t.id}"><td>${t.gate ? `<span class="gb">${esc(t.gate)}</span>` : t.gateway ? `<span class="gw"></span>` : "◆"} ${esc(t.name)}</td><td><b>${fmt(t[k()].start)}</b></td><td>${fmt(t.u.start)}</td><td class="num">${t.float}d</td>
    <td style="color:${t.deadline ? (miss ? "var(--crit)" : "var(--ok)") : "inherit"}">${t.deadline ? fmt(t.deadline) + (miss ? " missed" : " met") : ""}</td><td>${t.jira ? `<a href="${t.jira}" target="_blank" rel="noopener">${esc(t.jira_key)}</a>` : ""}</td></tr>`; }).join("") + `</table></div>`
    : `<div class="card">No milestones in this plan.</div>`;
}
function resources() {
  const head = P.weeks.map(w => `<th>${fmt(w).slice(0, 6)}</th>`).join("");
  const cell = v => { const pct = Math.round(v * 100), bg = v > 1.001 ? "var(--crit)" : v > 0.8 ? "var(--warn)" : v > 0 ? "var(--bar)" : "transparent";
    return `<td style="background:${bg};color:${v > 0 ? "#fff" : "var(--muted)"};opacity:${v > 0 ? Math.min(1, 0.35 + v * 0.65) : 1}">${v > 0 ? pct + "%" : "·"}</td>`; };
  $("r").innerHTML = `<div class="card" style="overflow:auto"><p class="kpi">Weekly load as % of each person's availability (${st.lev ? "levelled" : "unlevelled"} schedule). Red is over-allocated.</p>
    <table class="heat"><tr><th>Person</th>${head}</tr>${P.resources.map(r => `<tr><td style="text-align:left"><b>${esc(r.name)}</b>${r.role ? `<br><span class="kpi">${esc(r.role)}</span>` : ""}</td>${r.load[k()].map(cell).join("")}</tr>`).join("")}</table></div>
    <div class="cards" style="margin-top:8px">${P.resources.map(r => `<div class="card"><b>${esc(r.name)}</b> ${r.max !== 1 ? `(${Math.round(r.max * 100)}% available)` : ""}
    ${r.leave.length ? `<span class="kpi"> · leave ${r.leave.map(l => fmt(l[0]) + "–" + fmt(l[1])).join(", ")}</span>` : ""}
    <table>${r.tasks.map(id => P.tasks.find(t => t.id === id)).map(t => `<tr><td>${esc(t.wbs)}</td><td style="padding-left:${6 + (t.level - 1) * 22}px">${esc(t.name)}</td><td>${fmt(t[k()].start)}</td><td>${fmt(t[k()].finish)}</td></tr>`).join("")}</table></div>`).join("")}</div>`;
}
function health() {
  $("h").innerHTML = `<div class="cards">${P.health.length ? P.health.map(h => `<div class="card sev-${h.severity}"><b>${esc(h.check)}</b> <span class="kpi">${h.severity}</span><br>${esc(h.message)}</div>`).join("") : `<div class="card">No issues found.</div>`}</div>`;
}
function detail(id) {
  const t = P.tasks.find(x => x.id === id); if (!t) return;
  const s = t[k()], box = $("detail");
  box.innerHTML = `<b>${esc(t.wbs)} ${esc(t.name)}</b> <a href="#" id="dx" style="float:right">close</a><br>${fmt(s.start)} – ${fmt(s.finish)} · ${s.days}d · float ${t.float}d${t.critical ? " · <b style='color:var(--crit)'>critical</b>" : ""}
    ${s.delay ? `<br>Levelling delay: ${s.delay} working days` : ""}${t.owners ? `<br>Owner: ${esc(t.owners)}` : ""}${t.preds ? `<br>Predecessors: ${esc(t.preds)}` : ""}
    ${t.gate ? `<br>Gate ${esc(t.gate)}: closes its stage${P.method ? ` · <a href="${P.method}gates/${esc(t.gate.toLowerCase())}/" target="_blank" rel="noopener">${esc(t.gate)} in the {org} method</a>` : ""}` : ""}${t.gateway ? "<br>Mid-stage gateway" : ""}
    ${t.deadline ? `<br>Deadline: ${fmt(t.deadline)}` : ""}${t.jira ? `<br>Jira: <a href="${t.jira}" target="_blank" rel="noopener">${esc(t.jira_key)}</a>` : ""}
    ${t.document ? `<br>Document: ${esc(t.document)}` : ""}${t.url ? `<br><a href="${esc(t.url)}" target="_blank" rel="noopener">${t.subplan ? "Open the sub-plan" : "Open link"}</a>` : ""}${t.notes ? `<p>${esc(t.notes)}</p>` : ""}`;
  box.style.display = "block"; $("dx").onclick = e => { e.preventDefault(); box.style.display = "none"; };
}
// ---- Overview: stages, gates and key milestones; hover for detail, click to drill into the Gantt ----
st.ovl = "1";
const PARENT = {}; P.tasks.forEach(t => PARENT[t.id] = t.parent);
const ctx2d = document.createElement("canvas").getContext("2d");
const tw = (s, px = 11) => { ctx2d.font = `${px}px system-ui,-apple-system,"Segoe UI",sans-serif`; return ctx2d.measureText(s).width; };
function lanesJS(items, max) {  // same as overview.lanes: first lane whose last label ends before this one starts
  const order = items.map((_, i) => i).sort((p, q) => items[p][0] - items[q][0]), ends = [], out = items.map(() => null);
  for (const i of order) { const [x, w] = items[i]; for (let k = 0; k < max; k++) { if (k >= ends.length) ends.push(-Infinity); if (x - w / 2 >= ends[k]) { ends[k] = x + w / 2; out[i] = k; break; } } }
  return out;
}
function ovLabel(m) { let n = m.name; if (m.kind === "gate" && m.code && n.toUpperCase().startsWith(m.code)) n = n.slice(m.code.length).replace(/^[\s\-–—:]+/, ""); return [n.length > 40 ? n.slice(0, 38) + "…" : n, fmt(m.date)]; }
const OV = () => P.overview[st.ovl] || P.overview["1"];
function overview() {
  const O = OV(), box = $("ovc"); if (!O || !box.offsetParent) return;
  $("ovbar").innerHTML = `<b>${esc(O.name)}</b><span class="kpi">${fmt(O.start)} – ${fmt(O.finish)} · ${esc(O.duration)}</span>` +
    (P.overview["2"] ? `<span style="margin-left:auto"></span><button data-l="1" class="${st.ovl === "1" ? "on" : ""}">Stages</button><button data-l="2" class="${st.ovl === "2" ? "on" : ""}">One level down</button>` : "") +
    `<span class="kpi" style="${P.overview["2"] ? "" : "margin-left:auto"}">Hover for detail · click to open in the Gantt</span>`;
  const W = Math.max(640, box.clientWidth), LW = Math.min(300, Math.max(150, W * 0.22)), x0 = LW + 14, x1 = W - 12, TW = x1 - x0;
  const a = D(O.start).getTime(), b = D(O.finish).getTime() + DAY;
  const X = (iso, end) => x0 + (D(iso).getTime() + (end ? DAY : 0) - a) / (b - a) * TW;
  const LH = 30, BAR = 34, GL = 30;
  const lw = m => { const [n, d] = ovLabel(m); return Math.max(tw(n), tw(d)) + 10; };
  const clampX = (x, w) => Math.min(Math.max(x, x0 + w / 2), x1 - w / 2);
  const gx = O.gates.map(g => clampX(X(g.date, true), lw(g)));
  const glev = lanesJS(O.gates.map((g, i) => [gx[i], lw(g) + 6]), 6);
  const brow = lanesJS(O.gates.map(g => [X(g.date, true), 36]), 2).map(v => v || 0);
  const top = O.milestones.some(m => m.row === null);
  const keys = (top ? [null] : []).concat(O.workstreams.map((_, i) => i));
  const mx = O.milestones.map(m => clampX(X(m.date, true), lw(m))), mlane = {};
  const heights = keys.map(key => {
    const mem = O.milestones.map((_, i) => i).filter(i => O.milestones[i].row === key);
    const got = lanesJS(mem.map(i => [mx[i], lw(O.milestones[i]) + 6]), 6); mem.forEach((i, j) => mlane[i] = got[j]);
    const used = got.filter(v => v !== null);
    return {above: Math.max(0, ...used.filter(v => v % 2 === 0).map(v => v / 2 + 1)), below: Math.max(0, ...used.filter(v => v % 2 === 1).map(v => (v - 1) / 2 + 1))};
  });
  const bandLv = Math.max(-1, ...glev.filter(v => v !== null)) + 1, brows = Math.max(0, ...brow) + 1;
  const bandY = 30 + bandLv * GL + 8;
  let y = bandY + 24 + (brows - 1) * 22;
  const rowY = heights.map(h => { y += h.above * LH; const c = y + BAR / 2; y += BAR + h.below * LH; return c; });
  const H = y + 10;
  let s = `<svg width="${W}" height="${H}" xmlns="http://www.w3.org/2000/svg">`;
  O.ticks.forEach((t, i) => { const x = X(t.date);
    if (x < x0 && i === 0) s += `<text x="${x0 + 3}" y="14" class="lbl">${esc(t.label)}</text>`;
    else if (x >= x0 && x <= x1) s += `<line x1="${x}" x2="${x}" y1="20" y2="${H}" stroke="var(--line)"/><text x="${x + 3}" y="14" class="lbl">${esc(t.label)}</text>`; });
  for (const [val, colr] of [[P.today, "var(--warn)"], [P.status, "var(--ok)"]]) if (val) { const x = X(val); if (x >= x0 && x <= x1) s += `<line x1="${x}" x2="${x}" y1="${bandY - 10}" y2="${H}" stroke="${colr}" stroke-dasharray="4 3"/>`; }
  if (O.gates.length) s += `<text x="${x0 - 22}" y="${bandY + 4}" text-anchor="end" class="gatehead">Gates</text>`;
  O.gates.forEach((g, i) => { const x = X(g.date, true), by = bandY + brow[i] * 22, col = g.late ? "var(--crit)" : "var(--gate)";
    s += `<line x1="${x}" x2="${x}" y1="${by}" y2="${H}" stroke="${col}" stroke-dasharray="5 4" opacity=".75"/>`;
    s += `<g class="ovh" data-ov="g${i}"><rect x="${x - 16}" y="${by - 9}" width="32" height="18" rx="4" fill="${col}"/><text x="${x}" y="${by + 4}" text-anchor="middle" class="badge">${esc(g.code || "G")}</text></g>`;
    if (glev[i] !== null) { const [n, d] = ovLabel(g), ly = bandY - 16 - glev[i] * GL;
      s += `<text x="${gx[i]}" y="${ly - 12}" text-anchor="middle" class="ovh" data-ov="g${i}">${esc(n)}</text><text x="${gx[i]}" y="${ly}" text-anchor="middle" class="lbl">${esc(d)}</text>`; } });
  const off = top ? 1 : 0;
  if (top) s += `<text x="${LW}" y="${rowY[0] + 4}" text-anchor="end" style="font-weight:600">Key milestones</text>`;
  O.workstreams.forEach((r, i) => { const yy = rowY[i + off], xa = X(r.start), xb = X(r.finish, true), h = r.sub ? 12 : 18, col = r.critical ? "var(--crit)" : "var(--bar)";
    const nm = r.name.length > 46 ? r.name.slice(0, 44) + "…" : r.name;
    s += `<g class="ovh ovrow" data-ov="w${i}"><rect x="0" y="${yy - BAR / 2}" width="${W}" height="${BAR}" fill="transparent"/>` +
      `<text x="${LW}" y="${yy + 4}" text-anchor="end" style="${r.sub ? "fill:var(--muted)" : "font-weight:600"}">${esc(nm)}${r.url ? " ↗" : ""}</text>` +
      `<rect x="${xa}" y="${yy - h / 2}" width="${Math.max(3, xb - xa)}" height="${h}" rx="4" fill="${col}" opacity="${r.sub ? .6 : .9}"/></g>`;
    const marked = O.milestones.some(m => m.row === i), inside = !marked && (xb - xa) > tw(r.span) + 14;
    s += `<text x="${inside ? xa + 6 : xb + 6}" y="${yy + 4}" class="${inside ? "inbar" : "lbl"}" pointer-events="none">${esc(r.span)}</text>`; });
  O.milestones.forEach((m, i) => { const yy = rowY[m.row === null ? 0 : m.row + off], x = X(m.date, true);
    s += m.kind === "gateway" ? `<rect class="ovh" data-ov="m${i}" x="${x - 6}" y="${yy - 6}" width="12" height="12" fill="var(--panel)" stroke="var(--gate)" stroke-width="2"/>`
      : `<path class="ovh" data-ov="m${i}" d="M${x} ${yy - 8} l8 8 -8 8 -8 -8z" fill="${m.late ? "var(--crit)" : "var(--ink)"}" stroke="var(--panel)"/>`;
    const ln = mlane[i]; if (ln === null || ln === undefined) return;
    const depth = Math.floor(ln / 2), [n, d] = ovLabel(m), above = ln % 2 === 0;
    const nameY = above ? yy - 30 - depth * LH : yy + 22 + depth * LH;
    s += `<text x="${mx[i]}" y="${nameY}" text-anchor="middle" class="ovh" data-ov="m${i}">${esc(n)}</text><text x="${mx[i]}" y="${nameY + 12}" text-anchor="middle" class="lbl">${esc(d)}</text>`; });
  box.innerHTML = s + "</svg>";
}
function ovItem(key) { const O = OV(), n = +key.slice(1); return [key[0], key[0] === "g" ? O.gates[n] : key[0] === "m" ? O.milestones[n] : O.workstreams[n]]; }
function tipHtml(kind, it) {
  if (kind === "w") return `<b>${esc(it.name)}</b><br>${fmt(it.start)} – ${fmt(it.finish)} · ${esc(it.span)}<br>${it.tasks} task${it.tasks === 1 ? "" : "s"}${it.critical ? ` · <b style="color:var(--crit)">on the critical path</b>` : ""}` +
    `<br><span class="kpi">Click to open this stage in the Gantt${it.url ? "; Ctrl or ⌘-click to open its own plan" : ""}</span>`;
  const t = P.tasks.find(x => x.id === it.id) || {};
  const ev = kind === "g" && P.deliverables && P.deliverables.gates[it.code];
  const what = (kind === "g" ? `Gate ${esc(it.code)}: closes ${esc(it.workstream || "its stage")}` : it.kind === "gateway" ? "Mid-stage gateway" : "Milestone") +
    (ev ? `<br>Evidence: <b>${ev.approved} of ${ev.total}</b> deliverables approved${ev.late ? ` · <b style="color:var(--crit)">${ev.late} late</b>` : ""}${ev.at_risk ? ` · ${ev.at_risk} at risk` : ""}` : "");
  return `<b>${kind === "g" ? `<span class="gb">${esc(it.code)}</span> ` : ""}${esc(it.name)}</b><br>${fmt(it.date)} · ${what}` +
    (kind !== "g" && it.workstream ? `<br>${esc(it.workstream)}` : "") + `<br>Float ${it.float}d${it.critical ? ` · <b style="color:var(--crit)">critical</b>` : ""}` +
    (it.deadline ? `<br>Deadline ${fmt(it.deadline)}${it.late ? ` · <b style="color:var(--crit)">late</b>` : " · on time"}` : "") +
    (t.owners ? `<br>Owner: ${esc(t.owners)}` : "") + `<br><span class="kpi">Click to open it in the Gantt</span>`;
}
function showTab(id) {
  document.querySelectorAll("nav [data-tab]").forEach(x => x.classList.toggle("on", x.dataset.tab === id));
  document.querySelectorAll(".tab").forEach(x => x.classList.toggle("on", x.id === id));
  if (id === "o") overview();
}
function under(id, anc) { let p = PARENT[id]; while (p) { if (p === anc) return true; p = PARENT[p]; } return false; }
function drill(id, stage) {
  const t = P.tasks.find(x => x.id === id); if (!t) return;
  const chain = new Set(); let p = t.parent; while (p) { chain.add(p); p = PARENT[p]; }
  // everything else folded to its stage; the stage asked for opened fully
  st.closed = new Set(P.tasks.filter(x => x.summary && !chain.has(x.id) && x.id !== id && !(stage && under(x.id, id))).map(x => x.id));
  showTab("g"); gantt();
  const row = $("tbl").querySelector(`tr[data-id="${CSS.escape(id)}"]`);
  if (row) { $("tbl").scrollTop = row.offsetTop - 60; row.classList.add("flash"); setTimeout(() => row.classList.remove("flash"), 1800); }
  const x0g = D(P.start).getTime() - 7 * DAY; $("tl").scrollLeft = Math.max(0, (D(t[k()].start).getTime() - x0g) / DAY * st.px - 60);
  if (!stage) detail(id);
}
$("ovc").addEventListener("mousemove", e => {
  const el = e.target.closest("[data-ov]"), tip = $("tip"); if (!el) { tip.style.display = "none"; return; }
  const [kind, it] = ovItem(el.dataset.ov); tip.innerHTML = tipHtml(kind, it); tip.style.display = "block";
  const r = tip.getBoundingClientRect(); let x = e.clientX + 14, y2 = e.clientY + 14;
  if (x + r.width > innerWidth - 8) x = e.clientX - r.width - 14; if (y2 + r.height > innerHeight - 8) y2 = e.clientY - r.height - 14;
  tip.style.left = x + "px"; tip.style.top = y2 + "px";
});
$("ovc").addEventListener("mouseleave", () => { $("tip").style.display = "none"; });
$("ovc").addEventListener("click", e => {
  const el = e.target.closest("[data-ov]"); if (!el) return; $("tip").style.display = "none";
  const [kind, it] = ovItem(el.dataset.ov);
  if (kind === "w" && it.url && (e.ctrlKey || e.metaKey)) { window.open(it.url, "_blank", "noopener"); return; }
  drill(it.id, kind === "w");
});
$("ovbar").addEventListener("click", e => { const b = e.target.closest("[data-l]"); if (b) { st.ovl = b.dataset.l; overview(); } });
addEventListener("resize", () => { clearTimeout(overview.t); overview.t = setTimeout(overview, 120); });
// large plans open folded to their stages in the Gantt
if (P.tasks.length > 80) P.tasks.filter(t => t.summary && t.level === 1).forEach(t => st.closed.add(t.id));
// ---- Deliverables by phase (deliverables.py): method deliverables per stage, with documents and status from the register ----
st.df = {status: "all", q: ""};
function chip(r) {
  if (r.late) return `<span class="chip c-late" title="${esc(r.status)}">Late · ${esc(r.status)}</span>`;
  if (r.at_risk) return `<span class="chip c-risk" title="${esc(r.status)}">At risk · ${esc(r.status)}</span>`;
  const cls = r.done ? "c-done" : /draft|review|check|approval/i.test(r.status) ? "c-rev" : "c-none";
  return `<span class="chip ${cls}">${esc(r.status)}</span>`;
}
function deliverablesTab() {
  const D = P.deliverables; if (!D) return; $("dtabbtn").style.display = "";
  const reg = D.register || {}, f = st.df, q = f.q.toLowerCase();
  const keep = r => (f.status === "all" || (f.status === "done" && r.done) || (f.status === "open" && !r.done) ||
    (f.status === "late" && (r.late || r.at_risk)) || (f.status === "nonumber" && !r.documents.length)) &&
    (!q || (r.id + " " + r.name + " " + r.owners + " " + r.documents.map(d => d.number + " " + d.title).join(" ")).toLowerCase().includes(q));
  let h = `<div class="card"><div class="dctl"><b>Deliverables by stage</b>
    <span class="kpi">${reg.file ? `Register: <a href="${esc(reg.url)}" target="_blank" rel="noopener">${esc(reg.file)}</a> (read ${new Date(reg.read_at * 1000).toLocaleString()})` : reg.error ? `<span style="color:var(--warn)">${esc(reg.error)}</span>` : "No register linked: status comes from the plan only"}
    · ${esc((D.method || {}).cite_as || "")}</span><span style="margin-left:auto"></span>
    <select id="dfs"><option value="all">All</option><option value="open">Not approved</option><option value="late">Late or at risk</option><option value="nonumber">No document yet</option><option value="done">Approved</option></select>
    <input id="dfq" placeholder="Filter" value="${esc(f.q)}"></div>`;
  for (const g of D.stages) {
    const rows = g.rows.filter(keep); if (!rows.length && f.status !== "all") continue;
    h += `<div class="dstage"><h3>${esc(g.name)}</h3>${g.gate ? `<span class="gb">${esc(g.gate)}</span><span class="kpi">${esc(g.gate_name || "")}${g.gate_date ? " · " + fmt(g.gate_date) : ""}</span>` : ""}
      <span class="kpi"><b>${g.approved}</b> of ${g.total} approved · ${g.numbered} with a document · ${g.planned} planned</span></div>
      <table class="dtab"><tr><th>ID</th><th>Deliverable</th><th>Status</th><th>Documents</th><th>Due</th><th>Owner</th><th>Tasks</th><th>Producer → approver</th></tr>` +
      rows.map(r => `<tr><td class="id">${r.url ? `<a href="${esc(r.url)}" target="_blank" rel="noopener" title="In the {org} method">${esc(r.id)}</a>` : esc(r.id)}</td>
        <td>${esc(r.name)}</td><td>${chip(r)}</td>
        <td>${r.documents.map(d => `${d.url ? `<a href="${esc(d.url)}" target="_blank" rel="noopener" title="${esc(d.title)}">${esc(d.number)}</a>` : esc(d.number)}${d.rev ? ` <span class="kpi">Rev ${esc(d.rev)}</span>` : ""}${d.status ? ` <span class="kpi">${esc(d.status)}</span>` : ""}`).join("<br>") || `<span class="kpi">none yet</span>`}</td>
        <td>${r.due ? fmt(r.due) : ""}</td><td>${esc(r.owners)}</td>
        <td>${r.tasks.map(t => `<a href="#" data-drill="${esc(t.id)}">${esc(t.name)}</a>`).join("<br>") || `<span class="kpi">not planned</span>`}</td>
        <td class="kpi">${esc(r.producer)} → ${esc(r.approver)}</td></tr>`).join("") + `</table>`;
  }
  $("d").innerHTML = h + `</div>`;
  $("dfs").value = f.status;
  $("dfs").onchange = e => { st.df.status = e.target.value; deliverablesTab(); };
  $("dfq").oninput = e => { st.df.q = e.target.value; clearTimeout(deliverablesTab.t); deliverablesTab.t = setTimeout(() => { deliverablesTab(); const q = $("dfq"); q.focus(); q.setSelectionRange(q.value.length, q.value.length); }, 200); };
}
$("d").addEventListener("click", e => { const a = e.target.closest("[data-drill]"); if (a) { e.preventDefault(); drill(a.dataset.drill, false); } });
function all() { kpis(); gantt(); milestones(); resources(); health(); deliverablesTab(); overview(); }
document.querySelectorAll("nav [data-tab]").forEach(b => b.onclick = () => showTab(b.dataset.tab));
$("fold").onclick = () => { st.closed = new Set(P.tasks.filter(t => t.summary).map(t => t.id)); P.tasks.filter(t => t.summary && t.level > 1).forEach(t => st.closed.add(t.id)); gantt(); };
$("unfold").onclick = () => { st.closed = new Set(); gantt(); };
$("lev").onclick = () => { st.lev = !st.lev; $("lev").classList.toggle("on", st.lev); $("lev").textContent = st.lev ? "Levelled" : "Unlevelled"; all(); };
$("crit").onclick = () => { st.crit = !st.crit; $("crit").classList.toggle("on", st.crit); gantt(); };
$("deps").onclick = () => { st.deps = !st.deps; $("deps").classList.toggle("on", st.deps); gantt(); };
$("zoom").onchange = e => { st.px = +e.target.value; gantt.scrolled = false; gantt(); };
$("theme").onclick = () => { const r = document.documentElement, dark = r.dataset.theme ? r.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches; r.dataset.theme = dark ? "light" : "dark"; };
document.addEventListener("click", e => {
  const c = e.target.closest("[data-c]"); if (c) { const id = c.dataset.c; st.closed.has(id) ? st.closed.delete(id) : st.closed.add(id); gantt(); return; }
  const r = e.target.closest("[data-id]"); if (r) detail(r.dataset.id);
});
$("tbl").addEventListener("scroll", () => { $("tl").scrollTop = $("tbl").scrollTop; });
$("tl").addEventListener("scroll", () => { $("tbl").scrollTop = $("tl").scrollTop; });
all();
</script></body></html>
"""
PAGE = localise(PAGE)


def html_viewer(s: Schedule, download: str | None = None, links: list[tuple[str, str]] | None = None,
                deliverables: dict | None = None) -> bytes:
    """The viewer page; `download` (a relative link to the MS Project XML) adds a download link to the header, and
    `links` [(label, href)] more (the overview for slides)."""
    pd = plan_data(s)
    if deliverables:  # deliverables by phase (deliverables.py): a Deliverables tab and their findings in Health
        pd["deliverables"] = deliverables
        pd["health"] = pd["health"] + deliverables.get("findings", [])
    data = json.dumps(pd).replace("</", "<\\/")  # never close the script tag from data
    link = (f'<a class="dl" href="{html.escape(download)}" download>Download MS Project XML</a>' if download else "")
    link += "".join(f' <a class="dl" href="{html.escape(h)}" target="_blank" rel="noopener">{html.escape(t)}</a>'
                    for t, h in links or [])
    return (PAGE.replace("__TITLE__", html.escape(s.plan.name)).replace("__DOWNLOAD__", link)
            .replace("__DATA__", data).encode("utf-8"))


def _mm(text: str) -> str:
    """Mermaid task and section text can't contain : # ; or line breaks."""
    return re.sub(r"\s+", " ", re.sub(r"[:#;]", " -", text or "")).strip() or "task"


def mermaid(s: Schedule, levelled: bool = True) -> str:
    plan = s.plan
    lines = ["gantt", f"    title {_mm(plan.name)}", "    dateFormat YYYY-MM-DD", "    axisFormat %d %b",
             "    excludes weekends"]
    holidays = s.calendar.non_working_between(s.calendar.start, s.finish_date_of(max(s.finish, s.lev_finish)))
    if holidays:
        lines.append("    excludes " + ", ".join(sorted(d.isoformat() for d in holidays)))
    current = None
    for tid in plan.order:
        t = plan.tasks[tid]
        top = tid
        while plan.tasks[top].parent:
            top = plan.tasks[top].parent
        if t.summary:
            if plan.outline(tid) == 1:
                lines.append(f"    section {_mm(t.name)}")
                current = top
            continue
        if current is None or (current != top and not plan.tasks[top].summary):
            lines.append("    section Project")
            current = top
        tags = ["crit"] if t.critical else []
        start = s.start_date(t, levelled)
        if t.milestone:
            tags.append("milestone")
            ident = "m_" + re.sub(r"[^A-Za-z0-9_]", "_", tid)
            lines.append(f"    {_mm(t.name)} :{', '.join(tags + [ident])}, {start.isoformat()}, 0d")
            continue
        if t.percent_complete >= 100:
            tags.append("done")
        elif t.percent_complete > 0:
            tags.append("active")
        end = s.finish_date(t, levelled) + timedelta(days=1)  # Mermaid end dates are exclusive
        ident = re.sub(r"[^A-Za-z0-9_]", "_", tid)
        lines.append(f"    {_mm(t.name)} :{', '.join(tags + [ident])}, {start.isoformat()}, {end.isoformat()}")
    return "\n".join(lines) + "\n"


def mermaid_page(s: Schedule, levelled: bool = True) -> bytes:
    body = html.escape(mermaid(s, levelled))
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(s.plan.name)}</title><style>body{{font-family:system-ui,sans-serif;margin:16px;background:#fff}}</style></head>
<body><h1 style="font-size:18px">{html.escape(s.plan.name)}</h1><pre class="mermaid">{body}</pre>
<script type="module">import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";
mermaid.initialize({{startOnLoad: true, gantt: {{barHeight: 18, fontSize: 12, sectionFontSize: 13}}}});</script></body></html>""".encode()
