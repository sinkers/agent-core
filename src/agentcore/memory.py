"""Long-term memory on Hindsight.

One Hindsight bank per agent. After every turn the exchange is retained in the background;
before every turn memories relevant to the message are recalled and given to the model.

Scoping, so one person never hears what another said privately:
  * `scope:shared`         group chats, channels, meetings, Jira/Confluence work: recalled for everyone
  * `scope:private:<aad>`  1:1 chats and 1:1 calls: recalled only for that same person
Recall uses tags_match="any_strict" over [shared, private:<asker>], so untagged or other
people's private memories are never returned.

Team-shared banks (`memory.shared_across_team`, at the owner's request): everything is
retained as shared and recall has no tag filter, so what one person tells the agent, 1:1 included (and memories
kept privately before the switch), is recalled for anyone in the organisation. Unverified phone callers still
get no memory.

Blocked content: personal health, financial and HR matters are neither sent for
retention (keyword guard) nor extracted (bank retain instructions + directive).
Failures never break a turn: recall has a short timeout and retain runs in the background.
"""

from __future__ import annotations

import asyncio
import uuid
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from .org import org, slug

log = logging.getLogger(__name__)

BLOCKED_RE = re.compile(
    r"\b(medical|diagnos\w*|illness|sick leave|surgery|pregnan\w*|mental health|therapy|medication|"
    r"salary|salaries|pay rise|payslip|bank account|bsb|tax file number|tfn|credit card|debt|"
    r"disciplinary|grievance|performance (review|improvement)|termination|redundanc\w*|resign\w*|"
    r"harass\w*|complaint about|workers'? comp\w*)\b", re.IGNORECASE)

RETAIN_INSTRUCTIONS = (
    "Extract durable work facts: projects, tickets, decisions, dates, owners, preferences about how people "
    "like to work with the agent, and who asked for what. Never extract personal health, medical, financial "
    "(pay, bank, debt) or HR matters (performance, discipline, grievances, leave reasons, resignations), "
    "and never store secrets, passwords, tokens or keys. Never extract statements about the agent's own "
    "abilities, tools, access or limits (what it can or can't do, see or remember): they change with software "
    "updates and would mislead it later."
)


# Claims about what the agent itself can or can't do. The memory service extracts them despite
# RETAIN_INSTRUCTIONS, and a stale one ("Ada's email tool only supports plain text", "Ada cannot write to
# SharePoint") makes the agent refuse things it can now do. They are dropped at recall: the agent's tools as listed
# now are the only authority (14 such memories were once found and deleted from one agent's bank).
SELF_CAPABILITY_RE = re.compile(
    r"\b(can(?:no|')t|cannot|unable to|lacks?|doesn'?t have|does not have|has no|no (?:tools?|way|ability|access)|"
    r"only (?:supports?|has|reads?|retrieves?)|read-only|not able to|(?:limited|restricted) to)\b"
    r".{0,100}?\b(tools?|upload|write|writing|attach\w*|access|calendar|invites?|e-?mail|SharePoint|Jira|Teams|"
    r"calls?|calling|files?|folders?|permissions?|formatting|plain text)\b", re.I)


def self_capability_claim(line: str, agent_name: str) -> bool:
    return bool(re.search(rf"\b({re.escape(agent_name)}|the agent|his tools?|its tools?)\b", line, re.I)
                and SELF_CAPABILITY_RE.search(line))


@dataclass
class Recalled:
    lines: list[str]

    def prompt_block(self) -> str:
        if not self.lines:
            return ""
        body = "\n".join(f"- {line}" for line in self.lines)
        return ("What you remember from earlier conversations (may be out of date; prefer live Jira/Confluence "
                "data when it disagrees, and do not reveal where a private memory came from). What you can do is "
                "set by your tools now, not by anything remembered: if a memory says you can't do something, check "
                f"your tools and try:\n{body}")


def memory_from_config(cfg) -> "HindsightMemory | None":
    """The agent's memory if it is enabled and its URL is set (with its own token when one is given)."""
    if not (cfg.memory.enabled and os.environ.get(cfg.memory.url_env)):
        return None
    key = os.environ.get(cfg.memory.api_key_env)
    http = httpx.AsyncClient(timeout=30, headers={"Authorization": f"Bearer {key}"} if key else None)
    return HindsightMemory(os.environ[cfg.memory.url_env], cfg.memory.bank_id or f"{slug()}-{cfg.agent.name}",
                           cfg.agent.display_name.split()[0], http=http, team_shared=cfg.memory.shared_across_team)


