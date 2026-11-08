"""A worker: one agent, started for the work waiting for it, gone again when there is none.

  python -m agentcore.worker

The front door starts a worker (in an OpenShell sandbox, or any other launcher) when work arrives for an agent
that has none running. The worker:
  1. builds the agent with no database: its store is the front door's work API (remote.py), and the kill switch
     state comes back with every reply;
  2. claims work items one at a time (a chat message, or a scheduled task the front door found due) and answers
     them through the normal agent loop: tools, confirms, budgets and the kill switch all apply;
  3. stays up for IDLE_SECONDS after the last item, so a conversation's follow-ups don't wait for a start;
  4. exits when idle, when the kill switch is engaged, or when the front door says it has gone.
Results go back to the front door, which delivers them (Telegram, voxhelm, the chat API...).

A pooled worker (WORKER_POOL set) starts before there is work and with no agent: it imports what it needs, waits
at the front door to be given an agent (its AGENT_DIR and settings come with the assignment), and from then on
works as above. Each worker serves one agent for its whole life.

Environment: FRONTDOOR_URL, WORKER_TOKEN, AGENT_DIR (or WORKER_POOL), SKILLS_DIR, IDLE_SECONDS (default 600), PRICING_FILE,
IMAGE_TAG (keys the cached MCP tool lists), AGENT_OWNER (overrides agent.yaml's owner), WORKER_TEST_HOOKS=1 (PoC tests only: worker_testhooks.py).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("agentcore.worker")
HEARTBEAT_S = 15.0
CLAIM_WAIT_S = 20.0
MCP_CACHE_USER = "_worker"  # prefs rows holding cached MCP tool lists (one per server and image)


class ToolListCache:
    """Remote MCP servers' tool lists, kept in the agent's own store (ToolGateway.discover)."""

    def __init__(self, store, image: str):
        self.store, self.image = store, image

    async def get(self, server: str):
        return await self.store.get_pref(MCP_CACHE_USER, f"mcp_tools:{server}:{self.image}")

    async def put(self, server: str, tools: list) -> None:
        await self.store.set_pref(MCP_CACHE_USER, f"mcp_tools:{server}:{self.image}", tools)


async def build_agent(agent_dir: Path, store, killswitch, *, model=None, on_new_task=None):
    """The agent with the in-process tools a worker can offer without Teams or Graph. Heavy modules (documents,
    voice, Graph) are not imported: a chat turn that needs none of them shouldn't pay for loading them."""
    from .agent import Agent
    from .openrouter import model_client
    from .budget import BudgetTracker
    from .config import PRICING_DEFAULT, load_agent_config, load_pricing
    from .org import set_agent
    from .memory import memory_from_config
    from .skills import SkillLoader
    from .tools import ToolGateway, build_clients

    cfg = load_agent_config(agent_dir / "agent.yaml")
    if os.environ.get("AGENT_OWNER"):  # the deployment's owner, when the image's agent.yaml is generic
        cfg.agent.owner = os.environ["AGENT_OWNER"]
    set_agent(cfg.agent.display_name, cfg.agent.owner)
    skills = SkillLoader(os.environ.get("SKILLS_DIR", "/app/skills"), cfg.agent.name, cfg.skills.always,
                         cfg.skills.on_demand)
    clients = build_clients(cfg)
    tools = ToolGateway(cfg, skills, clients)
    if "schedule" in cfg.mcp_servers:
        from .scheduler import ScheduleTools
        clients["schedule"] = ScheduleTools(store)
        clients["schedule"].on_new = on_new_task
    if "history" in cfg.mcp_servers:
        from .transcripts import TranscriptTools
        clients["history"] = TranscriptTools(store)
    if "web" in cfg.mcp_servers:
        from .web import WebTools
        clients["web"] = WebTools(store, cfg.web)
    skipped = sorted(set(cfg.mcp_servers) - set(clients))
    if skipped:
        log.warning("not available in a worker yet, hidden: %s", ", ".join(skipped))
    await tools.discover(cache=ToolListCache(store, os.environ.get("IMAGE_TAG", "dev")))
    budget = BudgetTracker(cfg.budget, store, load_pricing(os.environ.get("PRICING_FILE") or PRICING_DEFAULT))
    agent = Agent(cfg, model=model or model_client(cfg.models), store=store,
                  budget=budget, tools=tools, skills=skills, killswitch=killswitch,
                  persona=Agent.load_persona(agent_dir), memory=memory_from_config(cfg))
    log.info("memory: %s", "hindsight" if agent.memory else "off")
    return agent


