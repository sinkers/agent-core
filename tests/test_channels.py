"""One person across channels, channel-aware replies, and background work started from a call."""

import json
from datetime import datetime, timedelta, timezone

from agentcore.agent import Turn, format_elsewhere, where_label
from agentcore.scheduler import ResultDelivery, ScheduleTools, TaskRunner
from agentcore.store import MemoryStore, Message
from agentcore.tools import CallContext
from agentcore.voice import voice_context

from .conftest import make_agent

NOW = datetime.now(timezone.utc)


def system_text(call: dict) -> str:
    return "\n".join(call["system"])


# ---- the store: which other conversations count as this person's own ---------------------------------------

async def test_recent_elsewhere_only_private_conversations_of_that_person():
    store = MemoryStore()
    add = store.messages.append
    add(Message("chat-1to1", "user", "Can you draft the PRJ brief?", "aad-alex", "Alex", NOW - timedelta(hours=1)))
    add(Message("chat-1to1", "assistant", "Drafting it now.", at=NOW - timedelta(minutes=59)))
    add(Message("group", "user", "my group message", "aad-alex", "Alex", NOW - timedelta(minutes=50)))
    add(Message("group", "user", "someone else", "aad-erin", "Erin", NOW - timedelta(minutes=49)))
    add(Message("call:meet", "user", "said in a meeting", "aad-alex", "Meeting", NOW - timedelta(minutes=40)))
    add(Message("marked", "user", "in a group chat only I spoke in", "aad-alex", "Alex", NOW - timedelta(minutes=30)))
    add(Message("old", "user", "two days ago", "aad-alex", "Alex", NOW - timedelta(days=2)))
    add(Message("summary:x", "user", "summarise this call", "aad-alex", None, NOW - timedelta(minutes=20)))
    add(Message("call:now", "user", "hello again", "aad-alex", "Alex", NOW))
    await store.set_pref("marked", "shared_conversation", True)

    got = await store.recent_elsewhere("aad-alex", "call:now", NOW - timedelta(hours=24), 10)
    assert [m.text for m in got] == ["Can you draft the PRJ brief?", "Drafting it now."]


def test_format_elsewhere_labels_channels_and_trims():
    msgs = [Message("mail:t1", "user", "x" * 50, "u", "Alex", NOW),
            Message("call:c1", "assistant", "On it.", at=NOW),
            Message("task:3", "assistant", "Done.", at=NOW),
            Message("19:abc@thread", "user", "hi", "u", "Alex", NOW)]
    block = format_elsewhere(msgs, "Alex", 24, 10)
    assert "[email ·" in block and "Alex: xxxxxxxxxx…" in block
    assert "[call ·" in block and "You: On it." in block
    assert "[background task ·" in block and "[Teams chat ·" in block
    assert format_elsewhere([], "Alex", 24, 10) == ""
    assert where_label("call:abc") == "call"


# ---- the agent: a call picks up where the chat left off ------------------------------------------------------

async def test_call_sees_the_chat_that_came_before(cfg):
    agent, model, store, _ = make_agent(cfg, ["Sure, I'll start the PRJ brief.", "Yes, the brief you asked for."])
    await agent.handle(Turn("chat-1", "Please start the PRJ design brief", "aad-alex", "Alex"))
    await agent.handle(Turn("call:c1", "Did you get my message earlier?", "aad-alex", "Alex",
                            channel="voice", source="teams_call"))
    on_call = system_text(model.calls[-1])
    assert "RECENTLY ON OTHER CHANNELS" in on_call
    assert "Please start the PRJ design brief" in on_call and "[Teams chat ·" in on_call
    assert "CHANNEL: you are on a live Teams call" in on_call


async def test_chat_sees_the_call_but_groups_never_do(cfg):
    agent, model, store, _ = make_agent(cfg, ["ok", "ok", "ok"])
    await agent.handle(Turn("call:c1", "The crane is booked for Tuesday", "aad-alex", "Alex", channel="voice"))
    await agent.handle(Turn("chat-1", "What did I tell you about the crane?", "aad-alex", "Alex",
                            source="teams_1to1"))
    assert "The crane is booked for Tuesday" in system_text(model.calls[-1])
    assert "CHANNEL: a 1:1 Teams chat" in system_text(model.calls[-1])

    await agent.handle(Turn("group-1", "Team, any updates?", "aad-alex", "Alex", scope="shared",
                            source="teams_group"))
    group = system_text(model.calls[-1])
    assert "RECENTLY ON OTHER CHANNELS" not in group and "crane" not in group
    assert "everyone in it reads your reply" in group
    assert await store.get_pref("group-1", "shared_conversation") is True


async def test_unverified_caller_gets_nothing_from_other_channels(cfg):
    agent, model, store, _ = make_agent(cfg, ["ok", "ok"])
    await agent.handle(Turn("chat-1", "Secret project notes", "aad-alex", "Alex"))
    await agent.handle(Turn("call:p1", "Hi who is this", "aad-alex", None, channel="voice", verified=False,
                            caller_number="+61400000000"))
    assert "Secret project notes" not in system_text(model.calls[-1])


async def test_cross_channel_can_be_switched_off(cfg):
    cfg.cross_channel.enabled = False
    agent, model, _, _ = make_agent(cfg, ["ok", "ok"])
    await agent.handle(Turn("chat-1", "remember the crane", "aad-alex", "Alex"))
    await agent.handle(Turn("call:c1", "hello", "aad-alex", "Alex", channel="voice"))
    assert "RECENTLY ON OTHER CHANNELS" not in system_text(model.calls[-1])


# ---- what a call tells the agent about channels -----------------------------------------------------------

