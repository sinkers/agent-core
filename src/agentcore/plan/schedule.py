"""Scheduling: critical path, float and resource levelling, in working-day indices (calendar.py).

A task occupies working days [es, ef) (`ef` exclusive); a milestone has es == ef. Links between summaries are
expanded to the summary's leaf tasks. Links:

  FS  successor starts after the predecessor finishes (+ lag)      es(s) >= ef(p) + lag
  SS  starts after it starts                                       es(s) >= es(p) + lag
  FF  finishes after it finishes                                   ef(s) >= ef(p) + lag
  SF  finishes after it starts                                     ef(s) >= es(p) + lag

"Start no earlier than" moves a task forward; deadlines never move work (health.py flags misses).

Levelling is serial and keeps every link: tasks are placed in order of early start, then priority (higher first),
then total float (less first), then plan order; each one goes at the first start where every assigned resource has
the units free on every day it runs, given max_units and leave. A task that needs more units than a resource ever has
(e.g. 150% of a full-timer) is placed at its link-driven start and reported as over-allocated.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .calendar import WorkCalendar
from .model import Plan, PlanError, Task


@dataclass
class Schedule:
    plan: Plan
    calendar: WorkCalendar
    finish: int = 0             # project finish (exclusive index), unlevelled
    lev_finish: int = 0         # levelled
    critical: list[str] = field(default_factory=list)
    over_allocated: list[dict] = field(default_factory=list)   # after levelling (should be empty)
    unlevelled_over: list[dict] = field(default_factory=list)  # before levelling
    load: dict = field(default_factory=dict)       # {resource: {day: units}} unlevelled
    lev_load: dict = field(default_factory=dict)   # levelled

    def start_date(self, t: Task, levelled: bool = True):
        es = t.lev_es if levelled else t.es
        if t.milestone and es > 0:  # a milestone sits at the end of its predecessor's last day, as in MS Project
            return self.calendar.date_of(es - 1)
        return self.calendar.date_of(es)

    def finish_date(self, t: Task, levelled: bool = True):
        """The last working day the task runs (a milestone: its date)."""
        es, ef = (t.lev_es, t.lev_ef) if levelled else (t.es, t.ef)
        if ef == es:
            return self.start_date(t, levelled)
        return self.calendar.date_of(ef - 1)

    def finish_date_of(self, ef: int):
        """Date of the last working day before exclusive index `ef` (a project finish)."""
        return self.calendar.date_of(max(ef - 1, 0))

    def milestone_at_end_of_day(self, t: Task, levelled: bool = True) -> bool:
        return t.milestone and (t.lev_es if levelled else t.es) > 0


def _expanded_links(plan: Plan) -> dict[str, list]:
    """Each leaf task's predecessors as (pred_leaf_id, type, lag): summary links expanded to leaves, and a child
    inheriting its summary's links."""
    out: dict[str, list] = defaultdict(list)

    def leaves_of(tid: str) -> list[str]:
        return plan.descendants(tid) if plan.tasks[tid].summary else [tid]
    for t in plan.tasks.values():
        targets = leaves_of(t.id)
        for link in t.links:
            for pred in leaves_of(link.pred):
                for target in targets:
                    if pred != target:
                        out[target].append((pred, link.type, link.lag))
    return out


def _topological(plan: Plan, preds: dict[str, list]) -> list[str]:
    leaves = [t.id for t in plan.leaves()]
    indeg = {t: 0 for t in leaves}
    succs: dict[str, list] = defaultdict(list)
    for t in leaves:
        for p, _, _ in preds.get(t, []):
            indeg[t] += 1
            succs[p].append(t)
    ready = [t for t in leaves if indeg[t] == 0]
    out = []
    while ready:
        t = ready.pop(0)
        out.append(t)
        for s in succs[t]:
            indeg[s] -= 1
            if indeg[s] == 0:
                ready.append(s)
    if len(out) != len(leaves):
        stuck = sorted(set(leaves) - set(out))
        raise PlanError(f"dependency loop involving {', '.join(stuck[:6])}: a task can't (indirectly) depend on itself")
    return out


def _earliest(t: Task, preds: list, plan: Plan, cal: WorkCalendar, levelled: bool) -> int:
    es = cal.index_of(t.start_no_earlier) if t.start_no_earlier else 0
    for pid, kind, lag in preds:
        p = plan.tasks[pid]
        p_es, p_ef = (p.lev_es, p.lev_ef) if levelled else (p.es, p.ef)
        bound = {"FS": p_ef + lag, "SS": p_es + lag, "FF": p_ef + lag - t.duration, "SF": p_es + lag - t.duration}[kind]
        es = max(es, bound)
    return max(es, 0)


