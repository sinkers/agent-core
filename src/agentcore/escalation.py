"""Chasing people, warning them, and escalating when they still don't respond (the escalation skill).

Every chase goes through `chase`, which sends the message on one channel (Teams, email, a Jira comment that
@mentions them, or a call) and records it, so the count is the agent's own record, not the model's memory.
The rules come from config (`escalation` in agent.yaml, config.ChasingConfig):

  1. A chase counts if its channel is in count_channels and it is at least min_hours_between_chases after the
     last counted chase on the same item (sooner ones are refused: no pestering).
  2. When a person has chases_before_warning counted chases about one item, on at least min_channels
     channels, within window_days, `warn` tells them first: what they were chased about, when and how, the
     deadline, who the overdue list goes to, and their record (how many times they've been on it before).
  3. grace_hours after the warning, if the item is still open, `escalate` puts it on the overdue list. The
     list (every due item, worst first, with each person's record) is emailed to email_to (copying the people
     on it) and posted in the Teams chat teams_chat_id. With require_approval, `escalate` only drafts it, and
     an approver says send (`send_escalation`): the agent never escalates on its own judgement alone.
  4. `close_chase` when they answer or the item is done. Every warning and escalation stays on the person's
     record for good; record_days sets the "N times in the last 90 days" shown.

State lives in user_prefs: one row per (person, item) under key "escalation-chase", one per person under
"escalation-record", and the pending list under ("escalation", "draft").
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from .org import org, place, tz, localise

LOCAL_TZ = tz()
CHASE = "escalation-chase"
RECORD = "escalation-record"
CHANNELS = ("teams", "email", "jira", "phone")
EMAIL_RE = re.compile(r"^[^@\s]+@([^@\s]+)$")

TOOLS = [
    {"name": "chase",
     "description": ("Chase a {org} person about something they owe (an answer, a review, a ticket update), on one "
                     "channel, and record it toward escalation. channel: teams (a Teams message), email, jira (a "
                     "comment on `jira_key` that @mentions them) or phone (a Teams call). `item` is a short, stable "
                     "name for what's owed, e.g. 'PRJ-371 site plan review' (use the same item for every chase "
                     "about it). `message` is what you say. Always chase through this tool, never send_teams_message "
                     "or send_email, or the chase isn't counted. Returns the count and what happens next."),
     "inputSchema": {"type": "object", "required": ["person", "item", "channel", "message"], "properties": {
         "person": {"type": "string", "description": "email (preferred) or name"},
         "item": {"type": "string"}, "channel": {"type": "string", "enum": list(CHANNELS)},
         "message": {"type": "string"}, "jira_key": {"type": "string"}, "link": {"type": "string"}}}},
    {"name": "chase_status",
     "description": ("Open chases with their counts and channels, which people are due a warning, which items are "
                     "due for escalation, and each person's record. `person` narrows it to one person."),
     "inputSchema": {"type": "object", "properties": {"person": {"type": "string"}}}},
    {"name": "warn",
     "description": ("Warn a person that their overdue item is about to be escalated: lists every chase (date and "
                     "channel), the deadline, who the overdue list goes to and their record. Only once the rules are "
                     "met (chase_status says so). Sent on the configured warning channels."),
     "inputSchema": {"type": "object", "required": ["person", "item"], "properties": {
         "person": {"type": "string"}, "item": {"type": "string"}}}},
    {"name": "escalate",
     "description": ("Build the overdue list from every item whose warning deadline has passed and is still open, "
                     "and send it (email and the escalation Teams chat), or, when approval is required, draft it and "
                     "return it for an approver. Check each item is really still outstanding first, and close_chase "
                     "the ones that aren't."),
     "inputSchema": {"type": "object", "properties": {"note": {"type": "string",
                                                               "description": "one optional line at the top"}}}},
    {"name": "send_escalation",
     "description": "Approvers only: send the drafted overdue list (`draft_id` from escalate).",
     "inputSchema": {"type": "object", "required": ["draft_id"], "properties": {"draft_id": {"type": "string"}}}},
    {"name": "close_chase",
     "description": ("Close a chase: they answered, it's done, or it's no longer needed. `outcome`: answered, done "
                     "or cancelled, with a short reason."),
     "inputSchema": {"type": "object", "required": ["person", "item", "outcome"], "properties": {
         "person": {"type": "string"}, "item": {"type": "string"},
         "outcome": {"type": "string", "enum": ["answered", "done", "cancelled"]}, "reason": {"type": "string"}}}},
]
TOOLS = localise(TOOLS)


def _key(email: str, item: str) -> str:
    item = re.sub(r"\s+", " ", item.strip().lower())
    return f"{email.lower()}|{item}"


def _when(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(LOCAL_TZ).strftime("%a %d %b %H:%M")


def _day(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(LOCAL_TZ).strftime("%d %b")


class EscalationTools:
    in_process = True

    def __init__(self, store, cfg, *, teams=None, mail=None, jira=None, phone=None, connector=None,
                 owner: str = "", internal_domains: set[str] | None = None, killswitch=None, clock=None):
        self.store, self.cfg = store, cfg
        self.teams, self.mail, self.jira, self.phone = teams, mail, jira, phone  # the agent's own channels
        self.connector = connector  # Bot Framework: posts the list in the escalation chat
        self.owner = owner
        self.internal = {d.lower() for d in (internal_domains or set(org().internal_domains))}
        self.killswitch = killswitch
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def list_tools(self) -> list[dict]:
        return TOOLS

    async def call_tool(self, name: str, args: dict, ctx=None) -> str:
        if not self.cfg.enabled:
            return json.dumps({"error": "escalation isn't switched on for this agent"})
        handler = {"chase": self._chase, "chase_status": self._status, "warn": self._warn, "escalate": self._escalate,
                   "send_escalation": self._send_draft, "close_chase": self._close}.get(name)
        if not handler:
            raise ValueError(f"unknown tool {name}")
        if name in ("chase", "warn", "escalate", "send_escalation") and self.killswitch \
                and not self.killswitch.outbound_allowed:
            return json.dumps({"error": "kill switch engaged: outbound messages are stopped"})
        try:
            return json.dumps(await handler(args, ctx), default=str)
        except ValueError as exc:  # no such person, not internal, a channel that can't be used: tell the model
            return json.dumps({"error": str(exc)})

    # ---- people and state ----
    async def _person(self, who: str) -> dict:
        who = (who or "").strip()
        people = await self.teams.find(who) if self.teams else []
        if not people and EMAIL_RE.match(who):
            people = [{"name": who.split("@")[0], "email": who, "id": None}]
        if not people:
            raise ValueError(f"no {org().name} person matches {who!r}")
        if len(people) > 1 and not EMAIL_RE.match(who):
            raise ValueError(f"more than one person matches {who!r}: "
                             f"{', '.join(p['name'] + ' <' + p['email'] + '>' for p in people[:5])}; use their email")
        p = people[0]
        m = EMAIL_RE.match(p.get("email") or "")
        if not m or m.group(1).lower() not in self.internal:
            raise ValueError(f"{p.get('email')} isn't a {org().name} address: only {org().name} people are chased or escalated")
        return {"name": p["name"], "email": p["email"].lower(), "id": p.get("id")}

    async def _get(self, email: str, item: str) -> dict | None:
        return await self.store.get_pref(_key(email, item), CHASE)

    async def _put(self, c: dict) -> None:
        await self.store.set_pref(_key(c["email"], c["item"]), CHASE, c)

    async def _open(self) -> list[dict]:
        return [c for _, c in await self.store.prefs_with_key(CHASE) if c.get("status") == "open"]

    async def _record(self, email: str) -> dict:
        return await self.store.get_pref(email.lower(), RECORD) or {"warnings": [], "escalations": []}

    def _record_line(self, rec: dict, now: datetime) -> str:
        since = now - timedelta(days=self.cfg.record_days)
        recent = [e for e in rec["escalations"] if datetime.fromisoformat(e["at"]) > since]
        total = len(rec["escalations"])
        if not total:
            return "You haven't been on the overdue list before."
        last = _day(rec["escalations"][-1]["at"])
        return (f"You've been on the overdue list {len(recent)} time(s) in the last {self.cfg.record_days} days "
                f"({total} in all, most recently {last}).")

    def _counted(self, c: dict, now: datetime) -> list[dict]:
        since = now - timedelta(days=self.cfg.window_days)
        return [x for x in c["chases"] if x["counted"] and datetime.fromisoformat(x["at"]) > since]

    def _stage(self, c: dict, now: datetime) -> str:
        counted = self._counted(c, now)
        channels = {x["channel"] for x in counted}
        if c.get("warned_at"):
            deadline = datetime.fromisoformat(c["warned_at"]) + timedelta(hours=self.cfg.grace_hours)
            if c["email"] in {e.lower() for e in self.cfg.exempt}:
                return "exempt"
            return "due_escalation" if now >= deadline else "warned"
        if len(counted) >= self.cfg.chases_before_warning and len(channels) >= self.cfg.min_channels:
            return "due_warning"
        return "chasing"

    def _next(self, c: dict, now: datetime) -> str:
        counted = self._counted(c, now)
        channels = {x["channel"] for x in counted}
        stage = self._stage(c, now)
        if stage == "due_warning":
            return "warning due: call warn"
        if stage == "warned":
            deadline = datetime.fromisoformat(c["warned_at"]) + timedelta(hours=self.cfg.grace_hours)
            return f"warned; escalation due after {_when(deadline.isoformat())} {place()} if still open"
        if stage == "due_escalation":
            return "warning deadline passed: goes on the overdue list at the next escalate"
        if stage == "exempt":
            return "warned; this person is exempt from escalation"
        need = max(0, self.cfg.chases_before_warning - len(counted))
        more = max(0, self.cfg.min_channels - len(channels))
        bits = [f"{need} more chase(s)"] if need else []
        if more:
            bits.append(f"{more} more channel(s) than {', '.join(sorted(channels)) or 'none yet'}")
        return f"before a warning: {' and '.join(bits)}"

    # ---- sending ----
    async def _send(self, channel: str, person: dict, subject: str, text: str, ctx, jira_key: str | None) -> str:
        if channel == "teams":
            out = await self.teams.send(person["email"], text, ctx)
            if not out.get("sent"):
                raise ValueError(out.get("reason") or "the Teams message didn't send")
            return "Teams message"
        if channel == "email":
            if not self.mail:
                raise ValueError("email isn't available")
            await self.mail.call_tool("send_email", {"to": [person["email"]], "subject": subject, "body": text})
            return "email"
        if channel == "jira":
            if not self.jira or not jira_key:
                raise ValueError("a Jira chase needs jira_key (the ticket to comment on)")
            users = json.loads(await self.jira.call_tool("find_user", {"query": person["email"]})).get("users") or []
            if not users:
                raise ValueError(f"{person['name']} has no Jira account to @mention")
            await self.jira.call_tool("add_comment", {"key": jira_key.strip().upper(), "body": text,
                                                      "mention_account_ids": [users[0]["account_id"]]})
            return f"Jira comment on {jira_key.upper()}"
        if channel == "phone":
            if not self.phone:
                raise ValueError("calls aren't available")
            out = json.loads(await self.phone.call_tool("call_person", {"person": person["email"], "reason": text[:200]}, ctx))
            if out.get("error") or out.get("calling") is False:
                raise ValueError(out.get("error") or out.get("reason") or "the call didn't go through")
            return "call"
        raise ValueError(f"unknown channel {channel!r}: use one of {', '.join(CHANNELS)}")

    # ---- tools ----
    async def _chase(self, args: dict, ctx) -> dict:
        now = self.clock()
        person = await self._person(args.get("person", ""))
        item, channel = args.get("item", "").strip(), args.get("channel", "").strip().lower()
        if not item:
            raise ValueError("item is required: a short name for what's owed")
        c = await self._get(person["email"], item) or {
            "email": person["email"], "name": person["name"], "aad_id": person["id"], "item": item,
            "link": args.get("link"), "jira_key": args.get("jira_key"), "status": "open", "chases": [],
            "opened_at": now.isoformat()}
        if c["status"] != "open":  # chasing a closed item again starts a fresh count
            c.update(status="open", chases=[], warned_at=None, opened_at=now.isoformat(), closed=None)
        counted_before = [x for x in c["chases"] if x["counted"]]
        counts = channel in self.cfg.count_channels
        if counts and counted_before:
            gap = now - datetime.fromisoformat(counted_before[-1]["at"])
            if gap < timedelta(hours=self.cfg.min_hours_between_chases):
                return {"sent": False, "reason": (f"{person['name']} was last chased about this "
                                                  f"{_when(counted_before[-1]['at'])} {place()}; the next chase is allowed "
                                                  f"{self.cfg.min_hours_between_chases:g} hours after that")}
        via = await self._send(channel, person, f"Chasing: {item}", args.get("message", "").strip(), ctx,
                               args.get("jira_key") or c.get("jira_key"))
        c["chases"].append({"at": now.isoformat(), "channel": channel, "counted": counts,
                            "by": getattr(ctx, "actor_name", None) or getattr(ctx, "actor", None)})
        c["link"] = args.get("link") or c.get("link")
        c["jira_key"] = args.get("jira_key") or c.get("jira_key")
        await self._put(c)
        await self.store.audit({"actor": getattr(ctx, "actor", None), "action": "chase", "target": person["email"],
                                "detail": {"item": item, "channel": channel, "counted": counts}})
        return {"sent": True, "via": via, "to": person["name"], "item": item,
                "counted_chases": len(self._counted(c, now)),
                "channels": sorted({x["channel"] for x in self._counted(c, now)}), "next": self._next(c, now)}

    async def _status(self, args: dict, ctx) -> dict:
        now = self.clock()
        chases = await self._open()
        if args.get("person"):
            email = (await self._person(args["person"]))["email"]
            chases = [c for c in chases if c["email"] == email]
        out = []
        for c in sorted(chases, key=lambda c: c["opened_at"]):
            counted = self._counted(c, now)
            rec = await self._record(c["email"])
            out.append({"person": c["name"], "email": c["email"], "item": c["item"], "link": c.get("link"),
                        "stage": self._stage(c, now), "counted_chases": len(counted),
                        "channels": sorted({x["channel"] for x in counted}),
                        "chases": [f"{_when(x['at'])} {x['channel']}{'' if x['counted'] else ' (not counted)'}"
                                   for x in c["chases"]],
                        "warned": _when(c["warned_at"]) if c.get("warned_at") else None,
                        "times_escalated": len(rec["escalations"]), "next": self._next(c, now)})
        rules = (f"warning after {self.cfg.chases_before_warning} chases on {self.cfg.min_channels}+ channels "
                 f"within {self.cfg.window_days} days (at least {self.cfg.min_hours_between_chases:g} h apart; counted "
                 f"channels {', '.join(self.cfg.count_channels)}); escalation {self.cfg.grace_hours:g} h after the "
                 f"warning; approval {'required' if self.cfg.require_approval else 'not required'}")
        return {"rules": rules, "open": out}

    def _audience(self) -> str:
        parts = []
        if self.cfg.email_to:
            parts.append(", ".join(self.cfg.email_to))
        if self.cfg.teams_chat_id:
            parts.append("the escalation Teams chat")
        return " and ".join(parts) or "the project lead"

    async def _warn(self, args: dict, ctx) -> dict:
        now = self.clock()
        person = await self._person(args.get("person", ""))
        c = await self._get(person["email"], args.get("item", ""))
        if not c or c["status"] != "open":
            return {"sent": False, "reason": "no open chase for that person and item (see chase_status)"}
        stage = self._stage(c, now)
        if stage != "due_warning":
            return {"sent": False, "reason": f"not due a warning: {self._next(c, now)}"}
        counted = self._counted(c, now)
        deadline = now + timedelta(hours=self.cfg.grace_hours)
        rec = await self._record(person["email"])
        first = person["name"].split()[0]
        lines = "\n".join(f"- {_when(x['at'])}: {x['channel']}" for x in counted)
        text = (f"Hi {first}, a heads-up before this goes further. I've followed up with you {len(counted)} times "
                f"about **{c['item']}**{' (' + c['link'] + ')' if c.get('link') else ''}:\n{lines}\n\n"
                f"If it's still open at {_when(deadline.isoformat())} {place()}, it goes on the overdue follow-ups list, "
                f"which goes to {self._audience()}. {self._record_line(rec, now)}\n\n"
                f"A reply (even \"not mine\" or \"by Friday\") closes it.")
        sent, failed = [], []
        for ch in self.cfg.warning_channels:
            try:
                sent.append(await self._send(ch, person, f"Before this is escalated: {c['item']}", text, ctx,
                                             c.get("jira_key")))
            except Exception as exc:
                failed.append(f"{ch}: {exc}")
        if not sent:
            return {"sent": False, "reason": "; ".join(failed)}
        c["warned_at"] = now.isoformat()
        await self._put(c)
        rec["warnings"].append({"at": now.isoformat(), "item": c["item"], "chases": len(counted)})
        await self.store.set_pref(person["email"], RECORD, rec)
        await self.store.audit({"actor": getattr(ctx, "actor", None), "action": "escalation_warning",
                                "target": person["email"], "detail": {"item": c["item"], "chases": len(counted)}})
        return {"sent": True, "via": sent, "failed": failed, "deadline": _when(deadline.isoformat()) + " " + place()}

    async def _escalate(self, args: dict, ctx) -> dict:
        now = self.clock()
        due = [c for c in await self._open() if self._stage(c, now) == "due_escalation"]
        if not due:
            return {"sent": False, "reason": "nothing is due for escalation"}
        rows = []
        for c in due:
            rec = await self._record(c["email"])
            counted = self._counted(c, now)
            rows.append({"name": c["name"], "email": c["email"], "item": c["item"], "link": c.get("link"),
                         "chases": len(c["chases"]), "channels": sorted({x["channel"] for x in counted}),
                         "since": c["opened_at"], "warned_at": c["warned_at"],
                         "before": len(rec["escalations"])})
        rows.sort(key=lambda r: (-r["chases"], r["since"]))
        draft = {"id": f"E-{secrets.token_hex(3)}", "at": now.isoformat(), "rows": rows,
                 "note": (args.get("note") or "").strip()}
        if self.cfg.require_approval:
            await self.store.set_pref("escalation", "draft", draft)
            names = ", ".join(a.name for a in self.cfg.approvers) or self.owner or "an approver"
            return {"sent": False, "draft_id": draft["id"], "needs_approval_from": names,
                    "preview": self._render(draft, markdown=True),
                    "next": f"show the preview to {names}; it goes out when they ask you to send {draft['id']}"}
        return await self._deliver(draft, ctx)

    def _approver(self, ctx) -> bool:
        actor = (getattr(ctx, "actor", None) or "").lower()
        return any(actor and actor in {(a.aad_id or "").lower(), (a.email or "").lower(),
                                       (a.jira_account_id or "").lower(), f"jira:{(a.jira_account_id or '').lower()}"}
                   for a in self.cfg.approvers)

    async def _send_draft(self, args: dict, ctx) -> dict:
        draft = await self.store.get_pref("escalation", "draft")
        if not draft or draft["id"] != (args.get("draft_id") or "").strip():
            return {"sent": False, "reason": "no such draft: run escalate again for a fresh one"}
        if not self._approver(ctx):
            names = ", ".join(a.name for a in self.cfg.approvers) or "none configured"
            return {"sent": False, "reason": f"only an escalation approver can send it ({names})"}
        out = await self._deliver(draft, ctx)
        if out.get("sent"):
            await self.store.set_pref("escalation", "draft", None)
        return out

    def _render(self, draft: dict, markdown: bool) -> str:
        day = datetime.fromisoformat(draft["at"]).astimezone(LOCAL_TZ).strftime("%A %d %B %Y")
        head = f"Overdue follow-ups, {day}"
        intro = (f"{draft['note']}\n\n" if draft.get("note") else "") + (
            f"Each item below was chased at least {self.cfg.chases_before_warning} times on "
            f"{self.cfg.min_channels} or more channels, then warned {self.cfg.grace_hours:g} hours ago or more, and "
            f"is still open.")
        if markdown:
            table = ["| Person | Item | Chases | Channels | Open since | Warned | Times on this list before |",
                     "|---|---|---|---|---|---|---|"]
            for r in draft["rows"]:
                item = f"[{r['item']}]({r['link']})" if r.get("link") else r["item"]
                table.append(f"| {r['name']} | {item} | {r['chases']} | {', '.join(r['channels'])} | "
                             f"{_day(r['since'])} | {_day(r['warned_at'])} | {r['before']} |")
            return f"**{head}**\n\n{intro}\n\n" + "\n".join(table)
        lines = [f"- {r['name']}: {r['item']}{' <' + r['link'] + '>' if r.get('link') else ''}. "
                 f"Chased {r['chases']} times ({', '.join(r['channels'])}) since {_day(r['since'])}, warned "
                 f"{_day(r['warned_at'])}. On this list {r['before']} time(s) before." for r in draft["rows"]]
        return f"{head}\n\n{intro}\n\n" + "\n".join(lines)

    async def _deliver(self, draft: dict, ctx) -> dict:
        now = self.clock()
        sent, failed = [], []
        if self.cfg.email_to and self.mail:
            to = list(dict.fromkeys(self.cfg.email_to + ([r["email"] for r in draft["rows"]]
                                                         if self.cfg.cc_people_listed else [])))
            try:
                await self.mail.call_tool("send_email", {"to": to, "body": self._render(draft, markdown=True),
                                                         "subject": f"Overdue follow-ups: {len(draft['rows'])} item(s)"})
                sent.append(f"email to {len(to)}")
            except Exception as exc:
                failed.append(f"email: {exc}")
        if self.cfg.teams_chat_id and self.connector:
            ref = await self.store.get_conversation_ref(self.cfg.teams_chat_id)
            if not ref:
                failed.append("Teams chat: I'm not in it yet (add me and @mention me there once)")
            else:
                try:
                    await self.connector.send(ref, self._render(draft, markdown=True))
                    sent.append("Teams chat")
                except Exception as exc:
                    failed.append(f"Teams chat: {exc}")
        if not sent:
            return {"sent": False, "reason": "; ".join(failed) or "no email_to or teams_chat_id configured"}
        for r in draft["rows"]:  # on the record for good; the chase stays open until they answer
            rec = await self._record(r["email"])
            rec["escalations"].append({"at": now.isoformat(), "item": r["item"], "chases": r["chases"],
                                       "list": draft["id"]})
            await self.store.set_pref(r["email"], RECORD, rec)
            c = await self._get(r["email"], r["item"])
            if c:
                c["escalated_at"] = now.isoformat()
                c["warned_at"] = None  # a fresh round of chases and a new warning before it's listed again
                c["chases"] = [{**x, "counted": False} for x in c["chases"]]
                await self._put(c)
        await self.store.audit({"actor": getattr(ctx, "actor", None), "action": "escalation_sent",
                                "target": draft["id"], "detail": {"people": [r["email"] for r in draft["rows"]],
                                                                  "via": sent}})
        return {"sent": True, "via": sent, "failed": failed, "items": len(draft["rows"])}

    async def _close(self, args: dict, ctx) -> dict:
        person = await self._person(args.get("person", ""))
        c = await self._get(person["email"], args.get("item", ""))
        if not c or c["status"] != "open":
            return {"closed": False, "reason": "no open chase for that person and item"}
        c.update(status="closed", closed={"at": self.clock().isoformat(), "outcome": args.get("outcome"),
                                          "reason": args.get("reason")})
        await self._put(c)
        return {"closed": True, "person": c["name"], "item": c["item"], "chases": len(c["chases"])}
