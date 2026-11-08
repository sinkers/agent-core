"""Schedule health checks (DCMA-style, adapted for gated plans). Each finding: {check, severity, task?, message}."""

from __future__ import annotations

import re

from .schedule import Schedule

LONG_TASK_DAYS = 20


BUFFER_RE = re.compile(r"\b(contingency|buffer|tolerance|float)\b", re.I)


def check(s: Schedule) -> list[dict]:
    plan, out = s.plan, []

    def add(check_name, severity, message, task=None):
        out.append({"check": check_name, "severity": severity, "message": message, **({"task": task} if task else {})})
    leaves = plan.leaves()
    has_succ = {link.pred for t in plan.tasks.values() for link in t.links}
    has_pred = set()
    for t in plan.tasks.values():
        if t.links:
            has_pred.update(plan.descendants(t.id) if t.summary else [t.id])
    succ_leaves = set()
    for pid in has_succ:
        succ_leaves.update(plan.descendants(pid) if plan.tasks[pid].summary else [pid])
    first, last = min(t.es for t in leaves), max(t.ef for t in leaves)
    for t in leaves:
        label = f"{t.name} ({t.id})"
        if t.id not in has_pred and t.es > first and not t.start_no_earlier:
            add("open start", "warning", f"{label} has no predecessor: what does it wait for?", t.id)
        if t.id not in succ_leaves and t.ef < last and not t.milestone:
            add("open end", "warning", f"{label} has no successor: nothing depends on it finishing", t.id)
        if t.float_days < 0:
            add("negative float", "error", f"{label} is {-t.float_days} working days late against its links", t.id)
        if t.duration > LONG_TASK_DAYS:
            add("long task", "info", f"{label} runs {t.duration} working days: split it so progress is visible", t.id)
        if not t.milestone and not t.assignments and not BUFFER_RE.search(t.name):  # buffers are nobody's work
            add("no owner", "warning", f"{label} has no one assigned", t.id)
        if any(link.lag < 0 for link in t.links):
            add("lead", "info", f"{label} uses a negative lag (lead): prefer an SS link with a lag", t.id)
        if t.deadline:
            finish = s.finish_date(t)
            if finish > t.deadline:
                add("deadline missed", "error", f"{label} finishes {finish:%a %d %b} after its deadline "
                                                f"{t.deadline:%a %d %b} (levelled schedule)", t.id)
    still = {x["resource"] for x in s.over_allocated}
    for o in s.unlevelled_over:
        if o["resource"] not in still:  # the unresolved ones are reported as errors below
            add("over-allocated", "info", f"{o['name']} was over-allocated on {o['days']} working days (peak "
                                          f"{o['peak_units'] * 100:.0f}%) before levelling; levelling resolved it")
    for o in s.over_allocated:
        add("over-allocated after levelling", "error", f"{o['name']} is still over-allocated on {o['days']} days "
                                                       f"from {o['first']} (peak {o['peak_units'] * 100:.0f}%): an "
                                                       "assignment needs more than they have, or it runs into their leave")
    if s.lev_finish > s.finish:
        add("levelling delay", "warning", f"levelling moves the finish from {s.finish_date_of(s.finish):%a %d %b} "
                                          f"to {s.finish_date_of(s.lev_finish):%a %d %b} "
                                          f"({s.lev_finish - s.finish} working days): add people or accept the date")
    milestones = [t for t in plan.tasks.values() if t.milestone]
    if not milestones:
        add("no milestones", "warning", "the plan has no milestones: add stage gates, approvals and handover")
    out += gate_findings(s)
    return out


def stage_of(plan, tid: str) -> str | None:
    """The top-level summary a task sits in (its stage), or None at the top level."""
    t = plan.tasks[tid]
    while t.parent and plan.tasks[t.parent].parent:
        t = plan.tasks[t.parent]
    return t.parent


def gate_findings(s: Schedule) -> list[dict]:
    """In the method each gate closes its stage (G3 closes Stage 3); only mid-stage gateways sit inside one.
    Work a stage still has after its gate belongs in the next stage, or in a stream that runs across stages."""
    plan, out = s.plan, []
    gates = [t for t in plan.tasks.values() if t.gate]
    for g in gates:
        stage = stage_of(plan, g.id)
        if stage:
            members, where = plan.descendants(stage), plan.tasks[stage].name
        elif len(gates) == 1:  # a stage plan on its own: its one gate closes the whole plan
            members, where = [t.id for t in plan.leaves()], plan.name
        else:
            continue
        when = s.start_date(g)
        late = [plan.tasks[d] for d in members
                if d != g.id and not plan.tasks[d].gate and s.finish_date(plan.tasks[d]) > when]
        if late:
            late.sort(key=lambda t: s.finish_date(t))
            names = "; ".join(f"{t.name} (to {s.finish_date(t):%d %b %y})" for t in late[:4])
            out.append({"check": "work after the gate", "severity": "warning", "task": g.id,
                        "message": (f"{where}: {len(late)} task(s) finish after {g.gate} "
                                    f"({when:%d %b %y}), but a gate closes its stage: {names}"
                                    + (" …" if len(late) > 4 else "") + ". Move them to the next stage, or to a "
                                    "stream that runs across stages (e.g. long-lead procurement, released at G2 and "
                                    "delivered in Stages 4 and 5); a decision part-way through a stage is a "
                                    "mid-stage gateway (gateway: true), not a gate.")})
    return out
