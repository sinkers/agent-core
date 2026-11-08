import pytest

from agentcore_mcp_mail.server import MailTools


class FakeMailer:
    sender = "dan@acme.example"

    def __init__(self):
        self.sent = []

    async def send(self, to, subject, body, footer, cc=None):
        self.sent.append((to, subject, body, footer))


async def test_internal_send_adds_footer():
    m = FakeMailer()
    out = await MailTools(m, "Dan (AI) is an AI agent.", {"acme.example"}).send_email(["Noah@Acme.Example"], "Hi", "Body")
    assert out["to"] == ["Noah@Acme.Example"] and m.sent[0][3] == "Dan (AI) is an AI agent."


async def test_external_refused_on_internal_tool_but_allowed_on_external_tool():
    m = FakeMailer()
    tools = MailTools(m, "f", {"acme.example"})
    out = await tools.send_email(["noah@acme.example", "someone@gmail.com"], "s", "b")
    assert out["sent"] is False and "send_email_external" in out["reason"] and "find_person" in out["reason"]
    assert m.sent == []
    await tools.send_email_external(["someone@gmail.com"], "s", "b")
    assert m.sent[0][0] == ["someone@gmail.com"]


@pytest.mark.parametrize("bad", ["", "not-an-email", "a@b", "x <y@acme.example>"])
async def test_bad_addresses(bad):
    out = await MailTools(FakeMailer(), "f", {"acme.example"}).send_email([bad] if bad else [], "s", "b")
    assert out["sent"] is False


async def test_guessed_tenant_domain_is_refused_with_a_lookup_hint():
    """Ada guessed alex.owner@acme.com (from acme.sharepoint.com) on 2 Oct 2026."""
    out = await MailTools(FakeMailer(), "f", {"acme.example"}).send_email(["alex.owner@acme.com"], "s", "b")
    assert out["sent"] is False and "never guess" in out["reason"]


async def test_graph_send_failure_is_reported_not_raised():
    class Failing(FakeMailer):
        async def send(self, *a, **k):
            raise RuntimeError("sendMail failed 403: denied")
    out = await MailTools(Failing(), "f", {"acme.example"}).send_email(["noah@acme.example"], "s", "b")
    assert out == {"sent": False, "reason": "sendMail failed 403: denied"}


class CcMailer(FakeMailer):
    async def send(self, to, subject, body, footer, cc=None):
        self.sent.append((to, subject, body, footer, cc))


async def test_cc_is_sent_and_checked_like_to():
    m = CcMailer()
    tools = MailTools(m, "f", {"acme.example"})
    out = await tools.send_email(["noah@acme.example", "sam@acme.example"], "s", "b", cc=["alex.owner@acme.example"])
    assert out["to"] == ["noah@acme.example", "sam@acme.example"] and out["cc"] == ["alex.owner@acme.example"]
    assert m.sent[0][4] == ["alex.owner@acme.example"]
    refused = await tools.send_email(["noah@acme.example"], "s", "b", cc=["someone@gmail.com"])
    assert refused["sent"] is False and "send_email_external" in refused["reason"] and len(m.sent) == 1


def test_recipients_are_not_repeated_and_the_rule_is_in_every_send_tool():
    from agentcore.files import TOOLS
    from agentcore.mail import RECIPIENT_RULES, unique_recipients
    assert unique_recipients(["A@acme.example", "a@acme.example", "b@acme.example"],
                             ["B@acme.example", "c@acme.example", "ada@acme.example", "c@acme.example"],
                             "ada@acme.example") == (["A@acme.example", "b@acme.example"], ["c@acme.example"])
    assert "cc the person who asked" in RECIPIENT_RULES and "one email" in RECIPIENT_RULES
    email_file = next(t for t in TOOLS if t["name"] == "email_file")
    assert RECIPIENT_RULES in email_file["description"] and "cc" in email_file["inputSchema"]["properties"]


async def test_graph_message_carries_cc_without_repeats():
    import httpx
    from agentcore.mail import GraphMailer
    seen = []

    def handler(request):
        if "oauth2" in str(request.url):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        seen.append(__import__("json").loads(request.content))
        return httpx.Response(202)
    mailer = GraphMailer("a", "p", "t", "ada@acme.example", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await mailer.send(["noah@acme.example"], "s", "b", "f", cc=["alex@acme.example", "Noah@acme.example"])
    msg = seen[0]["message"]
    assert [r["emailAddress"]["address"] for r in msg["ccRecipients"]] == ["alex@acme.example"]
    await mailer.send(["noah@acme.example"], "s", "b", "f")
    assert "ccRecipients" not in seen[1]["message"]
