"""The sales fact register: drafts, queries on anything ambiguous, approval by the configured approvers only."""

import json

from agentcore.facts import FactTools, numbers_in
from agentcore.store import MemoryStore
from agentcore.tools import CallContext

OWNER = {"name": "A. Owner", "email": "alex.owner@acme.example", "aad_id": "aad-alex",
          "jira_account_id": "712020:alex"}
SALES = CallContext("chat-1", "aad-sam", [], actor_name="Sam Sales")
APPROVER = CallContext("chat-2", "aad-alex", [], actor_name="Alex Owner")


async def call(tools, name, args, ctx=SALES):
    return json.loads(await tools.call_tool(name, args, ctx))


async def test_a_fact_is_drafted_then_approved_only_by_an_approver():
    tools = FactTools(MemoryStore(), [OWNER])
    made = await call(tools, "propose_fact", {"statement": "10 MW site energised in 14-18 months from contract",
                                              "area": "10 MW build", "value": "14-18", "unit": "months",
                                              "conditions": "connection offer in hand", "source": "Acme-PRJ-REQ-001",
                                              "source_rev": "E"})
    fid = made["fact"]["id"]
    assert fid == "F-0001" and made["fact"]["status"] == "draft" and made["approvers"] == "A. Owner"
    refused = await call(tools, "approve_fact", {"fact_id": fid})
    assert refused["refused"] and "A. Owner" in refused["error"]
    ok = await call(tools, "approve_fact", {"fact_id": fid}, APPROVER)
    assert ok["fact"]["status"] == "approved" and ok["fact"]["approved_by"] == "Alex Owner"
    # Jira identity counts too
    tools2 = FactTools(MemoryStore(), [OWNER])
    f2 = (await call(tools2, "propose_fact", {"statement": "PUE 1.2", "area": "GPU hall", "source": "x"}))["fact"]["id"]
    via_jira = await call(tools2, "approve_fact", {"fact_id": f2}, CallContext("jira:PRJ-1", "jira:712020:alex", []))
    assert via_jira["fact"]["status"] == "approved"


async def test_approvers_can_be_limited_to_areas():
    eng = {"name": "E. Engineer", "aad_id": "aad-eng", "areas": ["GPU hall"]}
    tools = FactTools(MemoryStore(), [OWNER, eng])
    f = (await call(tools, "propose_fact", {"statement": "x", "area": "10 MW build", "source": "s"}))["fact"]["id"]
    assert (await call(tools, "approve_fact", {"fact_id": f}, CallContext("c", "aad-eng", [])))["refused"]
    g = (await call(tools, "propose_fact", {"statement": "y", "area": "GPU hall", "source": "s"}))["fact"]["id"]
    assert (await call(tools, "approve_fact", {"fact_id": g}, CallContext("c", "aad-eng", [])))["fact"]["status"] == "approved"


async def test_a_query_holds_the_fact_back_until_the_approver_resolves_it():
    tools = FactTools(MemoryStore(), [OWNER])
    f = (await call(tools, "propose_fact", {"statement": "Delivery 14 months", "area": "10 MW build",
                                            "value": "14", "unit": "months", "source": "deck v3"}))["fact"]["id"]
    await call(tools, "approve_fact", {"fact_id": f}, APPROVER)
    q = await call(tools, "raise_query", {"kind": "conflict", "fact_id": f, "summary": "REQ-001 Rev E says 18 months",
                                          "sources": ["deck v3 slide 4", "Acme-PRJ-REQ-001 Rev E 5.2"]})
    assert q["query"]["id"] == "Q-0001" and "A. Owner" in q["next"]
    assert (await call(tools, "find_facts", {"status": "queried"}))["count"] == 1
    assert (await call(tools, "approve_fact", {"fact_id": f}, APPROVER))["error"].startswith("this fact has an open query")
    assert (await call(tools, "list_queries", {}))["count"] == 1
    assert (await call(tools, "resolve_query", {"query_id": "Q-0001", "resolution": "18 is right"}))["refused"]
    done = await call(tools, "resolve_query", {"query_id": "Q-0001", "resolution": "18 is right", "fact_status": "superseded"},
                      APPROVER)
    assert done["fact"]["status"] == "superseded" and (await call(tools, "list_queries", {}))["count"] == 0
    assert (await call(tools, "raise_query", {"kind": "ambiguous", "summary": "x", "sources": []}))["error"]


