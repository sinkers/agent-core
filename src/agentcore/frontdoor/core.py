"""The front door's work: take messages in, start workers, answer workers, deliver results.

Always on (two or more replicas). Every replica serves the HTTP APIs; one replica, the leader (a Postgres
lease in Postgres, renewed every 3 s), also runs the loops that must run once:
  ticker     due schedules into the queue (with jitter), expired leases back on the queue (or dead after the
             retry cap), workers started for waiting work (per-agent and global caps, launch backoff, stop after
             repeated failed starts), ended or silent workers cleaned up, the kill switch enforced
  delivery   finished work to the channel it came from (Telegram, voxhelm), schedule results to the person
  channels   the Telegram long polls and the voxhelm sockets
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from datetime import datetime, timedelta, timezone

from ..killswitch import KillSwitch
from ..workapi import encode
from .config import FrontDoorConfig
from .db import WAKE_CHANNEL, FrontDoorDB, agent_store

log = logging.getLogger(__name__)
TICK_S = 1.0
SCHEDULE_SCAN_S = 5.0
LEADER_CHECK_S = 3.0
LEASE_S = 15.0  # a leader that stops renewing is replaced within this
SORRY_DEAD = "Sorry, I couldn't finish that. I've logged it so someone can look into it."
SORRY_ERROR = "Sorry, something went wrong while I was working on that. Please try again."


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FrontDoor:
    def __init__(self, cfg: FrontDoorConfig, db: FrontDoorDB, launcher, *, speech=None, env: dict | None = None):
        self.cfg, self.db, self.launcher, self.speech = cfg, db, launcher, speech
        self.env = env or {}
        self.stores: dict[str, object] = {}
        self.killswitches: dict[str, KillSwitch] = {}
        self.channels: dict[tuple[str, str], object] = {}  # (agent, channel) -> TelegramBot | VoxhelmLink
        self.is_leader = False
        self._wake = asyncio.Event()
        self._deliver_wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._leader_tasks: list[asyncio.Task] = []
        self._last_schedule_scan = 0.0
        self._stopping: set[str] = set()  # sandboxes being deleted
        self._launcher_errors: dict[str, int] = {}  # launcher failures in a row per agent (backoff only)
        self._pool_failures: dict[str, int] = {}  # pooled workers in a row that didn't come up, per pool
        self._pool_backoff: dict[str, float] = {}  # monotonic time before which a pool isn't topped up

    # --- life ---------------------------------------------------------------------------------------------
    async def start(self) -> None:
        await self.db.open()
        async with self.db.pool.connection() as c:  # one replica at a time creates the agents' schemas
            await c.execute("SELECT pg_advisory_lock(hashtext('frontdoor:migrate'))")
            try:
                for agent in self.cfg.agents:
                    self.stores[agent] = await agent_store(self.db, agent)
            finally:
                await c.execute("SELECT pg_advisory_unlock(hashtext('frontdoor:migrate'))")
        for agent in self.cfg.agents:
            ks = KillSwitch(agent, configmap=self.cfg.killswitch_configmap)
            await ks.poll_once()
            ks.start()
            self.killswitches[agent] = ks
        self._build_channels()
        self._tasks.append(asyncio.create_task(self._leadership()))

    def _build_channels(self) -> None:
        import os
        for agent, entry in self.cfg.agents.items():
            if entry.telegram and os.environ.get(entry.telegram.token_env):
                from .telegram import TelegramBot
                self.channels[(agent, "telegram")] = TelegramBot(
                    agent, os.environ[entry.telegram.token_env], self, speech=self.speech,
                    voice_replies=entry.telegram.voice_replies)
            elif entry.telegram:
                log.warning("telegram for %s: %s is not set", agent, entry.telegram.token_env)
            for vx in entry.voxhelm:
                if not vx.users:
                    continue
                from .voxhelm import VoxhelmLink
                token = os.environ.get(vx.token_env or "") or None
                if vx.token_env and not token:
                    log.warning("%s for %s: %s is not set", vx.name, agent, vx.token_env)
                    continue
                self.channels[(agent, vx.name)] = VoxhelmLink(agent, vx.url, vx.users, self, token=token,
                                                              agent_id=vx.agent_id, name=vx.name)

    async def stop(self) -> None:
        for t in self._tasks + self._leader_tasks:
            t.cancel()
        for ks in self.killswitches.values():
            await ks.stop()
        for s in self.stores.values():
            await s.close()
        await self.db.close()

    def display_name(self, agent: str) -> str:
        return self.cfg.agents[agent].display_name or agent

    def stopped(self, agent: str) -> bool:
        if agent == "":  # a pooled worker, not yet given an agent
            return False
        ks = self.killswitches.get(agent)
        return ks is None or not ks.outbound_allowed

    def agent_state(self, agent: str) -> dict:
        return {"stopped": self.stopped(agent)}

    async def hold_warm(self, agent: str, seconds: float) -> None:
        """A call started or went on: have a worker up (from the pool, or started now) and keep it up for
        `seconds`, so no turn of the call waits for a sandbox to start."""
        if agent not in self.cfg.agents or self.stopped(agent):
            return
        await self.db.hold_warm(agent, seconds)
        await self.db.notify()

    # --- intake -------------------------------------------------------------------------------------------
    async def intake(self, agent: str, *, text: str, conversation_id: str, user_id: str | None,
                     user_name: str | None, reply_to: dict, source: str, dedupe_key: str | None = None,
                     channel: str = "chat") -> dict:
        """A message for an agent: queued, a worker woken if none is up, and the holding line sent at once when
        the agent is asleep. Returns {"item_id", "holding", "warm"}."""
        entry = self.cfg.agents[agent]
        out_channel = self.channels.get((agent, reply_to.get("channel")))
        if self.stopped(agent):
            if out_channel:
                await out_channel.send(reply_to, entry.paused_line, holding=True)
            return {"item_id": None, "holding": entry.paused_line, "warm": False, "paused": True}
        t0 = time.monotonic()
        row = await self.db.enqueue(agent, "chat", {
            "conversation_id": conversation_id, "text": text, "user_id": user_id, "user_name": user_name,
            "source": source, "channel": channel}, conversation_id=conversation_id, reply_to=reply_to,
            dedupe_key=dedupe_key)
        if row is None:
            return {"item_id": None, "holding": None, "warm": False, "duplicate": True}
        live = await self.db.live_workers(agent)
        warm = any(w["state"] in ("idle", "busy") for w in live)
        holding = None if warm else entry.holding_line
        if out_channel:  # sent alongside, so taking the message in never waits on the channel's API
            self._spawn(self._acknowledge(agent, out_channel, reply_to, holding, row["id"]))
        await self.db.event(agent, "intake", item=row["id"], warm=warm, source=source,
                            holding_ms=round((time.monotonic() - t0) * 1000))
        return {"item_id": row["id"], "holding": holding, "warm": warm}

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.append(task)
        task.add_done_callback(lambda t: self._tasks.remove(t) if t in self._tasks else None)

    async def _acknowledge(self, agent: str, channel, reply_to: dict, holding: str | None, item_id: int) -> None:
        t0 = time.monotonic()
        try:
            if holding:
                await channel.send(reply_to, holding, holding=True)
            await channel.typing(reply_to)
            if holding:
                await self.db.event(agent, "holding_sent", item=item_id, ms=round((time.monotonic() - t0) * 1000))
        except Exception:
            log.exception("holding line not sent")

    # --- worker API ---------------------------------------------------------------------------------------
    async def worker_claim(self, worker: dict, wait: float) -> dict | None:
        agent = worker["agent"]
        await self.contact(worker, "idle")
        deadline = time.monotonic() + max(0.0, min(wait, 25.0))
        last_seen = time.monotonic()
        while True:
            if time.monotonic() - last_seen > 5:  # a worker waiting for work is alive, however long it waits
                await self.db.seen(worker["id"])
                last_seen = time.monotonic()
            if self.stopped(agent):
                return None
            item = await self.db.claim(agent, worker["id"], self.cfg.limits.lease_seconds)
            if item:
                await self.db.seen(worker["id"], "busy")
                await self.db.event(agent, "claimed", item=item["id"], worker=worker["id"],
                                    queued_s=(utcnow() - item["created_at"]).total_seconds())
                return {"id": item["id"], "kind": item["kind"], "payload": item["payload"],
                        "attempt": item["attempts"]}
            if time.monotonic() >= deadline:
                await self.db.seen(worker["id"])
                return None
            await asyncio.sleep(0.25)

    async def contact(self, worker: dict, state: str | None = None) -> None:
        """Any call from a worker. The first one means it started: the launch counts as a success."""
        await self.db.seen(worker["id"], state)
        if worker["first_seen_at"] is None and worker["agent"] == "":
            worker["first_seen_at"] = utcnow()
            self._pool_failures.pop(worker.get("pool"), None)
            await self.db.event(None, "pool_ready", worker=worker["id"], pool=worker.get("pool"),
                                seconds=(utcnow() - worker["launched_at"]).total_seconds())
        elif worker["first_seen_at"] is None:
            worker["first_seen_at"] = utcnow()
            await self.db.launch_ok(worker["agent"])
            await self.db.event(worker["agent"], "worker_ready", worker=worker["id"],
                                seconds=(utcnow() - worker["launched_at"]).total_seconds())

    async def worker_assign(self, worker: dict, wait: float) -> dict | None:
        """A pooled worker waiting for an agent: the agent and its worker settings once it has been given one."""
        await self.contact(worker)
        deadline = time.monotonic() + max(0.0, min(wait, 25.0))
        last_seen = time.monotonic()
        while True:
            row = await self.db.worker_by_id(worker["id"])
            if not row or row["state"] == "gone":
                return None
            if row["agent"]:
                entry = self.cfg.agents[row["agent"]]
                await self.db.event(row["agent"], "assigned_taken", worker=worker["id"])
                return {"agent": row["agent"], "env": {"AGENT_DIR": f"/app/agents/{row['agent']}",
                                                       "IDLE_SECONDS": str(entry.idle_seconds), **entry.env}}
            if time.monotonic() >= deadline:
                return None
            if time.monotonic() - last_seen > 5:
                await self.db.seen(worker["id"])
                last_seen = time.monotonic()
            await asyncio.sleep(0.1)

    async def worker_result(self, worker: dict, item_id: int, result: dict) -> bool:
        # Idle first, then done: a follow-up sent the moment the answer shows must find this worker ready,
        # not busy (which would start a second worker for nothing).
        await self.db.q("UPDATE frontdoor.workers SET state='idle', last_seen=now(), items_done=items_done+1 "
                        "WHERE id=%s AND state <> 'gone'", worker["id"])
        row = await self.db.finish(item_id, worker["id"], result)
        if row:
            await self.db.event(worker["agent"], "done", item=item_id, worker=worker["id"],
                                total_s=(row["finished_at"] - row["created_at"]).total_seconds(),
                                work_s=result.get("seconds"), error=result.get("error"))
            self._deliver_wake.set()
        return bool(row)

    async def worker_store(self, worker: dict, method: str, args: list, kwargs: dict):
        await self.contact(worker)
        return await getattr(self.stores[worker["agent"]], method)(*args, **kwargs)

    # --- leadership ---------------------------------------------------------------------------------------
    async def _leadership(self) -> None:
        """Hold the leader lease (renewed every LEADER_CHECK_S, valid LEASE_S) and run the leader's loops while
        holding it. A replica that can't renew steps down before its lease runs out, so two never lead at once."""
        import socket
        import secrets
        me = f"{socket.gethostname()}-{secrets.token_hex(3)}"
        last_ok = 0.0
        try:
            while True:
                try:
                    held = await self.db.try_lead(me, LEASE_S)
                    if held:
                        last_ok = time.monotonic()
                except Exception as exc:
                    log.warning("leader lease check failed: %s", exc)
                    held = self.is_leader and time.monotonic() - last_ok < LEASE_S * 0.6
                if held and not self.is_leader:
                    log.info("this replica is the leader (%s)", me)
                    self.is_leader = True
                    self._leader_tasks = [asyncio.create_task(self._ticker()), asyncio.create_task(self._delivery()),
                                          asyncio.create_task(self._listen())]
                    self._leader_tasks += [asyncio.create_task(ch.run_forever()) for ch in self.channels.values()]
                elif not held and self.is_leader:
                    log.warning("no longer the leader")
                    self._step_down()
                await asyncio.sleep(LEADER_CHECK_S)
        finally:
            self._step_down()
            with contextlib.suppress(Exception):
                await self.db.resign(me)

    def _step_down(self) -> None:
        self.is_leader = False
        for t in self._leader_tasks:
            t.cancel()
        self._leader_tasks = []

    async def _listen(self) -> None:
        """Wake the ticker and delivery when any replica queues or finishes work (NOTIFY), not only each second."""
        import psycopg
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(self.db.dsn, autocommit=True) as conn:
                    await conn.execute(f"LISTEN {WAKE_CHANNEL}")
                    while True:
                        async for _ in conn.notifies(timeout=LEADER_CHECK_S):
                            self._wake.set()
                            self._deliver_wake.set()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("listen connection dropped: %s", exc)
                await asyncio.sleep(1)

    # --- ticker -------------------------------------------------------------------------------------------
    async def _ticker(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("tick failed")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), TICK_S)
            self._wake.clear()

    async def tick(self) -> None:
        lim = self.cfg.limits
        for item in await self.db.requeue_expired(lim.attempts):
            await self.db.event(item["agent"], "dead" if item["status"] == "dead" else "requeued", item=item["id"],
                                attempts=item["attempts"])
            if item["status"] == "dead":
                self._deliver_wake.set()
        await self.watch_workers()
        await self.enforce_killswitch()
        if time.monotonic() - self._last_schedule_scan >= SCHEDULE_SCAN_S or self._wake.is_set():
            self._last_schedule_scan = time.monotonic()
            await self.scan_schedules()
        await self.reconcile()

    async def scan_schedules(self) -> None:
        now = utcnow()
        for agent, store in self.stores.items():
            if self.stopped(agent):
                continue
            for t in await store.claim_due(now, limit=50):
                delay = random.uniform(0, self.cfg.limits.schedule_jitter_seconds)
                row = await self.db.enqueue(agent, "schedule", {"task": encode(t)}, conversation_id=f"task:{t['id']}",
                                            dedupe_key=f"sched:{agent}:{t['id']}:{t['run_at'].isoformat()}",
                                            not_before=now + timedelta(seconds=delay))
                if row:
                    await self.db.event(agent, "schedule_due", item=row["id"], task=t["id"], jitter_s=round(delay, 1))

    async def reconcile(self) -> None:
        """Start a worker for each agent with more waiting work than workers ready to take it, within the caps."""
        # Workers first, then the queue: a worker that claims in between lowers the count rather than leaving
        # waiting work with no ready worker counted (which would start a second worker for nothing).
        live = await self.db.live_workers()
        pending = await self.db.pending()
        total = len(live)
        total += await self.refill_pools(live, total)
        for agent in await self.db.warm_agents():  # a call in progress: one worker up even with nothing queued
            if agent not in pending and not any(w["agent"] == agent for w in live):
                pending[agent] = 1
        if not pending:
            return
        for agent, n in pending.items():
            entry = self.cfg.agents.get(agent)
            if entry is None or self.stopped(agent):
                continue
            st = await self.db.agent_state(agent)
            if st["blocked_until"] and st["blocked_until"] > utcnow():
                continue
            mine = [w for w in live if w["agent"] == agent]
            ready = sum(1 for w in mine if w["state"] in ("idle", "starting"))
            if n <= ready or len(mine) >= entry.max_workers:
                continue
            if entry.pool:  # a ready worker from the pool: no sandbox start to wait for
                got = await self.db.assign_pooled(entry.pool, agent)
                if got:
                    await self.db.event(agent, "assigned", worker=got["id"], pool=entry.pool)
                    self._wake.set()  # top the pool up now
                    continue
            if total >= self.cfg.limits.max_workers:
                await self.db.event(agent, "capped", pending=n, total=total)
                continue
            await self.launch(agent)
            total += 1

    async def refill_pools(self, live: list[dict], total: int) -> int:
        """Start pooled workers until each pool has its size waiting (within the global cap). Returns how many."""
        started = 0
        for name, pool in self.cfg.pools.items():
            waiting = sum(1 for w in live if w.get("pool") == name and w["agent"] == "")
            backoff_until = self._pool_backoff.get(name, 0.0)
            while waiting < pool.size and total + started < self.cfg.limits.max_workers \
                    and time.monotonic() >= backoff_until:
                if not await self.launch_pooled(name):
                    break
                waiting += 1
                started += 1
        return started

    async def launch_pooled(self, name: str) -> str | None:
        pool = self.cfg.pools[name]
        worker_id, token = await self.db.new_worker("", pool=name)
        env = {"FRONTDOOR_URL": self.cfg.worker_url, "WORKER_TOKEN": token, "WORKER_POOL": name}
        try:
            handle = await self.launcher.start("", pool, worker_id, env)
        except Exception as exc:
            log.warning("starting a pooled worker for %s failed: %s", name, str(exc)[:200])
            await self.db.end_worker(worker_id, f"launcher error: {exc}")
            self._pool_failed(name)
            return None
        await self.db.set_worker(worker_id, sandbox=handle)
        await self.db.event(None, "pool_launched", worker=worker_id, pool=name, sandbox=handle)
        return worker_id

    def _pool_failed(self, name: str) -> None:
        n = self._pool_failures[name] = self._pool_failures.get(name, 0) + 1
        self._pool_backoff[name] = time.monotonic() + min(300, 5 * 2 ** n)

    async def launch(self, agent: str) -> str | None:
        entry = self.cfg.agents[agent]
        worker_id, token = await self.db.new_worker(agent)
        env = {"FRONTDOOR_URL": self.cfg.worker_url, "WORKER_TOKEN": token, "AGENT_DIR": f"/app/agents/{agent}",
               "IDLE_SECONDS": str(entry.idle_seconds), **entry.env}
        t0 = time.monotonic()
        try:
            handle = await self.launcher.start(agent, entry, worker_id, env)
        except Exception as exc:
            # The launcher itself failed (gateway unreachable or timing out, e.g. while a node is down): the
            # platform's problem, not the agent's. Back off and keep the work queued; only workers that were
            # created and never came up count towards giving up (_launch_failed).
            log.warning("launching a worker for %s failed: %s", agent, str(exc)[:200])
            await self.db.end_worker(worker_id, f"launcher error: {exc}")
            self._launcher_errors[agent] = self._launcher_errors.get(agent, 0) + 1
            delay = min(60, 2 ** self._launcher_errors[agent])
            await self.db.block_agent(agent, utcnow() + timedelta(seconds=delay))
            await self.db.event(agent, "launcher_error", error=str(exc)[:300], retry_in_s=delay)
            return None
        self._launcher_errors.pop(agent, None)
        await self.db.set_worker(worker_id, sandbox=handle)
        await self.db.event(agent, "launched", worker=worker_id, sandbox=handle,
                            create_ms=round((time.monotonic() - t0) * 1000))
        return worker_id

    async def _launch_failed(self, agent: str, reason: str) -> None:
        n = await self.db.launch_failed(agent, reason)
        await self.db.event(agent, "launch_failed", count=n, reason=reason[:300])
        if n >= self.cfg.limits.launch_failures:
            # Stop trying: tell the people waiting, and leave the agent blocked until an admin resets it.
            await self.db.block_agent(agent, utcnow() + timedelta(days=365))
            dead = await self.db.dead_letter_queued(agent, f"{n} workers in a row failed to start: {reason[:200]}")
            await self.db.event(agent, "launch_stopped", failures=n, dead_lettered=len(dead))
            self._deliver_wake.set()

    async def watch_workers(self) -> None:
        lim = self.cfg.limits
        sandboxes = await self.launcher.list()
        live = await self.db.live_workers()
        now = utcnow()
        known = set()
        for w in live:
            h = w["sandbox"]
            known.add(h)
            state = sandboxes.get(h) if h else None
            age = (now - w["launched_at"]).total_seconds()
            reason = None
            if h and state is None and age > 10:
                reason = "sandbox disappeared"
            elif state in ("completed", "failed"):
                reason = f"worker exited ({state})"
            elif w["first_seen_at"] is None and age > lim.start_timeout_seconds:
                reason = f"did not start within {lim.start_timeout_seconds} s"
            elif w["last_seen"] and (now - w["last_seen"]).total_seconds() > lim.lease_seconds * 1.5:
                reason = "stopped answering"
            if reason:
                await self._end(w, reason)
        for h in sandboxes:  # ended workers' sandboxes, and any the database has forgotten
            if h not in known:
                self._stop_later(h)

    def _stop_later(self, handle: str) -> None:
        """Delete a sandbox without waiting: a delete waits for the pod to go (up to its grace period), and the
        ticker must not stall for that."""
        if handle in self._stopping:
            return
        self._stopping.add(handle)

        async def stop() -> None:
            try:
                await self.launcher.stop(handle)
            except Exception as exc:
                log.warning("stopping %s failed: %s", handle, exc)
            finally:
                self._stopping.discard(handle)
        self._spawn(stop())

    async def _end(self, w: dict, reason: str) -> None:
        if w["sandbox"]:
            self._stop_later(w["sandbox"])
        if await self.db.end_worker(w["id"], reason):
            await self.db.event(w["agent"], "worker_ended", worker=w["id"], reason=reason)
            if w["first_seen_at"] is None and w["agent"] == "" and w.get("pool"):
                self._pool_failed(w["pool"])  # a pooled worker that never came up: back off topping up
            elif w["first_seen_at"] is None and reason != "kill switch":  # stopped on purpose: not a failed start
                await self._launch_failed(w["agent"], reason)

    async def enforce_killswitch(self) -> None:
        for w in await self.db.live_workers():
            if w["agent"] and self.stopped(w["agent"]):
                await self._end(w, "kill switch")

    # --- delivery -----------------------------------------------------------------------------------------
    async def _delivery(self) -> None:
        while True:
            try:
                await self.deliver_pending()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("delivery failed")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._deliver_wake.wait(), 1.0)
            self._deliver_wake.clear()

    async def deliver_pending(self) -> None:
        # A stopped agent's items are held until it's released, and left out here so they can't fill the batch and
        # hold up every other agent.
        held = [a for a in self.cfg.agents if self.stopped(a)]
        for item in await self.db.undelivered(exclude=held):
            agent = item["agent"]
            try:
                await self._deliver(item)
            except Exception as exc:
                log.exception("delivering item %s failed", item["id"])
                await self.db.event(agent, "delivery_failed", item=item["id"], error=str(exc)[:300])
            await self.db.mark_delivered(item["id"])

    async def _deliver(self, item: dict) -> None:
        agent, result = item["agent"], item["result"] or {}
        if item["kind"] == "schedule":
            if item["status"] == "dead":
                await self._finish_dead_schedule(item)
            for d in result.get("deliveries") or []:
                b = await self.db.binding_for_user(agent, d["user_id"])
                ch = self.channels.get((agent, b["channel"])) if b else None
                if not ch:
                    await self.db.event(agent, "undeliverable", item=item["id"], user=d["user_id"])
                    continue
                to = {"chat_id": b["address"]} if b["channel"] == "telegram" else {"user": b["address"]}
                await ch.send({"channel": b["channel"], **to}, d["text"])
                await self.db.event(agent, "delivered", item=item["id"], channel=b["channel"])
            return
        reply_to = item["reply_to"] or {}
        ch = self.channels.get((agent, reply_to.get("channel")))
        if item["status"] == "dead":
            text = SORRY_DEAD
        elif result.get("error"):
            text = SORRY_ERROR
        else:
            text = result.get("reply")
        if ch and not text and hasattr(ch, "nothing_to_say"):
            await ch.nothing_to_say(reply_to)  # a call: back to listening rather than thinking for ever
        if not ch or not text:
            return
        await ch.send(reply_to, text)
        await self.db.event(agent, "delivered", item=item["id"], channel=reply_to.get("channel"),
                            total_s=(utcnow() - item["created_at"]).total_seconds())

    async def _finish_dead_schedule(self, item: dict) -> None:
        """A schedule run that never finished: move the schedule on, so it isn't claimed again and again."""
        from ..scheduler import next_cron
        from ..workapi import decode
        t = decode(item["payload"]["task"])
        now = utcnow()
        nxt = next_cron(t["cron"], now) if t.get("cron") else None
        await self.stores[item["agent"]].finish_scheduled(t["id"], nxt, f"failed: {item.get('error')}", now)
