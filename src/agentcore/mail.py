"""Outbound email from the agent's own mailbox via Microsoft Graph.

Uses the agent's app registration with an app-only token. Exchange RBAC for
Applications limits `Mail.Send` to the agent's own mailbox, so this client cannot
send as anyone else even if asked to.
"""

from __future__ import annotations

import time

import httpx

from .emailfmt import markdown_to_html

GRAPH = "https://graph.microsoft.com/v1.0"

# Who goes on an email the agent sends; part of every send tool's description (send_email, send_email_external,
# email_file) so the rule is in front of the model whenever it emails.
RECIPIENT_RULES = ("When someone asks you to email other people, cc the person who asked (unless they are already on "
                   "it). The same email to several people is one email: all of them on To (or the main recipients "
                   "on To and the rest on Cc), never a separate copy each.")


def unique_recipients(to: list[str], cc: list[str] | None, sender: str = "") -> tuple[list[str], list[str]]:
    """To and Cc without repeats (case-insensitive): an address on To is dropped from Cc; the sender is dropped from Cc."""
    seen = {sender.lower()} if sender else set()
    out_to, out_cc = [], []
    for addr in to:
        if addr.lower() not in {a.lower() for a in out_to}:
            out_to.append(addr)
    seen |= {a.lower() for a in out_to}
    for addr in cc or []:
        if addr.lower() not in seen:
            out_cc.append(addr)
            seen.add(addr.lower())
    return out_to, out_cc


class GraphMailer:
    def __init__(self, app_id: str, app_password: str, tenant_id: str, sender: str,
                 http: httpx.AsyncClient | None = None):
        self.app_id = app_id
        self.app_password = app_password
        self.tenant_id = tenant_id
        self.sender = sender
        self.http = http or httpx.AsyncClient(timeout=30)
        self._token: tuple[str, float] | None = None

    async def _bearer(self) -> str:
        if self._token and self._token[1] - 300 > time.time():
            return self._token[0]
        r = await self.http.post(
            f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token",
            data={"grant_type": "client_credentials", "client_id": self.app_id,
                  "client_secret": self.app_password, "scope": "https://graph.microsoft.com/.default"},
        )
        r.raise_for_status()
        body = r.json()
        self._token = (body["access_token"], time.time() + int(body.get("expires_in", 3600)))
        return self._token[0]

    async def send(self, to: list[str], subject: str, body: str, footer: str,
                   attachments: list[tuple[str, bytes]] | None = None, cc: list[str] | None = None) -> None:
        to, cc = unique_recipients(to, cc, self.sender)
        message = {
            "subject": subject,
            "body": {"contentType": "HTML", "content": markdown_to_html(body, footer)},  # rich text (emailfmt.py)
            "toRecipients": [{"emailAddress": {"address": addr}} for addr in to],
        }
        if cc:
            message["ccRecipients"] = [{"emailAddress": {"address": addr}} for addr in cc]
        if attachments:  # small files inline (files.py caps them at 3 MB)
            import base64
            message["attachments"] = [{"@odata.type": "#microsoft.graph.fileAttachment", "name": name,
                                       "contentBytes": base64.b64encode(data).decode()} for name, data in attachments]
        r = await self.http.post(
            f"{GRAPH}/users/{self.sender}/sendMail",
            json={"message": message, "saveToSentItems": True},
            headers={"Authorization": f"Bearer {await self._bearer()}"},
        )
        if r.status_code >= 400:
            raise RuntimeError(f"sendMail failed {r.status_code}: {r.text[:300]}")
