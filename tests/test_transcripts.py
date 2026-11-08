import json
from datetime import datetime, timedelta, timezone

from agentcore.store import MemoryStore, Message
from agentcore.tools import CallContext
from agentcore.transcripts import CallSweeper, TranscriptTools, record_outbound

from .conftest import make_agent

NOW = datetime(2026, 10, 1, 0, 10, tzinfo=timezone.utc)


async def add_call(store, conv, user, start, turns=3):
    for i in range(turns):
        at = start + timedelta(seconds=20 * i)
        await store.save_message(Message(conv, "user", f"question {i}", user, "Alex Owner", at))
        await store.save_message(Message(conv, "assistant", f"answer {i}", None, None, at + timedelta(seconds=5)))


async def test_transcript_tools_only_show_the_askers_own_calls():
    store = MemoryStore()
    await add_call(store, "call:a1", "aad-alex", NOW - timedelta(hours=2))
    await add_call(store, "call:e1", "aad-erin", NOW - timedelta(hours=1))
    tools = TranscriptTools(store, clock=lambda: NOW)
    me = CallContext("chat", "aad-alex", [], actor_name="Alex Owner")
    calls = json.loads(await tools.call_tool("list_my_calls", {}, me))["calls"]
    assert [c["call"] for c in calls] == ["call:a1"] and calls[0]["turns"] == 3
    latest = json.loads(await tools.call_tool("get_call_transcript", {"call": "latest"}, me))
    assert "question 0" in latest["transcript"] and "answer 2" in latest["transcript"]
    other = json.loads(await tools.call_tool("get_call_transcript", {"call": "call:e1"}, me))
    assert other.get("found") is False  # someone else's call
    anon = CallContext("call:x", "unknown", [], restricted=True)
    assert json.loads(await tools.call_tool("list_my_calls", {}, anon))["calls"] == []


async def test_sweeper_summarises_a_silent_call_once_into_the_callers_chat(cfg):
    agent, model, store, _ = make_agent(cfg, ["- Talked about PRJ\n- Action: Alex to check drawings"])
    await store.upsert_conversation_ref("chat-a", {"conversation": {"id": "chat-a", "conversationType": "personal"}}, "aad-alex")
    await add_call(store, "call:cut", "aad-alex", NOW - timedelta(minutes=10))
    sent = []

    async def deliver(aad, text):
        sent.append((aad, text))
        await record_outbound(store, "chat-a", text)
        return True
    sweeper = CallSweeper(agent=agent, store=store, deliver=deliver, clock=lambda: NOW, idle_minutes=3)
    assert await sweeper.tick() == 1
    assert sent[0][0] == "aad-alex" and "PRJ" in sent[0][1] and "call at" in sent[0][1].lower()
    assert any(m.conversation_id == "chat-a" and "PRJ" in m.text for m in store.messages)  # in chat history now
    assert await sweeper.tick() == 0  # only once


async def test_sweeper_ignores_live_calls_and_one_line_calls(cfg):
    agent, _, store, _ = make_agent(cfg, [])
    await add_call(store, "call:live", "aad-alex", NOW - timedelta(minutes=1))
    await add_call(store, "call:tiny", "aad-alex", NOW - timedelta(minutes=20), turns=1)
    sweeper = CallSweeper(agent=agent, store=store, deliver=None, clock=lambda: NOW, idle_minutes=3)
    assert await sweeper.tick() == 0


async def test_sweeper_never_works_through_an_old_backlog(cfg):
    agent, model, store, _ = make_agent(cfg, [])
    await add_call(store, "call:old", "aad-alex", NOW - timedelta(hours=3))
    sweeper = CallSweeper(agent=agent, store=store, deliver=None, clock=lambda: NOW, idle_minutes=3)
    assert await sweeper.tick() == 0 and model.calls == []


class FakeMemory:
    def __init__(self):
        self.kept = []

    def retain_later(self, **kw):
        self.kept.append(kw)


async def test_a_meeting_is_visible_to_every_invitee_summarised_to_all_and_remembered(cfg):
    """5 Oct 2026: Ada joined Sam's '1P Dismantling Plan' review but only Sam could see it, so when
    Alex asked, Ada had no recollection of it."""
    agent, model, store, _ = make_agent(cfg, ["- Reviewed the 1P dismantling plan\n- Action: Sam to update costs"])
    agent.memory = FakeMemory()
    await add_call(store, "call:m1", "aad-sam", NOW - timedelta(minutes=30), turns=5)
    await store.set_pref("call:m1", "meeting", {"subject": "1P Dismantling Plan Review", "attendees": ["aad-sam", "aad-alex"]})
    await store.set_pref("aad-alex", "meeting_calls", ["call:m1"])
    tools = TranscriptTools(store, clock=lambda: NOW)
    alex = CallContext("chat", "aad-alex", [], actor_name="Alex Owner")
    calls = json.loads(await tools.call_tool("list_my_calls", {}, alex))["calls"]
    assert calls[0]["call"] == "call:m1" and calls[0]["meeting"] == "1P Dismantling Plan Review"
    t = json.loads(await tools.call_tool("get_call_transcript", {"call": "latest"}, alex))
    assert "Meeting: question 0" in t["transcript"]
    erin = CallContext("chat", "aad-erin", [], actor_name="Erin Hall")
    assert json.loads(await tools.call_tool("list_my_calls", {}, erin))["calls"] == []  # not invited

    sent = []

    async def deliver(aad, text):
        sent.append((aad, text))
        return True
    sweeper = CallSweeper(agent=agent, store=store, deliver=deliver, clock=lambda: NOW, idle_minutes=3)
    assert await sweeper.tick() == 1
    assert sorted(a for a, _ in sent) == ["aad-alex", "aad-sam"]
    assert all('Notes from "1P Dismantling Plan Review"' in text for _, text in sent)
    kept = agent.memory.kept[0]
    assert kept["scope"] == "shared" and "1P Dismantling Plan Review" in kept["user_text"] and "Sam to update" in kept["reply"]


