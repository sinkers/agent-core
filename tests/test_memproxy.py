"""memory-proxy: per-agent tokens pinned to one bank, and only the allow-listed routes reach Hindsight."""

import hashlib
import json

import httpx
import pytest

from agentcore.memproxy import create_app, load_grants

SEEN: list[tuple[str, str, str]] = []


def upstream(request: httpx.Request) -> httpx.Response:
    SEEN.append((request.method, request.url.path, request.headers.get("authorization", "")))
    return httpx.Response(200, json={"ok": True, "path": request.url.path})


@pytest.fixture
def client():
    SEEN.clear()
    grants = load_grants(json.dumps([
        {"name": "probe", "bank": "org-probe", "sha256": hashlib.sha256(b"tok-probe").hexdigest()},
        {"name": "ada", "bank": "org-ada", "sha256": hashlib.sha256(b"tok-ada").hexdigest()},
    ]))
    app = create_app(upstream="http://hs", upstream_key="upstream-secret", grants=grants,
                     transport=httpx.MockTransport(upstream))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://px")


def auth(tok):
    return {"Authorization": f"Bearer {tok}"}


async def test_own_bank_routes_pass_with_the_upstream_key(client):
    for method, path in [("PUT", ""), ("POST", "/memories"), ("POST", "/memories/recall"),
                         ("GET", "/directives"), ("POST", "/directives"), ("GET", "/stats")]:
        r = await client.request(method, f"/v1/default/banks/org-probe{path}", headers=auth("tok-probe"), json={})
        assert r.status_code == 200, (method, path)
    assert all(a == "Bearer upstream-secret" for _, _, a in SEEN)
    assert {p for _, p, _ in SEEN} >= {"/v1/default/banks/org-probe/memories/recall"}


async def test_me_means_the_tokens_own_bank(client):
    r = await client.post("/v1/default/banks/me/memories/recall", headers=auth("tok-ada"), json={"query": "x"})
    assert r.status_code == 200 and SEEN[-1][1] == "/v1/default/banks/org-ada/memories/recall"


async def test_another_agents_bank_is_refused(client):
    r = await client.post("/v1/default/banks/org-ada/memories/recall", headers=auth("tok-probe"), json={})
    assert r.status_code == 403 and SEEN == []


@pytest.mark.parametrize("method,path", [
    ("DELETE", "/v1/default/banks/org-probe"),
    ("DELETE", "/v1/default/banks/org-probe/memories"),
    ("GET", "/v1/default/banks"),
    ("PATCH", "/v1/default/banks/org-probe/config"),
    ("POST", "/v1/default/banks/org-probe/webhooks"),
    ("GET", "/v1/default/banks/org-probe/export"),
    ("POST", "/v1/default/banks/org-probe/import"),
    ("GET", "/v1/default/banks/org-probe/documents"),
    ("GET", "/v1/default/chunks/abc"),
    ("POST", "/mcp"),
    ("GET", "/metrics"),
    ("POST", "/v1/default/banks/org-probe/memories/../../org-ada/memories/recall"),
])
async def test_routes_off_the_allow_list_are_refused(client, method, path):
    r = await client.request(method, path, headers=auth("tok-probe"))
    assert r.status_code in (403, 404) and SEEN == []


async def test_no_or_unknown_token_is_401(client):
    assert (await client.post("/v1/default/banks/org-probe/memories/recall", json={})).status_code == 401
    r = await client.post("/v1/default/banks/org-probe/memories/recall", headers=auth("nope"), json={})
    assert r.status_code == 401 and SEEN == []


async def test_oversized_body_is_refused(client):
    r = await client.post("/v1/default/banks/org-probe/memories", headers=auth("tok-probe"), content=b"x" * (2 << 20))
    assert r.status_code == 413 and SEEN == []


def test_pool_carrying_one_agents_credentials_cannot_be_used_by_another():
    from agentcore.frontdoor.config import FrontDoorConfig
    base = {"pools": {"p": {"image": "i", "providers": ["memory-probe"], "only_for": ["probe"]}}}
    FrontDoorConfig.model_validate({**base, "agents": {"probe": {"image": "i", "pool": "p", "providers": ["memory-probe"]}}})
    with pytest.raises(ValueError, match="may not use pool"):
        FrontDoorConfig.model_validate({**base, "agents": {"other": {"image": "i", "pool": "p", "providers": ["memory-probe"]}}})


def test_memory_client_sends_the_agents_own_token(monkeypatch, cfg):
    from agentcore.memory import memory_from_config
    cfg.memory.enabled = True
    monkeypatch.setenv("HINDSIGHT_URL", "http://memory-proxy:8080")
    monkeypatch.setenv("HINDSIGHT_API_KEY", "tok-probe")
    m = memory_from_config(cfg)
    assert m.http.headers["authorization"] == "Bearer tok-probe"
    monkeypatch.delenv("HINDSIGHT_URL")
    assert memory_from_config(cfg) is None


async def test_chat_footer_can_be_turned_off(cfg):
    from agentcore.agent import Turn
    from agentcore.testing import make_agent
    from .conftest import SKILLS
    for on in (True, False):
        cfg.agent.chat_footer = on
        agent, *_ = make_agent(cfg, ["Hello there."], skills_dir=SKILLS)
        reply = (await agent.handle(Turn(f"test-footer-{on}", "hi", "u1", "Uma"))).reply
        assert ("is an AI agent" in reply) is on
