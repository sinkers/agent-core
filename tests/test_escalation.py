import json
from datetime import datetime, timedelta, timezone

from agentcore.config import ChasingConfig, FactApprover
from agentcore.escalation import EscalationTools
from agentcore.store import MemoryStore
from agentcore.tools import CallContext

SEAN = {"name": "Sam Example", "email": "sam@acme.example", "id": "aad-sam"}
APPROVER = FactApprover(name="A. Owner", email="alex.owner@acme.example", aad_id="aad-alex")


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 5, 23, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, hours):
        self.now += timedelta(hours=hours)


class FakeTeams:
    def __init__(self):
        self.sent = []

    async def find(self, q):
        return [SEAN] if "sam" in q.lower() else ([{"name": "Pat", "email": "pat@other.com", "id": "x"}] if "pat" in q else [])

    async def send(self, to, text, ctx):
        self.sent.append((to, text))
        return {"sent": True}


class FakeMcp:
    def __init__(self):
        self.calls = []

    async def call_tool(self, name, args, ctx=None):
        self.calls.append((name, args))
        if name == "find_user":
            return json.dumps({"users": [{"account_id": "jira-sam"}]})
        return json.dumps({"ok": True})


class FakeConnector:
    def __init__(self):
        self.posts = []

    async def send(self, ref, text):
        self.posts.append((ref, text))


def make(**over):
    cfg = ChasingConfig(enabled=True, email_to=["alex.owner@acme.example"], teams_chat_id="19:chat",
                           approvers=[APPROVER], **over)
    store, clock = MemoryStore(), Clock()
    teams, mail, jira, conn = FakeTeams(), FakeMcp(), FakeMcp(), FakeConnector()
    tools = EscalationTools(store, cfg, teams=teams, mail=mail, jira=jira, connector=conn, owner="A. Owner",
                            clock=clock)
    return tools, store, clock, teams, mail, jira, conn


async def call(tools, name, args, actor="aad-alex"):
    return json.loads(await tools.call_tool(name, args, CallContext("c1", actor, [])))


ITEM = {"person": "sam@acme.example", "item": "PRJ-371 site plan review",
        "link": "https://x/browse/PRJ-371", "jira_key": "PRJ-371"}


async def test_chases_are_counted_spaced_and_need_two_channels_before_a_warning():
    tools, store, clock, teams, mail, jira, _ = make()
    out = await call(tools, "chase", {**ITEM, "channel": "teams", "message": "Could you review the site plan?"})
    assert out["sent"] and out["counted_chases"] == 1 and "2 more chase" in out["next"]
    too_soon = await call(tools, "chase", {**ITEM, "channel": "email", "message": "again"})
    assert too_soon["sent"] is False and "20 hours" in too_soon["reason"]
    clock.advance(24)
    await call(tools, "chase", {**ITEM, "channel": "teams", "message": "Reminder"})
    clock.advance(24)
    out = await call(tools, "chase", {**ITEM, "channel": "teams", "message": "Reminder 2"})
    assert out["counted_chases"] == 3 and "1 more channel" in out["next"]  # three Teams messages aren't enough
    clock.advance(24)
    out = await call(tools, "chase", {**ITEM, "channel": "jira", "message": "Chasing on the ticket"})
    assert out["next"] == "warning due: call warn"
    assert jira.calls[-1] == ("add_comment", {"key": "PRJ-371", "body": "Chasing on the ticket",
                                              "mention_account_ids": ["jira-sam"]})


async def test_warning_lists_the_chases_and_record_then_escalation_needs_an_approver():
    tools, store, clock, teams, mail, jira, conn = make()
    await store.upsert_conversation_ref("19:chat", {"conversation": {"id": "19:chat"}}, None)
    for ch in ("teams", "email", "jira"):
        await call(tools, "chase", {**ITEM, "channel": ch, "message": "Please review"})
        clock.advance(21)
    early = await call(tools, "escalate", {})
    assert early["sent"] is False and "nothing is due" in early["reason"]
    warned = await call(tools, "warn", {"person": "Sam", "item": "prj-371  SITE plan review"})
    assert warned["sent"] and warned["via"] == ["Teams message", "email"]
    text = teams.sent[-1][1]
    assert "3 times" in text and "teams" in text and "jira" in text and "haven't been on the overdue list" in text
    assert (await call(tools, "warn", {**ITEM}))["sent"] is False  # only once
    clock.advance(23)
    assert "nothing is due" in (await call(tools, "escalate", {}))["reason"]  # still inside the grace period
    clock.advance(2)
    draft = await call(tools, "escalate", {})
    assert draft["sent"] is False and draft["draft_id"].startswith("E-") and "Sam Example" in draft["preview"]
    refused = await call(tools, "send_escalation", {"draft_id": draft["draft_id"]}, actor="aad-sam")
    assert refused["sent"] is False and "approver" in refused["reason"]
    sent = await call(tools, "send_escalation", {"draft_id": draft["draft_id"]})
    assert sent["sent"] and sent["via"] == ["email to 2", "Teams chat"]
    to = mail.calls[-1][1]["to"]
    assert to == ["alex.owner@acme.example", "sam@acme.example"]  # the person listed is copied
    assert "| Sam Example | [PRJ-371 site plan review](https://x/browse/PRJ-371) | 3 |" in conn.posts[-1][1]
    # the record stays; the count restarts and a fresh warning is needed before listing again
    status = await call(tools, "chase_status", {"person": "sam@acme.example"})
    assert status["open"][0]["times_escalated"] == 1 and status["open"][0]["stage"] == "chasing"
    for ch in ("teams", "email", "jira"):
        clock.advance(21)
        await call(tools, "chase", {**ITEM, "channel": ch, "message": "Still need this"})
    await call(tools, "warn", {**ITEM})
    assert "1 time(s) in the last 90 days (1 in all" in teams.sent[-1][1]


async def test_close_outsiders_exempt_and_no_approval_mode():
    tools, store, clock, teams, mail, jira, conn = make(require_approval=False, exempt=["sam@acme.example"])
    out = await call(tools, "chase", {"person": "pat", "item": "x", "channel": "teams", "message": "hi"})
    assert "isn't a Acme address" in out["error"]
    for ch in ("teams", "email"):
        await call(tools, "chase", {**ITEM, "channel": ch, "message": "Please review"})
        clock.advance(21)
    await call(tools, "chase", {**ITEM, "channel": "teams", "message": "Please review"})
    await call(tools, "warn", {**ITEM})
    clock.advance(30)
    assert (await call(tools, "chase_status", {}))["open"][0]["stage"] == "exempt"
    assert "nothing is due" in (await call(tools, "escalate", {}))["reason"]
    closed = await call(tools, "close_chase", {**ITEM, "outcome": "answered", "reason": "replied in Teams"})
    assert closed["closed"] and (await call(tools, "chase_status", {}))["open"] == []


async def test_switched_off_by_default():
    store = MemoryStore()
    tools = EscalationTools(store, ChasingConfig())
    out = json.loads(await tools.call_tool("chase_status", {}, CallContext("c1", "a", [])))
    assert "isn't switched on" in out["error"]
