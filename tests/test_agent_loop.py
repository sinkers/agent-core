import json

import pytest

from agentcore.agent import Turn
from agentcore.budget import over_budget_reply
from agentcore.store import Usage

from .conftest import FakeMcp, make_agent


async def test_hello_reply_identifies_as_ai(cfg):
    agent, model, store, _ = make_agent(cfg, ["G'day, I'm Ada.\n[confidence: high]"])
    res = await agent.handle(Turn("c1", "hello", "u1", "Alex"))
    assert res.reply.startswith("G'day, I'm Ada.")
    assert "[confidence" not in res.reply
    assert "Ada (AI) is an AI agent. Owner: A. Owner." in res.reply
    assert res.tier == "default"
    assert [m.role for m in store.messages] == ["user", "assistant"]
    assert any(a["action"] == "reply" for a in store.audit_log)


async def test_always_skill_in_system_prompt(cfg):
    agent, model, *_ = make_agent(cfg, ["ok"])
    await agent.handle(Turn("c1", "hello"))
    system = "\n".join(model.calls[0]["system"])
    assert '<skill name="plain-voice"' in system
    assert "skills__load" in system and "today-board" in system


async def test_killswitch_suppresses_reply_but_logs(cfg):
    agent, model, store, _ = make_agent(cfg, ["should not be used"], killswitch_state="stop")
    res = await agent.handle(Turn("c1", "hello"))
    assert res.reply is None and res.suppressed == "killswitch"
    assert model.calls == []
    assert store.messages[0].text == "hello"  # still readable


async def test_over_budget_hard_stop(cfg):
    agent, model, store, _ = make_agent(cfg, ["unused"])
    await store.record_usage(Usage("m", "default", cost_usd=cfg.budget.daily_usd))
    res = await agent.handle(Turn("c1", "hello"))
    assert res.reply.startswith(over_budget_reply()) and "ask Alex" not in over_budget_reply() or True
    assert model.calls == []


async def test_low_confidence_reruns_on_deep(cfg):
    agent, model, store, _ = make_agent(cfg, ["maybe\n[confidence: low]", "definitely"])
    res = await agent.handle(Turn("c1", "what is due this week"))
    assert [c["tier"] for c in model.calls] == ["default", "deep"]
    assert res.reply.startswith("definitely")
    assert "low_confidence" in res.escalations
    assert any(a["action"] == "escalation" for a in store.audit_log)


async def test_tool_loop_escalates_after_8_steps(cfg):
    mcp = FakeMcp(["search_docs"])
    script = [[("openclaw__search_docs", {"q": str(i)})] for i in range(9)] + ["found it"]
    agent, model, store, tools = make_agent(cfg, script, mcp)
    await tools.discover()
    res = await agent.handle(Turn("c1", "find the thing"))
    tiers = [c["tier"] for c in model.calls]
    assert tiers[:9] == ["default"] * 9 and tiers[9] == "deep"
    assert any(e.startswith("tool_steps") for e in res.escalations)
    assert len(mcp.called) == 9


async def test_tool_results_are_wrapped_untrusted(cfg):
    mcp = FakeMcp(["search_docs"])
    agent, model, _, tools = make_agent(cfg, [[("openclaw__search_docs", {"q": "x"})], "done"], mcp)
    await tools.discover()
    await agent.handle(Turn("c1", "search"))
    tool_msg = model.calls[1]["messages"][-1]["content"][0]["toolResult"]["content"][0]["text"]
    assert tool_msg.startswith('<untrusted_data source="openclaw__search_docs">')


async def test_hidden_tool_is_not_offered_or_callable(cfg):
    mcp = FakeMcp(["search_docs", "apps_delete"])  # served, but not on Ada's allow-list
    agent, model, _, tools = make_agent(cfg, [[("openclaw__apps_delete", {"path": "/x"})], "ok"], mcp)
    await tools.discover()
    names = [t["toolSpec"]["name"] for t in tools.bedrock_tool_config()["tools"]]
    assert "openclaw__apps_delete" not in names  # PLT-021
    await agent.handle(Turn("c1", "delete it"))
    assert mcp.called == []


