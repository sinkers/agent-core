"""GitHub App client for a reviewer agent's pull-request reviews.

Authenticates as the "Ada (AI)" GitHub App: a short-lived JWT signed with the App's private key, exchanged
for an installation token narrowed to the one repository being worked on. Only the worker (github_review.py)
writes to GitHub, and only a COMMENT review, comments and replies: APPROVE is refused here, whatever asks.

Environment: GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY (PEM), GITHUB_WEBHOOK_SECRET.
"""

from __future__ import annotations

import base64
import re
import time
from dataclasses import dataclass, field

import httpx

API = "https://api.github.com"
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.M)
ALLOWED_EVENTS = ("COMMENT", "REQUEST_CHANGES")


class GitHubError(RuntimeError):
    pass


def commentable_lines(patch: str | None) -> dict[str, set[int]]:
    """{"RIGHT": new-file lines, "LEFT": old-file lines} that an inline review comment may target: the lines
    inside the diff hunks (context, added or removed). A comment anywhere else is rejected by GitHub."""
    right: set[int] = set()
    left: set[int] = set()
    old = new = 0
    for line in (patch or "").splitlines():
        m = HUNK_RE.match(line)
        if m:
            old, new = int(m.group(1)), int(m.group(3))
            continue
        if line.startswith("+"):
            right.add(new)
            new += 1
        elif line.startswith("-"):
            left.add(old)
            old += 1
        elif line.startswith("\\"):
            continue
        else:
            right.add(new)
            left.add(old)
            old += 1
            new += 1
    return {"RIGHT": right, "LEFT": left}


@dataclass
class GitHubApp:
    app_id: str
    private_key: str
    http: httpx.AsyncClient = field(default_factory=lambda: httpx.AsyncClient(timeout=30))
    _tokens: dict = field(default_factory=dict)  # repo -> (token, expires_at)
    _installations: dict = field(default_factory=dict)  # repo -> installation id

    def _jwt(self) -> str:
        import jwt
        now = int(time.time())
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self.app_id}, self.private_key, algorithm="RS256")

    async def _app_get(self, path: str) -> dict:
        r = await self.http.get(f"{API}{path}", headers={"Authorization": f"Bearer {self._jwt()}",
                                                         "Accept": "application/vnd.github+json"})
        if r.status_code >= 400:
            raise GitHubError(f"GET {path} -> {r.status_code}: {r.text[:200]}")
        return r.json()

    async def token(self, repo: str) -> str:
        """An installation token for one repository (cached until 5 minutes before it expires)."""
        cached = self._tokens.get(repo)
        if cached and cached[1] - 300 > time.time():
            return cached[0]
        if repo not in self._installations:
            self._installations[repo] = (await self._app_get(f"/repos/{repo}/installation"))["id"]
        name = repo.split("/", 1)[1]
        r = await self.http.post(f"{API}/app/installations/{self._installations[repo]}/access_tokens",
                                 headers={"Authorization": f"Bearer {self._jwt()}", "Accept": "application/vnd.github+json"},
                                 json={"repositories": [name]})
        if r.status_code >= 400:
            raise GitHubError(f"installation token for {repo} -> {r.status_code}: {r.text[:200]}")
        body = r.json()
        expires = time.time() + 3300
        self._tokens[repo] = (body["token"], expires)
        return body["token"]

    async def request(self, method: str, repo: str, path: str, *, json=None, params=None, accept: str | None = None):
        r = await self.http.request(method, f"{API}/repos/{repo}{path}", json=json, params=params,
                                    headers={"Authorization": f"token {await self.token(repo)}",
                                             "Accept": accept or "application/vnd.github+json",
                                             "X-GitHub-Api-Version": "2022-11-28"})
        if r.status_code >= 400:
            raise GitHubError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        if accept and "diff" in accept:
            return r.text
        return r.json() if r.content else {}

    async def paged(self, repo: str, path: str, limit: int = 300) -> list:
        out, page = [], 1
        while len(out) < limit:
            batch = await self.request("GET", repo, path, params={"per_page": 100, "page": page})
            out += batch
            if len(batch) < 100:
                break
            page += 1
        return out[:limit]

    # ---- reads ----
    async def pr(self, repo: str, number: int) -> dict:
        return await self.request("GET", repo, f"/pulls/{number}")

    async def commits(self, repo: str, number: int) -> list:
        return await self.paged(repo, f"/pulls/{number}/commits", 250)

    async def files(self, repo: str, number: int) -> list:
        return await self.paged(repo, f"/pulls/{number}/files", 3000)

    async def compare(self, repo: str, base: str, head: str) -> dict:
        return await self.request("GET", repo, f"/compare/{base}...{head}")

    async def file(self, repo: str, path: str, ref: str, max_bytes: int = 20 * 1024 * 1024) -> bytes | None:
        try:
            body = await self.request("GET", repo, f"/contents/{path}", params={"ref": ref})
        except GitHubError:
            return None
        if isinstance(body, list) or body.get("size", 0) > max_bytes:
            return None
        if body.get("encoding") == "base64":
            return base64.b64decode(body.get("content") or "")
        if body.get("download_url"):
            r = await self.http.get(body["download_url"], headers={"Authorization": f"token {await self.token(repo)}"})
            return r.content if r.status_code < 400 else None
        return None

    async def review_comments(self, repo: str, number: int) -> list:
        return await self.paged(repo, f"/pulls/{number}/comments", 500)

    async def reviews(self, repo: str, number: int) -> list:
        return await self.paged(repo, f"/pulls/{number}/reviews", 200)

    async def open_prs(self, repo: str) -> list:
        return await self.paged(repo, "/pulls?state=open", 200)

    # ---- writes (the worker only) ----
    async def create_review(self, repo: str, number: int, commit_id: str, body: str, comments: list[dict],
                            event: str = "COMMENT") -> dict:
        if event not in ALLOWED_EVENTS:
            raise GitHubError(f"review event {event!r} isn't allowed: the reviewer comments, it never approves")
        return await self.request("POST", repo, f"/pulls/{number}/reviews",
                                  json={"commit_id": commit_id, "body": body, "event": event, "comments": comments})

    async def comment(self, repo: str, number: int, body: str) -> dict:
        return await self.request("POST", repo, f"/issues/{number}/comments", json={"body": body})

    async def reply(self, repo: str, number: int, comment_id: int, body: str) -> dict:
        return await self.request("POST", repo, f"/pulls/{number}/comments/{comment_id}/replies", json={"body": body})
