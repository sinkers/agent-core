import json
import re
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import pytest

from agentcore.plan.calendar import WorkCalendar, tas_south_holidays
from agentcore.plan.health import check
from agentcore.plan.model import PlanError, from_spec
from agentcore.plan.mspdi import NS, export_xml, import_xml
from agentcore.plan.render import html_viewer, mermaid, plan_data
from agentcore.plan.schedule import schedule

FIX = Path(__file__).parent / "fixtures"
SAMPLE = json.loads((FIX / "workstream_a_plan.json").read_text())


def plan(tasks, resources=None, start="2026-10-19", **kw):
    return schedule(from_spec({"name": "T", "start": start, "tasks": tasks, "resources": resources or [], **kw}))


def test_tasmanian_holidays_including_moved_ones():
    h26, h27 = tas_south_holidays(2026), tas_south_holidays(2027)
    assert h26[date(2026, 10, 22)] == "Royal Hobart Show" and h26[date(2026, 2, 9)] == "Royal Hobart Regatta"
    assert h26[date(2026, 12, 28)] == "Boxing Day"  # Boxing Day on Saturday -> Monday
    assert h27[date(2027, 12, 27)] == "Christmas Day" and h27[date(2027, 12, 28)] == "Boxing Day"
    assert h27[date(2027, 3, 26)] == "Good Friday" and h27[date(2027, 3, 29)] == "Easter Monday"
    cal = WorkCalendar(date(2026, 10, 17), region="tas-south")  # a Saturday: starts Monday
    assert cal.start == date(2026, 10, 19) and cal.date_of(3) == date(2026, 10, 23)  # skips the Show
    assert cal.index_of(date(2026, 10, 24)) == cal.index_of(date(2026, 10, 26)) == 4


@pytest.mark.parametrize("kind,lag,expect_start", [
    ("FS", 0, date(2026, 10, 26)), ("FS", 2, date(2026, 10, 28)), ("FS", -1, date(2026, 10, 23)),
    ("SS", 1, date(2026, 10, 20)), ("FF", 0, date(2026, 10, 21)), ("SF", 2, date(2026, 10, 19))])
def test_link_types_and_lags(kind, lag, expect_start):
    s = plan([{"id": "a", "name": "A", "duration": 4},
              {"id": "b", "name": "B", "duration": 2, "depends_on": [{"id": "a", "type": kind, "lag": lag}]}])
    assert s.start_date(s.plan.tasks["b"]) == expect_start


def test_critical_path_float_and_start_no_earlier():
    s = plan([{"id": "a", "name": "A", "duration": 5}, {"id": "b", "name": "B", "duration": 2},
              {"id": "c", "name": "C", "duration": 1, "depends_on": ["a", "b"]},
              {"id": "d", "name": "D", "duration": 1, "start_no_earlier": "2026-11-02", "depends_on": ["c"]},
              {"id": "m", "name": "Done", "milestone": True, "depends_on": ["d"]}])
    t = s.plan.tasks
    # D's "start no earlier than 2 Nov" is the driver: A and C have 3 days' slack before it, B 6
    assert t["d"].critical and t["m"].critical and not t["a"].critical
    assert (t["a"].float_days, t["b"].float_days, t["c"].float_days) == (3, 6, 3)
    assert s.start_date(t["d"]) == date(2026, 11, 2)
    tight = plan([{"id": "a", "name": "A", "duration": 5}, {"id": "b", "name": "B", "duration": 2},
                  {"id": "c", "name": "C", "duration": 1, "depends_on": ["a", "b"]}])
    assert tight.plan.tasks["a"].critical and tight.plan.tasks["b"].float_days == 3
    assert s.start_date(t["m"]) == date(2026, 11, 2)  # a milestone sits at the end of its predecessor's day


