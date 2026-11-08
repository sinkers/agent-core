"""Who is speaking in a Teams meeting, and how fast the agent talks.

Speakers: the media bridge takes per-speaker (unmixed) meeting audio and reports, every ~500 ms, who spoke and
for how long, by Teams identity (POST /<agent>/voice/session with type "speakers"). Each meeting utterance is
attributed to the person who spoke most since the previous one, so the agent knows who is asking and the meeting
record says who said what.

Speaking speed: ElevenLabs sets the voice's speed per conversation (0.7–1.2, default 1.0), not mid-conversation.
So a preference is saved per person and applied when their call starts; asked for during a Teams call, it is
applied at once by having the bridge reconnect the voice session at the new speed (the Teams call stays up, and
the conversation lives on our side). In a meeting the speed applies to that meeting only.

  voice__set_speaking_speed(speed, remember?)   "slower" | "much slower" | "normal" | "faster" | a number
"""

from __future__ import annotations

import json
import os
import time
from collections import deque

import httpx

SPEED_MIN, SPEED_MAX = 0.7, 1.2
WORDS = {"much slower": 0.8, "slowest": 0.75, "slower": 0.9, "slow": 0.9, "a bit slower": 0.92, "normal": 1.0,
         "default": 1.0, "faster": 1.1, "much faster": 1.15, "fast": 1.1}
SPEED_PREF = "voice_speed"          # pref key: per person (store.get_pref(aad_id, SPEED_PREF))
WINDOW_KEEP_S = 120                 # speaker windows kept per call

# conversation call id -> deque[(at_ms, [{"aad_id", "name", "speech_ms"}])]; bridge call id -> conversation call id
_TIMELINE: dict[str, deque] = {}
_CONV_OF: dict[str, str] = {}
_LAST_SPEECH_MS: dict[str, int] = {}


def link_call(bridge_call_id: str, conversation_call_id: str) -> None:
    """The bridge's call id and the conversation's (they differ for calls we placed: the conversation uses our ref)."""
    if bridge_call_id and conversation_call_id:
        _CONV_OF[bridge_call_id] = conversation_call_id
        if len(_CONV_OF) > 500:
            _CONV_OF.pop(next(iter(_CONV_OF)))


def bridge_call_for(conversation_call_id: str) -> str:
    for bridge, conv in reversed(list(_CONV_OF.items())):
        if conv == conversation_call_id:
            return bridge
    return conversation_call_id


def record(payload: dict) -> str:
    """A speakers report from the bridge. Returns the conversation call id it was filed under."""
    bridge = str(payload.get("call_id") or "")
    conv = _CONV_OF.get(bridge, bridge)
    at = int(payload.get("at_ms") or time.time() * 1000)
    people = [{"aad_id": str(s.get("aad_id") or ""), "name": str(s.get("name") or ""),
               "speech_ms": int(s.get("speech_ms") or 0)} for s in payload.get("speakers") or []]
    line = _TIMELINE.setdefault(conv, deque())
    line.append((at, people))
    if people:
        _LAST_SPEECH_MS[conv] = at
    while line and line[0][0] < at - WINDOW_KEEP_S * 1000:
        line.popleft()
    if len(_TIMELINE) > 200:
        old = next(iter(_TIMELINE))
        _TIMELINE.pop(old, None)
        _LAST_SPEECH_MS.pop(old, None)
    return conv


def dominant(call_id: str, since_ms: int, until_ms: int | None = None) -> dict | None:
    """{"aad_id", "name"} of whoever spoke most between the two times, or None if nobody was heard."""
    until_ms = until_ms or int(time.time() * 1000)
    totals: dict[str, dict] = {}
    for at, people in _TIMELINE.get(call_id, ()):
        if since_ms <= at <= until_ms + 1000:  # +1 s: the last report can land just after the transcript
            for p in people:
                key = p["aad_id"] or p["name"]
                if not key:
                    continue
                t = totals.setdefault(key, {"aad_id": p["aad_id"], "name": p["name"], "ms": 0})
                t["ms"] += p["speech_ms"]
    if not totals:
        return None
    best = max(totals.values(), key=lambda t: t["ms"])
    return {"aad_id": best["aad_id"], "name": best["name"]}