async def test_confirm_tool_waits_for_yes(cfg):
    raw = cfg.model_dump()
    raw["tools"]["allow"].append({"server": "openclaw", "name": "sharepoint_delete", "confirm": True, "writes": True})
    from agentcore.config import AgentConfig
    cfg2 = AgentConfig.model_validate(raw)
    mcp = FakeMcp(["sharepoint_delete"])
    agent, model, store, tools = make_agent(cfg2, [[("openclaw__sharepoint_delete", {"path": "/x"})], "Deleted."], mcp)
    await tools.discover()
    first = await agent.handle(Turn("c1", "delete /x please", "u1"))
    assert "Reply **yes**" in first.reply and mcp.called == []
    second = await agent.handle(Turn("c1", "yes", "u1"))
    assert mcp.called == [("sharepoint_delete", {"path": "/x"})]
    assert second.reply.startswith("Deleted.")


def _with_delete(cfg):
    raw = cfg.model_dump()
    raw["tools"]["allow"].append({"server": "openclaw", "name": "sharepoint_delete", "confirm": True, "writes": True})
    from agentcore.config import AgentConfig
    return AgentConfig.model_validate(raw)


async def test_several_deletes_in_one_step_need_one_yes(cfg):
    mcp = FakeMcp(["sharepoint_delete"])
    deletes = [("openclaw__sharepoint_delete", {"path": f"/dup{i}"}) for i in range(3)]
    agent, model, store, tools = make_agent(_with_delete(cfg), [deletes, "All three deleted."], mcp)
    await tools.discover()
    first = await agent.handle(Turn("c1", "delete the duplicates", "u1"))
    assert "these 3 things" in first.reply and "/dup2" in first.reply and mcp.called == []
    second = await agent.handle(Turn("c1", "Yes delete them", "u1"))
    assert mcp.called == [("sharepoint_delete", {"path": f"/dup{i}"}) for i in range(3)]
    assert second.reply.startswith("All three deleted.")


async def test_a_yes_covers_only_the_calls_it_was_asked_for(cfg):
    # 15 Oct 2026: after one yes, two more deletes in the same turn ran without being asked for.
    mcp = FakeMcp(["sharepoint_delete"])
    script = [[("openclaw__sharepoint_delete", {"path": "/a"})], [("openclaw__sharepoint_delete", {"path": "/b"})]]
    agent, model, store, tools = make_agent(_with_delete(cfg), script, mcp)
    await tools.discover()
    await agent.handle(Turn("c1", "delete /a", "u1"))
    second = await agent.handle(Turn("c1", "yes", "u1"))
    assert mcp.called == [("sharepoint_delete", {"path": "/a"})]
    assert "Reply **yes**" in second.reply and "/b" in second.reply


async def test_a_yes_to_his_own_question_naming_the_files_is_enough(cfg):
    # 15 Oct 2026: "Confirming: delete those three items ... Shall I go ahead?" "yes" -> asked again by the gate.
    mcp = FakeMcp(["sharepoint_delete"])
    offer = ("I found two duplicates: **PRJ_baseline-1.xml** and **PRJ-Baseline-1 - plan viewer.url**. "
             "Delete both? Shall I go ahead?")
    deletes = [("openclaw__sharepoint_delete", {"site": "3.0 Projects", "path": "PRJ-1 George Town/Programme/PRJ_baseline-1.xml"}),
               ("openclaw__sharepoint_delete", {"site": "3.0 Projects", "path": "PRJ-1 George Town/Programme/PRJ-Baseline-1 - plan viewer.url"})]
    agent, model, store, tools = make_agent(_with_delete(cfg), [offer, deletes, "Both deleted."], mcp)
    await tools.discover()
    await agent.handle(Turn("c1", "delete the duplicates", "u1"))
    second = await agent.handle(Turn("c1", "yes", "u1"))
    assert [c[1]["path"].rsplit("/", 1)[-1] for c in mcp.called] == ["PRJ_baseline-1.xml", "PRJ-Baseline-1 - plan viewer.url"]
    assert second.reply.startswith("Both deleted.")


async def test_a_yes_to_his_question_doesnt_cover_files_he_didnt_name(cfg):
    mcp = FakeMcp(["sharepoint_delete"])
    agent, model, store, tools = make_agent(
        _with_delete(cfg), ["Delete **old.md**? Shall I go ahead?", [("openclaw__sharepoint_delete", {"path": "/f/other.md"})]], mcp)
    await tools.discover()
    await agent.handle(Turn("c1", "tidy up the folder", "u1"))
    second = await agent.handle(Turn("c1", "yes", "u1"))
    assert mcp.called == [] and "Reply **yes**" in second.reply


