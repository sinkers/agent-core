"""Programmes: sub-plans in a master plan, edits without rewriting the spec, and the one-page overview."""

import base64
import io
import json

import pytest

from agentcore.files import FileTools
from agentcore.plan.tools import PlanTools
from agentcore.store import MemoryStore
from agentcore.tools import CallContext


class Apps:
    def __init__(self):
        self.calls = []

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        return "Published."


def civil():
    return {"name": "Civil works", "start": "2026-11-02",
            "resources": [{"id": "sam", "name": "Sam Taylor"}],
            "tasks": [{"id": "s1", "name": "Earthworks"},
                      {"id": "dig", "name": "Excavate pad", "parent": "s1", "duration": 5, "resources": ["sam"]},
                      {"id": "pour", "name": "Pour slab", "parent": "s1", "duration": 3, "depends_on": ["dig"]},
                      {"id": "m1", "name": "Pad ready", "milestone": True, "depends_on": ["pour"], "key": True}]}


def electrical():
    return {"name": "Electrical", "start": "2026-10-19",
            "resources": [{"id": "sam", "name": "Sam Taylor"}],
            "tasks": [{"id": "order", "name": "Order transformer", "duration": 2, "resources": ["sam"]},
                      {"id": "install", "name": "Install transformer", "duration": 4, "depends_on": ["order"]},
                      {"id": "m2", "name": "Energised", "milestone": True, "depends_on": ["install"], "key": True}]}


def setup():
    store, apps = MemoryStore(), Apps()
    tools = PlanTools(store, files=FileTools(store), apps=apps, apps_folder="ada")
    return tools, store, apps, CallContext("chat-a", "aad-alex", [], actor_name="Alex Owner")


async def call(tools, ctx, name, args):
    return json.loads(await tools.call_tool(name, args, ctx))


async def master_of(tools, ctx):
    c = await call(tools, ctx, "make_plan", {"plan": civil()})
    e = await call(tools, ctx, "make_plan", {"plan": electrical()})
    master = {"name": "George Town programme", "start": "2026-10-19",
              "tasks": [{"id": "m0", "name": "Notice to proceed", "milestone": True, "key": True},
                        {"id": "civ", "subplan": c["plan_id"], "depends_on": ["m0"]},
                        # the transformer can't go in until the pad is ready: a link into the civil sub-plan
                        {"id": "elec", "subplan": e["plan_id"], "depends_on": ["civ/m1"]},
                        {"id": "handover", "name": "Handover", "milestone": True, "depends_on": ["elec/m2"], "key": True}]}
    m = await call(tools, ctx, "make_plan", {"plan": master})
    return c, e, m


async def test_a_master_plan_pulls_in_its_sub_plans_and_links_across_them():
    tools, store, apps, ctx = setup()
    c, e, m = await master_of(tools, ctx)
    assert "error" not in m, m
    assert [s["plan_id"] for s in m["subplans"]] == [c["plan_id"], e["plan_id"]]
    assert m["tasks"] == 8  # m0, handover + dig, pour, m1 (civil) + order, install, m2 (electrical)
    # civil starts no earlier than its own start (Mon 2 Nov): dig 2–6 Nov, pour 9–11 Nov; electrical waits for the pad
    names = {x["name"]: x["date"] for x in m["milestones"]}
    assert names["Pad ready"] == "2026-11-11"
    assert names["Energised"] == "2026-11-19" and names["Handover"] == names["Energised"]


async def test_levelling_sees_one_person_across_sub_plans():
    tools, store, apps, ctx = setup()
    a, b = civil(), electrical()
    a["start"] = b["start"] = "2026-10-19"
    a["tasks"][2]["resources"] = ["sam"]
    pa = await call(tools, ctx, "make_plan", {"plan": a})
    pb = await call(tools, ctx, "make_plan", {"plan": b})
    m = await call(tools, ctx, "make_plan", {"plan": {"name": "Both", "start": "2026-10-19", "tasks": [
        {"id": "a", "subplan": pa["plan_id"]}, {"id": "b", "subplan": pb["plan_id"]}]}})
    # alone, each finishes early; together Sam can't do both at once, so levelling pushes one out
    assert m["still_over_allocated"] == [] and m["over_allocated_before_levelling"]
    assert m["finish"] > max(pa["finish"], pb["finish"])


