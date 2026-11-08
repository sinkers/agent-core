"""Jira timeline tools: native Start date, Blocks links that point the right way, rank, audit."""

import json
from datetime import date

import httpx
import pytest

from agentcore_mcp_jira.client import JiraError
from agentcore_mcp_jira.timeline import audit

from .test_jira_mcp import make_tools

FIELDS = [{"id": "customfield_10015", "name": "Start date", "custom": True,
           "schema": {"type": "date", "custom": "com.atlassian.jira.plugin.system.customfieldtypes:datepicker"}},
          {"id": "customfield_10999", "name": "Start date", "custom": True,
           "schema": {"type": "date", "custom": "com.atlassian.jira.plugin.system.customfieldtypes:datepicker"}},
          {"id": "customfield_10020", "name": "Sprint", "custom": True, "schema": {"type": "array", "custom": "com.pyxis.greenhopper.jira:gh-sprint"}},
          {"id": "duedate", "name": "Due date", "schema": {"type": "date"}}]


class FakeJira:
    """Enough Jira for the timeline tools. `reverse=True` stores links the opposite way to the request."""

    def __init__(self, reverse=False):
        self.reverse, self.links, self.next_id, self.puts, self.ranks = reverse, [], 1, [], []

    def links_of(self, key):
        out = []
        for lid, outward, inward in self.links:  # (id, the issue that blocks, the issue blocked)
            if outward == key:
                out.append({"id": str(lid), "type": {"name": "Blocks"}, "outwardIssue": {"key": inward}})
            if inward == key:
                out.append({"id": str(lid), "type": {"name": "Blocks"}, "inwardIssue": {"key": outward}})
        return out

    def __call__(self, req):
        path, method = req.url.path, req.method
        if path.endswith("/field"):
            return httpx.Response(200, json=FIELDS)
        if path.endswith("/field/search"):
            return httpx.Response(200, json={"values": [{"id": "customfield_10015", "name": "Start date", "isLocked": True},
                                                        {"id": "customfield_10999", "name": "Start date", "isLocked": False}]})
        if method == "GET" and "/issue/" in path:
            key = path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={"key": key, "fields": {"issuelinks": self.links_of(key)}})
        if method == "POST" and path.endswith("/issueLink"):
            body = json.loads(req.content)
            blocker, blocked = body["inwardIssue"]["key"], body["outwardIssue"]["key"]  # Jira: inwardIssue gets "blocks"
            if self.reverse:
                blocker, blocked = blocked, blocker
            self.links.append((self.next_id, blocker, blocked))
            self.next_id += 1
            return httpx.Response(201)
        if method == "DELETE" and "/issueLink/" in path:
            lid = int(path.rsplit("/", 1)[-1])
            self.links = [l for l in self.links if l[0] != lid]
            return httpx.Response(204)
        if method == "PUT" and path.endswith("/rest/agile/1.0/issue/rank"):
            self.ranks.append(json.loads(req.content))
            return httpx.Response(204)
        if method == "PUT" and "/issue/" in path:
            self.puts.append(json.loads(req.content))
            return httpx.Response(204)
        raise AssertionError(f"unexpected {method} {path}")


@pytest.mark.parametrize("reverse", [False, True])
async def test_link_issues_always_ends_with_the_blocker_blocking(reverse):
    jira = FakeJira(reverse=reverse)
    tools = make_tools(jira)
    out = await tools.link_issues("PRJ-1", "PRJ-2")
    assert out["created"] and jira.links_of("PRJ-1") == [{"id": jira.links_of("PRJ-1")[0]["id"], "type": {"name": "Blocks"},
                                                            "outwardIssue": {"key": "PRJ-2"}}]
    assert len(jira.links) == 1  # a reversed attempt was removed, not left behind
    again = await tools.link_issues("PRJ-1", "PRJ-2")
    assert again["created"] is False and len(jira.links) == 1
    with pytest.raises(JiraError, match="opposite way"):
        await tools.link_issues("PRJ-2", "PRJ-1")


