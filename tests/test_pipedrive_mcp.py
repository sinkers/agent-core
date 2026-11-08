"""Pipedrive (read-only) for Sage: deals with notes and activities, search, organisations."""

import httpx
import pytest

from agentcore_mcp_pipedrive.server import PipedriveClient, PipedriveError, PipedriveTools, _plain


def tools_with(routes: dict):
    seen = []

    def handler(req):
        seen.append((req.url.path, dict(req.url.params), req.headers.get("x-api-token")))
        for key, body in routes.items():
            if req.url.path.endswith(key):
                return httpx.Response(200, json=body)
        return httpx.Response(404, json={"success": False, "error": "not found"})
    client = PipedriveClient("tok", "acme", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return PipedriveTools(client), seen


async def test_a_deal_comes_with_its_customer_notes_and_activities():
    tools, seen = tools_with({
        "/v2/deals/7": {"success": True, "data": {"id": 7, "title": "GPU hall 2 MW", "value": 1200000, "currency": "AUD",
                                                  "status": "open", "org_id": 3, "person_id": 9}},
        "/v2/organizations/3": {"success": True, "data": {"id": 3, "name": "Acme AI"}},
        "/v2/persons/9": {"success": True, "data": {"id": 9, "name": "Jo Buyer", "job_title": "CTO"}},
        "/v1/notes": {"success": True, "data": [{"add_time": "2026-10-01", "user": {"name": "Sam"},
                                                 "content": "<p>Worried about <b>delivery time</b></p><p>Needs 2026 Q2</p>"}]},
        "/v2/activities": {"success": True, "data": [{"type": "call", "subject": "Intro", "done": True}]},
    })
    d = await tools.get_deal(7)
    assert d["title"] == "GPU hall 2 MW" and d["organization"]["name"] == "Acme AI" and d["person"]["job_title"] == "CTO"
    assert d["notes"][0]["text"] == "Worried about delivery time\nNeeds 2026 Q2"
    assert d["url"] == "https://acme.pipedrive.com/deal/7" and all(tok == "tok" for _, _, tok in seen)


async def test_search_and_errors():
    tools, seen = tools_with({"/v2/itemSearch": {"success": True, "data": {"items": [
        {"result_score": 0.9, "item": {"type": "deal", "id": 7, "title": "GPU hall 2 MW"}}]}}})
    out = await tools.search("GPU hall", ["deal"])
    assert out["results"] == [{"type": "deal", "id": 7, "title": "GPU hall 2 MW", "score": 0.9}]
    assert seen[0][1]["item_types"] == "deal"
    with pytest.raises(PipedriveError, match="2 characters"):
        await tools.search("x")
    with pytest.raises(PipedriveError, match="404"):
        await tools.get_person(1)


def test_notes_are_plain_text():
    assert _plain("a<br>b&nbsp;c<script>x</script>") == "a\nb cx"