async def test_changing_a_sub_plan_refreshes_the_published_master():
    tools, store, apps, ctx = setup()
    c, e, m = await master_of(tools, ctx)
    await call(tools, ctx, "publish_plan", {"plan_id": c["plan_id"]})
    pub = await call(tools, ctx, "publish_plan", {"plan_id": m["plan_id"]})
    html = next(base64.b64decode(a["files"][0]["content_base64"]) for _, a in reversed(apps.calls)
                if a["app_name"] == pub["url"].rstrip("/").rsplit("/", 1)[-1] and a["files"][0]["path"] == "index.html")
    assert b"plan-civil-works" in html  # the master's viewer links to the civil sub-plan's own viewer
    assert any(a["files"][0]["path"] == "overview.png" for _, a in apps.calls)  # and carries the overview image
    before = len(apps.calls)
    edited = await call(tools, ctx, "edit_plan", {"plan_id": c["plan_id"],
                                                  "upsert_tasks": [{"id": "dig", "duration": 10}]})
    assert edited["masters"][0]["plan_id"] == m["plan_id"] and edited["masters"][0]["finish"] > m["finish"]
    assert any(a["app_name"].startswith("plan-george-town") for _, a in apps.calls[before:])


async def test_edit_plan_changes_only_what_it_is_given():
    tools, store, apps, ctx = setup()
    c = await call(tools, ctx, "make_plan", {"plan": civil()})
    out = await call(tools, ctx, "edit_plan", {
        "plan_id": c["plan_id"], "set": {"name": "Civil works rev B"},
        "upsert_tasks": [{"id": "cure", "name": "Cure slab", "parent": "s1", "duration": 7, "depends_on": ["pour"],
                          "after": "pour"},
                         {"id": "m1", "depends_on": ["cure"]}],
        "remove_tasks": []})
    assert out["name"] == "Civil works rev B" and "added cure" in out["changes"]
    spec = (await call(tools, ctx, "get_plan", {"plan_id": c["plan_id"]}))["spec"]
    assert [t["id"] for t in spec["tasks"]] == ["s1", "dig", "pour", "cure", "m1"]
    assert spec["tasks"][1]["resources"] == ["sam"]  # untouched fields kept
    removed = await call(tools, ctx, "edit_plan", {"plan_id": c["plan_id"], "remove_tasks": ["cure"]})
    assert "m1: dropped its link(s) to removed tasks" in removed["changes"]
    branch = await call(tools, ctx, "get_plan", {"plan_id": c["plan_id"], "under": "s1"})
    assert {t["id"] for t in branch["spec"]["tasks"]} == {"s1", "dig", "pour"}


async def test_removing_a_summary_removes_its_tasks_and_links_to_them():
    tools, store, apps, ctx = setup()
    c = await call(tools, ctx, "make_plan", {"plan": civil()})
    out = await call(tools, ctx, "edit_plan", {"plan_id": c["plan_id"], "remove_tasks": ["s1"],
                                              "upsert_tasks": [{"id": "m1", "depends_on": None}]})
    assert "removed 3 task(s)" in out["changes"]


@pytest.mark.parametrize("bad,message", [
    ({"id": "x", "subplan": "nope"}, "no plan 'nope'"),
])
async def test_bad_sub_plans_get_a_clear_reason(bad, message):
    tools, store, apps, ctx = setup()
    out = await call(tools, ctx, "make_plan", {"plan": {"name": "M", "start": "2026-10-19", "tasks": [bad]}})
    assert message in out["error"]


