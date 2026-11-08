import json

import httpx

from agentcore.mailbox import MailboxPoller, classify, strip_quoted

from .conftest import make_agent


def msg(mid, sender, subject="PRJ drawings", body="Hi Ada,\nWhere are the drawings up to?\n\nFrom: x\nold thread", headers=None, conv="conv-1"):
    return {"id": mid, "subject": subject, "conversationId": conv, "receivedDateTime": "2026-10-02T08:00:00Z",
            "from": {"emailAddress": {"address": sender, "name": sender.split("@")[0].title()}},
            "body": {"contentType": "text", "content": body}, "hasAttachments": False,
            "internetMessageHeaders": headers or []}


def test_classify():
    assert classify(msg("1", "erin.hall@acme.example"), "ada@acme.example", {"acme.example"}) == "internal"
    assert classify(msg("2", "bob@example.com"), "ada@acme.example", {"acme.example"}) == "external"
    spoof = msg("3", "ceo@acme.example", headers=[{"name": "Authentication-Results", "value": "spf=fail; dmarc=fail action=none"}])
    assert classify(spoof, "ada@acme.example", {"acme.example"}) == "external"
    auto = msg("4", "erin.hall@acme.example", headers=[{"name": "Auto-Submitted", "value": "auto-replied"}])
    assert classify(auto, "ada@acme.example", {"acme.example"}) == "skip"
    assert classify(msg("5", "noreply@atlassian.net"), "ada@acme.example", {"acme.example"}) == "skip"
    assert classify(msg("6", "ada@acme.example"), "ada@acme.example", {"acme.example"}) == "skip"


def test_strip_quoted():
    assert strip_quoted("Thanks\n\nOn Tue, 1 Oct 2026 Ada wrote:\n> old") == "Thanks"
    assert strip_quoted("Quick q\n________________________________\nFrom: Ada") == "Quick q"


class People:
    async def find(self, q):
        return [{"id": "aad-erin", "name": "Erin Hall", "email": q}] if "erin" in q else []

    async def send_to_user(self, user_id, name, body):
        self.sent = (user_id, body)
        return True


def graph(inbox, log):
    def handler(req):
        if "login.microsoftonline.com" in str(req.url):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        log.append((req.method, req.url.path, json.loads(req.content) if req.content else None))
        if req.method == "GET" and req.url.path.endswith("/mailFolders/inbox/messages"):
            return httpx.Response(200, json={"value": inbox})
        if req.url.path.endswith("/createReplyAll"):  # a draft reply-all, with Graph's recipients
            mid = req.url.path.split("/messages/")[1].split("/")[0]
            src = next(m for m in inbox if m["id"] == mid)
            return httpx.Response(201, json={"id": f"draft-{mid}", "toRecipients": [src["from"]] + src.get("toRecipients", []),
                                             "ccRecipients": src.get("ccRecipients", [])})
        return httpx.Response(202 if req.url.path.endswith(("/replyAll", "/send")) else 200, json={})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_quiet_inbox_uses_no_model_tokens(cfg):
    agent, model, store, _ = make_agent(cfg, [])
    log = []
    poller = MailboxPoller(agent=agent, store=store, mailbox="ada@acme.example", app_id="a", app_password="p",
                           tenant_id="t", people=People(), owner_email="alex.owner@acme.example", http=graph([], log))
    assert await poller.tick() == 0
    assert model.calls == [] and [m for m, *_ in log] == ["GET"]


async def test_internal_email_gets_a_threaded_reply_as_that_person(cfg):
    agent, model, store, _ = make_agent(cfg, ["Hi Erin,\n\nThe drawings are with Sam for review.\n\nAda"])
    log = []
    poller = MailboxPoller(agent=agent, store=store, mailbox="ada@acme.example", app_id="a", app_password="p",
                           tenant_id="t", people=People(), owner_email="alex.owner@acme.example",
                           http=graph([msg("m1", "erin.hall@acme.example")], log))
    assert await poller.tick() == 1
    assert "Where are the drawings up to?" in str(model.calls[0]["messages"]) and "old thread" not in str(model.calls[0]["messages"])
    reply = next(b for meth, path, b in log if path.endswith("/m1/replyAll"))
    assert "with Sam for review" in reply["comment"] and "AI agent" in reply["comment"]
    assert any(meth == "PATCH" and path.endswith("/m1") for meth, path, _ in log)  # marked read
    turn_user = [m for m in store.messages if m.role == "user"][0]
    assert turn_user.user_id == "aad-erin" and turn_user.conversation_id == "mail:conv-1"
    assert await poller.tick() == 0  # never processed twice


