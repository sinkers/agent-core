"""Usage reporting: every model call records agent, person, channel, trigger and session."""

from datetime import datetime, timedelta, timezone

from agentcore.agent import Agent, Turn

from .conftest import FakeMcp, make_agent


async def test_every_model_call_says_who_where_and_which_session(cfg):
    mcp = FakeMcp(["search_docs"])
    agent, model, store, tools = make_agent(cfg, [[("openclaw__search_docs", {"q": "x"})], "found it"], mcp)
    await tools.discover()
    await agent.handle(Turn("a:chat-1", "find it", "aad-erin", "Erin Hall", source="teams_1to1"))
    assert len(store.usage) == 2
    for u in store.usage:
        assert (u.agent, u.conversation_id, u.channel, u.user_id, u.user_name, u.trigger) == (
            agent.name, "a:chat-1", "teams_1to1", "aad-erin", "Erin Hall", "person")
    assert store.usage[0].session_id and store.usage[0].session_id == store.usage[1].session_id


async def test_sessions_continue_within_30_minutes_then_start_again(cfg):
    agent, model, store, _ = make_agent(cfg, ["one", "two", "three"])
    await agent.handle(Turn("mail:t1", "a", "u1", "U"))
    await agent.handle(Turn("mail:t1", "b", "u1", "U"))
    first, second = store.usage[0].session_id, store.usage[1].session_id
    assert first == second
    saved = await store.get_pref("mail:t1", "usage_session")
    await store.set_pref("mail:t1", "usage_session",
                         {**saved, "last": (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()})
    await agent.handle(Turn("mail:t1", "c", "u1", "U"))
    assert store.usage[2].session_id != first and store.usage[2].channel == "email"


def test_channels_from_the_caller_or_the_conversation():
    ch = Agent.channel_of
    assert ch(Turn("19:x@thread.v2", "hi", source="teams_group")) == "teams_group"
    assert ch(Turn("mail:abc", "hi")) == "email" and ch(Turn("jira:PRJ-1", "hi")) == "jira"
    assert ch(Turn("task:7", "hi")) == "scheduled" and ch(Turn("summary:c", "hi")) == "summary"
    assert ch(Turn("call:1", "hi", caller_number="+614")) == "phone"
    assert ch(Turn("call:1", "hi", scope="shared")) == "meeting" and ch(Turn("call:1", "hi")) == "teams_call"
    assert ch(Turn("a:x", "hi")) == "teams_1to1" and ch(Turn("test-1", "hi")) == "internal"


async def test_scheduled_and_agent_triggers_are_recorded(cfg):
    agent, model, store, _ = make_agent(cfg, ["done", "ok"])
    await agent.handle(Turn("task:9", "weekly report", "u1", "U", trigger="schedule"))
    await agent.handle(Turn("jira:PRJ-1", "from eve", "jira:eve", "Eve (AI)", channel="jira", from_agent=True))
    assert [u.trigger for u in store.usage] == ["schedule", "agent"]
    assert [u.channel for u in store.usage] == ["scheduled", "jira"]


async def test_prometheus_counters_carry_channel_and_trigger(cfg):
    from prometheus_client import REGISTRY
    agent, model, store, _ = make_agent(cfg, ["hello"])
    await agent.handle(Turn("19:g@thread.v2", "hi", "u1", "U", scope="shared", source="teams_group"))
    labels = {"agent": agent.name, "outcome": "replied", "channel": "teams_group", "trigger": "person"}
    total = sum(s.value for metric in REGISTRY.collect() if metric.name == "agent_turns"
                for s in metric.samples if s.name == "agent_turns_total" and all(s.labels.get(k) == v for k, v in labels.items()))
    assert total >= 1
