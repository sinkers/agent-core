"""The front door's HTTP service.

Clients (bearer CLIENT_API_TOKEN):
  POST /v1/agents/{agent}/chat   {conversation_id, text, user_id?, user_name?} -> {item_id, holding, warm}
  GET  /v1/work/{item_id}?wait=  the item's state and result, waiting up to `wait` s for it to finish
Workers (bearer: the worker's own token, bound to one agent; workapi.py):
  POST /v1/worker/claim | heartbeat | result/{id} | store/{method} | wake | bye
  POST /v1/worker/assign            a pooled worker waits here for an agent (config.PoolEntry)
Admin (bearer ADMIN_API_TOKEN):
  GET  /v1/admin/status                          workers, queue, launch state, leader
  POST /v1/admin/agents/{agent}/pairing-code     a one-time Telegram pairing code (and the bot link)
  POST /v1/admin/agents/{agent}/reset            start launching again after repeated failed starts
  GET  /v1/admin/events?agent=&after=            the event log (timings for the PoC tests)
  GET  /healthz, /readyz

Environment: FRONTDOOR_CONFIG (YAML, config.py), DATABASE_URL, CLIENT_API_TOKEN, ADMIN_API_TOKEN,
ELEVENLABS_API_KEY (optional: voice notes), and each channel's token variable named in the config.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field

from ..workapi import STORE_METHODS, decode, encode
from .config import load_config
from .core import FrontDoor
from .db import FrontDoorDB
from .launcher import make_launcher

log = logging.getLogger(__name__)


MAX_CHAT_CHARS = 100_000


class ChatIn(BaseModel):
    conversation_id: str
    text: str = Field(max_length=MAX_CHAT_CHARS)
    user_id: str | None = None
    user_name: str | None = None


class ClaimIn(BaseModel):
    wait: float = 20.0


class AssignIn(BaseModel):
    wait: float = 20.0


class HeartbeatIn(BaseModel):
    item_id: int | None = None


class ResultIn(BaseModel):
    result: dict


class StoreIn(BaseModel):
    args: list = []
    kwargs: dict = {}


class ByeIn(BaseModel):
    reason: str = ""


def _api_id(value: str | None) -> str | None:
    """An API caller's id, kept in its own namespace: the shared client token must not be able to act as a Telegram or
    voxhelm person (their history, memory and pending confirmations are keyed by these ids)."""
    if value is None:
        return None
    return value if value.startswith("api:") else f"api:{value}"


def _bearer(authorization: str | None) -> str:
    return (authorization or "").removeprefix("Bearer ").strip()


def _check(authorization: str | None, env: str) -> None:
    expected = os.environ.get(env, "")
    if not expected or not hmac.compare_digest(_bearer(authorization).encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="unauthorised")


def create_app(fd: FrontDoor | None = None) -> FastAPI:
    state: dict = {"fd": fd} if fd else {}  # tests pass a started front door (and run no lifespan)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        door = fd
        if door is None:
            cfg = load_config(os.environ["FRONTDOOR_CONFIG"])
            speech = None
            if os.environ.get("ELEVENLABS_API_KEY"):
                from .speech import ElevenLabsSpeech
                speech = ElevenLabsSpeech(os.environ["ELEVENLABS_API_KEY"], cfg.speech_voice_id)
            door = FrontDoor(cfg, FrontDoorDB(os.environ["DATABASE_URL"]), make_launcher(cfg), speech=speech)
        await door.start()
        state["fd"] = door
        log.info("front door up for %s", ", ".join(door.cfg.agents))
        yield
        await door.stop()

    app = FastAPI(title="agentcore-frontdoor", lifespan=lifespan)

    def door() -> FrontDoor:
        return state["fd"]

    async def any_worker(authorization: str | None = Header(None)) -> dict:
        w = await state["fd"].db.worker_for_token(_bearer(authorization))
        if not w:
            raise HTTPException(status_code=401, detail="unknown or ended worker")
        return w

    async def worker(w: dict = Depends(any_worker)) -> dict:
        """A worker with an agent: a pooled worker can do nothing but wait for one."""
        if not w["agent"]:
            raise HTTPException(status_code=409, detail="no agent assigned yet")
        return w

    def client(authorization: str | None = Header(None)) -> None:
        _check(authorization, "CLIENT_API_TOKEN")

    def admin(authorization: str | None = Header(None)) -> None:
        _check(authorization, "ADMIN_API_TOKEN")

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/readyz")
    async def readyz(response: Response):
        try:
            await door().db.q("SELECT 1")
            return {"ok": True, "leader": door().is_leader}
        except Exception:
            response.status_code = 503
            return {"ok": False}

    # --- clients ------------------------------------------------------------------------------------------
    @app.post("/v1/agents/{agent}/chat", dependencies=[Depends(client)])
    async def chat(agent: str, body: ChatIn):
        if agent not in door().cfg.agents:
            raise HTTPException(status_code=404, detail="no such agent")
        return await door().intake(agent, text=body.text, conversation_id=_api_id(body.conversation_id),
                                   user_id=_api_id(body.user_id), user_name=body.user_name, reply_to={"channel": "api"},
                                   source="api")

    @app.get("/v1/work/{item_id}", dependencies=[Depends(client)])
    async def work(item_id: int, wait: float = 0):
        deadline = time.monotonic() + min(max(wait, 0), 60)
        while True:
            item = await door().db.get_item(item_id)
            # Only what came in through this API: replies on other channels are private to those people.
            if not item or (item["reply_to"] or {}).get("channel") != "api":
                raise HTTPException(status_code=404, detail="no such item")
            if item["status"] in ("done", "dead") or time.monotonic() >= deadline:
                return {k: item[k] for k in ("id", "agent", "kind", "status", "attempts", "result", "error",
                                             "created_at", "claimed_at", "finished_at")}
            await asyncio.sleep(0.25)

    # --- workers ------------------------------------------------------------------------------------------
    @app.post("/v1/worker/claim")
    async def claim(body: ClaimIn, w: dict = Depends(worker)):
        item = await door().worker_claim(w, body.wait)
        state = {**door().agent_state(w["agent"]), "warm": await door().db.is_warm(w["agent"])}
        return {"item": item, "agent_state": state}

    @app.post("/v1/worker/assign")
    async def assign(body: AssignIn, w: dict = Depends(any_worker)):
        if w["agent"]:  # already assigned (a retried call)
            got = await door().worker_assign(w, 0)
        else:
            got = await door().worker_assign(w, body.wait)
        return {"assignment": got, "agent_state": door().agent_state(got["agent"]) if got else {"stopped": False}}

    @app.post("/v1/worker/heartbeat")
    async def heartbeat(body: HeartbeatIn, w: dict = Depends(any_worker)):
        await door().contact(w)
        if body.item_id:
            await door().db.extend(body.item_id, w["id"], door().cfg.limits.lease_seconds)
        return {"ok": True, "agent_state": door().agent_state(w["agent"])}

    @app.post("/v1/worker/result/{item_id}")
    async def result(item_id: int, body: ResultIn, w: dict = Depends(worker)):
        ok = await door().worker_result(w, item_id, decode(body.result))
        return {"ok": ok, "agent_state": door().agent_state(w["agent"])}

    @app.post("/v1/worker/store/{method}")
    async def store(method: str, body: StoreIn, w: dict = Depends(worker)):
        if method not in STORE_METHODS:
            await door().db.event(w["agent"], "denied", worker=w["id"], method=method)
            raise HTTPException(status_code=403, detail=f"store method {method} is not allowed")
        out = await door().worker_store(w, method, decode(body.args), decode(body.kwargs))
        return {"result": encode(out), "agent_state": door().agent_state(w["agent"])}

    @app.post("/v1/worker/wake")
    async def wake(w: dict = Depends(worker)):
        await door().db.notify()
        return {"ok": True, "agent_state": door().agent_state(w["agent"])}

    @app.post("/v1/worker/bye")
    async def bye(body: ByeIn, w: dict = Depends(any_worker)):
        await door().db.end_worker(w["id"], f"said goodbye: {body.reason}"[:300])
        await door().db.event(w["agent"] or None, "worker_bye", worker=w["id"], reason=body.reason)
        return {"ok": True}

    # --- admin --------------------------------------------------------------------------------------------
    @app.get("/v1/admin/status", dependencies=[Depends(admin)])
    async def status():
        d = door()
        queue = await d.db.q("SELECT agent, status, count(*) AS n FROM frontdoor.work_items "
                             "WHERE status IN ('queued','claimed') GROUP BY agent, status")
        workers = [{k: v for k, v in w.items() if k != "token_hash"} for w in await d.db.live_workers()]
        return {"leader": d.is_leader, "workers": workers,
                "queue": queue, "agents": {a: {**await d.db.agent_state(a), "stopped": d.stopped(a)}
                                           for a in d.cfg.agents}}

    @app.post("/v1/admin/agents/{agent}/pairing-code", dependencies=[Depends(admin)])
    async def pairing_code(agent: str, by: str = "admin"):
        d = door()
        if agent not in d.cfg.agents:
            raise HTTPException(status_code=404, detail="no such agent")
        code = await d.db.new_pairing_code(agent, by)
        bot = d.channels.get((agent, "telegram"))
        if bot and bot.username is None:
            try:
                bot.username = (await bot.call("getMe")).get("username")
            except Exception:
                pass
        link = f"https://t.me/{bot.username}?start={code}" if bot and bot.username else None
        return {"code": code, "telegram_link": link, "expires_minutes": 60}

    @app.post("/v1/admin/agents/{agent}/reset", dependencies=[Depends(admin)])
    async def reset(agent: str):
        await door().db.reset_agent(agent)
        await door().db.notify()
        return {"ok": True}

    @app.get("/v1/admin/events", dependencies=[Depends(admin)])
    async def events(agent: str | None = None, after: int = 0, limit: int = 500):
        if agent:
            return await door().db.q("SELECT * FROM frontdoor.events WHERE id > %s AND agent=%s ORDER BY id LIMIT %s",
                                     after, agent, limit)
        return await door().db.q("SELECT * FROM frontdoor.events WHERE id > %s ORDER BY id LIMIT %s", after, limit)

    return app


def app() -> FastAPI:  # uvicorn --factory agentcore.frontdoor.app:app
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # every kill switch poll and Telegram long poll
    return create_app()