async def test_external_email_is_not_answered_but_reported_to_the_owner(cfg):
    agent, model, store, _ = make_agent(cfg, [])
    log, people = [], People()
    people.find = lambda q: _owner(q)
    poller = MailboxPoller(agent=agent, store=store, mailbox="ada@acme.example", app_id="a", app_password="p",
                           tenant_id="t", people=people, owner_email="alex.owner@acme.example",
                           http=graph([msg("m2", "bob@example.com", subject="Quote")], log))
    await poller.tick()
    assert model.calls == [] and not any(path.endswith(("/replyAll", "/send")) for _, path, _ in log)
    assert people.sent[0] == "aad-alex" and "bob@example.com" in people.sent[1] and "Quote" in people.sent[1]


async def _owner(q):
    return [{"id": "aad-alex", "name": "Alex Owner", "email": q}]


def test_agents_never_auto_reply_to_each_other():
    m = msg("7", "eve@acme.example")
    assert classify(m, "ada@acme.example", {"acme.example"}, {"eve@acme.example", "dan@acme.example"}) == "skip"


from datetime import datetime, timezone  # noqa: E402


def invite(mid, organizer, start="2026-10-05T00:00:00.0000000", kind="#microsoft.graph.eventMessageRequest",
           mtype="meetingRequest", event_id="ev-1", join="https://teams.microsoft.com/l/meetup-join/abc"):
    m = msg(mid, organizer, subject="PRJ coordination")
    m["@odata.type"] = kind
    detail = {**m, "meetingMessageType": mtype, "event": {
        "id": event_id, "subject": "PRJ coordination", "isCancelled": mtype == "meetingCancelled",
        "start": {"dateTime": start, "timeZone": "UTC"}, "end": {"dateTime": start.replace("T00", "T01"), "timeZone": "UTC"},
        "onlineMeeting": {"joinUrl": join}, "organizer": {"emailAddress": {"address": organizer}}}}
    return m, detail


def calendar_graph(inbox, details, log):
    def handler(req):
        if "login.microsoftonline.com" in str(req.url):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        log.append((req.method, req.url.path, json.loads(req.content) if req.content else None))
        if req.method == "GET" and req.url.path.endswith("/mailFolders/inbox/messages"):
            return httpx.Response(200, json={"value": inbox})
        if req.method == "GET" and "/messages/" in req.url.path:
            return httpx.Response(200, json=details[req.url.path.rsplit("/", 1)[-1]])
        return httpx.Response(202, json={})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def poller_for(agent, store, http, joins, now, **kw):
    async def joiner(url, requester):
        joins.append((url, requester))
        return "call-1"
    return MailboxPoller(agent=agent, store=store, mailbox="ada@acme.example", app_id="a", app_password="p",
                         tenant_id="t", people=People(), owner_email="alex.owner@acme.example", http=http,
                         joiner=joiner, clock=lambda: now, **kw)


async def test_internal_invite_is_accepted_then_joined_at_start(cfg):
    agent, model, store, _ = make_agent(cfg, [])
    log, joins = [], []
    m, d = invite("i1", "erin.hall@acme.example")  # Mon 5 Oct 00:00 UTC = 11:00 Hobart
    p = poller_for(agent, store, calendar_graph([m], {"i1": d}, log), joins, datetime(2026, 10, 4, 22, 0, tzinfo=timezone.utc))
    await p.tick()
    assert any(path.endswith("/events/ev-1/accept") for _, path, _ in log) and joins == [] and model.calls == []
    p.clock = lambda: datetime(2026, 10, 4, 23, 59, 30, tzinfo=timezone.utc)  # 30 s before start
    p.http = calendar_graph([], {}, log)
    await p.tick()
    assert joins == [("https://teams.microsoft.com/l/meetup-join/abc", "aad-erin")]
    await p.tick()
    assert len(joins) == 1  # once


