import json
from datetime import datetime, timedelta, timezone

import pytest

from agentcore.scheduler import ScheduleTools, TaskRunner, next_cron
from agentcore.store import MemoryStore
from agentcore.tools import CallContext

from .conftest import make_agent


async def make_tools():
    store = MemoryStore()
    await store.upsert_conversation_ref("c1", {"user": {"name": "Alex Owner"}, "conversation": {"id": "c1"}}, "aad-alex")
    return ScheduleTools(store), store


async def call(tools, name, args, user="aad-alex"):
    return json.loads(await tools.call_tool(name, args, CallContext("c1", user)))


async def test_one_off_and_weekday_repeat():
    tools, store = await make_tools()
    at = (datetime.now(timezone.utc) + timedelta(days=1)).astimezone().replace(tzinfo=None).isoformat(timespec="minutes")
    one = await call(tools, "schedule_task", {"instruction": "Remind me to send the PRJ brief", "at": at})
    assert one["scheduled"] and one["repeat"] == "once" and one["deliver_to"] == "Alex Owner"
    rep = await call(tools, "schedule_task", {"instruction": "Sweep the TODAY board", "repeat": "30 8 * * 1-5"})
    assert rep["scheduled"] and rep["repeat"] == "30 8 * * 1-5" and rep["next_run_local"].endswith("08:30")
    listed = await call(tools, "list_scheduled_tasks", {})
    assert len(listed["tasks"]) == 2


@pytest.mark.parametrize("args,reason", [
    ({"instruction": "x", "repeat": "*/5 * * * *"}, "15 minutes"),
    ({"instruction": "x", "repeat": "not cron"}, "cron"),
    ({"instruction": "x", "at": "2020-01-01T09:00"}, "past"),
    ({"instruction": "x"}, "time"),
])
async def test_rejects_bad_schedules(args, reason):
    tools, _ = await make_tools()
    res = await call(tools, "schedule_task", args)
    assert res["scheduled"] is False and reason in res["reason"]


async def test_only_creator_can_cancel():
    tools, _ = await make_tools()
    rep = await call(tools, "schedule_task", {"instruction": "x", "repeat": "0 9 * * *"})
    assert (await call(tools, "cancel_scheduled_task", {"task_id": rep["id"]}, user="aad-erin"))["cancelled"] is False
    assert (await call(tools, "cancel_scheduled_task", {"task_id": rep["id"]}))["cancelled"] is True


async def test_runner_runs_delivers_and_reschedules(cfg):
    agent, model, store, _ = make_agent(cfg, ["Board swept: 3 items need attention."])
    tools = ScheduleTools(store)
    await store.upsert_conversation_ref("c1", {"user": {"name": "Alex Owner"}, "conversation": {"id": "c1"}}, "aad-alex")
    rep = json.loads(await tools.call_tool("schedule_task", {"instruction": "Sweep the TODAY board", "repeat": "30 8 * * 1-5"},
                                           CallContext("c1", "aad-alex")))
    delivered = []

    async def deliver(uid, text, via="teams", task=None):
        delivered.append((uid, text))
        return True
    runner = TaskRunner(agent=agent, store=store, deliver=deliver)
    due_time = store.sched[0]["run_at"] + timedelta(seconds=1)
    assert await runner.tick(now=due_time) == 1
    assert delivered[0][0] == "aad-alex" and "Board swept" in delivered[0][1]
    assert "Scheduled task #1" in model.calls[0]["messages"][-1]["content"][0]["text"]
    t = store.sched[0]
    assert t["status"] == "active" and t["run_count"] == 1 and t["run_at"] == next_cron("30 8 * * 1-5", due_time)
    assert await runner.tick(now=due_time) == 0  # not due again until the next weekday


async def test_runner_waits_while_killswitch_engaged(cfg):
    agent, model, store, _ = make_agent(cfg, ["x"], killswitch_state="stop")
    await store.create_scheduled({"instruction": "x", "created_by": "u", "deliver_to": "u",
                                  "run_at": datetime.now(timezone.utc) - timedelta(minutes=1), "cron": None})
    assert await TaskRunner(agent=agent, store=store, deliver=None).tick() == 0
    assert store.sched[0]["status"] == "active" and model.calls == []
