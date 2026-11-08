import json

import pytest

from agentcore.phone import PhoneTools, caller_for, normalise
from agentcore.store import MemoryStore
from agentcore.tools import CallContext


@pytest.mark.parametrize("raw,e164", [
    ("0412 345 678", "+61412345678"), ("+61 412 345 678", "+61412345678"), ("61412345678", "+61412345678"),
    ("(03) 6159 7104", "+61361597104"), ("+1 415 555 0100", "+14155550100"), ("412345678", "+61412345678"),
])
def test_normalise(raw, e164):
    assert normalise(raw) == e164


@pytest.mark.parametrize("bad", ["", "anonymous", "123", "0412 345 67a", "+61 4"])
def test_normalise_rejects(bad):
    assert normalise(bad) is None


class FakeOutreach:
    def __init__(self):
        self.sent = []

    async def find(self, q):
        return [{"id": "aad-alex", "name": "Alex Owner", "email": q}]

    async def send_to_user(self, user_id, name, body):
        self.sent.append((user_id, body))
        return True


async def test_register_then_caller_is_known_and_only_self():
    store = MemoryStore()
    tools = PhoneTools(store, FakeOutreach(), owner_email="alex.owner@acme.example")
    ctx = CallContext("c", "aad-erin", ["my mobile is 0412 345 678"], actor_name="Erin Hall")
    res = json.loads(await tools.call_tool("register_my_phone", {"number": "0412 345 678"}, ctx))
    assert res == {"registered": True, "number": "+61412345678", "for": "Erin Hall"}
    assert await caller_for(store, "+61 412 345 678") == {"aad": "aad-erin", "name": "Erin Hall"}
    # someone else can't take over a registered number
    other = CallContext("c2", "aad-nick", ["register 0412345678"], actor_name="Noah")
    res = json.loads(await tools.call_tool("register_my_phone", {"number": "0412345678"}, other))
    assert res["registered"] is False and "already registered" in res["reason"]
    # and a caller with no identity (e.g. a phone call) can't register at all
    anon = CallContext("call:x", "unknown", [], restricted=True)
    res = json.loads(await tools.call_tool("register_my_phone", {"number": "0499 999 999"}, anon))
    assert res["registered"] is False
    removed = json.loads(await tools.call_tool("forget_my_phone", {"number": "0412345678"}, ctx))
    assert removed == {"removed": True, "number": "+61412345678"}
    assert await caller_for(store, "0412345678") is None


async def test_take_message_goes_to_owner():
    out = FakeOutreach()
    tools = PhoneTools(MemoryStore(), out, owner_email="alex.owner@acme.example")
    ctx = CallContext("call:abc", "unknown", [], restricted=True, caller_number="+61400111222")
    res = json.loads(await tools.call_tool("take_message", {"caller_name": "Sam Jones", "company": "TasNetworks",
                                                             "callback_number": "", "message": "Please call re PRJ"}, ctx))
    assert res == {"delivered": True, "to": "Alex Owner"}
    user, body = out.sent[0]
    assert user == "aad-alex" and "Sam Jones (TasNetworks)" in body and "+61400111222" in body
    assert "unverified caller" in body.lower()


async def test_gateway_blocks_everything_but_take_message_when_restricted():
    from agentcore.config import load_agent_config
    from agentcore.skills import SkillLoader
    from agentcore.tools import ToolGateway
    from .conftest import AGENTS, SKILLS
    gw = ToolGateway(load_agent_config(AGENTS / "ada" / "agent.yaml"), SkillLoader(SKILLS, "ada", [], []))
    ctx = CallContext("call:x", "unknown", [], restricted=True)
    out = await gw.call("jira__search_issues", {"jql": "project = PRJ"}, ctx, True)
    assert out.status == "blocked" and "verified" in out.content
    out = await gw.call("skills__load", {"name": "today-board"}, ctx, True)
    assert out.status == "blocked"
    cfg = gw.bedrock_tool_config(only={"phone__take_message"})
    assert cfg is None or [t["toolSpec"]["name"] for t in cfg["tools"]] == ["phone__take_message"]


