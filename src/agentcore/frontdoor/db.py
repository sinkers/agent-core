"""The front door's tables (schema `frontdoor`): the work queue, workers, launch failures, channel bindings,
pairing codes and an event log for timings. Every agent's own state stays in its own schema (`agent_<name>`),
reached only through `agent_store`."""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS frontdoor;
CREATE TABLE IF NOT EXISTS frontdoor.work_items (
  id bigserial PRIMARY KEY,
  agent text NOT NULL,
  kind text NOT NULL,                       -- chat | schedule
  conversation_id text,
  payload jsonb NOT NULL,
  reply_to jsonb,                           -- where the answer goes (channel and address)
  status text NOT NULL DEFAULT 'queued',    -- queued | claimed | done | dead | cancelled
  attempts int NOT NULL DEFAULT 0,
  not_before timestamptz NOT NULL DEFAULT now(),
  worker_id text,
  lease_until timestamptz,
  result jsonb,
  error text,
  delivered boolean NOT NULL DEFAULT false,
  dedupe_key text UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now(),
  claimed_at timestamptz,
  finished_at timestamptz
);
CREATE INDEX IF NOT EXISTS work_items_queue ON frontdoor.work_items (agent, status, not_before);
CREATE INDEX IF NOT EXISTS work_items_undelivered ON frontdoor.work_items (delivered, status) WHERE NOT delivered;
CREATE TABLE IF NOT EXISTS frontdoor.workers (
  id text PRIMARY KEY,
  agent text NOT NULL,
  token_hash text NOT NULL,
  sandbox text,
  state text NOT NULL DEFAULT 'starting',   -- starting | idle | busy | gone
  launched_at timestamptz NOT NULL DEFAULT now(),
  first_seen_at timestamptz,
  last_seen timestamptz,
  ended_at timestamptz,
  end_reason text,
  items_done int NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS workers_live ON frontdoor.workers (agent) WHERE state <> 'gone';
ALTER TABLE frontdoor.workers ADD COLUMN IF NOT EXISTS pool text;  -- a pooled worker: agent '' until assigned
CREATE TABLE IF NOT EXISTS frontdoor.agent_state (
  agent text PRIMARY KEY,
  launch_failures int NOT NULL DEFAULT 0,
  blocked_until timestamptz,
  last_failure text
);
ALTER TABLE frontdoor.agent_state ADD COLUMN IF NOT EXISTS warm_until timestamptz;  -- a call in progress: keep a worker up
CREATE TABLE IF NOT EXISTS frontdoor.bindings (
  agent text NOT NULL, channel text NOT NULL, address text NOT NULL,
  user_id text NOT NULL, user_name text,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (agent, channel, address)
);
CREATE TABLE IF NOT EXISTS frontdoor.pairing_codes (
  code_hash text PRIMARY KEY, agent text NOT NULL, created_by text,
  expires_at timestamptz NOT NULL, used_at timestamptz, used_by text
);
CREATE TABLE IF NOT EXISTS frontdoor.kv (key text PRIMARY KEY, value jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS frontdoor.leader (id int PRIMARY KEY, holder text NOT NULL, until timestamptz NOT NULL);
CREATE TABLE IF NOT EXISTS frontdoor.events (
  id bigserial PRIMARY KEY, at timestamptz NOT NULL DEFAULT now(), agent text, kind text NOT NULL, detail jsonb
);
"""

WAKE_CHANNEL = "frontdoor_wake"


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def now() -> datetime:
    return datetime.now(timezone.utc)


class FrontDoorDB:
    def __init__(self, dsn: str, *, max_size: int = 10):
        self.dsn = dsn
        self.pool = AsyncConnectionPool(dsn, min_size=1, max_size=max_size, open=False,
                                        kwargs={"row_factory": dict_row, "autocommit": True})

    async def open(self) -> None:
        await self.pool.open(wait=True, timeout=30)
        async with self.pool.connection() as c:
            await c.execute("SELECT pg_advisory_lock(hashtext('frontdoor:migrate'))")
            try:
                await c.execute(SCHEMA)
            finally:
                await c.execute("SELECT pg_advisory_unlock(hashtext('frontdoor:migrate'))")

    async def close(self) -> None:
        await self.pool.close()

    async def q(self, sql: str, *args) -> list[dict]:
        async with self.pool.connection() as c:
            cur = await c.execute(sql, args)
            return await cur.fetchall() if cur.description else []

    async def one(self, sql: str, *args) -> dict | None:
        rows = await self.q(sql, *args)
        return rows[0] if rows else None

    async def event(self, agent: str | None, kind: str, **detail) -> None:
        await self.q("INSERT INTO frontdoor.events (agent, kind, detail) VALUES (%s,%s,%s)",
                     agent, kind, json.dumps(detail, default=str))

    async def cancel_queued(self, item_id: int) -> bool:
        """Withdraw an item no worker has taken yet (e.g. merged into a later one). False if already claimed."""
        row = await self.one("UPDATE frontdoor.work_items SET status='cancelled', finished_at=now(), delivered=true "
                             "WHERE id=%s AND status='queued' RETURNING id", item_id)
        return row is not None

    async def notify(self) -> None:
        await self.q(f"NOTIFY {WAKE_CHANNEL}")

    # --- leadership ---------------------------------------------------------------------------------------
    async def try_lead(self, me: str, ttl_s: float) -> bool:
        """Take or renew the leader lease. A lease, not a session advisory lock: a replica on a node that lost
        power holds a lock until TCP gives up on its connection (hours by default); a lease simply expires."""
        row = await self.one(
            "INSERT INTO frontdoor.leader (id, holder, until) VALUES (1, %s, now() + make_interval(secs => %s)) "
            "ON CONFLICT (id) DO UPDATE SET holder=EXCLUDED.holder, until=EXCLUDED.until "
            "WHERE frontdoor.leader.holder = EXCLUDED.holder OR frontdoor.leader.until < now() RETURNING holder",
            me, ttl_s)
        return bool(row)

    async def resign(self, me: str) -> None:
        await self.q("UPDATE frontdoor.leader SET until=now() WHERE holder=%s", me)

    # --- work items ---------------------------------------------------------------------------------------
    async def enqueue(self, agent: str, kind: str, payload: dict, *, conversation_id: str | None = None,
                      reply_to: dict | None = None, dedupe_key: str | None = None,
                      not_before: datetime | None = None) -> dict | None:
        """A new work item, or None when `dedupe_key` was seen before (a webhook delivered twice)."""
        row = await self.one(
            "INSERT INTO frontdoor.work_items (agent, kind, conversation_id, payload, reply_to, dedupe_key, not_before) "
            "VALUES (%s,%s,%s,%s,%s,%s,COALESCE(%s, now())) ON CONFLICT (dedupe_key) DO NOTHING RETURNING *",
            agent, kind, conversation_id, json.dumps(payload, default=str),
            json.dumps(reply_to) if reply_to else None, dedupe_key, not_before)
        if row:
            await self.notify()
        return row

    async def claim(self, agent: str, worker_id: str, lease_s: int) -> dict | None:
        """The oldest ready item for `agent`, skipping conversations already being answered (one turn at a time
        per conversation, in order). Claims for one agent are serialised so that holds across workers."""
        async with self.pool.connection() as c:
            async with c.transaction():
                await c.execute("SELECT pg_advisory_xact_lock(hashtext('claim:' || %s))", (agent,))
                cur = await c.execute(
                    "UPDATE frontdoor.work_items w SET status='claimed', worker_id=%s, attempts=attempts+1, "
                    "  lease_until=now() + make_interval(secs => %s), claimed_at=now() "
                    "FROM (SELECT i.id FROM frontdoor.work_items i WHERE i.agent=%s AND i.status='queued' "
                    "      AND i.not_before <= now() AND NOT EXISTS (SELECT 1 FROM frontdoor.work_items b "
                    "        WHERE b.agent=i.agent AND b.status='claimed' AND b.conversation_id=i.conversation_id) "
                    "      ORDER BY i.id LIMIT 1 FOR UPDATE SKIP LOCKED) n "
                    "WHERE w.id=n.id RETURNING w.*", (worker_id, lease_s, agent))
                return await cur.fetchone()

    async def extend(self, item_id: int, worker_id: str, lease_s: int) -> None:
        await self.q("UPDATE frontdoor.work_items SET lease_until=now() + make_interval(secs => %s) "
                      "WHERE id=%s AND worker_id=%s AND status='claimed'", lease_s, item_id, worker_id)

    async def finish(self, item_id: int, worker_id: str, result: dict) -> dict | None:
        row = await self.one(
            "UPDATE frontdoor.work_items SET status='done', result=%s, error=%s, finished_at=now(), lease_until=NULL "
            "WHERE id=%s AND worker_id=%s AND status='claimed' RETURNING *",
            json.dumps(result, default=str), result.get("error"), item_id, worker_id)
        if row:
            await self.notify()
        return row

    async def get_item(self, item_id: int) -> dict | None:
        return await self.one("SELECT * FROM frontdoor.work_items WHERE id=%s", item_id)

    async def requeue_expired(self, max_attempts: int) -> list[dict]:
        """Claimed items whose worker stopped heart-beating: back on the queue with a backoff, or dead after
        `max_attempts` tries."""
        return await self.q(
            "UPDATE frontdoor.work_items SET "
            "  status = CASE WHEN attempts >= %s THEN 'dead' ELSE 'queued' END, "
            "  error = CASE WHEN attempts >= %s THEN 'worker stopped answering ' || attempts || ' times' ELSE error END,"
            "  not_before = now() + make_interval(secs => 5 * power(2, attempts)), "
            "  finished_at = CASE WHEN attempts >= %s THEN now() END, worker_id=NULL, lease_until=NULL "
            "WHERE status='claimed' AND lease_until < now() RETURNING *", max_attempts, max_attempts, max_attempts)

    async def release_worker_items(self, worker_id: str) -> None:
        """The worker has gone: its items expire now rather than at the end of their lease."""
        await self.q("UPDATE frontdoor.work_items SET lease_until=now() - interval '1 second' "
                     "WHERE worker_id=%s AND status='claimed'", worker_id)

    async def dead_letter_queued(self, agent: str, reason: str) -> list[dict]:
        return await self.q("UPDATE frontdoor.work_items SET status='dead', error=%s, finished_at=now() "
                            "WHERE agent=%s AND status='queued' RETURNING *", reason, agent)

    async def pending(self) -> dict[str, int]:
        # Waiting behind a turn already running in its conversation doesn't count: another worker couldn't take it.
        rows = await self.q("SELECT agent, count(*) AS n FROM frontdoor.work_items i WHERE status='queued' "
                            "AND not_before <= now() + interval '2 seconds' AND NOT EXISTS (SELECT 1 FROM "
                            "frontdoor.work_items b WHERE b.agent=i.agent AND b.status='claimed' "
                            "AND b.conversation_id=i.conversation_id) GROUP BY agent")
        return {r["agent"]: r["n"] for r in rows}

    async def undelivered(self, limit: int = 20, exclude: list[str] | None = None) -> list[dict]:
        return await self.q("SELECT * FROM frontdoor.work_items WHERE NOT delivered AND status IN ('done','dead') "
                            "AND NOT (agent = ANY(%s)) ORDER BY id LIMIT %s", exclude or [], limit)

    async def mark_delivered(self, item_id: int) -> None:
        await self.q("UPDATE frontdoor.work_items SET delivered=true WHERE id=%s", item_id)

    # --- workers ------------------------------------------------------------------------------------------
    async def new_worker(self, agent: str, pool: str | None = None) -> tuple[str, str]:
        """A worker for `agent`, or (agent '') a pooled one that waits to be given an agent."""
        worker_id = f"w-{agent or 'pool-' + (pool or '')}-{secrets.token_hex(4)}"
        token = secrets.token_urlsafe(32)
        await self.q("INSERT INTO frontdoor.workers (id, agent, token_hash, pool, state) VALUES (%s,%s,%s,%s,%s)",
                     worker_id, agent, _hash(token), pool, "pooled" if pool and not agent else "starting")
        return worker_id, token

    async def assign_pooled(self, pool: str, agent: str) -> dict | None:
        """Give a ready pooled worker (one that has asked for an assignment) to `agent`."""
        return await self.one(
            "UPDATE frontdoor.workers SET agent=%s, state='starting', launched_at=now() WHERE id = ("
            "  SELECT id FROM frontdoor.workers WHERE pool=%s AND agent='' AND state='pooled' "
            "  AND first_seen_at IS NOT NULL ORDER BY first_seen_at LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING *",
            agent, pool)

    async def worker_by_id(self, worker_id: str) -> dict | None:
        return await self.one("SELECT * FROM frontdoor.workers WHERE id=%s", worker_id)

    async def worker_for_token(self, token: str) -> dict | None:
        return await self.one("SELECT * FROM frontdoor.workers WHERE token_hash=%s AND state <> 'gone'", _hash(token))

    async def set_worker(self, worker_id: str, **fields: Any) -> None:
        cols = ", ".join(f"{k}=%s" for k in fields)
        await self.q(f"UPDATE frontdoor.workers SET {cols} WHERE id=%s", *fields.values(), worker_id)

    async def seen(self, worker_id: str, state: str | None = None) -> None:
        await self.q("UPDATE frontdoor.workers SET last_seen=now(), first_seen_at=COALESCE(first_seen_at, now()), "
                     "state=COALESCE(%s, state) WHERE id=%s AND state <> 'gone'", state, worker_id)

    async def live_workers(self, agent: str | None = None) -> list[dict]:
        if agent:
            return await self.q("SELECT * FROM frontdoor.workers WHERE state <> 'gone' AND agent=%s", agent)
        return await self.q("SELECT * FROM frontdoor.workers WHERE state <> 'gone'")

    async def end_worker(self, worker_id: str, reason: str) -> dict | None:
        row = await self.one("UPDATE frontdoor.workers SET state='gone', ended_at=now(), end_reason=%s "
                             "WHERE id=%s AND state <> 'gone' RETURNING *", reason[:500], worker_id)
        if row:
            await self.release_worker_items(worker_id)
        return row

    # --- launch failures ----------------------------------------------------------------------------------
    async def agent_state(self, agent: str) -> dict:
        return await self.one("SELECT * FROM frontdoor.agent_state WHERE agent=%s", agent) or {
            "agent": agent, "launch_failures": 0, "blocked_until": None, "last_failure": None, "warm_until": None}

    async def hold_warm(self, agent: str, seconds: float) -> None:
        """Keep a worker up for this agent for the next `seconds` (a voice call: no cold start mid-call)."""
        await self.q("INSERT INTO frontdoor.agent_state (agent, warm_until) VALUES (%s, now() + make_interval(secs => %s)) "
                     "ON CONFLICT (agent) DO UPDATE SET warm_until = EXCLUDED.warm_until", agent, seconds)

    async def warm_agents(self) -> set[str]:
        rows = await self.q("SELECT agent FROM frontdoor.agent_state WHERE warm_until > now()")
        return {r["agent"] for r in rows}

    async def is_warm(self, agent: str) -> bool:
        return bool(await self.one("SELECT 1 AS x FROM frontdoor.agent_state WHERE agent=%s AND warm_until > now()",
                                   agent))

    async def launch_failed(self, agent: str, reason: str) -> int:
        row = await self.one(
            "INSERT INTO frontdoor.agent_state (agent, launch_failures, blocked_until, last_failure) "
            "VALUES (%s, 1, now() + interval '10 seconds', %s) ON CONFLICT (agent) DO UPDATE SET "
            "launch_failures = frontdoor.agent_state.launch_failures + 1, last_failure = EXCLUDED.last_failure, "
            "blocked_until = now() + make_interval(secs => 10 * power(2, frontdoor.agent_state.launch_failures)) "
            "RETURNING launch_failures", agent, reason[:500])
        return row["launch_failures"]

    async def launch_ok(self, agent: str) -> None:
        await self.q("INSERT INTO frontdoor.agent_state (agent) VALUES (%s) ON CONFLICT (agent) DO UPDATE SET "
                     "launch_failures=0, blocked_until=NULL", agent)

    async def block_agent(self, agent: str, until: datetime | None) -> None:
        await self.q("INSERT INTO frontdoor.agent_state (agent, blocked_until) VALUES (%s,%s) ON CONFLICT (agent) "
                     "DO UPDATE SET blocked_until=EXCLUDED.blocked_until", agent, until)

    async def reset_agent(self, agent: str) -> None:
        await self.q("DELETE FROM frontdoor.agent_state WHERE agent=%s", agent)

    # --- channels -----------------------------------------------------------------------------------------
    async def new_pairing_code(self, agent: str, created_by: str, minutes: int = 60) -> str:
        code = secrets.token_hex(10).upper()  # 80 bits: anyone on Telegram can try a code
        await self.q("INSERT INTO frontdoor.pairing_codes (code_hash, agent, created_by, expires_at) "
                     "VALUES (%s,%s,%s,%s)", _hash(code), agent, created_by, now() + timedelta(minutes=minutes))
        return code

    async def use_pairing_code(self, agent: str, code: str, used_by: str) -> bool:
        row = await self.one("UPDATE frontdoor.pairing_codes SET used_at=now(), used_by=%s WHERE code_hash=%s "
                             "AND agent=%s AND used_at IS NULL AND expires_at > now() RETURNING agent",
                             used_by, _hash(code.strip().upper()), agent)
        return bool(row)

    async def bind(self, agent: str, channel: str, address: str, user_id: str, user_name: str | None) -> None:
        await self.q("INSERT INTO frontdoor.bindings (agent, channel, address, user_id, user_name) "
                     "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (agent, channel, address) DO UPDATE SET "
                     "user_id=EXCLUDED.user_id, user_name=EXCLUDED.user_name", agent, channel, address, user_id,
                     user_name)

    async def binding(self, agent: str, channel: str, address: str) -> dict | None:
        return await self.one("SELECT * FROM frontdoor.bindings WHERE agent=%s AND channel=%s AND address=%s",
                              agent, channel, address)

    async def binding_for_user(self, agent: str, user_id: str) -> dict | None:
        return await self.one("SELECT * FROM frontdoor.bindings WHERE agent=%s AND user_id=%s "
                              "ORDER BY created_at DESC LIMIT 1", agent, user_id)

    async def kv_get(self, key: str) -> Any:
        row = await self.one("SELECT value FROM frontdoor.kv WHERE key=%s", key)
        return row["value"] if row else None

    async def kv_set(self, key: str, value: Any) -> None:
        await self.q("INSERT INTO frontdoor.kv (key, value) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET "
                     "value=EXCLUDED.value", key, json.dumps(value))


def agent_dsn(dsn: str, agent: str) -> str:
    """The DSN for one agent's own schema (agent_<name>) in the front door's database."""
    sep = "&" if "?" in dsn else "?"
    return f"{dsn}{sep}options=-csearch_path%3Dagent_{agent}"


async def agent_store(db: FrontDoorDB, agent: str):
    """That agent's PostgresStore, its schema created and migrated."""
    from ..store import PostgresStore
    if not agent.replace("_", "").isalnum():
        raise ValueError(f"bad agent name {agent!r}")
    await db.q(f"CREATE SCHEMA IF NOT EXISTS agent_{agent}")
    store = PostgresStore(agent_dsn(db.dsn, agent))
    await store.open()
    return store