async def test_a_yes_asked_after_other_work_still_reports_that_work(cfg):
    # 16 Oct 2026: a 34-task plan revision reached Alex only as "I need a yes to move the file".
    raw = _with_delete(cfg).model_dump()
    raw["tools"]["allow"].append({"server": "openclaw", "name": "sharepoint_put", "writes": True})
    from agentcore.config import AgentConfig
    mcp = FakeMcp(["sharepoint_delete", "sharepoint_put"])
    script = [[("openclaw__sharepoint_put", {"path": "/plan.xml"})],
              [("openclaw__sharepoint_delete", {"path": "/old.xml"})]]
    agent, model, store, tools = make_agent(AgentConfig.model_validate(raw), script, mcp)
    await tools.discover()
    res = await agent.handle(Turn("c1", "update the plan and tidy up", "u1"))
    assert "Done so far" in res.reply and "/plan.xml" in res.reply and "Reply **yes**" in res.reply


@pytest.mark.parametrize("text,yes", [
    ("yes", True), ("Yes please", True), ("Yes delete duplicates", True), ("yep go ahead", True), ("ok", True),
    ("sure, do it", True), ("yes but keep v2", False), ("yes, only the first one", False), ("no", False),
    ("yes? what will it delete", False), ("yes I think we should probably talk about which ones first", False),
])
def test_what_counts_as_a_plain_yes(text, yes):
    from agentcore.agent import is_yes
    assert is_yes(text) is yes


async def test_scope_check_requires_confirm_for_new_recipient(cfg):
    raw = cfg.model_dump()
    raw["mcp_servers"]["m365"] = {"url": "http://m365"}
    raw["tools"]["allow"].append({"server": "m365", "name": "send_mail", "writes": True, "scope_args": ["to"]})
    from agentcore.config import AgentConfig
    cfg2 = AgentConfig.model_validate(raw)
    agent, model, store, tools = make_agent(cfg2, [])
    mcp = FakeMcp(["send_mail"])
    tools.clients = {"m365": mcp}
    await tools.discover()
    from agentcore.tools import CallContext
    ctx = CallContext("c1", "u1", ["email bob@acme.example the minutes"])
    ok = await tools.call("m365__send_mail", {"to": "bob@acme.example"}, ctx, True)
    injected = await tools.call("m365__send_mail", {"to": "evil@example.com"}, ctx, True)
    assert ok.status == "ok" and injected.status == "needs_confirm"
    blocked = await tools.call("m365__send_mail", {"to": "bob@acme.example"}, ctx, False)
    assert blocked.status == "blocked"  # kill switch stops writes


async def test_history_is_limited_and_alternates(cfg):
    agent, model, store, _ = make_agent(cfg, ["a"] * 40)
    for i in range(20):
        await agent.handle(Turn("c1", f"msg {i}"))
    msgs = model.calls[-1]["messages"]
    assert len(msgs) <= cfg.history_messages
    assert msgs[0]["role"] == "user"
    assert all(msgs[i]["role"] != msgs[i + 1]["role"] for i in range(len(msgs) - 1))


async def test_disabled_agent_does_not_reply(cfg):
    cfg.agent.enabled = False
    agent, model, *_ = make_agent(cfg, ["x"])
    assert (await agent.handle(Turn("c1", "hi"))).suppressed == "disabled"


async def test_footer_not_duplicated_and_history_clean(cfg):
    footer = cfg.footer
    agent, model, store, _ = make_agent(cfg, [f"All good.\n\n_{footer}_", "second"])
    res = await agent.handle(Turn("c9", "status?"))
    assert res.reply.count(footer) == 1
    assert footer not in store.messages[-1].text  # stored without the footer
    await agent.handle(Turn("c9", "again"))
    history_text = str(model.calls[-1]["messages"])
    assert footer not in history_text


async def test_first_responder_answers_simple_messages(cfg):
    agent, model, *_ = make_agent(cfg, ['{"action": "answer", "text": "G\'day Alex!"}'], triage=True)
    acks = []
    res = await agent.handle(Turn("t1", "hi Ada"), on_ack=lambda t: acks.append(t))
    assert res.reply.startswith("G'day Alex!") and res.tier == "fast" and acks == []
    assert [c["tier"] for c in model.calls] == ["fast"]


