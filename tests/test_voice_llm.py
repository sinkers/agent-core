import json

from agentcore.voice_llm import initiation_data, last_user_text, stream_turn

from .conftest import make_agent


def parse(lines):
    out, done = [], False
    for line in lines:
        assert line.startswith("data: ")
        payload = line[6:].strip()
        if payload == "[DONE]":
            done = True
            continue
        delta = json.loads(payload)["choices"][0]["delta"]
        if delta.get("content"):
            out.append(delta["content"])
    return out, done


async def collect(gen):
    return [x async for x in gen]


async def test_streams_ack_then_answer_with_buffer_words(cfg):
    agent, model, store, _ = make_agent(cfg, ['{"action": "work", "ack": "Let me check the PRJ board.", "tier": "default"}',
                                              "Three tickets are in progress."], triage=True)
    body = {"model": "ada", "stream": True,
            "messages": [{"role": "system", "content": "x"}, {"role": "user", "content": "What's on PRJ?"}],
            "elevenlabs_extra_body": {"call_id": "c1", "caller_aad_id": "aad-alex", "caller_name": "Alex Owner", "kind": "p2p"}}
    pieces, done = parse(await collect(stream_turn(agent, body)))
    assert pieces == ["Let me check the PRJ board... ", "Three tickets are in progress."] and done
    assert store.messages[0].conversation_id == "call:c1" and store.messages[0].user_id == "aad-alex"
    assert "live Teams call" in "\n".join(model.calls[-1]["system"])  # voice style applied


async def test_simple_turn_answers_directly(cfg):
    agent, model, *_ = make_agent(cfg, ['{"action": "answer", "text": "Morning Alex!"}'], triage=True)
    body = {"messages": [{"role": "user", "content": [{"type": "text", "text": "morning Ada"}]}],
            "elevenlabs_extra_body": {"call_id": "c2"}}
    pieces, done = parse(await collect(stream_turn(agent, body)))
    assert pieces == ["Morning Alex!"] and done and len(model.calls) == 1


def test_initiation_data_carries_caller_for_every_turn():
    d = initiation_data(call_id="c9", caller_aad_id="aad-x", caller_name="Erin Hall", kind="p2p", greeting="Morning Erin")
    assert d["type"] == "conversation_initiation_client_data" and d["dynamic_variables"]["greeting"] == "Morning Erin"
    assert d["custom_llm_extra_body"] == {"call_id": "c9", "caller_aad_id": "aad-x", "caller_name": "Erin Hall", "kind": "p2p"}