async def test_a_plan_cannot_include_itself_or_its_master():
    tools, store, apps, ctx = setup()
    c, e, m = await master_of(tools, ctx)
    loop = civil()
    loop["tasks"].append({"id": "back", "subplan": m["plan_id"]})
    out = await call(tools, ctx, "make_plan", {"plan": loop, "plan_id": c["plan_id"]})
    assert "includes itself" in out["error"]


async def test_sub_plan_links_must_stay_inside_it():
    tools, store, apps, ctx = setup()
    bad = electrical()
    bad["tasks"][0]["depends_on"] = ["civ/m1"]  # a link to another plan belongs in the master
    e = await call(tools, ctx, "make_plan", {"plan": bad})
    assert "isn't a task in the plan" in e["error"]


async def test_list_plans_shows_sub_plans_and_masters():
    tools, store, apps, ctx = setup()
    c, e, m = await master_of(tools, ctx)
    listed = {p["name"]: p for p in (await call(tools, ctx, "list_plans", {}))["plans"]}
    assert listed["George Town programme"]["subplans"] == [c["plan_id"], e["plan_id"]]
    assert listed["Civil works"]["included_in"] == [m["plan_id"]]
    assert [p["name"] for p in (await call(tools, ctx, "list_plans", {"query": "civil"}))["plans"]] == ["Civil works"]


async def test_overview_shows_workstreams_with_durations_and_key_milestones():
    from pptx import Presentation
    tools, store, apps, ctx = setup()
    c, e, m = await master_of(tools, ctx)
    out = await call(tools, ctx, "plan_overview", {"plan_id": m["plan_id"], "formats": ["pptx", "png", "svg"]})
    assert [w["name"] for w in out["workstreams"]] == ["Civil works", "Electrical"]
    assert all("wk" in w["span"] for w in out["workstreams"])
    assert [x["name"] for x in out["milestones"]] == ["Notice to proceed", "Pad ready", "Energised", "Handover"]
    assert out["milestones"][1]["workstream"] == "Civil works"
    data, name, _ = await tools.files.resolve(out["files"]["pptx"]["file"], "chat-a")
    prs = Presentation(io.BytesIO(data))
    slide = prs.slides[1]  # right after the title slide
    texts = [sh.text_frame.text for sh in slide.shapes if sh.has_text_frame]
    assert "Civil works" in texts and any("Pad ready" in t for t in texts)
    from pptx.enum.shapes import MSO_SHAPE, MSO_SHAPE_TYPE
    shapes = [sh for sh in slide.shapes if sh.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE]
    assert sum(1 for sh in shapes if sh.auto_shape_type == MSO_SHAPE.DIAMOND) == 4  # editable shapes, not a picture
    assert sum(1 for sh in shapes if sh.auto_shape_type == MSO_SHAPE.ROUNDED_RECTANGLE) == 2
    png, _, _ = await tools.files.resolve(out["files"]["png"]["file"], "chat-a")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


async def test_overview_without_key_milestones_falls_back_to_the_top_ones():
    from agentcore.plan.model import from_spec
    from agentcore.plan.overview import overview_data
    from agentcore.plan.schedule import schedule
    spec = civil()
    spec["tasks"][-1].pop("key")
    data = overview_data(schedule(from_spec(spec)))
    assert [w["name"] for w in data["workstreams"]] == ["Earthworks"]
    assert [x["name"] for x in data["milestones"]] == ["Pad ready"]
    assert data["ticks"][0]["label"] == "Nov 26"


def test_only_https_links_are_kept_on_tasks():
    from agentcore.plan.model import from_spec
    spec = civil()
    spec["tasks"][1]["url"] = "javascript:alert(1)"
    spec["tasks"][2]["url"] = "https://apps.example.com/ada/plan-x/"
    plan = from_spec(spec)
    assert plan.tasks["dig"].url is None and plan.tasks["pour"].url.startswith("https://")


