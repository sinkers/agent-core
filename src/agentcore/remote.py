"""The worker's side of the work API: a client for the front door, and a store that runs every call there.

  FrontDoorClient  claim work, report results, heartbeat, say goodbye; each reply carries the kill switch state
  RemoteStore      the Store interface over `POST /v1/worker/store/{method}` (workapi.py)

The worker's token is bound to one agent and one worker by the front door, so these calls reach only that agent's
data, and stop working once the worker has ended.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

import httpx

from .workapi import STORE_METHODS, decode, encode

log = logging.getLogger(__name__)


class WorkerGone(RuntimeError):
    """The front door no longer knows this worker (ended, replaced or revoked): stop."""


class FrontDoorClient:
    def __init__(self, base_url: str, token: str, *, on_state: Callable[[dict], None] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.http = httpx.AsyncClient(base_url=base_url.rstrip("/"), headers={"Authorization": f"Bearer {token}"},
                                      timeout=httpx.Timeout(60.0, connect=5.0), transport=transport)
        self.on_state = on_state  # called with {"stopped": bool, ...} from every reply
        self.warm = False  # the agent is on a call: stay up while it lasts (from each claim's reply)

    async def close(self) -> None:
        await self.http.aclose()

    async def _post(self, path: str, body: dict | None = None, timeout: float | None = None) -> Any:
        r = await self.http.post(path, json=body or {}, timeout=timeout or self.http.timeout)
        if r.status_code in (401, 410):
            raise WorkerGone(r.text[:200])
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and isinstance(data.get("agent_state"), dict):
            if "warm" in data["agent_state"]:
                self.warm = bool(data["agent_state"]["warm"])
            if self.on_state:
                self.on_state(data["agent_state"])
        return data

    async def claim(self, wait: float) -> dict | None:
        """The next work item for this agent, waiting up to `wait` seconds; None if there is none."""
        data = await self._post("/v1/worker/claim", {"wait": wait}, timeout=wait + 15)
        return data.get("item")

    async def assign(self, wait: float) -> dict | None:
        """A pooled worker: the agent it has been given ({"agent", "env"}), or None if none yet."""
        data = await self._post("/v1/worker/assign", {"wait": wait}, timeout=wait + 15)
        return data.get("assignment")

    async def heartbeat(self, item_id: int | None = None) -> dict:
        return await self._post("/v1/worker/heartbeat", {"item_id": item_id})

    async def result(self, item_id: int, result: dict) -> None:
        await self._post(f"/v1/worker/result/{item_id}", {"result": encode(result)})

    async def wake(self) -> None:
        """A background task was just scheduled: ask the front door to look for due work now."""
        await self._post("/v1/worker/wake")

    async def bye(self, reason: str) -> None:
        try:
            await self._post("/v1/worker/bye", {"reason": reason}, timeout=5)
        except Exception:
            log.debug("bye failed", exc_info=True)

    async def store_call(self, method: str, args: list, kwargs: dict) -> Any:
        data = await self._post(f"/v1/worker/store/{method}", {"args": encode(args), "kwargs": encode(kwargs)})
        return decode(data.get("result"))


class RemoteStore:
    """The Store interface, every call made by the front door on this agent's own store."""

    def __init__(self, client: FrontDoorClient):
        self._client = client

    def __getattr__(self, name: str):
        if name not in STORE_METHODS:
            raise AttributeError(f"store method {name!r} is not available to workers")

        async def call(*args, **kwargs):
            return await self._client.store_call(name, list(args), kwargs)
        call.__name__ = name
        return call
