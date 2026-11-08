import json
from datetime import datetime, timezone

import httpx

from agentcore.directory import Directory, changes, person_from_graph
from agentcore.phone import PhoneTools, caller_for
from agentcore.store import MemoryStore
from agentcore.tools import CallContext

USERS = [
    {"id": "aad-erin", "displayName": "Erin Hall", "givenName": "Erin", "surname": "Hall",
     "mail": "Erin.Hall@acme.example", "jobTitle": "Lead Engineer", "department": "Engineering",
     "mobilePhone": "0412 345 678", "accountEnabled": True, "userType": "Member",
     "manager": {"id": "aad-alex", "displayName": "Alex Owner"}},
    {"id": "aad-alex", "displayName": "Alex Owner", "givenName": "Alex", "surname": "Owner",
     "mail": "Alex.Owner@acme.example", "accountEnabled": True, "userType": "Member"},
    {"id": "aad-ada", "displayName": "Ada (AI)", "mail": "ada@acme.example", "accountEnabled": True, "userType": "Member"},
    {"id": "aad-room", "displayName": "Boardroom", "mail": None, "accountEnabled": True, "userType": "Member"},
    {"id": "aad-gone", "displayName": "Old Account", "mail": "old@acme.example", "accountEnabled": False, "userType": "Member"},
    {"id": "aad-guest", "displayName": "Guest", "mail": "g@other.com", "accountEnabled": True, "userType": "Guest"},
    {"id": "aad-book", "displayName": "Book a time with Alex", "mail": "BookatimewithAlex@acme.example",
     "accountEnabled": True, "userType": "Member", "assignedLicenses": []},
    {"id": "aad-arch", "displayName": "!Archived Someone", "mail": "someone@acme.example", "accountEnabled": True,
     "userType": "Member", "assignedLicenses": [{"skuId": "x"}]},
    {"id": "aad-adm", "displayName": "CPS Admin", "mail": "cps@acme.onmicrosoft.com", "accountEnabled": True,
     "userType": "Member", "assignedLicenses": [{"skuId": "x"}]},
    {"id": "aad-scrum", "displayName": "Scrummaster Bot", "mail": "scrumm4st3r@acme.example", "accountEnabled": True,
     "userType": "Member", "assignedLicenses": [{"skuId": "x"}]},
]


def graph(pages, seen):
    def handler(req):
        if "login.microsoftonline.com" in str(req.url):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        seen.append(str(req.url))
        return httpx.Response(200, json=pages.pop(0))
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class Memory:
    def __init__(self):
        self.retained = []

    def retain_later(self, **kw):
        self.retained.append(kw)


def make(store, pages, seen, memory=None):
    return Directory(store, app_id="a", app_password="p", tenant_id="t", memory=memory,
                     agent_mailboxes={"ada@acme.example"}, http=graph(pages, seen),
                     clock=lambda: datetime(2026, 10, 2, tzinfo=timezone.utc))


def test_only_reachable_active_members_are_kept():
    kept = [p for p in map(person_from_graph, USERS) if p]
    assert [p["name"] for p in kept] == ["Erin Hall", "Alex Owner", "Ada (AI)", "Scrummaster Bot"]
    assert kept[0]["mobile"] == "+61412345678" and kept[0]["manager"] == "Alex Owner"


async def test_sync_pages_stores_and_answers_lookups():
    store, seen = MemoryStore(), []
    d = make(store, [{"value": USERS[:3], "@odata.nextLink": "https://graph.microsoft.com/v1.0/users?page=2"},
                     {"value": USERS[3:]}], seen)
    out = await d.sync()
    assert out["people"] == 4 and out["with_mobile"] == 1 and len(seen) == 2
    assert [p["name"] for p in d.find("erin")] == ["Erin Hall"]
    assert [p["name"] for p in d.find("eri hal")] == ["Erin Hall"]
    assert d.find("alex.owner@acme.example")[0]["id"] == "aad-alex"
    assert d.find("nobody") == []
    block = d.prompt_block()
    assert "Erin Hall — Lead Engineer, Engineering <Erin.Hall@acme.example>, reports to Alex Owner" in block
    assert "Ada (AI) (AI agent)" in block and "0412" not in block and "+61" not in block  # no phone numbers
    again = make(store, [], [])
    await again.load()
    assert len(again.people) == 4 and not again.stale()
    assert "Scrummaster Bot (AI agent)" in block


