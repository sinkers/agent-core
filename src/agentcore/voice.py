"""Voice for Teams calls and meetings: interim turn-based pipeline.

Until Azure Communication Services is available, the agent's bot handles calls
itself through the Microsoft Graph cloud-communications API with service-hosted media:

    record (Graph recordResponse, ends on ~2 s of silence)
      -> Amazon Transcribe streaming, en-AU
      -> the agent loop on the `fast` tier, in spoken style
      -> Amazon Polly (en-AU neural) -> WAV served on /<agent>/audio/<id>.wav
      -> Graph playPrompt -> record again

It is turn-based, not streaming, so replies take about 3–5 s instead of a streaming pipeline's ~1.2 s,
and there is no barge-in. The SpeechBackend / CallTransport split lets an ACS pipeline
replace this without changing agent logic.

Meetings: the agent announces itself on join and answers only when addressed by
name. Every turn is stored; a summary goes to the requester's Teams chat after the call.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import secrets
import shutil
import subprocess
import time
import uuid
import wave
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo

import httpx
from .org import tz, org

log = logging.getLogger(__name__)
GRAPH = "https://graph.microsoft.com/v1.0"

MEETING_LINK_RE = re.compile(r"https://teams\.(?:microsoft|live)\.com/(?:l/meetup-join/[^\s>\"')]+|meet/[^\s>\"')]+)")

VOICE_STYLE = (
    "Reply in one to three short spoken sentences, plain words only: no markdown, lists, links, emoji or "
    "document IDs read character by character."
)


MEETING_MANNERS = (
    "MEETING: you are one participant in a Teams meeting with several people, not on a 1:1 call. You only hear "
    "what people say, and you only speak when someone speaks to you. Manners: answer the question you were asked "
    "and stop; no small talk, no 'anything else?', 'holler if you need me' or 'catch you later' (people will ask "
    "if they need more). Don't respond to remarks about you or banter between others, even if it mentions you. "
    "Keep answers short (two or three sentences); offer to put detail in the meeting chat rather than reading it "
    "out. If you need something from them (which project, which board), ask one specific question. If someone "
    "asks you to talk slower or faster, use voice__set_speaking_speed (in a meeting it's for this meeting only). "
    "Never promise what you can't do. "
)


def voice_context(medium: str, verified: bool = True, *, email: str | None = None, background: bool = False,
                  usual: str | None = None, meeting: bool = False) -> str:
    """What the agent must know about a live call (medium: 'teams' | 'phone'): the caller can only hear it, so
    anything written goes to another channel, and long work goes to the background rather than the line.

    `email` is the agent's own address (people can send it things), `background` whether it can start
    background tasks (scheduler.start_background_task), `usual` how this person last chose to get results."""
    where = "a live Teams meeting" if meeting else "a live Teams call" if medium == "teams" else "a phone call"
    text = (MEETING_MANNERS if meeting else "") + (f"CHANNEL: you are on {where}. The caller can only hear you: on this call they can't see text, links, "
            "tables or files, and they can't send you anything. So never say 'here', 'in this chat' or 'see below', "
            "and never ask them to paste, send, upload or look at something on the call. ")
    if not verified:
        return text + (f"You can't send them anything, so for anything written, ask them to email {org().name}. "
                       "Never say you have sent, posted or saved anything. " + VOICE_STYLE)
    text += ("Getting things to them: a short note, link or list goes to their Teams chat with you "
             "(teams__send_teams_message to them). A file or attachment goes by email (files__email_file to their "
             f"{org().name} address) or to their Teams chat (files__send_file); if they haven't said which, ask. Then say "
             "exactly where it went ('it's in your Teams chat', 'I've emailed it to you'). ")
    text += ("Getting things from them: ask them to put it in your Teams chat"
             + (f" or email it to {email}" if email else "") + ", and say you'll pick it up there. ")
    if background:
        text += ("BEFORE YOU START ANY WORK, judge how long it will take. Quick (one or two lookups: a ticket, a "
                 "status, a date, who's on what): do it now while they wait. Longer (several searches or tickets, "
                 "reading or drafting a document, making a file or plan, a review or analysis: anything likely to "
                 "take more than about 20 seconds): don't keep them waiting on the line. Say you'll do it in the "
                 "background and ask how they'd like the result: a Teams message, an email, or a call back"
                 + (f" (last time they chose {usual}, so offer that first)" if usual else "") + ". Once they say, "
                 "call schedule__start_background_task with an instruction that stands on its own (everything you "
                 "need from this call: names, ticket keys, documents, what they want back) and their choice, then "
                 "tell them in one sentence how and roughly when they'll get it. ")
    text += ("If they ask you to talk slower or faster, call voice__set_speaking_speed (it's saved for them for "
             "future calls). ")
    return text + ("Never say you have sent, posted or saved anything unless a tool call in this conversation "
                   "confirmed it. " + VOICE_STYLE)


# --------------------------------------------------------------------------- audio helpers

def pcm_to_wav(pcm: bytes, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def to_pcm16k(audio: bytes) -> bytes:
    """Any container Graph hands back -> raw s16le mono 16 kHz."""
    try:
        with wave.open(io.BytesIO(audio)) as w:
            if w.getnchannels() == 1 and w.getsampwidth() == 2 and w.getframerate() == 16000:
                return w.readframes(w.getnframes())
    except wave.Error:
        pass
    if not shutil.which("ffmpeg"):
        raise RuntimeError("recording is not 16 kHz mono PCM WAV and ffmpeg is not installed")
    out = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-f", "s16le", "-ac", "1", "-ar", "16000", "pipe:1"],
        input=audio, capture_output=True, check=True,
    )
    return out.stdout


CHUNK_SECONDS = 4          # length of each recordResponse chunk
MAX_UTTERANCE_SECONDS = 30  # stop collecting and answer after this much audio
FRAME = 3200               # 100 ms of 16 kHz s16le mono


def _rms(pcm: bytes) -> float:
    import array
    samples = array.array("h", pcm[: len(pcm) - len(pcm) % 2])
    if not samples:
        return 0.0
    return (sum(x * x for x in samples[::4]) / max(1, len(samples[::4]))) ** 0.5


def levels(pcm: bytes) -> tuple[int, int]:
    """(peak, tail) RMS over 100 ms frames, for calibration logs."""
    frames = [_rms(pcm[i:i + FRAME]) for i in range(0, len(pcm) - FRAME + 1, FRAME)] or [0.0]
    return int(max(frames)), int(max(frames[-8:]))


def utterance_state(pcm: bytes, tail_seconds: float = 0.8) -> str:
    """'none' (no speech yet), 'speaking' (speech runs into the end), or 'ended' (speech then a quiet tail).

    Relative, not absolute: Teams comfort noise is far quieter than speech, but never zero."""
    frames = [_rms(pcm[i:i + FRAME]) for i in range(0, len(pcm) - FRAME + 1, FRAME)]
    if not frames:
        return "none"
    peak = max(frames)
    if peak < 500:  # nothing louder than background noise
        return "none"
    tail = frames[-max(1, int(tail_seconds * 10)):]
    return "ended" if max(tail) < 0.20 * peak else "speaking"  # 0.15 was too tight on a real call (pause at 16%)


def addressed_by_name(text: str, name: str) -> bool:
    return re.search(rf"\b{re.escape(name)}\b", text, re.IGNORECASE) is not None


def parse_meeting_link(url: str) -> dict:
    """Teams join link -> Graph meetingInfo (+ chatInfo for classic links)."""
    parsed = urlparse(url)
    if "/meet/" in parsed.path:
        meeting_id = parsed.path.rsplit("/meet/", 1)[1].strip("/")
        passcode = parse_qs(parsed.query).get("p", [None])[0]
        info = {"@odata.type": "#microsoft.graph.joinMeetingIdMeetingInfo", "joinMeetingId": meeting_id}
        if passcode:
            info["passcode"] = passcode
        return {"meetingInfo": info}
    parts = parsed.path.split("/")
    thread_id = unquote(parts[parts.index("meetup-join") + 1])
    message_id = unquote(parts[parts.index("meetup-join") + 2]) if len(parts) > parts.index("meetup-join") + 2 else "0"
    context = json.loads(unquote(parse_qs(parsed.query).get("context", ["{}"])[0]) or "{}")
    if not context.get("Oid") or not context.get("Tid"):
        raise ValueError("meeting link has no organiser context")
    return {
        "tenantId": context["Tid"],
        "chatInfo": {"@odata.type": "#microsoft.graph.chatInfo", "threadId": thread_id, "messageId": message_id},
        "meetingInfo": {
            "@odata.type": "#microsoft.graph.organizerMeetingInfo",
            "organizer": {"@odata.type": "#microsoft.graph.identitySet",
                          "user": {"@odata.type": "#microsoft.graph.identity", "id": context["Oid"],
                                   "tenantId": context["Tid"]}},
            "allowConversationWithoutHost": True,
        },
    }


# --------------------------------------------------------------------------- speech backends

class AwsSpeech:
    """Amazon Polly (TTS) + Amazon Transcribe streaming (STT). Region ap-southeast-2."""

    def __init__(self, region: str, voice_id: str, language: str = "en-AU", engine: str = "neural"):
        import boto3

        self.region = region
        self.voice_id = voice_id
        self.language = language
        self.engine = engine
        self._polly = boto3.client("polly", region_name=region)

    async def synthesise(self, text: str) -> bytes:
        def _run() -> bytes:
            # LanguageCode is the transcription language; the Polly voice carries its own accent.
            r = self._polly.synthesize_speech(Text=text, OutputFormat="pcm", SampleRate="16000",
                                              VoiceId=self.voice_id, Engine=self.engine)
            return r["AudioStream"].read()
        return pcm_to_wav(await asyncio.to_thread(_run))

    async def transcribe(self, pcm: bytes) -> str:
        from amazon_transcribe.client import TranscribeStreamingClient
        from amazon_transcribe.handlers import TranscriptResultStreamHandler

        finals: list[str] = []

        class Handler(TranscriptResultStreamHandler):
            async def handle_transcript_event(self, event):
                for result in event.transcript.results:
                    if not result.is_partial and result.alternatives:
                        finals.append(result.alternatives[0].transcript)

        client = TranscribeStreamingClient(region=self.region)
        stream = await client.start_stream_transcription(
            language_code=self.language, media_sample_rate_hz=16000, media_encoding="pcm")

        async def feed():
            chunk = 3200  # 100 ms
            for i in range(0, len(pcm), chunk):
                await stream.input_stream.send_audio_event(audio_chunk=pcm[i:i + chunk])
            await stream.input_stream.end_stream()

        await asyncio.gather(feed(), Handler(stream.output_stream).handle_events())
        return " ".join(finals).strip()


class ElevenLabsTTS:
    """ElevenLabs streaming TTS (Flash) for the voice, Amazon Transcribe for listening.

    Only the sentence the agent speaks goes to ElevenLabs. Any ElevenLabs failure falls
    back to the Polly voice so a call never goes silent.
    """

    def __init__(self, api_key: str, voice_id: str, model: str, fallback: AwsSpeech,
                 http: httpx.AsyncClient | None = None):
        self.api_key = api_key
        self.voice_id = voice_id
        self.model = model
        self.fallback = fallback
        self.http = http or httpx.AsyncClient(timeout=20)

    async def synthesise(self, text: str) -> bytes:
        started = time.monotonic()
        try:
            r = await self.http.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}/stream",
                params={"output_format": "pcm_16000", "optimize_streaming_latency": 3},
                headers={"xi-api-key": self.api_key},
                json={"text": text, "model_id": self.model})
            r.raise_for_status()
            log.info("elevenlabs tts %d chars in %.2f s", len(text), time.monotonic() - started)
            return pcm_to_wav(r.content)
        except Exception as exc:
            log.warning("elevenlabs tts failed (%s); falling back to Polly", str(exc)[:200])
            return await self.fallback.synthesise(text)

    async def transcribe(self, pcm: bytes) -> str:
        return await self.fallback.transcribe(pcm)


class AudioStore:
    """Short-lived prompts Graph fetches by URL. IDs are unguessable; entries expire."""

    def __init__(self, ttl: float = 600):
        self.ttl = ttl
        self._items: dict[str, tuple[bytes, float]] = {}

    def put(self, wav: bytes) -> str:
        now = time.time()
        self._items = {k: v for k, v in self._items.items() if v[1] > now}
        key = secrets.token_urlsafe(24)
        self._items[key] = (wav, now + self.ttl)
        return key

    def get(self, key: str) -> bytes | None:
        item = self._items.get(key)
        return item[0] if item and item[1] > time.time() else None


# --------------------------------------------------------------------------- Graph transport

class GraphCalls:
    def __init__(self, app_id: str, app_password: str, tenant_id: str, http: httpx.AsyncClient | None = None):
        self.app_id = app_id
        self.app_password = app_password
        self.tenant_id = tenant_id
        self.http = http or httpx.AsyncClient(timeout=30)
        self._token: tuple[str, float] | None = None

    async def _bearer(self) -> str:
        if self._token and self._token[1] - 300 > time.time():
            return self._token[0]
        r = await self.http.post(
            f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token",
            data={"grant_type": "client_credentials", "client_id": self.app_id,
                  "client_secret": self.app_password, "scope": "https://graph.microsoft.com/.default"})
        r.raise_for_status()
        b = r.json()
        self._token = (b["access_token"], time.time() + int(b.get("expires_in", 3600)))
        return self._token[0]

    async def post(self, path: str, body: dict) -> dict:
        r = await self.http.post(f"{GRAPH}{path}", json=body, headers={"Authorization": f"Bearer {await self._bearer()}"})
        log.info("graph POST %s -> %s", path.rsplit("/", 1)[-1], r.status_code)
        if r.status_code >= 400:
            raise RuntimeError(f"Graph {path} {r.status_code}: {r.text[:400]}")
        return r.json() if r.content else {}

    async def answer(self, call_id: str, callback: str) -> None:
        await self.post(f"/communications/calls/{call_id}/answer", {
            "callbackUri": callback, "acceptedModalities": ["audio"],
            "mediaConfig": {"@odata.type": "#microsoft.graph.serviceHostedMediaConfig"}})

    async def join(self, link: str, callback: str) -> str:
        info = parse_meeting_link(link)
        body = {"@odata.type": "#microsoft.graph.call", "callbackUri": callback, "requestedModalities": ["audio"],
                "mediaConfig": {"@odata.type": "#microsoft.graph.serviceHostedMediaConfig"},
                "tenantId": info.pop("tenantId", self.tenant_id), **info}
        return (await self.post("/communications/calls", body))["id"]

    async def play(self, call_id: str, uri: str, context: str) -> None:
        await self.post(f"/communications/calls/{call_id}/playPrompt", {
            "clientContext": context,
            "prompts": [{"@odata.type": "#microsoft.graph.mediaPrompt",
                         "mediaInfo": {"@odata.type": "#microsoft.graph.mediaInfo", "uri": uri,
                                       "resourceId": str(uuid.uuid4())}}]})

    async def record(self, call_id: str, context: str) -> None:
        await self.post(f"/communications/calls/{call_id}/recordResponse", {
            "clientContext": context, "bargeInAllowed": True, "playBeep": False,
            # Teams audio often never reads as "silence" (comfort noise), so Graph's silence stop is
            # unreliable. We record short chunks and detect end-of-speech ourselves (utterance_state).
            "initialSilenceTimeoutInSeconds": 20, "maxSilenceTimeoutInSeconds": 2,
            "maxRecordDurationInSeconds": CHUNK_SECONDS, "stopTones": []})

    async def hang_up(self, call_id: str) -> None:
        r = await self.http.delete(f"{GRAPH}/communications/calls/{call_id}",
                                   headers={"Authorization": f"Bearer {await self._bearer()}"})
        if r.status_code >= 400 and r.status_code != 404:
            log.warning("hang up %s: %s", call_id, r.text[:200])

    async def fetch_recording(self, location: str, token: str) -> bytes:
        r = await self.http.get(location, headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        return r.content


# --------------------------------------------------------------------------- call control

@dataclass
class CallSession:
    call_id: str
    kind: str  # "p2p" | "meeting"
    requester_aad_id: str | None
    started: float = field(default_factory=time.time)
    established: bool = False
    greeted: bool = False
    greet_on_join: bool = False  # tests: speak even if nobody else is present
    working: bool = False  # an acknowledgement is playing while the real answer is prepared
    pending_pcm: bytes = b""  # speech so far in the current utterance (across record chunks)
    caller_name: str | None = None
    turns: list[tuple[str, str]] = field(default_factory=list)  # (speaker, text)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    ended: bool = False
    cancelled_streak: int = 0  # recordings cancelled with no audio in a row (call on hold): back off


class VoiceController:
    def __init__(self, *, agent, transport: GraphCalls, speech: AwsSpeech, audio: AudioStore,
                 public_base_url: str, name: str, notify_summary, max_minutes: int = 60,
                 wake_words: list[str] | None = None):
        self._sleep = asyncio.sleep  # tests replace this
        self.agent = agent
        self.transport = transport
        self.speech = speech
        self.audio = audio
        self.base = public_base_url.rstrip("/")
        self.name = name  # agent key, e.g. "ada"
        self.first_name = agent.cfg.agent.display_name.split()[0]
        self.wake_words = [self.first_name, *(wake_words or [])]
        self.notify_summary = notify_summary  # async (requester_aad_id, text) -> None
        self.max_seconds = max_minutes * 60
        self.calls: dict[str, CallSession] = {}
        # Strong references: asyncio only keeps weak ones, so an unreferenced task can vanish mid-turn.
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @property
    def callback(self) -> str:
        return f"{self.base}/{self.name}/api/calls"

    async def _greeting(self, s: "CallSession") -> str:
        """Full AI introduction the first time someone talks to the agent; a natural greeting after that.
        Meetings always get a short note-taking notice (consent)."""
        owner = self.agent.cfg.agent.owner
        if s.kind == "meeting":
            return f"Hi all, {self.first_name} here, the AI note-taker. Say {self.first_name} if you want me."
        return await first_contact_greeting(self.agent.store, s.requester_aad_id, s.caller_name,
                                            self.first_name, owner)

    async def _say(self, s: CallSession, text: str) -> None:
        if not self.agent.killswitch.outbound_allowed:
            return
        wav = await self.speech.synthesise(text)
        key = self.audio.put(wav)
        s.turns.append((self.first_name, text))
        await self.transport.play(s.call_id, f"{self.base}/{self.name}/audio/{key}.wav", "prompt")

    async def join_meeting(self, link: str, requester_aad_id: str | None, greet_now: bool = False) -> str:
        call_id = await self.transport.join(link, self.callback)
        self.calls[call_id] = CallSession(call_id, "meeting", requester_aad_id, greet_on_join=greet_now)
        await self.agent.store.audit({"actor": requester_aad_id, "action": "meeting_join", "target": call_id,
                                      "detail": {"link": link[:200]}})
        return call_id

    async def handle(self, payload: dict) -> None:
        for n in payload.get("value", []):
            try:
                await self._one(n)
            except Exception:
                log.exception("call notification failed: %s", json.dumps(n)[:500])

    async def _one(self, n: dict) -> None:
        url = n.get("resourceUrl", "")
        m = re.search(r"/communications/calls/([^/]+)", url)
        if not m:
            return
        call_id = m.group(1)
        data = n.get("resourceData") or {}
        if isinstance(data, list):  # participants roster
            if url.endswith("/participants"):
                await self._roster(call_id, data)
            return
        kind = data.get("@odata.type", "")
        log.info("call %s: %s %s state=%s status=%s reason=%s", call_id[:8], n.get("changeType"), kind.rsplit(".", 1)[-1],
                 data.get("state"), data.get("status"), data.get("completionReason") or (data.get("resultInfo") or {}).get("message"))

        if kind == "#microsoft.graph.call":
            state = data.get("state")
            s = self.calls.get(call_id)
            if state == "incoming" and n.get("changeType") == "created":
                caller_identity = (((data.get("source") or {}).get("identity") or {}).get("user") or {})
                caller = caller_identity.get("id")
                if not self.agent.killswitch.outbound_allowed:
                    log.warning("kill switch engaged: not answering call from %s", caller)
                    return
                self.calls[call_id] = CallSession(call_id, "p2p", caller, caller_name=caller_identity.get("displayName"))
                await self.agent.store.audit({"actor": caller, "action": "call_answer", "target": call_id})
                await self.transport.answer(call_id, self.callback)
            elif state == "established" and s and not s.established:
                s.established = True
                if s.kind == "p2p" or s.greet_on_join:
                    await self._greet(s)
            elif (state == "terminated" or n.get("changeType") == "deleted") and s and not s.ended:
                s.ended = True
                self._spawn(self._finish(s))
            return

        s = self.calls.get(call_id)
        if s is None or s.ended:
            return
        status = data.get("status")
        if kind == "#microsoft.graph.playPromptOperation" and status in ("completed", "failed"):
            if s.working:
                return  # that was the "just a moment" line; the answer follows, then we listen
            if time.time() - s.started > self.max_seconds:
                await self._say(s, "That's my time limit for this call. I'll post a summary in the chat.")
                await self.transport.hang_up(call_id)
                return
            await self.transport.record(call_id, "listen")
        elif kind == "#microsoft.graph.recordOperation" and status in ("completed", "failed"):
            self._spawn(self._turn(s, data))

    async def _greet(self, s: CallSession) -> None:
        if s.greeted:
            return
        s.greeted = True
        async with s.lock:
            await self._say(s, await self._greeting(s))

    async def _roster(self, call_id: str, participants: list[dict]) -> None:
        """Meetings: greet once the first person (not a bot, not in the lobby) is present."""
        s = self.calls.get(call_id)
        if not s or s.kind != "meeting" or not s.established or s.greeted:
            return
        people = [p for p in participants if not p.get("isInLobby")
                  and ((p.get("info") or {}).get("identity") or {}).get("user")]
        if people:
            log.info("call %s: %d people present, greeting", call_id[:8], len(people))
            await self._greet(s)

    async def _turn(self, s: CallSession, data: dict) -> None:
        cid = s.call_id[:8]
        try:
            await asyncio.wait_for(self._turn_inner(s, data), timeout=45)
        except Exception as exc:
            if s.ended or "8522" in str(exc) or "Call not found" in str(exc):
                log.info("call %s: caller hung up mid-turn; dropping the reply", cid)
                return
            log.exception("call %s: turn failed (%s)", cid, type(exc).__name__)
            try:
                await self._say(s, "Sorry, I didn't catch that. Could you say it again?")
            except Exception:
                log.exception("call %s: could not recover; listening again", cid)
                await self.transport.record(s.call_id, "listen")

    async def _turn_inner(self, s: CallSession, data: dict) -> None:
        cid = s.call_id[:8]
        async with s.lock:
            started = time.monotonic()
            text = ""
            location, token = data.get("recordingLocation"), data.get("recordingAccessToken")
            log.info("call %s: recording ready=%s reason=%s", cid, bool(location and token), data.get("completionReason"))
            if location and token:
                audio = await self.transport.fetch_recording(location, token)
                chunk = to_pcm16k(audio)
                s.pending_pcm += chunk
                state = utterance_state(s.pending_pcm)
                peak, tail = levels(s.pending_pcm)
                log.info("call %s: chunk %.1f s, utterance %.1f s, state=%s peak=%d tail=%d (%.2f s)", cid,
                         len(chunk) / 32000, len(s.pending_pcm) / 32000, state, peak, tail, time.monotonic() - started)
                if state == "none" and len(s.pending_pcm) < MAX_UTTERANCE_SECONDS * 32000:
                    s.pending_pcm = b""  # nothing said yet; keep listening
                    if data.get("completionReason") == "operationCanceled" and len(chunk) < 16000:
                        # Cancelled straight away with no audio (on hold, or another call took the media):
                        # pause before trying again rather than spinning against Graph.
                        s.cancelled_streak += 1
                        await self._sleep(min(0.25 * 2 ** s.cancelled_streak, 5.0))
                        if s.ended:
                            return
                    else:
                        s.cancelled_streak = 0
                    await self.transport.record(s.call_id, "listen")
                    return
                s.cancelled_streak = 0
                if state == "speaking" and len(s.pending_pcm) < MAX_UTTERANCE_SECONDS * 32000:
                    await self.transport.record(s.call_id, "listen")  # mid-sentence: collect another chunk
                    return
                pcm, s.pending_pcm = s.pending_pcm, b""
                if len(pcm) > 16000:  # > 0.5 s of audio
                    text = await self.speech.transcribe(pcm)
            log.info("call %s: heard %r (%.2f s)", cid, text[:120], time.monotonic() - started)
            if not text:
                await self.transport.record(s.call_id, "listen")
                return
            s.turns.append(("Caller" if s.kind == "p2p" else "Meeting", text))
            if s.kind == "meeting" and not any(addressed_by_name(text, w) for w in self.wake_words):
                await self.agent.store.save_message(_msg(s, "user", text))
                await self.transport.record(s.call_id, "listen")
                return
            from .agent import Turn

            async def ack(line: str) -> None:
                s.working = True
                log.info("call %s: acknowledging after %.2f s: %r", cid, time.monotonic() - started, line[:80])
                await self._say(s, line)

            # The fast tier answers simple things itself; otherwise it says "just a moment"
            # and the work runs on a heavier tier.
            try:
                result = await self.agent.handle(Turn(f"call:{s.call_id}", text, s.requester_aad_id, None,
                                                      requested_tier=None if self.agent.cfg.models.triage else "fast",
                                                      channel="voice", scope="private" if s.kind == "p2p" else "shared",
                                                      source="teams_call" if s.kind == "p2p" else "meeting"),
                                                 on_ack=ack)
            finally:
                s.working = False
            reply = result.reply or "Sorry, I can't answer right now."
            log.info("call %s: reply ready in %.2f s (%s): %r", cid, time.monotonic() - started, result.tier, reply[:120])
            if s.ended:
                return
            await self._say(s, reply)

    async def _finish(self, s: CallSession) -> None:
        self.calls.pop(s.call_id, None)
        minutes = (time.time() - s.started) / 60
        transcript = "\n".join(f"{who}: {text}" for who, text in s.turns)
        await self.agent.store.audit({"actor": s.requester_aad_id, "action": "call_ended", "target": s.call_id,
                                      "detail": {"kind": s.kind, "minutes": round(minutes, 1), "turns": len(s.turns)}})
        if not s.requester_aad_id or len(s.turns) < 2:
            return
        conv = f"call:{s.call_id}"
        if await self.agent.store.get_pref(conv, "summarised"):
            return  # the call sweeper (transcripts.py) already did it
        await self.agent.store.set_pref(conv, "summarised", datetime.now(timezone.utc).isoformat())
        from .agent import Turn

        prompt = ("Summarise this call for the Teams chat: 2-5 bullet points of what was discussed, then any "
                  f"actions with owners. Transcript:\n<untrusted_data source=\"call transcript\">\n{transcript}\n</untrusted_data>")
        result = await self.agent.handle(Turn(f"summary:{s.call_id}", prompt, s.requester_aad_id, None, trigger="system"))
        if result.reply:
            await self.notify_summary(s.requester_aad_id, f"**Call summary** ({minutes:.0f} min, {s.kind})\n\n{result.reply}")


def time_of_day_greeting(now: datetime | None = None) -> str:
    local = (now or datetime.now(timezone.utc)).astimezone(tz())
    return "Morning" if local.hour < 12 else "Afternoon" if local.hour < 17 else "Evening"


async def first_contact_greeting(store, user_id: str | None, user_name: str | None, agent_first: str, owner: str,
                                 now: datetime | None = None) -> str:
    first = (user_name or "").split()[0] if user_name else ""
    if user_id and not await store.get_pref(user_id, "introduced"):
        await store.set_pref(user_id, "introduced", True)
        hello = f"G'day {first}, " if first else "G'day, "
        return f"{hello}{agent_first} here. I'm an AI agent, owned by {owner}. What can I do for you?"
    return f"{time_of_day_greeting(now)}{' ' + first if first else ''}, what can I do for you?"


def _msg(s: CallSession, role: str, text: str):
    from .store import Message

    return Message(f"call:{s.call_id}", role, text, s.requester_aad_id, None)
