import asyncio
import json
import wave
import io
from urllib.parse import quote

import pytest

from agentcore.voice import (AudioStore, VoiceController, addressed_by_name, parse_meeting_link, pcm_to_wav,
                               to_pcm16k)

from .conftest import make_agent


def test_parse_classic_meeting_link():
    ctx = quote(json.dumps({"Tid": "tenant-1", "Oid": "org-1"}))
    link = f"https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc%40thread.v2/0?context={ctx}"
    info = parse_meeting_link(link)
    assert info["tenantId"] == "tenant-1"
    assert info["chatInfo"]["threadId"] == "19:meeting_abc@thread.v2"
    assert info["meetingInfo"]["organizer"]["user"]["id"] == "org-1"


def test_parse_short_meeting_link():
    info = parse_meeting_link("https://teams.microsoft.com/meet/2345678901234?p=AbCdEf")
    assert info["meetingInfo"] == {"@odata.type": "#microsoft.graph.joinMeetingIdMeetingInfo",
                                   "joinMeetingId": "2345678901234", "passcode": "AbCdEf"}


def test_addressed_by_name():
    assert addressed_by_name("Ada, raise a ticket for that", "Ada")
    assert addressed_by_name("what do you think ada?", "Ada")
    assert not addressed_by_name("Adaon sent the drawings", "Ada")


