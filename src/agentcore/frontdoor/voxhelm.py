"""voxhelm voice-gateway chat: the front door holds the agent's socket, so the agent shows online in voxhelm
while no worker is running, and messages wake it instead of being dropped (docs/plans/scale-to-zero.md,
section 7).

voice-gateway registers an agent from the socket's query (`/ws?user_id=U&agent_id=A&client_type=agent`), one
socket per (user, agent). App messages arrive as `chat_message` {text}; answers go back as `chat_response`
{text}. The gateway pings with WebSocket ping frames (the client library answers them).

Voice calls (voice-gateway internal/handler/room.go, voice_ws.go): the gateway does the speech both ways.
  connect_to_room {room_name, session_id}  a call starts: say "listening", and have a worker up for the call
  transcript {text}                        what the caller said: "thinking", then the answer as llm_response
                                           chunks {text, stream_seq, done} (one sentence each, so speech starts
                                           on the first), "speaking", and "listening" again
The gateway doesn't tell the agent when a call ends, so the worker is kept up for CALL_WARM_S after the last
thing said. Calls and chat with the same person share one conversation, so a call can pick up a chat.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from urllib.parse import urlencode

log = logging.getLogger(__name__)


def envelope(kind: str, payload: dict) -> str:
    return json.dumps({"type": kind, "id": str(uuid.uuid4()),
                       "timestamp": datetime.now(timezone.utc).isoformat(), "payload": payload})


PING_S = 20.0  # our own pings: a gateway that went away without closing the socket is noticed within ~40 s
CALL_WARM_S = 300.0  # keep a worker up this long after the last thing said on a call
# The agent never hears when the gateway finishes speaking (tts_finished goes to the app only), so "listening"
# is sent once the answer has probably been spoken: synthesis start-up plus the text at about 14 characters a second.
TTS_START_S = 1.5
SPOKEN_CHARS_PER_S = 14.0


VOICE_HOLD_S = 5.0    # no answer yet (normal turns take 2.5-3.5 s): say a holding line, so the call doesn't go silent
VOICE_GIVE_UP_S = 20.0  # still none: apologise and listen again (a late answer is then not spoken)
HOLD_LINE = "Let me check that."
GIVE_UP_LINE = "Sorry, that's taking me too long. Could you ask me again?"


def spoken_seconds(text: str) -> float:
    return TTS_START_S + len(text) / SPOKEN_CHARS_PER_S

_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")


def sentences(text: str, min_chars: int = 40) -> list[str]:
    """Spoken in chunks so the first sentence can play while the rest are synthesised; very short sentences are
    joined to the next so the voice doesn't stop and start."""
    out: list[str] = []
    for part in _SENTENCE.split(text.strip()):
        if out and len(out[-1]) < min_chars:
            out[-1] = f"{out[-1]} {part}"
        else:
            out.append(part)
    return [p for p in out if p.strip()]


