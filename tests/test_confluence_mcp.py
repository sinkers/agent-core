import json

import httpx
import pytest

from agentcore_mcp_confluence.server import ConfluenceClient, ConfluenceTools


def make(handler):
    def route(req):
        if req.url.host == "auth.atlassian.com":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return handler(req)
    return ConfluenceTools(ConfluenceClient("i", "s", "cloud", "https://site.atlassian.net",
                                            http=httpx.AsyncClient(transport=httpx.MockTransport(route))))


def adf_page(content, version=3):
    return {"id": "42", "title": "PRJ notes", "spaceId": "7", "version": {"number": version},
            "_links": {"webui": "/spaces/AU/pages/42"},
            "body": {"atlas_doc_format": {"value": json.dumps({"type": "doc", "version": 1, "content": content})}}}


async def test_search_words_builds_cql_and_links():
    seen = {}

    def h(req):
        seen["cql"] = req.url.params["cql"]
        return httpx.Response(200, json={"results": [{"content": {"id": "42", "title": "PRJ notes"},
                                                      "url": "/spaces/AU/pages/42", "excerpt": "@@@hl@@@PRJ@@@endhl@@@ due"}]})

    out = await make(h).search_pages("PRJ notes", space_key="AU")
    assert seen["cql"].startswith('space="AU" and (type=page and (title ~ "PRJ notes"')
    assert out["results"][0]["url"] == "https://site.atlassian.net/wiki/spaces/AU/pages/42"
    assert out["results"][0]["excerpt"] == "PRJ due"


async def test_update_appends_and_bumps_version():
    put = {}

    def h(req):
        if req.method == "GET":
            return httpx.Response(200, json=adf_page([{"type": "paragraph", "content": [{"type": "text", "text": "old"}]}]))
        put.update(json.loads(req.content))
        return httpx.Response(200, json={"_links": {"webui": "/spaces/AU/pages/42"}})

    out = await make(h).update_page("42", "- [ ] new action")
    body = json.loads(put["body"]["value"])
    assert [n["type"] for n in body["content"]] == ["paragraph", "taskList"]  # old kept, new appended
    assert put["version"]["number"] == 4 and out["mode"] == "append"


async def test_update_rejects_bad_mode():
    with pytest.raises(Exception, match="mode"):
        await make(lambda r: httpx.Response(200, json=adf_page([]))).update_page("42", "x", mode="delete")


async def test_get_page_returns_text():
    out = await make(lambda r: httpx.Response(200, json=adf_page([{"type": "paragraph", "content": [{"type": "text", "text": "Hello"}]}]))).get_page("42")
    assert out["text"] == "Hello" and out["url"].endswith("/wiki/spaces/AU/pages/42")
