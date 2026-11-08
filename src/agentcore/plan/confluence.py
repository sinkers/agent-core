"""A plan as a native Confluence page (Atlassian Document Format), laid out like Atlassian's project plan template.

Sections: a status panel (delivery date, status, budget, links to the web viewer and MS Project XML), overview,
goals and success measures, scope (in / not in), timeline (a Gantt drawn as a table with shaded cells, so it needs
no macro or app), milestones (status lozenges), work breakdown (owners, dates, dependencies, Jira smart links),
team and resources (availability, leave, peak load), decisions (DACI), risks and dependencies (the plan's own risks,
external dates, schedule health), and communications.

The narrative parts come from optional spec fields the scheduler ignores:

  summary, budget, approach, goals: [..], success_measures: [..], scope_in: [..], scope_out: [..],
  assumptions: [..], stakeholders: [{name, role: Driver|Approver|Contributor|Informed, notes}],
  risks: [{risk, likelihood, impact, owner, mitigation}], communications: [{what, audience, when, how}]

Everything scheduled (dates, critical path, load) comes from the same Schedule as the XML and the viewer.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

from .health import check
from .schedule import Schedule
from ..org import agent, org

# cell shading (Confluence's own table palette)
BAR, CRIT, SUMMARY, DONE, MILESTONE, HOLIDAY = "#deebff", "#ffbdad", "#c1c7d0", "#abf5d1", "#fff0b3", "#f4f5f7"
MAX_COLUMNS = 16
MAX_ROWS = 60


# ── ADF building blocks ─────────────────────────────────────────────────────────

def text(value, *, bold=False, link=None, italic=False) -> dict:
    marks = ([{"type": "strong"}] if bold else []) + ([{"type": "em"}] if italic else []) + \
            ([{"type": "link", "attrs": {"href": link}}] if link else [])
    node = {"type": "text", "text": str(value) if value not in (None, "") else "—"}
    if marks:
        node["marks"] = marks
    return node


def para(*parts) -> dict:
    content = [p if isinstance(p, dict) else text(p) for p in parts if p is not None]
    return {"type": "paragraph", "content": content} if content else {"type": "paragraph"}


def heading(value: str, level: int = 2) -> dict:
    return {"type": "heading", "attrs": {"level": level}, "content": [text(value)]}


def bullets(items) -> dict:
    return {"type": "bulletList", "content": [{"type": "listItem", "content": [para(i)]} for i in items]}


def status(label: str, color: str) -> dict:
    """An inline lozenge: neutral | purple | blue | red | yellow | green."""
    return {"type": "status", "attrs": {"text": label, "color": color, "localId": uuid.uuid4().hex}}


def adf_date(d: date) -> dict:
    ts = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)
    return {"type": "date", "attrs": {"timestamp": str(ts)}}


def smart_link(url: str) -> dict:
    return {"type": "inlineCard", "attrs": {"url": url}}


def cell(*parts, header=False, background=None) -> dict:
    content = [para() if q in ("", None) else q if isinstance(q, dict) and q.get("type") in ("paragraph", "bulletList")
               else para(q) for q in parts] or [para()]
    node = {"type": "tableHeader" if header else "tableCell", "content": content}
    if background:
        node["attrs"] = {"background": background}
    return node


def table(header: list, rows: list[list], *, full_width=False) -> dict:
    out = [{"type": "tableRow", "content": [cell(h, header=True) for h in header]}]
    for r in rows:
        out.append({"type": "tableRow", "content": [c if isinstance(c, dict) and c.get("type") in
                                                    ("tableCell", "tableHeader") else cell(c) for c in r]})
    return {"type": "table", "attrs": {"isNumberColumnEnabled": False, "layout": "full-width" if full_width else "default"},
            "content": out}


def panel(kind: str, *content) -> dict:
    return {"type": "panel", "attrs": {"panelType": kind}, "content": list(content)}


def expand(title: str, *content) -> dict:
    return {"type": "expand", "attrs": {"title": title}, "content": list(content)}


# ── the page ────────────────────────────────────────────────────────────────────

def plan_status(s: Schedule, findings: list[dict]) -> tuple[str, str]:
    """Off track: a deadline is missed or the schedule is impossible. At risk: levelling pushes the finish out or a
    deadline has no float left. Plan-quality warnings (open ends, no owner) show in the health panel instead."""
    if any(f["severity"] == "error" for f in findings):
        return "OFF TRACK", "red"
    tight = any(t.deadline and (s.calendar.index_of(t.deadline) + 1 - t.lev_ef) <= 0 for t in s.plan.leaves()) or \
        any(t.deadline and t.milestone and s.start_date(t) >= t.deadline for t in s.plan.tasks.values())
    if tight or any(f["check"] == "levelling delay" for f in findings):
        return "AT RISK", "yellow"
    return "ON TRACK", "green"


def _periods(first: date, last: date) -> tuple[list[date], int]:
    """Column start dates (Mondays) and the column width in days: weekly, fortnightly or monthly-ish."""
    monday = first - timedelta(days=first.weekday())
    weeks = (last - monday).days // 7 + 1
    for step in (7, 14, 28, 56):
        if weeks * 7 / step <= MAX_COLUMNS:
            break
    cols, d = [], monday
    while d <= last:
        cols.append(d)
        d += timedelta(days=step)
    return cols, step


def timeline(s: Schedule) -> list[dict]:
    plan = s.plan
    wbs = plan.wbs()
    first = s.calendar.start
    last = s.finish_date_of(s.lev_finish)
    cols, step = _periods(first, last)
    status_day = plan.status_date or date.today()
    leaves = len(plan.order) <= MAX_ROWS
    header = ["Task"] + [("▼ " if c <= status_day < c + timedelta(days=step) else "") + c.strftime("%d %b")
                         for c in cols]
    rows = []
    for tid in plan.order:
        t = plan.tasks[tid]
        if not leaves and not (t.summary or t.milestone):
            continue
        start, finish = s.start_date(t), s.finish_date(t)
        if t.summary:
            colour = SUMMARY
        elif t.milestone:
            colour = MILESTONE
        elif t.percent_complete >= 100:
            colour = DONE
        else:
            colour = CRIT if t.critical else BAR
        label = (" " * (plan.outline(tid) - 1)) + f"{wbs[tid]} {t.name}"
        row = [cell(text(label, bold=t.summary))]
        for c in cols:
            end = c + timedelta(days=step - 1)
            on = start <= end and finish >= c
            if t.milestone:
                row.append(cell("◆" if on else "", background=colour if on else None))
            else:
                row.append(cell("", background=colour if on else None))
        rows.append(row)
    unit = {7: "week", 14: "fortnight", 28: "4 weeks", 56: "8 weeks"}[step]
    key = para(text("Each column is a " + unit + ". "), text("Blue", bold=True), " planned, ",
               text("red", bold=True), " critical path, ", text("green", bold=True), " complete, ",
               text("◆", bold=True), " milestone, ", text("▼", bold=True), " status date."
               + ("" if leaves else " Stages and milestones only; the web viewer has every task."))
    return [table(header, rows, full_width=True), key]


def milestones(s: Schedule) -> dict | None:
    plan = s.plan
    rows = []
    for tid in plan.order:
        t = plan.tasks[tid]
        if not t.milestone:
            continue
        when = s.start_date(t)
        if t.percent_complete >= 100:
            lozenge = status("DONE", "green")
        elif t.deadline and when > t.deadline:
            lozenge = status("LATE", "red")
        elif t.float_days <= 0:
            lozenge = status("CRITICAL", "yellow")
        else:
            lozenge = status("ON TRACK", "blue")
        rows.append([cell(text(t.name, bold=True)), cell(adf_date(when)),
                     cell(adf_date(t.deadline)) if t.deadline else cell(""),
                     cell(f"{t.float_days} d"), cell(lozenge)])
    return table(["Milestone", "Forecast", "Deadline", "Float", "Status"], rows) if rows else None


def work_breakdown(s: Schedule) -> dict:
    plan = s.plan
    wbs = plan.wbs()
    rows = []
    for tid in plan.order:
        t = plan.tasks[tid]
        preds = ", ".join(wbs[link.pred] + (link.type if link.type != "FS" else "")
                          + (f"{link.lag:+d}d" if link.lag else "") for link in t.links)
        owners = ", ".join(plan.resources[a.resource].name + (f" {a.units:.0%}" if a.units != 1 else "")
                           for a in t.assignments)
        jira = cell(smart_link(plan.jira_base + t.jira)) if t.jira else cell("")
        done = (status("DONE", "green") if t.percent_complete >= 100 else
                status(f"{t.percent_complete}%", "blue") if t.percent_complete else None)
        rows.append([cell(wbs[tid]), cell(text((" " * (plan.outline(tid) - 1)) + t.name, bold=t.summary)),
                     cell(owners), cell(adf_date(s.start_date(t))), cell(adf_date(s.finish_date(t))),
                     cell("" if t.summary else f"{t.lev_ef - t.lev_es} d"), cell(preds),
                     cell("" if t.summary else ("critical" if t.critical else f"{t.float_days} d")),
                     jira, cell(done) if done else cell("")])
    return table(["WBS", "Task", "Owner", "Start", "Finish", "Days", "Depends on", "Float", "Jira", "Progress"],
                 rows, full_width=True)


def resources(s: Schedule) -> dict | None:
    plan = s.plan
    rows = []
    for r in plan.resources.values():
        if r.kind != "work":
            continue
        tasks = [t for t in plan.leaves() if any(a.resource == r.id for a in t.assignments)]
        days = s.lev_load.get(r.id, {})
        peak = max(days.values()) / r.max_units if days and r.max_units else 0
        leave = ", ".join(f"{a:%d %b}–{b:%d %b}" if a != b else f"{a:%d %b}" for a, b in r.leave if a and b)
        rows.append([cell(text(r.name, bold=True)), cell(r.role or ""), cell(f"{r.max_units:.0%}"),
                     cell(str(len(tasks))), cell(status(f"{peak:.0%}", "red" if peak > 1.0001 else "green")),
                     cell(leave)])
    return table(["Person", "Role", "Availability", "Tasks", "Peak load (levelled)", "Leave"], rows) if rows else None


def confluence_page(s: Schedule, spec: dict, viewer_url: str | None = None) -> dict:
    """The ADF document for the plan."""
    plan = s.plan
    findings = check(s)
    label, colour = plan_status(s, findings)
    finish = s.finish_date_of(s.lev_finish)
    facts = [text("Delivery: ", bold=True), adf_date(finish), text("   Start: ", bold=True), adf_date(s.calendar.start),
             text("   Status: ", bold=True), status(label, colour)]
    if spec.get("budget"):
        facts += [text("   Budget: ", bold=True), text(spec["budget"])]
    links = []
    if viewer_url:
        links = [para(text("Interactive Gantt", bold=True, link=viewer_url),
                      f" (levelled view, dependencies, resource load; {org().name} sign-in) · ",
                      text("MS Project XML", link=viewer_url + "plan.xml"), " (the master file)")]
    content: list[dict] = [panel("info", para(*facts), *links)]
    if spec.get("summary"):
        content += [heading("Overview"), para(spec["summary"])]
    if spec.get("approach"):
        content += [para(text("Approach: ", bold=True), spec["approach"])]
    if spec.get("goals") or spec.get("success_measures"):
        content.append(heading("Goals and success measures"))
        if spec.get("goals"):
            content.append(bullets(spec["goals"]))
        if spec.get("success_measures"):
            content += [para(text("We'll know it worked when:", bold=True)), bullets(spec["success_measures"])]
    if spec.get("scope_in") or spec.get("scope_out"):
        content += [heading("Scope"), table(["In scope", "Not in scope"], [[
            cell(bullets(spec.get("scope_in") or ["—"])), cell(bullets(spec.get("scope_out") or ["—"]))]])]
    content += [heading("Timeline"), *timeline(s)]
    ms = milestones(s)
    if ms:
        content += [heading("Milestones"), ms]
    content += [heading("Work breakdown"), expand(f"All {len(plan.leaves())} tasks", work_breakdown(s))]
    res = resources(s)
    if res:
        content += [heading("Team and resources"), res]
        if s.lev_finish > s.finish:
            content.append(para(text("Levelling adds ", italic=True),
                                text(f"{s.lev_finish - s.finish} working days", bold=True),
                                text(" so no one is over-booked; the dates above are levelled.", italic=True)))
    if spec.get("stakeholders"):
        content += [heading("Decisions and stakeholders (DACI)"), table(["Who", "Role", "Notes"], [
            [cell(text(p.get("name"), bold=True)), cell(p.get("role") or ""), cell(p.get("notes") or "")]
            for p in spec["stakeholders"]])]
    content.append(heading("Risks and dependencies"))
    if spec.get("risks"):
        content.append(table(["Risk", "Likelihood", "Impact", "Owner", "Mitigation"], [
            [cell(r.get("risk")), cell(r.get("likelihood") or ""), cell(r.get("impact") or ""),
             cell(r.get("owner") or ""), cell(r.get("mitigation") or "")] for r in spec["risks"]]))
    external = [t for t in (plan.tasks[i] for i in plan.order) if t.start_no_earlier]
    if external:
        content += [para(text("External dates the plan waits for:", bold=True)), bullets(
            [f"{t.name}: not before {t.start_no_earlier:%d %b %Y}" for t in external])]
    if spec.get("assumptions"):
        content += [para(text("Assumptions:", bold=True)), bullets(spec["assumptions"])]
    serious = [f for f in findings if f["severity"] in ("error", "warning")]
    if serious:
        kind = "error" if any(f["severity"] == "error" for f in serious) else "warning"
        content.append(panel(kind, para(text("Schedule health", bold=True)),
                             bullets([f["message"] for f in serious[:12]])))
    else:
        content.append(panel("success", para(text("Schedule health: ", bold=True), "no problems found.")))
    if spec.get("communications"):
        content += [heading("Communications"), table(["What", "Audience", "When", "How"], [
            [cell(c.get("what")), cell(c.get("audience") or ""), cell(c.get("when") or ""), cell(c.get("how") or "")]
            for c in spec["communications"]])]
    content.append(para(text(f"Generated by {agent().display_name} from the scheduled plan on {date.today():%d %b %Y}. "
                             f"Change the plan through {agent().short_name} so this page, the web viewer and the MS Project XML "
                             "stay the same.", italic=True)))
    return {"type": "doc", "version": 1, "content": content}