def someone_speaking(call_id: str, within_ms: int = 700) -> bool:
    """True if anyone was heard in the last `within_ms` (step 4: don't start talking over people)."""
    last = _LAST_SPEECH_MS.get(call_id)
    return last is not None and time.time() * 1000 - last <= within_ms


def parse_speed(value) -> float:
    if isinstance(value, (int, float)):
        speed = float(value)
    else:
        text = str(value or "").strip().lower()
        speed = WORDS.get(text)
        if speed is None:
            try:
                speed = float(text.rstrip("x"))
            except ValueError:
                raise ValueError("speed is 'slower', 'much slower', 'normal', 'faster' or a number from 0.7 to 1.2")
    return round(min(SPEED_MAX, max(SPEED_MIN, speed)), 2)


async def speed_for_call(store, conversation_call_id: str, caller_aad_id: str | None, kind: str) -> float:
    """The speed a voice session should start at: one set during this call, else the caller's saved preference
    (not in meetings: a meeting's speed is for that meeting)."""
    call = await store.get_pref(f"call:{conversation_call_id}", SPEED_PREF)
    if call:
        return float(call)
    if kind != "meeting" and caller_aad_id:
        mine = await store.get_pref(caller_aad_id, SPEED_PREF)
        if mine:
            return float(mine)
    return 1.0


TOOLS = [{
    "name": "set_speaking_speed",
    "description": ("Change how fast you talk on this call, when someone asks you to slow down or speed up. speed: "
                    "'slower', 'much slower', 'normal', 'faster', or a number from 0.7 (slowest) to 1.2 (fastest); "
                    "normal is 1.0. On a Teams call it changes now (a second's pause while the voice reconnects); on "
                    "the phone it applies from their next call. remember (default true on a 1:1 call, false in a "
                    "meeting) saves it as this person's preference for future calls. On a Teams call the voice "
                    "reconnects and confirms the change itself, so reply with one word ('Sure.'); otherwise say in "
                    "one short sentence when it takes effect."),
    "inputSchema": {"type": "object", "properties": {"speed": {}, "remember": {"type": "boolean"}},
                    "required": ["speed"]}}]


class VoiceTools:
    in_process = True

    def __init__(self, store, bridge_url: str | None = None, bridge_token: str | None = None,
                 http: httpx.AsyncClient | None = None):
        self.store = store
        self.bridge_url = (bridge_url or os.environ.get("VOICE_BRIDGE_URL") or "").rstrip("/")
        self.bridge_token = bridge_token or os.environ.get("VOICE_BRIDGE_TOKEN") or ""
        self.http = http or httpx.AsyncClient(timeout=10)

    async def list_tools(self) -> list[dict]:
        return TOOLS

    async def call_tool(self, name: str, args: dict, ctx=None) -> str:
        if name != "set_speaking_speed":
            raise ValueError(f"unknown tool {name}")
        try:
            speed = parse_speed(args.get("speed"))
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        conv = getattr(ctx, "conversation_id", "") or ""
        actor = getattr(ctx, "actor", None)
        on_call = conv.startswith("call:")
        meeting = on_call and (await self.store.get_pref(conv, "voice_kind")) == "meeting"
        remember = args.get("remember")
        remember = (not meeting) if remember is None else bool(remember)
        out = {"speed": speed}
        if remember and actor and actor not in ("unknown", "github"):
            await self.store.set_pref(actor, SPEED_PREF, speed)
            out["saved"] = "as their preference for future calls"
        if not on_call:
            out["applies"] = "from their next call"
            return json.dumps(out)
        call_id = conv.removeprefix("call:")
        await self.store.set_pref(conv, SPEED_PREF, speed)
        if not (self.bridge_url and self.bridge_token):
            out["applies"] = "from their next call (this call's voice can't be changed mid-call)"
            return json.dumps(out)
        try:
            r = await self.http.post(f"{self.bridge_url}/api/voice/reconnect",
                                     json={"call_id": bridge_call_for(call_id)},
                                     headers={"Authorization": f"Bearer {self.bridge_token}"})
            ok = r.status_code < 400
        except httpx.HTTPError:
            ok = False
        # 404: not a bridge call (a phone call goes straight to ElevenLabs), so it applies next time
        out["applies"] = "now (the voice reconnects in a second)" if ok else "from the next call"
        return json.dumps(out)
