"""Jira mentions: when someone @mentions the agent in a Jira comment, it answers on the ticket.

The agent is an Atlassian service account, which gets no notification email, so it polls instead: every
`poll_seconds` (no faster than 5 minutes) it searches its projects for issues updated since the last
check, reads their newest comments as ADF and picks out comments that @mention its own account. Each becomes a
turn in the ticket's conversation (`jira:<KEY>`); the reply is posted as a comment that @mentions the author.

The organisation's other agents (Atlassian service accounts named "... (AI)") can ask each other this way too:
that is how agents hand work to each other, in the open on the ticket. To stop two agents talking in
a loop, a mention is answered only while at most MAX_AGENT_RUN agent comments in a row (this one included) have
followed the last comment by a person, and a "yes" from an agent never approves anything (Turn.from_agent).
The agent's own comments and other apps (Jira automation) are always ignored.

Approvals: when the agent asked for a yes on a ticket (an action its rules make it confirm), a person's comment
that is just "yes" (with or without an @mention) approves it, like a yes in Teams chat. Any person who can comment
on the ticket can give it; it expires after an hour. An agent's yes never counts. The first run looks back `backfill_hours`, so mentions made before this existed
are answered (the turn says how old they are).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .agent import is_yes
from .org import org, tz, place

log = logging.getLogger(__name__)

LOCAL_TZ = tz()
STATE = "jira-watch"
WORKING = "jira-working"  # comments being answered now (a restart mid-turn leaves them marked)
MAX_ISSUES = 100  # search_issues' limit
SLICE_MINUTES = 60
MAX_AGENT_RUN = 3  # agent comments in a row before a person must step in
MAX_COMMENTS = 20


def _when(value: str) -> datetime | None:
    """Jira's 2026-10-05T21:25:56.612+0000."""
    try:
        return datetime.strptime(value[:19] + value[23:], "%Y-%m-%dT%H:%M:%S%z")
    except (ValueError, TypeError):
        return None


AGENT_NAMES = {m.split("@")[0].lower() for m in org().agent_mailboxes}  # the agents' accounts (org.yaml)


def is_agent(author: dict) -> bool:
    """An AI agent's service account (Ada (AI), ...), not a person or Jira automation."""
    name = (author.get("displayName") or "").strip()
    return author.get("accountType") == "app" and (name.endswith("(AI)") or name.split(" ")[0].lower() in AGENT_NAMES)


def is_person(author: dict) -> bool:
    return author.get("accountType") in (None, "atlassian")


def agent_run(comments: list[dict], comment_id: str) -> int:
    """How many agent comments in a row end at this one (comments oldest first)."""
    run = 0
    for c in comments:
        author = c.get("author") or {}
        run = run + 1 if is_agent(author) else (0 if is_person(author) else run)
        if c.get("id") == comment_id:
            return run
    return run


def mentions(node, account_id: str) -> bool:
    """True if this ADF node (or anything inside it) is an @mention of `account_id`."""
    if isinstance(node, dict):
        if node.get("type") == "mention" and (node.get("attrs") or {}).get("id") == account_id:
            return True
        return any(mentions(c, account_id) for c in node.get("content") or [])
    if isinstance(node, list):
        return any(mentions(c, account_id) for c in node)
    return False


def without_mentions(node) -> str:
    """The comment's words with @mentions taken out ("@Ada (AI) yes" -> "yes")."""
    if isinstance(node, list):
        return " ".join(without_mentions(n) for n in node).strip()
    if not isinstance(node, dict) or node.get("type") == "mention":
        return ""
    if node.get("type") == "text":
        return node.get("text", "")
    return without_mentions(node.get("content") or [])


def adf_text(node) -> str:
    """Plain text of an ADF comment, mentions as @Name."""
    if isinstance(node, list):
        return "".join(adf_text(n) for n in node)
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    if kind == "text":
        return node.get("text", "")
    if kind == "mention":
        return (node.get("attrs") or {}).get("text") or "@someone"
    if kind in ("inlineCard", "blockCard"):
        return (node.get("attrs") or {}).get("url", "")
    if kind == "hardBreak":
        return "\n"
    if kind == "media":  # a file or image pasted into the comment: an attachment on the issue
        name = (node.get("attrs") or {}).get("alt") or ""
        return f"[attachment: {name}]" if name else "[attachment]"
    inner = adf_text(node.get("content") or [])
    return inner + ("\n" if kind in ("paragraph", "heading", "listItem", "codeBlock") else "")