class FakeDialler:
    def __init__(self):
        self.calls = []

    async def dial(self, to_number, initiation):
        self.calls.append((to_number, initiation))
        return {"success": True, "conversation_id": "conv_1"}


class People(FakeOutreach):
    async def find(self, q):
        people = {"erin": {"id": "aad-erin", "name": "Erin Hall", "email": "erin.hall@acme.example"},
                  "alex": {"id": "aad-alex", "name": "Alex Owner", "email": "alex.owner@acme.example"}}
        return [p for k, p in people.items() if k in q.lower()]


async def registered(store, aad, name, number):
    tools = PhoneTools(store, People(), owner_email="alex.owner@acme.example")
    await tools.call_tool("register_my_phone", {"number": number}, CallContext("c", aad, [], actor_name=name))


def alex_ctx(**kw):
    return CallContext("chat-a", "aad-alex", ["call erin about the PRJ drawings"], actor_name="Alex Owner", **kw)


async def test_call_person_dials_registered_staff_number_with_reason():
    from datetime import datetime
    store, dialler = MemoryStore(), FakeDialler()
    await registered(store, "aad-erin", "Erin Hall", "0412 345 678")
    tools = PhoneTools(store, People(), owner_email="alex.owner@acme.example", dialler=dialler,
                       clock=lambda: datetime(2026, 10, 1, 10, 0))
    res = json.loads(await tools.call_tool("call_person", {"person": "Erin", "reason": "the PRJ drawings", "channel": "mobile"}, alex_ctx()))
    assert res["calling"] is True and res["to"] == "Erin Hall"
    number, init = dialler.calls[0]
    assert number == "+61412345678"
    assert "Alex Owner asked me to call about the PRJ drawings" in init["dynamic_variables"]["greeting"]
    extra = init["custom_llm_extra_body"]
    assert extra["caller_aad_id"] == "aad-erin" and extra["kind"] == "phone-out"
    # the reason is in the call's history, so Ada knows why he rang
    assert any("PRJ drawings" in m.text for m in store.messages if m.conversation_id == f"call:{extra['call_id']}")


async def test_call_person_refuses_unregistered_and_unverified_and_after_hours():
    from datetime import datetime
    store, dialler = MemoryStore(), FakeDialler()
    tools = PhoneTools(store, People(), owner_email="x", dialler=dialler, clock=lambda: datetime(2026, 10, 1, 22, 30))
    res = json.loads(await tools.call_tool("call_person", {"person": "Erin", "reason": "x", "channel": "mobile"}, alex_ctx()))
    assert res["calling"] is False and "register" in res["reason"]
    await registered(store, "aad-erin", "Erin Hall", "0412 345 678")
    res = json.loads(await tools.call_tool("call_person", {"person": "Erin", "reason": "x", "channel": "mobile"}, alex_ctx()))
    assert res["calling"] is False and "hours" in res["reason"]  # 22:30 Hobart, and it's not the requester
    await registered(store, "aad-alex", "Alex Owner", "0418 000 000")
    res = json.loads(await tools.call_tool("call_person", {"person": "Alex", "reason": "x", "channel": "mobile"}, alex_ctx()))
    assert res["calling"] is True  # people can ask to be called themselves at any hour
    anon = CallContext("call:x", "unknown", [], restricted=True)
    res = json.loads(await tools.call_tool("call_person", {"person": "Erin", "reason": "x", "channel": "mobile"}, anon))
    assert res["calling"] is False


async def test_call_person_daily_limit_per_person():
    from datetime import datetime
    store, dialler = MemoryStore(), FakeDialler()
    await registered(store, "aad-erin", "Erin Hall", "0412 345 678")
    tools = PhoneTools(store, People(), owner_email="x", dialler=dialler, clock=lambda: datetime(2026, 10, 1, 10, 0))
    results = [json.loads(await tools.call_tool("call_person", {"person": "Erin", "reason": "x", "channel": "mobile"}, alex_ctx()))
               for _ in range(4)]
    assert [r["calling"] for r in results] == [True, True, True, False]


