import pytest


class FakeSharePoint:
    """Stands in for the openclaw MCP: files that exist, and what each call received."""
    def __init__(self, existing=()):
        self.existing, self.calls = set(existing), []

    async def list_tools(self):
        return [{"name": n, "description": n, "inputSchema": {"type": "object"}}
                for n in ("sharepoint_get", "sharepoint_put", "sharepoint_move", "sharepoint_delete")]

    async def call_tool(self, name, args):
        self.calls.append((name, dict(args)))
        if name == "sharepoint_put":
            key = f"{args.get('folder_path', '')}/{args['filename']}"
            if key in self.existing and args.get("overwrite", True) is False:
                return "Error uploading to SharePoint: File already exists and overwrite is disabled."
            self.existing.add(key)
            return '{"webUrl": "https://acme.sharepoint.com/sites/3.0Projects/x"}'
        return "ok"


async def _ada_gateway(cfg, sp):
    from agentcore.skills import SkillLoader
    from agentcore.tools import ToolGateway
    from .conftest import SKILLS
    gw = ToolGateway(cfg, SkillLoader(SKILLS, "ada", [], []), {"openclaw": sp})
    await gw.discover()
    return gw


async def test_ada_writes_new_files_only_in_allowed_sites(cfg):
    from agentcore.tools import CallContext
    sp = FakeSharePoint()
    gw = await _ada_gateway(cfg, sp)
    ctx = CallContext("c", "aad-alex", ["save the minutes"], actor_name="Alex Owner")
    ok = await gw.call("openclaw__sharepoint_put", {"site": "3.0Projects", "folder_path": "PRJ/Minutes",
                                                    "filename": "2026-10-06.md", "content_base64": "eA=="}, ctx, True)
    assert ok.status == "ok" and sp.calls[-1][1]["overwrite"] is False  # never replaces silently
    for site in ("2.0 Strategy", "https://acme.sharepoint.com/sites/Finance"):
        no = await gw.call("openclaw__sharepoint_put", {"site": site, "filename": "x.md", "content_base64": "eA=="}, ctx, True)
        assert no.status == "blocked" and "3.0 Projects" in no.content
    assert len(sp.calls) == 1
    url_form = await gw.call("openclaw__sharepoint_put", {"site": "https://acme.sharepoint.com/sites/3.0Projects/",
                                                          "filename": "y.md", "content_base64": "eA=="}, ctx, True)
    assert url_form.status == "ok"
    ops = await gw.call("openclaw__sharepoint_put", {"site": "https://acme.sharepoint.com/sites/4.0Operations/",
                                                     "filename": "z.md", "content_base64": "eA=="}, ctx, True)
    assert ops.status == "ok"  # 4.0 Operations added 12 Oct 2026


async def test_replacing_a_file_needs_a_yes_then_replaces(cfg):
    from agentcore.tools import CallContext
    sp = FakeSharePoint(existing={"PRJ/Plan.md"})
    gw = await _ada_gateway(cfg, sp)
    args = {"site": "3.0 Projects", "folder_path": "PRJ", "filename": "Plan.md", "content_base64": "eA=="}
    ctx = CallContext("c", "aad-alex", ["update the plan"], actor_name="Alex Owner")
    first = await gw.call("openclaw__sharepoint_put", args, ctx, True)
    assert first.status == "needs_confirm" and "already exists" in first.content
    ctx.confirmed = True
    second = await gw.call("openclaw__sharepoint_put", args, ctx, True)
    assert second.status == "ok" and "overwrite" not in sp.calls[-1][1]  # the server's default: replace


