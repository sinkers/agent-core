"""`confluence` MCP server: Confluence Cloud as one agent's Atlassian service account.

Uses the v2 REST API (v1 content endpoints are retired) plus v1 CQL search. Bodies are written
as Atlassian Document Format via the same converter as the jira server, so checklists and
links render. Editing an existing page is a separate tool, `update_page`, which the agent's
allow-list tags `confirm` (no silent overwrites). It appends by default.

Environment: CONFLUENCE_CLIENT_ID, CONFLUENCE_CLIENT_SECRET, ATLASSIAN_CLOUD_ID,
ATLASSIAN_SITE_URL, MCP_SERVER_TOKEN, PORT.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
from typing import Any

import httpx

from agentcore_mcp_jira.adf import to_adf, to_text
from agentcore_mcp_jira.client import JiraClient, JiraError
from agentcore.org import org

log = logging.getLogger(__name__)


class ConfluenceClient(JiraClient):
    """Same OAuth client-credentials flow as Jira; different API base."""

    def __init__(self, client_id, client_secret, cloud_id, site_url, http=None):
        super().__init__(client_id, client_secret, cloud_id, site_url, http)
        self.base = f"https://api.atlassian.com/ex/confluence/{cloud_id}/wiki"

    def page_url(self, webui: str | None, page_id: str) -> str:
        return f"{self.site_url}/wiki{webui}" if webui else f"{self.site_url}/wiki/pages/viewpage.action?pageId={page_id}"


def _doc(value: str | dict) -> dict:
    if isinstance(value, dict):
        return value
    s = value.strip()
    if s.startswith("{"):
        try:
            d = json.loads(s)
            if d.get("type") == "doc":
                return d
        except ValueError:
            pass
    return to_adf(value)


class ConfluenceTools:
    def __init__(self, c: ConfluenceClient):
        self.c = c

    async def search_pages(self, query: str, space_key: str | None = None, limit: int = 10) -> dict:
        """Full-text search. `query` is plain words, or raw CQL if it contains '=' or '~'."""
        if re.search(r"[=~]", query):
            cql = query
        else:
            words = query.replace('"', "")
            cql = f'type=page and (title ~ "{words}" or text ~ "{words}")'
        if space_key:
            cql = f'space="{space_key}" and ({cql})'
        r = await self.c.request("GET", "/rest/api/search", params={"cql": cql + " order by lastmodified desc",
                                                                    "limit": max(1, min(limit, 25))})
        out = []
        for res in r.get("results", []):
            content = res.get("content") or {}
            out.append({"id": content.get("id"), "title": content.get("title") or res.get("title"),
                        "space": (res.get("resultGlobalContainer") or {}).get("title"),
                        "url": self.c.page_url(res.get("url"), content.get("id", "")),
                        "last_modified": res.get("friendlyLastModified"),
                        "excerpt": re.sub(r"@@@(end)?hl@@@", "", res.get("excerpt") or "")[:300]})
        return {"count": len(out), "results": out}

    async def get_page(self, page_id: str, max_chars: int = 12000) -> dict:
        p = await self.c.request("GET", f"/api/v2/pages/{page_id}", params={"body-format": "atlas_doc_format"})
        body = json.loads(((p.get("body") or {}).get("atlas_doc_format") or {}).get("value") or "{}")
        text = to_text(body).strip()
        return {"id": p["id"], "title": p.get("title"), "space_id": p.get("spaceId"), "version": (p.get("version") or {}).get("number"),
                "url": self.c.page_url((p.get("_links") or {}).get("webui"), p["id"]),
                "text": text[:max_chars], "truncated": len(text) > max_chars}

    async def create_page(self, space_id: str, title: str, body: str | dict, parent_id: str | None = None) -> dict:
        payload: dict[str, Any] = {"spaceId": space_id, "status": "current", "title": title,
                                   "body": {"representation": "atlas_doc_format", "value": json.dumps(_doc(body))}}
        if parent_id:
            payload["parentId"] = parent_id
        p = await self.c.request("POST", "/api/v2/pages", json=payload)
        return {"id": p["id"], "title": p.get("title"), "url": self.c.page_url((p.get("_links") or {}).get("webui"), p["id"])}

    async def update_page(self, page_id: str, body: str | dict, mode: str = "append", version_message: str = "") -> dict:
        """Append a section to (default) or replace the body of an existing page. Needs a human confirm."""
        if mode not in ("append", "replace"):
            raise JiraError("mode must be 'append' or 'replace'")
        cur = await self.c.request("GET", f"/api/v2/pages/{page_id}", params={"body-format": "atlas_doc_format"})
        new = _doc(body)
        if mode == "append":
            old = json.loads(((cur.get("body") or {}).get("atlas_doc_format") or {}).get("value") or '{"type":"doc","version":1,"content":[]}')
            new = {"type": "doc", "version": 1, "content": list(old.get("content", [])) + list(new.get("content", []))}
        version = (cur.get("version") or {}).get("number", 1) + 1
        p = await self.c.request("PUT", f"/api/v2/pages/{page_id}", json={
            "id": page_id, "status": "current", "title": cur.get("title"),
            "body": {"representation": "atlas_doc_format", "value": json.dumps(new)},
            "version": {"number": version, "message": version_message or f"Updated by {os.environ.get('AGENT_DISPLAY_NAME', 'the AI agent')} ({mode})"}})
        return {"id": page_id, "version": version, "mode": mode,
                "url": self.c.page_url((p.get("_links") or {}).get("webui"), page_id)}

    async def add_comment(self, page_id: str, body: str | dict) -> dict:
        c = await self.c.request("POST", "/api/v2/footer-comments", json={
            "pageId": page_id, "body": {"representation": "atlas_doc_format", "value": json.dumps(_doc(body))}})
        return {"page_id": page_id, "comment_id": c.get("id")}

    async def list_spaces(self, limit: int = 50) -> dict:
        r = await self.c.request("GET", "/api/v2/spaces", params={"limit": max(1, min(limit, 250))})
        return {"spaces": [{"id": s["id"], "key": s.get("key"), "name": s.get("name")} for s in r.get("results", [])]}


TOOL_DOCS = {
    "search_pages": "Search Confluence pages by words (or CQL). Returns id, title, space, url, excerpt.",
    "get_page": "Read a Confluence page as plain text (with its url and version).",
    "create_page": "Create a new page in a space (space_id from list_spaces or a page). body accepts markdown or an ADF doc.",
    "update_page": "Edit an existing page: mode 'append' (default) adds a section, 'replace' rewrites the body. Needs a human confirm.",
    "add_comment": "Add a footer comment to a page. body accepts markdown or an ADF doc.",
    "list_spaces": "List the Confluence spaces you can see.",
}


def create_app():
    from mcp.server.mcpserver import MCPServer
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    logging.getLogger("httpx").setLevel(logging.WARNING)
    tools = ConfluenceTools(ConfluenceClient(os.environ["CONFLUENCE_CLIENT_ID"], os.environ["CONFLUENCE_CLIENT_SECRET"],
                                             os.environ["ATLASSIAN_CLOUD_ID"], os.environ["ATLASSIAN_SITE_URL"]))
    server = MCPServer(name="confluence", instructions=f"Confluence Cloud for {org().name} as the agent's own service account.")
    for name, doc in TOOL_DOCS.items():
        server.tool(name=name, description=doc)(getattr(tools, name))
    mcp_app = server.streamable_http_app(streamable_http_path="/", stateless_http=True, host="0.0.0.0")
    expected = os.environ["MCP_SERVER_TOKEN"]

    class BearerGuard:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http":
                auth = dict(scope.get("headers", [])).get(b"authorization", b"").decode()
                if not hmac.compare_digest(auth.removeprefix("Bearer ").strip(), expected):
                    await JSONResponse({"error": "unauthorised"}, status_code=401)(scope, receive, send)
                    return
            await self.app(scope, receive, send)

    async def healthz(request):
        return JSONResponse({"ok": True})

    return Starlette(routes=[Route("/healthz", healthz), Mount("/mcp", app=BearerGuard(mcp_app))],
                     lifespan=getattr(mcp_app.router, "lifespan_context", None))


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), log_level="warning")


if __name__ == "__main__":
    main()
