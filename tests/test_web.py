import json
from datetime import datetime, timezone

import httpx

from agentcore.config import WebConfig
from agentcore.store import MemoryStore
from agentcore.tools import CallContext
from agentcore.web import WebTools, html_text

CTX = CallContext("c1", "aad-a", [])
PAGE = b"""<!doctype html><html><head><title>Liquid cooling CDU</title><script>alert(1)</script></head>
<body><nav>Home | Products</nav><h1>CDU-800</h1><p>Cooling capacity 800&nbsp;kW at 35 &deg;C.</p>
<ul><li>Lead time 16 weeks</li></ul><footer>Contact</footer></body></html>"""


def make(handler, key="k", public=lambda h: h not in ("internal.local", "169.254.169.254", "10.0.0.5"), **cfg):
    async def resolver(host):
        return public(host)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return WebTools(MemoryStore(), WebConfig(**cfg), api_key=key, http=http, resolver=resolver,
                    clock=lambda: datetime(2026, 10, 8, tzinfo=timezone.utc))


async def call(tools, name, args):
    return json.loads(await tools.call_tool(name, args, CTX))


async def test_brave_search_returns_titles_links_snippets_and_counts_toward_the_daily_cap():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"web": {"results": [
            {"title": "CDU-800 datasheet", "url": "https://vendor.example/cdu", "description": "<strong>800 kW</strong> CDU",
             "age": "2 weeks ago"}]}})
    tools = make(handler, provider="brave", searches_per_day=1)
    out = await call(tools, "web_search", {"query": "CDU 800 kW lead time", "freshness": "month"})
    assert out["results"] == [{"title": "CDU-800 datasheet", "url": "https://vendor.example/cdu",
                               "snippet": "800 kW CDU", "age": "2 weeks ago"}]
    assert seen[0].headers["x-subscription-token"] == "k" and seen[0].url.params["freshness"] == "pm"
    assert "daily limit" in (await call(tools, "web_search", {"query": "again"}))["error"]


async def test_no_key_and_confidential_queries_are_refused_before_anything_is_sent():
    sent = []
    tools = make(lambda req: sent.append(req) or httpx.Response(200, json={}), key="", provider="brave")
    assert "isn't set up" in (await call(tools, "web_search", {"query": "transformer prices"}))["error"]
    tools = make(lambda req: sent.append(req) or httpx.Response(200, json={}))
    assert "Acme email" in (await call(tools, "web_search", {"query": "sam@acme.example proposal"}))["error"]
    assert sent == []


async def test_searxng_is_the_default_and_needs_no_key():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"results": [
            {"title": "EPP Air Quality", "url": "https://epa.tas.gov.au/epp", "content": "Design <b>criteria</b>",
             "publishedDate": "2022-01-24T00:00:00"}, {"title": "Second", "url": "https://b.example", "content": "x"}],
            "unresponsive_engines": []})
    tools = make(handler, key="", max_results=1)
    out = await call(tools, "web_search", {"query": "Tasmania air design criteria", "freshness": "year"})
    assert out["provider"] == "searxng" and out["results"] == [
        {"title": "EPP Air Quality", "url": "https://epa.tas.gov.au/epp", "snippet": "Design criteria",
         "age": "2022-01-24T00:00:00"}]
    q = seen[0].url
    assert (q.host, q.path) == ("agents-searxng", "/search")
    assert q.params["format"] == "json" and q.params["time_range"] == "year" and q.params["language"] == "en-AU"


async def test_searxng_says_when_the_engines_are_blocked_or_the_service_is_down():
    blocked = make(lambda req: httpx.Response(200, json={"results": [], "unresponsive_engines": [
        ["google", "CAPTCHA"], ["bing", "timeout"]]}), key="")
    assert "google" in (await call(blocked, "web_search", {"query": "q"}))["error"]

    def down(req):
        raise httpx.ConnectError("refused")
    assert "didn't answer" in (await call(make(down, key=""), "web_search", {"query": "q"}))["error"]
    empty = make(lambda req: httpx.Response(200, json={"results": []}), key="")
    assert (await call(empty, "web_search", {"query": "q"}))["results"] == []


async def test_tavily_and_exa_providers():
    def handler(req):
        if req.url.host == "api.tavily.com":
            assert json.loads(req.content)["time_range"] == "week"
            return httpx.Response(200, json={"results": [{"title": "T", "url": "https://t.example", "content": "c"}]})
        assert req.headers["x-api-key"] == "k" and "startPublishedDate" in json.loads(req.content)
        return httpx.Response(200, json={"results": [{"title": "E", "url": "https://e.example", "highlights": ["h1", "h2"]}]})
    assert (await call(make(handler, provider="tavily"), "web_search", {"query": "q", "freshness": "week"}))["results"][0]["title"] == "T"
    assert (await call(make(handler, provider="exa"), "web_search", {"query": "q", "freshness": "day"}))["results"][0]["snippet"] == "h1 … h2"


async def test_read_web_page_gives_readable_text_and_follows_public_redirects():
    def handler(req):
        if req.url.path == "/old":
            return httpx.Response(301, headers={"location": "/cdu"})
        return httpx.Response(200, content=PAGE, headers={"content-type": "text/html; charset=utf-8"})
    out = await call(make(handler), "read_web_page", {"url": "https://vendor.example/old"})
    assert out["url"] == "https://vendor.example/cdu" and out["title"] == "Liquid cooling CDU"
    assert "Cooling capacity 800 kW at 35 °C." in out["text"] and "- Lead time 16 weeks" in out["text"]
    assert "alert" not in out["text"] and "Home | Products" not in out["text"]


async def test_private_addresses_and_redirects_into_the_network_are_refused():
    def handler(req):
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
    tools = make(handler)
    assert "isn't a public" in (await call(tools, "read_web_page", {"url": "https://vendor.example/x"}))["error"]
    assert "isn't a public" in (await call(tools, "read_web_page", {"url": "http://internal.local/admin"}))["error"]
    assert "only public" in (await call(tools, "read_web_page", {"url": "file:///etc/passwd"}))["error"]
    assert "login" in (await call(tools, "read_web_page", {"url": "https://user:pw@vendor.example/"}))["error"]


def test_html_text_keeps_headings_and_drops_scripts():
    title, text = html_text(PAGE.decode())
    assert title == "Liquid cooling CDU" and text.startswith("# CDU-800")
