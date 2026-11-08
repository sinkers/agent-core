import httpx

from agentcore.emailfmt import markdown_to_html
from agentcore.mail import GraphMailer


def test_markdown_becomes_formatted_email_with_inline_styles():
    out = markdown_to_html("## PRJ\n\n**Due Friday**\n\n- one\n- two\n\n| Item | Owner |\n|---|---|\n| UPS | Sam |\n\n"
                           "See <https://acme.sharepoint.com/x> or [the plan](https://acme.sharepoint.com/p).",
                           "Ada (AI) is an AI agent. Owner: A. Owner.")
    assert '<h2 style="' in out and "<strong>Due Friday</strong>" in out and "<li>one</li>" in out
    assert '<th style="border:1px solid' in out and "<td" in out
    assert 'href="https://acme.sharepoint.com/x"' in out and 'href="https://acme.sharepoint.com/p"' in out
    assert out.startswith('<div style="font-family:Segoe UI') and "<em>Ada (AI) is an AI agent. Owner: A. Owner.</em>" in out


def test_raw_html_and_script_links_cannot_get_in():
    out = markdown_to_html('<script>alert(1)</script> <img src="https://track.example/p.gif"> '
                           '[click](javascript:alert(1)) <b onclick="x">hi</b> ![logo](https://track.example/q.gif)')
    assert "<script" not in out and "<img" not in out and "<b " not in out and 'href="javascript' not in out
    assert "&lt;script&gt;" in out  # shown as text, never run
    assert '<a href="https://track.example/q.gif">logo</a>' in out  # markdown image -> plain link


async def test_graph_mailer_sends_html():
    seen = {}

    def handler(req):
        if "login.microsoftonline.com" in str(req.url):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        import json
        seen.update(json.loads(req.content))
        return httpx.Response(202)
    m = GraphMailer("a", "p", "t", "ada@acme.example", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await m.send(["alex.owner@acme.example"], "PRJ", "**Status:** on track", "Ada (AI) is an AI agent.")
    body = seen["message"]["body"]
    assert body["contentType"] == "HTML" and "<strong>Status:</strong> on track" in body["content"]
    assert "Ada (AI) is an AI agent." in body["content"]
