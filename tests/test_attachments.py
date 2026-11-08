import json

import httpx

from agentcore.attachments import AttachmentTools, file_attachments, remember_attachments
from agentcore.store import MemoryStore
from agentcore.teams import message_text
from agentcore.tools import CallContext

from .test_documents import docx_bytes

FILE = {"contentType": "application/vnd.microsoft.teams.file.download.info", "name": "brief.docx",
        "content": {"downloadUrl": "https://dl.sharepoint.com/brief.docx?tempauth=SECRET", "uniqueId": "u1", "fileType": "docx"}}
IMAGE = {"contentType": "image/png", "contentUrl": "https://smba.trafficmanager.net/au/v3/attachments/a1/views/original", "name": "shot.png"}


def test_attached_files_are_announced_without_their_secret_links():
    text = message_text({"text": "please review", "attachments": [FILE]}, "bot")
    assert "brief.docx" in text and "read_attachment" in text and "tempauth" not in text


async def test_read_attachment_downloads_and_converts_the_latest_file():
    store = MemoryStore()
    await remember_attachments(store, "c1", file_attachments({"attachments": [FILE, IMAGE]}))
    seen = []

    def handler(req):
        seen.append((str(req.url), req.headers.get("authorization")))
        return httpx.Response(200, content=docx_bytes(["Scope: 2 x 1.5 MVA"]))

    async def bot_token():
        return "bot-token"
    tools = AttachmentTools(store, bot_token=bot_token, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    out = json.loads(await tools.call_tool("read_attachment", {"name": "brief.docx"}, CallContext("c1", "aad-a", [])))
    assert out["name"] == "brief.docx" and "1.5 MVA" in out["text"]
    assert seen[0][1] is None  # pre-authenticated SharePoint link: no bot token sent
    listed = json.loads(await tools.call_tool("list_attachments", {}, CallContext("c1", "aad-a", [])))
    assert [a["name"] for a in listed["attachments"]] == ["brief.docx", "shot.png"]
    # another conversation can't read this one's files
    other = json.loads(await tools.call_tool("read_attachment", {}, CallContext("c2", "aad-b", [])))
    assert other.get("found") is False


async def test_bot_framework_attachment_urls_get_the_bot_token():
    store = MemoryStore()
    pdf = {"contentType": "application/pdf", "contentUrl": "https://smba.trafficmanager.net/au/v3/attachments/p1/views/original", "name": "spec.pdf"}
    await remember_attachments(store, "c3", file_attachments({"attachments": [pdf]}))
    seen = []

    def handler(req):
        seen.append(req.headers.get("authorization"))
        return httpx.Response(200, content=b"%PDF-1.4 not really")

    async def bot_token():
        return "bot-token"
    tools = AttachmentTools(store, bot_token=bot_token, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await tools.call_tool("read_attachment", {}, CallContext("c3", "a", []))
    assert seen == ["Bearer bot-token"]


async def test_untrusted_hosts_are_never_fetched():
    store = MemoryStore()
    evil = {"contentType": "application/pdf", "contentUrl": "http://169.254.169.254/latest/meta-data", "name": "x.pdf"}
    assert file_attachments({"attachments": [evil]}) == []


def test_every_agent_config_has_well_formed_tool_server_urls():
    import re
    from agentcore.config import load_agent_config
    from .conftest import AGENTS
    for agent in ("ada", "eve", "dan"):
        cfg = load_agent_config(AGENTS / agent / "agent.yaml")
        for name, server in cfg.mcp_servers.items():
            assert server.url_env or re.fullmatch(r"(inprocess://[a-z]+|https?://[\w.\-:/]+)", server.url or ""), (agent, name)


class FakeJira:
    def __init__(self, files):
        self.files, self.calls = files, []

    async def call_tool(self, name, args):
        import base64
        self.calls.append((name, args))
        match = next((f for f in self.files if not args.get("name") or args["name"].lower() in f["filename"].lower()), None)
        if not match:
            raise RuntimeError(f"Error executing tool get_attachment: {args['key']} has no attachment {args.get('name')!r}")
        return json.dumps({"filename": match["filename"], "type": match["type"],
                           "content_base64": base64.b64encode(match["data"]).decode()})


async def test_files_on_a_jira_ticket_can_be_read_and_viewed():
    from .test_images import noisy_png as png_bytes
    jira = FakeJira([{"filename": "scope.docx", "type": "application/vnd.openxmlformats", "data": docx_bytes(["Scope: 2 x 1.5 MVA"])},
                     {"filename": "image-20261007.png", "type": "image/png", "data": png_bytes()}])
    tools = AttachmentTools(MemoryStore(), jira=jira)
    ctx = CallContext("jira:PRJ-371", "aad-a", [])
    out = json.loads(await tools.call_tool("read_attachment", {"issue": "prj-371", "name": "scope.docx"}, ctx))
    assert "1.5 MVA" in out["text"] and out["issue"] == "PRJ-371"
    # scanned pages and drawings aren't in the text: the result says how to look at them (PRJ-349, 13 Oct 2026)
    assert "view_attachment" in out["visual"] and "PRJ-371" in out["visual"]
    assert jira.calls[0] == ("get_attachment", {"key": "PRJ-371", "name": "scope.docx"})
    seen = await tools.call_tool("view_attachment", {"issue": "PRJ-371", "name": "image-20261007.png"}, ctx)
    assert seen["images"] and json.loads(seen["text"])["name"] == "image-20261007.png"
    # an image read with read_attachment is shown, not converted to text
    assert (await tools.call_tool("read_attachment", {"issue": "PRJ-371", "name": "image"}, ctx))["images"]
    missing = json.loads(await tools.call_tool("read_attachment", {"issue": "PRJ-371", "name": "x.dwg"}, ctx))
    assert "has no attachment" in missing["error"] and "Error executing" not in missing["error"]
    restricted = CallContext("phone:1", None, [], restricted=True)
    assert "can't be opened" in json.loads(await tools.call_tool("read_attachment", {"issue": "PRJ-371"}, restricted))["error"]