class FakeTeamsCaller:
    def __init__(self, ok=True):
        self.ok, self.calls = ok, []

    async def ring(self, aad, ref):
        self.calls.append((aad, ref))
        return {"success": True, "call_id": "graph-1"} if self.ok else {"success": False, "message": "boom"}


async def test_call_person_defaults_to_teams_and_needs_no_registered_number():
    from datetime import datetime
    store, dialler, teams = MemoryStore(), FakeDialler(), FakeTeamsCaller()
    tools = PhoneTools(store, People(), owner_email="x", dialler=dialler, teams_caller=teams,
                       clock=lambda: datetime(2026, 10, 1, 10, 0))
    res = json.loads(await tools.call_tool("call_person", {"person": "Erin", "reason": "the PRJ drawings"}, alex_ctx()))
    assert res["calling"] is True and res["channel"] == "teams" and "on Teams" in res["say"]
    assert not dialler.calls  # no paid phone call
    aad, ref = teams.calls[0]
    assert aad == "aad-erin" and ref.startswith("tout-")
    pending = await store.get_pref(f"call-out:{ref}", "pending")
    assert "Alex Owner asked me to call about the PRJ drawings" in pending["opener"]
    assert any("on Teams" in m.text for m in store.messages if m.conversation_id == f"call:{ref}")


async def test_call_person_mobile_says_mobile_and_teams_failure_offers_mobile():
    from datetime import datetime
    store, dialler = MemoryStore(), FakeDialler()
    await registered(store, "aad-erin", "Erin Hall", "0412 345 678")
    tools = PhoneTools(store, People(), owner_email="x", dialler=dialler, teams_caller=FakeTeamsCaller(ok=False),
                       clock=lambda: datetime(2026, 10, 1, 10, 0))
    res = json.loads(await tools.call_tool("call_person", {"person": "Erin", "reason": "x"}, alex_ctx()))
    assert res["calling"] is False and res["channel"] == "teams" and "mobile instead" in res["reason"]
    assert not dialler.calls  # never silently falls back to a paid call
    res = json.loads(await tools.call_tool("call_person", {"person": "Erin", "reason": "x", "channel": "phone"},
                                           alex_ctx()))
    assert res["calling"] is True and res["channel"] == "mobile" and "not Teams" in res["say"]
    assert dialler.calls[0][0] == "+61412345678"


async def test_call_person_teams_not_configured_and_bad_channel():
    store = MemoryStore()
    tools = PhoneTools(store, People(), owner_email="x", dialler=FakeDialler())
    res = json.loads(await tools.call_tool("call_person", {"person": "Erin", "reason": "x"}, alex_ctx()))
    assert res["calling"] is False and "not set up" in res["reason"]
    res = json.loads(await tools.call_tool("call_person", {"person": "Erin", "reason": "x", "channel": "fax"},
                                           alex_ctx()))
    assert res["calling"] is False


async def test_teams_caller_posts_to_bridge_with_bearer():
    import httpx
    from agentcore.phone import TeamsCaller
    seen = {}

    def handler(request):
        seen["auth"], seen["body"], seen["url"] = request.headers["authorization"], json.loads(request.content), str(request.url)
        return httpx.Response(202, json={"callId": "graph-9"})
    caller = TeamsCaller("https://bridge.example/", "tok", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await caller.ring("aad-1", "tout-1") == {"success": True, "call_id": "graph-9"}
    assert seen == {"auth": "Bearer tok", "body": {"userAadId": "aad-1", "ref": "tout-1"},
                    "url": "https://bridge.example/api/call"}
    busy = TeamsCaller("https://b", "t", http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))))
    assert (await busy.ring("a", "r"))["success"] is False
