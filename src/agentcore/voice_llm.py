"""Live voice through an ElevenLabs agent, with this agent as its "custom LLM".

  GET  /<agent>/voice/session          the Teams media bridge asks for a one-time ElevenLabs session
                                        (bearer VOICE_BRIDGE_TOKEN). The ElevenLabs key never leaves the cluster.
  POST /<agent>/v1/chat/completions    the ElevenLabs agent's brain: OpenAI-compatible, streamed (SSE)
                                        (bearer CUSTOM_LLM_TOKEN, stored as an ElevenLabs workspace secret).

ElevenLabs does turn detection, barge-in, speech-to-text (Scribe) and the voice (Steve). Each turn
reaches the normal agent loop, so memory, tools, confirms, budgets and the kill switch all apply.
The first responder's "just a moment..." is streamed first, ending in '... ' (ElevenLabs' buffer
words), then the real answer. If the caller talks over the agent, ElevenLabs drops the request; the
task is cancelled, so no further tokens are spent and no write tool starts afterwards.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import re
import logging
import time
import uuid

import httpx
from .org import agent as current_agent, org, slug

log = logging.getLogger(__name__)
EL_API = "https://api.elevenlabs.io/v1"
# While the caller waits, a spoken progress update every few seconds (progress.py): what it's doing and what it's
# found so far. Each streamed line also resets ElevenLabs' 15 s cascade timer, so the gap stays well under it.
UPDATE_EVERY_S = 7.0
MAX_UPDATES = 30  # about three and a half minutes of updates: past that the work belongs in the background


def bearer_ok(authorization: str | None, expected: str | None) -> bool:
    given = (authorization or "").removeprefix("Bearer ").strip()
    return bool(expected) and hmac.compare_digest(given, expected)


async def signed_url(api_key: str, agent_id: str, http: httpx.AsyncClient | None = None) -> str:
    client = http or httpx.AsyncClient(timeout=15)
    r = await client.get(f"{EL_API}/convai/conversation/get-signed-url", params={"agent_id": agent_id},
                         headers={"xi-api-key": api_key})
    r.raise_for_status()
    return r.json()["signed_url"]


def initiation_data(*, call_id: str, caller_aad_id: str | None, caller_name: str | None, kind: str,
                    greeting: str, speed: float = 1.0) -> dict:
    override = {"conversation_config_override": {"tts": {"speed": speed}}} if abs(speed - 1.0) > 0.005 else {}
    return {
        **override,
        "type": "conversation_initiation_client_data",
        "user_id": caller_aad_id or None,
        "dynamic_variables": {"greeting": greeting, "caller_name": caller_name or "there"},
        # Forwarded to our custom LLM with every turn as `elevenlabs_extra_body`.
        "custom_llm_extra_body": {"call_id": call_id, "caller_aad_id": caller_aad_id or "",
                                  "caller_name": caller_name or "", "kind": kind},
    }


async def outbound_teams_call(store, ref: str, call_id: str, caller_name: str) -> tuple[str, str, str]:
    """For a Teams call we placed: (conversation call id, callee name, opener). The call is kept under our own
    ref (call:{ref} already holds the call note), not the bridge's Graph call id."""
    pending = await store.get_pref(f"call-out:{ref}", "pending") if ref else None
    if not pending:
        return call_id, caller_name, f"Hi, it's {current_agent().short_name} from {org().name}, calling you back."
    return ref, pending.get("name") or caller_name, pending["opener"]


VOICE_PROBLEMS = [  # (pattern in the ElevenLabs close reason, key, what to tell the owner)
    (re.compile(r"quota_exceeded|run out of credits|insufficient.?credit|payment", re.I), "credits",
     "ElevenLabs has run out of credits, so I can't speak or listen on Teams calls, meetings or the phone line. "
     "Top up or upgrade at elevenlabs.io → Subscription; nothing else needs changing."),
    (re.compile(r"unauthori[sz]ed|invalid.?api.?key|authentication|forbidden", re.I), "auth",
     "ElevenLabs refused my voice session (authentication). The ElevenLabs API key or agent settings need checking "
     "(scripts/elevenlabs-agent-sync.py)."),
]
ALERT_EVERY = 6 * 3600