def _load(plan: Plan, levelled: bool) -> dict:
    load: dict = defaultdict(lambda: defaultdict(float))
    for t in plan.leaves():
        es, ef = (t.lev_es, t.lev_ef) if levelled else (t.es, t.ef)
        for a in t.assignments:
            if plan.resources[a.resource].kind != "work":
                continue
            for d in range(es, ef):
                load[a.resource][d] += a.units
    return load


def _capacity(plan: Plan, cal: WorkCalendar) -> dict:
    """{resource: set of working-day indices on leave}"""
    out: dict = {}
    for r in plan.resources.values():
        off = set()
        for first, last in r.leave:
            if first and last:
                for d in range(cal.index_of(first), cal.index_of(last) + (1 if cal.is_working(last) else 0)):
                    off.add(d)
        out[r.id] = off
    return out


def _overloads(plan: Plan, load: dict, leave: dict, cal: WorkCalendar) -> list[dict]:
    out = []
    for rid, days in load.items():
        cap = plan.resources[rid].max_units
        bad = sorted(d for d, u in days.items() if u > cap + 1e-9 or (d in leave[rid] and u > 0))
        if bad:
            out.append({"resource": rid, "name": plan.resources[rid].name, "days": len(bad),
                        "first": cal.date_of(bad[0]).isoformat(), "peak_units": round(max(days[d] for d in bad), 2)})
    return out


def schedule(plan: Plan) -> Schedule:
    cal = WorkCalendar(plan.start, plan.non_working, plan.region)
    preds = _expanded_links(plan)
    order = _topological(plan, preds)
    # forward pass
    for tid in order:
        t = plan.tasks[tid]
        t.es = _earliest(t, preds.get(tid, []), plan, cal, levelled=False)
        t.ef = t.es + t.duration
    finish = max(plan.tasks[t].ef for t in order)
    # backward pass
    succs: dict[str, list] = defaultdict(list)
    for tid, ps in preds.items():
        for p, kind, lag in ps:
            succs[p].append((tid, kind, lag))
    for tid in reversed(order):
        t = plan.tasks[tid]
        lf = finish
        for sid, kind, lag in succs.get(tid, []):
            s = plan.tasks[sid]
            lf = min(lf, {"FS": s.ls - lag, "SS": s.ls - lag + t.duration, "FF": s.lf - lag,
                          "SF": s.lf - lag + t.duration}[kind])
        t.lf, t.ls = lf, lf - t.duration
    # levelling
    leave = _capacity(plan, cal)
    lev: dict = defaultdict(lambda: defaultdict(float))
    rank = {tid: i for i, tid in enumerate(plan.order)}
    placed: set[str] = set()
    pending = list(order)
    while pending:
        # among tasks whose predecessors are placed, take the best by (early start, priority, float, plan order)
        ready = [tid for tid in pending if all(p in placed for p, _, _ in preds.get(tid, []))]
        tid = min(ready, key=lambda i: (plan.tasks[i].es, -plan.tasks[i].priority, plan.tasks[i].float_days, rank[i]))
        pending.remove(tid)
        t = plan.tasks[tid]
        s = _earliest(t, preds.get(tid, []), plan, cal, levelled=True)
        work = [a for a in t.assignments if plan.resources[a.resource].kind == "work"]
        feasible = all(a.units <= plan.resources[a.resource].max_units + 1e-9 for a in work)
        if work and feasible and t.duration > 0:
            for _ in range(2000):  # search forward for a slot (2000 working days ≈ 8 years)
                if all(lev[a.resource][d] + a.units <= plan.resources[a.resource].max_units + 1e-9
                       and d not in leave[a.resource] for a in work for d in range(s, s + t.duration)):
                    break
                s += 1
        t.lev_es, t.lev_ef = s, s + t.duration
        for a in work:
            for d in range(t.lev_es, t.lev_ef):
                lev[a.resource][d] += a.units
        placed.add(tid)
    # summaries roll up
    for tid in reversed(plan.order):
        t = plan.tasks[tid]
        if t.summary:
            kids = [plan.tasks[c] for c in plan.descendants(tid)]
            if kids:
                t.es, t.ef = min(k.es for k in kids), max(k.ef for k in kids)
                t.lev_es, t.lev_ef = min(k.lev_es for k in kids), max(k.lev_ef for k in kids)
                t.ls, t.lf = min(k.ls for k in kids), max(k.lf for k in kids)
    sched = Schedule(plan, cal, finish=finish, lev_finish=max(plan.tasks[t].lev_ef for t in order))
    sched.critical = [tid for tid in plan.order if plan.tasks[tid].critical]
    sched.load, sched.lev_load = _load(plan, False), _load(plan, True)
    sched.unlevelled_over = _overloads(plan, sched.load, leave, cal)
    sched.over_allocated = _overloads(plan, sched.lev_load, leave, cal)
    return sched
