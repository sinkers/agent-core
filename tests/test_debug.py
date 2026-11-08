import asyncio
import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from agentcore import debug
from agentcore.debug import AuthError, CognitoVerifier, DebugBus, redact, viewer_allowed

POOL, CLIENT = "ap-southeast-2_TEST", "client-123"
ISS = f"https://cognito-idp.ap-southeast-2.amazonaws.com/{POOL}"


def test_redact_masks_secrets_but_keeps_content():
    out = redact({"text": "use Bearer abcdefghijklmnop1234 and sk-ant-0123456789abcdefghij",
                  "api_key": "plainvalue123", "args": {"key": "PRJ-328"},
                  "url": "https://x.blob.core.windows.net/a.zip?sv=1&sig=abcdefghijklmno"})
    assert "abcdefghijklmnop1234" not in out["text"] and "sk-ant-0123" not in out["text"]
    assert out["api_key"] == "[redacted]"
    assert out["args"] == {"key": "PRJ-328"}  # Jira keys are not secrets
    assert "abcdefghijklmno" not in out["url"]
    assert len(redact("x" * 20000)) < 9000


def test_bus_groups_events_by_turn_and_streams_to_subscribers():
    bus = DebugBus("ada")
    q = bus.subscribe()
    tokens = debug.start_turn("chat-1")
    bus.emit("turn.start", text="hi")
    bus.emit("model", text="thinking")
    debug.end_turn(tokens)
    bus.emit("log", msg="outside")
    a, b, c = bus.backlog()
    assert a["turn"] == b["turn"] and a["turn"] and a["conv"] == "chat-1"
    assert c["turn"] is None
    assert q.qsize() == 3
    assert [e["id"] for e in bus.backlog(since=a["id"])] == [b["id"], c["id"]]


def test_slow_viewer_never_blocks_the_agent():
    bus = DebugBus("x")
    bus.subscribe()
    for i in range(debug.QUEUE + 50):
        bus.emit("log", msg=str(i))  # must not raise


def test_viewer_allow_list():
    assert viewer_allowed("Alex.Owner@acme.example", ["alex.owner@acme.example"])
    assert viewer_allowed("erin@acme.example", ["*@acme.example"])
    assert not viewer_allowed("erin@acme.example", ["alex.owner@acme.example"])
    assert not viewer_allowed("eve@evil.com", ["*@acme.example"])


@pytest.fixture
def keys():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk["kid"] = "k1"
    return key, jwk


def _token(key, **over):
    claims = {"iss": ISS, "aud": CLIENT, "token_use": "id", "email": "alex.owner@acme.example",
              "exp": int(time.time()) + 600, **over}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})


def _verifier(jwk, viewers=("alex.owner@acme.example",)):
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"keys": [jwk]})))
    return CognitoVerifier(region="ap-southeast-2", pool_id=POOL, client_id=CLIENT, viewers=list(viewers), http=http)


async def test_verifier_accepts_good_tokens_and_refuses_the_rest(keys):
    key, jwk = keys
    v = _verifier(jwk)
    assert (await v.verify(_token(key)))["email"] == "alex.owner@acme.example"
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    for bad in [None, "junk", _token(other), _token(key, aud="other"), _token(key, iss="https://evil"),
                _token(key, token_use="access"), _token(key, exp=int(time.time()) - 10),
                _token(key, email="eve@evil.com"), _token(key, email="erin@acme.example")]:
        with pytest.raises(AuthError):
            await v.verify(bad)


def _app(verifier, bus):
    from fastapi import FastAPI, WebSocket
    app = FastAPI()

    @app.websocket("/ada/debug/ws")
    async def ws_route(ws: WebSocket):
        await debug.serve(ws, verifier=verifier, origins=["https://apps.example.com"], internal_token="int-tok",
                          bus=bus, ping_s=0.05)
    return app


def test_socket_streams_backlog_to_signed_in_viewer_and_refuses_others(keys):
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    key, jwk = keys
    bus = DebugBus("ada")
    bus.emit("log", msg="before")
    client = TestClient(_app(_verifier(jwk), bus))
    with client.websocket_connect("/ada/debug/ws", headers={"origin": "https://apps.example.com",
                                                               "cookie": f"CognitoToken={_token(key)}"}) as ws:
        hello = ws.receive_json()
        assert hello["kind"] == "hello" and hello["viewer"] == "alex.owner@acme.example"
        assert ws.receive_json()["msg"] == "before"
        assert ws.receive_json()["kind"] == "live"
    with client.websocket_connect("/ada/debug/ws", headers={"authorization": "Bearer int-tok"}) as ws:
        hello = ws.receive_json()
        assert hello["viewer"] == "internal"
    bus.emit("log", msg="after")
    # resuming within the same boot sends only newer events; a stale boot gets everything
    last = bus.backlog()[0]["id"]
    with client.websocket_connect(f"/ada/debug/ws?since={last}&boot={hello['boot']}",
                                  headers={"authorization": "Bearer int-tok"}) as ws:
        ws.receive_json()
        assert ws.receive_json()["msg"] == "after"
    with client.websocket_connect(f"/ada/debug/ws?since={last}&boot=old",
                                  headers={"authorization": "Bearer int-tok"}) as ws:
        ws.receive_json()
        assert ws.receive_json()["msg"] == "before"
    for headers in [{"origin": "https://evil.example", "cookie": f"CognitoToken={_token(key)}"},
                    {"origin": "https://apps.example.com"},
                    {"origin": "https://apps.example.com", "cookie": f"CognitoToken={_token(key, email='erin@acme.example')}"},
                    {"authorization": "Bearer wrong"}]:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ada/debug/ws", headers=headers) as ws:
                ws.receive_json()


async def test_agent_turn_shows_as_one_group_with_thinking_and_tools(cfg):
    from agentcore.agent import Turn
    from .conftest import FakeMcp, make_agent
    mcp = FakeMcp(["search_docs"])
    agent, _, _, tools = make_agent(cfg, [[("openclaw__search_docs", {"q": "PRJ"})], "found it"], mcp)
    await tools.discover()
    start = debug.BUS.backlog()[-1]["id"] if debug.BUS.events else 0
    await agent.handle(Turn("test-debug", "find PRJ docs", "u1", "Alex"))
    events = [e for e in debug.BUS.backlog(since=start) if e["conv"] == "test-debug"]
    kinds = [e["kind"] for e in events]
    assert kinds[0] == "turn.start" and kinds[-1] == "turn.end"
    assert {"route", "model", "tool.call", "tool.result"} <= set(kinds)
    assert len({e["turn"] for e in events}) == 1
    call = next(e for e in events if e["kind"] == "tool.call")
    assert call["args"] == {"q": "PRJ"}
    assert next(e for e in events if e["kind"] == "turn.end")["tools"] == ["openclaw__search_docs"]
