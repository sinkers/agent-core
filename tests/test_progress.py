"""Spoken progress while Ada works on a live call (progress.py, voice_llm.py)."""
import asyncio
import json

from agentcore import voice_llm
from agentcore.progress import Narrator, describe, found
from agentcore.voice_llm import stream_turn

from .conftest import FakeMcp, make_agent
from .test_voice_llm import collect, parse


def test_tool_calls_are_described_in_plain_words():
    assert describe("jira__search_issues", {"jql": "project = PRJ AND status != Done"}) == "searching Jira in PRJ"
    assert describe("jira__get_issue", {"key": "PRJ-479"}) == "reading PRJ-479"
    assert describe("openclaw__search_docs", {"query": "PTU lead time"}) == "searching SharePoint for PTU lead time"
    assert describe("something__odd_tool", {}) == "working on odd tool"
    assert found("jira__search_issues", json.dumps({"issues": [1, 2, 3]})) == "3 found"
    assert found("openclaw__search_docs", "Found 6 relevant chunks from Acme SharePoint") == "6 documents found"


async def test_the_narrator_says_what_he_is_doing_and_what_he_found():
    n = Narrator("What's overdue on PRJ?")
    await n.add({"type": "tool", "name": "jira__search_issues", "args": {"jql": "project = PRJ"}})
    await n.add({"type": "result", "name": "jira__search_issues", "status": "ok",
                 "content": json.dumps({"issues": [{"key": "PRJ-1"}] * 12})})
    await n.add({"type": "tool", "name": "jira__get_issue", "args": {"key": "PRJ-479"}})
    seen = {}

    async def write(prompt):
        seen["prompt"] = prompt
        return "I've got twelve open PRJ tickets and I'm reading PRJ-479 now."
    line = await n.next_line(write)
    assert line == "I've got twelve open PRJ tickets and I'm reading PRJ-479 now"
    assert "What's overdue on PRJ?" in seen["prompt"] and "PRJ-479" in seen["prompt"] and "12" not in line
    # no model (or it fails): a template naming the step and what came back
    assert await n.next_line(None) == "12 found; now reading PRJ-479"
    t = Narrator("x")
    await t.add({"type": "tool", "name": "web__web_search", "args": {"query": "Delta PTU"}})
    await t.add({"type": "result", "name": "web__web_search", "status": "ok", "content": json.dumps({"results": [1, 2]})})
    await t.add({"type": "tool", "name": "web__read_web_page", "args": {}})
    assert await t.next_line(None) == "2 found; now reading a web page"
    # unverified callers hear only the step, never what a tool returned, and no model is asked
    u = Narrator("x", verified=False)
    await u.add({"type": "result", "name": "jira__search_issues", "status": "ok", "content": "secret stuff"})
    await u.add({"type": "tool", "name": "jira__get_issue", "args": {"key": "PRJ-1"}})

    async def never(prompt):
        raise AssertionError("no model for unverified callers")
    assert "secret" not in await u.next_line(never)


async def test_the_agent_reports_notes_tool_calls_and_results(cfg):
    mcp = FakeMcp(["search_docs"])
    agent, model, *_ = make_agent(cfg, [[("openclaw__search_docs", {"query": "PTU"})], "Delta quotes 36 weeks."], mcp=mcp)
    events = []

    async def on_progress(e):
        events.append(e)
    from agentcore.agent import Turn
    await agent.handle(Turn("test-progress-1", "PTU lead time?", "aad-1", "Alex"), on_progress=on_progress)
    kinds = [(e["type"], e.get("name")) for e in events]
    assert ("tool", "openclaw__search_docs") in kinds and ("result", "openclaw__search_docs") in kinds


async def test_a_slow_answer_gets_specific_updates_with_no_cap_of_three(cfg, monkeypatch):
    agent, model, *_ = make_agent(cfg, ["Twelve are overdue."])
    monkeypatch.setattr(voice_llm, "UPDATE_EVERY_S", 0.02)
    lines = iter(f"Update {c} about PRJ" for c in "abcdefghijklmnopqrst")

    async def progress_line(prompt):
        return next(lines)
    agent.progress_line = progress_line
    real = agent.handle

    async def slow_handle(turn, on_ack=None, on_progress=None):
        await on_progress({"type": "tool", "name": "jira__search_issues", "args": {"jql": "project = PRJ"}})
        await asyncio.sleep(0.2)  # about ten update intervals
        return await real(turn, on_ack=on_ack, on_progress=on_progress)
    agent.handle = slow_handle
    body = {"messages": [{"role": "user", "content": "What's overdue on PRJ?"}],
            "elevenlabs_extra_body": {"call_id": "pg1", "kind": "p2p", "caller_aad_id": "aad-1"}}
    pieces, done = parse(await collect(stream_turn(agent, body)))
    updates = [p for p in pieces if p.startswith("Update")]
    assert len(updates) > 3 and all(p.endswith("... ") for p in updates) and done
    assert pieces[-1] == "Twelve are overdue." and len(set(updates)) == len(updates)


async def test_an_update_may_only_quote_numbers_that_came_back():
    n = Narrator("lead times?")
    await n.add({"type": "result", "name": "openclaw__sharepoint_get", "status": "ok", "content": "UPS ~14 wk; transformers ~128 wk"})
    await n.add({"type": "tool", "name": "jira__get_issue", "args": {"key": "PRJ-349"}})
    assert n.checked("UPS 14 weeks and transformers 128 weeks; now reading PRJ-349")
    assert not n.checked("PTUs 36 weeks; now reading PRJ-349")  # 36 never came back
    assert not n.checked("PRJ-349 shows 14 weeks")  # that ticket hasn't been read yet
    assert n.checked("Checking the PRJ board now")  # project codes aren't numbers

    async def bad(prompt):
        return "PRJ-349 shows the PTU at 36 weeks"
    assert "36" not in await n.next_line(bad)