async def test_move_and_delete_always_need_a_yes_and_stay_in_allowed_sites(cfg):
    from agentcore.tools import CallContext
    sp = FakeSharePoint()
    gw = await _ada_gateway(cfg, sp)
    ctx = CallContext("c", "aad-alex", ["tidy up"], actor_name="Alex Owner")
    move = {"src_site": "3.0 Projects", "src_path": "a.md", "dest_site": "3.0 Projects", "dest_path": "old/a.md"}
    assert (await gw.call("openclaw__sharepoint_move", move, ctx, True)).status == "needs_confirm"
    out_of_scope = {**move, "dest_site": "2.0 Strategy"}
    assert (await gw.call("openclaw__sharepoint_move", out_of_scope, ctx, True)).status == "blocked"
    assert (await gw.call("openclaw__sharepoint_delete", {"site": "3.0 Projects", "path": "a.md"}, ctx, True)).status == "needs_confirm"
    ctx.confirmed = True
    assert (await gw.call("openclaw__sharepoint_delete", {"site": "3.0 Projects", "path": "a.md"}, ctx, True)).status == "ok"
    assert (await gw.call("openclaw__sharepoint_delete", {"site": "Finance", "path": "a.md"}, ctx, True)).status == "blocked"
    assert [c[0] for c in sp.calls] == ["sharepoint_delete"]


async def test_sharepoint_writes_stop_with_the_kill_switch(cfg):
    from agentcore.tools import CallContext
    gw = await _ada_gateway(cfg, FakeSharePoint())
    ctx = CallContext("c", "aad-alex", [], actor_name="Alex Owner")
    out = await gw.call("openclaw__sharepoint_put", {"site": "3.0 Projects", "filename": "x.md"}, ctx, False)
    assert out.status == "blocked" and "kill switch" in out.content


async def test_an_unreachable_mcp_server_fails_cleanly_and_never_breaks_startup():
    """6 Oct 2026: the openclaw MCP key was rotated; the half-open session left behind by the 401 later blew up
    in another task and Ada crash-looped. A failed connection must close itself, here, every time."""
    import anyio
    from agentcore.tools import StreamableHttpMcpClient
    client = StreamableHttpMcpClient("http://127.0.0.1:9/mcp")  # nothing listens on the discard port
    async with anyio.create_task_group():  # stands in for the app's lifespan
        for _ in range(2):
            try:
                await client.list_tools()
            except Exception:
                pass
            assert client._stack is None and client._session is None
        with pytest.raises(Exception):
            await client.call_tool("anything", {})
        assert client._stack is None
    await client.close()  # idempotent


async def test_the_model_sees_its_own_site_limits_in_the_tool_description(cfg):
    gw = await _ada_gateway(cfg, FakeSharePoint())
    put = gw.specs["openclaw__sharepoint_put"].description
    assert "only use this on these SharePoint sites: 3.0 Projects, 4.0 Operations" in put and "replacing an existing file asks" in put
    assert "asks the person for a yes first" in gw.specs["openclaw__sharepoint_delete"].description
    assert "SharePoint sites" not in gw.specs["openclaw__sharepoint_get"].description


async def test_mcp_client_accepts_events_big_enough_for_a_25_mb_attachment(monkeypatch):
    """A 1 MB PDF on PRJ-349 failed (13 Oct 2026): get_attachment's base64 reply is one SSE event, and the MCP
    client's default cap is 1 MiB, so every Jira file over ~750 KB was lost."""
    import mcp.client.streamable_http as sh
    from agentcore.documents import MAX_DOWNLOAD_BYTES
    from agentcore.tools import StreamableHttpMcpClient
    seen = {}

    def fake(url, **kwargs):
        seen.update(kwargs)
        raise ConnectionError("stop here")
    monkeypatch.setattr(sh, "streamable_http_client", fake)
    with pytest.raises(ConnectionError):
        await StreamableHttpMcpClient("http://x/mcp")._ensure()
    assert seen["max_sse_event_size"] > MAX_DOWNLOAD_BYTES * 4 / 3 + 64 * 1024  # base64 plus the JSON around it


async def test_an_unreachable_mcp_server_is_an_error_not_a_cancellation():
    """A server that can't be reached made the MCP library cancel its own scope, and the CancelledError got past
    every `except Exception`: an agent whose MCP pod was down couldn't start (found 30 Oct 2026)."""
    import asyncio
    from agentcore.tools import StreamableHttpMcpClient
    for url in ("http://127.0.0.1:1/mcp/", "http://no-such-host.invalid/mcp/"):
        client = StreamableHttpMcpClient(url)
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(client.list_tools(), 60)
        assert client._stack is None and client._session is None


