"""Incoming email for an agent's own mailbox.

Every `poll_seconds` (default 60) the poller asks Graph for unread inbox messages: a plain HTTP request,
no model tokens. Only when something new arrives does the agent read it and use a model:

  internal  (an internal-domain sender that Exchange accepted as genuine, found in the directory)
            -> runs as that person (same tools, confirms, budgets as Teams chat) and is answered with a
               threaded reply from the agent's mailbox, footer included
  external  -> never answered automatically (a spoofable channel that could leak information); the owner
               gets a Teams message with sender, subject and a preview
  skip      -> auto-replies, bulk mail, no-reply senders, bounces and the agent's own mail

Meeting invites (a calendar invite to the agent, like inviting a person): from an internal organiser, the agent
accepts it in Outlook and joins the Teams meeting at its start time through the media bridge (`joiner`);
cancellations drop the join; if `meeting_hours` is set (bridge VM on a schedule), invites outside it are
declined with a note; invites from
outside the organisation are reported to the owner. Joining is checked on every tick, also token-free.

Each message is marked read and recorded before work starts, so it is never processed twice. While the
kill switch is engaged nothing is read or marked. Access is limited to the agent's own mailbox by
Exchange RBAC for Applications (scripts/exo-agent-mail-rbac.ps1).
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from .config import INTERNAL_DOMAINS
from .org import tz, org, place

log = logging.getLogger(__name__)
GRAPH = "https://graph.microsoft.com/v1.0"
MAX_BODY_CHARS = 20_000
SKIP_SENDERS = re.compile(r"(^|[._-])(no-?reply|do-?not-?reply|mailer-daemon|postmaster|notifications?)@|@.*\.atlassian\.net$"
                          r"|@(communication|email|engage|mail)\.microsoft\.com$", re.I)
MAX_ATTEMPTS = 3  # a failing email is retried on the next checks, then reported to the owner
QUOTE_MARKERS = re.compile(
    r"^(On .{5,200} wrote:|-{2,}\s*Original Message\s*-{2,}|_{10,}|From: .+|Sent from my .+)\s*$", re.I | re.M)


def _headers(m: dict) -> dict[str, str]:
    return {h.get("name", "").lower(): h.get("value", "") for h in m.get("internetMessageHeaders") or []}


def classify(m: dict, mailbox: str, internal_domains: set[str], ignore: set[str] | frozenset = frozenset()) -> str:
    """'internal' | 'external' | 'skip' for a Graph message."""
    sender = ((m.get("from") or {}).get("emailAddress") or {}).get("address", "").lower()
    if not sender or sender == mailbox.lower() or sender in ignore or SKIP_SENDERS.search(sender):
        return "skip"
    h = _headers(m)
    if h.get("auto-submitted", "no").lower() != "no" or h.get("precedence", "").lower() in ("bulk", "junk", "list") \
            or "list-unsubscribe" in h:  # newsletters and marketing; people don't send unsubscribe links
        return "skip"
    domain = sender.rsplit("@", 1)[-1]
    if domain not in internal_domains:
        return "external"
    # Mail from inside the tenant carries no internet headers; spoofed internal-domain mail from outside does,
    # and fails SPF/DMARC or is stamped AuthAs: Anonymous.
    auth = h.get("authentication-results", "").lower()
    if "dmarc=fail" in auth or "spf=fail" in auth or h.get("x-ms-exchange-organization-authas", "internal").lower() != "internal":
        return "external"
    return "internal"


FORWARD_RE = re.compile(r"^\s*(fw|fwd)\s*:", re.I)


def email_body(subject: str, text: str) -> str:
    """What the agent should read. A forward IS the quoted part (the forwarder's note plus the original email and
    its links), so keep it whole; a reply only needs its new part. (A reviewer agent once couldn't see the link in a
    forwarded email because the forwarded message was cut off as 'quoted thread'.)"""
    if FORWARD_RE.match(subject or ""):
        return (text or "").strip()
    return strip_quoted(text)


def strip_quoted(text: str) -> str:
    """The new part of an email: drop the quoted thread below the first reply marker."""
    m = QUOTE_MARKERS.search(text or "")
    body = text[: m.start()] if m else text
    return "\n".join(line for line in body.strip().splitlines() if not line.startswith(">")).strip()


def _html_comment(text: str, footer: str) -> str:
    """The agent's reply as formatted email (Markdown -> HTML, emailfmt.py)."""
    from .emailfmt import markdown_to_html
    return markdown_to_html(text, footer)