async def test_start_date_uses_jiras_own_locked_field_and_dates_are_plain():
    jira = FakeJira()
    tools = make_tools(jira)
    await tools.update_issue("PRJ-5", start_date="2026-11-02T00:00:00+11:00", due_date="2026-11-27",
                             parent="PRJ-1", fix_versions_add=["M2 - FAT complete"])
    put = jira.puts[0]
    assert put["fields"] == {"customfield_10015": "2026-11-02", "duedate": "2026-11-27", "parent": {"key": "PRJ-1"}}
    assert put["update"]["fixVersions"] == [{"add": {"name": "M2 - FAT complete"}}]
    with pytest.raises(JiraError, match="YYYY-MM-DD"):
        await tools.update_issue("PRJ-5", due_date="next Tuesday")
    await tools.update_issue("PRJ-5", start_date="none")
    assert jira.puts[-1]["fields"] == {"customfield_10015": None}


async def test_rank_keeps_the_whole_list_in_order_in_chunks_of_50():
    jira = FakeJira()
    tools = make_tools(jira)
    keys = [f"PRJ-{n}" for n in range(1, 121)]
    out = await tools.rank_issues(keys)
    assert out["ranked"] == 120
    assert [(r.get("rankAfterIssue"), r["issues"][0], len(r["issues"])) for r in jira.ranks] == [
        ("PRJ-1", "PRJ-2", 50), ("PRJ-51", "PRJ-52", 50), ("PRJ-101", "PRJ-102", 19)]
    jira.ranks.clear()
    await tools.rank_issues(["PRJ-9", "PRJ-8"], before="PRJ-3")
    assert jira.ranks == [{"issues": ["PRJ-9", "PRJ-8"], "rankBeforeIssue": "PRJ-3"}]


def _it(key, summary="x", type_="Task", start=None, due=None, parent=None, blocked_by=(), done=False, assignee="A",
        level=0, subtask=False):
    return {"key": key, "url": "", "summary": summary, "type": type_, "subtask": subtask, "level": level, "status": "To Do",
            "done": done, "assignee": assignee, "start": start, "due": due, "parent": parent, "fix_versions": [],
            "blocks": [], "blocked_by": list(blocked_by)}


def test_audit_finds_what_makes_a_timeline_wrong():
    items = [
        _it("E-1", "Stage 1", "Epic", "2026-11-02", "2026-11-20", level=1),
        _it("E-2", "Stage 2", "Epic", "2026-11-09", None, level=1),          # one date
        _it("E-3", "Empty", "Epic", "2026-12-01", "2026-12-05", level=1),     # no children
        _it("T-1", "Isolate", start="2026-11-02", due="2026-11-06", parent="E-1"),
        _it("T-2", "Pack", start="2026-11-05", due="2026-11-25", parent="E-1", blocked_by=["T-1"]),  # conflict, after epic
        _it("T-3", "Move", start="2026-10-30", due="2026-10-31", parent="E-1"),  # before epic; ranked after T-2
        _it("T-4", "Drain", parent="E-2", assignee=None),                       # no dates, no owner
        _it("T-5", "Loose", start="2026-11-01", due="2026-11-02"),              # orphan
        _it("S-1", "Sub", subtask=True, parent="T-1"),
        _it("T-6", "Late", start="2026-09-01", due="2026-09-30", parent="E-2"),
    ]
    found = {(f["check"], f.get("key")) for f in audit(items, today=date(2026, 10, 6))}
    for expected in [("one date", "E-2"), ("epic without children", "E-3"), ("dependency conflict", "T-2"),
                     ("ends after parent", "T-2"), ("starts before parent", "T-3"), ("no dates", "T-4"),
                     ("no owner", "T-4"), ("orphan", "T-5"), ("subtask", "S-1"), ("overdue", "T-6"),
                     ("out of order", "T-3")]:
        assert expected in found, expected
    assert ("dependency conflict", "T-1") not in found and ("no dates", "E-3") not in found
    # children scheduled by sprint aren't judged on their own dates
    sprinted = {(f["check"], f.get("key")) for f in audit(items, today=date(2026, 10, 6), sprint_keys={"T-4"})}
    assert ("no dates", "T-4") not in sprinted
