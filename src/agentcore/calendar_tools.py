"""The agent's own calendar (the agent sends meeting invites).

In-process `calendar` tools, all on the agent's own mailbox only (Exchange RBAC for Applications,
`Application Calendars.ReadWrite` scoped to it; scripts/exo-agent-mail-rbac.ps1):

  list_my_meetings(days)                         what's on the agent's calendar (local time)
  schedule_meeting(subject, start, duration_minutes, attendees, agenda)
                                                 a Teams meeting organised by the agent, invites sent from
                                                 its mailbox; internal attendees only; the agent joins it at
                                                 the start (same pending-join list as accepted invites)
  cancel_meeting(event_id, message)              only meetings the agent organised

Invites other people send the agent are handled by mailbox.py (accept, then join).
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from .config import INTERNAL_DOMAINS
from .org import agent, org, place, tz, localise

log = logging.getLogger(__name__)
GRAPH = "https://graph.microsoft.com/v1.0"
TZ = tz()
WINDOWS_TZ = org().windows_timezone
ADDRESS_RE = re.compile(r"^[^@\s<>]+@([^@\s<>]+\.[^@\s<>]+)$")

TOOLS = [
    {"name": "list_my_meetings", "description": "List the meetings on your own calendar for the next `days` days ({place} time).",
     "inputSchema": {"type": "object", "properties": {"days": {"type": "integer", "description": "default 7"}}}},
    {"name": "schedule_meeting",
     "description": ("Book a Teams meeting organised by you and send the invites from your mailbox. `start` is local "
                     f"{place()} time (e.g. 2026-10-06T14:00). `attendees` are {org().name} email addresses (use find_person if "
                     "you only have a name). You join the meeting automatically at the start to take notes. You can't "
                     "see attendees' free/busy, so ask the person asking for a time."),
     "inputSchema": {"type": "object", "properties": {
         "subject": {"type": "string"}, "start": {"type": "string"},
         "duration_minutes": {"type": "integer", "description": "default 30"},
         "attendees": {"type": "array", "items": {"type": "string"}},
         "agenda": {"type": "string"}}, "required": ["subject", "start", "attendees"]}},
    {"name": "cancel_meeting", "description": "Cancel a meeting you organised (event id from list_my_meetings).",
     "inputSchema": {"type": "object", "properties": {"event_id": {"type": "string"}, "message": {"type": "string"}},
                     "required": ["event_id"]}},
]
TOOLS = localise(TOOLS)


def _local(dt: dict) -> datetime:
    value = (dt.get("dateTime") or "")[:19]
    tz = dt.get("timeZone") or "UTC"
    zone = timezone.utc if tz.upper() == "UTC" else (TZ if tz == WINDOWS_TZ else ZoneInfo(tz))
    return datetime.fromisoformat(value).replace(tzinfo=zone).astimezone(TZ)


class CalendarTools:
    in_process = True

    def __init__(self, store, *, mailbox: str, app_id: str, app_password: str, tenant_id: str,
                 internal_domains: set[str] | None = None, http: httpx.AsyncClient | None = None, clock=None):
        self.store = store
        self.mailbox = mailbox
        self.app_id, self.app_password, self.tenant_id = app_id, app_password, tenant_id
        self.internal = internal_domains or set(INTERNAL_DOMAINS)
        self.http = http or httpx.AsyncClient(timeout=30)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._token: tuple[str, float] | None = None

    async def list_tools(self) -> list[dict]:
        return TOOLS

    async def _graph(self, method: str, path: str, **kw) -> httpx.Response:
        if not self._token or self._token[1] - 300 < time.time():
            r = await self.http.post(f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token", data={
                "grant_type": "client_credentials", "client_id": self.app_id, "client_secret": self.app_password,
                "scope": "https://graph.microsoft.com/.default"})
            r.raise_for_status()
            b = r.json()
            self._token = (b["access_token"], time.time() + int(b.get("expires_in", 3600)))
        headers = {"Authorization": f"Bearer {self._token[0]}", **kw.pop("headers", {})}
        return await self.http.request(method, f"{GRAPH}/users/{self.mailbox}{path}", headers=headers, **kw)

    async def call_tool(self, name: str, args: dict, ctx=None) -> str:
        if getattr(ctx, "restricted", False):
            return json.dumps({"error": "not available"})
        if name == "list_my_meetings":
            return json.dumps(await self._list(int(args.get("days") or 7)))
        if name == "schedule_meeting":
            return json.dumps(await self._schedule(args, ctx))
        if name == "cancel_meeting":
            return json.dumps(await self._cancel(args.get("event_id", ""), args.get("message", "")))
        raise ValueError(f"unknown tool {name}")

    async def _list(self, days: int) -> dict:
        now = self.clock()
        r = await self._graph("GET", "/calendarView", params={
            "startDateTime": now.isoformat(), "endDateTime": (now + timedelta(days=max(1, min(days, 60)))).isoformat(),
            "$select": "id,subject,start,end,organizer,onlineMeeting,isOrganizer,isCancelled", "$top": "50",
            "$orderby": "start/dateTime"}, headers={"Prefer": 'outlook.timezone="UTC"'})
        if r.status_code in (401, 403):
            return {"error": "my calendar isn't accessible yet (Exchange permission still propagating)"}
        r.raise_for_status()
        return {"meetings": [{
            "event_id": e["id"], "subject": e.get("subject"),
            "start": _local(e["start"]).strftime("%a %d %b %H:%M"), "end": _local(e["end"]).strftime("%H:%M"),
            "organizer": ((e.get("organizer") or {}).get("emailAddress") or {}).get("name"),
            "you_organised": bool(e.get("isOrganizer")), "teams": bool((e.get("onlineMeeting") or {}).get("joinUrl")),
        } for e in r.json().get("value", []) if not e.get("isCancelled")]}

    async def _schedule(self, args: dict, ctx) -> dict:
        attendees = [a.strip() for a in args.get("attendees") or [] if a and a.strip()]
        bad = [a for a in attendees if not ADDRESS_RE.match(a) or ADDRESS_RE.match(a).group(1).lower() not in self.internal]
        if not attendees or bad:
            return {"scheduled": False, "reason": (f"{', '.join(bad)} is outside {org().name} or not an email address: I can only "
                                                   f"invite {org().name} staff; external invites go from a person's own Outlook")
                    if bad else "no attendees"}
        try:
            start = datetime.fromisoformat(args["start"].strip())
        except (KeyError, ValueError):
            return {"scheduled": False, "reason": f"start must be a date and time like 2026-10-06T14:00 ({place()})"}
        start = start.replace(tzinfo=TZ) if start.tzinfo is None else start.astimezone(TZ)
        if start < self.clock().astimezone(TZ) - timedelta(minutes=5):
            return {"scheduled": False, "reason": "that time is in the past"}
        end = start + timedelta(minutes=max(10, min(int(args.get("duration_minutes") or 30), 480)))
        requester = getattr(ctx, "actor_name", None) or "a colleague"
        agenda = (args.get("agenda") or "").strip()
        body = (f"<p>{agenda}</p>" if agenda else "") + (
            f"<p>Booked by {agent().display_name} at the request of {requester}. I'll join to take notes and post a summary "
            f"afterwards.</p><p><em>{agent().display_name} is an AI agent. Owner: {agent().owner}.</em></p>")
        r = await self._graph("POST", "/events", json={
            "subject": args["subject"].strip(),
            "start": {"dateTime": start.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": WINDOWS_TZ},
            "end": {"dateTime": end.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": WINDOWS_TZ},
            "attendees": [{"emailAddress": {"address": a}, "type": "required"} for a in attendees],
            "body": {"contentType": "HTML", "content": body},
            "isOnlineMeeting": True, "onlineMeetingProvider": "teamsForBusiness", "allowNewTimeProposals": True})
        if r.status_code in (401, 403):
            return {"scheduled": False, "reason": "my calendar isn't accessible yet (Exchange permission still propagating)"}
        r.raise_for_status()
        ev = r.json()
        join_url = (ev.get("onlineMeeting") or {}).get("joinUrl", "")
        if join_url:  # join our own meeting at the start, like an accepted invite (mailbox.py)
            key = f"meetings:{self.mailbox}"
            pending = await self.store.get_pref(key, "pending") or []
            pending.append({"event_id": ev["id"], "join_url": join_url, "subject": args["subject"].strip(),
                            "start": start.astimezone(timezone.utc).isoformat(),
                            "end": end.astimezone(timezone.utc).isoformat(),
                            "organizer_aad": getattr(ctx, "actor", None), "joined": False})
            await self.store.set_pref(key, "pending", pending)
        await self.store.audit({"actor": getattr(ctx, "actor", None), "action": "meeting_scheduled", "target": ev["id"][:40],
                                "detail": {"subject": args["subject"][:120], "attendees": attendees,
                                           "start": start.isoformat(), "requested_by": requester}})
        return {"scheduled": True, "event_id": ev["id"], "start": start.strftime("%a %d %b %H:%M"),
                "end": end.strftime("%H:%M"), "attendees": attendees, "join_url": join_url,
                "outlook_link": ev.get("webLink")}

    async def _cancel(self, event_id: str, message: str) -> dict:
        r = await self._graph("GET", f"/events/{event_id}", params={"$select": "isOrganizer,subject"})
        if r.status_code == 404:
            return {"cancelled": False, "reason": "no such meeting on my calendar"}
        r.raise_for_status()
        if not r.json().get("isOrganizer"):
            return {"cancelled": False, "reason": "I can only cancel meetings I organised; I can decline others"}
        subject = r.json().get("subject")
        r = await self._graph("POST", f"/events/{event_id}/cancel", json={"comment": message or "Cancelled."})
        r.raise_for_status()
        key = f"meetings:{self.mailbox}"
        await self.store.set_pref(key, "pending", [j for j in await self.store.get_pref(key, "pending") or []
                                                   if j["event_id"] != event_id])
        return {"cancelled": True, "subject": subject}