class MailboxPoller:
    def __init__(self, *, agent, store, mailbox: str, app_id: str, app_password: str, tenant_id: str, people,
                 owner_email: str, internal_domains: set[str] | None = None, poll_seconds: int = 60,
                 ignore_senders: set[str] | None = None, joiner=None, clock=None,
                 meeting_hours: tuple[int, int] | None = None, meeting_days: frozenset = frozenset(range(7)),
                 http: httpx.AsyncClient | None = None):
        self.agent = agent
        self.store = store
        self.mailbox = mailbox
        self.app_id, self.app_password, self.tenant_id = app_id, app_password, tenant_id
        self.people = people  # TeamsOutreach-like: find(email), send_to_user(aad, name, text)
        self.owner_email = owner_email
        self.internal = internal_domains or set(INTERNAL_DOMAINS)
        self.poll_seconds = poll_seconds
        self.ignore = {a.lower() for a in (ignore_senders or set())}
        self.joiner = joiner  # async (join_url, requester_aad_id) -> call id; None = meetings not supported
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.meeting_hours, self.meeting_days = meeting_hours, meeting_days
        self.http = http or httpx.AsyncClient(timeout=30)
        self._token: tuple[str, float] | None = None
        self._backoff_until = 0.0

    async def _bearer(self) -> str:
        if self._token and self._token[1] - 300 > time.time():
            return self._token[0]
        r = await self.http.post(f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token", data={
            "grant_type": "client_credentials", "client_id": self.app_id, "client_secret": self.app_password,
            "scope": "https://graph.microsoft.com/.default"})
        r.raise_for_status()
        b = r.json()
        self._token = (b["access_token"], time.time() + int(b.get("expires_in", 3600)))
        return self._token[0]

    async def _graph(self, method: str, path: str, **kw) -> httpx.Response:
        headers = {"Authorization": f"Bearer {await self._bearer()}", **kw.pop("headers", {})}
        return await self.http.request(method, f"{GRAPH}/users/{self.mailbox}{path}", headers=headers, **kw)

    async def requeue_unfinished(self) -> int:
        """Emails a previous run claimed but never finished (the pod restarted mid-turn): unclaim them and mark
        them unread so this run handles them. Repeated failures still stop after MAX_ATTEMPTS (_failed)."""
        working = [mid for mid, _ in await self.store.prefs_with_key(f"mail-working:{self.mailbox}")]
        n = 0
        for message_id in working:
            await self.store.set_pref(message_id, f"mail-working:{self.mailbox}", None)
            log.warning("email %s was cut off by a restart; handling it again", message_id[:12])
            await self._failed({"id": message_id})
            n += 1
        return n

    async def run_forever(self) -> None:
        try:
            await self.requeue_unfinished()
        except Exception:
            log.exception("couldn't requeue unfinished email")
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("mailbox poll failed")
            await asyncio.sleep(self.poll_seconds)

    async def tick(self) -> int:
        """One check. Returns how many new messages were handled."""
        if not self.agent.killswitch.outbound_allowed:
            return 0
        await self._join_due()
        if time.time() < self._backoff_until:
            return 0
        r = await self._graph("GET", "/mailFolders/inbox/messages", params={
            "$filter": "isRead eq false", "$top": "10",
            "$select": "id,subject,from,toRecipients,ccRecipients,receivedDateTime,conversationId,body,hasAttachments,"
                       "internetMessageHeaders"},
            headers={"Prefer": 'outlook.body-content-type="text"'})
        if r.status_code in (401, 403):
            log.warning("mailbox %s not readable yet (%s): run scripts/exo-agent-mail-rbac.ps1; retrying in 10 min",
                        self.mailbox, r.status_code)
            self._backoff_until = time.time() + 600
            return 0
        r.raise_for_status()
        messages = sorted(r.json().get("value", []), key=lambda m: m.get("receivedDateTime", ""))
        handled = 0
        for m in messages:
            if await self.store.get_pref(f"mail:{self.mailbox}", m["id"]):
                continue
            # Claim before any work, marked "working" until it's done: a restart mid-turn (a deploy, a crash)
            # leaves the mark, and the next start puts the email back (requeue_unfinished) instead of losing it.
            await self.store.set_pref(f"mail:{self.mailbox}", m["id"], "working")
            await self.store.set_pref(m["id"], f"mail-working:{self.mailbox}", True)
            await self._graph("PATCH", f"/messages/{m['id']}", json={"isRead": True})
            try:
                if m.get("@odata.type", "").startswith("#microsoft.graph.eventMessage"):
                    await self._handle_meeting(m)
                else:
                    await self._handle(m)
            except Exception:
                log.exception("email %s failed", m["id"][:12])
                await self._failed(m)
            await self.store.set_pref(m["id"], f"mail-working:{self.mailbox}", None)
            handled += 1
        return handled

    async def _failed(self, m: dict) -> None:
        """Put a failed email back (unclaimed, unread) for the next check; after MAX_ATTEMPTS leave it and say so.
        Before this, a failure (e.g. a 403 on the sender lookup) marked the email handled and it was lost."""
        key = f"mail-attempts:{self.mailbox}"
        attempts = (await self.store.get_pref(key, m["id"]) or 0) + 1
        await self.store.set_pref(key, m["id"], attempts)
        if attempts < MAX_ATTEMPTS:
            await self.store.set_pref(f"mail:{self.mailbox}", m["id"], None)
            try:
                await self._graph("PATCH", f"/messages/{m['id']}", json={"isRead": False})
            except Exception:
                log.warning("could not mark %s unread for retry", m["id"][:12])
            return
        sender = ((m.get("from") or {}).get("emailAddress") or {})
        await self.store.audit({"actor": None, "action": "email_failed", "target": sender.get("address"),
                                "detail": {"subject": (m.get("subject") or "")[:120], "attempts": attempts}})
        try:
            await self._tell_owner(sender.get("address", ""), sender.get("name") or "", m.get("subject") or "",
                                   (m.get("body") or {}).get("content", ""), note=f"I couldn't process it after {attempts} tries")
        except Exception:
            log.warning("could not tell the owner about failed email %s", m["id"][:12])

    async def _handle(self, m: dict) -> None:
        kind = classify(m, self.mailbox, self.internal, self.ignore)
        sender = (m.get("from") or {}).get("emailAddress") or {}
        address, name = sender.get("address", ""), sender.get("name") or sender.get("address", "")
        subject = m.get("subject") or "(no subject)"
        await self.store.audit({"actor": None, "action": "email_in", "target": address,
                                "detail": {"kind": kind, "subject": subject[:120]}})
        if kind == "skip":
            return
        body = email_body(subject, (m.get("body") or {}).get("content", ""))[:MAX_BODY_CHARS]
        if kind == "external":
            await self._tell_owner(address, name, subject, body)
            return
        people = await self.people.find(address)
        # Not in the directory (e.g. an alias on another of the organisation's domains) but sent from inside, with
        # SPF/DMARC passing (classify): still a colleague, so answer as that address.
        person = people[0] if len(people) == 1 else {"id": f"mail:{address.lower()}", "name": name or address}
        from .agent import Turn
        conv = f"mail:{m.get('conversationId') or m['id']}"
        note = ""
        if m.get("hasAttachments"):
            files, links = await self.email_attachments(m["id"])
            if files:
                from .attachments import remember_attachments
                await remember_attachments(self.store, conv, files)
                note += "\nAttached (read with read_attachment): " + "; ".join(
                    f"{f['name']}" + (f" ({f['size'] / 1_048_576:.1f} MB)" if f.get("size") else "") for f in files)
            if links:
                note += "\nLinked cloud files (open with sharepoint_get): " + "; ".join(f"{n}: {u}" for n, u in links)
        everyone, outside = self._reply_all_recipients(m)
        others = [a for a in everyone if a.lower() != address.lower()]
        if others or outside:
            note += ("\nYour reply goes to everyone on the thread: " + ", ".join([address] + others)
                     + (f" (not to {', '.join(outside)}: outside {org().name})" if outside else ""))
        text = (f"[Email to you from {person.get('name') or name} <{address}>]{note}\n"
                f"Subject: {subject}\n\n{body}")
        result = await self.agent.handle(Turn(conv, text, person["id"], person.get("name") or name, channel="email"))
        if not result.reply:
            return
        reply = self.agent._strip_footer(result.reply)
        if outside:  # say who wasn't copied, so the sender can forward it if they should see it
            reply += (f"\n\n_Not copied to {', '.join(outside)} (outside {org().name}): I only email outside {org().name} with a "
                      "person's say-so. Forward this if they should see it._")
        await self._send_reply_all(m, _html_comment(reply, self.agent.cfg.footer), everyone, outside)
        await self.store.audit({"actor": person["id"], "action": "email_reply", "target": address,
                                "detail": {"subject": subject[:120], "tier": result.tier, "to": everyone,
                                           "not_copied": outside}})

    def _reply_all_recipients(self, m: dict) -> tuple[list[str], list[str]]:
        """(who a reply-all reaches: the sender, then To and Cc, without this mailbox; outside addresses
        left out of it). Replies go to everyone on the thread, except people outside
        the organisation, who are never emailed without a person's yes."""
        sender = ((m.get("from") or {}).get("emailAddress") or {}).get("address", "")
        seen, inside, outside = set(), [], []
        for addr in [sender] + [((r.get("emailAddress") or {}).get("address") or "")
                                for r in (m.get("toRecipients") or []) + (m.get("ccRecipients") or [])]:
            key = addr.lower().strip()
            if not key or key in seen or key == self.mailbox.lower():
                continue
            seen.add(key)
            (inside if key.rsplit("@", 1)[-1] in self.internal else outside).append(addr)
        return inside, outside

    async def _send_reply_all(self, m: dict, html: str, everyone: list[str], outside: list[str]) -> None:
        if not outside:  # everyone is inside the organisation: Graph's reply-all as it is
            r = await self._graph("POST", f"/messages/{m['id']}/replyAll", json={"comment": html})
            r.raise_for_status()
            return
        # Someone outside the organisation is on the thread: draft the reply-all, take them off it, then send
        r = await self._graph("POST", f"/messages/{m['id']}/createReplyAll", json={"comment": html})
        r.raise_for_status()
        draft = r.json()
        keep = {a.lower() for a in everyone}

        def only_internal(recipients):
            return [x for x in recipients or [] if ((x.get("emailAddress") or {}).get("address") or "").lower() in keep]
        to, cc = only_internal(draft.get("toRecipients")), only_internal(draft.get("ccRecipients"))
        if not to:
            to, cc = cc[:1], cc[1:]
        r = await self._graph("PATCH", f"/messages/{draft['id']}", json={"toRecipients": to, "ccRecipients": cc})
        r.raise_for_status()
        r = await self._graph("POST", f"/messages/{draft['id']}/send")
        r.raise_for_status()

    async def email_attachments(self, message_id: str) -> tuple[list[dict], list[tuple[str, str]]]:
        """The real attachments of an email (inline images such as signature logos skipped), as entries for
        attachments.remember_attachments, plus cloud-file links (OneDrive/SharePoint "reference" attachments)."""
        try:
            r = await self._graph("GET", f"/messages/{message_id}/attachments",
                                  params={"$select": "id,name,contentType,size,isInline"})
            r.raise_for_status()
            items = r.json().get("value", [])
        except Exception:
            log.warning("could not list attachments of %s", message_id[:12])
            return [], []
        files, links = [], []
        for a in items:
            kind = a.get("@odata.type", "")
            # Screenshots and photos attached to an email are kept; small inline pictures are signature logos.
            if a.get("isInline") and (a.get("size") or 0) < 30_000:
                continue
            if kind.endswith("referenceAttachment"):
                links.append((a.get("name") or "file", a.get("sourceUrl") or ""))
                continue
            files.append({"name": a.get("name") or "attachment", "type": a.get("contentType") or "",
                          "source": "mail", "mailbox": self.mailbox, "message_id": message_id,
                          "attachment_id": a["id"], "size": a.get("size") or 0,
                          "item": kind.endswith("itemAttachment")})
        return files, links

    async def fetch_attachment(self, entry: dict) -> tuple[bytes, str]:
        """Download one email attachment registered by email_attachments: (bytes, file name to parse it as)."""
        path = f"/messages/{entry['message_id']}/attachments/{entry['attachment_id']}"
        if entry.get("item"):  # an attached email: read its subject and body
            r = await self._graph("GET", path, params={"$expand": "microsoft.graph.itemattachment/item"},
                                  headers={"Prefer": 'outlook.body-content-type="text"'})
            r.raise_for_status()
            item = r.json().get("item") or {}
            sender = (((item.get("from") or {}).get("emailAddress")) or {}).get("address", "")
            text = f"Attached email from {sender}\nSubject: {item.get('subject')}\n\n{(item.get('body') or {}).get('content', '')}"
            return text.encode(), "email.txt"
        r = await self._graph("GET", path + "/$value")
        r.raise_for_status()
        return r.content, entry["name"]

    async def _tell_owner(self, address: str, name: str, subject: str, body: str, note: str = "external sender") -> None:
        owners = await self.people.find(self.owner_email)
        if not owners:
            return
        preview = " ".join(body.split())[:400]
        await self.people.send_to_user(owners[0]["id"], owners[0].get("name"), (
            f"**Email to {self.mailbox}** from {name} <{address}> ({note}, so I haven't replied)\n\n"
            f"**Subject:** {subject}\n\n{preview}{'…' if len(body) > 400 else ''}\n\n"
            "Tell me here if you'd like me to reply, and what to say."))

    # ── meeting invites ──────────────────────────────────────────────────────────────────────────

    @property
    def _meetings_key(self) -> str:
        return f"meetings:{self.mailbox}"

    async def _pending(self) -> list[dict]:
        return await self.store.get_pref(self._meetings_key, "pending") or []

    @staticmethod
    def _when(dt: dict) -> datetime:
        value = (dt.get("dateTime") or "")[:19]
        tz = dt.get("timeZone") or "UTC"
        try:
            zone = timezone.utc if tz.upper() == "UTC" else ZoneInfo(tz)
        except Exception:
            log.warning("unknown time zone %r on an invite; assuming UTC", tz)
            zone = timezone.utc
        return datetime.fromisoformat(value).replace(tzinfo=zone).astimezone(timezone.utc)

    def _in_hours(self, start: datetime, end: datetime) -> bool:
        if self.meeting_hours is None:  # the media bridge runs 24x7
            return True
        local_s, local_e = start.astimezone(tz()), end.astimezone(tz())
        open_h, close_h = self.meeting_hours
        return (local_s.weekday() in self.meeting_days and local_s.hour >= open_h
                and (local_e.hour, local_e.minute) <= (close_h, 0) and local_s.date() == local_e.date())

    async def _respond(self, event_id: str, action: str, comment: str) -> None:
        r = await self._graph("POST", f"/events/{event_id}/{action}", json={"comment": comment, "sendResponse": True})
        if r.status_code >= 400:
            log.warning("could not %s invite %s: %s", action, event_id[:12], r.status_code)

    async def _handle_meeting(self, m: dict) -> None:
        r = await self._graph("GET", f"/messages/{m['id']}", params={"$expand": "microsoft.graph.eventMessage/event"},
                              headers={"Prefer": 'outlook.timezone="UTC"'})
        r.raise_for_status()
        detail = r.json()
        event = detail.get("event") or {}
        mtype = detail.get("meetingMessageType", "")
        event_id = event.get("id", "")
        subject = event.get("subject") or m.get("subject") or "a meeting"
        pending = [j for j in await self._pending() if j["event_id"] != event_id]
        if mtype == "meetingCancelled" or event.get("isCancelled"):
            await self.store.set_pref(self._meetings_key, "pending", pending)
            await self.store.audit({"action": "meeting_cancelled", "target": event_id[:40], "detail": {"subject": subject}})
            return
        if mtype != "meetingRequest" or not event_id:
            return  # responses, forwards and updates of other kinds need nothing
        kind = classify(m, self.mailbox, self.internal, self.ignore)
        sender = (m.get("from") or {}).get("emailAddress") or {}
        if kind != "internal":
            if kind == "external":
                await self._tell_owner(sender.get("address", ""), sender.get("name", ""), f"Meeting invite: {subject}",
                                       "", note=f"meeting invite from outside {org().name}, not accepted")
            return
        people = await self.people.find(sender.get("address", ""))
        join_url = ((event.get("onlineMeeting") or {}).get("joinUrl") or "")
        if not people or not self.joiner:
            return
        if not join_url:
            await self._respond(event_id, "decline", "I can only join Teams meetings: this invite has no Teams link.")
            return
        start, end = self._when(event.get("start") or {}), self._when(event.get("end") or {})
        if not self._in_hours(start, end):
            await self._respond(event_id, "decline", (
                f"Sorry, I can only join meetings between 07:00 and 19:00 on weekdays ({place()} time) for now. "
                "Paste the meeting link in my Teams chat if you'd like me to try anyway."))
            return
        await self._respond(event_id, "accept", "I'll join to take notes and post a summary afterwards.")
        pending.append({"event_id": event_id, "join_url": join_url, "subject": subject,
                        "start": start.isoformat(), "end": end.isoformat(),
                        "organizer_aad": people[0]["id"], "joined": False})
        await self.store.set_pref(self._meetings_key, "pending", pending)
        await self.store.audit({"actor": people[0]["id"], "action": "meeting_accepted", "target": event_id[:40],
                                "detail": {"subject": subject, "start": start.isoformat()}})

    async def _claim_join(self, join_url: str, start: datetime) -> bool:
        """True the first time this meeting occurrence is joined, by either path (invite list or calendar)."""
        key = f"{join_url}|{start.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M')}"
        if await self.store.get_pref(f"joined:{self.mailbox}", key):
            return False
        await self.store.set_pref(f"joined:{self.mailbox}", key, True)
        return True

    async def _join_from_calendar(self, now: datetime) -> None:
        """Join Teams meetings on the agent's own calendar that start now, however they got there: an invite
        email, a forwarded invite Exchange added by itself (often already marked read), or a series. Declined
        and cancelled meetings are skipped. A Graph read, no model tokens."""
        try:
            r = await self._graph("GET", "/calendarView", params={
                "startDateTime": (now - timedelta(minutes=10)).isoformat(),
                "endDateTime": (now + timedelta(seconds=90)).isoformat(),
                "$select": "id,subject,start,end,onlineMeeting,isCancelled,responseStatus,organizer,isOrganizer",
                "$top": "20"}, headers={"Prefer": 'outlook.timezone="UTC"'})
            if r.status_code != 200:
                return
            events = r.json().get("value", [])
        except Exception:
            log.warning("calendar check failed", exc_info=True)
            return
        for e in events:
            url = (e.get("onlineMeeting") or {}).get("joinUrl")
            response = ((e.get("responseStatus") or {}).get("response") or "").lower()
            if not url or e.get("isCancelled") or response == "declined":
                continue
            start = datetime.fromisoformat(e["start"]["dateTime"][:19]).replace(tzinfo=timezone.utc)
            end = datetime.fromisoformat(e["end"]["dateTime"][:19]).replace(tzinfo=timezone.utc)
            if not (start - timedelta(seconds=60) <= now < min(end, start + timedelta(minutes=10))):
                continue  # only near the start: never barge into a meeting half way through
            if not self._in_hours(start, end) or not await self._claim_join(url, start):
                continue
            organizer = ((e.get("organizer") or {}).get("emailAddress") or {}).get("address", "")
            found = await self.people.find(organizer) if organizer else []
            requester = found[0]["id"] if len(found) == 1 else None
            try:
                call_id = await self.joiner(url, requester)
                await self.store.audit({"actor": requester, "action": "meeting_joined", "target": e["id"][:40],
                                        "detail": {"call_id": call_id, "subject": e.get("subject"), "via": "calendar"}})
                await self.link_meeting(e["id"], call_id, e.get("subject"), start)
            except Exception:
                log.exception("could not join %s", e.get("subject"))

    async def link_meeting(self, event_id: str, call_id: str | None, subject: str | None, start: datetime) -> dict:
        """Tie a joined meeting's transcript to every internal person invited (organizer and attendees), so any of
        them can ask about it and all get the summary (transcripts.py). Before this only the organizer could:
        the agent once "had no recollection" of a review meeting when an attendee asked."""
        if not call_id:
            return {}
        try:
            r = await self._graph("GET", f"/events/{event_id}", params={"$select": "subject,attendees,organizer"})
            ev = r.json() if r.status_code == 200 else {}
        except Exception:
            log.warning("could not read attendees of %s", (subject or "")[:40])
            ev = {}
        emails = [((ev.get("organizer") or {}).get("emailAddress") or {}).get("address", "")]
        emails += [((a.get("emailAddress") or {}).get("address", "")) for a in ev.get("attendees") or []
                   if (a.get("status") or {}).get("response") != "declined"]
        ids: list[str] = []
        for email in dict.fromkeys(e.lower() for e in emails if e):
            if email == self.mailbox.lower() or email in self.ignore or email.rsplit("@", 1)[-1] not in self.internal:
                continue
            try:
                found = await self.people.find(email)
            except Exception:
                found = []
            if len(found) == 1 and found[0]["id"] not in ids:
                ids.append(found[0]["id"])
        meta = {"subject": ev.get("subject") or subject, "start": start.isoformat(), "attendees": ids[:50]}
        conv = f"call:{call_id}"
        await self.store.set_pref(conv, "meeting", meta)
        for aad in ids[:50]:
            mine = [c for c in await self.store.get_pref(aad, "meeting_calls") or [] if c != conv]
            await self.store.set_pref(aad, "meeting_calls", (mine + [conv])[-200:])
        return meta

    async def _join_due(self) -> None:
        if not self.joiner:
            return
        now = self.clock()
        pending, changed = await self._pending(), False
        for j in pending:
            start, end = datetime.fromisoformat(j["start"]), datetime.fromisoformat(j["end"])
            if not j["joined"] and start - timedelta(seconds=60) <= now < end:
                j["joined"] = True
                changed = True
                if not await self._claim_join(j["join_url"], start):
                    continue
                try:
                    call_id = await self.joiner(j["join_url"], j["organizer_aad"])
                    await self.store.audit({"actor": j["organizer_aad"], "action": "meeting_joined",
                                            "target": j["event_id"][:40], "detail": {"call_id": call_id, "subject": j["subject"]}})
                    await self.link_meeting(j["event_id"], call_id, j["subject"], start)
                except Exception:
                    log.exception("could not join %s", j["subject"])
        await self._join_from_calendar(now)
        kept = [j for j in pending if datetime.fromisoformat(j["end"]) > now - timedelta(days=1)]
        if changed or len(kept) != len(pending):
            await self.store.set_pref(self._meetings_key, "pending", kept)

