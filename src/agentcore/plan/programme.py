"""Programmes: a master plan made of sub-plans, so no single plan is too big to write in one go.

Each sub-plan is an ordinary plan (make_plan / edit_plan, its own plan_id, viewer and MS Project XML) that an agent
can work on by itself. A master plan links them in with a task that names the sub-plan:

  {"id": "civil", "subplan": "a1b2c3d4e5", "depends_on": ["m0"]}       (name defaults to the sub-plan's name)

When the master is scheduled, each sub-plan's tasks are placed under that task with ids "civil/<task id>", so:
  - the master can depend on, or be depended on by, any task inside a sub-plan: "depends_on": ["civil/m3"];
  - links on the sub-plan task apply to all of its work (as for any summary task);
  - nothing in a sub-plan starts before that sub-plan's own start date;
  - resources with the same id are the same person across the programme, so levelling sees all their work;
  - non-working days are combined.
Sub-plans can contain sub-plans (up to MAX_DEPTH); a plan can't include itself.
"""

from __future__ import annotations

import copy
from typing import Awaitable, Callable

from .model import PlanError

MAX_DEPTH = 3
SEP = "/"

Loader = Callable[[str], Awaitable[dict | None]]  # plan_id -> spec (or None)


def subplan_ids(spec: dict) -> list[str]:
    return [str(t["subplan"]) for t in spec.get("tasks") or [] if isinstance(t, dict) and t.get("subplan")]


async def expand(spec: dict, load: Loader, _stack: tuple[str, ...] = ()) -> tuple[dict, list[dict]]:
    """The spec with every sub-plan pulled in, and [{task, plan_id, name, tasks}] for each one included."""
    if not subplan_ids(spec):
        return spec, []
    if len([x for x in _stack if x]) > MAX_DEPTH:
        raise PlanError(f"sub-plans are nested more than {MAX_DEPTH} deep ({' > '.join(_stack)})")
    out = copy.deepcopy(spec)
    tasks: list[dict] = []
    resources = {str(r.get("id") or r.get("name")): r for r in out.get("resources") or []}
    non_working = list(out.get("non_working") or [])
    included: list[dict] = []
    for raw in out.get("tasks") or []:
        if not raw.get("subplan"):
            tasks.append(raw)
            continue
        sub_id, link_id = str(raw["subplan"]), str(raw.get("id") or "").strip()
        if not link_id:
            raise PlanError(f"the task that includes sub-plan {sub_id} needs an id")
        if sub_id in _stack:
            raise PlanError(f"sub-plan {sub_id} includes itself ({' > '.join(_stack + (sub_id,))})")
        sub = await load(sub_id)
        if not sub:
            raise PlanError(f"{link_id}: no plan {sub_id!r} (make it with make_plan first; list_plans shows them)")
        sub, nested = await expand(sub, load, _stack + (sub_id,))
        own = {str(t.get("id")) for t in sub.get("tasks") or []}
        summary = {k: v for k, v in raw.items() if k != "subplan"}
        summary.setdefault("name", sub.get("name") or link_id)
        summary["subplan_of"] = sub_id
        tasks.append(summary)
        start = sub.get("start")
        for t in sub.get("tasks") or []:
            t = dict(t)
            tid = str(t.get("id"))
            t["id"] = f"{link_id}{SEP}{tid}"
            t["parent"] = f"{link_id}{SEP}{t['parent']}" if t.get("parent") else link_id
            deps = []
            for d in t.get("depends_on") or []:
                d = {"id": d} if isinstance(d, str) else dict(d)
                if str(d["id"]) not in own:
                    raise PlanError(f"sub-plan {sub.get('name') or sub_id}: {tid} depends on {d['id']!r}, which isn't "
                                    "in that plan (links between plans go in the master: \"depends_on\": "
                                    f"[\"{link_id}{SEP}<task id>\"])")
                deps.append({**d, "id": f"{link_id}{SEP}{d['id']}"})
            t["depends_on"] = deps
            if start and not t.get("start_no_earlier"):
                t["start_no_earlier"] = start  # nothing in a sub-plan starts before its own start
            tasks.append(t)
        for r in sub.get("resources") or []:
            resources.setdefault(str(r.get("id") or r.get("name")), r)
        non_working += sub.get("non_working") or []
        included.append({"task": link_id, "plan_id": sub_id, "name": summary["name"], "tasks": len(own)})
        included += [{**n, "task": f"{link_id}{SEP}{n['task']}"} for n in nested]
    out["tasks"], out["resources"], out["non_working"] = tasks, list(resources.values()), non_working
    return out, included
