"""Files the agent sends: in Teams chat, on Jira tickets, by email and into SharePoint (in-process `files` tools).

  create_file(filename, content)            build a .docx/.xlsx/.svg/.png/text file from text (filemaker.py) into
                                            this conversation's outbox; returns a file reference. Images a .docx
                                            names (![..](ref)) are fetched first; a drawing comes back as a preview
                                            so the model can check it
  edit_file(file, edits, track_changes?, allow_losses?, filename?)
                                            change a Word, Excel or PowerPoint file (docbuild/sheets/deck .edit_*);
                                            the result is a new outbox file and a list of what changed
  file_outline(file)                        what's in a document: headings and tables, sheets, or slides
  preview_file(file, pages?)                its pages as images (office.py, LibreOffice) to check the layout
  insert_image(document, image|svg, after?, caption?, width_cm?, filename?)
                                            put a drawing or picture into an existing Word document (after a
                                            heading or text, or in place of a [placeholder]) or onto a new slide
                                            at the end of a deck; the result is a new file in the outbox
  send_file(file, message?)                 into this Teams chat. 1:1 chats: Teams' file-consent flow (the person
                                            clicks Allow; the file goes to their OneDrive and shows in the chat).
                                            Group chats and channels can't receive files from bots, so a SharePoint
                                            file is shared as a link and anything else must be saved first.
  attach_to_jira(issue_key, file)           attach to a ticket (the Jira MCP's add_attachment, as the agent's account)
  email_file(to, subject, body, file, cc?)  email it as an attachment to colleagues (rich text body, up to 3 MB)
  save_file_to_sharepoint(file, site, folder_path, filename?)
                                            through the gateway's sharepoint_put rule, so its site allow-list and
                                            "replacing asks" apply exactly as if the model had called it

`file` is any of: a reference from create_file ("outbox:…"), the name of a file sent in this chat or email
thread (attachments.py), or a SharePoint link. File bytes never pass through the model.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

import httpx

from .config import INTERNAL_DOMAINS
from .mail import RECIPIENT_RULES
from .filemaker import image_refs, make_file
from .org import org, localise

log = logging.getLogger(__name__)
OUTBOX_DAYS = 7
MAX_EMAIL_BYTES = 3 * 1024 * 1024  # Graph sendMail with inline attachments
CONSENT = "application/vnd.microsoft.teams.card.file.consent"
FILE_INFO = "application/vnd.microsoft.teams.card.file.info"
SHAREPOINT_RE = re.compile(r"^https://[a-z0-9-]+\.sharepoint\.com/", re.I)
ADDRESS_RE = re.compile(r"^[^@\s<>]+@([^@\s<>]+\.[^@\s<>]+)$")

FILE_ARG = {"type": "string", "description": ("The file: a reference from create_file (outbox:…), the name of a file "
                                              "someone sent in this chat or email, or a SharePoint link.")}
TOOLS = [
    {"name": "create_file",
     "description": ("Create a file from text you write, to send, attach or save. Word, Excel and PowerPoint "
                     "come out in the house style (office-documents skill). .docx from Markdown: optional front "
                     "matter (type, title, subtitle, number, revision, date, status, author, reviewers) makes the "
                     "cover and title block; then headings, lists, tables, **bold**, [links](https://..), "
                     "> quotes, code, <!-- pagebreak -->, figures (```svg Caption block, or ![Caption](ref)). "
                     ".xlsx from Markdown tables (a sheet per '## Sheet name'), CSV, or JSON {sheets: [{name, "
                     "columns, rows, totals?}]}; '=SUM(B2:B9)' stays a formula. .pptx from Markdown (# deck title, "
                     "then a slide per '## Title' or '---': bullets, a table, > section divider, ![](ref) picture, "
                     "```chart bar CSV) or JSON {title, slides: [{kind, ...}]} on the brand's master deck. .svg/.png "
                     "from SVG markup; .md/.txt/.csv/.json/.html as text. Word, Excel and PowerPoint come back with "
                     "a preview of the first page or slides: look at it and fix anything wrong before sending. "
                     "Returns a file reference for send_file, attach_to_jira, email_file, save_file_to_sharepoint, "
                     "edit_file or insert_image."),
     "inputSchema": {"type": "object", "properties": {
         "filename": {"type": "string", "description": "e.g. 'PRJ-dismantling-plan.docx'"},
         "content": {"type": "string"},
         "preview": {"type": "boolean", "description": "default true for .docx/.xlsx/.pptx"}},
         "required": ["filename", "content"]}},
    {"name": "edit_file",
     "description": ("Change an existing Word, Excel or PowerPoint file (one you made, one sent in, or a "
                     "SharePoint link); the result is a NEW file, so the original is untouched until you save over "
                     "it. Look at it first with file_outline. edits is a list, applied in order:\n"
                     "Word: {op: replace_text, find, replace} · {op: insert, markdown, after_heading? | after? "
                     "(text)} · {op: replace_section, heading, markdown} · {op: delete_section, heading} · "
                     "{op: set_table_cell, table, row, col, text} · {op: add_table_row, table, values} · "
                     "{op: comment, on (text in the document), text}. track_changes: true makes them Word tracked "
                     "changes by you (use it on other people's documents).\n"
                     "Excel: {op: set_cells, sheet?, cells: {B4: 1200, C4: '=B4*1.1'}} · {op: append_rows | "
                     "insert_rows (at), sheet?, rows} · {op: delete_rows, sheet?, from, count} · {op: add_sheet, "
                     "name, rows?} · {op: rename_sheet, sheet, to} · {op: set_column_width, sheet?, column, width}.\n"
                     "PowerPoint: {op: replace_text, find, replace} · {op: set_title | set_bullets | set_notes, "
                     "slide, ...} · {op: add_slides, after?, slides: [...]} · {op: delete_slide | move_slide, "
                     "slide, to?} · {op: set_table_cell, slide, row, col, text}. Tables, rows, columns and slides "
                     "are numbered from 1. Comes back with what changed and a preview."),
     "inputSchema": {"type": "object", "properties": {
         "file": FILE_ARG, "edits": {"type": "array", "items": {"type": "object"}},
         "track_changes": {"type": "boolean", "description": "Word only: as tracked changes by you"},
         "allow_losses": {"type": "boolean", "description": ("Excel only: go ahead although the workbook has "
                                                             "charts/pivots/images that won't survive (ask first)")},
         "preview_pages": {"description": "pages or slides to preview, e.g. [1, 3] or '2-4'; default the first"},
         "filename": {"type": "string", "description": "name for the result; default: the file's own name"}},
         "required": ["file", "edits"]}},
    {"name": "file_outline",
     "description": ("What's in a Word, Excel or PowerPoint file, to plan an edit: Word headings, tables (with "
                     "their first row), comments and tracked changes; Excel sheets with sizes and first rows; "
                     "PowerPoint slides with layout, title and text."),
     "inputSchema": {"type": "object", "properties": {"file": FILE_ARG}, "required": ["file"]}},
    {"name": "preview_file",
     "description": ("See pages of a Word, Excel or PowerPoint file (or PDF) as they'll look, to check layout "
                     "before sending: up to 5 pages per call, e.g. pages: [1, 2] or '3-5'."),
     "inputSchema": {"type": "object", "properties": {"file": FILE_ARG, "pages": {}}, "required": ["file"]}},
    {"name": "send_file",
     "description": ("Send a file to the person in this Teams chat (on a call, by email or in a background task: to "
                     "their own 1:1 Teams chat with you). In a 1:1 chat they get a card to accept it (it "
                     "saves to their OneDrive). Group chats and channels can't receive files from you: there a "
                     "SharePoint file is shared as a link; save other files to SharePoint first."),
     "inputSchema": {"type": "object", "properties": {"file": FILE_ARG, "message": {"type": "string"}},
                     "required": ["file"]}},
    {"name": "attach_to_jira", "description": "Attach a file to a Jira issue (as your Jira account).",
     "inputSchema": {"type": "object", "properties": {"issue_key": {"type": "string"}, "file": FILE_ARG},
                     "required": ["issue_key", "file"]}},
    {"name": "email_file",
     "description": ("Email a file as an attachment to {org} people (internal addresses only; exact addresses from "
                     "find_person). Body in Markdown, sent formatted. Attachments up to 3 MB; for bigger files "
                     "email a SharePoint link instead. " + RECIPIENT_RULES),
     "inputSchema": {"type": "object", "properties": {
         "to": {"type": "array", "items": {"type": "string"}},
         "cc": {"type": "array", "items": {"type": "string"}}, "subject": {"type": "string"},
         "body": {"type": "string"}, "file": FILE_ARG}, "required": ["to", "subject", "body", "file"]}},
    {"name": "insert_image",
     "description": ("Put a drawing or picture into an existing document; the result is a new file (send or save "
                     "it as usual). Word (.docx): placed after the first paragraph or heading containing `after`, "
                     "or in place of a paragraph that is exactly `after` when that is a placeholder like "
                     "'[Figure: site layout]'; with no `after`, at the end. PowerPoint (.pptx): on a new slide at "
                     "the end, titled with the caption. Give `image` (a file reference: an .svg or picture) or "
                     "`svg` (markup). SVG goes in as vectors with a picture fallback."),
     "inputSchema": {"type": "object", "properties": {
         "document": FILE_ARG, "image": FILE_ARG, "svg": {"type": "string"},
         "after": {"type": "string"}, "caption": {"type": "string"},
         "width_cm": {"type": "number", "description": "default: natural size, at most the text width (16 cm)"},
         "filename": {"type": "string", "description": "name for the result; default: the document's own name"}},
         "required": ["document"]}},
    {"name": "save_file_to_sharepoint",
     "description": ("Save a file (e.g. one you created) into SharePoint. Same rules as sharepoint_put: only your "
                     "allowed sites, and replacing an existing file asks first."),
     "inputSchema": {"type": "object", "properties": {
         "file": FILE_ARG, "site": {"type": "string"}, "folder_path": {"type": "string"},
         "filename": {"type": "string", "description": "defaults to the file's own name"}},
         "required": ["file", "site"]}},
]
TOOLS = localise(TOOLS)


def _parse_sharepoint_get(text: str) -> tuple[str, str]:
    """(name, download_url) from the openclaw MCP's sharepoint_get Markdown result."""
    fields = dict(re.findall(r"^\*\*([a-z_]+):\*\*[ \t]*(.*)$", text or "", re.M))
    if "folder" in fields:
        raise ValueError("that link is a folder; give me a file inside it")
    if not fields.get("download_url"):
        raise ValueError(f"couldn't open that SharePoint link: {(text or '')[:200]}")
    return fields.get("name") or "file", fields["download_url"].strip()


