"""The front door's configuration (FRONTDOOR_CONFIG, YAML): which agents it serves and how their workers start."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class TelegramChannel(BaseModel):
    token_env: str  # the bot token's environment variable (from @BotFather)
    voice_replies: bool = True  # answer a voice note with a voice note too (needs speech)


class VoxhelmChannel(BaseModel):
    name: str = "voxhelm"  # this gateway's name for the agent (one per gateway: voxhelm, voxhelm-dev...)
    url: str  # the voice-gateway's agent socket, e.g. ws://voice-gateway.voxhelm:8080/ws
    agent_id: str | None = None  # the agent's id on that gateway (default: the agent's name)
    users: list[str] = []  # voxhelm user ids this agent talks to (one socket each)
    token_env: str | None = None  # bearer token when the gateway checks auth


class PoolEntry(BaseModel):
    """Workers started ahead of time with no agent, each given one when work arrives (no sandbox start to wait
    for). A sandbox's policy and credentials are fixed when it is created, so a pool serves the agents that share
    its image, policy and providers. A worker serves one agent for its whole life, then the pool is topped up."""

    size: int = 1
    image: str
    policy: str | None = None
    providers: list[str] = []
    # A pool whose providers carry one agent's own credentials (its memory token) must only ever be given to that
    # agent: list it here. Empty: any agent with exactly this image, policy and providers may use it.
    only_for: list[str] = []


class AgentEntry(BaseModel):
    display_name: str | None = None
    pool: str | None = None  # take a ready worker from this pool (its image, policy and providers must be this agent's)
    image: str  # the worker image (the agent's folder is in it, at /app/agents/<name>)
    policy: str | None = None  # OpenShell policy YAML (a path); None: the image's own
    providers: list[str] = []  # OpenShell providers attached (credentials the proxy injects)
    max_workers: int = 2
    idle_seconds: int = 600
    holding_line: str = "Just a moment, I'm waking up."
    paused_line: str = "I'm paused at the moment, so I can't answer. I'll pick this up when I'm back."
    env: dict[str, str] = {}  # extra worker environment (never secrets: those come through providers)
    telegram: TelegramChannel | None = None
    voxhelm: list[VoxhelmChannel] = []  # one entry per voice-gateway

    @field_validator("voxhelm", mode="before")
    @classmethod
    def _one_or_many(cls, v):
        return [] if v is None else [v] if isinstance(v, dict) else v


class Limits(BaseModel):
    max_workers: int = 20  # across all agents
    attempts: int = 3  # tries per work item before it is dead-lettered
    launch_failures: int = 3  # workers in a row that end without taking work: stop launching for that agent
    lease_seconds: int = 60  # a claimed item comes back if its worker stops heart-beating for this long
    start_timeout_seconds: int = 120  # a worker that hasn't asked for work by then has failed to start
    schedule_jitter_seconds: float = 10.0  # due schedules are spread over this, so they don't all start at once


class OpenShellSettings(BaseModel):
    endpoint: str = "openshell.openshell.svc.cluster.local:8080"
    workspace: str = "default"
    tls_dir: str | None = "/etc/openshell-tls"  # ca.crt, tls.crt, tls.key
    command: list[str] = ["/usr/local/bin/python3.12", "-m", "agentcore.worker"]


class FrontDoorConfig(BaseModel):
    agents: dict[str, AgentEntry]
    pools: dict[str, PoolEntry] = {}
    launcher: str = "openshell"  # openshell | local (a subprocess per worker: tests and development)
    openshell: OpenShellSettings = Field(default_factory=OpenShellSettings)
    worker_url: str = "http://frontdoor.agents.svc.cluster.local:8080"  # how workers reach the work API
    limits: Limits = Field(default_factory=Limits)
    killswitch_configmap: str = "agents-killswitch"
    speech_voice_id: str = "JBFqnCBsd6RMkjVDRZzb"  # ElevenLabs premade voice for spoken replies

    @model_validator(mode="after")
    def _pools_only_for_their_agents(self):
        for name, entry in self.agents.items():
            pool = self.pools.get(entry.pool) if entry.pool else None
            if entry.pool and pool is None:
                raise ValueError(f"agent {name}: pool {entry.pool!r} is not defined")
            if pool and pool.only_for and name not in pool.only_for:
                raise ValueError(f"agent {name} may not use pool {entry.pool!r} (it carries credentials for "
                                 f"{', '.join(pool.only_for)} only)")
            # A sandbox's policy and credentials are fixed when it's created, so a pooled worker has the pool's:
            # any difference would quietly give the agent someone else's credentials or wider network access.
            if pool and (pool.image, pool.policy, set(pool.providers)) != (entry.image, entry.policy, set(entry.providers)):
                raise ValueError(f"agent {name}: image, policy and providers must match pool {entry.pool!r}")
        return self


def load_config(path: str | Path) -> FrontDoorConfig:
    return FrontDoorConfig.model_validate(yaml.safe_load(Path(path).read_text()))
