"""Launchers: how the front door starts and stops a worker.

  OpenShellLauncher  a worker per OpenShell sandbox: the agent's policy, its providers (credentials the proxy
                     injects; the sandbox holds placeholders only) and the worker as the sandbox's own process, so
                     it runs on whether or not the front door replica that started it is still there
  LocalLauncher      a subprocess per worker (tests and development; no isolation at all)

Every launcher answers `start(agent, worker_id, env) -> handle`, `stop(handle)` and `list() -> {handle: state}`
(state: running | completed | failed), so the ticker can notice workers that ended without saying goodbye.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Protocol

import yaml

from .config import AgentEntry, FrontDoorConfig

log = logging.getLogger(__name__)
WORKER_LABEL = "agentcore-worker"


class Launcher(Protocol):
    async def start(self, agent: str, entry: AgentEntry, worker_id: str, env: dict[str, str]) -> str: ...
    async def stop(self, handle: str) -> None: ...
    async def list(self) -> dict[str, str]: ...


_ENUMS = {"enforcement": "NETWORK_ENFORCEMENT_MODE_", "tls": "NETWORK_TLS_MODE_", "access": "NETWORK_ACCESS_PRESET_"}


def policy_proto(doc: dict):
    """An OpenShell policy file (the CLI's YAML) as the gateway's SandboxPolicy message."""
    from google.protobuf import json_format
    from openshell._proto import sandbox_pb2
    doc = dict(doc)
    if "filesystem_policy" in doc:
        doc["filesystem"] = doc.pop("filesystem_policy")
    for rule in (doc.get("network_policies") or {}).values():
        for ep in rule.get("endpoints") or []:
            for key, prefix in _ENUMS.items():
                if isinstance(ep.get(key), str) and not ep[key].startswith(prefix):
                    ep[key] = prefix + ep[key].upper()
    return json_format.ParseDict(doc, sandbox_pb2.SandboxPolicy())


class OpenShellLauncher:
    def __init__(self, cfg: FrontDoorConfig):
        from openshell import SandboxClient, TlsConfig
        s = cfg.openshell
        tls = None
        if s.tls_dir:
            d = Path(s.tls_dir)
            tls = TlsConfig(ca_path=d / "ca.crt", cert_path=d / "tls.crt", key_path=d / "tls.key")
        self.client = SandboxClient(s.endpoint, tls=tls, timeout=30)
        self.workspace = s.workspace
        self.command = s.command
        self._policies: dict[str, object] = {}

    def _policy(self, path: str | None):
        if not path:
            return None
        if path not in self._policies:
            self._policies[path] = policy_proto(yaml.safe_load(Path(path).read_text()))
        return self._policies[path]

    async def start(self, agent: str, entry: AgentEntry, worker_id: str, env: dict[str, str]) -> str:
        from openshell._proto import openshell_pb2
        spec = openshell_pb2.SandboxSpec(
            template=openshell_pb2.SandboxTemplate(image=entry.image),
            environment=env, providers=entry.providers, command=self.command,
            restart_policy=openshell_pb2.SANDBOX_RESTART_POLICY_NEVER)
        policy = self._policy(entry.policy)
        if policy is not None:
            spec.policy.CopyFrom(policy)
        ref = await asyncio.to_thread(self.client.create, workspace=self.workspace, spec=spec,
                                      labels={WORKER_LABEL: worker_id, "agent": agent})
        return ref.name

    async def stop(self, handle: str) -> None:
        await asyncio.to_thread(self.client.delete, handle, workspace=self.workspace, allow_missing=True)

    async def list(self) -> dict[str, str]:
        from openshell._proto import openshell_pb2 as pb
        refs = await asyncio.to_thread(self.client.list_all, workspace=self.workspace)
        out = {}
        for r in refs:
            if WORKER_LABEL not in (r.labels or {}):
                continue
            phase = r.status.phase if r.status else pb.SANDBOX_PHASE_UNSPECIFIED
            out[r.name] = ("completed" if phase in (pb.SANDBOX_PHASE_COMPLETED, pb.SANDBOX_PHASE_STOPPED)
                           else "failed" if phase == pb.SANDBOX_PHASE_ERROR else "running")
        return out


class LocalLauncher:
    """Each worker a subprocess of the front door, with the same environment a sandbox would get."""

    def __init__(self, cfg: FrontDoorConfig, agents_dir: str | None = None):
        self.agents_dir = agents_dir or os.environ.get("AGENTS_DIR", "/app/agents")
        self.procs: dict[str, asyncio.subprocess.Process] = {}

    async def start(self, agent: str, entry: AgentEntry, worker_id: str, env: dict[str, str]) -> str:
        full = {**os.environ, "AGENT_DIR": str(Path(self.agents_dir) / agent), **env}
        proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "agentcore.worker", env=full)
        self.procs[worker_id] = proc
        return worker_id

    async def stop(self, handle: str) -> None:
        proc = self.procs.pop(handle, None)
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                proc.kill()

    async def list(self) -> dict[str, str]:
        return {h: ("running" if p.returncode is None else "completed" if p.returncode == 0 else "failed")
                for h, p in self.procs.items()}


def make_launcher(cfg: FrontDoorConfig) -> Launcher:
    return OpenShellLauncher(cfg) if cfg.launcher == "openshell" else LocalLauncher(cfg)