async def report_call_end(store, body: dict, notify_owner, now: float | None = None) -> dict:
    """The bridge reports how each call's voice conversation ended. Problems a person must fix (credits,
    authentication) are sent to the owner, at most once per ALERT_EVERY per problem; ordinary endings are
    only recorded. (The agent once dropped out of a standup three times because ElevenLabs credits ran
    out, and nobody was told.)"""
    reason = str(body.get("reason") or "")[:500]
    call_id = str(body.get("call_id") or "")
    await store.audit({"actor": None, "action": "voice_call_end", "target": call_id,
                       "detail": {"kind": body.get("kind"), "reason": reason, "seconds": body.get("seconds")}})
    for pattern, key, message in VOICE_PROBLEMS:
        if pattern.search(reason):
            now = time.time() if now is None else now
            last = await store.get_pref("voice-alert", key)
            if last is not None and now - last < ALERT_EVERY:
                return {"problem": key, "alerted": False}
            await store.set_pref("voice-alert", key, now)
            where = {"meeting": "a meeting", "p2p": "a Teams call", "teams-out": "a Teams call I placed"}.get(
                body.get("kind"), "a call")
            await notify_owner(f"**Voice problem:** I had to leave {where} just now. {message}")
            return {"problem": key, "alerted": True}
    return {"problem": None, "alerted": False}


def _chunk(cid: str, model: str, text: str | None = None, finish: str | None = None) -> str:
    payload = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
               "choices": [{"index": 0, "delta": ({"content": text} if text else {}), "finish_reason": finish}]}
    return f"data: {json.dumps(payload)}\n\n"


def last_user_text(messages: list[dict]) -> str:
    for m in reversed(messages or []):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, list):
                return " ".join(part.get("text", "") for part in c if isinstance(part, dict)).strip()
            return str(c or "").strip()
    return ""


SILENCE_RE = re.compile(r"^[\s.…·,!?-]*$")
# call id -> (the user text already handled, when). ElevenLabs re-sends lines and, after the agent speaks or stays
# quiet, folds everything said next into one growing user turn, so each turn is judged on its new words only.
# Several recent texts are kept: ElevenLabs can flip between a long and a short version of the same turn.
_HEARD: dict[str, tuple[list[str], float]] = {}
HEARD_KEEP = 8
# call id -> when the agent's last reply asked a real question: the next thing said is taken as the answer.
_ASKED: dict[str, float] = {}
_SPOKE_AT: dict[str, int] = {}  # call id -> wall ms of the last utterance (the start of the next speaker window)
ANSWER_WINDOW_S = 25.0
# call id -> (task, queue, narrator, when) of an answer cut off in a meeting. In a meeting ElevenLabs drops its reply
# whenever the room makes any sound ("um", "you there?"), which once lost every request it was asked.
# There the work carries on, and the answer is said on the next turn (the next words heard, or a pause).
_HELD: dict[str, tuple] = {}
HOLD_MAX_S = 90.0  # an answer older than this isn't said aloud (it's still in the meeting record)
REPEAT_WINDOW_S = 20.0  # the same words again after this long is a new request, not a re-send
GENERIC_QUESTION = re.compile(r"(anything else|need anything|what else|can i help|help you with)\W*$", re.I)
_WORD = re.compile(r"[\w'%$]+")


def _words(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text or "")]


def common_start(prev: str, text: str) -> int:
    """How many leading words two texts share (ignoring case and punctuation)."""
    a, b = _words(prev), _words(text)
    k = 0
    while k < min(len(a), len(b)) and a[k] == b[k]:
        k += 1
    return k


def new_since(heard: list[str], text: str) -> str:
    """New words in `text` against the recent texts heard: measured from the one it continues (longest shared
    start), else from the latest."""
    if not heard:
        return (text or "").strip()
    best = max(heard, key=lambda p: (common_start(p, text), heard.index(p)))
    return new_part(best if common_start(best, text) else heard[-1], text)