class VoxhelmLink:
    def __init__(self, agent: str, url: str, users: list[str], core, *, token: str | None = None,
                 agent_id: str | None = None, name: str = "voxhelm"):
        self.agent, self.url, self.users, self.core, self.token = agent, url, users, core, token
        self.agent_id = agent_id or agent
        self.channel = name
        self.sockets: dict[str, object] = {}
        self.calls: dict[str, dict] = {}  # user -> the call in progress (connect_to_room's payload)
        self._listening_later: dict[str, asyncio.Task] = {}  # user -> "listening" once the answer has been spoken

    async def run_forever(self) -> None:
        await asyncio.gather(*(self._user_loop(u) for u in self.users))

    async def _user_loop(self, user: str) -> None:
        import websockets
        url = f"{self.url}?{urlencode({'user_id': user, 'agent_id': self.agent_id, 'client_type': 'agent'})}"
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
        delay = 1.0
        while True:
            try:
                # Ping it ourselves too: after a gateway redeploy a socket can stay open on our side with nobody
                # behind it. No pong in time closes it, and the loop reconnects.
                async with websockets.connect(url, additional_headers=headers, ping_interval=PING_S,
                                              ping_timeout=PING_S, max_size=2**20, open_timeout=15) as ws:
                    self.sockets[user] = ws
                    log.info("%s: %s online for %s", self.channel, self.agent, user)
                    delay = 1.0
                    async for raw in ws:
                        await self._on_message(user, raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("%s socket %s/%s dropped: %s", self.channel, self.agent, user, str(exc)[:200])
            finally:
                self.sockets.pop(user, None)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    async def _on_message(self, user: str, raw) -> None:
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            return
        kind, payload = msg.get("type"), (msg.get("payload") or {})
        if kind == "ping":
            await self._send(user, "pong", {})
            return
        if kind == "connect_to_room":
            await self._call_started(user, payload)
            return
        if kind not in ("chat_message", "transcript"):
            return
        # voice-gateway's ws_pcm path sends its Go struct untagged ({"Text": ...}); the LiveKit path sends "text"
        text = (payload.get("text") or payload.get("Text") or "").strip()
        if not text:
            log.info("%s: empty %s ignored (payload keys %s)", self.channel, kind, sorted(payload)[:5])
            return
        voice = kind == "transcript"
        if voice:
            if user not in self.calls:  # a transcript without connect_to_room (a reconnect mid-call)
                self.calls[user] = {"session_id": None}
            call = self.calls[user]
            call["said"] = call.get("said", 0) + 1
            # One thought split by a pause arrives as two transcripts. While the earlier one is still unanswered,
            # withdraw it and answer everything said so far as one turn (an earlier turn already being worked on
            # is left to finish, and its answer isn't spoken: the caller has said more since).
            waiting = [p for p in call.get("pending", []) if p["said"] > call.get("closed", 0)]
            if waiting:
                text = " ".join([p["text"] for p in waiting] + [text])
                for p in waiting:
                    if p.get("item_id"):
                        await self.core.db.cancel_queued(p["item_id"])
                await self.core.db.event(self.agent, "voice_merged", channel=self.channel, parts=len(waiting) + 1)
            call["pending"] = [{"said": call["said"], "text": text}]
            await self.core.hold_warm(self.agent, CALL_WARM_S)
            await self.state(user, "thinking")
        reply_to = {"channel": self.channel, "agent": self.agent, "user": user}
        if voice:
            # numbered, so an answer overtaken by something said after it isn't spoken (the caller moved on)
            reply_to.update(voice=True, said=self.calls[user]["said"])
        if voice:
            self._spawn_watch(user, self.calls[user]["said"])
        queued = await self.core.intake(
            self.agent, text=text, conversation_id=f"{self.channel}:{user}", user_id=f"{self.channel}:{user}",
            user_name=user, reply_to=reply_to, source=f"{self.channel}-voice" if voice else self.channel,
            channel="voice" if voice else "chat",
            dedupe_key=f"{self.channel}:{self.agent}:{msg.get('id') or uuid.uuid4()}")
        if voice and isinstance(queued, dict) and queued.get("item_id"):
            for p in self.calls[user].get("pending", []):
                if p["said"] == self.calls[user]["said"]:
                    p["item_id"] = queued["item_id"]

    async def _call_started(self, user: str, payload: dict) -> None:
        self.calls[user] = {"session_id": payload.get("session_id"), "room": payload.get("room_name")}
        log.info("%s: call for %s started (session %s)", self.channel, self.agent, payload.get("session_id"))
        await self.state(user, "listening")
        await self.core.hold_warm(self.agent, CALL_WARM_S)
        await self.core.db.event(self.agent, "call_started", channel=self.channel,
                                 session=payload.get("session_id"))

    async def state(self, user: str, state: str) -> None:
        later = self._listening_later.pop(user, None)
        if later and later is not asyncio.current_task():
            later.cancel()  # a newer state (the caller spoke again) replaces the pending "listening"
        try:
            await self._send(user, "state_update", {"state": state})
        except Exception as exc:
            log.warning("%s: state %s not sent: %s", self.channel, state, str(exc)[:200])

    async def _send(self, user: str, kind: str, payload: dict) -> None:
        ws = self.sockets.get(user)
        if ws is None:
            raise RuntimeError(f"{self.channel} socket for {user} is not open")
        try:
            await ws.send(envelope(kind, payload))
        except Exception:
            await ws.close()  # a failed write: drop it so the loop reconnects
            raise

    async def typing(self, reply_to: dict) -> None:
        pass

    async def send(self, reply_to: dict, text: str, *, holding: bool = False) -> None:
        user = reply_to["user"]
        if not reply_to.get("voice"):
            await self._send(user, "chat_response", {"text": text, "metadata": {"holding": holding}})
            return
        call = self.calls.get(user) or {}
        latest = call.get("said", 0)
        if not holding and reply_to.get("said", latest) < latest:
            log.info("%s: answer to an earlier question not spoken (the caller has said more since)", self.channel)
            return
        if not holding and reply_to.get("said", latest) <= call.get("closed", 0):
            log.info("%s: late answer not spoken (the caller was told to ask again)", self.channel)
            return
        if not holding:
            call["closed"] = reply_to.get("said", latest)
        from .speech import speakable
        chunks = sentences(speakable(text, rest_note="")) or ["Sorry, I've nothing to add."]
        await self.state(user, "speaking")
        for i, chunk in enumerate(chunks):
            await self._send(user, "llm_response", {"text": chunk, "stream_seq": i, "done": i == len(chunks) - 1})
        if holding:  # a holding line ("one moment...") is followed by the answer: still thinking
            await self.state(user, "thinking")
        else:
            self._listening_later[user] = asyncio.create_task(
                self._listening_after(user, spoken_seconds(" ".join(chunks))))
        if not holding:
            await self.core.hold_warm(self.agent, CALL_WARM_S)

    def _spawn_watch(self, user: str, said: int) -> None:
        task = asyncio.create_task(self._watch(user, said))
        self.calls[user].setdefault("watches", set()).add(task)
        task.add_done_callback(self.calls[user]["watches"].discard)

    async def _watch(self, user: str, said: int) -> None:
        """A voice turn that runs long: a holding line, then an apology, so the caller never sits in silence."""
        def pending() -> bool:
            call = self.calls.get(user) or {}
            return call.get("said") == said and call.get("closed", 0) < said
        try:
            await asyncio.sleep(VOICE_HOLD_S)
            if not pending():
                return
            await self._speak(user, HOLD_LINE, then=None)
            # back to "thinking" once the line has been said, unless the answer comes first (no flicker)
            self._listening_later[user] = asyncio.create_task(
                self._listening_after(user, spoken_seconds(HOLD_LINE), state="thinking"))
            await asyncio.sleep(VOICE_GIVE_UP_S - VOICE_HOLD_S)
            if not pending():
                return
            self.calls[user]["closed"] = said
            await self.core.db.event(self.agent, "voice_gave_up", channel=self.channel, said=said)
            await self._speak(user, GIVE_UP_LINE, then=None)
            self._listening_later[user] = asyncio.create_task(self._listening_after(user, spoken_seconds(GIVE_UP_LINE)))
        except Exception as exc:
            log.warning("%s: call watch failed: %s", self.channel, str(exc)[:200])

    async def _speak(self, user: str, text: str, *, then: str | None) -> None:
        await self.state(user, "speaking")
        await self._send(user, "llm_response", {"text": text, "stream_seq": 0, "done": True})
        if then:
            await self.state(user, then)

    async def _listening_after(self, user: str, seconds: float, state: str = "listening") -> None:
        await asyncio.sleep(seconds)
        await self.state(user, state)

    async def nothing_to_say(self, reply_to: dict) -> None:
        if reply_to.get("voice"):
            await self.state(reply_to["user"], "listening")
