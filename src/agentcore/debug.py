"""Live debug stream: everything an agent does, for the debug console on the apps site.

Each agent keeps the last BUFFER events in memory (nothing is persisted) and streams new ones to websocket
viewers at /{agent}/debug/ws. Events:

  turn.start  {channel, user, text, verified}         a message (chat, voice, email, scheduled task) arrives
  memory      {chars, text}                           what Hindsight recalled for it
  triage      {action, tier, ack, text, ms, cost}     the first responder's decision
  route       {tier, model, reasons}                  the model chosen for the work
  model       {model, tier, in, out, ms, cost, stop, text, reasoning, tool_uses}   one model call; `text`
                                                      alongside tool uses is the model thinking out loud
  tool.call   {name, args}  /  tool.result {name, status, ms, content}
  turn.end    {tier, cost, ms, tools, reply, suppressed}
  log         {level, logger, msg}                    every log line from the agent process (INFO and up)

Events in a turn carry the same `turn` id, so the console can fold a turn into one line and expand it.

Who can watch: a viewer signs in to the apps site (Cognito, federated to Entra ID). CloudFront forwards
the HttpOnly `CognitoToken` cookie on the websocket upgrade (behaviour `/*/debug/ws`), and the agent checks
it here: signature (Cognito JWKS), issuer, audience, token_use=id, expiry, an organisation email and the viewer
allow-list (DEBUG_VIEWERS, comma separated emails or `*@example.com`; default the agent's owner). The
stream holds other people's private conversations, so keep the list short. Secrets are masked before an
event is stored.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import re
import time
import uuid
from collections import deque
from contextvars import ContextVar

import httpx

from .org import org
import jwt

BUFFER = 3000
MAX_STR = 8000
QUEUE = 1000
_TURN: ContextVar[str | None] = ContextVar("debug_turn", default=None)
_CONV: ContextVar[str | None] = ContextVar("debug_conv", default=None)

SECRET_PATTERNS = [
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}"), "Bearer [redacted]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[jwt redacted]"),
    (re.compile(r"\b(sk|xi|pk|rk)[-_][A-Za-z0-9_-]{16,}"), "[key redacted]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[aws key redacted]"),
    (re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|access[_-]?token|client[_-]?secret|token)"
                r"(\"?'?\s*[:=]\s*\"?'?)([^\s\"',}&]{8,})"), r"\1\2[redacted]"),
    (re.compile(r"(?i)([?&](?:sig|token|code|key)=)[^&\s\"']{8,}"), r"\1[redacted]"),
]


def redact(value, depth: int = 0):
    """Mask anything that looks like a secret and cut very long strings, recursively."""
    if isinstance(value, str):
        for pattern, repl in SECRET_PATTERNS:
            value = pattern.sub(repl, value)
        return value if len(value) <= MAX_STR else value[:MAX_STR] + f"… [{len(value) - MAX_STR} more chars]"
    if depth > 6:
        return "…"
    if isinstance(value, dict):
        return {k: ("[redacted]" if re.search(r"(?i)secret|password|token|api_?key", str(k)) and isinstance(v, str)
                    else redact(v, depth + 1)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, depth + 1) for v in list(value)[:200]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact(str(value), depth)


class DebugBus:
    def __init__(self, agent: str = "", size: int = BUFFER):
        self.agent = agent
        self.events: deque[dict] = deque(maxlen=size)
        self.subscribers: set[asyncio.Queue] = set()
        self._ids = itertools.count(1)
        self.boot = uuid.uuid4().hex[:8]  # event ids restart with the process; viewers resume only within a boot

    def emit(self, kind: str, **data) -> dict:
        event = {"id": next(self._ids), "ts": round(time.time(), 3), "agent": self.agent, "kind": kind,
                 "turn": _TURN.get(), "conv": _CONV.get(), **redact(data)}
        self.events.append(event)
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:  # a stalled viewer loses events; it never slows the agent
                pass
        return event

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def backlog(self, since: int = 0, limit: int = BUFFER) -> list[dict]:
        return [e for e in self.events if e["id"] > since][-limit:]


BUS = DebugBus()


def emit(kind: str, **data) -> None:
    try:
        BUS.emit(kind, **data)
    except Exception:  # debugging must never break a turn
        pass


def start_turn(conversation_id: str) -> tuple:
    """Give the current task a new turn id (events emitted in it are grouped). Returns reset tokens."""
    return _TURN.set(uuid.uuid4().hex[:10]), _CONV.set(conversation_id)


def end_turn(tokens: tuple) -> None:
    _TURN.reset(tokens[0])
    _CONV.reset(tokens[1])


class BusLogHandler(logging.Handler):
    """Copies log records into the stream (INFO and up), tagged with the turn they happened in."""

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(("uvicorn.access", "httpx", "httpcore")):
            return
        try:
            msg = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                msg += f"\n{type(record.exc_info[1]).__name__}: {record.exc_info[1]}"
            BUS.emit("log", level=record.levelname, logger=record.name, msg=msg)
        except Exception:
            pass


def install(agent: str) -> None:
    BUS.agent = agent
    root = logging.getLogger()
    if not any(isinstance(h, BusLogHandler) for h in root.handlers):
        handler = BusLogHandler(level=logging.INFO)
        root.addHandler(handler)


class AuthError(Exception):
    pass


def viewer_allowed(email: str, viewers: list[str]) -> bool:
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return False
    for v in viewers:
        v = v.strip().lower()
        if v == email or (v.startswith("*@") and email.endswith(v[1:])):
            return True
    return False


class CognitoVerifier:
    """Checks a Cognito id token (apps site sign-in) and the viewer allow-list."""

    def __init__(self, *, region: str, pool_id: str, client_id: str, viewers: list[str],
                 domains: tuple[str, ...] | None = None, http: httpx.AsyncClient | None = None, clock=time.time):
        self.issuer = f"https://cognito-idp.{region}.amazonaws.com/{pool_id}"
        self.client_id, self.viewers = client_id, viewers
        self.domains = tuple(domains) if domains is not None else tuple(org().internal_domains)
        self.http = http or httpx.AsyncClient(timeout=10)
        self.clock = clock
        self._jwks: tuple[dict, float] | None = None

    async def _key(self, kid: str):
        if not self._jwks or self._jwks[1] < self.clock() or kid not in self._jwks[0]:
            r = await self.http.get(f"{self.issuer}/.well-known/jwks.json")
            r.raise_for_status()
            keys = {k["kid"]: jwt.algorithms.RSAAlgorithm.from_jwk(k) for k in r.json().get("keys", [])}
            self._jwks = (keys, self.clock() + 3600)
        key = self._jwks[0].get(kid)
        if key is None:
            raise AuthError("unknown signing key")
        return key

    async def verify(self, token: str | None) -> dict:
        if not token:
            raise AuthError("not signed in")
        try:
            kid = jwt.get_unverified_header(token).get("kid", "")
            claims = jwt.decode(token, await self._key(kid), algorithms=["RS256"], audience=self.client_id,
                                issuer=self.issuer, options={"require": ["exp", "iss", "aud"]})
        except AuthError:
            raise
        except Exception as exc:
            raise AuthError(f"invalid token ({type(exc).__name__})") from None
        if claims.get("token_use") != "id":
            raise AuthError("not an id token")
        email = (claims.get("email") or "").lower()
        if not any(email.endswith("@" + d) for d in self.domains):
            raise AuthError(f"not a {org().name} account")
        if not viewer_allowed(email, self.viewers):
            raise AuthError(f"{email} is not on this agent's debug viewer list")
        return claims


async def serve(ws, *, verifier: CognitoVerifier | None, origins: list[str], internal_token: str = "",
                cookie: str = "CognitoToken", audit=None, bus: DebugBus | None = None, ping_s: float = 20) -> None:
    """One viewer: authenticate, send the backlog, then stream live events until they disconnect.

    Auth is either the apps site sign-in (cookie, Origin must be one of `origins`) or, for tooling
    inside the organisation, `Authorization: Bearer <INTERNAL_API_TOKEN>`. Refused connections are closed before accept
    (the browser sees a failed handshake; the reason is only logged)."""
    import hmac

    bus = bus or BUS
    viewer = None
    bearer = (ws.headers.get("authorization") or "").removeprefix("Bearer ").strip()
    if internal_token and bearer and hmac.compare_digest(bearer, internal_token):
        viewer = "internal"
    else:
        origin = ws.headers.get("origin", "")
        try:
            if origin not in origins:
                raise AuthError(f"origin {origin or '(none)'} not allowed")
            if verifier is None:
                raise AuthError("debug sign-in not configured")
            viewer = (await verifier.verify(ws.cookies.get(cookie))).get("email")
        except AuthError as exc:
            logging.getLogger(__name__).warning("debug viewer refused: %s", exc)
            await ws.close(code=1008)
            return
    await ws.accept()
    if audit:
        await audit({"actor": viewer, "action": "debug_view", "target": bus.agent})
    logging.getLogger(__name__).info("debug viewer connected: %s", viewer)
    q = bus.subscribe()
    try:
        since = int(ws.query_params.get("since") or 0) if ws.query_params.get("boot") == bus.boot else 0
        await ws.send_json({"kind": "hello", "agent": bus.agent, "viewer": viewer, "buffer": BUFFER, "boot": bus.boot})
        for event in bus.backlog(since):
            await ws.send_json(event)
        await ws.send_json({"kind": "live", "agent": bus.agent})

        async def drain_client():  # we don't take commands; this notices the disconnect
            while True:
                await ws.receive_text()

        reader = asyncio.create_task(drain_client())
        try:
            while not reader.done():
                try:
                    event = await asyncio.wait_for(q.get(), timeout=ping_s)
                except asyncio.TimeoutError:
                    event = {"kind": "ping", "agent": bus.agent, "ts": round(time.time(), 3)}
                await ws.send_json(event)
        finally:
            reader.cancel()
    except Exception:
        pass  # viewer went away
    finally:
        bus.unsubscribe(q)
        logging.getLogger(__name__).info("debug viewer disconnected: %s", viewer)
