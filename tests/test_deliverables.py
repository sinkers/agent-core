"""Deliverables by phase (plan/deliverables.py): method x plan tags x project register."""

import io
import json
from datetime import date

from openpyxl import Workbook

from agentcore.plan.deliverables import build, parse_register, propose_tags, status_of
from agentcore.plan.model import from_spec
from agentcore.plan.render import html_viewer
from agentcore.plan.schedule import schedule

METHOD = {"version": {"cite_as": "Acme method draft"}, "items": [
    {"id": "D1.02", "name": "Project brief", "stage": 1, "stage_name": "St1 Preparation", "gate": "G1",
     "producer": "Project manager", "approver": "Board at G1", "url": "https://m/d1-02/"},
    {"id": "D1.04", "name": "Procurement strategy", "stage": 1, "stage_name": "St1 Preparation", "gate": "G1",
     "producer": "Commercial lead", "approver": "Board", "url": "https://m/d1-04/"},
    {"id": "D2.02", "name": "Basis of design", "stage": 2, "stage_name": "St2 Concept", "gate": "G2",
     "producer": "Design lead", "approver": "Design authority", "url": "https://m/d2-02/"},
    {"id": "D3.01", "name": "Coordinated schematic set", "stage": 3, "stage_name": "St3", "gate": "G3",
     "producer": "Design lead", "approver": "Design authority", "url": ""}]}


def plan():
    return {"name": "P", "start": "2026-11-02", "status_date": "2026-11-20", "resources": [{"id": "pm", "name": "Pat PM"}],
            "tasks": [{"id": "s1", "name": "St1"},
                      {"id": "brief", "name": "Write the project brief", "parent": "s1", "duration": 5,
                       "resources": ["pm"], "deliverable": "D1.02"},
                      {"id": "proc", "name": "Procurement strategy and RACI", "parent": "s1", "duration": 5,
                       "depends_on": ["brief"]},
                      {"id": "g1", "name": "G1 Project brief and baseline", "parent": "s1", "milestone": True,
                       "depends_on": ["proc"]},
                      {"id": "s2", "name": "St2", "depends_on": ["g1"]},
                      {"id": "bod", "name": "Basis of design", "parent": "s2", "duration": 20, "deliverable": ["d2.2"]},
                      {"id": "g2", "name": "G2 Preliminary investment", "parent": "s2", "milestone": True,
                       "depends_on": ["bod"]}]}


def register_bytes():
    wb = Workbook()
    ws = wb.active
    ws.title = "Register"
    ws.append(["PRJ-1 George Town — project document register"])
    ws.append(["PRJ-GEN-REG-0005 Rev C"])
    ws.append([])
    ws.append([])
    ws.append(["Document number", "Description", "Type", "Workstream", "Class", "Req", "Ref", "Rev / version", "Status",
               "Walkthrough", "Comment sheet", "Revision story", "Approval", "Native", "Working folder",
               "Approved folder", "Updated by", "Updated", "Stage", "Method deliverable", "Concept"])
    ws.append(["PRJ-GEN-RPT-0014", "Project brief", "RPT", "GEN", "3", "", "", "B", "Approved", "", "", "", "env 1",
               "Word", "0 Pre-Design & Cleanup", "", "", "", "St1", "D1.02", ""])
    ws.append(["PRJ-GEN-BOD-0001", "Basis of design", "BOD", "GEN", "2", "", "", "C", "In review", "", "", "", "",
               "Word", "01 Concept Design/Concept A", "", "", "", "St2", "", "A"])
    d = wb.create_sheet("Deliverables")
    d.append(["PRJ-1 deliverables"])
    d.append([])
    d.append([])
    d.append(["Method ID", "Deliverable", "Stage", "Gate", "Producer", "Approver", "Produced", "Document number",
              "Status (from Register)", "Other document numbers"])
    d.append(["D2.02", "Basis of design", "St2", "G2", "", "", "", "PRJ-GEN-BOD-0001", "=formula", ""])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


CFG = {"site": "3.0 Projects", "library_root": "1. Project Delivery/PRJ-1"}


def test_register_is_read_from_both_tabs_with_folder_links():
    r = parse_register(register_bytes(), CFG)
    assert r["by_deliverable"] == {"D1.02": ["PRJ-GEN-RPT-0014"], "D2.02": ["PRJ-GEN-BOD-0001"]}
    assert r["docs"]["PRJ-GEN-BOD-0001"]["url"].endswith("PRJ-1/01%20Concept%20Design/Concept%20A")


def test_deliverables_join_method_plan_and_register():
    s = schedule(from_spec(plan()))
    d = build(s, METHOD, parse_register(register_bytes(), CFG), today=date(2026, 11, 20))
    assert [g["gate"] for g in d["stages"]] == ["G1", "G2"]  # only the plan's stages (D3.01 has no gate here)
    st1 = {r["id"]: r for r in d["stages"][0]["rows"]}
    assert st1["D1.02"]["status"] == "Approved" and st1["D1.02"]["done"] and st1["D1.02"]["tasks"][0]["id"] == "brief"
    assert st1["D1.02"]["owners"] == "Pat PM" and st1["D1.02"]["documents"][0]["rev"] == "B"
    assert st1["D1.04"]["status"] == "Not planned" and st1["D1.04"]["late"]  # G1 has passed without it
    bod = d["stages"][1]["rows"][0]
    assert bod["status"] == "In review" and bod["tasks"][0]["id"] == "bod" and not bod["done"]
    assert d["gates"]["G1"] == {"approved": 1, "total": 2, "late": 1, "at_risk": 0}
    msgs = " ".join(f["message"] for f in d["findings"])
    assert "G1 on" in msgs and "D1.04 Procurement strategy" in msgs


def test_status_is_the_least_advanced_live_document():
    assert status_of(["Approved", "In review"]) == "In review"
    assert status_of(["Approved", "Superseded"]) == "Approved"
    assert status_of([]) == ""


def test_proposals_match_task_names_within_the_gates_stage_and_skip_milestones():
    s = schedule(from_spec(plan()))
    props = {p["deliverable"]: p for p in propose_tags(s, METHOD)}
    assert "D1.02" not in props  # already tagged
    assert props["D1.04"]["candidates"][0]["task"] == "proc"
    assert all(c["task"] != "g1" for p in props.values() for c in p["candidates"])


def test_viewer_has_the_deliverables_tab_and_gate_evidence():
    s = schedule(from_spec(plan()))
    d = build(s, METHOD, parse_register(register_bytes(), CFG), today=date(2026, 11, 20))
    html = html_viewer(s, deliverables=d).decode()
    assert 'data-tab="d"' in html and "function deliverablesTab" in html and "PRJ-GEN-BOD-0001" in html
    assert "Evidence:" in html


def test_tasks_take_deliverable_ids_in_any_spelling():
    p = from_spec(plan())
    assert p.tasks["bod"].deliverable == ["D2.02"] and p.tasks["brief"].deliverable == ["D1.02"]