def test_levelling_keeps_links_respects_leave_and_reports_the_delay():
    s = plan([{"id": "a", "name": "A", "duration": 3, "resources": ["sam"]},
              {"id": "b", "name": "B", "duration": 2, "resources": ["sam"]},
              {"id": "c", "name": "C", "duration": 1, "depends_on": ["b"], "resources": ["sam"]}],
             resources=[{"id": "sam", "name": "Sam Taylor", "leave": [{"from": "2026-10-26", "to": "2026-10-26"}]}])
    t = s.plan.tasks
    days = {tid: (s.start_date(t[tid]), s.finish_date(t[tid])) for tid in "abc"}
    assert days["a"] == (date(2026, 10, 19), date(2026, 10, 21))      # A first (longest, critical)
    # B needs 2 days in a row: the 23rd then Sam's leave on the 26th won't do (tasks aren't split), so 27-28
    assert days["b"] == (date(2026, 10, 27), date(2026, 10, 28))
    assert days["c"][0] > days["b"][1] and s.over_allocated == [] and s.lev_finish > s.finish
    assert any(f["check"] == "levelling delay" for f in check(s))


def test_part_time_people_are_booked_at_their_availability_and_impossible_loads_are_flagged():
    s = plan([{"id": "a", "name": "A", "duration": 2, "resources": ["david"]}],
             resources=[{"id": "david", "name": "David Hillier", "max_units": 0.5}])
    assert s.plan.tasks["a"].assignments[0].units == 0.5 and s.over_allocated == []
    s = plan([{"id": "a", "name": "A", "duration": 2, "resources": [{"id": "david", "units": 1}]}],
             resources=[{"id": "david", "name": "David Hillier", "max_units": 0.5}])
    assert s.over_allocated and any(f["check"] == "over-allocated after levelling" for f in check(s))


def test_summary_links_expand_to_their_tasks_and_children_inherit():
    s = plan([{"id": "s1", "name": "Stage 1"}, {"id": "a", "name": "A", "parent": "s1", "duration": 2},
              {"id": "b", "name": "B", "parent": "s1", "duration": 4},
              {"id": "s2", "name": "Stage 2", "depends_on": ["s1"]}, {"id": "c", "name": "C", "parent": "s2", "duration": 1}])
    t = s.plan.tasks
    assert s.start_date(t["c"]) == date(2026, 10, 26) and s.finish_date(t["s1"]) == date(2026, 10, 23)
    assert s.plan.wbs() == {"s1": "1", "a": "1.1", "b": "1.2", "s2": "2", "c": "2.1"}


@pytest.mark.parametrize("tasks,message", [
    ([{"id": "a", "name": "A", "duration": 1, "depends_on": ["b"]}, {"id": "b", "name": "B", "duration": 1, "depends_on": ["a"]}], "loop"),
    ([{"id": "a", "name": "A"}], "needs a duration"),
    ([{"id": "a", "name": "A", "duration": 1, "depends_on": ["zz"]}], "isn't a task"),
    ([{"id": "a", "name": "A", "duration": 1, "depends_on": [{"id": "a", "type": "XX"}]}], "link type"),
    ([], "no tasks")])
def test_bad_plans_get_a_clear_reason(tasks, message):
    with pytest.raises(PlanError, match=message):
        plan(tasks)


def test_health_checks_on_the_sample():
    s = schedule(from_spec(SAMPLE))
    found = {(f["check"], f.get("task")) for f in check(s)}
    assert ("no owner", "t14") in found and ("no owner", "t23") not in found  # t23 is contingency
    assert not any(c == "deadline missed" for c, _ in found)
    late = dict(SAMPLE, tasks=[dict(t, deadline="2026-11-02") if t["id"] == "m21" else t for t in SAMPLE["tasks"]])
    assert any(f["check"] == "deadline missed" and f["task"] == "m21" for f in check(schedule(from_spec(late))))
    bare = plan([{"id": "a", "name": "A", "duration": 1, "resources": ["x"]}])
    assert any(f["check"] == "no milestones" for f in check(bare))


def _x(root, path):
    return root.findall(path.replace("/", "/{%s}" % NS).replace("./", "./{%s}" % NS) if False else path, {"p": NS})


