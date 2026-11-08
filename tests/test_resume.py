import asyncio
from datetime import datetime, timedelta, timezone

from agentcore.agent import Turn
from agentcore.resume import resume_interrupted

from .conftest import FakeMcp, make_agent

NOW = datetime(2026, 10, 5, 23, 0, tzinfo=timezone.utc)


async def test_a_chat_turn_is_marked_while_it_runs_and_cleared_after(cfg):
    agent, model, store, _ = make_agent(cfg, ["done"])
    seen = {}

    async def peek(*a, **k):
        seen["during"] = await store.get_pref("chat-a", "in_flight")
        return await original(*a, **k)
    original = agent._handle_traced
    agent._handle_traced = peek
    await agent.handle(Turn("chat-a", "build it in Jira", "aad-alex", "Alex Owner"))
    assert seen["during"]["text"] == "build it in Jira" and seen["during"]["attempt"] == 0
    assert await store.get_pref("chat-a", "in_flight") is None and agent.active == set()


async def test_email_voice_and_summary_turns_are_not_marked(cfg):
    agent, _, store, _ = make_agent(cfg, ["a", "b"])
    await agent.handle(Turn("mail:x", "hi", "u", channel="email"))
    await agent.handle(Turn("call:y", "hi", "u", channel="voice"))
    assert await store.prefs_with_key("in_flight") == []


async def test_a_cut_off_request_is_resumed_with_what_was_already_done(cfg):
    mcp = FakeMcp(["search_docs"])
    agent, model, store, tools = make_agent(cfg, ["I was restarted and have picked it up again. All built."], mcp)
    await tools.discover()
    started = NOW - timedelta(minutes=20)
    await store.set_pref("chat-a", "in_flight", {"text": "build it in Jira", "user_id": "aad-alex",
                                                 "user_name": "Alex Owner", "started": started.isoformat(),
                                                 "attempt": 0, "pod": "agent-ada-old"})
    await store.audit({"actor": "aad-alex", "action": "tool_call", "tool": "jira__create_issue",
                       "args": {"summary": "Stage 1 top deck"}, "detail": {"status": "ok"}})
    await store.audit({"actor": "aad-alex", "action": "tool_call", "tool": "jira__search_issues",  # a read: not listed
                       "args": {"jql": "x"}, "detail": {"status": "ok"}})
    store.audit_log[-1]["at"] = store.audit_log[-2]["at"] = (started + timedelta(minutes=1)).isoformat()
    sent = []

    async def send(conv, text):
        sent.append((conv, text))
    out = await resume_interrupted(agent, store, send, now=NOW)
    assert out == ["chat-a: resumed"]
    prompt = str(model.calls[-1]["messages"])
    assert "build it in Jira" in prompt and "restarted part-way" in prompt and "jira__create_issue" in prompt
    assert "jira__search_issues" not in prompt
    assert sent and "picked it up again" in sent[0][1]
    assert await store.get_pref("chat-a", "in_flight") is None  # finished this time


async def test_old_or_already_resumed_requests_get_an_apology_not_a_rerun(cfg):
    agent, model, store, _ = make_agent(cfg, [])
    await store.set_pref("chat-old", "in_flight", {"text": "do the thing", "user_id": "u", "user_name": "Erin Hall",
                                                   "started": (NOW - timedelta(hours=5)).isoformat(), "attempt": 0})
    await store.set_pref("chat-again", "in_flight", {"text": "do it", "user_id": "u", "user_name": "Sam Taylor",
                                                     "started": (NOW - timedelta(minutes=5)).isoformat(), "attempt": 1})
    sent = []

    async def send(conv, text):
        sent.append((conv, text))
    out = await resume_interrupted(agent, store, send, now=NOW)
    assert sorted(out) == ["chat-again: apologised", "chat-old: apologised"] and model.calls == []
    assert any("Sorry Erin" in t and "send it to me again" in t for _, t in sent)


async def test_a_turn_still_running_in_this_pod_is_left_alone(cfg, monkeypatch):
    monkeypatch.setenv("HOSTNAME", "agent-ada-new")
    agent, model, store, _ = make_agent(cfg, [])
    await store.set_pref("chat-a", "in_flight", {"text": "x", "user_id": "u", "started": NOW.isoformat(), "pod": "agent-ada-new"})
    assert await resume_interrupted(agent, store, lambda c, t: None, now=NOW) == []
    assert await store.get_pref("chat-a", "in_flight") is not None


async def test_drain_waits_for_turns_in_progress(cfg):
    agent, _, _, _ = make_agent(cfg, [])
    agent.active.add("chat-a")

    async def finish():
        await asyncio.sleep(0.6)
        agent.active.discard("chat-a")
    asyncio.create_task(finish())
    assert await agent.drain(5) == 0
    agent.active.add("chat-b")
    assert await agent.drain(0.6) == 1
