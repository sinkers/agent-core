"""Pick up Teams chat requests that a restart cut off.

The agent marks a chat turn "in progress" while it runs (agent.py, pref `in_flight` on the conversation) and clears
it when the turn ends. A deploy, crash or node restart in between leaves the marker behind. At startup this module
finds those markers and, per conversation:

  - started within RESUME_WINDOW and resumed fewer than MAX_RESUMES times before: runs the request again with a note of
    what the agent had already done before the restart (its write tool calls, from the audit log), so it checks the
    current state and doesn't repeat them; the reply opens by saying it was restarted
  - otherwise: apologises and asks the person to send it again

Why: a deploy once replaced the agent's pod while it was building a Jira work breakdown; the work stopped,
nothing was sent, and the person found out three hours later by asking. Deploys now also drain running turns first
(app.py lifespan, terminationGracePeriodSeconds), so this is the fallback.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)
RESUME_WINDOW = timedelta(hours=3)
DRAIN_SECONDS = 240  # app.py: how long a stopping pod waits for its turns
# A new pod starts while the old one is still draining, so it waits past the old pod's shutdown window
# (terminationGracePeriodSeconds 300 in the chart) before treating anything as orphaned.
RESUME_DELAY_SECONDS = 330
MAX_RESUMES = 1  # a request cut off again after being resumed gets an apology instead


def _snippet(text: str, n: int = 90) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


async def _done_before(store, tools, user_id: str | None, started: datetime) -> list[str]:
    """Write actions (successful tool calls) this person's request had already made since it started."""
    done = []
    for a in await store.recent_audit(1000):
        at = a.get("at")
        at = datetime.fromisoformat(at) if isinstance(at, str) else at
        if not at or at < started or a.get("actor") != user_id or a.get("action") not in ("tool_call", "tool_call_confirmed"):
            continue
        rule = tools.rules.get(a.get("tool") or "") if tools else None
        if rule and not rule.writes:
            continue
        if ((a.get("detail") or {}).get("status")) not in (None, "ok"):
            continue
        args = json.dumps(a.get("args") or {})[:160]
        done.append(f"{a.get('tool')} {args}")
    return list(reversed(done))[:40]


async def resume_interrupted(agent, store, send, now: datetime | None = None) -> list[str]:
    """Handle every turn a restart left in progress. `send(conversation_id, text)` posts into that chat.
    Returns what was done per conversation (for logs and tests)."""
    from .agent import Turn
    now = now or datetime.now(timezone.utc)
    outcomes = []
    import os
    me = os.environ.get("HOSTNAME", "")
    for conv, entry in await store.prefs_with_key("in_flight"):
        if me and entry.get("pod") == me:
            continue  # this pod's own turn, still running
        await store.set_pref(conv, "in_flight", None)  # claim it: never resumed twice from one restart
        try:
            started = datetime.fromisoformat(entry["started"])
            first = (entry.get("user_name") or "").split(" ")[0] or "there"
            original = entry.get("text") or ""
            attempt = int(entry.get("attempt") or 0)
            if now - started > RESUME_WINDOW or attempt >= MAX_RESUMES:
                await send(conv, f"Sorry {first}, I was restarted while working on your request (“{_snippet(original)}”) "
                                 "and couldn't finish it. Please send it to me again.")
                outcomes.append(f"{conv}: apologised")
                continue
            done = await _done_before(store, getattr(agent, "tools", None), entry.get("user_id"), started)
            note = ("\n\n[System note: you were restarted part-way through this request, which started at "
                    f"{started.astimezone().strftime('%H:%M')}. Already done before the restart: "
                    + ("; ".join(done) if done else "nothing that changed anything (you had only been reading)")
                    + ". Check the current state before acting, don't repeat what's done, then finish the request. "
                      "Begin your reply by saying in a few words that you were restarted and have picked it up again.]")
            await store.audit({"actor": entry.get("user_id"), "action": "turn_resumed", "target": conv,
                               "detail": {"attempt": attempt + 1, "done_before": len(done), "started": entry["started"]}})
            result = await agent.handle(Turn(conv, original + note, entry.get("user_id"), entry.get("user_name"),
                                             attempt=attempt + 1, original_text=original, trigger="resume"))
            if result.reply:
                await send(conv, result.reply)
            outcomes.append(f"{conv}: resumed")
        except Exception:
            log.exception("could not resume the interrupted turn in %s", conv[:40])
            outcomes.append(f"{conv}: failed")
    return outcomes