def test_ms_project_xml_has_everything_ms_project_needs():
    s = schedule(from_spec(SAMPLE))
    root = ET.fromstring(export_xml(s))
    ns = {"p": NS}
    tasks = {t.find("p:Name", ns).text: t for t in root.findall("p:Tasks/p:Task", ns)}
    pdus = tasks["Remove PDUs and cabling"]
    link = pdus.find("p:PredecessorLink", ns)
    assert link.find("p:Type", ns).text == "3" and link.find("p:LinkLag", ns).text == str(2 * 4800)  # SS+2d
    assert int(pdus.find("p:LevelingDelay", ns).text) > 0
    assert tasks["Plan approved"].find("p:Milestone", ns).text == "1"
    assert tasks["Plan approved"].find("p:Start", ns).text.endswith("T17:00:00")  # end of the predecessor's day
    assert tasks["Truck to Exeter (booked)"].find("p:ConstraintType", ns).text == "4"
    assert tasks["Finalise 1P dismantling plan Rev 1"].find("p:HyperlinkAddress", ns).text.endswith("/browse/PRJ-442")
    holidays = [p.find("p:FromDate", ns).text[:10] for p in root.findall("p:Calendars/p:Calendar/p:WeekDays/p:WeekDay/p:TimePeriod", ns)]
    assert "2026-10-22" in holidays and "2026-12-29" in holidays and "2026-11-09" in holidays  # holiday, close-down, leave
    units = {a.find("p:Units", ns).text for a in root.findall("p:Assignments/p:Assignment", ns)}
    assert "0.50" in units and "2.00" in units


def test_export_then_import_round_trips():
    s = schedule(from_spec(SAMPLE))
    again = schedule(from_spec(import_xml(export_xml(s, levelled=False))))
    assert again.finish_date_of(again.lev_finish) == s.finish_date_of(s.lev_finish)
    assert [t.name for t in again.plan.leaves()] == [t.name for t in s.plan.leaves()]


def test_imports_ms_project_xml_written_by_other_software():
    """Written by MPXJ (the library ProjectLibre uses): different element order and defaults."""
    spec = import_xml((FIX / "mpxj_written_plan.xml").read_bytes())
    s = schedule(from_spec(spec))
    assert s.finish_date_of(s.lev_finish) == date(2026, 12, 9) and len(spec["resources"]) == 4
    with pytest.raises(ValueError):
        import_xml(b"<not-xml")


def test_html_viewer_carries_the_plan_and_can_not_be_broken_by_task_names():
    evil = dict(SAMPLE, tasks=SAMPLE["tasks"] + [{"id": "x", "name": "</script><script>alert(1)</script>", "duration": 1}])
    page = html_viewer(schedule(from_spec(evil))).decode()
    assert page.count("</script>") == 1 and "<\\/script>" in page
    data = plan_data(schedule(from_spec(SAMPLE)))
    assert data["finish"]["l"] == "2026-12-09" and {r["name"] for r in data["resources"]} >= {"Sam Taylor", "Site crew"}
    sam = next(r for r in data["resources"] if r["name"] == "Sam Taylor")
    assert sam["load"]["l"][0] == 0.75  # 3 of 4 working days in the week of the Show


def test_mermaid_sections_critical_milestones_and_safe_names():
    s = schedule(from_spec(dict(SAMPLE, tasks=SAMPLE["tasks"] + [{"id": "y", "name": "Fix: #1; urgent", "duration": 1}])))
    text = mermaid(s)
    assert text.startswith("gantt") and "section Stage 1 - Top deck" in text
    assert ":crit, t14, 2026-11-23, 2026-11-24" in text and "Plan approved :milestone, m_m01, 2026-10-28, 0d" in text
    assert "Fix - -1 - urgent" in text and "excludes 2026-10-22" in text


async def test_ada_tools_make_export_and_import(cfg):
    from agentcore.files import FileTools
    from agentcore.plan.tools import PlanTools
    from agentcore.store import MemoryStore
    from agentcore.tools import CallContext
    store = MemoryStore()
    files = FileTools(store)
    tools = PlanTools(store, files=files)
    ctx = CallContext("chat-a", "aad-alex", [], actor_name="Alex Owner")
    made = json.loads(await tools.call_tool("make_plan", {"plan": SAMPLE}, ctx))
    assert made["finish"] == "2026-12-09" and made["critical_path"] and made["milestones"][0]["name"] == "Plan approved"
    out = json.loads(await tools.call_tool("export_plan", {"plan_id": made["plan_id"],
                                                           "formats": ["msproject_xml", "html", "mermaid", "mermaid_html"]}, ctx))
    assert set(out["files"]) == {"msproject_xml", "html", "mermaid", "mermaid_html"}
    default = json.loads(await tools.call_tool("export_plan", {"plan_id": made["plan_id"]}, ctx))
    assert set(default["files"]) == {"msproject_xml", "html"}
    xml_ref = out["files"]["msproject_xml"]["file"]
    data, name, _ = await files.resolve(xml_ref, "chat-a")
    assert name.endswith(".xml") and b"schemas.microsoft.com/project" in data
    imported = json.loads(await tools.call_tool("import_project_xml", {"file": xml_ref}, ctx))
    assert imported["finish"] == "2026-12-09" and imported["plan_id"] != made["plan_id"]
    bad = json.loads(await tools.call_tool("make_plan", {"plan": {"name": "x", "start": "2026-10-19", "tasks": [{"id": "a", "name": "A"}]}}, ctx))
    assert "needs a duration" in bad["error"]
    revised = json.loads(await tools.call_tool("make_plan", {"plan": SAMPLE, "plan_id": made["plan_id"]}, ctx))
    assert revised["plan_id"] == made["plan_id"]