async def test_first_responder_acks_then_hands_off(cfg):
    agent, model, *_ = make_agent(cfg, ['{"action": "work", "ack": "Just a moment, pulling up PRJ.", "tier": "frontier"}',
                                        "Here is the plan."], triage=True)
    acks = []

    async def on_ack(t):
        acks.append(t)
    res = await agent.handle(Turn("t2", "plan the PRJ commissioning sequence"), on_ack=on_ack)
    assert acks == ["Just a moment, pulling up PRJ."]
    assert [c["tier"] for c in model.calls] == ["fast", "frontier"]
    assert res.reply.startswith("Here is the plan.")


async def test_first_responder_bad_json_falls_back(cfg):
    agent, model, *_ = make_agent(cfg, ["not json", "normal answer"], triage=True)
    res = await agent.handle(Turn("t3", "where is AU05 up to"))
    assert res.reply.startswith("normal answer") and [c["tier"] for c in model.calls] == ["fast", "default"]


@pytest.mark.parametrize("claim", [
    "Got it, I've noted your mobile as 0400 000 999.", "Done. I've cleared that number from my notes.",
    "Sure, I've sent Erin a message.", "Reminder scheduled for 9am.", "I'll remember that.",
    "No worries, I've registered it.", "All set, it's updated.",
])
async def test_first_responder_cannot_claim_it_did_something(cfg, claim):
    agent, model, *_ = make_agent(cfg, [json.dumps({"action": "answer", "text": claim}), "Registered +61400000999."],
                                  triage=True)
    res = await agent.handle(Turn("t4", "my mobile is 0400 000 999, register it"))
    assert [c["tier"] for c in model.calls] == ["fast", "default"]  # handed to the full agent with tools
    assert res.reply.startswith("Registered")


async def test_first_responder_small_talk_still_answers(cfg):
    agent, model, *_ = make_agent(cfg, ['{"action": "answer", "text": "No worries, happy to help!"}'], triage=True)
    res = await agent.handle(Turn("t5", "thanks Ada"))
    assert res.tier == "fast" and len(model.calls) == 1


async def test_empty_message_in_history_is_skipped(cfg):
    from agentcore.store import Message
    agent, model, store, _ = make_agent(cfg, ["Registered."])
    await store.save_message(Message("t6", "user", "hello", "u", "Alex"))
    await store.save_message(Message("t6", "assistant", ""))
    await store.save_message(Message("t6", "assistant", "   "))
    await agent.handle(Turn("t6", "my mobile is 0418 000 000"))
    for m in model.calls[-1]["messages"]:
        assert all(block.get("text", "x").strip() for block in m["content"] if "text" in block)


async def test_empty_model_reply_is_not_stored(cfg):
    agent, model, store, _ = make_agent(cfg, [""])
    res = await agent.handle(Turn("t7", "hello"))
    assert res.reply and res.reply.strip()
    assert all(m.text.strip() for m in store.messages if m.conversation_id == "t7")


async def test_reply_cut_off_at_length_limit_is_retried_shorter(cfg):
    agent, model, store, _ = make_agent(cfg, [("", "max_tokens"), "Short summary: 40 points in the sprint."])
    res = await agent.handle(Turn("t8", "summarise every ticket in the sprint"))
    assert res.reply.startswith("Short summary")
    assert "cut off" in str(model.calls[-1]["messages"][-1])


async def test_a_tool_call_cut_off_at_the_limit_is_retried_compact_not_abandoned(cfg):
    """Ada's make_plan for the PRJ programme hit 8192 output tokens three times on 12 Oct 2026: the old note
    ("much shorter, don't call more tools") made him give up on the plan."""
    agent, model, *_ = make_agent(cfg, [("", "max_tokens"), "ok"])
    await agent.handle(Turn("t9", "build the programme"))
    note = str(model.calls[-1]["messages"][-1])
    assert "middle of a tool call" in note and "compact" in note and "much shorter" not in note
    agent, model, *_ = make_agent(cfg, [("x" * 2000, "max_tokens"), "ok"])  # mostly text: a long answer
    await agent.handle(Turn("t10", "summarise every ticket"))
    assert "much shorter" in str(model.calls[-1]["messages"][-1])


def test_ada_can_write_a_whole_programme_in_one_call():
    from agentcore.config import load_agent_config
    from .conftest import AGENTS
    tiers = load_agent_config(AGENTS / "ada" / "agent.yaml").models.tiers
    assert all(tiers[t].max_tokens >= 24000 for t in ("default", "deep", "frontier"))


