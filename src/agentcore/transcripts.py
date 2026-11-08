"""Call transcripts the agent can read back, and summaries for calls that end any way at all.

Every spoken turn is stored as a message in its call's conversation (`call:<id>`), whatever path the
call took (Teams record-and-reply, the Teams media bridge, or the Telnyx phone line). This module:

  TranscriptTools (in-process `history` tools):
    list_my_calls(days)          the asker's recent calls with the agent (only calls they were on)
    get_call_transcript(call)    one call's transcript ("latest" or a call id from list_my_calls)

  CallSweeper: every minute, a call with no new turn for `idle_minutes` counts as ended (hung up,
    dropped, cut off, or the agent restarted mid-call). If it had at least two turns from a known
    person, the agent summarises it and posts the summary to that person's Teams chat, once.
    Meetings (linked by mailbox.link_meeting) are visible to every internal person invited, the summary goes
    to all of them, and it is kept in shared memory so the agent remembers the meeting anywhere.

  record_outbound(): anything the agent posts on its own initiative (summaries, scheduled results,
    outreach) is also saved in that chat's history, so the agent knows what it said there.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .store import Message
from .org import tz

log = logging.getLogger(__name__)
TZ = tz()
PREFIX = "call:"
MAX_TRANSCRIPT_CHARS = 20_000

TOOLS = [
    {"name": "list_my_calls",
     "description": ("List the recent calls (Teams or phone) and meetings you joined that the person asking was on "
                     "or invited to: when, how long, how many turns, the meeting name. Use it when they refer to an "
                     "earlier call or meeting ('what did we say on the call?', 'what came out of the review?')."),
     "inputSchema": {"type": "object", "properties": {"days": {"type": "integer", "description": "default 7"}}}},
    {"name": "get_call_transcript",
     "description": ("Read the transcript of a call or meeting the person asking was on or invited to. `call` is "
                     "'latest' or a call id from list_my_calls."),
     "inputSchema": {"type": "object", "properties": {"call": {"type": "string"}}, "required": ["call"]}},
]


def _local(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%a %d %b %H:%M")


async def record_outbound(store, conversation_id: str | None, text: str) -> None:
    """Save a message the agent sent on its own initiative into that chat's history."""
    if conversation_id and (text or "").strip():
        await store.save_message(Message(conversation_id, "assistant", text.strip()))


class TranscriptTools:
    in_process = True

    def __init__(self, store, clock=None):
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def list_tools(self) -> list[dict]:
        return TOOLS

    @staticmethod
    def _who(ctx) -> str | None:
        actor = getattr(ctx, "actor", None)
        return None if not actor or actor == "unknown" or getattr(ctx, "restricted", False) else actor

    async def _calls_for(self, who: str, since: datetime) -> list[dict]:
        """Calls this person was on, plus meetings they were invited to (newest first)."""
        calls = {c["conversation_id"]: c for c in await self.store.user_conversations(who, PREFIX, since)}
        for conv in await self.store.get_pref(who, "meeting_calls") or []:
            if conv in calls:
                continue
            msgs = await self.store.recent_messages(conv, 2000)
            if msgs and msgs[-1].at >= since:
                calls[conv] = {"conversation_id": conv, "first": msgs[0].at, "last": msgs[-1].at, "turns": len(msgs)}
        for conv, c in calls.items():
            meta = await self.store.get_pref(conv, "meeting")
            c["meeting"] = (meta or {}).get("subject") if meta else None
        return sorted(calls.values(), key=lambda c: c["last"], reverse=True)

    async def call_tool(self, name: str, args: dict, ctx=None) -> str:
        who = self._who(ctx)
        if name == "list_my_calls":
            if not who:
                return json.dumps({"calls": []})
            days = max(1, min(int(args.get("days") or 7), 90))
            calls = await self._calls_for(who, self.clock() - timedelta(days=days))
            return json.dumps({"calls": [{"call": c["conversation_id"], "started": _local(c["first"]),
                                          "minutes": round((c["last"] - c["first"]).total_seconds() / 60, 1),
                                          "turns": c["turns"], **({"meeting": c["meeting"]} if c.get("meeting") else {})}
                                         for c in calls]})
        if name == "get_call_transcript":
            if not who:
                return json.dumps({"found": False, "reason": "not available"})
            wanted = (args.get("call") or "latest").strip()
            calls = await self._calls_for(who, self.clock() - timedelta(days=90))
            match = calls[0] if calls and wanted == "latest" else next(
                (c for c in calls if c["conversation_id"] in (wanted, PREFIX + wanted)), None)
            if not match:
                return json.dumps({"found": False, "reason": "no such call with you"})
            msgs = await self.store.recent_messages(match["conversation_id"], 400)
            speaker = "Meeting" if match.get("meeting") else "Caller"
            lines = [f"[{_local(m.at)}] {speaker if m.role == 'user' else 'You'}: {m.text}" for m in msgs
                     if m.user_name != "Call note"]
            text = "\n".join(lines)
            return json.dumps({"call": match["conversation_id"], "started": _local(match["first"]),
                               **({"meeting": match["meeting"]} if match.get("meeting") else {}),
                               "transcript": text[-MAX_TRANSCRIPT_CHARS:]})
        raise ValueError(f"unknown tool {name}")