class HindsightMemory:
    def __init__(self, base_url: str, bank_id: str, agent_name: str, http: httpx.AsyncClient | None = None,
                 recall_timeout: float = 2.5, max_tokens: int = 800, team_shared: bool = False):
        self.team_shared = team_shared
        self.base = base_url.rstrip("/")
        self.bank = bank_id
        self.agent_name = agent_name
        self.http = http or httpx.AsyncClient(timeout=30)
        self.recall_timeout = recall_timeout
        self.max_tokens = max_tokens
        self._tasks: set[asyncio.Task] = set()
        self._ready = False

    @staticmethod
    def scope_tags(scope: str, user_id: str | None, team_shared: bool = False) -> list[str]:
        if scope == "private" and user_id and not team_shared:
            return [f"scope:private:{user_id}"]
        return ["scope:shared"]

    async def ensure_bank(self) -> None:
        if self._ready:
            return
        r = await self.http.put(f"{self.base}/v1/default/banks/{self.bank}", json={
            "name": f"{self.agent_name} (AI) memory",
            "mission": f"Long-term working memory for {self.agent_name} (AI), a {org().name} AI agent.",
            "retain_custom_instructions": RETAIN_INSTRUCTIONS,
        })
        if r.status_code >= 400:
            raise RuntimeError(f"hindsight bank setup failed {r.status_code}: {r.text[:200]}")
        existing = await self.http.get(f"{self.base}/v1/default/banks/{self.bank}/directives")
        names = {d.get("name") for d in (existing.json().get("items", []) if existing.status_code == 200 else [])}
        if f"{slug()}-no-self-capabilities" not in names:
            await self.http.post(f"{self.base}/v1/default/banks/{self.bank}/directives", json={
                "name": f"{slug()}-no-self-capabilities", "priority": 90, "is_active": True,
                "content": ("Do not retain the agent's statements about its own abilities, tools, access or "
                            "limitations; they change with software updates.")})
        if f"{slug()}-blocked-content" not in names:
            await self.http.post(f"{self.base}/v1/default/banks/{self.bank}/directives", json={
                "name": f"{slug()}-blocked-content", "priority": 100, "is_active": True,
                "content": "Do not retain or surface personal health, financial or HR matters, or any secrets."})
        self._ready = True

    async def recall(self, query: str, user_id: str | None) -> Recalled:
        tags = ["scope:shared"] + ([f"scope:private:{user_id}"] if user_id else [])
        try:
            await asyncio.wait_for(self.ensure_bank(), self.recall_timeout)
            r = await asyncio.wait_for(self.http.post(
                f"{self.base}/v1/default/banks/{self.bank}/memories/recall",
                json={"query": query[:2000], "budget": "low", "max_tokens": self.max_tokens,
                      **({} if self.team_shared else {"tags": tags, "tags_match": "any_strict"})}), self.recall_timeout)
            r.raise_for_status()
            lines = [res["text"].strip() for res in r.json().get("results", []) if res.get("text")]
            return Recalled([line for line in lines if not BLOCKED_RE.search(line)
                             and not self_capability_claim(line, self.agent_name)][:15])
        except Exception as exc:
            log.warning("memory recall skipped: %s", str(exc)[:200] or type(exc).__name__)
            return Recalled([])

    def retain_later(self, *, conversation_id: str, scope: str, user_id: str | None, user_name: str | None,
                     channel: str, user_text: str, reply: str) -> None:
        task = asyncio.create_task(self._retain(conversation_id, scope, user_id, user_name, channel, user_text, reply))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _retain(self, conversation_id, scope, user_id, user_name, channel, user_text, reply) -> None:
        if BLOCKED_RE.search(user_text) or BLOCKED_RE.search(reply):
            log.info("memory: not retaining a turn that touches blocked content")
            return
        who = user_name or "A colleague"
        where = {"voice": "on a Teams call"}.get(channel, "in a 1:1 Teams chat" if scope == "private" else "in a group Teams chat")
        try:
            await self.ensure_bank()
            r = await self.http.post(f"{self.base}/v1/default/banks/{self.bank}/memories", json={
                "async": True,
                "items": [{
                    "content": f"{who}: {user_text}\n{self.agent_name}: {reply}",
                    "context": f"{who} talking to {self.agent_name} {where}",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    # One document per turn: Hindsight replaces a document retained again under the same id,
                    # so a per-conversation id kept only the latest exchange of each chat or call.
                    "document_id": f"conv:{conversation_id}:{uuid.uuid4().hex[:12]}",
                    "tags": self.scope_tags(scope, user_id, self.team_shared),
                    "metadata": {"conversation_id": conversation_id, "channel": channel, "user_id": user_id or ""},
                }]})
            if r.status_code >= 400:
                log.warning("memory retain failed %s: %s", r.status_code, r.text[:200])
        except Exception as exc:
            log.warning("memory retain skipped: %s", str(exc)[:200] or type(exc).__name__)