async def test_publish_plan_to_apps_and_link_it_from_sharepoint(cfg):
    import base64

    from agentcore.files import FileTools
    from agentcore.plan.tools import PlanTools, app_name
    from agentcore.store import MemoryStore
    from agentcore.tools import CallContext, ToolOutcome

    class Apps:
        def __init__(self):
            self.calls, self.fail = [], False

        async def call_tool(self, name, args):
            if self.fail:
                raise RuntimeError("S3 is down")
            self.calls.append((name, args))
            return "✅ Published successfully."

    class Gateway:
        def __init__(self):
            self.calls = []

        async def call(self, name, args, ctx, internal):
            self.calls.append((name, args))
            return ToolOutcome("saved")

    store, apps, gateway = MemoryStore(), Apps(), Gateway()
    files = FileTools(store)
    files.gateway = gateway
    tools = PlanTools(store, files=files, apps=apps, apps_folder="ada")
    ctx = CallContext("chat-a", "aad-alex", [], actor_name="Alex Owner")
    made = json.loads(await tools.call_tool("make_plan", {"plan": SAMPLE}, ctx))
    assert "published" not in made  # nothing is published until asked
    out = json.loads(await tools.call_tool("publish_plan", {"plan_id": made["plan_id"], "site": "3.0 Projects",
                                                            "folder_path": "AU01/Plans"}, ctx))
    app = app_name(SAMPLE["name"])
    assert out["url"] == f"https://apps.example.com/ada/{app}/" and out["download"].endswith("/plan.xml")
    assert [c[1]["files"][0]["path"] for c in apps.calls] == ["index.html", "plan.xml", "overview.png"]  # one file per call
    assert all(n == "apps_publish" and a["agent"] == "ada" and a["app_name"] == app for n, a in apps.calls)
    sent = {f["path"]: base64.b64decode(f["content_base64"]) for _, a in apps.calls for f in a["files"]}
    assert set(sent) == {"index.html", "plan.xml", "overview.png"} and b'href="plan.xml" download' in sent["index.html"]
    assert b"schemas.microsoft.com/project" in sent["plan.xml"]
    put_name, put = gateway.calls[0]
    assert put_name == "openclaw__sharepoint_put" and put["site"] == "3.0 Projects" and put["folder_path"] == "AU01/Plans"
    assert put["filename"].endswith(".url") and base64.b64decode(put["content_base64"]).decode() == (
        f"[InternetShortcut]\r\nURL={out['url']}\r\n")
    # revising a published plan refreshes the same address, even if it's renamed
    renamed = {**SAMPLE, "name": "Workstream A (rev 2)"}
    revised = json.loads(await tools.call_tool("make_plan", {"plan": renamed, "plan_id": made["plan_id"]}, ctx))
    assert revised["published"] == out["url"] and apps.calls[2][1]["app_name"] == app
    apps.fail = True
    stale = json.loads(await tools.call_tool("make_plan", {"plan": renamed, "plan_id": made["plan_id"]}, ctx))
    assert "wasn't refreshed" in stale["publish_error"] and stale["finish"]
    assert "publishing failed" in json.loads(await tools.call_tool("publish_plan", {"plan_id": made["plan_id"]}, ctx))["error"]
    assert "no such plan" in json.loads(await tools.call_tool("publish_plan", {"plan_id": "nope"}, ctx))["error"]


