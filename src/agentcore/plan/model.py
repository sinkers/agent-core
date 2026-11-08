"""The plan as data, built from the JSON spec the agent writes (or from an MS Project XML import).

Spec (all dates ISO, durations in working days):

  {"name": "Workstream A - Site Make Good", "start": "2026-10-19", "status_date": "2026-10-06",
   "days_per_point": 1, "non_working": [{"date": "2026-12-29", "name": "Close-down"}],
   "resources": [{"id": "sam", "name": "Sam Taylor", "email": "...", "role": "PM", "max_units": 1.0,
                  "leave": [{"from": "2026-11-02", "to": "2026-11-06"}]}],
   "tasks": [{"id": "s1", "name": "Stage 1 - Top deck"},                                   # a summary (has children)
             {"id": "t1", "name": "Isolate UPS", "parent": "s1", "duration": 3, "resources": ["sam"],
              "depends_on": ["m0"], "jira": "PRJ-364"},
             {"id": "t2", "name": "Remove PDUs", "parent": "s1", "story_points": 5, "resources": [{"id": "sam", "units": 0.5}],
              "depends_on": [{"id": "t1", "type": "SS", "lag": 1}], "deadline": "2026-11-13"},
             {"id": "m1", "name": "Stage 1 complete", "milestone": true, "depends_on": ["t2"]}]}

Task fields: id, name, parent, duration | story_points | milestone, depends_on (ids or {id, type FS|SS|FF|SF, lag}),
resources (ids or {id, units}; units default to the resource's max_units, as in MS Project), start_no_earlier, deadline, priority (1-1000, default 500), percent_complete,
baseline_start/baseline_finish, jira, notes, document, key (a key milestone or workstream for the overview),
url (a link shown on the task, e.g. a sub-plan's own viewer), deliverable (method deliverable ID(s) the task
produces, e.g. "D2.02": deliverables.py joins them with the method and the project register), gate ("G3": a stage gate, the milestone that closes
its stage in the method; a milestone named "G3 ..." is one automatically), gateway (true: a mid-stage gateway,
e.g. design freeze (IFC) in Stage 4, IST passed in Stage 6; also detected from those names).

Sub-plans (programme.py): a task {"id": "civil", "subplan": "<plan_id>"} in a master plan pulls that plan in as a
summary task; its tasks get ids "civil/<task id>", so the master can link to them ("depends_on": ["civil/m3"]).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date

from ..org import org

LINK_TYPES = ("FS", "SS", "FF", "SF")


GATE_NAME = re.compile(r"^\s*(G\d{1,2})\b")
GATEWAY_NAME = re.compile(r"mid-stage gateway|\bgateway\b|design freeze|\bIST passed\b", re.I)


def _gate_of(raw: dict, milestone: bool) -> str | None:
    g = raw.get("gate")
    if isinstance(g, str) and g.strip():
        return g.strip().upper()
    m = GATE_NAME.match(str(raw.get("name") or ""))
    if g is True or (g is None and milestone and m):
        return m.group(1).upper() if m else "G"
    return None


_DID = re.compile(r"D\s*(\d)\s*[.\-]\s*(\d{1,2})", re.I)


def _deliverables(value) -> list[str]:
    """"D2.2", ["d2.02", "D3.10"] -> ["D2.02", "D3.10"]: method deliverable IDs a task produces."""
    items = value if isinstance(value, list) else [value] if value else []
    out = []
    for v in items:
        for m in _DID.finditer(str(v)):
            did = f"D{m.group(1)}.{int(m.group(2)):02d}"
            if did not in out:
                out.append(did)
    return out


class PlanError(ValueError):
    """The spec can't be scheduled; the message says what to fix."""


@dataclass
class Link:
    pred: str
    type: str = "FS"
    lag: int = 0  # working days; negative = lead


@dataclass
class Assignment:
    resource: str
    units: float = 1.0


@dataclass
class Resource:
    id: str
    name: str
    email: str | None = None
    role: str | None = None
    max_units: float = 1.0
    leave: list[tuple[date, date]] = field(default_factory=list)
    kind: str = "work"  # work | material