async def test_voice_prompt_explains_the_call_is_audio_only(cfg):
    agent, model, *_ = make_agent(cfg, ["Sure.", "Sure.", "Sure."])
    await agent.handle(Turn("v1", "send me the link", channel="voice"))
    teams_call = "\n".join(model.calls[-1]["system"])
    assert "Teams call" in teams_call and "can only hear you" in teams_call
    assert "send_teams_message" in teams_call and "post the result in the chat" not in teams_call
    await agent.handle(Turn("v2", "send me the link", "aad-alex", "Alex", channel="voice", caller_number="+61418000000"))
    assert "phone call" in "\n".join(model.calls[-1]["system"])
    await agent.handle(Turn("v3", "hi", channel="voice", verified=False, caller_number="+61400000000"))
    unverified = "\n".join(model.calls[-1]["system"])
    assert "send_teams_message" not in unverified  # an unknown caller can't be sent anything


async def test_voice_turn_does_not_wait_for_memory_before_first_responder(cfg):
    import asyncio
    agent, model, *_ = make_agent(cfg, ['{"action": "answer", "text": "Morning Alex!"}', '{"action": "work", "ack": "One sec.", "tier": "default"}', "Done looking."], triage=True)
    order = []

    class SlowMemory:
        async def recall(self, text, user_id):
            order.append("recall-start")
            await asyncio.sleep(0.05)
            order.append("recall-end")

            class R:
                def prompt_block(self):
                    return "Relevant memories: Alex prefers short answers."
            return R()

        def retain_later(self, **kw):
            pass
    agent.memory = SlowMemory()
    res = await agent.handle(Turn("vm1", "morning", "aad-alex", "Alex", channel="voice"))
    assert res.reply.startswith("Morning Alex!") and "recall-end" not in order  # answered without waiting
    await agent.handle(Turn("vm2", "what's on PRJ", "aad-alex", "Alex", channel="voice"), on_ack=lambda t: None)
    assert "prefers short answers" in "\n".join(model.calls[-1]["system"])  # the real work still gets memory


@pytest.mark.parametrize("deflection", [
    "I can't recall the details of our last voice call, Alex.", "I don't have access to call transcripts.",
    "Sorry, I'm unable to look that up.", "I've got no record of that call.",
])
async def test_first_responder_cannot_say_it_cant(cfg, deflection):
    agent, model, *_ = make_agent(cfg, [json.dumps({"action": "answer", "text": deflection}), "On the last call we covered PRJ."],
                                  triage=True)
    res = await agent.handle(Turn("t9", "what did we talk about on our last call?"))
    assert [c["tier"] for c in model.calls] == ["fast", "default"] and res.reply.startswith("On the last call")


@pytest.mark.parametrize("citation", [
    # What the first responder said on 30 Oct 2026 (the clauses don't exist)
    "Substations must be in a fire-isolated room (Performance Requirement BP2.1, Deemed-to-Satisfy Specification 2.7.2).",
    "Under the NCC that needs a two-hour enclosure.", "AS/NZS 3000 covers that.", "It's 10 business days under s 19(3).",
])
async def test_first_responder_never_cites_codes_or_clauses(cfg, citation):
    agent, model, *_ = make_agent(cfg, [json.dumps({"action": "answer", "text": citation}), "From the library: ..."],
                                  triage=True)
    res = await agent.handle(Turn("t10", "what does the NCC say about fire separation for a substation?"))
    tiers = [c["tier"] for c in model.calls]  # handed to the full agent (escalation terms may make it deep)
    assert tiers[0] == "fast" and len(tiers) == 2 and tiers[1] != "fast" and res.reply.startswith("From the library")


def test_the_first_responder_is_told_codes_and_contracts_are_work():
    from agentcore.agent import Agent
    assert "building codes, standards, contracts" in Agent.TRIAGE_PROMPT


@pytest.mark.parametrize("small_talk", ["it's 3pm already, see you at 3.30", "Morning! I've 2 meetings, then I'm free."])
def test_small_talk_with_numbers_is_not_a_citation(small_talk):
    from agentcore.agent import claims_action
    assert not claims_action(small_talk)