def test_app_names_are_valid_apps_slugs():
    from agentcore.plan.tools import app_name
    assert app_name("Workstream A - Site Make Good!") == "plan-workstream-a-site-make-good"
    assert app_name("###") == "plan-project"
    long = app_name("x" * 200)
    assert len(long) <= 64 and re.fullmatch(r"[a-z0-9_-]{1,64}", long)


NARRATIVE = {
    "summary": "Make good the AU01 site after the dismantling works.", "budget": "A$180k",
    "goals": ["Site handed back clean"], "success_measures": ["Landlord sign-off with no defects"],
    "scope_in": ["Top deck", "Ground floor"], "scope_out": ["Roof"],
    "stakeholders": [{"name": "Alex Owner", "role": "Approver"}],
    "risks": [{"risk": "Crane availability", "likelihood": "Medium", "impact": "High", "owner": "Sam",
               "mitigation": "Book early"}],
    "communications": [{"what": "Status", "audience": "Steering", "when": "Weekly", "how": "Teams"}]}


def _nodes(node):
    yield node
    for child in node.get("content", []) if isinstance(node, dict) else []:
        yield from _nodes(child)


def test_confluence_page_follows_the_atlassian_plan_layout_and_is_valid_adf():
    from agentcore.plan.confluence import CRIT, MAX_COLUMNS, confluence_page
    jsonschema = pytest.importorskip("jsonschema")
    s = schedule(from_spec({**SAMPLE, **NARRATIVE}))
    doc = confluence_page(s, {**SAMPLE, **NARRATIVE}, "https://apps.example.com/ada/plan-x/")
    jsonschema.Draft4Validator(json.loads((FIX / "adf-schema-v1.json").read_text())).validate(doc)
    headings = [n["content"][0]["text"] for n in _nodes(doc) if n.get("type") == "heading"]
    assert headings == ["Overview", "Goals and success measures", "Scope", "Timeline", "Milestones", "Work breakdown",
                        "Team and resources", "Decisions and stakeholders (DACI)", "Risks and dependencies",
                        "Communications"]
    nodes = list(_nodes(doc))
    assert all(n.get("text") != "" for n in nodes if n.get("type") == "text")  # Confluence rejects empty text
    links = {m["attrs"]["href"] for n in nodes for m in n.get("marks", []) if m["type"] == "link"}
    assert links == {"https://apps.example.com/ada/plan-x/", "https://apps.example.com/ada/plan-x/plan.xml"}
    at = next(i for i, n in enumerate(doc["content"]) if n.get("type") == "heading" and n["content"][0]["text"] == "Timeline")
    gantt = doc["content"][at + 1]
    assert len(gantt["content"][0]["content"]) <= MAX_COLUMNS + 1
    assert any(c.get("attrs", {}).get("background") == CRIT for r in gantt["content"] for c in r["content"])
    assert any(n.get("type") == "inlineCard" and "PRJ-" in n["attrs"]["url"] for n in nodes)  # Jira smart links
    assert {n["attrs"]["text"] for n in nodes if n.get("type") == "status"} & {"ON TRACK", "CRITICAL", "AT RISK"}
    # a bare plan (no narrative) still makes a valid page
    jsonschema.Draft4Validator(json.loads((FIX / "adf-schema-v1.json").read_text())).validate(
        confluence_page(schedule(from_spec(SAMPLE)), SAMPLE))