def new_part(prev: str, text: str) -> str:
    """The part of `text` not already in `prev`: the new words when ElevenLabs re-sends a growing transcript.
    Compared word by word, ignoring case and punctuation (Scribe revises those as it goes)."""
    if not prev:
        return (text or "").strip()
    pw, raw = _words(prev), (text or "").split()
    tw = [_words(t) for t in raw]  # words per raw token (a token can hold none, e.g. "-")
    flat = [w for ws in tw for w in ws]
    if not flat:
        return ""
    if flat == pw:
        return ""
    k = 0
    while k < min(len(pw), len(flat)) and pw[k] == flat[k]:
        k += 1
    cut = None
    if k >= max(3, int(0.6 * len(pw))) or (k == len(pw) and k > 0):
        cut = k
    else:  # the earlier text's last words appear later on (the start was trimmed or revised)
        tail = pw[-6:]
        for i in range(len(flat) - len(tail), -1, -1):
            if len(tail) >= 3 and flat[i:i + len(tail)] == tail:
                cut = i + len(tail)
                break
    if cut is None:
        return (text or "").strip()  # a fresh turn
    seen, out = 0, []
    for token, ws in zip(raw, tw):
        if seen >= cut:
            out.append(token)
        seen += len(ws)
    return " ".join(out).strip(" -")


