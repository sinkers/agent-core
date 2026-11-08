"""`pipedrive` MCP server: the organisation's Pipedrive CRM for the pre-sales agent, read-only to start.

The agent reads deals, organisations, people, notes and activities so material targets each customer's actual
pain points. Writes (notes, activities) come later, behind a confirm.

The Pipedrive MCP connected to Claude signs in as a person through Pipedrive's OAuth; an agent running
unattended in the cluster uses an API token instead (a Pipedrive user for the agent, or one the owner issues).

Environment: PIPEDRIVE_API_TOKEN, PIPEDRIVE_DOMAIN (the company subdomain, e.g. "acme" for
acme.pipedrive.com), MCP_SERVER_TOKEN (bearer required from callers), PORT (default 8080).
"""

from __future__ import annotations

import hmac
import logging
import os
import re
from typing import Any

import httpx
from agentcore.org import org

log = logging.getLogger(__name__)

MAX_TEXT = 1500


class PipedriveError(RuntimeError):
    pass


def _plain(html: str | None) -> str:
    """Notes are HTML in Pipedrive: plain text for the model."""
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", html or "", flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&nbsp;", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()[:MAX_TEXT]


class PipedriveClient:
    def __init__(self, token: str, domain: str, http: httpx.AsyncClient | None = None):
        self.token = token
        self.base = f"https://{domain}.pipedrive.com/api"
        self.web = f"https://{domain}.pipedrive.com"
        self.http = http or httpx.AsyncClient(timeout=30)

    async def get(self, path: str, params: dict | None = None, version: str = "v2") -> Any:
        r = await self.http.get(f"{self.base}/{version}{path}", params=params or {},
                                headers={"x-api-token": self.token, "Accept": "application/json"})
        if r.status_code >= 400:
            try:
                msg = r.json().get("error") or r.text[:200]
            except ValueError:
                msg = r.text[:200]
            raise PipedriveError(f"GET {path} -> {r.status_code}: {msg}")
        body = r.json()
        if body.get("success") is False:
            raise PipedriveError(f"GET {path}: {body.get('error') or 'failed'}")
        return body


