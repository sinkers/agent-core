"""MS Project XML (MSPDI, the "Save as XML" format): export a scheduled plan, import an existing one.

Opens in MS Project (2007 and later), ProjectLibre and Primavera's XML import. Durations are hours at 8 h/day
(PT24H0M0S = 3 days); link lags and levelling delays are in tenths of a minute (4800 = 1 day). The export carries:
the working calendar with its public holidays, a calendar per person with their leave, tasks in outline order
with WBS codes, summaries, milestones, links (type and lag), SNET constraints, deadlines, % complete, baselines,
the levelling delay (so MS Project shows the levelled dates), resources with max units, and assignments with work.
Jira keys go into each task's hyperlink.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date, timedelta

from .calendar import HOURS_PER_DAY
from .schedule import Schedule
from ..org import org, place

NS = "http://schemas.microsoft.com/project"
LINK_CODE = {"FF": 0, "FS": 1, "SF": 2, "SS": 3}
CODE_LINK = {v: k for k, v in LINK_CODE.items()}
TENTHS_PER_DAY = HOURS_PER_DAY * 60 * 10


def _el(parent, tag, text=None):
    e = ET.SubElement(parent, tag)
    if text is not None:
        e.text = str(text)
    return e


def _hours(days: float) -> str:
    h = days * HOURS_PER_DAY
    return f"PT{int(h)}H{int(round((h - int(h)) * 60))}M0S"


def _dt(d: date, end: bool = False) -> str:
    return f"{d.isoformat()}T{'17:00:00' if end else '08:00:00'}"


def _weekdays(parent, exceptions: dict[date, str] | None, base: bool) -> None:
    wds = _el(parent, "WeekDays")
    if base:
        for day_type in range(1, 8):  # 1 = Sunday ... 7 = Saturday
            wd = _el(wds, "WeekDay")
            _el(wd, "DayType", day_type)
            working = day_type not in (1, 7)
            _el(wd, "DayWorking", int(working))
            if working:
                times = _el(wd, "WorkingTimes")
                for start, finish in (("08:00:00", "12:00:00"), ("13:00:00", "17:00:00")):
                    wt = _el(times, "WorkingTime")
                    _el(wt, "FromTime", start)
                    _el(wt, "ToTime", finish)
    for d, _ in sorted((exceptions or {}).items()):
        wd = _el(wds, "WeekDay")
        _el(wd, "DayType", 0)
        _el(wd, "DayWorking", 0)
        period = _el(wd, "TimePeriod")
        _el(period, "FromDate", f"{d.isoformat()}T00:00:00")
        _el(period, "ToDate", f"{d.isoformat()}T23:59:00")


def export_xml(s: Schedule, levelled: bool = True) -> bytes:
    plan, cal = s.plan, s.calendar
    wbs = plan.wbs()
    finish_idx = s.lev_finish if levelled else s.finish
    project_finish = s.finish_date_of(finish_idx)
    root = ET.Element("Project", xmlns=NS)
    for tag, value in (("SaveVersion", 14), ("Name", plan.name), ("Title", plan.name), ("ScheduleFromStart", 1),
                       ("StartDate", _dt(cal.start)), ("FinishDate", _dt(project_finish, end=True)),
                       ("CalendarUID", 1), ("DefaultStartTime", "08:00:00"), ("DefaultFinishTime", "17:00:00"),
                       ("MinutesPerDay", HOURS_PER_DAY * 60), ("MinutesPerWeek", HOURS_PER_DAY * 60 * 5),
                       ("DaysPerMonth", 20), ("DurationFormat", 7), ("WorkFormat", 2), ("NewTasksAreManual", 0),
                       ("CurrencyCode", "AUD")):
        _el(root, tag, value)
    if plan.status_date:
        _el(root, "StatusDate", _dt(plan.status_date, end=True))
    # calendars: the base one, then one per person for their leave
    cals = _el(root, "Calendars")
    holidays = cal.non_working_between(cal.start - timedelta(days=7), project_finish + timedelta(days=90))
    base = _el(cals, "Calendar")
    for tag, value in (("UID", 1), ("Name", org().calendar_name or f"{org().name} {place()}"), ("IsBaseCalendar", 1),
                       ("BaseCalendarUID", -1)):
        _el(base, tag, value)
    _weekdays(base, holidays, base=True)
    res_uid = {rid: i for i, rid in enumerate(plan.resources, start=1)}
    for rid, uid in res_uid.items():
        r = plan.resources[rid]
        c = _el(cals, "Calendar")
        for tag, value in (("UID", uid + 1), ("Name", r.name), ("IsBaseCalendar", 0), ("BaseCalendarUID", 1)):
            _el(c, tag, value)
        leave = {}
        for first, last in r.leave:
            d = first
            while d and last and d <= last:
                leave[d] = "Leave"
                d += timedelta(days=1)
        _weekdays(c, leave, base=False)
    # tasks
    task_uid = {tid: i for i, tid in enumerate(plan.order, start=1)}
    tasks = _el(root, "Tasks")
    t0 = _el(tasks, "Task")
    for tag, value in (("UID", 0), ("ID", 0), ("Name", plan.name), ("OutlineLevel", 0), ("Summary", 1),
                       ("Start", _dt(cal.start)), ("Finish", _dt(project_finish, end=True))):
        _el(t0, tag, value)
    for n, tid in enumerate(plan.order, start=1):
        t = plan.tasks[tid]
        es, ef = (t.lev_es, t.lev_ef) if levelled else (t.es, t.ef)
        start, finish = s.start_date(t, levelled), s.finish_date(t, levelled)
        days = ef - es
        e = _el(tasks, "Task")
        for tag, value in (("UID", task_uid[tid]), ("ID", n), ("Name", t.name), ("Type", 1), ("IsNull", 0),
                           ("WBS", wbs[tid]), ("OutlineNumber", wbs[tid]), ("OutlineLevel", plan.outline(tid)),
                           ("Priority", t.priority),
                           ("Start", _dt(start, end=s.milestone_at_end_of_day(t, levelled))),
                           ("Finish", _dt(finish, end=not t.milestone or s.milestone_at_end_of_day(t, levelled))),
                           ("Duration", _hours(days)), ("DurationFormat", 7),
                           ("Work", _hours(sum(a.units for a in t.assignments) * days)),
                           ("Milestone", int(t.milestone)), ("Summary", int(t.summary)), ("Critical", int(t.critical)),
                           ("Manual", 0), ("PercentComplete", t.percent_complete),
                           ("TotalSlack", int(t.float_days * TENTHS_PER_DAY))):
            _el(e, tag, value)
        if levelled and not t.summary and t.lev_es > t.es:
            _el(e, "LevelingDelay", int((t.lev_es - t.es) * TENTHS_PER_DAY))
            _el(e, "LevelingDelayFormat", 7)
        if t.start_no_earlier:
            _el(e, "ConstraintType", 4)
            _el(e, "ConstraintDate", _dt(t.start_no_earlier))
        else:
            _el(e, "ConstraintType", 0)
        if t.deadline:
            _el(e, "Deadline", _dt(t.deadline, end=True))
        if t.jira:
            _el(e, "Hyperlink", t.jira)
            _el(e, "HyperlinkAddress", plan.jira_base + t.jira)
        notes = "\n".join(x for x in (t.notes, f"Document: {t.document}" if t.document else None) if x)
        if notes:
            _el(e, "Notes", notes)
        for link in t.links:  # MS Project allows links to summaries, so they go across as written
            pl = _el(e, "PredecessorLink")
            _el(pl, "PredecessorUID", task_uid[link.pred])
            _el(pl, "Type", LINK_CODE[link.type])
            _el(pl, "CrossProject", 0)
            _el(pl, "LinkLag", int(link.lag * TENTHS_PER_DAY))
            _el(pl, "LagFormat", 7)
        if t.baseline_start or t.baseline_finish:
            b = _el(e, "Baseline")
            _el(b, "Number", 0)
            if t.baseline_start:
                _el(b, "Start", _dt(t.baseline_start))
            if t.baseline_finish:
                _el(b, "Finish", _dt(t.baseline_finish, end=True))
    # resources and assignments
    resources = _el(root, "Resources")
    for rid, uid in res_uid.items():
        r = plan.resources[rid]
        e = _el(resources, "Resource")
        initials = "".join(w[0] for w in r.name.split()[:3]).upper()
        for tag, value in (("UID", uid), ("ID", uid), ("Name", r.name), ("Type", 1 if r.kind == "work" else 0),
                           ("IsNull", 0), ("Initials", initials), ("MaxUnits", f"{r.max_units:.2f}"),
                           ("CalendarUID", uid + 1)):
            _el(e, tag, value)
        if r.role:
            _el(e, "Group", r.role)
        if r.email:
            _el(e, "EmailAddress", r.email)
    assignments = _el(root, "Assignments")
    n = 0
    for tid in plan.order:
        t = plan.tasks[tid]
        es, ef = (t.lev_es, t.lev_ef) if levelled else (t.es, t.ef)
        for a in t.assignments:
            n += 1
            e = _el(assignments, "Assignment")
            for tag, value in (("UID", n), ("TaskUID", task_uid[tid]), ("ResourceUID", res_uid[a.resource]),
                               ("Units", f"{a.units:.2f}"), ("Work", _hours(a.units * (ef - es))),
                               ("Start", _dt(s.start_date(t, levelled))),
                               ("Finish", _dt(s.finish_date(t, levelled), end=True))):
                _el(e, tag, value)
    ET.indent(root)
    return b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + ET.tostring(root, encoding="utf-8")


_DUR = re.compile(r"PT(\d+(?:\.\d+)?)H(\d+)M(\d+(?:\.\d+)?)S")


def _days(value: str | None) -> float:
    m = _DUR.match(value or "")
    if not m:
        return 0.0
    return (float(m.group(1)) + int(m.group(2)) / 60) / HOURS_PER_DAY


def import_xml(data: bytes) -> dict:
    """An MSPDI file as a plan spec (model.from_spec), so it can be re-scheduled, levelled and viewed."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError(f"that isn't valid MS Project XML ({exc})") from None
    ns = {"p": NS} if root.tag.startswith("{") else {}
    q = (lambda tag: f"p:{tag}") if ns else (lambda tag: tag)

    def text(el, tag, default=None):
        found = el.find(q(tag), ns)
        return found.text if found is not None and found.text is not None else default
    start = (text(root, "StartDate") or "")[:10]
    if not start:
        raise ValueError("that MS Project XML has no StartDate")
    uid_to_id, stack, tasks = {}, [], []
    for el in root.findall(f"{q('Tasks')}/{q('Task')}", ns):
        level = int(text(el, "OutlineLevel", "1") or 1)
        if level == 0 or text(el, "IsNull") == "1":
            continue
        tid = f"t{text(el, 'UID')}"
        uid_to_id[text(el, "UID")] = tid
        while stack and stack[-1][0] >= level:
            stack.pop()
        task = {"id": tid, "name": text(el, "Name", tid), "parent": stack[-1][1] if stack else None,
                "milestone": text(el, "Milestone") == "1", "priority": int(text(el, "Priority", "500") or 500),
                "percent_complete": int(text(el, "PercentComplete", "0") or 0)}
        if text(el, "Summary") != "1":
            task["duration"] = round(_days(text(el, "Duration")), 2)
        if text(el, "ConstraintType") == "4" and text(el, "ConstraintDate"):
            task["start_no_earlier"] = text(el, "ConstraintDate")[:10]
        if text(el, "Deadline"):
            task["deadline"] = text(el, "Deadline")[:10]
        if text(el, "Notes"):
            task["notes"] = text(el, "Notes")
        link = text(el, "HyperlinkAddress") or ""
        if "/browse/" in link:
            task["jira"] = link.rsplit("/browse/", 1)[1]
        base = el.find(q("Baseline"), ns)
        if base is not None:
            if text(base, "Start"):
                task["baseline_start"] = text(base, "Start")[:10]
            if text(base, "Finish"):
                task["baseline_finish"] = text(base, "Finish")[:10]
        task["_links"] = [(text(pl, "PredecessorUID"), CODE_LINK.get(int(text(pl, "Type", "1") or 1), "FS"),
                           round(int(text(pl, "LinkLag", "0") or 0) / TENTHS_PER_DAY))
                          for pl in el.findall(q("PredecessorLink"), ns)]
        tasks.append(task)
        stack.append((level, tid))
    for t in tasks:
        t["depends_on"] = [{"id": uid_to_id[u], "type": k, "lag": lag} for u, k, lag in t.pop("_links") if u in uid_to_id]
        if t["parent"] is None:
            t.pop("parent")
    res_by_uid, resources = {}, []
    for el in root.findall(f"{q('Resources')}/{q('Resource')}", ns):
        uid, name = text(el, "UID"), text(el, "Name")
        if not name or uid == "0":
            continue
        rid = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or f"r{uid}"
        res_by_uid[uid] = rid
        resources.append({"id": rid, "name": name, "email": text(el, "EmailAddress"), "role": text(el, "Group"),
                          "max_units": float(text(el, "MaxUnits", "1") or 1),
                          "type": "work" if text(el, "Type", "1") == "1" else "material"})
    by_id = {t["id"]: t for t in tasks}
    for el in root.findall(f"{q('Assignments')}/{q('Assignment')}", ns):
        tid, rid = uid_to_id.get(text(el, "TaskUID")), res_by_uid.get(text(el, "ResourceUID"))
        if tid and rid and tid in by_id:
            by_id[tid].setdefault("resources", []).append({"id": rid, "units": float(text(el, "Units", "1") or 1)})
    status = text(root, "StatusDate")
    return {"name": text(root, "Title") or text(root, "Name") or "Imported plan", "start": start,
            **({"status_date": status[:10]} if status else {}), "resources": resources, "tasks": tasks}
