import base64
import json

import httpx
import pytest

from agentcore.documents import document_text
from agentcore.filemaker import make_file
from agentcore.files import CONSENT, FILE_INFO, FileTools
from agentcore.store import MemoryStore
from agentcore.tools import CallContext

PLAN = "# 1P Dismantling Plan\n\n## Stage 1\n\n- Isolate the **UPS**\n- Remove PDUs\n\n| Item | Owner |\n|---|---|\n| UPS | Sam |\n"


def test_docx_is_a_real_word_document_with_structure():
    data = make_file("plan.docx", PLAN)
    text = document_text(data, "plan.docx")
    assert "1P Dismantling Plan" in text and "Isolate the UPS" in text and "Sam" in text
    from docx import Document
    import io
    d = Document(io.BytesIO(data))
    assert [p.style.name for p in d.paragraphs if p.text == "Stage 1"] == ["Heading 1"]  # "# title" is on the cover
    assert any(p.style.name == "List Bullet" for p in d.paragraphs)
    assert len(d.tables) == 2  # the Acme title block, then the plan's table
    assert d.core_properties.title == "1P Dismantling Plan"


def test_xlsx_from_table_and_text_files_and_refusals():
    from openpyxl import load_workbook
    import io
    ws = load_workbook(io.BytesIO(make_file("costs.xlsx", "| Item | Cost |\n|---|---|\n| Crane | 4500 |"))).active
    assert [c.value for c in ws[1]] == ["Item", "Cost"] and ws["B2"].value == 4500 and ws["A1"].font.bold
    assert make_file("notes.md", "hi") == b"hi"
    for bad in ("evil.exe", "../x.md", ".hidden", "photo.png"):
        with pytest.raises(ValueError):
            make_file(bad, "x")


class FakeConnector:
    def __init__(self):
        self.activities, self.texts = [], []

    async def send_activity(self, ref, activity):
        self.activities.append(activity)

    async def send(self, ref, text, reply_to_id=None):
        self.texts.append(text)


class FakeMcp:
    def __init__(self, reply="{}"):
        self.calls, self.reply = [], reply

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        return self.reply


class FakeMailer:
    def __init__(self):
        self.sent = []

    async def send(self, to, subject, body, footer, attachments=None, cc=None):
        self.sent.append((to, subject, attachments))


def ctx(conv="chat-a"):
    return CallContext(conv, "aad-alex", ["send me the plan"], actor_name="Alex Owner")


async def _personal(store, conv="chat-a", kind="personal"):
    await store.upsert_conversation_ref(conv, {"serviceUrl": "https://smba.example/", "conversation": {
        "id": conv, "conversationType": kind}, "bot": {"id": "28:bot"}}, "aad-alex")


