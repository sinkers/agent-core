"""Files people send the agents in Teams (bots declare supportsFiles in their app manifest) or attach to email.

Teams delivers a file sent to a bot as an attachment with a short-lived, pre-authenticated SharePoint
download link (`application/vnd.microsoft.teams.file.download.info`); pasted images and some other
files come as Bot Framework attachment URLs that need the bot's own token. The links never reach the
model: the app stores them per conversation (`remember_attachments`) and the message only says which
files arrived. The agent reads one with the in-process `attachments` tools, which download it (from
allowed hosts only) and convert it to text (documents.py).

  list_attachments()          files sent in this conversation recently
  read_attachment(name?)      the text of one (default: the latest); an image is shown to the model itself
  view_attachment(name?|url?, pages?)
                              what a file looks like: an image, PDF pages rendered (drawings, SLDs, layouts), or the
                              pictures embedded in a Word/PowerPoint/Excel file; also for SharePoint links (images.py)

Email attachments (mailbox.py registers them with `source: "mail"`) are downloaded with the agent's own mailbox
access (Exchange RBAC, Mail.ReadWrite on its mailbox only) through `mail_fetch`, and kept for MAIL_MAX_AGE
because email threads run for days. At first email attachments were never fetched at all and the agent
asked people for a SharePoint link instead.

Jira attachments (files on a ticket, including images pasted into a comment, which get_issue shows as
"[attachment: name]") are read with `issue` plus `name`: the bytes come from the agent's own Jira MCP server
(get_attachment, as its service account), so the agent sees exactly what its Jira account can. At first
there was no way to open them at all.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx

from .documents import MAX_DOWNLOAD_BYTES, MAX_TEXT_CHARS, document_text

log = logging.getLogger(__name__)
KEEP = 10
MAX_AGE = timedelta(hours=12)  # Teams download links expire; older entries are useless
MAIL_MAX_AGE = timedelta(days=30)  # email attachments stay in the mailbox
FILE_INFO = "application/vnd.microsoft.teams.file.download.info"
SHAREPOINT_HOSTS = (".sharepoint.com",)
BOT_HOSTS = (".trafficmanager.net", ".botframework.com", ".teams.microsoft.com")

TOOLS = [
    {"name": "list_attachments", "description": "List the files the person has sent you in this chat recently.",
     "inputSchema": {"type": "object", "properties": {}}},
    # `issue` on view/read: a file on a Jira ticket (get_issue lists them under attachments)
    {"name": "view_attachment",
     "description": ("Look at a file: an image, the pages of a PDF (drawings, single-line diagrams, layouts, scanned "
                     "pages), a DXF drawing (model space and each sheet, drawn) or the pictures inside a Word, "
                     "PowerPoint or Excel file. Use it whenever the content is visual, e.g. reviewing a drawing or a "
                     "design with diagrams. `name` is a file sent in this chat or email; or `url` for a SharePoint "
                     "link. `pages` picks PDF pages, DXF views (1 = model space, 2+ = sheets) or picture numbers, e.g. "
                     "'1-3' or [2, 5] (up to 5 per call; default the first 5). For a DXF, read_attachment gives its "
                     "title block, notes, dimensions and layers as text. For a file on a Jira ticket pass `issue` (e.g. "
                     "'PRJ-371') and `name` (the filename from get_issue)."),
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string"}, "url": {"type": "string"},
         "issue": {"type": "string", "description": "Jira issue key, for a file attached to that ticket"},
         "pages": {"description": "e.g. '1-3' or [2, 5]"}}}},
    {"name": "read_attachment",
     "description": ("Read a file the person attached in this chat or email (Word, PDF, Excel, PowerPoint, text, or "
                     "an attached email), or attached to a Jira ticket (pass `issue`, e.g. 'PRJ-371'; get_issue "
                     "lists the ticket's files, and '[attachment: name]' in a comment is one of them). `name` is "
                     "the file name; leave it out for the most recent file. Returns the file's text."),
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string"},
         "issue": {"type": "string", "description": "Jira issue key, for a file attached to that ticket"}}}},
]


def _host_ok(url: str, hosts: tuple[str, ...]) -> bool:
    try:
        u = urlparse(url)
    except ValueError:
        return False
    return u.scheme == "https" and any((u.hostname or "").endswith(h) for h in hosts)


def file_attachments(activity: dict) -> list[dict]:
    """The files in a Teams activity, with where and how to download each (untrusted hosts dropped)."""
    out: list[dict] = []
    for att in activity.get("attachments") or []:
        kind, content = att.get("contentType", ""), att.get("content")
        if kind == FILE_INFO and isinstance(content, dict):
            url = content.get("downloadUrl", "")
            if _host_ok(url, SHAREPOINT_HOSTS):
                out.append({"name": att.get("name") or "file", "url": url, "bot_auth": False,
                            "type": content.get("fileType") or ""})
        elif att.get("contentUrl") and kind not in ("text/html",) and not kind.startswith("application/vnd.microsoft.card"):
            url = att["contentUrl"]
            # An image pasted into the message comes as name and type "image/*": give it a usable name
            name = att.get("name") or kind
            if "/" in name:
                name = "pasted image" if kind.startswith("image/") else (kind.split("/")[-1] or "file")
            if _host_ok(url, BOT_HOSTS):
                out.append({"name": name, "url": url, "bot_auth": True, "type": kind})
            elif _host_ok(url, SHAREPOINT_HOSTS):
                out.append({"name": name, "url": url, "bot_auth": False, "type": kind})
    return out


def named(match: dict, data: bytes) -> str:
    """A file name with a real extension: its own, else from its bytes, else from its type (pasted images and
    unnamed files arrive as "image/*" and the like)."""
    from .images import sniff_ext
    name = match["name"]
    if "." in name and "/" not in name:
        return name
    base = re.sub(r"[^A-Za-z0-9 _-]+", "", name).strip() or "file"
    ext = sniff_ext(data) or re.sub(r"[^a-z0-9]", "", (match.get("type") or "").lower().split("/")[-1]) or "bin"
    return f"{base}.{ext}"


def _fresh(a: dict, now: datetime) -> bool:
    return datetime.fromisoformat(a["at"]) > now - (MAIL_MAX_AGE if a.get("source") == "mail" else MAX_AGE)


async def remember_attachments(store, conversation_id: str, files: list[dict]) -> None:
    if not files:
        return
    now = datetime.now(timezone.utc)
    kept = [a for a in await store.get_pref(conversation_id, "attachments") or [] if _fresh(a, now)]
    kept += [{**f, "at": now.isoformat()} for f in files]
    await store.set_pref(conversation_id, "attachments", kept[-KEEP:])


class AttachmentTools:
    in_process = True

    def __init__(self, store, bot_token=None, http: httpx.AsyncClient | None = None, mail_fetch=None, sharepoint=None,
                 jira=None):
        self.store = store
        self.jira = jira  # the agent's Jira MCP client: files on tickets (get_attachment)
        self.sharepoint = sharepoint  # openclaw MCP client: view_attachment(url=...) on SharePoint files
        self.mail_fetch = mail_fetch  # async (entry) -> (bytes, file name): email attachments (mailbox.py)
        self.bot_token = bot_token  # async () -> str, the Bot Framework token for bot-hosted attachments
        self.http = http or httpx.AsyncClient(timeout=60, follow_redirects=True)

    async def list_tools(self) -> list[dict]:
        return TOOLS

    async def _files(self, ctx) -> list[dict]:
        conv = getattr(ctx, "conversation_id", None)
        if not conv or getattr(ctx, "restricted", False):
            return []
        now = datetime.now(timezone.utc)
        return [a for a in await self.store.get_pref(conv, "attachments") or [] if _fresh(a, now)]

    async def call_tool(self, name: str, args: dict, ctx=None) -> str:
        issue = (args.get("issue") or "").strip().upper()
        if issue and name in ("read_attachment", "view_attachment"):
            return await self._jira(name, issue, (args.get("name") or "").strip(), args.get("pages"), ctx)
        files = await self._files(ctx)
        if name == "list_attachments":
            return json.dumps({"attachments": [{"name": f["name"], "type": f["type"], "sent": f["at"]} for f in files]})
        if name == "view_attachment":
            return await self._view(args, files)
        if name != "read_attachment":
            raise ValueError(f"unknown tool {name}")
        wanted = (args.get("name") or "").strip().lower()
        match = next((f for f in reversed(files) if not wanted or f["name"].lower() == wanted), None) or next(
            (f for f in reversed(files) if wanted and wanted in f["name"].lower()), None)
        if not match:
            return json.dumps({"found": False, "reason": "no such file sent in this chat (last 12 hours) or email thread"})
        from .images import is_image
        if is_image(match["name"], match.get("type")):
            return await self._view({"name": match["name"]}, files)
        if match.get("source") == "mail":
            return await self._read_mail(match)
        headers = {}
        if match["bot_auth"] and self.bot_token:
            headers["Authorization"] = f"Bearer {await self.bot_token()}"
        try:
            r = await self.http.get(match["url"], headers=headers)
            r.raise_for_status()
            if len(r.content) > MAX_DOWNLOAD_BYTES:
                return json.dumps({"name": match["name"], "error": "file is too large to read here (over 25 MB)"})
            text = await asyncio.to_thread(document_text, r.content, named(match, r.content))  # off the event loop
        except ValueError as exc:
            return json.dumps({"name": match["name"], "error": str(exc)})
        except Exception as exc:
            log.warning("attachment download failed for %s: %s", match["name"], type(exc).__name__)
            return json.dumps({"name": match["name"], "error": "could not download it; the link may have expired, "
                                                               "ask them to send it again"})
        return json.dumps(self._out(match["name"], text))

    @staticmethod
    def _out(name: str, text: str, issue: str = "") -> dict:
        out = {"name": name, "text": text[:MAX_TEXT_CHARS]}
        if len(text) > MAX_TEXT_CHARS:
            out["text_truncated"] = f"first {MAX_TEXT_CHARS} of {len(text)} characters"
        if name.lower().endswith((".pdf", ".docx", ".pptx", ".xlsx")):
            # scanned pages, drawings and pictures aren't in the text (the agent once gave up on a PDF quote)
            where = f"issue='{issue}', " if issue else ""
            out["visual"] = (f"scanned pages, drawings and pictures aren't in this text: view_attachment({where}"
                             f"name='{name}', pages=[...]) shows them")
        return out

    async def _view(self, args: dict, files: list[dict]):
        """{"text", "images"} for the gateway: the file's pages or pictures as images the model sees."""
        from .images import visual
        url, wanted = (args.get("url") or "").strip(), (args.get("name") or "").strip().lower()
        try:
            if url:
                if not self.sharepoint:
                    return json.dumps({"error": "SharePoint isn't available here"})
                from .files import _parse_sharepoint_get
                name, download = _parse_sharepoint_get(
                    await self.sharepoint.call_tool("sharepoint_get", {"url": url, "max_inline_bytes": 0}))
                r = await self.http.get(download)
                r.raise_for_status()
                data = r.content
            else:
                match = next((f for f in reversed(files) if not wanted or f["name"].lower() == wanted), None) or next(
                    (f for f in reversed(files) if wanted and wanted in f["name"].lower()), None)
                if not match:
                    return json.dumps({"found": False, "reason": "no such file in this chat or email thread; "
                                                                 "for a SharePoint file pass url"})
                data, name = await self.fetch(match)
            # Drawing a big DXF or PDF takes up to a minute of CPU: off the event loop, or the health check stops
            # answering and the pod is restarted mid-turn
            shown, info = await asyncio.to_thread(visual, data, name, args.get("pages"))
            # For a DXF the first look also carries the drawing's exact data (title block, notes, dimensions):
            # a SharePoint DXF has no other way to it, because sharepoint_get's text doesn't read DXF (dxf.py)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        except Exception as exc:
            log.warning("view failed: %s", type(exc).__name__)
            return json.dumps({"error": "couldn't open that file to look at it"})
        note = {"name": name, **info, "showing": [label for _, _, label in shown]}
        if not shown:
            note["note"] = "nothing visual to show (no pictures larger than icons)"
        return {"text": json.dumps(note), "images": shown}

    async def jira_fetch(self, issue: str, name: str = "") -> tuple[bytes, str]:
        """The bytes and file name of an attachment on a Jira ticket, through the agent's own Jira account."""
        if not self.jira:
            raise ValueError("Jira isn't available here")
        args = {"key": issue, **({"name": name} if name else {})}
        try:
            out = json.loads(await self.jira.call_tool("get_attachment", args))
        except RuntimeError as exc:  # the server's message: no such file (with the ones it has), or too large
            raise ValueError(str(exc).split("Error executing tool get_attachment:")[-1].strip()[:500]) from None
        data = base64.b64decode(out.get("content_base64") or "")
        return data, named({"name": out.get("filename") or name or "file", "type": out.get("type")}, data)

    async def _jira(self, tool: str, issue: str, name: str, pages, ctx):
        if getattr(ctx, "restricted", False):
            return json.dumps({"error": "Jira files can't be opened in this conversation"})
        from .images import is_image, visual
        try:
            data, filename = await self.jira_fetch(issue, name)
            if tool == "view_attachment" or is_image(filename, None):
                shown, info = await asyncio.to_thread(visual, data, filename, pages)
                note = {"name": filename, "issue": issue, **info, "showing": [label for _, _, label in shown]}
                if not shown:
                    note["note"] = "nothing visual to show (no pictures larger than icons)"
                return {"text": json.dumps(note), "images": shown}
            text = await asyncio.to_thread(document_text, data, filename)
        except ValueError as exc:
            return json.dumps({"name": name, "issue": issue, "error": str(exc)})
        except Exception as exc:
            log.warning("Jira attachment %s on %s failed: %s", name, issue, type(exc).__name__)
            return json.dumps({"name": name, "issue": issue, "error": "couldn't download that file from Jira"})
        return json.dumps({**self._out(filename, text, issue), "issue": issue})

    async def files(self, conversation_id: str) -> list[dict]:
        """Files sent in this conversation that can still be fetched."""
        now = datetime.now(timezone.utc)
        return [a for a in await self.store.get_pref(conversation_id, "attachments") or [] if _fresh(a, now)]

    async def fetch(self, match: dict) -> tuple[bytes, str]:
        """The bytes of one remembered attachment (Teams or email) and its file name (files.py forwards these)."""
        if match.get("source") == "mail":
            if not self.mail_fetch:
                raise ValueError("email attachments can't be fetched here")
            return await self.mail_fetch(match)
        headers = {}
        if match["bot_auth"] and self.bot_token:
            headers["Authorization"] = f"Bearer {await self.bot_token()}"
        r = await self.http.get(match["url"], headers=headers)
        r.raise_for_status()
        if len(r.content) > MAX_DOWNLOAD_BYTES:
            raise ValueError("file is too large (over 25 MB)")
        return r.content, named(match, r.content)

    async def _read_mail(self, match: dict) -> str:
        if not self.mail_fetch:
            return json.dumps({"name": match["name"], "error": "email attachments can't be read here"})
        if (match.get("size") or 0) > MAX_DOWNLOAD_BYTES:
            return json.dumps({"name": match["name"], "error": "file is too large to read here (over 25 MB)"})
        try:
            data, filename = await self.mail_fetch(match)
            text = await asyncio.to_thread(document_text, data, filename)
        except ValueError as exc:
            return json.dumps({"name": match["name"], "error": str(exc)})
        except Exception as exc:
            log.warning("email attachment download failed for %s: %s", match["name"], type(exc).__name__)
            return json.dumps({"name": match["name"], "error": "could not download it from the email"})
        return json.dumps(self._out(match["name"], text))