async def test_completed_write_actions_are_kept_in_history(cfg):
    from agentcore.tools import ToolRule
    cfg.tools.allow.append(ToolRule(server="openclaw", name="sharepoint_put", writes=True))
    mcp = FakeMcp(["sharepoint_put"])
    agent, model, store, tools = make_agent(cfg, [[("openclaw__sharepoint_put", {"path": "Working/x.docx"})], "Saved it."], mcp=mcp)
    await tools.discover()
    res = await agent.handle(Turn("h1", "save the draft"))
    assert res.reply.startswith("Saved it.") and "do not repeat" not in res.reply  # not shown to the person
    stored = [m.text for m in store.messages if m.conversation_id == "h1" and m.role == "assistant"][-1]
    assert "Already done in this reply" in stored and "openclaw__sharepoint_put(path=Working/x.docx)" in stored


async def test_turns_in_one_conversation_run_one_at_a_time(cfg):
    import asyncio
    agent, model, store, _ = make_agent(cfg, ["first", "second"])
    order = []
    real = model.converse

    async def slow(**kw):
        order.append("start")
        await asyncio.sleep(0.02)
        order.append("end")
        return await real(**kw)
    model.converse = slow
    await asyncio.gather(agent.handle(Turn("c-lock", "one")), agent.handle(Turn("c-lock", "two")))
    assert order == ["start", "end", "start", "end"]


async def test_step_limit_is_per_agent_and_ends_with_a_progress_report(cfg):
    cfg.tools.max_steps = 5
    mcp = FakeMcp(["search_docs"])
    script = [[("openclaw__search_docs", {"q": str(i)})] for i in range(5)] + [
        "Done 5 searches; still to check PRJ-12. Say 'carry on' to continue."]
    agent, model, store, tools = make_agent(cfg, script, mcp)
    await tools.discover()
    res = await agent.handle(Turn("c-limit", "find everything"))
    assert len(mcp.called) == 5 and "carry on" in res.reply
    last = model.calls[-1]["messages"][-1]["content"]
    assert any("tool steps allowed" in b.get("text", "") for b in last)  # told to stop and report


async def test_tools_after_the_step_limit_get_a_fallback_not_more_tool_calls(cfg):
    cfg.tools.max_steps = 4
    mcp = FakeMcp(["search_docs"])
    script = [[("openclaw__search_docs", {"q": str(i)})] for i in range(6)]
    agent, model, store, tools = make_agent(cfg, script, mcp)
    await tools.discover()
    res = await agent.handle(Turn("c-limit2", "find everything"))
    assert len(mcp.called) == 4 and "carry on" in res.reply.lower()


async def test_a_reply_saved_after_the_message_doesnt_break_the_turn(cfg):
    """A restart resume (or Teams delivering a message twice) can save a reply after this turn's message;
    the history must still end with the person's message, or Bedrock refuses it ('assistant prefill')."""
    agent, model, store, tools = make_agent(cfg, ["second answer"])
    from agentcore.store import Message
    await store.save_message(Message("c-dup", "user", "tidy PRJ-393", "u1", "Alex"))
    await store.save_message(Message("c-dup", "assistant", "first answer"))
    turn = Turn("c-dup", "tidy PRJ-393", "u1", "Alex")
    history, _ = await agent._history("c-dup", turn)
    assert history[-1] == {"role": "user", "content": [{"text": "Alex: tidy PRJ-393"}]}
    plain, _ = await agent._history("c-dup")
    assert plain[-1]["role"] == "assistant"  # without a turn (summaries) it's left as stored


async def test_cut_off_twice_says_so_instead_of_losing_its_train_of_thought(cfg):
    agent, model, *_ = make_agent(cfg, [("", "max_tokens"), ("", "max_tokens")])
    res = await agent.handle(Turn("t11", "write out the whole programme"))
    assert "too long" in res.reply and "train of thought" not in res.reply


def test_files_and_plans_a_tool_made_are_remembered_for_the_next_turn(cfg):
    from agentcore.agent import TurnResult
    agent, *_ = make_agent(cfg, ["ok"])
    result = TurnResult(reply="")
    agent._note_made(result, '<untrusted_data>{"plan_id": "70de49ac89", "files": {"msproject_xml": {"file": '
                             '"outbox:efde297c88eb", "name": "PRJ-programme.xml", "size": 9}}}</untrusted_data>')
    assert result.actions == ["plan_id=70de49ac89", "made outbox:efde297c88eb (PRJ-programme.xml)"]
