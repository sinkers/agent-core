"""The organisation's staff directory (Entra ID / the GAL), copied daily into the agent's own store.

Why: the agent should know who's who without a Graph call per question, find people by first name, nickname
or partial name, and reach anyone: Teams (AAD id), email (mail) and mobile (directory mobilePhone).

What is kept (active member accounts with a mailbox; rooms, guests, disabled accounts and service accounts
without mail are skipped): AAD id, name, email, job title, department, office, mobile (E.164), manager.
Needs Graph User.Read.All (application, read-only); with only User.ReadBasic.All Graph returns names and
emails and nulls the rest, which still works.

Uses:
  - `prompt_block()`: a compact staff list in the system prompt (static part, so prompt caching covers it;
    it changes at most once a day).
  - `find(query)`: local lookup for find_person / call_person / calendar invites (Graph is the fallback).
  - phone prefs: `dirphone:{e164}` -> person, so calls from a directory mobile are recognised, and
    `{aad}/directory_mobile` so call_person can ring it (phone.py). Self-registered numbers win.
  - memory: joiners, leavers and title/department changes are retained as one shared memory per sync.

Refreshed at startup when older than a day, then daily at SYNC_HOUR (local time).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from .phone import normalise
from .org import org, tz

log = logging.getLogger(__name__)
GRAPH = "https://graph.microsoft.com/v1.0"
TZ = tz()
SYNC_HOUR = 5
KEY = "directory"
SELECT = ("id,displayName,givenName,surname,mail,userPrincipalName,jobTitle,department,officeLocation,"
          "mobilePhone,accountEnabled,userType,assignedLicenses")
_WORD = re.compile(r"[^\w]+")


def _words(*texts: str | None) -> set[str]:
    out: set[str] = set()
    for t in texts:
        out |= {w for w in _WORD.split((t or "").lower()) if w}
    return out


def person_from_graph(u: dict) -> dict | None:
    """One directory entry, or None for accounts that aren't people we can reach."""
    mail = (u.get("mail") or "").lower()
    if not mail or u.get("userType", "Member") != "Member" or u.get("accountEnabled") is False:
        return None
    # Bookings pages, archived and admin accounts: unlicensed, "!Archived …" or on the onmicrosoft.com domain.
    if ("assignedLicenses" in u and not u["assignedLicenses"]) or (u.get("displayName") or "").startswith("!") \
            or mail.endswith(".onmicrosoft.com"):
        return None
    manager = u.get("manager") or {}
    return {"id": u["id"], "name": u.get("displayName") or u["mail"], "given": u.get("givenName"),
            "surname": u.get("surname"), "email": u["mail"], "title": u.get("jobTitle"),
            "department": u.get("department"), "office": u.get("officeLocation"),
            "mobile": normalise(u.get("mobilePhone")), "manager": manager.get("displayName"),
            "manager_id": manager.get("id")}


def changes(old: list[dict], new: list[dict]) -> list[str]:
    """Plain-English differences between two syncs (for memory)."""
    before, after = {p["id"]: p for p in old}, {p["id"]: p for p in new}
    out = [f"{p['name']} joined {org().name}" + (f" as {p['title']}" if p.get("title") else "") + f" ({p['email']})"
           for i, p in after.items() if i not in before]
    out += [f"{p['name']} is no longer in the {org().name} directory (left, or account disabled)"
            for i, p in before.items() if i not in after]
    for i, p in after.items():
        if i in before:
            for field, label in (("title", "job title"), ("department", "department"), ("manager", "manager")):
                if p.get(field) and p.get(field) != before[i].get(field):
                    out.append(f"{p['name']}'s {label} is now {p[field]}"
                               + (f" (was {before[i][field]})" if before[i].get(field) else ""))
    return out