async def test_plan_to_confluence_creates_once_then_replaces_with_a_yes(cfg):
    from agentcore.plan.tools import PlanTools
    from agentcore.store import MemoryStore
    from agentcore.tools import CallContext, ToolOutcome

    class Gateway:
        def __init__(self):
            self.calls = []

        async def call(self, name, args, ctx, internal):
            self.calls.append((name, args))
            if name == "confluence__update_page":
                return ToolOutcome("editing a page needs a yes", status="needs_confirm")
            return ToolOutcome(json.dumps({"id": "123", "url": "https://acme.atlassian.net/wiki/x/123"}))

    store, gateway = MemoryStore(), Gateway()
    tools = PlanTools(store)
    tools.gateway = gateway
    ctx = CallContext("chat-a", "aad-alex", [], actor_name="Alex Owner")
    made = json.loads(await tools.call_tool("make_plan", {"plan": {**SAMPLE, **NARRATIVE}}, ctx))
    need = json.loads(await tools.call_tool("plan_to_confluence", {"plan_id": made["plan_id"]}, ctx))
    assert "space_id" in need["error"]
    first = json.loads(await tools.call_tool("plan_to_confluence", {"plan_id": made["plan_id"], "space_id": "98",
                                                                    "parent_id": "77"}, ctx))
    assert first["created"] and first["page"].endswith("/123") and "publish_plan" in first["tip"]
    name, args = gateway.calls[0]
    assert name == "confluence__create_page" and args["space_id"] == "98" and args["parent_id"] == "77"
    assert args["title"].endswith("- project plan") and args["body"]["type"] == "doc"
    again = json.loads(await tools.call_tool("plan_to_confluence", {"plan_id": made["plan_id"]}, ctx))
    assert again["status"] == "needs_confirm" and again["page"].endswith("/123")
    tool, pending = await store.pop_pending("chat-a")
    assert tool == "confluence__update_page" and pending["page_id"] == "123" and pending["mode"] == "replace"


def test_contingency_tasks_need_no_owner():
    spec = {"name": "x", "start": "2026-10-19", "resources": [{"id": "a"}],
            "tasks": [{"id": "t1", "name": "Work", "duration": 2, "resources": ["a"]},
                      {"id": "t2", "name": "Stage 1 contingency", "duration": 2, "depends_on": ["t1"]},
                      {"id": "t3", "name": "Unowned work", "duration": 1, "depends_on": ["t2"]},
                      {"id": "m", "name": "Done", "milestone": True, "depends_on": ["t3"]}]}
    owners = [f["task"] for f in check(schedule(from_spec(spec))) if f["check"] == "no owner"]
    assert owners == ["t3"]


async def test_plan_to_jira_previews_then_sets_dates_parents_links_and_rank(cfg):
    from agentcore.plan.tools import PlanTools
    from agentcore.store import MemoryStore
    from agentcore.tools import CallContext, ToolOutcome

    spec = {"name": "J", "start": "2026-10-19", "resources": [{"id": "a"}],
            "tasks": [{"id": "s1", "name": "Stage 1", "jira": "PRJ-1"},
                      {"id": "t1", "name": "Isolate", "parent": "s1", "duration": 2, "resources": ["a"], "jira": "PRJ-2"},
                      {"id": "t2", "name": "Pack", "parent": "s1", "duration": 3, "resources": ["a"], "jira": "PRJ-3",
                       "depends_on": ["t1"]},
                      {"id": "t3", "name": "Paperwork", "parent": "s1", "duration": 1, "resources": ["a"], "jira": "PRJ-4",
                       "depends_on": [{"id": "t2", "type": "SS"}]},
                      {"id": "m1", "name": "Stage done", "parent": "s1", "milestone": True, "depends_on": ["t3"], "jira": "PRJ-5"},
                      {"id": "t9", "name": "Not in Jira", "duration": 1, "depends_on": ["m1"]}]}
    # Jira today: PRJ-3 already has the right dates and parent; rank is reversed for two of them; 5 doesn't exist
    jira = {"PRJ-1": {"start": None, "due": None, "parent": None, "blocks": []},
            "PRJ-3": {"start": "2026-10-21", "due": "2026-10-26", "parent": "PRJ-1", "blocks": []},  # Thu 22 Oct: Show Day
            "PRJ-2": {"start": "2026-10-19", "due": None, "parent": None, "blocks": []},
            "PRJ-4": {"start": None, "due": None, "parent": "PRJ-1", "blocks": []}}

    class Gateway:
        def __init__(self):
            self.calls = []

        async def call(self, name, args, ctx, internal):
            self.calls.append((name, args))
            if name == "jira__timeline_audit":
                items = [{"key": k, **v} for k, v in jira.items() if k in args["keys"]]
                return ToolOutcome(json.dumps({"items": items, "summary": {"error": 0, "warning": 0, "info": 0},
                                               "findings": []}))
            return ToolOutcome(json.dumps({"ok": True}))

    store, gateway = MemoryStore(), Gateway()
    tools = PlanTools(store)
    tools.gateway = gateway
    ctx = CallContext("chat-j", "aad-alex", [], actor_name="Alex Owner")
    made = json.loads(await tools.call_tool("make_plan", {"plan": spec}, ctx))
    preview = json.loads(await tools.call_tool("plan_to_jira", {"plan_id": made["plan_id"]}, ctx))
    assert preview["missing_in_jira"] == ["PRJ-5"] and "apply=true" in preview["next"]
    assert [c[0] for c in gateway.calls] == ["jira__timeline_audit"]  # a preview writes nothing
    assert any("SS" in n for n in preview["not_drawn"])
    applied = json.loads(await tools.call_tool("plan_to_jira", {"plan_id": made["plan_id"], "apply": True}, ctx))
    assert applied["failed"] == [] and applied["applied"] == preview["changes"]
    writes = [c for c in gateway.calls if c[0] != "jira__timeline_audit"]
    edits = {a["key"]: a for n, a in writes if n == "jira__update_issue"}
    assert edits["PRJ-1"] == {"key": "PRJ-1", "start_date": "2026-10-19", "due_date": "2026-10-27"}
    assert edits["PRJ-2"] == {"key": "PRJ-2", "due_date": "2026-10-20", "parent": "PRJ-1"}
    assert "PRJ-3" not in edits  # already right
    assert edits["PRJ-4"]["start_date"] == "2026-10-27"  # levelled: the same person finishes Pack first
    links = [a for n, a in writes if n == "jira__link_issues"]
    assert links == [{"blocker": "PRJ-2", "blocked": "PRJ-3"}]  # FS only; the SS link isn't drawn by Jira
    ranks = [a for n, a in writes if n == "jira__rank_issues"]
    assert ranks == [{"keys": ["PRJ-1", "PRJ-2", "PRJ-3", "PRJ-4"]}]