class FileTools:
    in_process = True

    def __init__(self, store, *, attachments=None, sharepoint=None, jira=None, connector=None, mailer=None,
                 footer: str = "", internal_domains: set[str] | None = None, http: httpx.AsyncClient | None = None,
                 author: str = ""):
        self.store = store
        self.attachments = attachments  # AttachmentTools
        self.sharepoint = sharepoint    # MCP client for the openclaw server (sharepoint_get)
        self.jira = jira                # MCP client for the agent's Jira server (add_attachment)
        self.connector = connector      # TeamsConnector (send_activity)
        self.mailer = mailer            # GraphMailer for the agent's own mailbox
        self.gateway = None             # ToolGateway, set by the app: saves go through sharepoint_put's rule
        self.footer = footer
        self.author = author            # tracked changes and comments in Word are by this name
        self.internal = internal_domains or set(INTERNAL_DOMAINS)
        self.http = http or httpx.AsyncClient(timeout=120, follow_redirects=True)

    async def list_tools(self) -> list[dict]:
        return TOOLS

    # ---- the outbox: files the agent made, per conversation ----------------------------------
    async def _put_outbox(self, conv: str, name: str, data: bytes) -> str:
        fid = uuid.uuid4().hex[:12]
        await self.store.set_pref(f"outbox:{fid}", "data", {"name": name, "b64": base64.b64encode(data).decode(),
                                                             "conv": conv})
        now = datetime.now(timezone.utc)
        items = [i for i in await self.store.get_pref(conv, "outbox") or []
                 if datetime.fromisoformat(i["at"]) > now - timedelta(days=OUTBOX_DAYS)]
        items.append({"id": fid, "name": name, "size": len(data), "at": now.isoformat()})
        await self.store.set_pref(conv, "outbox", items[-20:])
        return f"outbox:{fid}"

    async def _outbox_bytes(self, fid: str, conv: str) -> tuple[bytes, str] | None:
        entry = await self.store.get_pref(f"outbox:{fid}", "data")
        if not entry or entry.get("conv") != conv:  # only the conversation that made it
            return None
        return base64.b64decode(entry["b64"]), entry["name"]

    async def outbox_files(self, conv: str) -> list[tuple[str, bytes]]:
        """Every file this conversation made that is still kept, as (name, bytes): attached to a background
        task's emailed result."""
        out = []
        for item in await self.store.get_pref(conv, "outbox") or []:
            if got := await self._outbox_bytes(item["id"], conv):
                out.append((got[1], got[0]))
        return out

    async def resolve(self, ref: str, conv: str) -> tuple[bytes, str, str]:
        """(bytes, file name, kind) for a file argument; kind is outbox | attachment | sharepoint."""
        ref = (ref or "").strip()
        if ref.startswith("outbox:"):
            got = await self._outbox_bytes(ref.removeprefix("outbox:"), conv)
            if got:
                return got[0], got[1], "outbox"
            raise ValueError("that file reference isn't from this conversation (or has expired); create it again")
        if SHAREPOINT_RE.match(ref):
            if not self.sharepoint:
                raise ValueError("SharePoint isn't available")
            text = await self.sharepoint.call_tool("sharepoint_get", {"url": ref, "max_inline_bytes": 0})
            name, url = _parse_sharepoint_get(text)
            r = await self.http.get(url)
            r.raise_for_status()
            return r.content, name, "sharepoint"
        wanted = ref.lower()
        for item in reversed(await self.store.get_pref(conv, "outbox") or []):
            if item["name"].lower() == wanted:
                got = await self._outbox_bytes(item["id"], conv)
                if got:
                    return got[0], got[1], "outbox"
        if self.attachments:
            files = await self.attachments.files(conv)
            match = next((f for f in reversed(files) if f["name"].lower() == wanted), None) or next(
                (f for f in reversed(files) if wanted and wanted in f["name"].lower()), None)
            if match:
                data, name = await self.attachments.fetch(match)
                return data, name, "attachment"
        raise ValueError(f"no file {ref!r} here: create it with create_file, or give a SharePoint link")

    # ---- tools ------------------------------------------------------------------------------
    async def call_tool(self, name: str, args: dict, ctx=None) -> str:
        conv = getattr(ctx, "conversation_id", None) or ""
        if getattr(ctx, "restricted", False) or not conv:
            return json.dumps({"error": "not available"})
        try:
            if name == "create_file":
                return await self._create(args, conv)
            if name == "edit_file":
                return await self._edit(args, ctx, conv)
            if name == "file_outline":
                return json.dumps(await self._outline(args, conv))
            if name == "preview_file":
                return await self._preview_tool(args, conv)
            if name == "insert_image":
                return json.dumps(await self._insert_image(args, conv))
            if name == "send_file":
                return json.dumps(await self._send_file(args, ctx, conv))
            if name == "attach_to_jira":
                return json.dumps(await self._attach_to_jira(args, conv))
            if name == "email_file":
                return json.dumps(await self._email_file(args, conv))
            if name == "save_file_to_sharepoint":
                return await self._save(args, ctx, conv)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        raise ValueError(f"unknown tool {name}")

    async def _create(self, args: dict, conv: str):
        filename, content = (args.get("filename") or "").strip(), args.get("content", "")
        images = {}
        if filename.lower().endswith((".docx", ".pptx")):
            refs = image_refs(content)
            if filename.lower().endswith(".pptx"):
                from .deck import image_refs_in_deck
                refs = image_refs_in_deck(content)
            for ref in refs:
                try:
                    data, name, _ = await self.resolve(ref, conv)
                    images[ref] = (name, data)
                except ValueError as exc:
                    return json.dumps({"error": f"image {ref!r}: {exc}"})
        data = await asyncio.to_thread(make_file, filename, content, images)
        fid = await self._put_outbox(conv, filename, data)
        result = {"file": fid, "name": filename, "size": len(data)}
        if images:
            result["figures"] = len(images)
        if filename.lower().endswith((".svg", ".png")):  # let the model see what it drew
            from .vector import svg_to_png
            preview = data if filename.lower().endswith(".png") else await asyncio.to_thread(
                svg_to_png, data.decode(), 1200)
            return {"text": json.dumps(result), "images": [("png", preview, f"preview of {filename}")]}
        if args.get("preview", True) and filename.lower().endswith((".docx", ".xlsx", ".pptx")):
            return await self._with_preview(result, data, filename, [1, 2, 3] if filename.lower().endswith(".pptx") else [1])
        return json.dumps(result)

    async def _with_preview(self, result: dict, data: bytes, name: str, pages):
        """The result plus page images, so the model checks the layout; text only when previews aren't here."""
        from . import office
        if not office.available():
            return json.dumps(result)
        try:
            shown, info = await asyncio.to_thread(office.render_pages, data, name, pages)
        except ValueError as exc:
            result["preview"] = f"not available: {exc}"
            return json.dumps(result)
        result["preview"] = {"pages": info.get("pages"), "showing": [label for _, _, label in shown],
                             "check": "fonts in the preview are open look-alikes of Georgia/Calibri/Consolas"}
        return {"text": json.dumps(result), "images": shown}

    async def _edit(self, args: dict, ctx, conv: str):
        data, name, _ = await self.resolve(args.get("file", ""), conv)
        edits = args.get("edits") or []
        if isinstance(edits, dict):
            edits = [edits]
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        author = self.author
        if ext in ("docx", "docm"):
            from .docbuild import edit_docx
            new, done = await asyncio.to_thread(edit_docx, data, edits, author, bool(args.get("track_changes")))
        elif ext in ("xlsx", "xlsm"):
            from .sheets import edit_xlsx
            new, done = await asyncio.to_thread(edit_xlsx, data, edits, bool(args.get("allow_losses")))
        elif ext == "pptx":
            from .deck import edit_deck
            images = {}
            for e in edits:
                for s in (e.get("slides") or []):
                    if isinstance(s, dict) and s.get("image"):
                        got = await self.resolve(s["image"], conv)
                        images[s["image"]] = (got[1], got[0])
            new, done = await asyncio.to_thread(edit_deck, data, edits, images)
        else:
            raise ValueError(f"I can edit .docx, .xlsx and .pptx files, not .{ext or '?'}")
        out_name = (args.get("filename") or name).strip()
        fid = await self._put_outbox(conv, out_name, new)
        result = {"file": fid, "name": out_name, "changes": done,
                  "next": "check the preview, then send it or save it with save_file_to_sharepoint (replacing the "
                          "original asks first)"}
        pages = args.get("preview_pages") or [1]
        return await self._with_preview(result, new, out_name, pages)

    async def _outline(self, args: dict, conv: str) -> dict:
        data, name, _ = await self.resolve(args.get("file", ""), conv)
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext in ("docx", "docm"):
            from .docbuild import outline
            out = await asyncio.to_thread(outline, data)
        elif ext in ("xlsx", "xlsm"):
            from .sheets import outline
            out = await asyncio.to_thread(outline, data)
        elif ext == "pptx":
            from .deck import deck_outline
            out = {"kind": "PowerPoint deck", "slides": await asyncio.to_thread(deck_outline, data)}
        else:
            raise ValueError(f"I can outline .docx, .xlsx and .pptx files, not .{ext or '?'}")
        return {"name": name, **out}

    async def _preview_tool(self, args: dict, conv: str):
        data, name, _ = await self.resolve(args.get("file", ""), conv)
        from .images import visual
        try:
            shown, info = await asyncio.to_thread(visual, data, name, args.get("pages"))
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        return {"text": json.dumps({"name": name, **info, "showing": [label for _, _, label in shown]}),
                "images": shown}

    async def _insert_image(self, args: dict, conv: str) -> dict:
        doc_data, doc_name, _ = await self.resolve(args.get("document", ""), conv)
        if args.get("svg"):
            from .vector import clean_svg
            img_data, img_name = clean_svg(args["svg"]).encode(), "figure.svg"
        elif args.get("image"):
            img_data, img_name, _ = await self.resolve(args["image"], conv)
        else:
            raise ValueError("give `image` (a file) or `svg` (markup)")
        out_name = (args.get("filename") or doc_name).strip()
        from .filemaker import insert_into_document
        data, where = await asyncio.to_thread(insert_into_document, doc_data, doc_name, img_data, img_name,
                                              (args.get("after") or "").strip(), (args.get("caption") or "").strip(),
                                              args.get("width_cm"))
        fid = await self._put_outbox(conv, out_name, data)
        return {"file": fid, "name": out_name, "placed": where,
                "next": "send it, or save it with save_file_to_sharepoint (replacing the original asks first)"}

    async def _send_file(self, args: dict, ctx, conv: str) -> dict:
        ref = await self.store.get_conversation_ref(conv)
        chat = conv
        actor = getattr(ctx, "actor", None)
        if not ref and actor and actor != "unknown":
            # On a call, in email or in a background task there's no chat here: use the person's own 1:1 chat.
            ref = await self.store.conversation_ref_for_user(actor)
            chat = ((ref or {}).get("conversation") or {}).get("id") or conv
        if not ref or not self.connector:
            return {"sent": False, "reason": ("I have no Teams chat with them yet; use email_file instead")}
        data, name, kind = await self.resolve(args.get("file", ""), conv)
        kind_of_chat = (ref.get("conversation") or {}).get("conversationType", "personal")
        if kind_of_chat != "personal":
            if kind == "sharepoint":
                await self.connector.send(ref, f"{(args.get('message') or '').strip()}\n\n📄 [{name}]({args['file'].strip()})".strip())
                return {"sent": True, "as": "link", "name": name}
            return {"sent": False, "reason": ("Teams doesn't let bots post files in group chats or channels. Save it "
                                              "with save_file_to_sharepoint and share the link here instead.")}
        if kind != "outbox":  # keep a copy: the consent may be accepted a while later
            fid = await self._put_outbox(conv, name, data)
        else:
            fid = args["file"].strip() if args["file"].strip().startswith("outbox:") else None
            if not fid:
                fid = await self._put_outbox(conv, name, data)
        token = uuid.uuid4().hex
        await self.store.set_pref("file-consent", token, {"file": fid, "name": name, "conv": conv, "chat": chat,
                                                          "message": (args.get("message") or "").strip()})
        await self.connector.send_activity(ref, {"type": "message", "attachments": [{
            "contentType": CONSENT, "name": name,
            "content": {"description": (args.get("message") or name)[:200], "sizeInBytes": len(data),
                        "acceptContext": {"token": token}, "declineContext": {"token": token}}}]})
        return {"sent": True, "as": "file (waiting for them to click Allow)", "name": name,
                "where": "this chat" if chat == conv else "their 1:1 Teams chat with you"}

    async def on_consent(self, activity: dict) -> None:
        """Teams invoke `fileConsent/invoke`: upload the file on Allow, or acknowledge a Decline."""
        value = activity.get("value") or {}
        token = ((value.get("context") or {}).get("token")) or ""
        pending = await self.store.get_pref("file-consent", token) if token else None
        if not pending:
            return
        await self.store.set_pref("file-consent", token, None)
        ref = await self.store.get_conversation_ref(pending.get("chat") or pending["conv"])
        if value.get("action") != "accept":
            if ref:
                await self.connector.send(ref, f"OK, I won't send {pending['name']}.")
            return
        data, name = await self._outbox_bytes(pending["file"].removeprefix("outbox:"), pending["conv"]) or (None, None)
        upload = value.get("uploadInfo") or {}
        if not data or not upload.get("uploadUrl"):
            if ref:
                await self.connector.send(ref, f"Sorry, I couldn't send {pending['name']} (it expired). Ask me again.")
            return
        r = await self.http.put(upload["uploadUrl"], content=data, headers={
            "Content-Length": str(len(data)), "Content-Range": f"bytes 0-{len(data) - 1}/{len(data)}"})
        r.raise_for_status()
        if ref:
            await self.connector.send_activity(ref, {"type": "message", "attachments": [{
                "contentType": FILE_INFO, "contentUrl": upload.get("contentUrl"), "name": upload.get("name") or name,
                "content": {"uniqueId": upload.get("uniqueId"), "fileType": upload.get("fileType")}}]})
        await self.store.audit({"actor": (activity.get("from") or {}).get("aadObjectId"), "action": "file_sent",
                                "target": pending["conv"], "detail": {"name": name, "size": len(data)}})

    async def _attach_to_jira(self, args: dict, conv: str) -> dict:
        if not self.jira:
            return {"attached": False, "reason": "Jira isn't available"}
        data, name, _ = await self.resolve(args.get("file", ""), conv)
        out = await self.jira.call_tool("add_attachment", {"key": args["issue_key"].strip().upper(), "filename": name,
                                                            "content_base64": base64.b64encode(data).decode()})
        result = json.loads(out) if out.strip().startswith("{") else {"result": out}
        return {"attached": True, **result}

    async def _email_file(self, args: dict, conv: str) -> dict:
        if not self.mailer:
            return {"sent": False, "reason": "email isn't available"}
        to = [a.strip() for a in args.get("to") or [] if a and a.strip()]
        cc = [a.strip() for a in args.get("cc") or [] if a and a.strip()]
        bad = [a for a in to + cc if not ADDRESS_RE.match(a) or ADDRESS_RE.match(a).group(1).lower() not in self.internal]
        if not to or bad:
            return {"sent": False, "reason": (f"{', '.join(bad)} isn't a {org().name} address: attachments go to {org().name} people only. "
                                              "Use exact addresses from find_person." if bad else "no recipients")}
        data, name, _ = await self.resolve(args.get("file", ""), conv)
        if len(data) > MAX_EMAIL_BYTES:
            return {"sent": False, "reason": f"{name} is over 3 MB: save it to SharePoint and email the link"}
        await self.mailer.send(to, args.get("subject", "").strip() or name, args.get("body", ""), self.footer,
                               attachments=[(name, data)], cc=cc)
        return {"sent": True, "to": to, **({"cc": cc} if cc else {}), "attachment": name}

    async def _save(self, args: dict, ctx, conv: str) -> str:
        if not self.gateway:
            return json.dumps({"error": "SharePoint isn't available"})
        data, name, _ = await self.resolve(args.get("file", ""), conv)
        put = {"site": args.get("site", ""), "folder_path": args.get("folder_path", ""),
               "filename": (args.get("filename") or name).strip(), "content_base64": base64.b64encode(data).decode()}
        outcome = await self.gateway.call("openclaw__sharepoint_put", put, ctx, True)
        if outcome.status == "needs_confirm":  # remember it so a "yes" replaces the file
            await self.store.put_pending(conv, "openclaw__sharepoint_put", put)
        return json.dumps({"status": outcome.status, "result": outcome.content[:1500]})