async def test_superseding_keeps_history():
    tools = FactTools(MemoryStore(), [OWNER])
    old = (await call(tools, "propose_fact", {"statement": "14 months", "area": "build", "source": "Rev D"}))["fact"]["id"]
    new = await call(tools, "propose_fact", {"statement": "18 months", "area": "build", "source": "Rev E", "supersedes": old})
    assert "superseded" in new["note"]
    olds = (await call(tools, "find_facts", {"status": "superseded"}))["facts"]
    assert olds[0]["id"] == old and "superseded by F-0002" in olds[0]["history"][0]["event"]


async def test_check_claims_finds_numbers_and_ids_without_an_approved_fact():
    tools = FactTools(MemoryStore(), [OWNER])
    f = (await call(tools, "propose_fact", {"statement": "10 MW site energised in 14-18 months", "area": "build",
                                            "value": "14-18", "unit": "months", "source": "REQ-001 E"}))["fact"]["id"]
    d = (await call(tools, "propose_fact", {"statement": "PUE 1.15", "area": "build", "value": "1.15", "source": "x"}))["fact"]["id"]
    await call(tools, "approve_fact", {"fact_id": f}, APPROVER)
    text = f"Your 10 MW site can be energised in 14–18 months [{f}] with a PUE of 1.15 [{d}] and 99.99% uptime in 2027."
    out = await call(tools, "check_claims", {"text": text})
    assert not out["ok"]
    assert "99.99%" in out["unsupported_numbers"] and "1.15" in out["unsupported_numbers"]
    assert not any(n.startswith("14") or n.startswith("10") for n in out["unsupported_numbers"])  # approved
    assert out["fact_ids_not_approved"] == [{"fact_id": d, "status": "draft"}]
    assert (await call(tools, "check_claims", {"text": f"Energised in 14-18 months [{f}]."}))["ok"]


def test_numbers_skip_years_and_list_markers():
    assert numbers_in("1. In 2027 we deliver 2.5 MW for A$1.2m at 30%") == ["2.5 MW", "A$1.2m", "30%"]



async def test_currency_amounts_match_their_approved_fact():
    tools = FactTools(MemoryStore(), [OWNER])
    f = (await call(tools, "propose_fact", {"statement": "GPUaaS from US$2.10 per GPU-hour", "area": "GPUaaS",
                                            "value": "2.10", "unit": "US$/GPU-hour", "source": "M1 release 3"}))["fact"]["id"]
    await call(tools, "approve_fact", {"fact_id": f}, APPROVER)
    assert (await call(tools, "check_claims", {"text": f"Rates from US$2.10 per GPU-hour [{f}]."}))["ok"]
    assert "US$1.90" in (await call(tools, "check_claims", {"text": "Rates from US$1.90 per GPU-hour."}))["unsupported_numbers"]


async def test_sweeps_from_config_are_scheduled_once_and_replaced_when_changed():
    from datetime import datetime, timezone
    from agentcore.config import Sweep
    from agentcore.scheduler import ensure_sweeps
    store = MemoryStore()
    now = datetime(2026, 10, 7, 6, 0, tzinfo=timezone.utc)
    sweeps = [Sweep(name="technical", cron="0 7 * * 1-5", instruction="Run the technical sweep.")]
    assert await ensure_sweeps(store, sweeps, "aad-alex", "A. Owner", now) == ["technical"]
    assert await ensure_sweeps(store, sweeps, "aad-alex", "A. Owner", now) == []  # already there
    [task] = await store.list_scheduled(None)
    assert task["instruction"].startswith("[sweep:technical]") and task["created_by"] == "aad-alex"
    assert task["run_at"].isoformat() == "2026-10-07T20:00:00+00:00"  # 07:00 Thursday Hobart (AEDT)
    changed = [Sweep(name="technical", cron="30 7 * * 1-5", instruction="Run the technical sweep.")]
    assert await ensure_sweeps(store, changed, "aad-alex", "A. Owner", now) == ["technical"]
    active = await store.list_scheduled(None)
    assert len(active) == 1 and active[0]["cron"] == "30 7 * * 1-5"
    assert await ensure_sweeps(store, sweeps, None, None, now) == []  # no owner id: nothing scheduled
