"""In-process `teams` tools: the agent starts a Teams chat with a person.

These tools need the agent's own Bot Framework connector, its conversation references
and its Graph app identity, all of which live in the agent process, so they are served
in-process behind the same ToolGateway rules as MCP tools (allow-list, `writes`,
`scope_args` confirm, kill switch, audit).

Reaching a person, in order:
  1. an existing 1:1 conversation reference (they have chatted with the agent);
  2. a new 1:1 conversation through the Bot Connector (works once the app is installed for them);
  3. install the agent's own app for them (Graph TeamsAppInstallation.ReadWriteSelfForUser.All,
     needs `webApplicationInfo` in the manifest), then 2 again.
Every message carries the AI footer and names who asked for it.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone

import httpx
from .org import localise, org, admin_first

log = logging.getLogger(__name__)
GRAPH = "https://graph.microsoft.com/v1.0"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DAILY_LIMIT_PER_RECIPIENT = 10

TOOLS = [
    {"name": "find_person", "description": "Look up {org} staff by name or email. Returns name, email and id.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "send_teams_message",
     "description": "Send a Teams chat message to one {org} person (email address or full name) from you. "
                    "Say who it is from and why; the requester is named automatically.",
     "inputSchema": {"type": "object", "properties": {"to": {"type": "string"}, "text": {"type": "string"}},
                     "required": ["to", "text"]}},
]
TOOLS = localise(TOOLS)


class TeamsOutreach:
    in_process = True

    def __init__(self, *, connector, store, app_id: str, app_password: str, tenant_id: str, footer: str,
                 teams_app_external_id: str | None = None, default_service_url: str = "https://smba.trafficmanager.net/au/",
                 http: httpx.AsyncClient | None = None):
        self.connector = connector
        self.store = store
        self.app_id = app_id
        self.app_password = app_password
        self.tenant_id = tenant_id
        self.footer = footer
        self.teams_app_external_id = teams_app_external_id
        self.default_service_url = default_service_url
        self.http = http or httpx.AsyncClient(timeout=20)
        self._token: tuple[str, float] | None = None
        self.directory = None  # directory.Directory: local copy of the GAL, tried before Graph
        self._sent: dict[tuple[str, str], int] = {}

    # ------------------------------------------------------------------ Graph
    async def _graph(self, method: str, path: str, **kw) -> httpx.Response:
        if not self._token or self._token[1] - 120 < time.time():
            r = await self.http.post(f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token",
                                     data={"grant_type": "client_credentials", "client_id": self.app_id,
                                           "client_secret": self.app_password,
                                           "scope": "https://graph.microsoft.com/.default"})
            r.raise_for_status()
            b = r.json()
            self._token = (b["access_token"], time.time() + int(b.get("expires_in", 3600)))
        return await self.http.request(method, f"{GRAPH}{path}", headers={"Authorization": f"Bearer {self._token[0]}"}, **kw)

    async def find(self, query: str) -> list[dict]:
        if self.directory and (local := self.directory.find(query)):
            return local[:10]
        q = query.strip().replace("'", "''")
        if EMAIL_RE.match(q):
            flt = f"mail eq '{q}' or userPrincipalName eq '{q}'"
        else:
            flt = f"startswith(displayName,'{q}') or startswith(givenName,'{q}') or startswith(surname,'{q}')"
        r = await self._graph("GET", "/users", params={"$filter": flt, "$select": "id,displayName,mail,jobTitle", "$top": "10"})
        r.raise_for_status()
        return [{"id": u["id"], "name": u.get("displayName"), "email": u.get("mail"), "title": u.get("jobTitle")}
                for u in r.json().get("value", []) if u.get("mail")]

    async def _install_self(self, user_id: str) -> bool:
        if not self.teams_app_external_id:
            return False
        r = await self._graph("GET", "/appCatalogs/teamsApps",
                              params={"$filter": f"externalId eq '{self.teams_app_external_id}'"})
        catalog_id = (r.json().get("value") or [{}])[0].get("id") if r.status_code == 200 else None
        if not catalog_id:
            log.warning("cannot resolve Teams catalog id for self-install (HTTP %s)", r.status_code)
            return False
        r = await self._graph("POST", f"/users/{user_id}/teamwork/installedApps",
                              json={"teamsApp@odata.bind": f"{GRAPH}/appCatalogs/teamsApps/{catalog_id}"})
        ok = r.status_code in (201, 409)
        log.info("self-install for %s -> %s", user_id, r.status_code)
        return ok

    # ------------------------------------------------------------------ tools
    async def list_tools(self) -> list[dict]:
        return TOOLS

    async def call_tool(self, name: str, args: dict, ctx=None) -> str:
        if name == "find_person":
            return json.dumps({"people": [{k: p[k] for k in ("name", "email", "title")} for p in await self.find(args["query"])]})
        if name == "send_teams_message":
            return json.dumps(await self.send(args["to"], args["text"], ctx))
        raise ValueError(f"unknown tool {name}")

    async def send(self, to: str, text: str, ctx) -> dict:
        people = await self.find(to)
        if not people:
            return {"sent": False, "reason": f"no {org().name} person matches {to!r}"}
        if len(people) > 1 and not EMAIL_RE.match(to.strip()):
            return {"sent": False, "reason": "more than one match; ask which one",
                    "candidates": [{"name": p["name"], "email": p["email"]} for p in people]}
        person = people[0]
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        count = self._sent.get((person["id"], day), 0)
        if count >= DAILY_LIMIT_PER_RECIPIENT:
            return {"sent": False, "reason": f"daily limit of {DAILY_LIMIT_PER_RECIPIENT} messages to {person['name']} reached"}

        requester = await self._requester_name(ctx)
        body = f"{text.strip()}\n\n_{self.footer}" + (f" Sent at the request of {requester}._" if requester else "_")
        ref = await self.store.conversation_ref_for_user(person["id"])
        if ref:
            await self.connector.send(ref, body)
            return self._ok(person, day, "existing chat")

        service_url = await self._service_url(ctx)
        conv = await self._create_conversation(service_url, person["id"])
        if conv is None and await self._install_self(person["id"]):
            conv = await self._create_conversation(service_url, person["id"])
        if conv is None:
            return {"sent": False, "reason": f"{person['name']} does not have the agent's Teams app installed; "
                                             f"ask {admin_first()} to add it to the Teams setup policy, or use email"}
        ref = {"serviceUrl": service_url, "channelId": "msteams", "conversation": {"id": conv, "conversationType": "personal"},
               "bot": {"id": f"28:{self.app_id}"}, "user": {"aadObjectId": person["id"], "name": person["name"]},
               "tenantId": self.tenant_id}
        await self.store.upsert_conversation_ref(conv, ref, person["id"])
        await self.connector.send(ref, body)
        return self._ok(person, day, "new chat")

    async def email_for(self, user_id: str) -> str | None:
        """A person's email address by aad id: the directory copy first, then Graph."""
        for p in getattr(self.directory, "people", None) or []:
            if p.get("id") == user_id:
                return p.get("email")
        r = await self._graph("GET", f"/users/{user_id}", params={"$select": "mail"})
        return r.json().get("mail") if r.status_code == 200 else None

    async def send_to_user(self, user_id: str, name: str | None, body: str) -> bool:
        """Deliver a platform message (e.g. a scheduled task result) to one person by aad id."""
        ref = await self.store.conversation_ref_for_user(user_id)
        if not ref:
            conv = await self._create_conversation(self.default_service_url, user_id)
            if conv is None and await self._install_self(user_id):
                conv = await self._create_conversation(self.default_service_url, user_id)
            if conv is None:
                return False
            ref = {"serviceUrl": self.default_service_url, "channelId": "msteams",
                   "conversation": {"id": conv, "conversationType": "personal"}, "bot": {"id": f"28:{self.app_id}"},
                   "user": {"aadObjectId": user_id, "name": name}, "tenantId": self.tenant_id}
            await self.store.upsert_conversation_ref(conv, ref, user_id)
        await self.connector.send(ref, body)
        return True

    def _ok(self, person: dict, day: str, how: str) -> dict:
        self._sent[(person["id"], day)] = self._sent.get((person["id"], day), 0) + 1
        return {"sent": True, "to": person["name"], "email": person["email"], "via": how}

    async def _requester_name(self, ctx) -> str | None:
        uid = getattr(ctx, "actor", None)
        if not uid or uid == "unknown":
            return None
        ref = await self.store.conversation_ref_for_user(uid)
        return ((ref or {}).get("user") or {}).get("name")

    async def _service_url(self, ctx) -> str:
        uid = getattr(ctx, "actor", None)
        ref = await self.store.conversation_ref_for_user(uid) if uid else None
        return (ref or {}).get("serviceUrl") or self.default_service_url

    async def _create_conversation(self, service_url: str, user_id: str) -> str | None:
        try:
            return await self.connector.create_conversation(service_url, self.app_id, user_id, self.tenant_id)
        except httpx.HTTPStatusError as exc:
            log.info("create conversation for %s failed: %s", user_id, exc.response.status_code)
            return None
