"""Jira @mentions reach the agent and it answers on the ticket (DEN-013)."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from agentcore.jira_watch import JiraWatcher, adf_text, mentions
from agentcore.store import MemoryStore

ME = "712020:ada"


def comment(cid, author, text, created, mention=ME, account_type="atlassian"):
    body = {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [
        *([{"type": "mention", "attrs": {"id": mention, "text": "@Ada (AI)"}}] if mention else []),
        {"type": "text", "text": " " + text}]}]}
    return {"id": cid, "created": created, "body": body,
            "author": {"accountId": f"acct:{author}", "displayName": author, "accountType": account_type}}


class FakeJira:
    def __init__(self, comments):
        self.comments, self.posted, self.searches = comments, [], []

    async def call_tool(self, name, args):
        if name == "jira_get" and args["path"] == "/myself":
            return json.dumps({"accountId": ME})
        if name == "jira_get":
            return json.dumps({"comments": list(reversed(self.comments))})  # newest first, as asked
        if name == "search_issues":
            self.searches.append(args["jql"])
            return json.dumps({"issues": [{"key": "PRJ-393", "summary": "Review electrical SLD Rev E",
                                           "url": "https://x/browse/PRJ-393", "status": "In Progress"}]})
        if name == "add_comment":
            self.posted.append(args)
            return json.dumps({"comment_id": "9"})
        raise AssertionError(name)


class FakeAgent:
    def __init__(self):
        self.turns = []
        self.killswitch = SimpleNamespace(outbound_allowed=True)

    async def handle(self, turn):
        self.turns.append(turn)
        return SimpleNamespace(reply="Will do: I've set a reminder for the 9:00 standup.\n\n_footer_", tier="default")

    def _strip_footer(self, text):
        return text.split("\n\n_")[0]


def test_mentions_and_text():
    c = comment("1", "Erin", "can you remind us?", "2026-10-05T21:25:56.612+0000")
    assert mentions(c["body"], ME) and not mentions(c["body"], "someone-else")
    assert adf_text(c["body"]).strip() == "@Ada (AI) can you remind us?"


async def test_a_mention_becomes_a_turn_and_the_reply_is_posted_mentioning_the_author():
    now = datetime(2026, 10, 6, 21, 50, tzinfo=timezone.utc)
    jira = FakeJira([
        comment("50296", "Alex Owner", "is there a Rev E to review?", "2026-10-05T01:52:08.647+0000", mention="acct:erin"),
        comment("50389", "Erin Hall", "can you remind us at this mornings standup?", "2026-10-05T21:25:56.612+0000"),
        comment("50400", "Ada (AI)", "noted", "2026-10-05T21:30:00.000+0000", mention=ME),   # own comment
        comment("50401", "Automation for Jira", "rule ran", "2026-10-05T21:31:00.000+0000", account_type="app"),  # not an agent
    ])
    for c in jira.comments:
        if c["id"] == "50400":
            c["author"]["accountId"] = ME
    agent, store = FakeAgent(), MemoryStore()
    watcher = JiraWatcher(agent=agent, store=store, jira=jira, clock=lambda: now)
    assert await watcher.tick() == 1
    turn = agent.turns[0]
    assert turn.conversation_id == "jira:PRJ-393" and turn.channel == "jira" and turn.scope == "shared"
    assert "Erin Hall mentioned you" in turn.text and "1 day(s) ago" in turn.text and "Tue 06 Oct 08:25" in turn.text
    assert "is there a Rev E" in turn.text  # earlier comments for context
    assert jira.posted == [{"key": "PRJ-393", "body": "Will do: I've set a reminder for the 9:00 standup.",
                            "mention_account_ids": ["acct:Erin Hall"]}]
    # the next check doesn't answer it again, and only looks back from the last check
    assert await watcher.tick() == 0 and len(jira.posted) == 1
    first = [q for q in jira.searches if "AND updated <" in q or q.startswith("updated >= -60m")]
    assert len(first) == 37  # the 36-hour backfill (+5 min overlap) in one-hour slices
    assert jira.searches[-1] == "updated >= -5m ORDER BY updated DESC"  # the next check: just since the last


async def test_nothing_happens_while_the_killswitch_is_on():
    agent = FakeAgent()
    agent.killswitch.outbound_allowed = False
    jira = FakeJira([comment("1", "Erin", "hi", "2026-10-06T21:00:00.000+0000")])
    assert await JiraWatcher(agent=agent, store=MemoryStore(), jira=jira).tick() == 0 and not jira.searches


def test_polling_never_faster_than_five_minutes():
    assert JiraWatcher(agent=None, store=None, jira=None, poll_seconds=30).poll_seconds == 300


def test_typed_at_names_become_real_mentions_and_the_author_isnt_repeated():
    recent = [comment("1", "Alex Owner", "x", "2026-10-05T01:52:08.647+0000"),
              comment("2", "Erin Hall", "y", "2026-10-05T21:25:56.612+0000"),
              comment("3", "Eve (AI)", "z", "2026-10-05T21:30:00.000+0000", account_type="app")]
    text, also = JiraWatcher._mentions_in("@Erin Hall @Alex Owner That standup has passed; @Eve (AI) "
                                          "can check.", "Erin Hall", recent)
    assert text == "That standup has passed; Eve (AI) can check." and also == ["acct:Alex Owner", "acct:Eve (AI)"]
    text, also = JiraWatcher._mentions_in("Done. I'll ask @Alex Owner to sign off.", "Erin Hall", recent)
    assert text == "Done. I'll ask Alex Owner to sign off." and also == ["acct:Alex Owner"]


def test_agent_runs_count_from_the_last_person():
    from agentcore.jira_watch import agent_run
    cs = [comment("1", "Alex Owner", "a", "t"),
          comment("2", "Ada (AI)", "b", "t", account_type="app"),
          comment("3", "Eve (AI)", "c", "t", account_type="app"),
          comment("4", "Automation for Jira", "d", "t", account_type="app"),   # not an agent, not a person
          comment("5", "Ada (AI)", "e", "t", account_type="app")]
    assert [agent_run(cs, i) for i in "12345"] == [0, 1, 2, 2, 3]


async def test_agents_answer_each_other_on_tickets_but_not_in_a_loop():
    now = datetime(2026, 10, 7, 1, 0, tzinfo=timezone.utc)
    ask = comment("10", "Eve (AI)", "Ada, can you move the due date?", "2026-10-07T00:50:00.000+0000",
                  account_type="app")
    jira = FakeJira([comment("9", "Alex Owner", "Eve please review", "2026-10-07T00:40:00.000+0000",
                             mention="acct:eve"), ask])
    agent, store = FakeAgent(), MemoryStore()
    assert await JiraWatcher(agent=agent, store=store, jira=jira, clock=lambda: now).tick() == 1
    turn = agent.turns[0]
    assert turn.from_agent and "another Acme AI agent" in turn.text and "can't approve" in turn.text
    # four agent comments in a row with no person in between: stop
    loop = [comment(str(20 + n), ("Ada (AI)", "Eve (AI)")[n % 2], "and again", f"2026-10-07T00:5{n}:00.000+0000",
                    account_type="app", mention=ME) for n in range(4)]
    jira2, agent2 = FakeJira(loop), FakeAgent()
    for c in loop:
        if c["author"]["displayName"] == "Ada (AI)":
            c["author"]["accountId"] = ME
    assert await JiraWatcher(agent=agent2, store=MemoryStore(), jira=jira2, clock=lambda: now).tick() == 1
    assert "and again" in agent2.turns[0].text and len(agent2.turns) == 1  # the 2nd Eve comment (run 4) is left


async def test_an_agents_yes_never_confirms(cfg):
    from agentcore.agent import Turn
    from .conftest import make_agent
    agent, model, store, tools = make_agent(cfg, ["ok"])
    await store.put_pending("jira:PRJ-1", "jira__resolve_issue", {"key": "PRJ-1"})
    await agent.handle(Turn("jira:PRJ-1", "yes", "jira:eve", "Eve (AI)", channel="jira", from_agent=True))
    assert await store.pop_pending("jira:PRJ-1") == ("jira__resolve_issue", {"key": "PRJ-1"})  # still waiting


async def test_a_persons_yes_on_the_ticket_approves_what_the_agent_asked():
    now = datetime(2026, 10, 7, 1, 0, tzinfo=timezone.utc)
    yes_plain = comment("30", "Alex Owner", "yes", "2026-10-07T00:55:00.000+0000", mention=None)
    store = MemoryStore()
    jira, agent = FakeJira([yes_plain]), FakeAgent()
    watcher = JiraWatcher(agent=agent, store=store, jira=jira, clock=lambda: now)
    assert await watcher.tick() == 0 and not agent.turns  # nothing was asked: a plain "yes" is just a comment
    await store.put_pending("jira:PRJ-393", "jira__resolve_issue", {"key": "PRJ-393"})
    jira2, agent2 = FakeJira([comment("31", "Alex Owner", "yes", "2026-10-07T00:56:00.000+0000"),   # "@Ada yes"
                              comment("32", "Eve (AI)", "yes", "2026-10-07T00:57:00.000+0000", mention=None,
                                      account_type="app")]), FakeAgent()
    assert await JiraWatcher(agent=agent2, store=store, jira=jira2, clock=lambda: now).tick() == 1
    turn = agent2.turns[0]
    assert turn.text == "yes" and turn.user_name == "Alex Owner" and not turn.from_agent
    assert jira2.posted[0]["mention_account_ids"] == ["acct:Alex Owner"]


def test_dans_account_counts_as_an_agent_even_without_ai_in_its_name():
    from agentcore.jira_watch import is_agent
    assert is_agent({"accountType": "app", "displayName": "Dan"})
    assert is_agent({"accountType": "app", "displayName": "Eve (AI)"})
    assert not is_agent({"accountType": "app", "displayName": "Automation for Jira"})
    assert not is_agent({"accountType": "atlassian", "displayName": "Dan Smith"})  # a person



async def test_a_mention_cut_off_by_a_restart_is_answered_again():
    now = datetime(2026, 10, 7, 5, 20, tzinfo=timezone.utc)
    jira, agent, store = FakeJira([comment("50624", "Alex Owner", "can you review this", "2026-10-07T05:05:12.841+0000")]), FakeAgent(), MemoryStore()
    # a previous run claimed it, then the pod restarted before the reply (and moved its watermark past it)
    await store.set_pref("jira-watch", "done:50624", True)
    await store.set_pref("50624", "jira-working", {"key": "PRJ-393", "created": "2026-10-07T05:05:12.841+0000"})
    await store.set_pref("jira-watch", "since", "2026-10-07T05:15:00+00:00")
    watcher = JiraWatcher(agent=agent, store=store, jira=jira, clock=lambda: now)
    assert await watcher.requeue_unfinished() == 1
    assert await watcher.tick() == 1 and jira.posted and await store.prefs_with_key("jira-working") == []


def test_pasted_files_in_a_comment_are_named():
    from agentcore.jira_watch import adf_text
    body = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Markup attached"}]},
                                        {"type": "mediaSingle", "content": [{"type": "media", "attrs": {"alt": "plan.pdf"}}]}]}
    assert "[attachment: plan.pdf]" in adf_text(body)