def test_voice_context_points_written_things_elsewhere_and_sizes_work():
    text = voice_context("teams", True, email="ada@acme.example", background=True, usual="an email")
    assert "never say 'here', 'in this chat'" in text
    assert "files__email_file" in text and "files__send_file" in text and "ada@acme.example" in text
    assert "schedule__start_background_task" in text and "last time they chose an email" in text
    assert "start_background_task" not in voice_context("teams", True)
    unverified = voice_context("phone", False, email="ada@acme.example", background=True)
    assert "start_background_task" not in unverified and "files__" not in unverified


# ---- start_background_task ---------------------------------------------------------------------------------

async def test_start_background_task_runs_now_and_remembers_the_choice():
    store = MemoryStore()
    tools = ScheduleTools(store)
    woken = []
    tools.on_new = lambda: woken.append(True)
    out = json.loads(await tools.call_tool("start_background_task", {
        "instruction": "Review the 12 open PRJ tickets and summarise blockers", "deliver_via": "email"},
        CallContext("call:c1", "aad-alex", actor_name="Alex Owner")))
    assert out["started"] and out["deliver_via"] == "email" and woken == [True]
    t = store.sched[0]
    assert t["background"] and t["deliver_via"] == "email" and t["origin"] == "call:c1"
    assert t["deliver_to"] == "aad-alex" and t["run_at"] <= datetime.now(timezone.utc)
    assert await store.get_pref("aad-alex", "background_delivery") == "an email"

    bad = json.loads(await tools.call_tool("start_background_task", {"instruction": "x", "deliver_via": "fax"},
                                           CallContext("call:c1", "aad-alex")))
    assert bad["started"] is False
    nobody = json.loads(await tools.call_tool("start_background_task", {"instruction": "x", "deliver_via": "teams"},
                                              CallContext("call:c1", "unknown")))
    assert nobody["started"] is False


async def test_runner_tells_the_model_how_a_background_result_is_delivered(cfg):
    agent, model, store, _ = make_agent(cfg, ["Three blockers: ..."])
    tools = ScheduleTools(store)
    await tools.call_tool("start_background_task", {"instruction": "Summarise PRJ blockers", "deliver_via": "email"},
                          CallContext("call:c1", "aad-alex", actor_name="Alex"))
    got = []

    async def deliver(uid, text, via="teams", task=None):
        got.append((uid, text, via, task["id"]))
        return True
    assert await TaskRunner(agent=agent, store=store, deliver=deliver).tick() == 1
    prompt = model.calls[0]["messages"][-1]["content"][0]["text"]
    assert "Background task #1" in prompt and "by email" in prompt and "attached automatically" in prompt
    uid, text, via, task_id = got[0]
    assert (uid, via, task_id) == ("aad-alex", "email", 1)
    assert text.startswith("**Background task done:** Summarise PRJ blockers") and "Three blockers" in text
    assert store.sched[0]["status"] == "done"


# ---- ResultDelivery ------------------------------------------------------------------------------------------

class Teams:
    def __init__(self):
        self.sent = []

    async def __call__(self, uid, text):
        self.sent.append((uid, text))
        return True


class Mailer:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    async def send(self, to, subject, body, footer, attachments=None, cc=None):
        if self.fail:
            raise RuntimeError("graph said no")
        self.sent.append((to, subject, body, attachments))


class People:
    async def email_for(self, uid):
        return "alex.owner@acme.example"


class Files:
    async def outbox_files(self, conv):
        return [("brief.docx", b"PK..")] if conv == "task:7" else []


class Phone:
    def __init__(self, ok=True):
        self.calls, self.ok = [], ok

    async def call_back(self, uid, name, reason, brief, channel, conv):
        self.calls.append((uid, reason, brief, channel))
        return {"calling": True} if self.ok else {"calling": False, "reason": "all my call lines are busy"}


TASK = {"id": 7, "instruction": "Draft the PRJ brief", "deliver_to_name": "Alex"}


async def test_email_delivery_attaches_the_task_files_and_strips_the_chat_footer():
    teams, mail = Teams(), Mailer()
    d = ResultDelivery(teams=teams, mailer=mail, outreach=People(), files=Files(), footer="AI footer",
                       strip_footer=lambda t: t.replace("\n\n_chat footer_", ""))
    assert await d("aad-alex", "Here it is.\n\n_chat footer_", via="email", task=TASK)
    to, subject, body, files = mail.sent[0]
    assert to == ["alex.owner@acme.example"] and subject.startswith("Done: Draft the PRJ brief")
    assert body == "Here it is." and files == [("brief.docx", b"PK..")] and teams.sent == []


async def test_email_failure_falls_back_to_teams():
    teams = Teams()
    d = ResultDelivery(teams=teams, mailer=Mailer(fail=True), outreach=People(), files=Files())
    assert await d("aad-alex", "Result", via="email", task=TASK)
    assert "couldn't email this" in teams.sent[0][1]


async def test_call_delivery_posts_to_teams_first_then_rings():
    teams, phone = Teams(), Phone()
    d = ResultDelivery(teams=teams, phone=phone)
    assert await d("aad-alex", "Full result", via="call_mobile", task=TASK)
    assert teams.sent == [("aad-alex", "Full result")]
    uid, reason, brief, channel = phone.calls[0]
    assert channel == "mobile" and brief == "Full result" and "Draft the PRJ brief" in reason


async def test_failed_call_back_says_so_in_teams():
    teams = Teams()
    await ResultDelivery(teams=teams, phone=Phone(ok=False))("aad-alex", "Full result", via="call", task=TASK)
    assert "tried to call you" in teams.sent[-1][1] and "busy" in teams.sent[-1][1]