# ---- gates (16 Oct 2026, A. Owner: gates are milestones but a distinct part of the process) ----

def staged():
    return {"name": "Staged", "start": "2026-11-02", "tasks": [
        {"id": "s1", "name": "St1 Briefing"},
        {"id": "a", "name": "Brief", "parent": "s1", "duration": 5},
        {"id": "g1", "name": "G1 Project brief and baseline (Board)", "parent": "s1", "milestone": True, "depends_on": ["a"]},
        {"id": "late", "name": "Consultant RFQ", "parent": "s1", "duration": 10, "depends_on": ["g1"]},
        {"id": "s2", "name": "St2 Concept", "depends_on": ["g1"]},
        {"id": "b", "name": "Concept design", "parent": "s2", "duration": 10},
        {"id": "fz", "name": "Design freeze (IFC)", "parent": "s2", "milestone": True, "depends_on": ["b"]},
        {"id": "m", "name": "Survey done", "parent": "s2", "milestone": True, "depends_on": ["b"]},
        {"id": "g2", "name": "Gate two", "gate": "G2", "parent": "s2", "milestone": True, "depends_on": ["fz"]}]}


def test_gates_and_gateways_are_recognised():
    from agentcore.plan.model import from_spec
    plan = from_spec(staged())
    assert plan.tasks["g1"].gate == "G1" and plan.tasks["g2"].gate == "G2"  # by name, and by field
    assert plan.tasks["fz"].gateway and not plan.tasks["fz"].gate
    assert not plan.tasks["m"].gate and not plan.tasks["m"].gateway


def test_work_after_its_stages_gate_is_flagged():
    from agentcore.plan.health import gate_findings
    from agentcore.plan.model import from_spec
    from agentcore.plan.schedule import schedule
    found = gate_findings(schedule(from_spec(staged())))
    assert len(found) == 1 and "St1 Briefing: 1 task(s) finish after G1" in found[0]["message"]
    assert "Consultant RFQ" in found[0]["message"] and "mid-stage gateway" in found[0]["message"]


def test_overview_always_shows_every_gate_and_labels_never_overlap():
    from agentcore.plan.model import from_spec
    from agentcore.plan.overview import MAX_MILESTONES, _frac, _label, layout, overview_data
    from agentcore.plan.schedule import schedule
    spec = staged()
    # more milestones than the cap, all in one stage on the same few days: the gates must still all show
    spec["tasks"] += [{"id": f"x{i}", "name": f"Milestone number {i}", "parent": "s2", "milestone": True,
                       "key": True, "depends_on": ["b"]} for i in range(MAX_MILESTONES + 4)]
    d = overview_data(schedule(from_spec(spec)))
    assert [g["code"] for g in d["gates"]] == ["G1", "G2"]
    assert any(m["kind"] == "gateway" for m in d["milestones"])
    L = layout(d, char_frac=0.006)
    by_lane: dict = {}
    for i, m in enumerate(d["milestones"]):
        ln = L["milestone_lane"].get(i)
        if ln is None:
            continue
        w = (max(len(x) for x in _label(m)) + 2) * 0.006
        x = L["milestone_x"][i]
        for (a, b) in by_lane.get((m["row"], ln), []):
            assert x + w / 2 <= a or x - w / 2 >= b  # no two labels in a lane overlap
        by_lane.setdefault((m["row"], ln), []).append((x - w / 2, x + w / 2))


def test_viewer_has_the_overview_tab_with_gates():
    from agentcore.plan.model import from_spec
    from agentcore.plan.render import html_viewer, plan_data
    from agentcore.plan.schedule import schedule
    s = schedule(from_spec(staged()))
    data = plan_data(s)
    assert data["overview"]["1"]["gates"][0]["code"] == "G1"
    html = html_viewer(s).decode()
    assert 'data-tab="o" class="on">Overview' in html and "function drill(" in html and 'class="gb"' in html