def test_wav_roundtrip_and_store_expiry():
    pcm = b"\x01\x00" * 16000
    wav = pcm_to_wav(pcm)
    with wave.open(io.BytesIO(wav)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
    assert to_pcm16k(wav) == pcm
    store = AudioStore(ttl=-1)
    key = store.put(wav)
    assert len(key) >= 32 and store.get(key) is None  # expired immediately


class FakeTransport:
    def __init__(self):
        self.ops: list[tuple] = []

    async def answer(self, call_id, callback):
        self.ops.append(("answer", call_id))

    async def join(self, link, callback):
        self.ops.append(("join", link))
        return "meet-1"

    async def play(self, call_id, uri, context):
        self.ops.append(("play", call_id, uri))

    async def record(self, call_id, context):
        self.ops.append(("record", call_id))

    async def hang_up(self, call_id):
        self.ops.append(("hangup", call_id))

    async def fetch_recording(self, location, token):
        return pcm_to_wav(speech_then_quiet())


class FakeSpeech:
    def __init__(self, heard):
        self.heard = list(heard)
        self.said: list[str] = []

    async def synthesise(self, text):
        self.said.append(text)
        return pcm_to_wav(b"\x00\x00" * 1600)

    async def transcribe(self, pcm):
        return self.heard.pop(0) if self.heard else ""


def tone(seconds: float, amplitude: int) -> bytes:
    import array, math
    n = int(16000 * seconds)
    return array.array("h", (int(amplitude * math.sin(2 * math.pi * 220 * i / 16000)) for i in range(n))).tobytes()


def speech_then_quiet(speech=1.5, quiet=1.2) -> bytes:
    return tone(speech, 8000) + tone(quiet, 60)  # comfort-noise level tail, not true silence


def notif(call_id, data, change="updated", op=None):
    url = f"/communications/calls/{call_id}" + (f"/operations/{op}" if op else "")
    return {"value": [{"changeType": change, "resourceUrl": url, "resourceData": data}]}


async def drain():
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.fixture
def controller(cfg):
    def build(script, heard):
        agent, model, store, _ = make_agent(cfg, script)
        summaries: list[tuple] = []

        async def notify(aad, text):
            summaries.append((aad, text))

        t, sp = FakeTransport(), FakeSpeech(heard)
        vc = VoiceController(agent=agent, transport=t, speech=sp, audio=AudioStore(), public_base_url="https://pub",
                             name="ada", notify_summary=notify)
        return vc, t, sp, model, summaries
    return build


async def test_p2p_call_full_turn_and_summary(controller):
    vc, t, sp, model, summaries = controller(["Sure, PRJ is on track.", "- Asked about PRJ"], ["Where is PRJ up to?"])
    incoming = {"@odata.type": "#microsoft.graph.call", "state": "incoming",
                "source": {"identity": {"user": {"id": "aad-alex"}}}}
    await vc.handle(notif("c1", incoming, change="created"))
    assert t.ops[0] == ("answer", "c1")
    await vc.handle(notif("c1", {"@odata.type": "#microsoft.graph.call", "state": "established"}))
    assert "I'm an AI agent, owned by A. Owner" in sp.said[0]  # first contact: full disclosure
    assert t.ops[-1][0] == "play" and t.ops[-1][2].startswith("https://pub/ada/audio/")
    await vc.handle(notif("c1", {"@odata.type": "#microsoft.graph.playPromptOperation", "status": "completed"}, op="o1"))
    assert t.ops[-1] == ("record", "c1")
    await vc.handle(notif("c1", {"@odata.type": "#microsoft.graph.recordOperation", "status": "completed",
                                 "recordingLocation": "https://rec", "recordingAccessToken": "tok"}, op="o2"))
    await drain()
    assert model.calls[0]["tier"] == "fast"
    assert "live Teams call" in "\n".join(model.calls[0]["system"])
    assert sp.said[-1] == "Sure, PRJ is on track."  # no text footer on voice
    await vc.handle(notif("c1", {"@odata.type": "#microsoft.graph.call", "state": "terminated"}))
    await drain()
    assert summaries and summaries[0][0] == "aad-alex" and "Call summary" in summaries[0][1]


async def test_meeting_only_answers_when_named(controller):
    vc, t, sp, model, _ = controller(["Ticket raised."], ["we should order the switchgear", "Ada, raise a ticket for that"])
    call_id = await vc.join_meeting("https://teams.microsoft.com/meet/123?p=x", "aad-alex")
    await vc.handle(notif(call_id, {"@odata.type": "#microsoft.graph.call", "state": "established"}))
    assert sp.said == []  # nobody there yet
    roster = [{"isInLobby": False, "info": {"identity": {"user": {"id": "aad-alex"}}}},
              {"isInLobby": False, "info": {"identity": {"application": {"id": "bot"}}}}]
    await vc.handle({"value": [{"changeType": "updated", "resourceUrl": f"/communications/calls/{call_id}/participants",
                                "resourceData": roster}]})
    assert "AI note-taker" in sp.said[0]  # PLT-055 notice in meetings
    rec = {"@odata.type": "#microsoft.graph.recordOperation", "status": "completed",
           "recordingLocation": "https://rec", "recordingAccessToken": "tok"}
    await vc.handle(notif(call_id, rec, op="o1"))
    await drain()
    assert model.calls == [] and t.ops[-1] == ("record", call_id)  # listened, did not speak
    await vc.handle(notif(call_id, rec, op="o2"))
    await drain()
    assert len(model.calls) == 1 and sp.said[-1] == "Ticket raised."


async def test_failed_turn_apologises_and_keeps_listening(controller):
    vc, t, sp, model, _ = controller(["unused"], [])

    async def broken(location, token):
        raise RuntimeError("download failed")

    t.fetch_recording = broken
    await vc.handle(notif("c2", {"@odata.type": "#microsoft.graph.call", "state": "incoming",
                                 "source": {"identity": {"user": {"id": "aad-alex"}}}}, change="created"))
    await vc.handle(notif("c2", {"@odata.type": "#microsoft.graph.recordOperation", "status": "completed",
                                 "recordingLocation": "https://rec", "recordingAccessToken": "tok"}, op="o1"))
    await drain()
    assert "didn't catch that" in sp.said[-1] and t.ops[-1][0] == "play"
    assert not vc._tasks  # finished tasks are released


async def test_killswitch_blocks_answering(controller):
    vc, t, *_ = controller([], [])
    vc.agent.killswitch.apply({"ada": "stop"})
    await vc.handle(notif("c9", {"@odata.type": "#microsoft.graph.call", "state": "incoming",
                                 "source": {"identity": {"user": {"id": "x"}}}}, change="created"))
    assert t.ops == []


async def test_elevenlabs_tts_and_polly_fallback():
    import httpx
    from agentcore.voice import ElevenLabsTTS

    class Polly:
        async def synthesise(self, text):
            return b"POLLY"

        async def transcribe(self, pcm):
            return "heard"

    calls = []

    def ok(req):
        calls.append(req)
        return httpx.Response(200, content=b"\x00\x01" * 800)

    tts = ElevenLabsTTS("k", "steve", "eleven_flash_v2_5", Polly(), http=httpx.AsyncClient(transport=httpx.MockTransport(ok)))
    wav = await tts.synthesise("G'day")
    assert wav[:4] == b"RIFF" and calls[0].url.path.endswith("/steve/stream")
    assert calls[0].url.params["output_format"] == "pcm_16000" and calls[0].headers["xi-api-key"] == "k"
    assert await tts.transcribe(b"") == "heard"
    bad = ElevenLabsTTS("k", "steve", "m", Polly(), http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(402))))
    assert await bad.synthesise("hi") == b"POLLY"


async def test_hangup_mid_turn_is_quiet(controller):
    vc, t, sp, model, _ = controller(["answer"], ["question"])

    async def gone(call_id, uri, context):
        raise RuntimeError('Graph /communications/calls/x/playPrompt 404: {"error":{"code":"8522","message":"Call not found."}}')

    await vc.handle(notif("c3", {"@odata.type": "#microsoft.graph.call", "state": "incoming",
                                 "source": {"identity": {"user": {"id": "u"}}}}, change="created"))
    t.play = gone
    await vc.handle(notif("c3", {"@odata.type": "#microsoft.graph.recordOperation", "status": "completed",
                                 "recordingLocation": "https://rec", "recordingAccessToken": "tok"}, op="o1"))
    await drain()
    assert not any("didn't catch" in x for x in sp.said)  # no apology into a dead call