def test_last_user_text_handles_both_shapes():
    assert last_user_text([{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]) == "a"
    assert last_user_text([{"role": "user", "content": [{"type": "text", "text": "x"}, {"type": "text", "text": "y"}]}]) == "x y"


async def test_unregistered_phone_caller_gets_restricted_turn(cfg):
    agent, model, store, _ = make_agent(cfg, ["Happy to take a message. Who's calling?", "Sure."])
    offered = []
    agent.tools.bedrock_tool_config = lambda only=None: offered.append(only)
    body = {"messages": [{"role": "user", "content": "What's the status of the PRJ substation?"}],
            "elevenlabs_extra_body": {"call_id": "p1", "caller_aad_id": "", "kind": "phone", "caller_number": "+61400111222"}}
    pieces, _ = parse(await collect(stream_turn(agent, body)))
    assert pieces == ["Happy to take a message. Who's calling?"]
    assert "UNVERIFIED NUMBER" in "\n".join(model.calls[-1]["system"])
    assert offered[-1] == {"phone__take_message"}
    await collect(stream_turn(agent, {**body, "elevenlabs_extra_body": {
        "call_id": "p1b", "caller_aad_id": "aad-alex", "kind": "phone"}}))
    assert offered[-1] is None  # a registered caller gets the usual tools


async def test_registered_phone_caller_is_a_normal_turn(cfg):
    agent, model, *_ = make_agent(cfg, ["Three tickets are in progress."])
    body = {"messages": [{"role": "user", "content": "What's on PRJ?"}],
            "elevenlabs_extra_body": {"call_id": "p2", "caller_aad_id": "aad-alex", "caller_name": "Alex Owner",
                                      "kind": "phone", "caller_number": "+61412345678"}}
    await collect(stream_turn(agent, body))
    assert "UNVERIFIED" not in "\n".join(model.calls[-1]["system"])


async def test_no_initiation_data_fails_closed(cfg):
    agent, model, *_ = make_agent(cfg, ["Hello."])
    await collect(stream_turn(agent, {"messages": [{"role": "user", "content": "hi"}]}))
    assert "UNVERIFIED NUMBER" in "\n".join(model.calls[-1]["system"])


async def test_silence_prompts_get_no_reply(cfg):
    # ElevenLabs sends "..." when the line goes quiet; answering each one made Ada talk every 10 s.
    agent, model, *_ = make_agent(cfg, ["Still here!"])
    for silence in ["...", "…", " . . ", ""]:
        body = {"messages": [{"role": "user", "content": silence}], "elevenlabs_extra_body": {"call_id": "s1", "kind": "p2p"}}
        pieces, done = parse(await collect(stream_turn(agent, body)))
        assert done
    assert model.calls == []


async def test_in_meetings_ada_only_speaks_when_addressed(cfg):
    agent, model, store, _ = make_agent(cfg, ["Sam has the drawings."])
    chatter = {"messages": [{"role": "user", "content": "So the miners come out next week, Sam."}],
               "elevenlabs_extra_body": {"call_id": "m1", "kind": "meeting", "caller_aad_id": "aad-alex"}}
    pieces, _ = parse(await collect(stream_turn(agent, chatter)))
    assert pieces in ([], [""]) and model.calls == []
    assert any("miners come out" in m.text for m in store.messages if m.conversation_id == "call:m1")  # kept for the summary
    asked = {**chatter, "messages": [{"role": "user", "content": "Ada, who has the drawings?"}]}
    pieces, _ = parse(await collect(stream_turn(agent, asked)))
    assert pieces == ["Sam has the drawings."]


async def test_the_same_transcript_line_is_answered_once(cfg):
    agent, model, *_ = make_agent(cfg, ["Answer one.", "Answer two."])
    body = {"messages": [{"role": "user", "content": "What's due this week?"}], "elevenlabs_extra_body": {"call_id": "d1", "kind": "p2p"}}
    await collect(stream_turn(agent, body))
    pieces, _ = parse(await collect(stream_turn(agent, body)))
    assert pieces in ([], [""]) and len(model.calls) == 1


async def test_outbound_teams_call_uses_our_ref_and_opener():
    from agentcore.store import MemoryStore
    from agentcore.voice_llm import outbound_teams_call
    store = MemoryStore()
    await store.set_pref("call-out:tout-1", "pending", {"opener": "Hi Erin, it's Ada.", "name": "Erin Hall"})
    assert await outbound_teams_call(store, "tout-1", "graph-1", "") == ("tout-1", "Erin Hall", "Hi Erin, it's Ada.")
    call_id, _, greeting = await outbound_teams_call(store, "unknown", "graph-2", "X")
    assert call_id == "graph-2" and greeting


def is_skip_turn(lines: list[str]) -> bool:
    """A quiet turn must be a skip_turn tool call: an empty reply makes ElevenLabs end the call."""
    chunks = [json.loads(l[6:]) for l in lines if l.startswith("data: {")]
    names = [tc["function"]["name"] for c in chunks for tc in c["choices"][0]["delta"].get("tool_calls", [])
             if tc.get("function", {}).get("name")]
    finishes = [c["choices"][0]["finish_reason"] for c in chunks if c["choices"][0]["finish_reason"]]
    args = "".join(tc["function"].get("arguments", "") for c in chunks
                   for tc in c["choices"][0]["delta"].get("tool_calls", []))
    return names == ["skip_turn"] and finishes == ["tool_calls"] and json.loads(args) == {} \
        and lines[-1] == "data: [DONE]\n\n"


async def test_every_quiet_turn_is_a_skip_turn_never_an_empty_reply(cfg):
    """5 Oct 2026: Ada joined the standup, then ElevenLabs hung up 11 s later ("Brain returned no
    response") because an unaddressed line got an empty reply."""
    agent, model, *_ = make_agent(cfg, ["first answer"])
    silence = {"messages": [{"role": "user", "content": "..."}], "elevenlabs_extra_body": {"call_id": "q1", "kind": "p2p"}}
    assert is_skip_turn(await collect(stream_turn(agent, silence)))
    chatter = {"messages": [{"role": "user", "content": "Morning all, let's start with PRJ."}],
               "elevenlabs_extra_body": {"call_id": "q2", "kind": "meeting"}}
    assert is_skip_turn(await collect(stream_turn(agent, chatter)))
    line = {"messages": [{"role": "user", "content": "What's due today?"}], "elevenlabs_extra_body": {"call_id": "q3", "kind": "p2p"}}
    await collect(stream_turn(agent, line))
    assert is_skip_turn(await collect(stream_turn(agent, line)))  # the repeat
    assert len(model.calls) == 1


def test_elevenlabs_agent_has_skip_turn_enabled():
    from agentcore.voice_llm import agent_config
    cfg = agent_config(name="Ada", custom_llm_url="https://x/v1", secret_id="s", voice_id="v", tts_model="eleven_flash_v2_5")
    tool = cfg["conversation_config"]["agent"]["prompt"]["built_in_tools"]["skip_turn"]
    assert tool["params"]["system_tool_type"] == "skip_turn"


async def test_out_of_credits_is_reported_to_the_owner_once_not_silently_dropped():
    """6 Oct 2026: ElevenLabs credits ran out; Ada left the standup three times and nobody was told."""
    from agentcore.store import MemoryStore
    from agentcore.voice_llm import ALERT_EVERY, report_call_end
    store, told = MemoryStore(), []

    async def notify(text):
        told.append(text)
    body = {"call_id": "c1", "kind": "meeting", "seconds": 81,
            "reason": "closed by agent: 3000 [quota_exceeded] You've run out of credits. Add credits or upgrade your plan"}
    assert (await report_call_end(store, body, notify, now=1000))["alerted"] is True
    assert "run out of credits" in told[0] and "a meeting" in told[0] and "elevenlabs.io" in told[0]
    assert (await report_call_end(store, {**body, "call_id": "c2"}, notify, now=1060))["alerted"] is False  # once
    assert (await report_call_end(store, {**body, "call_id": "c3"}, notify, now=1000 + ALERT_EVERY + 1))["alerted"] is True
    normal = await report_call_end(store, {"call_id": "c4", "kind": "p2p", "reason": "closed by agent: 1000 end_call"}, notify, now=99999)
    assert normal == {"problem": None, "alerted": False} and len(told) == 2
    assert sum(1 for a in store.audit_log if a["action"] == "voice_call_end") == 4


def meeting_turn(text, call_id="sm1"):
    return {"messages": [{"role": "user", "content": text}],
            "elevenlabs_extra_body": {"call_id": call_id, "kind": "meeting", "caller_aad_id": "aad-alex"}}


async def test_an_old_mention_in_a_growing_transcript_does_not_keep_him_talking(cfg):
    """15 Oct 2026 standup: after Ada spoke, ElevenLabs folded everything said next into one growing user turn,
    so the "Ada" from a minute earlier passed the name check every time and he answered the room's banter
    ("his meeting skills aren't very good", "call him Denise") about ten times."""
    from agentcore.voice_llm import new_part
    agent, model, store, _ = make_agent(cfg, ["One item on the PRJ board."])
    asked = "Can you slow down the voice of Ada? Ada, show us the TODAY board for PRJ."
    pieces, _ = parse(await collect(stream_turn(agent, meeting_turn(asked))))
    assert pieces == ["One item on the PRJ board."]
    banter = asked + " Anyway, his meeting skills aren't very good. Mark is on the firmware tickets now."
    assert is_skip_turn(await collect(stream_turn(agent, meeting_turn(banter))))
    assert len(model.calls) == 1
    kept = [m.text for m in store.messages if m.conversation_id == "call:sm1" and m.user_name == "Meeting"]
    assert any("meeting skills" in t and "TODAY board" not in t for t in kept)  # only the new words are recorded
    assert new_part("Ada, show us the board.", "Ada, show us the board. Mark has it.") == "Mark has it."
    assert new_part("Show us the board-", "show us the board. Mark has it") == "Mark has it"  # Scribe revises punctuation
    assert new_part("Morning all.", "What's due today?") == "What's due today?"  # a fresh turn
    assert new_part("Same words.", "Same words.") == ""
    from agentcore.voice_llm import new_since
    heard = ["Can you run us through what's on the Today board?", "Maybe I need to ask him."]
    assert new_since(heard, "Maybe I need to ask him. Ada, can you run us through what's on the today board?") \
        == "Ada, can you run us through what's on the today board?"  # a repeat of earlier words, now addressed
    assert new_since(["Long version of the turn so far, Ada, show us.", "Short version"],
                     "Long version of the turn so far, Ada, show us. Then Mark.") == "Then Mark."


async def test_he_takes_the_answer_to_his_own_question_without_his_name(cfg):
    """Same standup: he asked "which project?", someone said "PRJ", and he ignored it."""
    agent, model, *_ = make_agent(cfg, ["Which project do you want, or the whole board?", "PRJ has one item."])
    await collect(stream_turn(agent, meeting_turn("Ada, run us through the TODAY board.", "sm2")))
    pieces, _ = parse(await collect(stream_turn(agent, meeting_turn("PRJ.", "sm2"))))
    assert pieces == ["PRJ has one item."] and len(model.calls) == 2
    # a generic "anything else?" doesn't open the floor
    agent2, model2, *_ = make_agent(cfg, ["Done. Anything else?"])
    await collect(stream_turn(agent2, meeting_turn("Ada, move it to Friday.", "sm3")))
    assert is_skip_turn(await collect(stream_turn(agent2, meeting_turn("Righto, next item, Sam.", "sm3"))))


def test_meetings_get_meeting_manners_in_the_prompt():
    from agentcore.voice import voice_context
    text = voice_context("teams", True, meeting=True)
    assert "MEETING" in text and "anything else" in text and "live Teams meeting" in text
    assert "MEETING" not in voice_context("teams", True)


async def test_a_meeting_answer_cut_off_by_the_room_is_said_on_the_next_turn(cfg):
    """16 Oct 2026 standup: every answer was dropped when someone said "um, reviewed?" or "you there?" half a
    second after he started, so Ada never answered anyone."""
    import asyncio
    from types import SimpleNamespace
    agent, *_ = make_agent(cfg, [])

    async def slow_handle(turn, on_ack=None, on_progress=None):
        await on_ack("Let me check")
        await asyncio.sleep(0.05)
        return SimpleNamespace(reply="I'll follow up with David H. on the requirement doc today.")
    agent.handle = slow_handle
    stream = stream_turn(agent, meeting_turn("Ada, can you follow up with David H. on the requirement doc?", "cut1"))
    first = await stream.__anext__()
    assert "Let me check" in first
    await stream.aclose()  # ElevenLabs drops the request: someone in the room spoke
    await asyncio.sleep(0.1)
    pieces, _ = parse(await collect(stream_turn(agent, meeting_turn("you there?", "cut1"))))
    assert "".join(pieces) == "I'll follow up with David H. on the requirement doc today."
    assert any("you there" in m.text for m in agent.store.messages if m.conversation_id == "call:cut1")
    # and it isn't said twice
    assert is_skip_turn(await collect(stream_turn(agent, meeting_turn("Okay, next.", "cut1"))))


async def test_a_one_to_one_answer_is_still_dropped_when_talked_over(cfg):
    import asyncio
    from types import SimpleNamespace
    agent, *_ = make_agent(cfg, [])
    finished = []

    async def slow_handle(turn, on_ack=None, on_progress=None):
        await on_ack("Let me check")
        await asyncio.sleep(0.05)
        finished.append(True)
        return SimpleNamespace(reply="done")
    agent.handle = slow_handle
    body = {"messages": [{"role": "user", "content": "What's due this week?"}],
            "elevenlabs_extra_body": {"call_id": "p1", "kind": "p2p"}}
    stream = stream_turn(agent, body)
    await stream.__anext__()
    await stream.aclose()
    await asyncio.sleep(0.1)
    assert finished == []