@dataclass
class Task:
    id: str
    name: str
    parent: str | None = None
    duration: int = 0
    milestone: bool = False
    links: list[Link] = field(default_factory=list)
    assignments: list[Assignment] = field(default_factory=list)
    start_no_earlier: date | None = None
    deadline: date | None = None
    priority: int = 500
    percent_complete: int = 0
    baseline_start: date | None = None
    baseline_finish: date | None = None
    jira: str | None = None
    notes: str | None = None
    document: str | None = None
    key: bool = False
    deliverable: list = field(default_factory=list)  # method deliverable IDs this task produces ("D2.02")
    gate: str | None = None     # "G3": the gate that closes a stage (the method)
    gateway: bool = False       # a mid-stage gateway (design freeze, IST passed, a Stage 3 planning gateway)
    url: str | None = None
    subplan: str | None = None  # the plan_id this summary task pulls in (programme.py)
    children: list[str] = field(default_factory=list)
    # set by the scheduler
    es: int = 0
    ef: int = 0
    ls: int = 0
    lf: int = 0
    lev_es: int = 0
    lev_ef: int = 0

    @property
    def summary(self) -> bool:
        return bool(self.children)

    @property
    def float_days(self) -> int:
        return self.ls - self.es

    @property
    def critical(self) -> bool:
        return not self.summary and self.float_days <= 0


@dataclass
class Plan:
    name: str
    start: date
    status_date: date | None
    tasks: dict[str, Task]
    resources: dict[str, Resource]
    non_working: dict[date, str]
    order: list[str]  # spec order (display order, children after parents)
    jira_base: str = field(default_factory=lambda: f"{org().jira_url.rstrip('/')}/browse/" if org().jira_url else "")
    days_per_point: float = 1.0
    region: str = field(default_factory=lambda: org().holidays)

    def leaves(self) -> list[Task]:
        return [self.tasks[i] for i in self.order if not self.tasks[i].summary]

    def descendants(self, task_id: str) -> list[str]:
        out = []
        for c in self.tasks[task_id].children:
            out += [c] if not self.tasks[c].summary else self.descendants(c)
        return out

    def outline(self, task_id: str) -> int:
        level, t = 1, self.tasks[task_id]
        while t.parent:
            level, t = level + 1, self.tasks[t.parent]
        return level

    def wbs(self) -> dict[str, str]:
        """WBS codes 1, 1.1, 1.1.1 in display order."""
        codes: dict[str, str] = {}
        counters: dict[str | None, int] = {}
        for tid in self.order:
            parent = self.tasks[tid].parent
            counters[parent] = counters.get(parent, 0) + 1
            codes[tid] = f"{codes[parent]}.{counters[parent]}" if parent else str(counters[parent])
        return codes


def _date(value, what: str) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise PlanError(f"{what}: {value!r} isn't a date (use YYYY-MM-DD)") from None


