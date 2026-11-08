"""Who is speaking in a meeting, and speaking speed per person (speakers.py)."""
import json
import time

import httpx
import pytest

from agentcore import speakers
from agentcore.speakers import VoiceTools, dominant, parse_speed, record, speed_for_call
from agentcore.store import MemoryStore
from agentcore.tools import CallContext
from agentcore.voice_llm import initiation_data, stream_turn

from .conftest import make_agent
from .test_voice_llm import collect, parse


def report(call_id, at_ms, *people):
    return {"type": "speakers", "call_id": call_id, "at_ms": at_ms, "window_ms": 500,
            "speakers": [{"msi": i, "aad_id": aad, "name": name, "speech_ms": ms} for i, (aad, name, ms) in enumerate(people)]}


def test_the_utterance_goes_to_whoever_spoke_most_since_the_last_one():
    speakers.link_call("bridge-1", "conv-1")
    now = int(time.time() * 1000)
    record(report("bridge-1", now - 3000, ("aad-e", "Erin Hall", 400)))
    record(report("bridge-1", now - 2500, ("aad-e", "Erin Hall", 450), ("aad-a", "Alex Owner", 100)))
    record(report("bridge-1", now - 2000, ("aad-a", "Alex Owner", 200)))
    assert dominant("conv-1", now - 4000, now) == {"aad_id": "aad-e", "name": "Erin Hall"}
    assert dominant("conv-1", now - 2200, now)["name"] == "Alex Owner"
    assert dominant("conv-1", now + 10_000, now + 20_000) is None
    assert speakers.bridge_call_for("conv-1") == "bridge-1"


def test_speed_words_and_limits():
    assert parse_speed("slower") == 0.9 and parse_speed("much slower") == 0.8 and parse_speed("normal") == 1.0
    assert parse_speed(0.5) == 0.7 and parse_speed("1.5") == 1.2 and parse_speed("faster") == 1.1
    with pytest.raises(ValueError):
        parse_speed("glacial")
    assert "conversation_config_override" not in initiation_data(call_id="c", caller_aad_id="", caller_name="",
                                                                  kind="p2p", greeting="hi")
    assert initiation_data(call_id="c", caller_aad_id="", caller_name="", kind="p2p", greeting="hi",
                           speed=0.9)["conversation_config_override"] == {"tts": {"speed": 0.9}}


async def test_asking_him_to_slow_down_saves_it_and_reconnects_the_teams_voice():
    store = MemoryStore()
    posted = []

    def handler(req):
        posted.append(json.loads(req.content))
        return httpx.Response(202, json={"reconnected": True})
    tools = VoiceTools(store, "https://bridge", "tok", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    speakers.link_call("graph-call-9", "ref-9")
    await store.set_pref("call:ref-9", "voice_kind", "p2p")
    out = json.loads(await tools.call_tool("set_speaking_speed", {"speed": "slower"}, CallContext("call:ref-9", "aad-e")))
    assert out["speed"] == 0.9 and "saved" in out and out["applies"].startswith("now")
    assert posted == [{"call_id": "graph-call-9"}]  # the bridge's own call id
    assert await store.get_pref("aad-e", "voice_speed") == 0.9
    assert await speed_for_call(store, "another-call", "aad-e", "p2p") == 0.9  # next call starts slower
    # in a meeting: for this meeting only, not saved for the person
    await store.set_pref("call:m-1", "voice_kind", "meeting")
    out = json.loads(await tools.call_tool("set_speaking_speed", {"speed": "much slower"}, CallContext("call:m-1", "aad-x")))
    assert "saved" not in out and await store.get_pref("aad-x", "voice_speed") is None
    assert await speed_for_call(store, "m-1", "aad-e", "meeting") == 0.8
    assert await speed_for_call(store, "m-2", "aad-e", "meeting") == 1.0  # a person's speed isn't a meeting's
    # in a chat: saved for next time
    out = json.loads(await tools.call_tool("set_speaking_speed", {"speed": "normal"}, CallContext("teams-chat-1", "aad-e")))
    assert out["applies"] == "from their next call" and await store.get_pref("aad-e", "voice_speed") == 1.0


async def test_meeting_turns_say_who_spoke(cfg):
    agent, model, store, _ = make_agent(cfg, ["Sam has it."])
    now = int(time.time() * 1000)
    speakers.link_call("mt-1", "mt-1")
    record(report("mt-1", now - 1000, ("aad-e", "Erin Hall", 900)))
    body = {"messages": [{"role": "user", "content": "So the miners come out next week."}],
            "elevenlabs_extra_body": {"call_id": "mt-1", "kind": "meeting", "caller_aad_id": "aad-organiser"}}
    await collect(stream_turn(agent, body))
    kept = [m for m in store.messages if m.conversation_id == "call:mt-1"]
    assert kept[-1].text == "[Erin Hall] So the miners come out next week." and kept[-1].user_id == "aad-e"
    record(report("mt-1", int(time.time() * 1000), ("aad-a", "Alex Owner", 800)))
    body["messages"] = [{"role": "user", "content": "Ada, who has the drawings?"}]
    pieces, _ = parse(await collect(stream_turn(agent, body)))
    assert pieces == ["Sam has it."]
    assert "[Alex Owner] Ada, who has the drawings?" in str(model.calls[-1]["messages"])
