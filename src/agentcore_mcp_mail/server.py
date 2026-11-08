"""`m365-mail` MCP server: send email as one agent's own mailbox.

Runs with the agent's Entra app. Exchange RBAC for Applications limits that app to its own
mailbox, so this server cannot send as anyone else. Internal recipients (the tenant's
domains) use `send_email`; anything else must go through `send_email_external`, which the
agent's allow-list tags `confirm`. Every message carries the AI footer.

Environment: MAIL_APP_ID, MAIL_APP_PASSWORD, MAIL_TENANT_ID, MAIL_SENDER, MAIL_FOOTER,
MAIL_INTERNAL_DOMAINS (comma list, e.g. example.com), MCP_SERVER_TOKEN, PORT.
"""

from __future__ import annotations

import hmac
import logging
import os
import re

from agentcore.mail import RECIPIENT_RULES, GraphMailer, unique_recipients
from agentcore.org import org

ADDRESS_RE = re.compile(r"^[^@\s<>]+@([^@\s<>]+\.[^@\s<>]+)$")


class MailTools:
    def __init__(self, mailer: GraphMailer, footer: str, internal_domains: set[str]):
        self.mailer = mailer
        self.footer = footer
        self.internal = {d.lower() for d in internal_domains}

    def _check(self, addresses: list[str], internal_only: bool, required: bool = True) -> list[str]:
        clean = []
        for a in addresses:
            m = ADDRESS_RE.match(a.strip())
            if not m:
                raise ValueError(f"not an email address: {a!r}")
            if internal_only and m.group(1).lower() not in self.internal:
                raise ValueError(f"{a} is outside {sorted(self.internal)}: use send_email_external, which needs a confirm")
            clean.append(a.strip())
        if not clean and required:
            raise ValueError("no recipients")
        return clean

    async def _send(self, to: list[str], subject: str, body: str, internal_only: bool,
                    cc: list[str] | None = None) -> dict:
        # Refusals come back as a result the model can act on, not an exception (which reaches it as a bare
        # "error" and gets retried with another guess).
        try:
            recipients = self._check(to, internal_only)
            copied = self._check(cc or [], internal_only, required=False)
        except ValueError as exc:
            return {"sent": False, "reason": f"{exc}. Use the exact address from find_person (or the one the person "
                                              "gave you); never guess or construct an email address."}
        try:
            await self.mailer.send(recipients, subject, body, self.footer, cc=copied)
        except RuntimeError as exc:
            return {"sent": False, "reason": str(exc)[:300]}
        recipients, copied = unique_recipients(recipients, copied, self.mailer.sender)
        return {"sent": True, "from": self.mailer.sender, "to": recipients, **({"cc": copied} if copied else {})}

    async def send_email(self, to: list[str], subject: str, body: str, cc: list[str] | None = None) -> dict:
        return await self._send(to, subject, body, internal_only=True, cc=cc)

    async def send_email_external(self, to: list[str], subject: str, body: str, cc: list[str] | None = None) -> dict:
        return await self._send(to, subject, body, internal_only=False, cc=cc)


def create_app():
    from mcp.server.mcpserver import MCPServer
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    logging.getLogger("httpx").setLevel(logging.WARNING)
    mailer = GraphMailer(os.environ["MAIL_APP_ID"], os.environ["MAIL_APP_PASSWORD"], os.environ["MAIL_TENANT_ID"],
                         os.environ["MAIL_SENDER"])
    tools = MailTools(mailer, os.environ["MAIL_FOOTER"],
                      set(os.environ.get("MAIL_INTERNAL_DOMAINS", "").split(",")))
    server = MCPServer(name="m365-mail", instructions=f"Send email as {mailer.sender}.")
    server.tool(name="send_email", description=(
        f"Email people at {org().name} (internal addresses only). Write the body in Markdown (headings, **bold**, lists, links, "
        "tables): it is sent as formatted email. Use exact addresses: from find_person, "
        "or as the person wrote them. Never guess or build an address from a name or a website/SharePoint domain. "
        + RECIPIENT_RULES
    ))(tools.send_email)
    server.tool(name="send_email_external", description=(
        f"Email someone outside {org().name}, at an address the person asking gave you. Body in Markdown (sent "
        f"formatted). Needs a human confirm. Cc'd {org().name} people are fine; anyone outside {org().name} goes on To "
        "or Cc only if the person "
        "asking gave you their address. " + RECIPIENT_RULES
    ))(tools.send_email_external)
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