class JiraWatcher:
    def __init__(self, *, agent, store, jira, poll_seconds: int = 300, backfill_hours: int = 36, clock=None):
        self.agent, self.store, self.jira = agent, store, jira  # jira: the agent's jira MCP client
        self.poll_seconds = max(300, poll_seconds)
        self.backfill = timedelta(hours=backfill_hours)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._me: str | None = None

    async def _get(self, path: str, params: dict | None = None):
        out = json.loads(await self.jira.call_tool("jira_get", {"path": path, **({"params": params} if params else {})}))
        return {} if isinstance(out, dict) and out.get("truncated") else out  # 20 comments never come near it

    async def me(self) -> str:
        if not self._me:
            self._me = (await self._get("/myself"))["accountId"]
        return self._me

    async def requeue_unfinished(self) -> int:
        """Comments a previous run claimed but never answered (the pod restarted mid-turn, e.g. a deploy): release
        them and look back far enough that the next check answers them."""
        working = await self.store.prefs_with_key(WORKING)
        if not working:
            return 0
        oldest = None
        for comment_id, info in working:
            await self.store.set_pref(STATE, f"done:{comment_id}", None)
            await self.store.set_pref(comment_id, WORKING, None)
            created = _when((info or {}).get("created") or "")
            oldest = created if created and (oldest is None or created < oldest) else oldest
            log.warning("Jira comment %s on %s was cut off by a restart; answering it again", comment_id,
                        (info or {}).get("key"))
        saved = await self.store.get_pref(STATE, "since")
        if oldest and (not saved or datetime.fromisoformat(saved) > oldest):
            await self.store.set_pref(STATE, "since", (oldest - timedelta(minutes=1)).isoformat())
        return len(working)

    async def run_forever(self) -> None:
        try:
            await self.requeue_unfinished()
        except Exception:
            log.exception("couldn't requeue unfinished Jira comments")
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("jira mention check failed")
            await asyncio.sleep(self.poll_seconds)

    async def tick(self) -> int:
        """One check. Returns how many mentions were answered."""
        if not self.agent.killswitch.outbound_allowed:
            return 0
        now = self.clock()
        saved = await self.store.get_pref(STATE, "since")
        since = datetime.fromisoformat(saved) if saved else now - self.backfill
        minutes = math.ceil((now - since).total_seconds() / 60) + 5  # overlap; comment ids stop repeats
        me = await self.me()
        handled = 0
        for issue in await self._updated(minutes):
            page = await self._get(f"/issue/{issue['key']}/comment",
                                   {"orderBy": "-created", "maxResults": MAX_COMMENTS, "expand": "renderedBody"})
            for c in reversed(page.get("comments", [])):
                created = _when(c.get("created", ""))
                author = c.get("author") or {}
                if not created or created < since - timedelta(minutes=5):
                    continue
                if author.get("accountId") == me or not (is_person(author) or is_agent(author)):
                    continue
                if is_agent(author) and agent_run(list(reversed(page.get("comments", []))), c["id"]) > MAX_AGENT_RUN:
                    log.info("not answering %s on %s: %d agent comments in a row; a person needs to step in",
                             author.get("displayName"), issue["key"], MAX_AGENT_RUN)
                    continue
                approval = is_person(author) and is_yes(without_mentions(c.get("body")))
                if approval and not await self.store.has_pending(f"jira:{issue['key']}"):
                    approval = None
                if not approval and not mentions(c.get("body"), me):
                    continue
                if await self.store.get_pref(STATE, f"done:{c['id']}"):
                    continue
                await self.store.set_pref(STATE, f"done:{c['id']}", True)  # claim before any work, and mark it
                await self.store.set_pref(c["id"], WORKING, {"key": issue["key"], "created": c.get("created")})
                try:  # "working" until answered: a restart mid-turn leaves the mark (requeue_unfinished)
                    if approval:  # a person's "yes" to an action the agent asked about on this ticket
                        await self._approve(issue, c)
                    else:
                        await self._answer(issue, c, page.get("comments", []), now)
                    handled += 1
                except Exception:
                    log.exception("answering the mention in %s comment %s failed", issue["key"], c["id"])
                await self.store.set_pref(c["id"], WORKING, None)
        await self.store.set_pref(STATE, "since", now.isoformat())
        return handled

    @staticmethod
    def _mentions_in(reply: str, author: str, recent: list[dict]) -> tuple[str, list[str]]:
        """The reply without hand-written @names (Jira shows them as plain text and nobody is notified), and the
        account ids of people named that way who are on the ticket, so they become real mentions."""
        people = {}
        for o in recent:
            a = o.get("author") or {}
            if a.get("displayName") and a.get("accountId") and (is_person(a) or is_agent(a)):
                people[a["displayName"]] = a["accountId"]
        also: list[str] = []

        def note(display):
            if display != author and people[display] not in also:
                also.append(people[display])
        names = sorted(people, key=len, reverse=True)
        reply = reply.lstrip()
        while True:  # a leading "@Jane Citizen @John Smith": the real mentions replace it
            lead = next((d for d in names if reply.startswith(f"@{d}")), None)
            if not lead:
                break
            note(lead)
            reply = reply[len(lead) + 1:].lstrip(" ,:\n")
        for display in names:  # "ask @John Smith to..." keeps the name and notifies them
            if f"@{display}" in reply:
                note(display)
                reply = reply.replace(f"@{display}", display)
        return reply.strip(), also

    async def _updated(self, minutes: int) -> list[dict]:
        """Issues updated in the last `minutes`, oldest first, searched in slices of SLICE_MINUTES so a busy day
        (or the first run's backfill) never loses issues past one search's limit."""
        seen: dict[str, dict] = {}
        for newer in range(0, minutes, SLICE_MINUTES):
            older = min(minutes, newer + SLICE_MINUTES)
            jql = f"updated >= -{older}m" + (f" AND updated < -{newer}m" if newer else "") + " ORDER BY updated DESC"
            found = json.loads(await self.jira.call_tool("search_issues", {"jql": jql, "limit": MAX_ISSUES}))
            if len(found.get("issues", [])) >= MAX_ISSUES:
                log.warning("over %d issues updated in one %d-minute slice; some may be missed", MAX_ISSUES, SLICE_MINUTES)
            for issue in found.get("issues", []):
                seen.setdefault(issue["key"], issue)
        return list(reversed(list(seen.values())))

    async def _approve(self, issue: dict, c: dict) -> None:
        """A person said yes on the ticket to the action the agent asked about there: run it as that person's yes."""
        from .agent import Turn
        key, author = issue["key"], c.get("author") or {}
        await self.store.audit({"actor": f"jira:{author.get('accountId')}", "action": "jira_approval", "target": key,
                                "detail": {"comment": c["id"], "by": author.get("displayName")}})
        result = await self.agent.handle(Turn(f"jira:{key}", "yes", f"jira:{author.get('accountId')}",
                                              author.get("displayName"), channel="jira", scope="shared"))
        if result.reply:
            reply, _ = self._mentions_in(self.agent._strip_footer(result.reply), author.get("displayName") or "", [])
            await self.jira.call_tool("add_comment", {"key": key, "body": reply,
                                                      "mention_account_ids": [author.get("accountId")]})

    async def _answer(self, issue: dict, c: dict, recent: list[dict], now: datetime) -> None:
        from .agent import Turn
        key, author = issue["key"], c.get("author") or {}
        name, account = author.get("displayName") or "someone", author.get("accountId")
        created = _when(c["created"])
        age = now - created
        local = created.astimezone(LOCAL_TZ).strftime("%a %d %b %H:%M")
        late = (f" That was {age.days} day(s) ago." if age.days else
                f" That was {int(age.total_seconds() // 3600)} hours ago." if age > timedelta(hours=2) else "")
        context = "\n".join(f"- {(o.get('author') or {}).get('displayName')}: {adf_text(o.get('body')).strip()[:400]}"
                            for o in reversed(recent[:6]) if o.get("id") != c["id"])
        agent = is_agent(author)
        who = f"{name}, another {org().name} AI agent," if agent else name
        text = (f"[Jira: {who} mentioned you in a comment on {key} \"{issue.get('summary')}\" ({issue.get('url')}), "
                f"status {issue.get('status')}, at {local} {place()}.{late}]\n\n"
                f"{adf_text(c.get('body')).strip()}\n\n"
                + (f"Earlier comments on the ticket (newest last):\n{context}\n\n" if context else "")
                + (f"Files on the ticket ([attachment: name] above, or get_issue's attachments) open with "
                   f"read_attachment or view_attachment with issue=\"{key}\" and the file name.\n\n"
                   if "[attachment" in adf_text(c.get("body")) + context else "")
                + "Format it for Jira: Jira is not markdown, and only its listed forms "
                  "convert (## headings, - lists, | tables | with a |---| header row, > [!info] panels, ---, "
                  "**bold**, [links](https://...)); keep tables to a few short columns. "
                + "Your reply is posted on the ticket as your comment and already starts with an @mention of them, "
                  "so write the comment itself, don't add_comment it yourself, and don't type @names (to notify "
                  "someone else on the ticket, write @ and their full name as shown above and it becomes a mention). If they asked you to do something (a reminder, a chase, "
                  "an update), do it with your tools first, then say what you did. If the moment has passed, say so "
                  "plainly and offer what you can do now."
                + (" This request is from an AI agent, not a person: do the work if it's yours to do, but an agent "
                   "can't approve anything, so anything that needs a yes still needs a person." if agent else ""))
        await self.store.audit({"actor": f"jira:{account}", "action": "jira_mention", "target": key,
                                "detail": {"comment": c["id"], "age_h": round(age.total_seconds() / 3600, 1)}})
        result = await self.agent.handle(Turn(f"jira:{key}", text, f"jira:{account}", name, channel="jira",
                                              scope="shared", from_agent=agent))
        if not result.reply:
            return
        reply, also = self._mentions_in(self.agent._strip_footer(result.reply), name, recent)
        posted = json.loads(await self.jira.call_tool("add_comment", {"key": key, "body": reply,
                                                                      "mention_account_ids": [account, *also]}))
        await self.store.audit({"actor": f"jira:{account}", "action": "jira_reply", "target": key,
                                "detail": {"comment": posted.get("comment_id"), "tier": result.tier}})
