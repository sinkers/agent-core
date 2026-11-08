"""The work API between the front door and its workers: which store calls a worker may make, and how their
arguments and results travel as JSON.

A worker runs in a sandbox with no database access (docs/plans/scale-to-zero.md, section 4). It reads and writes
its agent's state through the front door (`POST /v1/worker/store/{method}`), which runs the call on that agent's
own store and nothing else. Only the methods below are allowed: claiming due schedules is the front door's job,
so `claim_due` is not among them.
"""

from __future__ import annotations

from dataclasses import asdict, fields
from datetime import datetime
from typing import Any

from .store import Message, Usage

# The Store calls a worker may make (PostgresStore's public methods, less claim_due and the pool's own).
STORE_METHODS = frozenset({
    "alert_sent", "audit", "cancel_scheduled", "conversation_ref_for_user", "count_scheduled", "create_scheduled",
    "enqueue_task", "finish_scheduled", "get_conversation_ref", "get_pref", "has_pending", "idle_conversations",
    "list_scheduled", "mark_alert", "ping", "pop_pending", "prefs_with_key", "put_pending", "recent_audit",
    "recent_elsewhere", "recent_messages", "record_usage", "save_message", "set_pref", "spend_since",
    "upsert_conversation_ref", "user_conversations",
})

_TYPES = {"Message": Message, "Usage": Usage}


def encode(v: Any) -> Any:
    """A value as JSON: datetimes and the store's dataclasses are tagged so `decode` gets the same types back."""
    if isinstance(v, datetime):
        return {"$dt": v.isoformat()}
    if isinstance(v, (Message, Usage)):
        return {"$" + type(v).__name__: {k: encode(x) for k, x in asdict(v).items()}}
    if isinstance(v, tuple):
        return {"$tuple": [encode(x) for x in v]}
    if isinstance(v, list):
        return [encode(x) for x in v]
    if isinstance(v, dict):
        return {k: encode(x) for k, x in v.items()}
    return v


def decode(v: Any) -> Any:
    if isinstance(v, list):
        return [decode(x) for x in v]
    if isinstance(v, dict):
        if len(v) == 1:
            (k, x), = v.items()
            if k == "$dt":
                return datetime.fromisoformat(x)
            if k == "$tuple":
                return tuple(decode(i) for i in x)
            if k.startswith("$") and k[1:] in _TYPES:
                cls = _TYPES[k[1:]]
                names = {f.name for f in fields(cls)}
                return cls(**{n: decode(val) for n, val in x.items() if n in names})
        return {k: decode(x) for k, x in v.items()}
    return v
