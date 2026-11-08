"""Microsoft Teams via the Bot Framework protocol.

Inbound: every POST to /api/messages carries a Bot Framework JWT, verified here
against the Bot Framework signing keys, the bot's app ID and the activity's
serviceUrl.
Outbound: replies and proactive messages go to the Bot Connector REST API with a
client-credentials token for the bot's single-tenant Entra app.
"""

from __future__ import annotations

import asyncio
import html
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt

BOTFRAMEWORK_OPENID = "https://login.botframework.com/v1/.well-known/openidconfiguration"
BOTFRAMEWORK_ISSUER = "https://api.botframework.com"
BOTFRAMEWORK_SCOPE = "https://api.botframework.com/.default"


class AuthError(Exception):
    pass


class BotAuthenticator:
    def __init__(self, app_id: str, openid_url: str = BOTFRAMEWORK_OPENID, issuer: str = BOTFRAMEWORK_ISSUER,
                 jwks_client: Any = None):
        self.app_id = app_id
        self.issuer = issuer
        self.openid_url = openid_url
        self._jwks = jwks_client

    def _jwks_client(self) -> Any:
        if self._jwks is None:
            meta = httpx.get(self.openid_url, timeout=10).json()
            self._jwks = jwt.PyJWKClient(meta["jwks_uri"], cache_keys=True, lifespan=86400)
        return self._jwks

    def _verify(self, token: str, service_url: str | None) -> dict:
        key = self._jwks_client().get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token, key, algorithms=["RS256"], audience=self.app_id, issuer=self.issuer,
            leeway=300, options={"require": ["exp", "iss", "aud"]},
        )
        if service_url and claims.get("serviceurl") and claims["serviceurl"].rstrip("/") != service_url.rstrip("/"):
            raise AuthError("serviceUrl claim does not match activity")
        return claims

    async def verify(self, authorization: str | None, service_url: str | None) -> dict:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise AuthError("missing bearer token")
        try:
            return await asyncio.to_thread(self._verify, authorization.split(" ", 1)[1], service_url)
        except AuthError:
            raise
        except Exception as exc:
            raise AuthError(str(exc)) from exc


@dataclass
class _Token:
    value: str
    expires_at: float


class BotConnector:
    def __init__(self, app_id: str, app_password: str, tenant_id: str, http: httpx.AsyncClient | None = None):
        self.app_id = app_id
        self.app_password = app_password
        self.tenant_id = tenant_id
        self.http = http or httpx.AsyncClient(timeout=20)
        self._token: _Token | None = None

    async def token(self) -> str:
        if self._token and self._token.expires_at - 300 > time.time():
            return self._token.value
        r = await self.http.post(
            f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token",
            data={"grant_type": "client_credentials", "client_id": self.app_id,
                  "client_secret": self.app_password, "scope": BOTFRAMEWORK_SCOPE},
        )
        r.raise_for_status()
        body = r.json()
        self._token = _Token(body["access_token"], time.time() + int(body.get("expires_in", 3600)))
        return self._token.value

    async def send(self, ref: dict, text: str, reply_to_id: str | None = None) -> dict:
        service_url = ref["serviceUrl"].rstrip("/")
        conv_id = ref["conversation"]["id"]
        url = f"{service_url}/v3/conversations/{conv_id}/activities"
        if reply_to_id:
            url += f"/{reply_to_id}"
        activity = {"type": "message", "text": text, "textFormat": "markdown",
                    "from": ref.get("bot"), "conversation": ref["conversation"]}
        r = await self.http.post(url, json=activity, headers={"Authorization": f"Bearer {await self.token()}"})
        r.raise_for_status()
        return r.json() if r.content else {}


    async def send_activity(self, ref: dict, activity: dict) -> dict:
        """Post any activity (cards, file consent, file info) into a conversation."""
        url = f"{ref['serviceUrl'].rstrip('/')}/v3/conversations/{ref['conversation']['id']}/activities"
        body = {"from": ref.get("bot"), "conversation": ref["conversation"], **activity}
        r = await self.http.post(url, json=body, headers={"Authorization": f"Bearer {await self.token()}"})
        r.raise_for_status()
        return r.json() if r.content else {}

    async def create_conversation(self, service_url: str, bot_app_id: str, user_aad_id: str, tenant_id: str) -> str:
        """Start a 1:1 chat with a user who has the app installed. Returns the conversation id."""
        r = await self.http.post(
            f"{service_url.rstrip('/')}/v3/conversations",
            json={"isGroup": False, "bot": {"id": f"28:{bot_app_id}"}, "members": [{"id": f"8:orgid:{user_aad_id}"}],
                  "tenantId": tenant_id, "channelData": {"tenant": {"id": tenant_id}}},
            headers={"Authorization": f"Bearer {await self.token()}"})
        r.raise_for_status()
        return r.json()["id"]


def conversation_reference(activity: dict) -> dict:
    """The parts of an activity needed to message that conversation later."""
    return {
        "serviceUrl": activity.get("serviceUrl"),
        "channelId": activity.get("channelId"),
        "conversation": activity.get("conversation", {}),
        "bot": activity.get("recipient"),
        "user": activity.get("from"),
        "tenantId": (activity.get("channelData") or {}).get("tenant", {}).get("id"),
    }


_HREF_RE = re.compile(r"""<a\s[^>]*href=["']([^"']+)["'][^>]*>(.*?)</a>""", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def message_links(activity: dict) -> list[str]:
    """Links Teams keeps out of `text`: pasted SharePoint links become anchors in the HTML attachment, and
    shared files arrive as file cards. Returned as 'name: url' lines."""
    found: list[str] = []
    for att in activity.get("attachments") or []:
        kind, content = att.get("contentType", ""), att.get("content")
        if kind == "text/html" and isinstance(content, str):
            for href, label in _HREF_RE.findall(content):
                label = html.unescape(_TAG_RE.sub("", label)).strip()
                found.append(f"{label}: {html.unescape(href)}" if label and label != href else html.unescape(href))
        elif kind == "application/vnd.microsoft.teams.file.download.info" or (
                att.get("contentUrl") and kind != "text/html" and not kind.startswith("application/vnd.microsoft.card")):
            # Attached files: never put their (pre-authenticated) links in front of the model (attachments.py).
            found.append(f"{att.get('name') or kind} (attached file: read it with read_attachment)")
    return list(dict.fromkeys(found))


def message_text(activity: dict, bot_id: str | None) -> str:
    """Activity text with the bot's own @mention removed, plus any links Teams carried as attachments."""
    text = activity.get("text") or ""
    for ent in activity.get("entities") or []:
        if ent.get("type") == "mention" and (ent.get("mentioned") or {}).get("id") == bot_id:
            text = text.replace(ent.get("text", ""), "")
    text = text.strip()
    links = [l for l in message_links(activity) if l.rsplit(": ", 1)[-1] not in text]
    if links:
        text += "\n\n[Links in this message]\n" + "\n".join(f"- {l}" for l in links)
    return text


def is_addressed(activity: dict, bot_id: str | None, agent_first_name: str) -> bool:
    """Personal chat always; group chats and channels only when mentioned or named."""
    conv_type = (activity.get("conversation") or {}).get("conversationType", "personal")
    if conv_type == "personal":
        return True
    for ent in activity.get("entities") or []:
        if ent.get("type") == "mention" and (ent.get("mentioned") or {}).get("id") == bot_id:
            return True
    return re.search(rf"\b{re.escape(agent_first_name)}\b", activity.get("text") or "", re.IGNORECASE) is not None