class Directory:
    def __init__(self, store, *, app_id: str, app_password: str, tenant_id: str, memory=None,
                 agent_mailboxes: set[str] | None = None, http: httpx.AsyncClient | None = None, clock=None):
        self.store = store
        self.app_id, self.app_password, self.tenant_id = app_id, app_password, tenant_id
        self.memory = memory
        self.agents = {a.lower() for a in (agent_mailboxes or set())}
        self.http = http or httpx.AsyncClient(timeout=30)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.people: list[dict] = []
        self.synced_at: str | None = None

    async def load(self) -> None:
        saved = await self.store.get_pref(KEY, "people") or {}
        self.people, self.synced_at = saved.get("people", []), saved.get("synced_at")

    async def _token(self) -> str:
        r = await self.http.post(f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token", data={
            "grant_type": "client_credentials", "client_id": self.app_id, "client_secret": self.app_password,
            "scope": "https://graph.microsoft.com/.default"})
        r.raise_for_status()
        return r.json()["access_token"]

    async def _fetch(self) -> list[dict]:
        headers = {"Authorization": f"Bearer {await self._token()}"}
        url, params = f"{GRAPH}/users", {"$select": SELECT, "$top": "999",
                                          "$expand": "manager($select=id,displayName)"}
        users: list[dict] = []
        while url:
            r = await self.http.get(url, params=params, headers=headers)
            if r.status_code == 403 and "$expand" in (params or {}):  # no User.Read.All: manager is off limits
                params = {k: v for k, v in params.items() if k != "$expand"}
                continue
            r.raise_for_status()
            body = r.json()
            users += body.get("value", [])
            url, params = body.get("@odata.nextLink"), None
        return users

    async def sync(self) -> dict:
        """Fetch the directory, store it, update phone lookups and retain what changed. Returns a summary."""
        people = sorted((p for p in map(person_from_graph, await self._fetch()) if p), key=lambda p: p["name"])
        for p in people:
            p["ai_agent"] = (p["email"].lower() in self.agents or "ai agent" in (p.get("title") or "").lower()
                             or bool(re.search(r"\bbot\b", p["name"], re.I)))
        old = self.people
        diff = changes(old, people) if old else []
        # Phone lookups: directory mobiles, keeping self-registered numbers (phone:{e164}) authoritative.
        old_numbers = {p["mobile"] for p in old if p.get("mobile")}
        new_numbers = {p["mobile"]: p for p in people if p.get("mobile")}
        for number in old_numbers - set(new_numbers):
            await self.store.set_pref(f"dirphone:{number}", "owner", None)
        for number, p in new_numbers.items():
            await self.store.set_pref(f"dirphone:{number}", "owner", {"aad": p["id"], "name": p["name"]})
        for p in people:
            await self.store.set_pref(p["id"], "directory_mobile", p.get("mobile"))
        for p in old:
            if p["id"] not in {q["id"] for q in people}:
                await self.store.set_pref(p["id"], "directory_mobile", None)
        self.people, self.synced_at = people, self.clock().isoformat()
        await self.store.set_pref(KEY, "people", {"people": people, "synced_at": self.synced_at})
        if diff and self.memory:
            self.memory.retain_later(conversation_id="directory", scope="shared", user_id=None,
                                     user_name="Directory sync", channel="system",
                                     user_text=f"Changes in the {org().name} staff directory today",
                                     reply="; ".join(diff)[:4000])
        summary = {"people": len(people), "with_mobile": len(new_numbers),
                   "with_title": sum(1 for p in people if p.get("title")), "changes": diff}
        await self.store.audit({"actor": None, "action": "directory_sync", "detail": summary})
        log.info("directory sync: %d people, %d mobiles, %d changes", len(people), len(new_numbers), len(diff))
        return summary

    def stale(self) -> bool:
        if not self.synced_at:
            return True
        return self.clock() - datetime.fromisoformat(self.synced_at) > timedelta(hours=24)

    async def run_forever(self) -> None:
        while True:
            try:
                if self.stale():
                    await self.sync()
            except Exception:
                log.exception("directory sync failed")
            now = self.clock().astimezone(TZ)
            nxt = now.replace(hour=SYNC_HOUR, minute=0, second=0, microsecond=0)
            if nxt <= now:
                nxt += timedelta(days=1)
            await asyncio.sleep(max(60.0, (nxt - now).total_seconds()))
            self.synced_at = None  # force the daily refresh

    # ---- lookups ----------------------------------------------------------------------
    def find(self, query: str) -> list[dict]:
        """People matching an email, a full or partial name, or a first name; best matches first."""
        q = (query or "").strip().lower()
        if not q or not self.people:
            return []
        if "@" in q:
            return [self._public(p) for p in self.people if p["email"].lower() == q]
        terms = [t for t in _WORD.split(q) if t]
        scored = []
        for p in self.people:
            words = _words(p["name"], p.get("given"), p.get("surname"), p["email"].split("@")[0])
            if all(any(w.startswith(t) for w in words) for t in terms):
                exact = p["name"].lower() == q or (p.get("given") or "").lower() == q
                scored.append((0 if exact else 1, p["name"], p))
        return [self._public(p) for _, _, p in sorted(scored, key=lambda s: (s[0], s[1]))]

    @staticmethod
    def _public(p: dict) -> dict:
        return {"id": p["id"], "name": p["name"], "email": p["email"], "title": p.get("title"),
                "department": p.get("department")}

    def prompt_block(self) -> str:
        """Who's who, one line per person (no phone numbers: those stay behind the call tool)."""
        if not self.people:
            return ""
        lines = []
        for p in self.people:
            role = ", ".join(x for x in (p.get("title"), p.get("department")) if x)
            extra = " (AI agent)" if p.get("ai_agent") else ""
            lines.append(f"- {p['name']}{extra}" + (f" — {role}" if role else "") + f" <{p['email']}>"
                         + (f", reports to {p['manager']}" if p.get("manager") else ""))
        return (f"{org().name} staff directory (from Entra ID, refreshed daily; "
                f"{len(self.people)} people). Use these exact email addresses; you can email, Teams message "
                "and Teams call anyone here:\n" + "\n".join(lines))
