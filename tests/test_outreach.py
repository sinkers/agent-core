import json

import httpx
import pytest

from agentcore.outreach import TeamsOutreach
from agentcore.store import MemoryStore
from agentcore.tools import CallContext, _named_in

OWNER = {"id": "aad-alex", "displayName": "Alex Owner", "mail": "alex.owner@acme.example"}
ERIN = {"id": "aad-erin", "displayName": "Erin Hall", "mail": "erin.hall@acme.example"}


class FakeConnector:
    def __init__(self, installed: set[str]):
        self.installed = installed
        self.sent: list[tuple[dict, str]] = []
        self.created: list[str] = []

    async def send(self, ref, text, reply_to=None):
        self.sent.append((ref, text))

    async def create_conversation(self, service_url, app_id, user_id, tenant_id):
        if user_id not in self.installed:
            raise httpx.HTTPStatusError("403", request=httpx.Request("POST", service_url),
                                        response=httpx.Response(403))
        self.created.append(user_id)
        return f"conv-{user_id}"


def graph(users, install_ok=True, installs=None):
    def handler(req: httpx.Request):
        if "login.microsoftonline.com" in str(req.url):
            return httpx.Response(200, json={"access_token": "g", "expires_in": 3600})
        if req.url.path.endswith("/users"):
            q = req.url.params["$filter"].lower()
            return httpx.Response(200, json={"value": [u for u in users if u["mail"].lower() in q
                                                       or u["displayName"].split()[0].lower() in q]})
        if req.url.path.endswith("/appCatalogs/teamsApps"):
            return httpx.Response(200, json={"value": [{"id": "catalog-1"}]})
        if req.url.path.endswith("/installedApps"):
            if installs is not None:
                installs.append(req.url.path.split("/")[-3])
            return httpx.Response(201 if install_ok else 403)
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def make(users, installed, install_ok=True, installs=None):
    store = MemoryStore()
    await store.upsert_conversation_ref("conv-a", {"serviceUrl": "https://smba/au/", "conversation": {"id": "conv-a"},
                                                   "user": {"name": "Erin Hall"}}, "aad-erin")
    conn = FakeConnector(installed)
    out = TeamsOutreach(connector=conn, store=store, app_id="app", app_password="pw", tenant_id="t",
                        footer="Ada (AI) is an AI agent. Owner: A. Owner.", teams_app_external_id="ext",
                        http=graph(users, install_ok, installs))
    return out, conn, store


async def test_new_chat_names_requester_and_saves_reference():
    out, conn, store = await make([OWNER, ERIN], installed={"aad-alex"})
    res = json.loads(await out.call_tool("send_teams_message", {"to": "alex.owner@acme.example", "text": "Erin asked me to flag PRJ."},
                                         CallContext("conv-a", "aad-erin")))
    assert res == {"sent": True, "to": "Alex Owner", "email": "alex.owner@acme.example", "via": "new chat"}
    ref, text = conn.sent[0]
    assert ref["serviceUrl"] == "https://smba/au/"  # reuses the requester's region
    assert "Sent at the request of Erin Hall." in text and "AI agent" in text
    assert await store.conversation_ref_for_user("aad-alex") is not None


async def test_existing_chat_is_reused():
    out, conn, _ = await make([ERIN], installed=set())
    res = json.loads(await out.call_tool("send_teams_message", {"to": "erin.hall@acme.example", "text": "hi"}, CallContext("c", "x")))
    assert res["via"] == "existing chat" and conn.created == []


async def test_self_install_then_retry():
    installs = []
    out, conn, _ = await make([OWNER], installed=set(), installs=installs)
    conn.installed = set()

    async def create_after_install(service_url, app_id, user_id, tenant_id):
        if not installs:
            raise httpx.HTTPStatusError("403", request=httpx.Request("POST", "x"), response=httpx.Response(403))
        return "conv-new"
    conn.create_conversation = create_after_install
    res = json.loads(await out.call_tool("send_teams_message", {"to": "alex.owner@acme.example", "text": "hi"}, CallContext("c", "x")))
    assert res["sent"] and installs == ["aad-alex"]


async def test_not_installed_and_install_refused_reports_clearly():
    out, conn, _ = await make([OWNER], installed=set(), install_ok=False)
    res = json.loads(await out.call_tool("send_teams_message", {"to": "alex.owner@acme.example", "text": "hi"}, CallContext("c", "x")))
    assert res["sent"] is False and "setup policy" in res["reason"]


async def test_ambiguous_name_asks_which_one():
    twins = [OWNER, {"id": "aad-2", "displayName": "Alex Smith", "mail": "alex.smith@acme.example"}]
    out, *_ = await make(twins, installed={"aad-alex", "aad-2"})
    res = json.loads(await out.call_tool("send_teams_message", {"to": "Alex", "text": "hi"}, CallContext("c", "x")))
    assert res["sent"] is False and len(res["candidates"]) == 2


def test_requester_counts_as_named():
    from agentcore.config import AgentConfig, load_agent_config
    from agentcore.skills import SkillLoader
    from agentcore.tools import ToolGateway
    from .conftest import AGENTS, SKILLS
    cfg = load_agent_config(AGENTS / "ada" / "agent.yaml")
    gw = ToolGateway(cfg, SkillLoader(SKILLS, "ada", [], []))
    ctx = CallContext("c", "aad-alex", ["send me a reminder at 3"], actor_name="Alex Owner")
    assert gw.needs_confirm("schedule__schedule_task", {"deliver_to": "Alex Owner"}, ctx) is None
    assert gw.needs_confirm("schedule__schedule_task", {"deliver_to": "Noah Smith"}, ctx) is not None
    # emailing Sam for Alex: Alex may be cc'd without a yes, but nobody else unnamed can be
    ctx = CallContext("c", "aad-alex", ["email Sam the plan"], actor_name="Alex Owner")
    for agent in ("ada", "eve", "dan"):
        gw = ToolGateway(load_agent_config(AGENTS / agent / "agent.yaml"), SkillLoader(SKILLS, agent, [], []))
        mail = {"to": ["sam.taylor@acme.example"], "subject": "Plan", "body": "x"}
        assert gw.needs_confirm("mail__send_email", {**mail, "cc": ["alex.owner@acme.example"]}, ctx) is None
        assert "cc=" in gw.needs_confirm("mail__send_email", {**mail, "cc": ["nick.smith@acme.example"]}, ctx)


@pytest.mark.parametrize("value,said,ok", [
    ("alex.owner@acme.example", "can you message alex for me", True),
    ("Alex Owner", "tell alex the drawings are late", True),
    ("evil@example.com", "email bob@acme.example the minutes", False),
    ("nick.smith@acme.example", "tell alex", False),
    ("someone@acme.example", "acme example stuff", False),  # domain parts never count
])
def test_named_in(value, said, ok):
    assert _named_in(value, said) is ok