def from_spec(spec: dict) -> Plan:
    if not isinstance(spec, dict):
        raise PlanError("the plan must be a JSON object")
    start = _date(spec.get("start"), "start")
    if not start:
        raise PlanError("start (the project start date) is required")
    dpp = float(spec.get("days_per_point") or 1)
    resources: dict[str, Resource] = {}
    for r in spec.get("resources") or []:
        rid = str(r.get("id") or r.get("name") or "").strip()
        if not rid:
            raise PlanError("every resource needs an id")
        leave = [(_date(lv.get("from"), f"{rid} leave"), _date(lv.get("to") or lv.get("from"), f"{rid} leave"))
                 for lv in r.get("leave") or []]
        resources[rid] = Resource(rid, str(r.get("name") or rid), r.get("email"), r.get("role"),
                                  float(r.get("max_units") or 1.0), leave, str(r.get("type") or "work"))
    tasks: dict[str, Task] = {}
    order: list[str] = []
    for raw in spec.get("tasks") or []:
        tid = str(raw.get("id") or "").strip()
        if not tid or tid in tasks:
            raise PlanError(f"task ids must be present and unique (problem: {tid or raw.get('name')!r})")
        milestone = bool(raw.get("milestone"))
        if raw.get("duration") is not None:
            duration = raw["duration"]
        elif raw.get("story_points") is not None:
            duration = float(raw["story_points"]) * dpp
        else:
            duration = 0 if milestone else None
        links = []
        for dep in raw.get("depends_on") or []:
            d = {"id": dep} if isinstance(dep, str) else dict(dep)
            kind = str(d.get("type") or "FS").upper()
            if kind not in LINK_TYPES:
                raise PlanError(f"{tid}: link type {kind!r} must be one of {', '.join(LINK_TYPES)}")
            links.append(Link(str(d["id"]), kind, int(round(float(d.get("lag") or 0)))))
        assignments = []
        for a in raw.get("resources") or []:
            a = {"id": a} if isinstance(a, str) else dict(a)
            if str(a["id"]) not in resources:
                resources[str(a["id"])] = Resource(str(a["id"]), str(a["id"]))  # named inline: add it
            # As in MS Project, an assignment defaults to the resource's availability (a 50% person at 50%).
            units = a.get("units")
            assignments.append(Assignment(str(a["id"]), float(units) if units else resources[str(a["id"])].max_units))
        tasks[tid] = Task(
            id=tid, name=str(raw.get("name") or tid), parent=(str(raw["parent"]) if raw.get("parent") else None),
            duration=max(0, math.ceil(float(duration))) if duration is not None else -1, milestone=milestone,
            links=links, assignments=assignments,
            start_no_earlier=_date(raw.get("start_no_earlier"), f"{tid} start_no_earlier"),
            deadline=_date(raw.get("deadline"), f"{tid} deadline"), priority=int(raw.get("priority") or 500),
            percent_complete=int(raw.get("percent_complete") or 0),
            baseline_start=_date(raw.get("baseline_start"), f"{tid} baseline_start"),
            baseline_finish=_date(raw.get("baseline_finish"), f"{tid} baseline_finish"),
            jira=raw.get("jira"), notes=raw.get("notes"), document=raw.get("document"), key=bool(raw.get("key")),
            gate=_gate_of(raw, milestone), deliverable=_deliverables(raw.get("deliverable")),
            gateway=bool(raw.get("gateway")) or (raw.get("gateway") is None and milestone and not _gate_of(raw, milestone)
                                                 and bool(GATEWAY_NAME.search(str(raw.get("name") or "")))),
            url=raw.get("url") if str(raw.get("url") or "").startswith("https://") else None,
            subplan=raw.get("subplan_of") or None)
        order.append(tid)
    if not tasks:
        raise PlanError("the plan has no tasks")
    for t in tasks.values():
        if t.parent:
            if t.parent not in tasks:
                raise PlanError(f"{t.id}: parent {t.parent!r} isn't a task in the plan")
            tasks[t.parent].children.append(t.id)
        for link in t.links:
            if link.pred not in tasks:
                raise PlanError(f"{t.id} depends on {link.pred!r}, which isn't a task in the plan")
    for t in tasks.values():
        if not t.summary and t.duration < 0:
            raise PlanError(f"{t.id} ({t.name}) needs a duration (working days), story_points, or milestone: true")
        if t.summary:
            t.duration = 0
        if t.milestone:
            t.duration = 0
    # display order: each child right after its parent's previous children
    ordered: list[str] = []

    def walk(tid: str) -> None:
        ordered.append(tid)
        for c in tasks[tid].children:
            walk(c)
    for tid in order:
        if not tasks[tid].parent:
            walk(tid)
    non_working = {}
    for nw in spec.get("non_working") or []:
        nw = {"date": nw} if isinstance(nw, str) else nw
        non_working[_date(nw.get("date"), "non_working")] = str(nw.get("name") or "Non-working day")
    return Plan(name=str(spec.get("name") or "Project plan"), start=start,
                status_date=_date(spec.get("status_date"), "status_date"), tasks=tasks, resources=resources,
                non_working=non_working, order=ordered, days_per_point=dpp)