async def test_intro_only_on_first_contact_then_natural_greeting():
    from datetime import datetime, timezone
    from agentcore.store import MemoryStore
    from agentcore.voice import first_contact_greeting
    store = MemoryStore()
    first = await first_contact_greeting(store, "aad-erin", "Erin Hall", "Ada", "A. Owner")
    assert first.startswith("G'day Erin, Ada here. I'm an AI agent")
    morning = datetime(2026, 10, 1, 21, 0, tzinfo=timezone.utc)  # 08:00 in Hobart (AEDT +11)
    again = await first_contact_greeting(store, "aad-erin", "Erin Hall", "Ada", "A. Owner", now=morning)
    assert again == "Morning Erin, what can I do for you?"
    evening = datetime(2026, 10, 1, 8, 30, tzinfo=timezone.utc)  # 19:30 in Hobart
    assert (await first_contact_greeting(store, "aad-erin", None, "Ada", "A. Owner", now=evening)).startswith("Evening,")


def test_utterance_state_detects_end_of_speech_over_comfort_noise():
    from agentcore.voice import utterance_state
    assert utterance_state(tone(3, 60)) == "none"                # comfort noise only
    assert utterance_state(tone(1, 60) + tone(2, 8000)) == "speaking"  # still talking at chunk end
    assert utterance_state(speech_then_quiet()) == "ended"


async def test_mid_sentence_chunks_are_joined(controller):
    vc, t, sp, model, _ = controller(["Answer."], ["Where is PRJ up to this week?"])
    chunks = iter([tone(1, 60) + tone(3, 8000), tone(1, 8000) + tone(1.2, 60)])  # speech spans two chunks

    async def fetch(location, token):
        return pcm_to_wav(next(chunks))
    t.fetch_recording = fetch
    await vc.handle(notif("c5", {"@odata.type": "#microsoft.graph.call", "state": "incoming",
                                 "source": {"identity": {"user": {"id": "u"}}}}, change="created"))
    rec = {"@odata.type": "#microsoft.graph.recordOperation", "status": "completed",
           "recordingLocation": "https://rec", "recordingAccessToken": "tok"}
    await vc.handle(notif("c5", rec, op="o1"))
    await drain()
    assert t.ops[-1] == ("record", "c5") and model.calls == []  # still speaking: listen for more
    await vc.handle(notif("c5", rec, op="o2"))
    await drain()
    assert len(model.calls) == 1 and sp.said[-1] == "Answer."


async def test_cancelled_empty_recordings_back_off(controller):
    # On hold (or another call took over), Graph cancels each recording at once with no audio.
    # Re-recording immediately spun ~380 times a minute; it must back off instead.
    vc, t, *_ = controller([], [])
    slept = []

    async def fake_sleep(s):
        slept.append(s)
    vc._sleep = fake_sleep

    async def fetch(location, token):
        return pcm_to_wav(tone(0.1, 0))
    t.fetch_recording = fetch
    await vc.handle(notif("c6", {"@odata.type": "#microsoft.graph.call", "state": "incoming",
                                 "source": {"identity": {"user": {"id": "u"}}}}, change="created"))
    rec = {"@odata.type": "#microsoft.graph.recordOperation", "status": "completed", "completionReason": "operationCanceled",
           "recordingLocation": "https://rec", "recordingAccessToken": "tok"}
    for i in range(6):
        await vc.handle(notif("c6", rec, op=f"o{i}"))
        await drain()
    assert slept and slept == sorted(slept) and slept[-1] >= 2  # growing pauses
    assert max(slept) <= 5


async def test_meeting_join_goes_to_the_media_bridge_when_configured(monkeypatch):
    import httpx
    from agentcore.app import join_meeting
    seen = {}

    def handler(req):
        seen.update(url=str(req.url), auth=req.headers.get("authorization"), body=json.loads(req.content))
        return httpx.Response(202, json={"callId": "c-1"})
    monkeypatch.setenv("VOICE_BRIDGE_URL", "https://bridge.example")
    monkeypatch.setenv("VOICE_BRIDGE_TOKEN", "tok")
    call_id = await join_meeting({"voice": None}, "https://teams.microsoft.com/l/meetup-join/x", "aad-alex",
                                 http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert call_id == "c-1" and seen["url"] == "https://bridge.example/api/join" and seen["auth"] == "Bearer tok"
    assert seen["body"] == {"joinUrl": "https://teams.microsoft.com/l/meetup-join/x", "requestedBy": "aad-alex"}
