"""Agent state: conversation log, conversation references, task queue,
audit log, per-user preferences, model usage for budgets.

`PostgresStore` is the only store used in the cluster. `MemoryStore` exists for unit
tests and implements the same interface.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from .org import org
from datetime import datetime, timedelta, timezone
from importlib import resources
from typing import Any, Protocol


# Conversations never shown as "elsewhere": generated summaries, and channels whose user ids aren't people's.
EXCLUDED_ELSEWHERE = ("summary:", "github:", "jira:")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Message:
    conversation_id: str
    role: str  # "user" | "assistant"
    text: str
    user_id: str | None = None
    user_name: str | None = None
    at: datetime = field(default_factory=utcnow)


@dataclass
class Usage:
    model_id: str
    tier: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    at: datetime = field(default_factory=utcnow)
    # who, where and which session (usage reporting; set from the turn, budget.USAGE_META)
    agent: str | None = None
    conversation_id: str | None = None
    session_id: str | None = None
    channel: str | None = None
    user_id: str | None = None
    user_name: str | None = None
    trigger: str | None = None


class Store(Protocol):
    async def ping(self) -> bool: ...
    async def save_message(self, msg: Message) -> None: ...
    async def recent_messages(self, conversation_id: str, limit: int) -> list[Message]: ...
    async def upsert_conversation_ref(self, conversation_id: str, ref: dict, user_aad_id: str | None) -> None: ...
    async def get_conversation_ref(self, conversation_id: str) -> dict | None: ...
    async def conversation_ref_for_user(self, user_aad_id: str) -> dict | None: ...
    async def audit(self, entry: dict[str, Any]) -> None: ...
    async def recent_audit(self, limit: int) -> list[dict[str, Any]]: ...
    async def record_usage(self, usage: Usage) -> None: ...
    async def spend_since(self, since: datetime) -> float: ...
    async def alert_sent(self, key: str) -> bool: ...
    async def mark_alert(self, key: str) -> None: ...
    async def put_pending(self, conversation_id: str, tool: str, args: dict) -> None: ...
    async def pop_pending(self, conversation_id: str) -> tuple[str, dict] | None: ...
    async def has_pending(self, conversation_id: str) -> bool: ...
    async def user_conversations(self, user_id: str, prefix: str, since: datetime) -> list[dict]: ...
    async def recent_elsewhere(self, user_id: str, exclude: str, since: datetime, limit: int) -> list[Message]: ...
    async def idle_conversations(self, prefix: str, idle_before: datetime, since: datetime) -> list[dict]: ...
    async def get_pref(self, user_id: str, key: str) -> Any: ...
    async def set_pref(self, user_id: str, key: str, value: Any) -> None: ...
    async def prefs_with_key(self, key: str) -> list[tuple[str, Any]]: ...
    async def enqueue_task(self, kind: str, payload: dict) -> int: ...


class MemoryStore:
    def __init__(self) -> None:
        self.messages: list[Message] = []
        self.refs: dict[str, tuple[dict, str | None]] = {}
        self.audit_log: list[dict] = []
        self.usage: list[Usage] = []
        self.alerts: set[str] = set()
        self.pending: dict[str, tuple[str, dict]] = {}
        self.prefs: dict[tuple[str, str], Any] = {}
        self.tasks: list[tuple[str, dict]] = []
        self.sched: list[dict] = []

    async def ping(self) -> bool:
        return True

    async def save_message(self, msg: Message) -> None:
        self.messages.append(msg)

    async def recent_messages(self, conversation_id: str, limit: int) -> list[Message]:
        return [m for m in self.messages if m.conversation_id == conversation_id][-limit:]

    async def upsert_conversation_ref(self, conversation_id, ref, user_aad_id) -> None:
        self.refs[conversation_id] = (ref, user_aad_id)

    async def get_conversation_ref(self, conversation_id) -> dict | None:
        found = self.refs.get(conversation_id)
        return found[0] if found else None

    async def conversation_ref_for_user(self, user_aad_id) -> dict | None:
        for ref, uid in reversed(list(self.refs.values())):
            if uid == user_aad_id and ref.get("conversation", {}).get("conversationType", "personal") == "personal":
                return ref
        return None

    async def audit(self, entry) -> None:
        self.audit_log.append({"at": utcnow().isoformat(), **entry})

    async def recent_audit(self, limit) -> list[dict]:
        return self.audit_log[-limit:][::-1]

    async def record_usage(self, usage: Usage) -> None:
        self.usage.append(usage)

    async def spend_since(self, since: datetime) -> float:
        return sum(u.cost_usd for u in self.usage if u.at >= since)

    async def alert_sent(self, key) -> bool:
        return key in self.alerts

    async def mark_alert(self, key) -> None:
        self.alerts.add(key)

    async def put_pending(self, conversation_id, tool, args) -> None:
        self.pending[conversation_id] = (tool, args)

    async def pop_pending(self, conversation_id):
        return self.pending.pop(conversation_id, None)

    async def has_pending(self, conversation_id) -> bool:
        return conversation_id in self.pending

    def _summaries(self, prefix: str) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for m in self.messages:
            if not m.conversation_id.startswith(prefix):
                continue
            c = out.setdefault(m.conversation_id, {"conversation_id": m.conversation_id, "first": m.at, "last": m.at,
                                                   "turns": 0, "user_id": None, "user_name": None})
            c["first"], c["last"] = min(c["first"], m.at), max(c["last"], m.at)
            if m.role == "user" and m.user_id:
                c["turns"] += 1
                c["user_id"], c["user_name"] = m.user_id, m.user_name or c["user_name"]
        return out

    async def user_conversations(self, user_id, prefix, since) -> list[dict]:
        rows = [c for c in self._summaries(prefix).values() if c["user_id"] == user_id and c["last"] >= since]
        return sorted(rows, key=lambda c: c["last"], reverse=True)

    async def idle_conversations(self, prefix, idle_before, since) -> list[dict]:
        return [c for c in self._summaries(prefix).values() if since <= c["last"] < idle_before]

    async def recent_elsewhere(self, user_id, exclude, since, limit) -> list[Message]:
        mine = {m.conversation_id for m in self.messages if m.role == "user" and m.user_id == user_id
                and m.at >= since and m.conversation_id != exclude and not m.conversation_id.startswith(EXCLUDED_ELSEWHERE)}
        shared = {m.conversation_id for m in self.messages if m.role == "user" and (
            (m.user_id and m.user_id != user_id) or m.user_name == "Meeting")}
        shared |= {u for (u, k), v in self.prefs.items() if k == "shared_conversation" and v}
        rows = [m for m in self.messages if m.conversation_id in mine - shared and m.at >= since]
        return rows[-limit:]

    async def get_pref(self, user_id, key):
        return self.prefs.get((user_id, key))

    async def set_pref(self, user_id, key, value) -> None:
        self.prefs[(user_id, key)] = value

    async def prefs_with_key(self, key) -> list[tuple[str, Any]]:
        return [(u, v) for (u, k), v in self.prefs.items() if k == key and v is not None]

    async def enqueue_task(self, kind, payload) -> int:
        self.tasks.append((kind, payload))
        return len(self.tasks)

    # scheduled tasks
    async def create_scheduled(self, t: dict) -> int:
        self.sched.append({"deliver_via": "teams", "background": False, "origin": None, **t,
                           "id": len(self.sched) + 1, "status": "active", "run_count": 0,
                           "last_run_at": None, "last_result": None, "locked_until": None})
        return len(self.sched)

    async def list_scheduled(self, user_id: str | None) -> list[dict]:
        return [t for t in self.sched if t["status"] == "active" and (user_id is None or user_id in (t["created_by"], t["deliver_to"]))]

    async def cancel_scheduled(self, task_id: int, user_id: str | None) -> bool:
        for t in self.sched:
            if t["id"] == task_id and t["status"] == "active" and (user_id is None or t["created_by"] == user_id):
                t["status"] = "cancelled"
                return True
        return False

    async def count_scheduled(self) -> int:
        return sum(1 for t in self.sched if t["status"] == "active")

    async def claim_due(self, now: datetime, limit: int = 5) -> list[dict]:
        due = [t for t in self.sched if t["status"] == "active" and t["run_at"] <= now
               and not (t["locked_until"] and t["locked_until"] > now)][:limit]
        for t in due:
            t["locked_until"] = now.replace(microsecond=0) + timedelta(minutes=10)
        return [dict(t) for t in due]

    async def finish_scheduled(self, task_id: int, next_run: datetime | None, result: str, now: datetime) -> None:
        for t in self.sched:
            if t["id"] == task_id:
                t.update(run_count=t["run_count"] + 1, last_run_at=now, last_result=result[:2000], locked_until=None)
                if next_run:
                    t["run_at"] = next_run
                else:
                    t["status"] = "done"


class PostgresStore:
    """psycopg3 async pool. One database per agent."""

    def __init__(self, dsn: str):
        from psycopg_pool import AsyncConnectionPool

        self.pool = AsyncConnectionPool(dsn, min_size=1, max_size=5, open=False)

    async def open(self) -> None:
        await self.pool.open(wait=True, timeout=30)
        await self.migrate()

    async def close(self) -> None:
        await self.pool.close()

    async def migrate(self) -> None:
        files = sorted(f.name for f in resources.files("agentcore.migrations").iterdir() if f.name.endswith(".sql"))
        async with self.pool.connection() as conn:
            for name in files:
                await conn.execute(resources.files("agentcore.migrations").joinpath(name).read_text())

    async def _exec(self, sql: str, *args) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(sql, args)

    async def _fetch(self, sql: str, *args) -> list[tuple]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, args)
            return await cur.fetchall()

    async def ping(self) -> bool:
        try:
            await self._fetch("SELECT 1")
            return True
        except Exception:
            return False

    async def save_message(self, msg: Message) -> None:
        await self._exec(
            "INSERT INTO messages (conversation_id, role, text, user_id, user_name, at) VALUES (%s,%s,%s,%s,%s,%s)",
            msg.conversation_id, msg.role, msg.text, msg.user_id, msg.user_name, msg.at,
        )

    async def recent_messages(self, conversation_id: str, limit: int) -> list[Message]:
        rows = await self._fetch(
            "SELECT conversation_id, role, text, user_id, user_name, at FROM messages "
            "WHERE conversation_id=%s ORDER BY id DESC LIMIT %s",
            conversation_id, limit,
        )
        return [Message(*r) for r in reversed(rows)]

    async def upsert_conversation_ref(self, conversation_id, ref, user_aad_id) -> None:
        await self._exec(
            "INSERT INTO conversation_refs (conversation_id, ref, user_aad_id, updated_at) VALUES (%s,%s,%s,now()) "
            "ON CONFLICT (conversation_id) DO UPDATE SET ref=EXCLUDED.ref, "
            "user_aad_id=COALESCE(EXCLUDED.user_aad_id, conversation_refs.user_aad_id), updated_at=now()",
            conversation_id, json.dumps(ref), user_aad_id,
        )

    async def get_conversation_ref(self, conversation_id) -> dict | None:
        rows = await self._fetch("SELECT ref FROM conversation_refs WHERE conversation_id=%s", conversation_id)
        return rows[0][0] if rows else None

    async def conversation_ref_for_user(self, user_aad_id) -> dict | None:
        rows = await self._fetch(
            "SELECT ref FROM conversation_refs WHERE user_aad_id=%s "
            "AND coalesce(ref->'conversation'->>'conversationType','personal')='personal' "
            "ORDER BY updated_at DESC LIMIT 1",
            user_aad_id,
        )
        return rows[0][0] if rows else None

    async def audit(self, entry) -> None:
        await self._exec(
            "INSERT INTO audit_log (actor, action, tool, args, target, model_id, cost_usd, detail) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            entry.get("actor"), entry["action"], entry.get("tool"), json.dumps(entry.get("args")),
            entry.get("target"), entry.get("model_id"), entry.get("cost_usd"), json.dumps(entry.get("detail")),
        )

    async def recent_audit(self, limit) -> list[dict]:
        rows = await self._fetch(
            "SELECT at, actor, action, tool, args, target, model_id, cost_usd, detail FROM audit_log "
            "ORDER BY id DESC LIMIT %s",
            limit,
        )
        keys = ("at", "actor", "action", "tool", "args", "target", "model_id", "cost_usd", "detail")
        return [{k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in zip(keys, r)} for r in rows]

    async def record_usage(self, usage: Usage) -> None:
        await self._exec(
            "INSERT INTO model_usage (model_id, tier, input_tokens, output_tokens, cache_read_tokens, "
            "cache_write_tokens, cost_usd, at, agent, conversation_id, session_id, channel, user_id, user_name, trigger) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            usage.model_id, usage.tier, usage.input_tokens, usage.output_tokens,
            usage.cache_read_tokens, usage.cache_write_tokens, usage.cost_usd, usage.at, usage.agent,
            usage.conversation_id, usage.session_id, usage.channel, usage.user_id, usage.user_name, usage.trigger,
        )

    async def spend_since(self, since: datetime) -> float:
        rows = await self._fetch("SELECT coalesce(sum(cost_usd),0) FROM model_usage WHERE at >= %s", since)
        return float(rows[0][0])

    async def alert_sent(self, key) -> bool:
        return bool(await self._fetch("SELECT 1 FROM budget_alerts WHERE key=%s", key))

    async def mark_alert(self, key) -> None:
        await self._exec("INSERT INTO budget_alerts (key) VALUES (%s) ON CONFLICT DO NOTHING", key)

    async def put_pending(self, conversation_id, tool, args) -> None:
        await self._exec(
            "INSERT INTO pending_confirmations (conversation_id, tool, args) VALUES (%s,%s,%s) "
            "ON CONFLICT (conversation_id) DO UPDATE SET tool=EXCLUDED.tool, args=EXCLUDED.args, created_at=now()",
            conversation_id, tool, json.dumps(args),
        )

    async def pop_pending(self, conversation_id):
        rows = await self._fetch(
            "DELETE FROM pending_confirmations WHERE conversation_id=%s "
            "AND created_at > now() - interval '1 hour' RETURNING tool, args",
            conversation_id,
        )
        return (rows[0][0], rows[0][1]) if rows else None

    async def has_pending(self, conversation_id) -> bool:
        rows = await self._fetch("SELECT 1 FROM pending_confirmations WHERE conversation_id=%s "
                                 "AND created_at > now() - interval '1 hour'", conversation_id)
        return bool(rows)

    _CONV_SUMMARY = ("SELECT conversation_id, min(at), max(at), count(*) FILTER (WHERE role='user' AND user_id IS NOT NULL), "
                     "max(user_id) FILTER (WHERE role='user'), max(user_name) FILTER (WHERE role='user') "
                     "FROM messages WHERE conversation_id LIKE %s ")

    @staticmethod
    def _conv_rows(rows) -> list[dict]:
        return [{"conversation_id": r[0], "first": r[1], "last": r[2], "turns": r[3], "user_id": r[4], "user_name": r[5]}
                for r in rows]

    async def user_conversations(self, user_id, prefix, since) -> list[dict]:
        rows = await self._fetch(self._CONV_SUMMARY + "GROUP BY conversation_id "
                                 "HAVING max(user_id) FILTER (WHERE role='user') = %s AND max(at) >= %s "
                                 "ORDER BY max(at) DESC LIMIT 50", prefix + "%", user_id, since)
        return self._conv_rows(rows)

    async def idle_conversations(self, prefix, idle_before, since) -> list[dict]:
        rows = await self._fetch(self._CONV_SUMMARY + "GROUP BY conversation_id "
                                 "HAVING max(at) < %s AND max(at) >= %s LIMIT 50", prefix + "%", idle_before, since)
        return self._conv_rows(rows)

    async def recent_elsewhere(self, user_id, exclude, since, limit) -> list[Message]:
        """This person's recent messages (and the agent's replies) in their other private conversations: not
        group chats or meetings (another person spoke, or the conversation is marked shared), nor `exclude`."""
        rows = await self._fetch(
            "WITH mine AS (SELECT DISTINCT conversation_id FROM messages WHERE role='user' AND user_id=%s "
            "  AND at >= %s AND conversation_id <> %s AND NOT conversation_id LIKE ANY(%s)), "
            "private AS (SELECT conversation_id FROM mine WHERE NOT EXISTS ("
            "    SELECT 1 FROM messages o WHERE o.conversation_id = mine.conversation_id AND o.role='user' "
            "    AND ((o.user_id IS NOT NULL AND o.user_id <> %s) OR o.user_name = 'Meeting')) "
            "  AND NOT EXISTS (SELECT 1 FROM user_prefs p WHERE p.user_id = mine.conversation_id "
            "    AND p.key = 'shared_conversation' AND p.value = 'true'::jsonb)) "
            "SELECT conversation_id, role, text, user_id, user_name, at FROM messages "
            "WHERE conversation_id IN (SELECT conversation_id FROM private) AND at >= %s ORDER BY id DESC LIMIT %s",
            user_id, since, exclude, [f"{p}%" for p in EXCLUDED_ELSEWHERE], user_id, since, limit)
        return [Message(*r) for r in reversed(rows)]

    async def get_pref(self, user_id, key):
        rows = await self._fetch("SELECT value FROM user_prefs WHERE user_id=%s AND key=%s", user_id, key)
        return rows[0][0] if rows else None

    async def set_pref(self, user_id, key, value) -> None:
        await self._exec(
            "INSERT INTO user_prefs (user_id, key, value) VALUES (%s,%s,%s) "
            "ON CONFLICT (user_id, key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()",
            user_id, key, json.dumps(value),
        )

    async def prefs_with_key(self, key) -> list[tuple[str, Any]]:
        rows = await self._fetch("SELECT user_id, value FROM user_prefs WHERE key=%s AND value <> 'null'::jsonb", key)
        return [(r[0], r[1]) for r in rows]

    async def enqueue_task(self, kind, payload) -> int:
        rows = await self._fetch(
            "INSERT INTO tasks (kind, payload) VALUES (%s,%s) RETURNING id", kind, json.dumps(payload)
        )
        return rows[0][0]

    # scheduled tasks
    _SCHED_COLS = ("id", "instruction", "created_by", "created_by_name", "deliver_to", "deliver_to_name",
                   "run_at", "cron", "tz", "status", "run_count", "last_run_at", "last_result",
                   "deliver_via", "background", "origin")

    def _sched_row(self, r) -> dict:
        return dict(zip(self._SCHED_COLS, r))

    async def create_scheduled(self, t: dict) -> int:
        rows = await self._fetch(
            "INSERT INTO scheduled_tasks (instruction, created_by, created_by_name, deliver_to, deliver_to_name, run_at, cron, tz, "
            "deliver_via, background, origin) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            t["instruction"], t.get("created_by"), t.get("created_by_name"), t.get("deliver_to"), t.get("deliver_to_name"),
            t["run_at"], t.get("cron"), t.get("tz") or org().timezone, t.get("deliver_via") or "teams",
            bool(t.get("background")), t.get("origin"))
        return rows[0][0]

    async def list_scheduled(self, user_id: str | None) -> list[dict]:
        cols = ", ".join(self._SCHED_COLS)
        if user_id is None:
            rows = await self._fetch(f"SELECT {cols} FROM scheduled_tasks WHERE status='active' ORDER BY run_at")
        else:
            rows = await self._fetch(f"SELECT {cols} FROM scheduled_tasks WHERE status='active' "
                                     "AND (created_by=%s OR deliver_to=%s) ORDER BY run_at", user_id, user_id)
        return [self._sched_row(r) for r in rows]

    async def cancel_scheduled(self, task_id: int, user_id: str | None) -> bool:
        rows = await self._fetch(
            "UPDATE scheduled_tasks SET status='cancelled' WHERE id=%s AND status='active' "
            "AND (%s::text IS NULL OR created_by=%s) RETURNING id", task_id, user_id, user_id)
        return bool(rows)

    async def count_scheduled(self) -> int:
        return (await self._fetch("SELECT count(*) FROM scheduled_tasks WHERE status='active'"))[0][0]

    async def claim_due(self, now: datetime, limit: int = 5) -> list[dict]:
        cols = ", ".join(f"t.{c}" for c in self._SCHED_COLS)
        rows = await self._fetch(
            f"UPDATE scheduled_tasks t SET locked_until = %s + interval '10 minutes' FROM ("
            "  SELECT id FROM scheduled_tasks WHERE status='active' AND run_at <= %s "
            "  AND (locked_until IS NULL OR locked_until < %s) ORDER BY run_at LIMIT %s FOR UPDATE SKIP LOCKED"
            f") due WHERE t.id = due.id RETURNING {cols}", now, now, now, limit)
        return [self._sched_row(r) for r in rows]

    async def finish_scheduled(self, task_id: int, next_run: datetime | None, result: str, now: datetime) -> None:
        await self._exec(
            "UPDATE scheduled_tasks SET run_count=run_count+1, last_run_at=%s, last_result=%s, locked_until=NULL, "
            "run_at=COALESCE(%s, run_at), status=CASE WHEN %s::timestamptz IS NULL THEN 'done' ELSE status END WHERE id=%s",
            now, result[:2000], next_run, next_run, task_id)