async def test_link_meeting_ties_internal_invitees_but_not_externals_decliners_or_agents(cfg):
    import httpx
    from agentcore.mailbox import MailboxPoller

    class People:
        async def find(self, q):
            return [{"id": "aad-" + q.split(".")[0].split("@")[0], "name": q}]
    event = {"subject": "1P Dismantling Plan Review",
             "organizer": {"emailAddress": {"address": "sol.taylor@acme.example"}},
             "attendees": [{"emailAddress": {"address": "alex.owner@acme.example"}, "status": {"response": "accepted"}},
                           {"emailAddress": {"address": "ada@acme.example"}, "status": {"response": "accepted"}},
                           {"emailAddress": {"address": "eve@acme.example"}, "status": {"response": "none"}},
                           {"emailAddress": {"address": "contractor@other.com"}, "status": {"response": "accepted"}},
                           {"emailAddress": {"address": "erin.hall@acme.example"}, "status": {"response": "declined"}},
                           {"emailAddress": {"address": "sam@acme.example.net"}, "status": {"response": "none"}}]}

    def handler(req):
        if "login" in str(req.url):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(200, json=event)
    store = MemoryStore()
    p = MailboxPoller(agent=None, store=store, mailbox="ada@acme.example", app_id="a", app_password="p", tenant_id="t",
                      people=People(), owner_email="x", ignore_senders={"eve@acme.example"},
                      http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    meta = await p.link_meeting("ev-1", "call-9", None, NOW)
    assert meta["attendees"] == ["aad-sol", "aad-alex", "aad-sam"]
    assert await store.get_pref("aad-alex", "meeting_calls") == ["call:call-9"]
    assert (await store.get_pref("call:call-9", "meeting"))["subject"] == "1P Dismantling Plan Review"


async def test_team_shared_memory_recalls_everything_and_keeps_1to1_as_shared():
    import httpx
    from agentcore.memory import HindsightMemory
    bodies = []

    def handler(req):
        if req.url.path.endswith("/recall"):
            bodies.append(json.loads(req.content))
            return httpx.Response(200, json={"results": [{"text": "Sam is updating the 1P cost model"}]})
        return httpx.Response(200, json={})
    m = HindsightMemory("http://h", "acme-ada", "Ada", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                        team_shared=True)
    m._ready = True
    out = await m.recall("1P costs", "aad-alex")
    assert out.lines == ["Sam is updating the 1P cost model"] and "tags" not in bodies[0]
    assert HindsightMemory.scope_tags("private", "aad-sam", team_shared=True) == ["scope:shared"]
    assert HindsightMemory.scope_tags("private", "aad-sam") == ["scope:private:aad-sam"]  # other agents unchanged


def test_stale_claims_about_the_agents_own_abilities_are_never_recalled():
    from agentcore.memory import self_capability_claim
    stale = ["Ada's email tool lacks attachment capability and only supports subject and plain text body.",
             "Ada cannot upload or write files to SharePoint, only read from it using sharepoint_get",
             "Ada does not have access to calendar invites or meeting tools",
             "Ada can only join Teams standups if someone shares the content, due to his lack of calendar access",
             "Ada's available tools are limited to creating/editing Jira tickets and adding comments"]
    keep = ["Sam is updating the 1P cost model by Friday",
            "Alex prefers short replies with links",
            "The PRJ drawings can't be issued until Erin approves them",  # a fact about work, not the agent
            "Ada joined the 1P Dismantling review and posted notes to the attendees"]
    assert all(self_capability_claim(s, "Ada") for s in stale)
    assert not any(self_capability_claim(k, "Ada") for k in keep)


async def test_recall_drops_capability_claims():
    import httpx
    from agentcore.memory import HindsightMemory

    def handler(req):
        return httpx.Response(200, json={"results": [
            {"text": "Ada's email tool only supports plain text body"},
            {"text": "Sam owns the 1P cost model"}]})
    m = HindsightMemory("http://h", "b", "Ada", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    m._ready = True
    assert (await m.recall("email", "aad-alex")).lines == ["Sam owns the 1P cost model"]