async def test_cancelled_invite_is_not_joined(cfg):
    agent, _, store, _ = make_agent(cfg, [])
    log, joins = [], []
    m, d = invite("i2", "erin.hall@acme.example")
    p = poller_for(agent, store, calendar_graph([m], {"i2": d}, log), joins, datetime(2026, 10, 4, 22, 0, tzinfo=timezone.utc))
    await p.tick()
    c, cd = invite("i3", "erin.hall@acme.example", kind="#microsoft.graph.eventMessage", mtype="meetingCancelled")
    p.http = calendar_graph([c], {"i3": cd}, log)
    await p.tick()
    p.clock = lambda: datetime(2026, 10, 5, 0, 0, 10, tzinfo=timezone.utc)
    p.http = calendar_graph([], {}, log)
    await p.tick()
    assert joins == []


async def test_out_of_hours_invite_is_declined_and_external_reported(cfg):
    agent, _, store, _ = make_agent(cfg, [])
    log, joins = [], []
    late, ld = invite("i4", "erin.hall@acme.example", start="2026-10-05T10:00:00.0000000")  # 21:00 Hobart
    ext, ed = invite("i5", "bob@example.com", event_id="ev-2")
    p = poller_for(agent, store, calendar_graph([late, ext], {"i4": ld, "i5": ed}, log), joins,
                   datetime(2026, 10, 4, 22, 0, tzinfo=timezone.utc), meeting_hours=(7, 19), meeting_days=frozenset(range(5)))
    p.people.find = lambda q: _owner(q) if "alex" in q else People().find(q)
    await p.tick()
    decline = next(b for _, path, b in log if path.endswith("/events/ev-1/decline"))
    assert "07:00" in decline["comment"]
    assert not any(path.endswith("/events/ev-2/accept") for _, path, _ in log)
    assert "bob@example.com" in p.people.sent[1]


async def test_with_the_bridge_24x7_late_meetings_are_accepted(cfg):
    agent, _, store, _ = make_agent(cfg, [])
    log, joins = [], []
    late, ld = invite("i6", "erin.hall@acme.example", start="2026-10-05T10:00:00.0000000")  # 21:00 Hobart
    p = poller_for(agent, store, calendar_graph([late], {"i6": ld}, log), joins, datetime(2026, 10, 4, 22, 0, tzinfo=timezone.utc))
    await p.tick()
    assert any(path.endswith("/events/ev-1/accept") for _, path, _ in log)


def calendar_view_graph(events, log):
    def handler(req):
        if "login.microsoftonline.com" in str(req.url):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        log.append((req.method, req.url.path, None))
        if req.url.path.endswith("/calendarView"):
            return httpx.Response(200, json={"value": events})
        if req.url.path.endswith("/mailFolders/inbox/messages"):
            return httpx.Response(200, json={"value": []})
        return httpx.Response(202, json={})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def cal_event(eid, start, response="accepted", cancelled=False, join="https://teams.microsoft.com/l/meetup-join/standup"):
    return {"id": eid, "subject": "Delivery & Op's Standup", "isCancelled": cancelled,
            "start": {"dateTime": start, "timeZone": "UTC"}, "end": {"dateTime": start.replace(":30:", ":45:"), "timeZone": "UTC"},
            "onlineMeeting": {"joinUrl": join}, "responseStatus": {"response": response},
            "organizer": {"emailAddress": {"address": "erin.hall@acme.example"}}}


async def test_meetings_on_the_calendar_are_joined_even_without_an_unread_invite(cfg):
    """A forwarded invite Exchange added by itself (already read) still gets joined at its start, once."""
    agent, model, store, _ = make_agent(cfg, [])
    log, joins = [], []
    events = [cal_event("occ-1", "2026-10-04T21:30:00.0000000", response="notResponded"),
              cal_event("occ-2", "2026-10-04T21:30:00.0000000", response="declined", join="https://x/declined"),
              cal_event("occ-3", "2026-10-04T21:30:00.0000000", cancelled=True, join="https://x/cancelled")]
    p = poller_for(agent, store, calendar_view_graph(events, log), joins, datetime(2026, 10, 4, 21, 29, 30, tzinfo=timezone.utc))
    await p.tick()
    await p.tick()
    assert joins == [("https://teams.microsoft.com/l/meetup-join/standup", "aad-erin")]
    assert model.calls == []  # no tokens spent