def _quiet(cid: str, model: str):
    """Say nothing this turn, and keep the call going.

    An empty reply is NOT silence to ElevenLabs: it ends the conversation with "custom_llm_error: Brain
    returned no response" (the agent once dropped out of a standup 11 s after joining). The supported way to
    stay quiet is its built-in `skip_turn` system tool (enabled in agent_config), called like any OpenAI tool.
    """
    def raw(delta: dict, finish: str | None = None) -> str:
        payload = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
                   "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        return f"data: {json.dumps(payload)}\n\n"

    yield raw({"role": "assistant", "tool_calls": [{"index": 0, "id": f"call_{cid[-12:]}", "type": "function",
                                                     "function": {"name": "skip_turn", "arguments": ""}}]})
    yield raw({"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]})
    yield raw({}, finish="tool_calls")
    yield "data: [DONE]\n\n"


def _wake_words(agent) -> list[str]:
    first = agent.cfg.agent.display_name.split()[0]
    return [first, *getattr(agent.cfg.voice, "wake_words", [])]


async def stream_turn(agent, body: dict):
    """Async generator of SSE lines for one ElevenLabs turn."""
    from .agent import Turn
    from .store import Message
    from .voice import addressed_by_name

    extra = body.get("elevenlabs_extra_body") or {}
    call_id = extra.get("call_id") or body.get("user") or "unknown"
    text = last_user_text(body.get("messages") or [])
    cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    model = body.get("model") or current_agent().short_name.lower()
    kind = extra.get("kind", "p2p")
    held = _HELD.pop(call_id, None) if kind == "meeting" else None
    if held and time.monotonic() - held[3] <= HOLD_MAX_S:
        if text and not SILENCE_RE.match(text):  # what was said meanwhile still goes in the meeting record
            await _keep_heard(agent, call_id, text, extra)
        relay = _relay(cid, model, *held[:3], call_id=call_id, kind=kind, started=held[3])
        try:
            async for line in relay:
                yield line
        finally:
            await relay.aclose()
        return
    if held:
        held[0].cancel() if not held[0].done() else None
    # 1. Silence: ElevenLabs sends "..." when nobody speaks for a while. Answering each one had the agent talking
    #    every 10 s on an idle line, so stay quiet.
    if SILENCE_RE.match(text or ""):
        for line in _quiet(cid, model):
            yield line
        return
    # 2. Only the new words count: a re-sent line, or a growing transcript that folds in what was already
    #    heard (an old mention of its name in it once kept it answering the room's banter about it).
    now = time.monotonic()
    prev_texts, prev_at = _HEARD.get(call_id, ([], 0.0))
    fresh = new_since(prev_texts, text)
    if not fresh and prev_texts and now - prev_at > REPEAT_WINDOW_S and kind != "meeting":
        fresh = text  # the same request again, a while later (1:1 calls): answer it
    _HEARD[call_id] = ((prev_texts + [text])[-HEARD_KEEP:], now)
    if len(_HEARD) > 500:
        _HEARD.pop(next(iter(_HEARD)))
    if not fresh or SILENCE_RE.match(fresh):
        for line in _quiet(cid, model):
            yield line
        return
    # 3. Meetings: keep everything for the summary, but only speak when someone addresses it by name, or answers
    #    the question it just asked ("Which project?" "PRJ01.").
    speaker = None
    if kind == "meeting":  # who said it, from the bridge's per-speaker audio (speakers.py)
        from . import speakers
        wall = int(time.time() * 1000)
        speaker = speakers.dominant(call_id, max(_SPOKE_AT.get(call_id, 0), wall - 20000), wall)
        _SPOKE_AT[call_id] = wall
        said = f"[{speaker['name']}] {fresh}" if speaker and speaker.get("name") else fresh
        answering = now - _ASKED.pop(call_id, -1e9) <= ANSWER_WINDOW_S
        if not answering and not any(addressed_by_name(fresh, w) for w in _wake_words(agent)):
            await agent.store.save_message(Message(f"call:{call_id}", "user", said,
                                                   (speaker or {}).get("aad_id") or extra.get("caller_aad_id") or None,
                                                   "Meeting"))
            for line in _quiet(cid, model):
                yield line
            return
        fresh = said
    text = fresh

    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def ack(line: str) -> None:
        line = line.strip().rstrip(".")
        await queue.put(f"{line}... ")  # ElevenLabs buffer words: keeps speech flowing while we work

    from .progress import Narrator
    verified = bool(extra) and not (kind == "phone" and not (extra.get("caller_aad_id") or None))
    narrator = Narrator(text, verified=verified, meeting=kind == "meeting")
    narrator.writer = getattr(agent, "progress_line", None)

    async def run() -> None:
        try:
            kind = extra.get("kind", "p2p")
            aad = ((speaker or {}).get("aad_id") or extra.get("caller_aad_id") or None)
            name = (speaker or {}).get("name") or extra.get("caller_name") or None
            # A phone call is verified only if the number was registered by a staff member (phone.py).
            # Teams calls come through the media bridge, where Teams has identified the caller.
            result = await agent.handle(
                Turn(f"call:{call_id}", text, aad, name,
                     channel="voice", scope="shared" if kind == "meeting" else "private",
                     source={"phone": "phone", "meeting": "meeting"}.get(kind, "teams_call"),
                     verified=bool(extra) and not (kind == "phone" and not aad),
                     caller_number=extra.get("caller_number") or None),
                on_ack=ack, on_progress=narrator.add)
            reply = result.reply or "Sorry, I can't answer that right now."
            if reply.rstrip().endswith("?") and not GENERIC_QUESTION.search(reply.rstrip()):
                _ASKED[call_id] = time.monotonic()  # it asked something: the next thing said is the answer
            await queue.put(reply)
        except asyncio.CancelledError:
            log.info("voice turn cancelled (caller spoke over the agent or hung up)")
            raise
        except Exception:
            log.exception("voice turn failed")
            await queue.put("Sorry, something went wrong on my side.")
        finally:
            await queue.put(None)

    started = time.monotonic()
    task = asyncio.create_task(run())
    relay = _relay(cid, model, task, queue, narrator, call_id=call_id, kind=kind, started=started,
                   on_cut=lambda: _HEARD.__setitem__(call_id, (prev_texts, prev_at)))
    try:
        async for line in relay:
            yield line
    finally:
        await relay.aclose()


async def _keep_heard(agent, call_id: str, text: str, extra: dict) -> None:
    """Record words heard in a meeting while it was answering something else (and count them as heard)."""
    from .store import Message
    prev_texts, _ = _HEARD.get(call_id, ([], 0.0))
    fresh = new_since(prev_texts, text)
    _HEARD[call_id] = ((prev_texts + [text])[-HEARD_KEEP:], time.monotonic())
    if fresh and not SILENCE_RE.match(fresh):
        await agent.store.save_message(Message(f"call:{call_id}", "user", fresh,
                                               extra.get("caller_aad_id") or None, "Meeting"))


async def _relay(cid: str, model: str, task, queue, narrator, *, call_id: str, kind: str, started: float,
                 on_cut=None):
    """Stream a turn's answer: progress updates while it works, then the reply. If ElevenLabs closes the stream
    (someone spoke), a 1:1 turn is cancelled (they talked over it); a meeting turn is held for the next turn."""
    first = True
    updates = 0
    done = False
    try:
        while True:
            wait = UPDATE_EVERY_S if updates < MAX_UPDATES else None
            try:
                piece = await asyncio.wait_for(queue.get(), timeout=wait)
            except asyncio.TimeoutError:
                line = await narrator.next_line(getattr(narrator, "writer", None))
                updates += 1
                if not queue.empty():  # the answer arrived while the update was being written: skip the update
                    continue
                yield _chunk(cid, model, f"{line}... ")  # ElevenLabs buffer words keep the speech flowing
                continue
            if piece is None:
                if first and not updates:  # nothing at all to say (a held answer already spoken): stay quiet,
                    done = True            # since an empty reply ends the ElevenLabs conversation
                    for line in _quiet(cid, model):
                        yield line
                    return
                break
            if first:
                log.info("voice llm first text after %.2f s", time.monotonic() - started)
                first = False
            yield _chunk(cid, model, piece)
        done = True
        yield _chunk(cid, model, finish="stop")
        yield "data: [DONE]\n\n"
    finally:
        if not done:
            if kind == "meeting" and not (task.done() and queue.empty()):
                _HELD[call_id] = (task, queue, narrator, started)  # say it on the next turn
                log.info("meeting answer cut off by the room; holding it for the next turn")
            elif not task.done():
                task.cancel()  # barge-in on a 1:1 call: ElevenLabs closed the stream
                if first and on_cut:
                    on_cut()  # cut off before it said anything: the request wasn't handled, so hear it again


def agent_config(*, name: str, custom_llm_url: str, secret_id: str, voice_id: str, tts_model: str,
                 agent_key: str = "", display_name: str = "", greeting: str = "") -> dict:
    """ElevenLabs agent definition (applied by scripts/elevenlabs-agent-sync.py)."""
    return {
        "name": name,
        "tags": [slug(), "ai-agent", "managed-by-repo"],
        # Allow the bridge to pass the caller and call id per session (forwarded to our custom LLM),
        # and require signed URLs (issued only by our session endpoint).
        # Phone calls (Telnyx SIP trunk) fetch the caller's identity from our phone-init webhook (phone.py).
        # Speaking speed is set per conversation (speakers.py): a saved preference, or a change asked for mid-call.
        "platform_settings": {"overrides": {"custom_llm_extra_body": True,
                                            "enable_conversation_initiation_client_data_from_webhook": True,
                                            "conversation_config_override": {"tts": {"speed": True}}},
                              "auth": {"enable_auth": True}},
        "conversation_config": {
            "agent": {
                "first_message": "{{greeting}}",
                "language": "en",
                "dynamic_variables": {"dynamic_variable_placeholders": {"greeting": greeting or f"Hello, {(display_name or name).split(' (')[0]} here.",
                                                                        "caller_name": "there"}},
                "prompt": {
                    "prompt": (f"You are the voice of {display_name or name}. Every reply comes from the custom LLM; "
                               "speak it as given."),
                    "llm": "custom-llm",
                    # How long ElevenLabs waits on us before retrying (max 15 s). Tool-using answers take
                    # 8-15 s, so use the maximum and keep the stream alive with spoken progress updates (UPDATE_EVERY_S).
                    "cascade_timeout_seconds": 15,
                    # Staying quiet (silence prompts, repeats, meeting talk not addressed to it) is a skip_turn
                    # tool call; an empty reply would end the call (_quiet).
                    "built_in_tools": {"skip_turn": {"type": "system", "name": "skip_turn",
                                                     "description": "Stay silent this turn and keep listening.",
                                                     "params": {"system_tool_type": "skip_turn"}}},
                    "custom_llm": {"url": custom_llm_url, "model_id": agent_key or "agent",
                                   "api_key": {"secret_id": secret_id}},
                },
            },
            "asr": {"quality": "high", "provider": "scribe_realtime", "user_input_audio_format": "pcm_16000"},
            # ElevenLabs requires English agents to use a v2 model (flash/turbo), not v2.5.
            "tts": {"model_id": tts_model.replace("_v2_5", "_v2"), "voice_id": voice_id, "agent_output_audio_format": "pcm_16000",
                    "optimize_streaming_latency": 3},
            "turn": {"mode": "turn", "turn_eagerness": "normal", "turn_timeout": 7},
            "conversation": {"max_duration_seconds": 3600,
                             "client_events": ["audio", "interruption", "user_transcript", "agent_response", "ping"]},
        },
    }