class PipedriveTools:
    def __init__(self, client: PipedriveClient):
        self.c = client

    def _deal(self, d: dict) -> dict:
        return {"id": d.get("id"), "title": d.get("title"), "value": d.get("value"), "currency": d.get("currency"),
                "status": d.get("status"), "stage_id": d.get("stage_id"), "pipeline_id": d.get("pipeline_id"),
                "org_id": d.get("org_id"), "person_id": d.get("person_id"), "owner_id": d.get("owner_id"),
                "expected_close_date": d.get("expected_close_date"), "probability": d.get("probability"),
                "updated": d.get("update_time"), "url": f"{self.c.web}/deal/{d.get('id')}"}

    async def search(self, term: str, item_types: list[str] | None = None, limit: int = 10) -> dict:
        """Find deals, organisations, people or leads by name or text."""
        if len(term.strip()) < 2:
            raise PipedriveError("search needs at least 2 characters")
        params: dict = {"term": term.strip(), "limit": max(1, min(limit, 50))}
        if item_types:
            params["item_types"] = ",".join(item_types)
        body = await self.c.get("/itemSearch", params)
        items = ((body.get("data") or {}).get("items")) or []
        return {"results": [{"type": i.get("item", {}).get("type"), "id": i.get("item", {}).get("id"),
                             "title": i.get("item", {}).get("title") or i.get("item", {}).get("name"),
                             "score": i.get("result_score")} for i in items]}

    async def list_deals(self, status: str = "open", pipeline_id: int | None = None, stage_id: int | None = None,
                         limit: int = 50) -> dict:
        """Deals by status (open, won, lost, all), optionally in one pipeline or stage, newest update first."""
        params: dict = {"limit": max(1, min(limit, 100)), "sort_by": "update_time", "sort_direction": "desc"}
        if status != "all":
            params["status"] = status
        if pipeline_id:
            params["pipeline_id"] = pipeline_id
        if stage_id:
            params["stage_id"] = stage_id
        body = await self.c.get("/deals", params)
        return {"deals": [self._deal(d) for d in body.get("data") or []]}

    async def get_deal(self, deal_id: int, notes: int = 10, activities: int = 10) -> dict:
        """One deal with its organisation, contact, recent notes and activities: what the customer cares about."""
        d = (await self.c.get(f"/deals/{deal_id}")).get("data") or {}
        out = self._deal(d)
        if d.get("org_id"):
            out["organization"] = await self._org(d["org_id"])
        if d.get("person_id"):
            out["person"] = await self._person(d["person_id"])
        nb = await self.c.get("/notes", {"deal_id": deal_id, "limit": notes, "sort": "add_time DESC"}, version="v1")
        out["notes"] = [{"added": n.get("add_time"), "by": (n.get("user") or {}).get("name"),
                         "text": _plain(n.get("content"))} for n in nb.get("data") or []]
        ab = await self.c.get("/activities", {"deal_id": deal_id, "limit": activities, "sort_by": "update_time",
                                              "sort_direction": "desc"})
        out["activities"] = [{"type": a.get("type"), "subject": a.get("subject"), "due": a.get("due_date"),
                              "done": a.get("done"), "note": _plain(a.get("note"))} for a in ab.get("data") or []]
        return out

    async def _org(self, org_id: int) -> dict:
        o = (await self.c.get(f"/organizations/{org_id}")).get("data") or {}
        return {"id": o.get("id"), "name": o.get("name"), "address": (o.get("address") or {}).get("value")
                if isinstance(o.get("address"), dict) else o.get("address"), "url": f"{self.c.web}/organization/{o.get('id')}"}

    async def _person(self, person_id: int) -> dict:
        p = (await self.c.get(f"/persons/{person_id}")).get("data") or {}
        return {"id": p.get("id"), "name": p.get("name"), "job_title": p.get("job_title"),
                "org_id": p.get("org_id"), "url": f"{self.c.web}/person/{p.get('id')}"}

    async def get_organization(self, org_id: int) -> dict:
        """An organisation with its open deals."""
        out = await self._org(org_id)
        deals = await self.c.get("/deals", {"org_id": org_id, "limit": 50})
        out["deals"] = [self._deal(d) for d in deals.get("data") or []]
        return out

    async def get_person(self, person_id: int) -> dict:
        return await self._person(person_id)

    async def list_pipelines(self) -> dict:
        """Pipelines and their stages (names for stage ids)."""
        pipes = (await self.c.get("/pipelines")).get("data") or []
        stages = (await self.c.get("/stages", {"limit": 500})).get("data") or []
        return {"pipelines": [{"id": p.get("id"), "name": p.get("name"),
                               "stages": [{"id": s.get("id"), "name": s.get("name")} for s in stages
                                          if s.get("pipeline_id") == p.get("id")]} for p in pipes]}


TOOL_DOCS = {
    "search": "Find deals, organisations, people or leads in Pipedrive by name or text (item_types: deal, person, organization, lead).",
    "list_deals": "List deals by status (open, won, lost, all), optionally one pipeline or stage; newest update first.",
    "get_deal": ("One deal with its organisation, contact, recent notes and activities: use the notes to find the "
                 f"customer's pain points before preparing material. Customer data: never share outside {org().name}."),
    "get_organization": "An organisation and its deals.",
    "get_person": "A contact person.",
    "list_pipelines": "Pipelines and their stages, to name stage ids.",
}


def build_server(tools: PipedriveTools):
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(name="pipedrive", instructions=f"{org().name}'s Pipedrive CRM, read-only, for the pre-sales agent.")
    for name, doc in TOOL_DOCS.items():
        server.tool(name=name, description=doc)(getattr(tools, name))
    return server


def create_app():
    """Starlette app: MCP at /mcp behind a bearer check, plus /healthz."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    logging.getLogger("httpx").setLevel(logging.WARNING)
    client = PipedriveClient(os.environ["PIPEDRIVE_API_TOKEN"], os.environ["PIPEDRIVE_DOMAIN"])
    mcp_app = build_server(PipedriveTools(client)).streamable_http_app(streamable_http_path="/", stateless_http=True,
                                                                       host="0.0.0.0")
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