async def test_directory_mobile_is_recognised_and_callable_but_registered_numbers_win():
    store, seen = MemoryStore(), []
    d = make(store, [{"value": USERS}], seen)
    await d.sync()
    assert (await caller_for(store, "0412345678"))["aad"] == "aad-erin"
    await store.set_pref("phone:+61412345678", "owner", {"aad": "aad-someone", "name": "Registered"})
    assert (await caller_for(store, "0412345678"))["aad"] == "aad-someone"

    class Dialler:
        calls = []

        async def dial(self, number, init):
            self.calls.append(number)
            return {"success": True}

    class People:
        async def find(self, q):
            return d.find(q)
    tools = PhoneTools(store, People(), owner_email="x", dialler=Dialler(), clock=lambda: datetime(2026, 10, 2, 10))
    ctx = CallContext("c", "aad-alex", ["call erin"], actor_name="Alex Owner")
    res = json.loads(await tools.call_tool("call_person", {"person": "Erin", "reason": "x", "channel": "mobile"}, ctx))
    assert res["calling"] is True and Dialler.calls == ["+61412345678"]


async def test_changes_are_described_and_retained_and_leavers_lose_their_number():
    store, memory = MemoryStore(), Memory()
    d = make(store, [{"value": USERS}], [], memory)
    await d.sync()
    assert memory.retained == []  # first sync is a baseline, not news
    promoted = dict(USERS[1], jobTitle="CEO")
    d.http = graph([{"value": [promoted, {"id": "aad-new", "displayName": "Noah New", "mail": "noah@acme.example",
                                          "jobTitle": "Engineer", "accountEnabled": True, "userType": "Member"}]}], [])
    out = await d.sync()
    assert "Noah New joined Acme as Engineer (noah@acme.example)" in out["changes"]
    assert "Alex Owner's job title is now CEO" in out["changes"]
    assert any("Erin Hall is no longer" in c for c in out["changes"])
    assert memory.retained and memory.retained[0]["scope"] == "shared"
    assert await caller_for(store, "0412345678") is None
    assert await store.get_pref("aad-erin", "directory_mobile") is None


def test_changes_ignore_unchanged_people():
    p = person_from_graph(USERS[0])
    assert changes([p], [dict(p)]) == []


async def test_without_user_read_all_manager_expand_is_dropped():
    store, seen = MemoryStore(), []

    def handler(req):
        if "login.microsoftonline.com" in str(req.url):
            return httpx.Response(200, json={"access_token": "t"})
        seen.append(str(req.url))
        if "expand" in str(req.url):
            return httpx.Response(403, json={})
        return httpx.Response(200, json={"value": [{"id": "x", "displayName": "X", "mail": "x@acme.example"}]})
    d = Directory(store, app_id="a", app_password="p", tenant_id="t", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert (await d.sync())["people"] == 1 and len(seen) == 2


async def test_outreach_find_uses_the_local_directory_first():
    from agentcore.outreach import TeamsOutreach
    store = MemoryStore()
    d = make(store, [{"value": USERS}], [])
    await d.sync()
    o = TeamsOutreach(connector=None, store=store, app_id="a", app_password="p", tenant_id="t", footer="f",
                      http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    o.directory = d
    assert (await o.find("Erin"))[0]["email"] == "Erin.Hall@acme.example"  # no Graph call (would 500)


def test_staff_list_is_in_the_prompt_for_staff_only(cfg):
    from .conftest import make_agent
    agent, _, _, _ = make_agent(cfg, [])

    class D:
        def prompt_block(self):
            return "Acme staff directory: - Erin"
    agent.directory = D()
    assert any("Acme staff directory" in p for p in agent.system_prompt("default"))
    assert not any("Acme staff directory" in p for p in agent.system_prompt("default", verified=False))