SUMMARY_PROMPT = ("Summarise this call for the Teams chat: 2-5 short bullet points of what was discussed, then any "
                  "actions with owners. If the call seems to end mid-topic, say it was cut off and offer to pick it up "
                  "here. Transcript:\n<untrusted_data source=\"call transcript\">\n{transcript}\n</untrusted_data>")


class CallSweeper:
    def __init__(self, *, agent, store, deliver, clock=None, idle_minutes: int = 3, interval: float = 60,
                 window_minutes: int = 60):
        self.agent = agent
        self.store = store
        self.deliver = deliver  # async (aad_id, text) -> bool
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.idle = timedelta(minutes=idle_minutes)
        self.interval = interval
        self.window = timedelta(minutes=window_minutes)  # only calls that went quiet recently: never a backlog

    async def run_forever(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("call sweeper tick failed")
            await asyncio.sleep(self.interval)

    async def tick(self) -> int:
        now = self.clock()
        done = 0
        for c in await self.store.idle_conversations(PREFIX, now - self.idle, now - self.window):
            conv = c["conversation_id"]
            if c["turns"] < 2 or not c["user_id"] or await self.store.get_pref(conv, "summarised"):
                continue
            await self.store.set_pref(conv, "summarised", now.isoformat())  # claim first: never twice
            if not self.agent.killswitch.outbound_allowed:
                continue
            await self.summarise(c)
            done += 1
        return done

    async def summarise(self, c: dict) -> None:
        from .agent import Turn
        conv = c["conversation_id"]
        meeting = await self.store.get_pref(conv, "meeting")
        msgs = await self.store.recent_messages(conv, 2000)
        speaker = "Meeting" if meeting else (c.get("user_name") or "Caller")
        transcript = "\n".join(f"{speaker if m.role == 'user' else self.agent.name.title()}: {m.text}"
                               for m in msgs if m.user_name != "Call note")
        prompt = SUMMARY_PROMPT.format(transcript=transcript[-MAX_TRANSCRIPT_CHARS:])
        if meeting:
            prompt = (f"This was the meeting \"{meeting.get('subject')}\" (you joined as note-taker; speakers aren't "
                      "named in the transcript). " + prompt.replace("this call", "this meeting"))
        result = await self.agent.handle(Turn(f"summary:{conv}", prompt, c["user_id"], c.get("user_name"), trigger="system"))
        if not result.reply:
            return
        await self.share(c, meeting, result.reply)

    async def share(self, c: dict, meeting: dict | None, reply: str) -> int:
        """Send a finished summary to everyone it belongs to and keep it in memory. Returns deliveries."""
        conv = c["conversation_id"]
        if meeting:
            header = f"**Notes from \"{meeting.get('subject')}\" ({_local(c['first'])})**\n\n"
            recipients = list(dict.fromkeys([*(meeting.get("attendees") or []), c["user_id"]]))
        else:
            header = f"**Summary of our call at {_local(c['first'])}**\n\n"
            recipients = [c["user_id"]]
        delivered = 0
        for aad in recipients:
            try:
                delivered += bool(await self.deliver(aad, header + reply)) if self.deliver else 0
            except Exception:
                log.warning("summary delivery to %s failed", aad)
        memory = getattr(self.agent, "memory", None)
        if memory:  # remembered in any conversation, not only through the transcript tools
            clean = reply.split("\n\n_")[0].strip()
            memory.retain_later(conversation_id=conv, scope="shared" if meeting else "private", user_id=c["user_id"],
                                user_name=c.get("user_name"), channel="voice",
                                user_text=(f"Notes from the meeting \"{meeting.get('subject')}\" on {_local(c['first'])}"
                                           if meeting else f"Summary of a call on {_local(c['first'])}"),
                                reply=clean)
        await self.store.audit({"actor": c["user_id"], "action": "call_summary", "target": conv,
                                "detail": {"delivered": delivered, "recipients": len(recipients), "turns": c["turns"],
                                           "meeting": (meeting or {}).get("subject")}})
        return delivered