async def test_send_file_in_a_1to1_chat_uses_consent_then_uploads_on_allow():
    store, conn = MemoryStore(), FakeConnector()
    await _personal(store)
    puts = []

    def handler(req):
        puts.append((str(req.url), req.headers.get("content-range"), len(req.content)))
        return httpx.Response(201, json={})
    files = FileTools(store, connector=conn, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    made = json.loads(await files.call_tool("create_file", {"filename": "plan.docx", "content": PLAN}, ctx()))
    sent = json.loads(await files.call_tool("send_file", {"file": made["file"], "message": "Here's the plan"}, ctx()))
    assert sent["sent"] is True
    card = conn.activities[0]["attachments"][0]
    assert card["contentType"] == CONSENT and card["name"] == "plan.docx" and card["content"]["sizeInBytes"] == made["size"]
    token = card["content"]["acceptContext"]["token"]
    await files.on_consent({"value": {"type": "fileUpload", "action": "accept", "context": {"token": token},
                                      "uploadInfo": {"name": "plan.docx", "uploadUrl": "https://onedrive.example/up",
                                                     "contentUrl": "https://onedrive.example/plan.docx",
                                                     "uniqueId": "u1", "fileType": "docx"}}, "from": {"aadObjectId": "aad-alex"}})
    assert puts == [("https://onedrive.example/up", f"bytes 0-{made['size'] - 1}/{made['size']}", made["size"])]
    info = conn.activities[1]["attachments"][0]
    assert info["contentType"] == FILE_INFO and info["contentUrl"] == "https://onedrive.example/plan.docx"
    await files.on_consent({"value": {"action": "accept", "context": {"token": token}, "uploadInfo": {}}})
    assert len(puts) == 1  # a token is used once


async def test_decline_is_acknowledged_and_group_chats_get_links_not_files():
    store, conn = MemoryStore(), FakeConnector()
    await _personal(store)
    files = FileTools(store, connector=conn)
    made = json.loads(await files.call_tool("create_file", {"filename": "a.md", "content": "x"}, ctx()))
    await files.call_tool("send_file", {"file": made["file"]}, ctx())
    token = conn.activities[0]["attachments"][0]["content"]["declineContext"]["token"]
    await files.on_consent({"value": {"action": "decline", "context": {"token": token}}})
    assert "won't send a.md" in conn.texts[-1]
    await _personal(store, "group-1", "groupChat")
    made = json.loads(await files.call_tool("create_file", {"filename": "b.md", "content": "x"}, ctx("group-1")))
    out = json.loads(await files.call_tool("send_file", {"file": made["file"]}, ctx("group-1")))
    assert out["sent"] is False and "save_file_to_sharepoint" in out["reason"]


async def test_outbox_files_stay_in_their_own_conversation():
    store = MemoryStore()
    files = FileTools(store)
    made = json.loads(await files.call_tool("create_file", {"filename": "secret.md", "content": "x"}, ctx("chat-a")))
    with pytest.raises(ValueError):
        await files.resolve(made["file"], "chat-b")


async def test_attach_to_jira_sends_the_bytes_not_the_model():
    store, jira = MemoryStore(), FakeMcp('{"key": "PRJ-328", "attachment_id": "10001"}')
    files = FileTools(store, jira=jira)
    made = json.loads(await files.call_tool("create_file", {"filename": "plan.docx", "content": PLAN}, ctx()))
    out = json.loads(await files.call_tool("attach_to_jira", {"issue_key": "prj-328", "file": "plan.docx"}, ctx()))
    assert out["attached"] is True and out["attachment_id"] == "10001"
    name, args = jira.calls[0]
    assert name == "add_attachment" and args["key"] == "PRJ-328" and args["filename"] == "plan.docx"
    assert len(base64.b64decode(args["content_base64"])) == made["size"]


async def test_email_file_is_internal_only_and_size_capped():
    store, mailer = MemoryStore(), FakeMailer()
    files = FileTools(store, mailer=mailer)
    made = json.loads(await files.call_tool("create_file", {"filename": "plan.md", "content": "x"}, ctx()))
    ok = json.loads(await files.call_tool("email_file", {"to": ["Sam.Taylor@acme.example"], "subject": "Plan",
                                                         "body": "**Attached**", "file": made["file"]}, ctx()))
    assert ok["sent"] is True and mailer.sent[0][2] == [("plan.md", b"x")]
    out = json.loads(await files.call_tool("email_file", {"to": ["someone@gmail.com"], "subject": "x", "body": "x",
                                                          "file": made["file"]}, ctx()))
    assert out["sent"] is False and len(mailer.sent) == 1
    cc_out = json.loads(await files.call_tool("email_file", {"to": ["sam.taylor@acme.example"], "subject": "x",
                                                             "body": "x", "file": made["file"],
                                                             "cc": ["someone@gmail.com"]}, ctx()))
    assert cc_out["sent"] is False and "someone@gmail.com" in cc_out["reason"] and len(mailer.sent) == 1


async def test_email_file_copies_the_requestor():
    store = MemoryStore()

    class CcMailer(FakeMailer):
        async def send(self, to, subject, body, footer, attachments=None, cc=None):
            self.sent.append((to, cc))
    mailer = CcMailer()
    files = FileTools(store, mailer=mailer)
    made = json.loads(await files.call_tool("create_file", {"filename": "plan.md", "content": "x"}, ctx()))
    ok = json.loads(await files.call_tool("email_file", {
        "to": ["sam.taylor@acme.example", "noah@acme.example"], "cc": ["alex.owner@acme.example"], "subject": "Plan",
        "body": "x", "file": made["file"]}, ctx()))
    assert ok["cc"] == ["alex.owner@acme.example"]
    assert mailer.sent == [(["sam.taylor@acme.example", "noah@acme.example"], ["alex.owner@acme.example"])]


async def test_sharepoint_links_and_received_attachments_can_be_forwarded():
    store = MemoryStore()
    sp = FakeMcp("**name:** Plan.docx\n**size:** 3 bytes\n**download_url:** https://dl.example/plan\n")

    def handler(req):
        return httpx.Response(200, content=b"doc")
    files = FileTools(store, sharepoint=sp, jira=FakeMcp('{"key": "X-1"}'),
                      http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    data, name, kind = await files.resolve("https://acme.sharepoint.com/:w:/s/3.0Projects/E1", "chat-a")
    assert (data, name, kind) == (b"doc", "Plan.docx", "sharepoint")
    assert sp.calls[0] == ("sharepoint_get", {"url": "https://acme.sharepoint.com/:w:/s/3.0Projects/E1", "max_inline_bytes": 0})

    class Attachments:
        async def files(self, conv):
            return [{"name": "SLD-rev3.pdf", "source": "mail"}]

        async def fetch(self, match):
            return b"%PDF", match["name"]
    files.attachments = Attachments()
    assert (await files.resolve("SLD-rev3.pdf", "chat-a"))[:2] == (b"%PDF", "SLD-rev3.pdf")


async def test_saving_to_sharepoint_goes_through_the_site_rule(cfg):
    from .test_tools import FakeSharePoint, _ada_gateway
    store, sp = MemoryStore(), FakeSharePoint()
    gw = await _ada_gateway(cfg, sp)
    files = FileTools(store)
    files.gateway = gw
    made = json.loads(await files.call_tool("create_file", {"filename": "plan.docx", "content": PLAN}, ctx()))
    ok = json.loads(await files.call_tool("save_file_to_sharepoint", {"file": made["file"], "site": "3.0 Projects",
                                                                      "folder_path": "PRJ"}, ctx()))
    assert ok["status"] == "ok" and sp.calls[-1][1]["filename"] == "plan.docx" and sp.calls[-1][1]["overwrite"] is False
    no = json.loads(await files.call_tool("save_file_to_sharepoint", {"file": made["file"], "site": "2.0 Strategy"}, ctx()))
    assert no["status"] == "blocked"


async def test_edit_outline_and_preview_tools_on_word_and_excel(monkeypatch):
    from agentcore import office
    store = MemoryStore()
    tools = FileTools(store, author="Ada (AI)")
    ctx = CallContext("conv-edit", "aad-1", [])
    made = json.loads(await tools.call_tool("create_file", {"filename": "plan.docx", "content": PLAN, "preview": False}, ctx))
    out = await tools.call_tool("edit_file", {"file": made["file"], "track_changes": True, "edits": [
        {"op": "replace_text", "find": "Remove PDUs", "replace": "Remove PDUs and racks"},
        {"op": "comment", "on": "Isolate the", "text": "Who isolates?"}]}, ctx)
    result = json.loads(out["text"]) if isinstance(out, dict) else json.loads(out)
    assert result["name"] == "plan.docx" and len(result["changes"]) == 2 and result["file"] != made["file"]
    outline = json.loads(await tools.call_tool("file_outline", {"file": result["file"]}, ctx))
    assert outline["tracked_changes"] > 0 and outline["comments"][0]["author"] == "Ada (AI)"
    sheet = json.loads(await tools.call_tool("create_file", {"filename": "c.xlsx", "content": "a,b\n1,2", "preview": False}, ctx))
    edited = await tools.call_tool("edit_file", {"file": sheet["file"], "edits": [{"op": "set_cells", "cells": {"B2": 5}}]}, ctx)
    assert "set 1 cell" in (edited["text"] if isinstance(edited, dict) else edited)
    bad = json.loads(await tools.call_tool("edit_file", {"file": made["file"], "edits": [{"op": "nope"}]}, ctx))
    assert "unknown op" in bad["error"]
    monkeypatch.setattr(office, "available", lambda: False)  # no LibreOffice: a create still works, no preview
    plain = await tools.call_tool("create_file", {"filename": "p.docx", "content": PLAN}, ctx)
    assert isinstance(plain, str) and "preview" not in json.loads(plain)


async def test_create_and_edit_a_deck_through_the_file_tools():
    tools = FileTools(MemoryStore())
    ctx = CallContext("conv-deck", "aad-1", [])
    made = json.loads(await tools.call_tool("create_file", {"filename": "update.pptx", "preview": False, "content":
        "# PRJ update\nOctober 2026\n## Headlines\n- On track\n- Offer due Nov\n## Questions?\n"}, ctx))
    assert made["name"] == "update.pptx" and made["size"] > 10_000
    outline = json.loads(await tools.call_tool("file_outline", {"file": made["file"]}, ctx))
    assert any(s["title"] == "Headlines" for s in outline["slides"])
    out = await tools.call_tool("edit_file", {"file": made["file"], "edits": [
        {"op": "replace_text", "find": "On track", "replace": "Ahead of plan"}]}, ctx)
    assert "Ahead of plan" in (out["text"] if isinstance(out, dict) else out)
