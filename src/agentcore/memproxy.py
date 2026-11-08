"""memory-proxy: Hindsight behind per-agent credentials, each pinned to one memory bank.

Hindsight has one API key for everything and lets a caller name any bank, so on its own any agent could read or delete
any other agent's memories. This proxy sits in front:

  * each agent has its own bearer token; only its SHA-256 is stored here (MEMPROXY_TOKENS, JSON
    [{"name": "probe", "bank": "org-probe", "sha256": "..."}]);
  * the token decides the bank: the path's bank must be that bank (or `me`), anything else is 403;
  * only an allow-list of routes passes (retain, recall, directives, bank setup, stats); deletes, bank listing,
    config, webhooks, import/export, documents, files, MCP and everything else are refused;
  * the upstream key (MEMPROXY_UPSTREAM_KEY) is added here and never seen by clients.

The paths are Hindsight's own (`/v1/default/banks/{bank}/...`), so any Hindsight client works by pointing it at the
proxy with its agent's token as the API key and a fixed bank id (OpenClaw's plugin: `dynamicBankId: false`).

Run: uvicorn --factory agentcore.memproxy:app --host 0.0.0.0 --port 8080
Environment: MEMPROXY_UPSTREAM (e.g. http://hindsight:8888), MEMPROXY_UPSTREAM_KEY, MEMPROXY_TOKENS (JSON, or
MEMPROXY_TOKENS_FILE), MEMPROXY_MAX_BODY (bytes, default 1 MiB).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
from dataclasses import dataclass

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

log = logging.getLogger("agentcore.memproxy")

# (method, path after /v1/default/banks/{bank}) -> allowed. Anything not listed is refused.
ALLOWED: list[tuple[str, re.Pattern]] = [
    ("PUT", re.compile(r"")),                      # create or update the bank's name, mission, retain instructions
    ("GET", re.compile(r"")),                      # the bank's profile
    ("GET", re.compile(r"/directives")),
    ("POST", re.compile(r"/directives")),
    ("POST", re.compile(r"/memories")),            # retain
    ("POST", re.compile(r"/memories/recall")),
    ("GET", re.compile(r"/stats")),
]
BANK_PATH = re.compile(r"^/v1/default/banks/(?P<bank>[A-Za-z0-9._-]{1,128})(?P<rest>(/[A-Za-z0-9._/-]*)?)$")


@dataclass(frozen=True)
class Grant:
    name: str
    bank: str


def load_grants(raw: str) -> dict[str, Grant]:
    grants: dict[str, Grant] = {}
    for g in json.loads(raw or "[]"):
        digest = g["sha256"].lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"grant {g.get('name')!r}: sha256 must be 64 hex characters")
        grants[digest] = Grant(g["name"], g["bank"])
    return grants


def allowed(method: str, rest: str) -> bool:
    rest = rest.rstrip("/")
    return any(m == method and p.fullmatch(rest) for m, p in ALLOWED)


def create_app(*, upstream: str, upstream_key: str, grants: dict[str, Grant], max_body: int = 1 << 20,
               transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    app = FastAPI(title="memory-proxy", docs_url=None, redoc_url=None, openapi_url=None)
    client = httpx.AsyncClient(base_url=upstream.rstrip("/"), timeout=httpx.Timeout(60, connect=5),
                               transport=transport)

    def who(request: Request) -> Grant | None:
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not token:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        for known, grant in grants.items():  # constant-time over every grant
            if hmac.compare_digest(known, digest):
                return grant
        return None

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def forward(path: str, request: Request):
        grant = who(request)
        if grant is None:
            return JSONResponse({"error": "unauthorised"}, status_code=401)
        m = BANK_PATH.match("/" + path)
        if not m:
            log.info("refused %s %s for %s: not a bank route", request.method, "/" + path, grant.name)
            return JSONResponse({"error": "not allowed"}, status_code=403)
        bank, rest = m.group("bank"), m.group("rest") or ""
        if bank not in (grant.bank, "me"):
            log.warning("refused %s for %s: bank %s is not theirs", request.method, grant.name, bank)
            return JSONResponse({"error": "not your bank"}, status_code=403)
        if ".." in rest or not allowed(request.method, rest):
            log.info("refused %s %s for %s", request.method, rest or "/", grant.name)
            return JSONResponse({"error": "not allowed"}, status_code=403)
        body = b""
        async for chunk in request.stream():  # stops at the limit, so one agent can't fill a shared proxy's memory
            body += chunk
            if len(body) > max_body:
                return JSONResponse({"error": "too large"}, status_code=413)
        headers = {"Authorization": f"Bearer {upstream_key}"}
        if request.headers.get("content-type"):
            headers["Content-Type"] = request.headers["content-type"]
        r = await client.request(request.method, f"/v1/default/banks/{grant.bank}{rest}",
                                 params=dict(request.query_params), content=body or None, headers=headers)
        log.info("%s %s %s -> %s", grant.name, request.method, rest or "/", r.status_code)
        out = {k: v for k, v in r.headers.items() if k.lower() == "content-type"}
        return Response(r.content, status_code=r.status_code, headers=out)

    return app


def app() -> FastAPI:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    raw = os.environ.get("MEMPROXY_TOKENS")
    if raw is None and os.environ.get("MEMPROXY_TOKENS_FILE"):
        with open(os.environ["MEMPROXY_TOKENS_FILE"]) as f:
            raw = f.read()
    return create_app(upstream=os.environ["MEMPROXY_UPSTREAM"], upstream_key=os.environ["MEMPROXY_UPSTREAM_KEY"],
                      grants=load_grants(raw or "[]"),
                      max_body=int(os.environ.get("MEMPROXY_MAX_BODY", str(1 << 20))))