async def test_confirm_when_arg_asks_only_when_the_argument_is_set(cfg):
    from agentcore.config import ToolRule
    from agentcore.tools import CallContext, ToolGateway
    from .conftest import make_agent  # noqa: F401  (cfg fixture builds the gateway's config)
    rule = ToolRule(server="plan", name="plan_to_jira", writes=True, confirm_when_arg="apply")
    gw = ToolGateway.__new__(ToolGateway)
    gw.rules, gw.cfg = {"plan__plan_to_jira": rule}, cfg
    ctx = CallContext("c", "u", [])
    assert gw.needs_confirm("plan__plan_to_jira", {"plan_id": "x"}, ctx) is None
    assert "yes" in gw.needs_confirm("plan__plan_to_jira", {"plan_id": "x", "apply": True}, ctx)


async def test_every_plan_keeps_its_ms_project_xml_current_in_sharepoint(cfg):
    """A. Owner, 12 Oct 2026: Ada always creates and maintains the MS Project XML version of a plan."""
    import base64

    from agentcore.files import FileTools
    from agentcore.plan.tools import PlanTools
    from agentcore.store import MemoryStore
    from agentcore.tools import CallContext, ToolOutcome

    class Gateway:
        def __init__(self):
            self.calls, self.modified = [], None

        async def call(self, name, args, ctx, internal):
            self.calls.append((name, args, ctx.confirmed))
            if name == "openclaw__sharepoint_get":
                return ToolOutcome(f"**name:** x.xml\n**last_modified:** {self.modified}\n" if self.modified else "none")
            return ToolOutcome('{"webUrl": "https://sp/x.xml"}')

    store, gateway = MemoryStore(), Gateway()
    files = FileTools(store)
    tools = PlanTools(store, files=files)
    tools.gateway = gateway
    ctx = CallContext("chat-a", "aad-alex", [], actor_name="Alex Owner")

    # no home yet: the XML is still made, and Ada is told to give it a folder
    made = json.loads(await tools.call_tool("make_plan", {"plan": SAMPLE}, ctx))
    xml = made["msproject_xml"]
    data, name, _ = await files.resolve(xml["file"], "chat-a")
    assert name.endswith(".xml") and b"schemas.microsoft.com/project" in data and "folder_path" in xml["sharepoint"]
    assert gateway.calls == []

    # first save into the project folder: a new file, never replacing silently
    plan_id = made["plan_id"]
    first = json.loads(await tools.call_tool("make_plan", {"plan": SAMPLE, "plan_id": plan_id, "site": "3.0 Projects",
                                                           "folder_path": "AU01/Plans"}, ctx))
    assert first["msproject_xml"]["sharepoint"]["saved"] is True
    name_, put, confirmed = gateway.calls[-1]
    assert name_ == "openclaw__sharepoint_put" and "overwrite" not in put and not confirmed
    assert put["folder_path"] == "AU01/Plans" and put["filename"].endswith(".xml")
    assert b"schemas.microsoft.com/project" in base64.b64decode(put["content_base64"])

    # a revision replaces Ada's own copy without asking (it is checked for outside edits first)
    from datetime import datetime, timezone
    gateway.modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    revised = {**SAMPLE, "tasks": SAMPLE["tasks"] + [{"id": "zz", "name": "Extra check", "duration": 2}]}
    again = json.loads(await tools.call_tool("make_plan", {"plan": revised, "plan_id": plan_id}, ctx))
    assert again["msproject_xml"]["sharepoint"]["saved"] is True
    get, put, confirmed = gateway.calls[-2], gateway.calls[-1][1], gateway.calls[-1][2]
    assert get[0] == "openclaw__sharepoint_get" and get[1]["path"].startswith("AU01/Plans/")
    assert put["overwrite"] is True and confirmed and b"Extra check" in base64.b64decode(put["content_base64"])

    # someone edits it in MS Project later: Ada leaves it alone until a yes
    home = await store.get_pref(f"plan:{plan_id}", "xml")
    await store.set_pref(f"plan:{plan_id}", "xml", {**home, "saved_at": home["saved_at"] - 3600})
    before = len(gateway.calls)
    held = json.loads(await tools.call_tool("make_plan", {"plan": revised, "plan_id": plan_id}, ctx))
    assert held["msproject_xml"]["sharepoint"]["saved"] is False and "import_project_xml" in held["msproject_xml"]["sharepoint"]["reason"]
    assert [c[0] for c in gateway.calls[before:]] == ["openclaw__sharepoint_get"]
    forced = json.loads(await tools.call_tool("make_plan", {"plan": revised, "plan_id": plan_id, "replace_edited": True}, ctx))
    assert forced["msproject_xml"]["sharepoint"]["saved"] is True and gateway.calls[-1][1]["overwrite"] is True