async def test_calendar_join_never_barges_in_late_and_skips_ones_already_joined_from_invites(cfg):
    agent, _, store, _ = make_agent(cfg, [])
    log, joins = [], []
    late = [cal_event("occ-9", "2026-10-04T21:30:00.0000000")]
    p = poller_for(agent, store, calendar_view_graph(late, log), joins, datetime(2026, 10, 4, 21, 42, tzinfo=timezone.utc))
    await p.tick()
    assert joins == []  # 12 minutes in: too late to walk in
    m, d = invite("i9", "erin.hall@acme.example", join="https://teams.microsoft.com/l/meetup-join/abc")
    p = poller_for(agent, store, calendar_graph([m], {"i9": d}, log), joins, datetime(2026, 10, 4, 22, 0, tzinfo=timezone.utc))
    await p.tick()  # invite accepted: on the pending list for Mon 5 Oct 00:00 UTC
    both = [cal_event("ev-1", "2026-10-05T00:00:00.0000000", join="https://teams.microsoft.com/l/meetup-join/abc")]
    both[0]["end"]["dateTime"] = "2026-10-05T01:00:00.0000000"
    p.http = calendar_view_graph(both, log)
    p.clock = lambda: datetime(2026, 10, 4, 23, 59, 30, tzinfo=timezone.utc)
    await p.tick()
    await p.tick()
    assert joins == [("https://teams.microsoft.com/l/meetup-join/abc", "aad-erin")]  # once, not twice


def test_every_internal_domain_is_internal_and_marketing_is_skipped():
    from agentcore.config import INTERNAL_DOMAINS
    internal = set(INTERNAL_DOMAINS)
    for sender in ("erin@acme.example", "erin@acme.example.net", "erin@acme.example.net"):
        assert classify(msg("x", sender), "ada@acme.example", internal) == "internal"
    assert classify(msg("x", "someone@acme-example.com"), "ada@acme.example", internal) == "external"
    assert classify(msg("x", "Microsoft365@communication.microsoft.com"), "eve@acme.example", internal) == "skip"
    promo = msg("x", "news@vendor.com", headers=[{"name": "List-Unsubscribe", "value": "<mailto:u@vendor.com>"}])
    assert classify(promo, "eve@acme.example", internal) == "skip"


class FlakyPeople(People):
    """Directory lookup fails (as Eve's did with no User.ReadBasic.All) until fixed."""
    def __init__(self, fail_times):
        self.fail_times = fail_times

    async def find(self, q):
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("403 from Graph")
        return await super().find(q)


async def test_a_failed_email_is_retried_not_lost(cfg):
    """5 Oct 2026: Eve's lookups got 403s and both emails were marked handled and read, so never answered."""
    agent, model, store, _ = make_agent(cfg, ["Hi Erin, on it.\n\nEve"])
    log = []
    poller = MailboxPoller(agent=agent, store=store, mailbox="eve@acme.example", app_id="a", app_password="p",
                           tenant_id="t", people=FlakyPeople(1), owner_email="alex.owner@acme.example",
                           http=graph([msg("m9", "erin.hall@acme.example")], log))
    await poller.tick()
    assert any(meth == "PATCH" and b == {"isRead": False} for meth, path, b in log)  # put back for the next check
    await poller.tick()
    assert any(path.endswith("/m9/replyAll") for _, path, _ in log)  # answered on the retry
    assert await poller.tick() == 0


async def test_an_email_that_keeps_failing_is_reported_after_three_tries(cfg):
    agent, _, store, _ = make_agent(cfg, [])

    class AlwaysFails(People):
        calls = 0

        async def find(self, q):
            AlwaysFails.calls += 1
            if "alex" in q:
                return [{"id": "aad-alex", "name": "Alex Owner"}]
            raise RuntimeError("boom")
    people = AlwaysFails()
    poller = MailboxPoller(agent=agent, store=store, mailbox="eve@acme.example", app_id="a", app_password="p",
                           tenant_id="t", people=people, owner_email="alex.owner@acme.example",
                           http=graph([msg("m8", "erin.hall@acme.example")], []))
    for _ in range(4):
        await poller.tick()
    assert "couldn't process it after 3 tries" in people.sent[1]
    assert await poller.tick() == 0  # given up: no endless loop


async def test_internal_sender_missing_from_the_directory_still_gets_a_reply(cfg):
    """An alias on acme.example.net / acme.example.net isn't in the directory, but it's still a colleague."""
    agent, model, store, _ = make_agent(cfg, ["Hi Sam, will do.\n\nAda"])
    log = []
    poller = MailboxPoller(agent=agent, store=store, mailbox="ada@acme.example", app_id="a", app_password="p",
                           tenant_id="t", people=People(), owner_email="alex.owner@acme.example",
                           http=graph([msg("m7", "sam@acme.example.net")], log))
    await poller.tick()
    assert any(path.endswith("/m7/replyAll") for _, path, _ in log)
    assert [m for m in store.messages if m.role == "user"][0].user_id == "mail:sam@acme.example.net"