class Worker:
    def __init__(self, client, agent, *, idle_seconds: float, test_hooks: bool = False):
        self.client, self.agent = client, agent
        self.idle_seconds = idle_seconds
        self.test_hooks = test_hooks
        self.items_done = 0

    async def run(self) -> str:
        """Claim and answer work until idle or stopped. Returns why it stopped."""
        from .remote import WorkerGone
        idle_until = time.monotonic() + self.idle_seconds
        try:
            while True:
                if not self.agent.killswitch.outbound_allowed:
                    return "kill switch"
                left = idle_until - time.monotonic()
                if left <= 0:
                    return "idle"
                item = await self.client.claim(wait=min(CLAIM_WAIT_S, max(1.0, left)))
                if item is None:
                    if getattr(self.client, "warm", False):  # a call in progress: don't idle out mid-call
                        idle_until = max(idle_until, time.monotonic() + self.idle_seconds)
                    continue
                await self._do(item)
                self.items_done += 1
                idle_until = time.monotonic() + self.idle_seconds
        except WorkerGone as exc:
            return f"gone: {exc}"

    async def _do(self, item: dict) -> None:
        beat = asyncio.create_task(self._heartbeat(item["id"]))
        started = time.monotonic()
        try:
            result = await self._run_item(item)
        except Exception as exc:
            log.exception("work item %s failed", item["id"])
            result = {"error": f"{type(exc).__name__}: {exc}"[:500]}
        finally:
            beat.cancel()
        result["seconds"] = round(time.monotonic() - started, 2)
        await self.client.result(item["id"], result)

    async def _heartbeat(self, item_id: int) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            try:
                await self.client.heartbeat(item_id)
            except Exception:
                log.warning("heartbeat failed", exc_info=True)

    async def _run_item(self, item: dict) -> dict:
        p = item["payload"]
        if self.test_hooks:
            hooked = await self._test_hook(p.get("text") or "")
            if hooked is not None:
                return hooked
        if item["kind"] == "chat":
            from .agent import Turn
            result = await self.agent.handle(Turn(
                p["conversation_id"], p["text"], p.get("user_id"), p.get("user_name"),
                channel=p.get("channel") or "chat", source=p.get("source"), scope=p.get("scope") or "private"))
            return {"reply": result.reply, "suppressed": result.suppressed, "tier": result.tier,
                    "cost_usd": result.cost_usd, "tools": result.tool_calls}
        if item["kind"] == "schedule":
            return await self._run_schedule(p["task"])
        return {"error": f"unknown work kind {item['kind']!r}"}

    async def _run_schedule(self, task: dict) -> dict:
        """A due scheduled task, run exactly as the in-pod scheduler runs it; what it would deliver is handed
        back for the front door to send."""
        from .scheduler import TaskRunner
        from .workapi import decode
        deliveries: list[dict] = []

        async def deliver(user_id: str, text: str, via: str = "chat", task: dict | None = None) -> bool:
            deliveries.append({"user_id": user_id, "text": text, "via": via})
            return True
        task = decode(task)
        await TaskRunner(agent=self.agent, store=self.agent.store, deliver=deliver)._run(
            task, datetime.now(timezone.utc))
        return {"deliveries": deliveries}

    async def _test_hook(self, text: str) -> dict | None:
        """PoC tests only (WORKER_TEST_HOOKS=1; worker_testhooks.py)."""
        from . import worker_testhooks
        return await worker_testhooks.run(text, self.client)


def preload() -> None:
    """Import what building and running an agent needs (a pooled worker does this before it has an agent)."""
    import boto3  # noqa: F401
    from . import agent, bedrock, budget, config, openrouter, scheduler, skills, tools, transcripts  # noqa: F401
    boto3.session.Session().client  # noqa: B018  (loads botocore's data on first use)


async def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    t0 = time.monotonic()
    # Stopped (kill switch, node drain, sandbox deleted): leave now. As the sandbox's main process this may be
    # PID 1, which ignores SIGTERM unless it has a handler, and would then be killed only after the grace period.
    import signal
    main_task = asyncio.current_task()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, main_task.cancel)
    from .killswitch import KillSwitch
    from .remote import FrontDoorClient, RemoteStore

    holder: dict = {}

    def on_state(state: dict) -> None:
        ks = holder.get("killswitch")
        if ks:
            ks.apply({ks.agent: "stop" if state.get("stopped") else "run"})

    client = FrontDoorClient(os.environ["FRONTDOOR_URL"], os.environ["WORKER_TOKEN"], on_state=on_state)
    reason = "error"
    try:
        if os.environ.get("WORKER_POOL"):
            # Pooled: started before any work, with no agent. Pay for the imports now, then wait to be given an
            # agent; from then on this worker is that agent's, until it exits.
            preload()
            log.info("pooled worker (%s) ready in %.2f s; waiting for an agent", os.environ["WORKER_POOL"],
                     time.monotonic() - t0)
            assignment = None
            while assignment is None:
                assignment = await client.assign(wait=20)
            if not str(assignment["agent"]).replace("_", "").replace("-", "").isalnum():
                raise RuntimeError("bad agent name in assignment")
            os.environ.update(assignment.get("env") or {})
            t0 = time.monotonic()
        agent_dir = Path(os.environ["AGENT_DIR"])
        name = agent_dir.name
        killswitch = KillSwitch(name)
        killswitch.override = "run"  # never polls the Kubernetes API (it can't reach it): the front door says
        holder["killswitch"] = killswitch
        await client.heartbeat()  # the kill switch state, before anything is answered
        store = RemoteStore(client)
        loop = asyncio.get_running_loop()

        def on_new_task() -> None:
            loop.create_task(client.wake())
        agent = await build_agent(agent_dir, store, killswitch, on_new_task=on_new_task)
        log.info("worker for %s ready in %.2f s", name, time.monotonic() - t0)
        worker = Worker(client, agent, idle_seconds=float(os.environ.get("IDLE_SECONDS", "600")),
                        test_hooks=os.environ.get("WORKER_TEST_HOOKS") == "1")
        reason = await worker.run()
        log.info("worker for %s stopping (%s) after %d item(s)", name, reason, worker.items_done)
        return 0
    except Exception as exc:  # said in the goodbye too: a sandbox's log goes with it
        log.exception("worker failed")
        reason = f"error: {type(exc).__name__}: {exc}"[:300]
        return 1
    finally:
        if reason == "error" and main_task.cancelling():
            reason = "terminated"
        await asyncio.shield(client.bye(reason))
        await client.close()


def run() -> int:
    try:
        return asyncio.run(main())
    except asyncio.CancelledError:  # SIGTERM
        return 0


if __name__ == "__main__":
    sys.exit(run())