def test_replacing_an_edited_xml_needs_a_yes(cfg):
    from agentcore.config import load_agent_config
    from agentcore.skills import SkillLoader
    from agentcore.tools import CallContext, ToolGateway
    from .conftest import AGENTS, SKILLS
    gw = ToolGateway(load_agent_config(AGENTS / "ada" / "agent.yaml"), SkillLoader(SKILLS, "ada", [], []))
    ctx = CallContext("c", "aad-alex", ["update the plan"], actor_name="Alex Owner")
    assert gw.needs_confirm("plan__make_plan", {"plan": {}, "plan_id": "x"}, ctx) is None
    assert "yes" in gw.needs_confirm("plan__make_plan", {"plan": {}, "plan_id": "x", "replace_edited": True}, ctx)


def test_viewer_indents_tasks_under_their_stage():
    """A. Owner, 12 Oct 2026: the summary's expand arrow ate the indent, so tasks lined up with their stage."""
    page = html_viewer(schedule(from_spec(SAMPLE))).decode()
    assert "(t.level - 1) * 22" in page and '<span class="car"></span>' in page  # leaves keep the arrow's width
    assert 'class="l${t.level}' in page


def test_viewer_draws_dependency_lines_by_default_with_link_types():
    """A. Owner, 12 Oct 2026: lines between dependencies, MS Project style."""
    tasks = [{"id": "a", "name": "A", "duration": 3}, {"id": "b", "name": "B", "duration": 2, "depends_on": ["a"]},
             {"id": "c", "name": "C", "duration": 2, "depends_on": [{"id": "a", "type": "SS"}]}]
    data = plan_data(plan(tasks))
    c = next(t for t in data["tasks"] if t["id"] == "c")
    assert c["pred_links"] == [{"id": "a", "type": "SS"}]
    page = html_viewer(plan(tasks)).decode()
    assert "deps: true" in page and 'id="deps" class="on"' in page and 'class="dep"' in page
    assert ":root[data-theme=dark]{" in page and "--dep:" in page
