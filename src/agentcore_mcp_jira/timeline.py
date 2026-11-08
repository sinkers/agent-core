"""Jira timeline support: the native Start date field, dependency links that point the right way, rank order,
and an audit of how a project's timeline will look (jira-timeline skill).

What the Jira timeline draws (per Atlassian's docs):
  bars       the system "Start date" (a locked custom field, often customfield_10015) and `duedate`; nothing else.
             One date only gives a faded, open-ended bar. A parent with no dates of its own rolls up from its
             children; dates set on the parent override the roll-up.
  arrows     "Blocks" links only, within one project; a blocked item that starts before its blocker is due
             turns the arrow red. Jira warns; it never moves work.
  order      Rank (epics among epics, children within their parent).
  levels     epics and their children; subtasks never; at most 5,000 items and 500 epics.
  milestones releases (fixVersions with a release date) when "Releases" is ticked in view settings.

Link direction is the trap: REST gives the outward wording ("blocks") to the issue sent as `inwardIssue`, and
sources disagree, so every link made here is read back from the blocker and fixed if it came out reversed.
"""

from __future__ import annotations

from datetime import date
from typing import Any

BLOCKS = "Blocks"
LOCKED_START_NAME = "start date"


def _d(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def blocks_of(issue: dict) -> list[str]:
    """Keys this issue blocks (its outward Blocks links)."""
    out = []
    for link in (issue.get("fields") or {}).get("issuelinks") or []:
        if (link.get("type") or {}).get("name") == BLOCKS and link.get("outwardIssue"):
            out.append(link["outwardIssue"]["key"])
    return out


def blocked_by(issue: dict) -> list[str]:
    out = []
    for link in (issue.get("fields") or {}).get("issuelinks") or []:
        if (link.get("type") or {}).get("name") == BLOCKS and link.get("inwardIssue"):
            out.append(link["inwardIssue"]["key"])
    return out


def item(issue: dict, start_field: str | None, browse) -> dict:
    f = issue.get("fields") or {}
    return {"key": issue["key"], "url": browse(issue["key"]), "summary": f.get("summary"),
            "type": (f.get("issuetype") or {}).get("name"),
            "subtask": bool((f.get("issuetype") or {}).get("subtask")),
            "level": (f.get("issuetype") or {}).get("hierarchyLevel"),
            "status": (f.get("status") or {}).get("name"),
            "done": ((f.get("status") or {}).get("statusCategory") or {}).get("key") == "done",
            "assignee": (f.get("assignee") or {}).get("displayName"),
            "start": f.get(start_field) if start_field else None, "due": f.get("duedate"),
            "parent": (f.get("parent") or {}).get("key"),
            "fix_versions": [v.get("name") for v in f.get("fixVersions") or []],
            "blocks": blocks_of(issue), "blocked_by": blocked_by(issue)}


def audit(items: list[dict], today: date | None = None, sprint_keys: set[str] | None = None) -> list[dict]:
    """Findings for timeline items in rank order: [{check, severity, key?, message}]. `sprint_keys` are children
    scheduled by sprint (their own dates are ignored by the timeline)."""
    today = today or date.today()
    sprint_keys = sprint_keys or set()
    by_key = {i["key"]: i for i in items}
    out: list[dict] = []

    def add(check, severity, message, key=None):
        out.append({"check": check, "severity": severity, "message": message, **({"key": key} if key else {})})

    parents = {i["key"] for i in items if (i.get("level") or 0) >= 1 or i["type"] == "Epic"}
    children: dict[str, list[dict]] = {}
    for i in items:
        if i["parent"]:
            children.setdefault(i["parent"], []).append(i)
    for i in items:
        k, s, d = i["key"], _d(i["start"]), _d(i["due"])
        label = f"{k} {i['summary']}"
        if i["subtask"]:
            add("subtask", "info", f"{label} is a subtask: the timeline never shows subtasks", k)
            continue
        is_parent = k in parents
        if is_parent and not children.get(k):
            add("epic without children", "info", f"{label} has no child issues", k)
        if s and d and d < s:
            add("dates reversed", "error", f"{label} is due ({d}) before it starts ({s})", k)
        if not is_parent and k not in sprint_keys:
            if not s and not d:
                add("no dates", "warning", f"{label} has no start or due date, so it has no bar", k)
            elif not s or not d:
                add("one date", "warning", f"{label} has only a {'due' if d else 'start'} date: the bar fades out", k)
        if is_parent and (bool(s) != bool(d)):
            add("one date", "warning", f"{label} has only a {'due' if d else 'start'} date: set both or neither "
                                       "(neither rolls up from its children)", k)
        if not i["done"] and d and d < today:
            add("overdue", "warning", f"{label} was due {d} and is {i['status']}", k)
        if not i["done"] and not i["assignee"] and not is_parent:
            add("no owner", "info", f"{label} has no assignee", k)
        if not is_parent and not i["parent"] and i["type"] not in ("Epic",):
            add("orphan", "info", f"{label} has no parent epic", k)
        parent = by_key.get(i["parent"]) if i["parent"] else None
        if parent and k not in sprint_keys:
            ps, pd = _d(parent["start"]), _d(parent["due"])
            if ps and s and s < ps:
                add("starts before parent", "warning", f"{label} starts {s}, before its epic {parent['key']} ({ps})", k)
            if pd and d and d > pd:
                add("ends after parent", "warning", f"{label} is due {d}, after its epic {parent['key']} ({pd})", k)
        for blocker_key in i["blocked_by"]:
            blocker = by_key.get(blocker_key)
            if not blocker:
                continue
            bd = _d(blocker["due"])
            if bd and s and s <= bd and not blocker["done"]:
                add("dependency conflict", "error", f"{label} starts {s} but {blocker_key} (which blocks it) is due "
                                                   f"{bd}: the arrow shows red", k)
    # rank vs dates: within each parent (and among epics), the order shown should follow the start dates
    groups: dict[str | None, list[dict]] = {}
    for i in items:
        if not i["subtask"]:
            groups.setdefault(i["parent"] if i["key"] not in parents else "__epics__", []).append(i)
    for group, members in groups.items():
        dated = [m for m in members if _d(m["start"]) or _d(m["due"])]
        for a, b in zip(dated, dated[1:]):
            sa, sb = _d(a["start"]) or _d(a["due"]), _d(b["start"]) or _d(b["due"])
            if sa and sb and sb < sa:
                add("out of order", "info", f"{b['key']} starts {sb} but is ranked after {a['key']} ({sa}): "
                                            "rank_issues to put them in date order", b["key"])
                break
    if len(items) > 4500:
        add("too many", "warning", f"{len(items)} items: the timeline shows at most 5,000 (500 epics)")
    return out