async def test_discovery_skips_an_unreachable_server(cfg):
    from agentcore.tools import StreamableHttpMcpClient
    gw = await _ada_gateway(cfg, StreamableHttpMcpClient("http://127.0.0.1:1/mcp/"))
    assert not any(n.startswith("openclaw__") for n in gw.specs)


async def test_a_real_cancellation_still_cancels():
    import asyncio
    from agentcore.tools import StreamableHttpMcpClient

    async def hang(*a, **k):
        await asyncio.sleep(3600)
    client = StreamableHttpMcpClient("http://x/mcp")
    task = asyncio.create_task(client.list_tools())
    await asyncio.sleep(0)
    client._ensure = hang
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_an_in_process_tool_that_returns_an_error_is_audited_as_an_error(cfg):
    """publish_plan got 413 from the openclaw MCP for three days (27-30 Oct 2026) and said so to the agent, but the
    audit log showed "ok", so nobody saw it."""
    import json
    from agentcore.tools import CallContext

    class Plans:
        in_process = True

        async def list_tools(self):
            return [{"name": "publish_plan", "description": "", "inputSchema": {"type": "object"}}]

        async def call_tool(self, name, args, ctx):
            return json.dumps({"error": "publishing failed: Payload Too Large"}) if args.get("fail") else '{"url": "u"}'
    gw = await _ada_gateway(cfg, FakeSharePoint())
    gw.clients["plan"] = Plans()
    await gw.discover()
    ctx = CallContext(conversation_id="test-c", actor="test-u")
    assert (await gw.call("plan__publish_plan", {"fail": True}, ctx, True)).status == "error"
    assert (await gw.call("plan__publish_plan", {}, ctx, True)).status == "ok"


async def test_a_stale_mcp_session_reconnects_and_resends_once():
    """openclaw-mcp redeployed on 12 Oct 2026: Ada's open session was unknown to the new pod."""
    from agentcore.tools import StreamableHttpMcpClient

    class Session:
        def __init__(self, error=None):
            self.error, self.calls = error, 0

        async def call_tool(self, name, args):
            self.calls += 1
            if self.error:
                raise self.error
            return type("R", (), {"content": [type("C", (), {"text": "Uploaded successfully."})()], "isError": False})()

    client = StreamableHttpMcpClient("http://x/mcp")
    sessions = [Session(RuntimeError("Session terminated")), Session()]

    async def ensure():
        client._session = client._session or sessions.pop(0)
        return client._session
    client._ensure = ensure
    assert await client.call_tool("sharepoint_put", {}) == "Uploaded successfully."
    assert sessions == []

    client._session = Session(ValueError("bad argument"))  # anything else is not resent
    with pytest.raises(ValueError):
        await client.call_tool("sharepoint_put", {})


def test_path_rules_limit_a_writer_to_named_files_and_folders():
    from agentcore.config import ToolRule
    from agentcore.tools import site_allowed
    rule = ToolRule(server="openclaw", name="sharepoint_put", writes=True, sites=["3.0 Projects"], paths=[
        "1. Project Delivery/PRJ-1/01 Concept Design/Concept A/PRJ-GEN-REG-0005_*.xlsx",
        "1. Project Delivery/PRJ-1/*/Working/*"])
    ok = {"site": "3.0 Projects", "folder_path": "1. Project Delivery/PRJ-1/01 Concept Design/Concept A",
          "filename": "PRJ-GEN-REG-0005_B_PRJ-1-project-document-register.xlsx"}
    assert site_allowed(rule, ok) is None
    assert site_allowed(rule, {"site": "3.0 Projects", "folder_path": "1. Project Delivery/PRJ-1/St2 Concept Design/MEC Mechanical/Working/PRJ-MEC-BOD-0001",
                               "filename": "x.docx"}) is None
    assert "outside what you may write" in site_allowed(rule, {**ok, "filename": "PRJ-GEN-RPT-0001_E_brief.docx"})
    assert "outside what you may write" in site_allowed(rule, {"site": "3.0 Projects", "folder_path": "1. Project Delivery/PRJ-1/01 Concept Design/Concept A/Approved",
                                                              "filename": "x.docx"})
    assert "not a site you may write to" in site_allowed(rule, {**ok, "site": "4.0 Operations"})