def _docx(text: str) -> bytes:
    import io
    from docx import Document
    d = Document()
    d.add_paragraph(text)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def graph_with_attachments(inbox, attachments, files, log):
    def handler(req):
        if "login.microsoftonline.com" in str(req.url):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        path = req.url.path
        log.append((req.method, path, None))
        if path.endswith("/mailFolders/inbox/messages"):
            return httpx.Response(200, json={"value": inbox})
        if path.endswith("/attachments"):
            return httpx.Response(200, json={"value": attachments})
        if path.endswith("/$value"):
            return httpx.Response(200, content=files[path.split("/attachments/")[1].split("/")[0]])
        if "/attachments/" in path:  # an attached email, expanded
            return httpx.Response(200, json={"item": {"subject": "Costs", "from": {"emailAddress": {"address": "sam.taylor@acme.example"}},
                                                      "body": {"content": "The cost model is attached to the plan."}}})
        return httpx.Response(202 if path.endswith(("/replyAll", "/send")) else 200, json={})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_email_attachments_are_read_not_bounced_to_sharepoint(cfg):
    """5 Oct 2026: Alex forwarded 'Acme-PRJ-PLN-001 1P Dismantling Plan_Rev3a.docx' to Eve; she replied
    she couldn't open email attachments and asked for a SharePoint link."""
    import json as _json
    from agentcore.attachments import AttachmentTools
    from agentcore.tools import CallContext
    agent, model, store, _ = make_agent(cfg, ["Hi Erin, reviewing it now.\n\nEve"])
    name = "Acme-PRJ-PLN-001 1P Dismantling Plan_Rev3a.docx"
    attachments = [
        {"@odata.type": "#microsoft.graph.fileAttachment", "id": "img1", "name": "image001.png", "contentType": "image/png", "size": 481, "isInline": True},
        {"@odata.type": "#microsoft.graph.fileAttachment", "id": "doc1", "name": name,
         "contentType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "size": 5089888, "isInline": False},
        {"@odata.type": "#microsoft.graph.itemAttachment", "id": "eml1", "name": "Costs", "contentType": None, "size": 900, "isInline": False},
        {"@odata.type": "#microsoft.graph.referenceAttachment", "id": "ref1", "name": "Budget.xlsx",
         "sourceUrl": "https://acme.sharepoint.com/sites/x/Budget.xlsx", "isInline": False},
    ]
    m = msg("m5", "erin.hall@acme.example", subject="FW: 1P Dismantling Plan - Review")
    m["hasAttachments"] = True
    log = []
    http = graph_with_attachments([m], attachments, {"doc1": _docx("Section 4: remove the UPS and PDUs.")}, log)
    poller = MailboxPoller(agent=agent, store=store, mailbox="eve@acme.example", app_id="a", app_password="p",
                           tenant_id="t", people=People(), owner_email="alex.owner@acme.example", http=http)
    await poller.tick()
    prompt = str(model.calls[0]["messages"])
    assert name in prompt and "read_attachment" in prompt and "image001" not in prompt and "SharePoint link" not in prompt
    assert "Budget.xlsx: https://acme.sharepoint.com/sites/x/Budget.xlsx" in prompt
    tools = AttachmentTools(store, mail_fetch=poller.fetch_attachment)
    ctx = CallContext("mail:conv-1", "aad-erin", [], actor_name="Erin")
    listed = _json.loads(await tools.call_tool("list_attachments", {}, ctx))["attachments"]
    assert [a["name"] for a in listed] == [name, "Costs"]
    doc = _json.loads(await tools.call_tool("read_attachment", {"name": "dismantling plan"}, ctx))
    assert "remove the UPS and PDUs" in doc["text"]
    item = _json.loads(await tools.call_tool("read_attachment", {"name": "Costs"}, ctx))
    assert "The cost model is attached" in item["text"] and "sam.taylor@acme.example" in item["text"]


async def test_email_attachments_outlive_the_12_hour_teams_window():
    from datetime import datetime, timedelta, timezone
    from agentcore.attachments import _fresh
    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    assert _fresh({"at": old, "source": "mail"}, datetime.now(timezone.utc))
    assert not _fresh({"at": old}, datetime.now(timezone.utc))


def test_a_forward_keeps_the_forwarded_email_and_its_links_a_reply_does_not():
    from agentcore.mailbox import email_body
    fwd = ("Please review this\n\nFrom: Sam Taylor <Sam.Taylor@acme.example>\nSent: Monday\n"
           "Subject: 1P Dismantling Plan\n\nPlan is here: https://acme.sharepoint.com/sites/PRJ/Plan.docx")
    assert "https://acme.sharepoint.com/sites/PRJ/Plan.docx" in email_body("FW: 1P Dismantling Plan - Review", fwd)
    assert "https://acme" in email_body("Fwd: x", fwd)
    assert email_body("Re: 1P Dismantling Plan - Review", fwd) == "Please review this"



def _addr(a):
    return {"emailAddress": {"address": a, "name": a}}


async def test_replies_go_to_everyone_on_the_thread(cfg):
    agent, model, store, _ = make_agent(cfg, ["Thanks all, noted."])
    log = []
    m = msg("m20", "erin.hall@acme.example")
    m["toRecipients"] = [_addr("ada@acme.example"), _addr("sam.taylor@acme.example")]
    m["ccRecipients"] = [_addr("david.b@acme.example.net")]
    poller = MailboxPoller(agent=agent, store=store, mailbox="ada@acme.example", app_id="a", app_password="p",
                           tenant_id="t", people=People(), owner_email="alex.owner@acme.example", http=graph([m], log))
    assert await poller.tick() == 1
    assert any(meth == "POST" and path.endswith("/m20/replyAll") for meth, path, _ in log)
    assert not any(path.endswith("/m20/reply") for _, path, _ in log)
    said = str(model.calls[0]["messages"])
    assert "goes to everyone on the thread: erin.hall@acme.example, sam.taylor@acme.example, david.b@acme.example.net" in said
    assert "ada@acme.example" not in said.split("goes to everyone")[1].split("Subject")[0]  # not to itself


async def test_people_outside_the_org_on_the_thread_are_left_off_and_named(cfg):
    agent, model, store, _ = make_agent(cfg, ["Thanks, see attached plan."])
    log = []
    m = msg("m21", "erin.hall@acme.example")
    m["toRecipients"] = [_addr("ada@acme.example")]
    m["ccRecipients"] = [_addr("sam.taylor@acme.example"), _addr("pm@contractor.com")]
    poller = MailboxPoller(agent=agent, store=store, mailbox="ada@acme.example", app_id="a", app_password="p",
                           tenant_id="t", people=People(), owner_email="alex.owner@acme.example", http=graph([m], log))
    assert await poller.tick() == 1
    create = next(b for meth, path, b in log if path.endswith("/m21/createReplyAll"))
    assert "Not copied to pm@contractor.com" in create["comment"]
    patch = next(b for meth, path, b in log if meth == "PATCH" and path.endswith("/draft-m21"))
    sent_to = [r["emailAddress"]["address"] for r in patch["toRecipients"] + patch["ccRecipients"]]
    assert sent_to == ["erin.hall@acme.example", "sam.taylor@acme.example"]  # no contractor, not itself
    assert any(meth == "POST" and path.endswith("/draft-m21/send") for meth, path, _ in log)
    assert "not to pm@contractor.com: outside Acme" in str(model.calls[0]["messages"])


async def test_an_email_cut_off_by_a_restart_is_handled_again(cfg):
    agent, model, store, _ = make_agent(cfg, ["Done."])
    log = []
    poller = MailboxPoller(agent=agent, store=store, mailbox="eve@acme.example", app_id="a", app_password="p",
                           tenant_id="t", people=People(), owner_email="alex.owner@acme.example",
                           http=graph([msg("m30", "erin.hall@acme.example")], log))
    assert await poller.tick() == 1
    assert await store.prefs_with_key("mail-working:eve@acme.example") == []  # finished: no mark left
    # a run that was killed mid-turn left its mark; the next start puts the email back
    await store.set_pref("mail:eve@acme.example", "m31", "working")
    await store.set_pref("m31", "mail-working:eve@acme.example", True)
    assert await poller.requeue_unfinished() == 1
    assert await store.get_pref("mail:eve@acme.example", "m31") is None
    assert any(meth == "PATCH" and path.endswith("/m31") and body == {"isRead": False} for meth, path, body in log)
    assert await store.prefs_with_key("mail-working:eve@acme.example") == []
