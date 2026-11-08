"""Kill switch.

One ConfigMap (`agents-killswitch`) holds one key per agent. Value `stop` halts every
outbound action for that agent (messages, email, calls, file writes) while the agent
keeps receiving, logging and serving its read-only endpoints.

The ConfigMap is read from the Kubernetes API every `interval` seconds rather than
from a volume mount, because kubelet can take over 60 s to refresh a mounted
ConfigMap and the spec requires 30 s. The agent's RBAC allows `get` on this one
ConfigMap only, so it cannot flip its own switch.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import httpx

log = logging.getLogger(__name__)
SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
STOP_VALUES = {"stop", "stopped", "off", "kill", "true"}


class KillSwitch:
    def __init__(self, agent: str, configmap: str = "agents-killswitch", namespace: str | None = None,
                 interval: float = 5.0):
        self.agent = agent
        self.configmap = configmap
        self.namespace = namespace or self._namespace()
        self.interval = interval
        # Fail safe: until the first successful read the agent is stopped.
        self.stopped = True
        self.last_ok: float | None = None
        self._task: asyncio.Task | None = None
        self._client: httpx.AsyncClient | None = None
        self.override: str | None = os.environ.get("KILLSWITCH_OVERRIDE")  # local dev/tests only

    @staticmethod
    def _namespace() -> str:
        p = SA_DIR / "namespace"
        return p.read_text().strip() if p.exists() else os.environ.get("POD_NAMESPACE", "default")

    @property
    def outbound_allowed(self) -> bool:
        return not self.stopped

    def apply(self, data: dict[str, str] | None) -> None:
        value = (data or {}).get(self.agent, "run").strip().lower()
        stopped = value in STOP_VALUES
        if stopped != self.stopped:
            log.warning("killswitch %s for %s", "ENGAGED" if stopped else "released", self.agent)
        self.stopped = stopped

    async def poll_once(self) -> None:
        if self.override is not None:
            self.apply({self.agent: self.override})
            return
        host = os.environ.get("KUBERNETES_SERVICE_HOST")
        if not host:
            log.error("not in cluster and no KILLSWITCH_OVERRIDE; staying stopped")
            self.stopped = True
            return
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=f"https://{host}:{os.environ.get('KUBERNETES_SERVICE_PORT', '443')}",
                verify=str(SA_DIR / "ca.crt"), timeout=5.0,
            )
        token = (SA_DIR / "token").read_text().strip()  # re-read: projected tokens rotate
        url = f"/api/v1/namespaces/{self.namespace}/configmaps/{self.configmap}"
        try:
            r = await self._client.get(url, headers={"Authorization": f"Bearer {token}"})
            r.raise_for_status()
            self.apply(r.json().get("data"))
            self.last_ok = asyncio.get_running_loop().time()
        except Exception as exc:  # keep last known state; readiness reports staleness
            log.error("killswitch read failed: %s", exc)
            if self.last_ok is None or asyncio.get_running_loop().time() - self.last_ok > 30:
                self.stopped = True  # cannot confirm we are allowed to act: fail safe

    async def run(self) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(self.interval)

    def start(self) -> None:
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._client:
            await self._client.aclose()
