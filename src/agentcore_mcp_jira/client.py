"""Jira Cloud REST v3 client for an Atlassian service account (OAuth 2.0 client credentials)."""

from __future__ import annotations

import time
from typing import Any

import httpx

TOKEN_URL = "https://auth.atlassian.com/oauth/token"


class JiraError(RuntimeError):
    pass


class JiraClient:
    def __init__(self, client_id: str, client_secret: str, cloud_id: str, site_url: str,
                 http: httpx.AsyncClient | None = None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.cloud_id = cloud_id
        self.site_url = site_url.rstrip("/")
        self.base = f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3"
        self.http = http or httpx.AsyncClient(timeout=30)
        self._token: tuple[str, float] | None = None

    def browse_url(self, key: str) -> str:
        return f"{self.site_url}/browse/{key}"

    async def _bearer(self) -> str:
        if self._token and self._token[1] - 120 > time.time():
            return self._token[0]
        r = await self.http.post(TOKEN_URL, json={"grant_type": "client_credentials",
                                                  "client_id": self.client_id, "client_secret": self.client_secret})
        if r.status_code >= 400:
            raise JiraError(f"token request failed: {r.status_code}")
        body = r.json()
        self._token = (body["access_token"], time.time() + int(body.get("expires_in", 3600)))
        return self._token[0]

    async def agile(self, method: str, path: str, **kw) -> Any:
        """Jira Software REST (/rest/agile/1.0): rank, boards. Needs Jira Software scopes on the credential."""
        return await self.request(method, path, _base=self.base.replace("/rest/api/3", "/rest/agile/1.0"), **kw)

    async def request(self, method: str, path: str, headers: dict | None = None, _base: str | None = None, **kw) -> Any:
        r = await self.http.request(method, f"{_base or self.base}{path}",
                                    headers={"Authorization": f"Bearer {await self._bearer()}", "Accept": "application/json",
                                             **(headers or {})}, **kw)
        if r.status_code >= 400:
            try:
                detail = r.json()
                msg = "; ".join(detail.get("errorMessages", [])) or str(detail.get("errors") or detail)
            except ValueError:
                msg = r.text[:300]
            raise JiraError(f"{method} {path.split('?')[0]} -> {r.status_code}: {msg[:400]}")
        return r.json() if r.content else {}

    async def download(self, path: str, max_bytes: int = 25 * 1024 * 1024) -> bytes:
        """The bytes behind a REST path that redirects to the file (attachment content). The redirect goes to
        Atlassian's media store with its own signed link; httpx drops the bearer on the cross-host hop."""
        async with self.http.stream("GET", f"{self.base}{path}", follow_redirects=True,
                                    headers={"Authorization": f"Bearer {await self._bearer()}"}) as r:
            if r.status_code >= 400:
                raise JiraError(f"GET {path} -> {r.status_code}")
            chunks, size = [], 0
            async for chunk in r.aiter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise JiraError(f"file is over {max_bytes // 1_048_576} MB")
                chunks.append(chunk)
        return b"".join(chunks)

    async def search(self, jql: str, fields: list[str], limit: int) -> list[dict]:
        issues: list[dict] = []
        token = None
        while len(issues) < limit:
            body: dict = {"jql": jql, "fields": fields, "maxResults": min(100, limit - len(issues))}
            if token:
                body["nextPageToken"] = token
            page = await self.request("POST", "/search/jql", json=body)
            issues.extend(page.get("issues", []))
            token = page.get("nextPageToken")
            if not token:
                break
        return issues[:limit]

    async def myself(self) -> dict:
        return await self.request("GET", "/myself")
