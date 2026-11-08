"""The front door and its workers (docs/plans/scale-to-zero.md): the work API, the queue, launching and stopping
workers, retries, the kill switch, schedules.

The tests that need Postgres run when TEST_DATABASE_URL is set (any empty database; they create and drop the
`frontdoor` and `agent_*` schemas), e.g.
  docker run -d --name s2z-test-pg -e POSTGRES_PASSWORD=test -e POSTGRES_DB=agents -p 127.0.0.1:55432:5432 postgres:17-alpine
  TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:55432/agents pytest tests/test_frontdoor.py
Workers run in-process (an in-memory launcher) on a scripted model, talking to the front door's real HTTP API.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from agentcore.remote import FrontDoorClient, RemoteStore
from agentcore.store import Message, Usage
from agentcore.testing import FakeModel
from agentcore.tools import StreamableHttpMcpClient, ToolGateway
from agentcore.workapi import STORE_METHODS, decode, encode

from .conftest import AGENTS, SKILLS

DSN = os.environ.get("TEST_DATABASE_URL")
needs_pg = pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")


# --- no database ------------------------------------------------------------------------------------------
def test_codec_round_trip():
    at = datetime(2026, 11, 3, 9, 30, tzinfo=timezone.utc)
    value = {"m": Message("c1", "user", "hi", "u1", "Uma", at), "u": Usage("m", "default", 10, 2, cost_usd=0.1),
             "when": at, "pair": ("a", at), "rows": [{"run_at": at}]}
    back = decode(encode(value))
    assert back == value and isinstance(back["m"], Message) and back["pair"] == ("a", at)


def test_store_methods_exclude_schedule_claims_and_pool():
    assert "claim_due" not in STORE_METHODS and "migrate" not in STORE_METHODS and "save_message" in STORE_METHODS
    store = RemoteStore(client=None)  # type: ignore[arg-type]
    with pytest.raises(AttributeError):
        store.claim_due
    with pytest.raises(AttributeError):
        store._exec


async def test_cached_tool_lists_mean_no_server_is_contacted(cfg):
    from agentcore.skills import SkillLoader

    class Unreachable(StreamableHttpMcpClient):
        async def list_tools(self):
            raise AssertionError("contacted")

    class Cache:
        async def get(self, server):
            return [{"name": "search", "description": "Search", "inputSchema": {"type": "object"}}]

        async def put(self, server, tools):
            raise AssertionError("not expected")

    rule = next(r for r in cfg.tools.allow)
    tools = ToolGateway(cfg, SkillLoader(SKILLS, cfg.agent.name, [], []), {rule.server: Unreachable("http://x")})
    cfg.tools.allow[0].name = "search"
    tools.rules = {r.qualified: r for r in cfg.tools.allow}
    await tools.discover(cache=Cache())
    assert f"{rule.server}__search" in tools.specs


# --- with Postgres ----------------------------------------------------------------------------------------
class InProcessLauncher:
    """Workers as asyncio tasks in this process, on a scripted model, reaching the front door over HTTP."""

    def __init__(self, script, agents_dir: Path, *, fail_start: bool = False, crash_on: str | None = None,
                 never_ready: bool = False):
        self.script, self.agents_dir = script, agents_dir
        self.fail_start, self.crash_on, self.never_ready = fail_start, crash_on, never_ready
        self.tasks: dict[str, asyncio.Task] = {}
        self.stopped: list[str] = []
        self.started = 0
        self.app = None

    async def start(self, agent, entry, worker_id, env):
        if self.fail_start:
            raise RuntimeError("image pull failed")
        self.started += 1
        if self.never_ready:  # created, then exits before ever asking for work (a bad image, a crash at start)
            async def dies():
                raise RuntimeError("worker crashed at start")
            self.tasks[worker_id] = asyncio.create_task(dies())
            return worker_id
        self.tasks[worker_id] = asyncio.create_task(self._run(agent, env))
        return worker_id

    async def _run(self, agent, env):
        from agentcore.killswitch import KillSwitch
        from agentcore.worker import Worker, build_agent
        holder = {}
        client = FrontDoorClient("http://fd", env["WORKER_TOKEN"], transport=httpx.ASGITransport(app=self.app),
                                 on_state=lambda s: holder["ks"].apply(
                                     {holder["ks"].agent: "stop" if s.get("stopped") else "run"})
                                 if "ks" in holder else None)
        if env.get("WORKER_POOL"):  # pooled: wait to be given an agent
            got = None
            while got is None:
                got = await client.assign(wait=5)
            agent, env = got["agent"], {**env, **got["env"]}
        ks = holder["ks"] = KillSwitch(agent)
        ks.override = "run"
        await client.heartbeat()
        built = await build_agent(self.agents_dir / agent, RemoteStore(client), ks, model=FakeModel(self.script))
        built.cfg.models.triage = False  # the first responder would take a scripted reply
        w = Worker(client, built, idle_seconds=float(env["IDLE_SECONDS"]))
        if self.crash_on:
            real = w._run_item

            async def crashing(item):
                if item["payload"].get("text") == self.crash_on:
                    raise asyncio.CancelledError  # the worker dies mid-item: no result, no goodbye
                return await real(item)
            w._run_item = crashing
        reason = await w.run()
        await client.bye(reason)

    async def stop(self, handle):
        self.stopped.append(handle)
        t = self.tasks.pop(handle, None)
        if t:
            t.cancel()

    async def list(self):
        return {h: ("running" if not t.done() else "failed" if t.cancelled() or t.exception() else "completed")
                for h, t in self.tasks.items()}


@pytest.fixture
async def door(tmp_path, monkeypatch):
    if not DSN:
        pytest.skip("TEST_DATABASE_URL not set")
    import psycopg
    from agentcore.frontdoor.app import create_app
    from agentcore.frontdoor.config import FrontDoorConfig
    from agentcore.frontdoor.core import FrontDoor
    from agentcore.frontdoor.db import FrontDoorDB

    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as c:
        await c.execute("DROP SCHEMA IF EXISTS frontdoor CASCADE; DROP SCHEMA IF EXISTS agent_ada CASCADE")
    monkeypatch.setenv("KILLSWITCH_OVERRIDE", "run")
    monkeypatch.setenv("CLIENT_API_TOKEN", "client-t")
    monkeypatch.setenv("ADMIN_API_TOKEN", "admin-t")
    agents = tmp_path / "agents"
    shutil.copytree(AGENTS / "ada", agents / "ada")
    monkeypatch.setenv("SKILLS_DIR", str(SKILLS))

    async def make(script=("Hello from the worker.",), *, idle=30.0, pool: int = 0, **launcher_kw):
        cfg = FrontDoorConfig.model_validate({
            "agents": {"ada": {"image": "unused", "idle_seconds": idle, "max_workers": 2,
                               "pool": "general" if pool else None}},
            "pools": {"general": {"size": pool, "image": "unused"}} if pool else {},
            "launcher": "local", "limits": {"lease_seconds": 3, "start_timeout_seconds": 5}})
        launcher = InProcessLauncher(list(script) * 20, agents, **launcher_kw)
        fd = FrontDoor(cfg, FrontDoorDB(DSN), launcher)
        await fd.start()
        app = create_app(fd)
        launcher.app = app
        api = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fd",
                                headers={"Authorization": "Bearer client-t"})
        made.append((fd, api))
        for _ in range(50):  # wait to be the leader
            if fd.is_leader:
                break
            await asyncio.sleep(0.1)
        return fd, api, launcher
    made: list = []
    yield make
    for fd, api in made:
        for t in getattr(fd.launcher, "tasks", {}).values():
            t.cancel()
        await api.aclose()
        await fd.stop()


async def wait_for(cond, timeout=15.0):
    for _ in range(int(timeout / 0.1)):
        if await cond():
            return True
        await asyncio.sleep(0.1)
    return False


async def chat(api, text, conv="c1"):
    r = await api.post("/v1/agents/ada/chat", json={"conversation_id": conv, "text": text, "user_id": "u1",
                                                    "user_name": "Uma"})
    r.raise_for_status()
    return r.json()


async def result(api, item_id, wait=15):
    r = await api.get(f"/v1/work/{item_id}", params={"wait": wait})
    return r.json()


@needs_pg
async def test_cold_message_gets_holding_line_then_answer_and_follow_up_is_warm(door):
    fd, api, launcher = await door(["First answer.", "Second answer."])
    first = await chat(api, "hi")
    assert first["holding"] == "Just a moment, I'm waking up." and not first["warm"]
    done = await result(api, first["item_id"])
    assert done["status"] == "done" and done["result"]["reply"].startswith("First answer.")
    second = await chat(api, "and again")
    assert second["warm"] and second["holding"] is None
    assert (await result(api, second["item_id"]))["result"]["reply"].startswith("Second answer.")
    assert launcher.started == 1  # the follow-up went to the same worker
    # the conversation is in the agent's own store, written through the work API
    msgs = await fd.stores["ada"].recent_messages("api:c1", 10)
    assert [m.role for m in msgs] == ["user", "assistant", "user", "assistant"]


@needs_pg
async def test_idle_worker_leaves_and_its_token_stops_working(door):
    fd, api, launcher = await door(idle=1.0)
    item = await chat(api, "hi")
    await result(api, item["item_id"])
    assert await wait_for(lambda: _no_workers(fd))
    gone = (await fd.db.q("SELECT * FROM frontdoor.workers"))[0]
    assert gone["state"] == "gone" and "idle" in gone["end_reason"]


async def _no_workers(fd):
    return not await fd.db.live_workers()


@needs_pg
async def test_worker_dying_mid_item_is_retried_then_dead_lettered(door):
    fd, api, launcher = await door(crash_on="boom")
    item = await chat(api, "boom")
    assert await wait_for(lambda: _status(fd, item["item_id"], "dead"), timeout=60)
    row = await fd.db.get_item(item["item_id"])
    assert row["attempts"] == 3 and launcher.started == 3


async def _status(fd, item_id, status):
    return (await fd.db.get_item(item_id))["status"] == status


@needs_pg
async def test_launcher_errors_back_off_but_keep_the_work(door):
    fd, api, launcher = await door(fail_start=True)
    item = await chat(api, "hi")
    await asyncio.sleep(8)  # several tries, backing off
    assert (await fd.db.get_item(item["item_id"]))["status"] == "queued"
    st = await fd.db.agent_state("ada")
    assert st["launch_failures"] == 0 and st["blocked_until"] is not None
    launcher.fail_start = False
    await fd.db.reset_agent("ada")
    done = await result(api, item["item_id"], wait=30)
    assert done["status"] == "done"


@needs_pg
async def test_workers_that_cannot_start_stop_being_launched_after_three(door):
    fd, api, launcher = await door(never_ready=True)
    item = await chat(api, "hi")
    assert await wait_for(lambda: _status(fd, item["item_id"], "dead"), timeout=90)
    st = await fd.db.agent_state("ada")
    assert st["launch_failures"] == 3 and st["blocked_until"] > datetime.now(timezone.utc) + timedelta(days=1)
    r = await api.post("/v1/admin/agents/ada/reset", headers={"Authorization": "Bearer admin-t"})
    assert r.status_code == 200 and (await fd.db.agent_state("ada"))["launch_failures"] == 0


@needs_pg
async def test_kill_switch_stops_running_workers_and_pauses_intake(door):
    fd, api, launcher = await door(idle=60)
    item = await chat(api, "hi")
    await result(api, item["item_id"])
    assert len(await fd.db.live_workers("ada")) == 1
    ks = fd.killswitches["ada"]
    ks.override = "stop"
    await ks.poll_once()
    assert await wait_for(lambda: _no_workers(fd))

    async def cleaned_up():  # its sandbox is removed, whether it left by itself or was stopped
        return bool(launcher.stopped)
    assert await wait_for(cleaned_up)
    paused = await chat(api, "are you there?")
    assert paused.get("paused") and paused["item_id"] is None


@needs_pg
async def test_worker_store_is_its_agents_only_and_allow_listed(door):
    fd, api, launcher = await door()
    worker_id, token = await fd.db.new_worker("ada")
    w = httpx.AsyncClient(transport=httpx.ASGITransport(app=launcher.app), base_url="http://fd",
                          headers={"Authorization": f"Bearer {token}"})
    r = await w.post("/v1/worker/store/claim_due", json={"args": [], "kwargs": {}})
    assert r.status_code == 403
    r = await w.post("/v1/worker/store/save_message", json={"args": encode([Message("c9", "user", "x")]), "kwargs": {}})
    assert r.status_code == 200
    bad = httpx.AsyncClient(transport=httpx.ASGITransport(app=launcher.app), base_url="http://fd",
                            headers={"Authorization": "Bearer forged"})
    assert (await bad.post("/v1/worker/store/recent_messages", json={"args": ["c9", 5]})).status_code == 401
    await fd.db.end_worker(worker_id, "test")
    assert (await w.post("/v1/worker/claim", json={"wait": 0})).status_code == 401


@needs_pg
async def test_due_schedule_runs_in_a_worker_and_moves_on(door):
    fd, api, launcher = await door(["Here is your reminder."])
    store = fd.stores["ada"]
    task_id = await store.create_scheduled({"instruction": "Remind Uma to stretch", "created_by": "u1",
                                            "created_by_name": "Uma", "deliver_to": "u1", "deliver_to_name": "Uma",
                                            "run_at": datetime.now(timezone.utc) - timedelta(seconds=1)})
    fd.cfg.limits.schedule_jitter_seconds = 0.5
    fd._wake.set()

    async def finished():
        rows = await store.list_scheduled(None)
        return not any(r["id"] == task_id for r in rows)
    assert await wait_for(finished, timeout=30)

    async def item_done():  # the task moves on before the worker posts its result
        rows = await fd.db.q("SELECT status FROM frontdoor.work_items WHERE kind='schedule'")
        return rows[0]["status"] == "done"
    assert await wait_for(item_done, timeout=30)
    item = (await fd.db.q("SELECT * FROM frontdoor.work_items WHERE kind='schedule'"))[0]
    assert item["result"]["deliveries"][0]["user_id"] == "u1"
    assert "Here is your reminder." in item["result"]["deliveries"][0]["text"]


@needs_pg
async def test_duplicate_webhook_is_queued_once(door):
    fd, api, launcher = await door()
    a = await fd.intake("ada", text="x", conversation_id="c", user_id="u", user_name="U",
                        reply_to={"channel": "api"}, source="t", dedupe_key="k1")
    b = await fd.intake("ada", text="x", conversation_id="c", user_id="u", user_name="U",
                        reply_to={"channel": "api"}, source="t", dedupe_key="k1")
    assert a["item_id"] and b.get("duplicate")


@needs_pg
async def test_pooled_worker_takes_the_first_message_and_the_pool_refills(door):
    fd, api, launcher = await door(["Pooled answer."], pool=1)

    async def pool_ready():
        return bool(await fd.db.q("SELECT 1 FROM frontdoor.workers WHERE agent='' AND state='pooled' "
                                  "AND first_seen_at IS NOT NULL"))
    assert await wait_for(pool_ready)
    first = await chat(api, "hi")
    assert not first["warm"] and first["holding"]  # no worker of its own yet: the holding line still goes out
    done = await result(api, first["item_id"])
    assert done["status"] == "done" and done["result"]["reply"].startswith("Pooled answer.")
    ada = await fd.db.live_workers("ada")
    assert len(ada) == 1 and ada[0]["pool"] == "general"  # the pooled worker, now Ada's
    assert await wait_for(pool_ready)  # and another one waits in the pool
    assert launcher.started == 2


@needs_pg
async def test_pooled_worker_can_do_nothing_before_it_has_an_agent(door):
    fd, api, launcher = await door()
    worker_id, token = await fd.db.new_worker("", pool="general")
    w = httpx.AsyncClient(transport=httpx.ASGITransport(app=launcher.app), base_url="http://fd",
                          headers={"Authorization": f"Bearer {token}"})
    for path, body in (("/v1/worker/claim", {"wait": 0}), ("/v1/worker/store/recent_messages", {"args": ["c", 5]}),
                       ("/v1/worker/result/1", {"result": {}}), ("/v1/worker/wake", {})):
        assert (await w.post(path, json=body)).status_code == 409, path
    r = await w.post("/v1/worker/assign", json={"wait": 0})
    assert r.status_code == 200 and r.json()["assignment"] is None


# --- voice calls through voxhelm ----------------------------------------------------------------------------
def test_spoken_answer_is_split_into_sentences_short_ones_joined():
    from agentcore.frontdoor.voxhelm import sentences
    assert sentences("Yes. The meeting is at 3 pm on Thursday in the big room. Bring the draft!") == [
        "Yes. The meeting is at 3 pm on Thursday in the big room.", "Bring the draft!"]
    assert sentences("") == []


class FakeSocket:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, raw):
        import json
        self.sent.append(json.loads(raw))

    async def close(self):
        pass

    def kinds(self):
        return [(m["type"], m["payload"].get("state") or m["payload"].get("text")) for m in self.sent]


def _call_link(fd):
    from agentcore.frontdoor.voxhelm import VoxhelmLink
    link = VoxhelmLink("ada", "wss://unused", ["u1"], fd, name="voxhelm")
    link.sockets["u1"] = sock = FakeSocket()
    fd.channels[("ada", "voxhelm")] = link
    return link, sock


@needs_pg
async def test_call_keeps_a_worker_up_and_answers_in_spoken_chunks(door):
    import json
    fd, api, launcher = await door(["The meeting is on Thursday at three in the boardroom. I will send the notes after it."], idle=1.0)
    link, sock = _call_link(fd)
    await link._on_message("u1", json.dumps({"type": "connect_to_room", "id": "m1",
                                             "payload": {"room_name": "r1", "session_id": "s1"}}))
    assert sock.kinds() == [("state_update", "listening")]
    # a worker comes up with nothing queued, and outlives its idle time while the call lasts
    assert await wait_for(lambda: _workers_ready(fd))
    await asyncio.sleep(3)
    assert await _workers_ready(fd) and launcher.started == 1

    await link._on_message("u1", json.dumps({"type": "transcript", "id": "m2", "payload": {"text": "When's the meeting?"}}))
    assert await wait_for(lambda: _said(sock, ("state_update", "listening"), after=1), timeout=25)
    said = [m for m in sock.sent if m["type"] == "llm_response"]
    assert [m["payload"]["stream_seq"] for m in said] == [0, 1] and [m["payload"]["done"] for m in said] == [False, True]
    assert said[0]["payload"]["text"].startswith("The meeting is on Thursday")
    assert [k for k in sock.kinds() if k[0] == "state_update"] == [
        ("state_update", s) for s in ("listening", "thinking", "speaking", "listening")]
    assert launcher.started == 1  # answered by the worker kept up for the call
    item = (await fd.db.q("SELECT payload FROM frontdoor.work_items"))[0]
    assert item["payload"]["channel"] == "voice"

    # the call is over (nothing said for the hold time): the worker idles out as usual
    await fd.db.q("UPDATE frontdoor.agent_state SET warm_until = now() - interval '1 second'")
    assert await wait_for(lambda: _no_workers(fd))


async def _workers_ready(fd):
    return any(w["agent"] == "ada" and w["state"] in ("idle", "busy") for w in await fd.db.live_workers())


async def _said(sock, kind, after=0):
    return sum(1 for k in sock.kinds() if k == kind) > after


async def test_answer_overtaken_by_a_newer_question_is_not_spoken():
    from agentcore.frontdoor.voxhelm import VoxhelmLink

    class Core:
        async def hold_warm(self, *a):
            pass
    link = VoxhelmLink("ada", "wss://unused", ["u1"], Core(), name="voxhelm")
    link.sockets["u1"] = sock = FakeSocket()
    link.calls["u1"] = {"said": 2}
    await link.send({"user": "u1", "voice": True, "said": 1}, "The old answer.")
    assert sock.sent == []
    await link.send({"user": "u1", "voice": True, "said": 2}, "The new answer.")
    assert [m["payload"].get("text") for m in sock.sent if m["type"] == "llm_response"] == ["The new answer."]


async def test_transcript_with_go_struct_field_names_is_taken():
    import json
    from agentcore.frontdoor.voxhelm import VoxhelmLink
    seen = []

    class Core:
        async def hold_warm(self, *a):
            pass

        async def intake(self, agent, **kw):
            seen.append(kw["text"])
    link = VoxhelmLink("ada", "wss://unused", ["u1"], Core(), name="voxhelm")
    link.sockets["u1"] = FakeSocket()
    await link._on_message("u1", json.dumps({"type": "transcript", "payload": {"Text": "capital of France", "Confidence": 0.9}}))
    assert seen == ["capital of France"]


async def test_listening_is_sent_after_the_answer_has_been_spoken_and_cancelled_by_new_speech(monkeypatch):
    from agentcore.frontdoor import voxhelm
    from agentcore.frontdoor.voxhelm import VoxhelmLink
    monkeypatch.setattr(voxhelm, "TTS_START_S", 0.2)
    monkeypatch.setattr(voxhelm, "SPOKEN_CHARS_PER_S", 1000.0)

    class Core:
        async def hold_warm(self, *a):
            pass
    link = VoxhelmLink("ada", "wss://unused", ["u1"], Core(), name="voxhelm")
    link.sockets["u1"] = sock = FakeSocket()
    link.calls["u1"] = {"said": 1}
    await link.send({"user": "u1", "voice": True, "said": 1}, "Paris.")
    assert [k for k in sock.kinds() if k[0] == "state_update"] == [("state_update", "speaking")]
    await asyncio.sleep(0.4)
    assert sock.kinds()[-1] == ("state_update", "listening")
    # the caller talks over the next answer: "thinking" replaces the pending "listening"
    await link.send({"user": "u1", "voice": True, "said": 1}, "Paris again.")
    await link.state("u1", "thinking")
    await asyncio.sleep(0.4)
    assert sock.kinds()[-1] == ("state_update", "thinking")


async def test_slow_voice_turn_gets_a_holding_line_then_an_apology_and_the_late_answer_is_dropped(monkeypatch):
    import json
    from agentcore.frontdoor import voxhelm
    from agentcore.frontdoor.voxhelm import VoxhelmLink
    monkeypatch.setattr(voxhelm, "VOICE_HOLD_S", 0.1)
    monkeypatch.setattr(voxhelm, "VOICE_GIVE_UP_S", 0.3)
    monkeypatch.setattr(voxhelm, "TTS_START_S", 0.0)
    monkeypatch.setattr(voxhelm, "SPOKEN_CHARS_PER_S", 10000.0)

    class DB:
        async def event(self, *a, **kw):
            pass

    class Core:
        db = DB()

        async def hold_warm(self, *a):
            pass

        async def intake(self, agent, **kw):
            Core.reply_to = kw["reply_to"]
    link = VoxhelmLink("ada", "wss://unused", ["u1"], Core(), name="voxhelm")
    link.sockets["u1"] = sock = FakeSocket()
    await link._on_message("u1", json.dumps({"type": "transcript", "payload": {"text": "hard question"}}))
    await asyncio.sleep(0.5)
    spoken = [m["payload"]["text"] for m in sock.sent if m["type"] == "llm_response"]
    assert spoken == [voxhelm.HOLD_LINE, voxhelm.GIVE_UP_LINE]
    assert sock.kinds()[-1] == ("state_update", "listening")
    await link.send(Core.reply_to, "The very late answer.")
    assert [m["payload"]["text"] for m in sock.sent if m["type"] == "llm_response"] == spoken


async def test_bedrock_client_replaced_after_idling():
    from agentcore.bedrock import BedrockClient
    c = BedrockClient("ap-southeast-2", idle_reconnect_s=45.0)
    first = c._fresh()
    assert c._fresh() is first  # in use: the same pool
    c._last_call -= 60  # idle for a minute
    assert c._fresh() is not first


async def test_a_thought_split_by_a_pause_is_answered_as_one_turn():
    import json
    from agentcore.frontdoor.voxhelm import VoxhelmLink
    intakes, cancelled = [], []

    class DB:
        async def cancel_queued(self, item_id):
            cancelled.append(item_id)
            return True

        async def event(self, *a, **kw):
            pass

    class Core:
        db = DB()

        async def hold_warm(self, *a):
            pass

        async def intake(self, agent, **kw):
            intakes.append(kw)
            return {"item_id": len(intakes)}
    link = VoxhelmLink("ada", "wss://unused", ["u1"], Core(), name="voxhelm")
    link.sockets["u1"] = sock = FakeSocket()
    say = lambda t: link._on_message("u1", json.dumps({"type": "transcript", "payload": {"text": t}}))
    await say("yeah I don't know what's going on with the speech")
    await say("I said hey are you there")
    assert intakes[1]["text"] == "yeah I don't know what's going on with the speech I said hey are you there"
    assert cancelled == [1]
    # the merged turn's answer is spoken; a late answer to the first part is not
    await link.send(intakes[0]["reply_to"], "Answer to the first part only.")
    await link.send(intakes[1]["reply_to"], "Yep, I'm here and I heard both.")
    spoken = [m["payload"]["text"] for m in sock.sent if m["type"] == "llm_response"]
    assert spoken == ["Yep, I'm here and I heard both."]
    # after an answer, the next transcript starts fresh
    await say("thanks")
    assert intakes[2]["text"] == "thanks"
    for t in link.calls["u1"].get("watches", set()):
        t.cancel()


@needs_pg
async def test_client_token_cannot_read_replies_from_other_channels(door):
    fd, api, _ = await door()
    row = await fd.db.enqueue("ada", "chat", {"conversation_id": "tg:5", "text": "private", "user_id": "tg:5"},
                              conversation_id="tg:5", reply_to={"channel": "telegram", "chat_id": "5"})
    assert (await api.get(f"/v1/work/{row['id']}")).status_code == 404


@needs_pg
async def test_api_caller_cannot_act_as_a_person_on_another_channel(door):
    fd, api, _ = await door()
    item_id = (await chat(api, "what did I tell you?", conv="tg:5"))["item_id"]
    item = await fd.db.get_item(item_id)
    assert item["payload"]["conversation_id"] == "api:tg:5" and item["payload"]["user_id"] == "api:u1"


@needs_pg
async def test_stopped_agents_replies_do_not_hold_up_other_agents(door):
    fd, api, _ = await door()
    for n in range(3):
        row = await fd.db.enqueue("stopped-one", "chat", {"text": str(n)}, conversation_id="c", reply_to={})
        await fd.db.q("UPDATE frontdoor.work_items SET status='done' WHERE id=%s", row["id"])
    other = await fd.db.enqueue("ada", "chat", {"text": "x"}, conversation_id="c", reply_to={})
    await fd.db.q("UPDATE frontdoor.work_items SET status='done' WHERE id=%s", other["id"])
    got = await fd.db.undelivered(limit=1, exclude=["stopped-one"])
    assert [i["id"] for i in got] == [other["id"]]
