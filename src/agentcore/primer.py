"""An agent's primer: the Markdown files its owner keeps in SharePoint as "what it knows before it looks
anything up", e.g. `AI Team Agents/ada-memory`.

The folder's own index says which files are always loaded and which on demand. `always` files go into
the system prompt (prompt-cached); `on_demand` files become loadable skills (`skills__load`). Files are
read through the openclaw MCP `sharepoint_get` tool, refreshed every `refresh_minutes`, and the last
good copy is kept in Postgres so a cold start works even when SharePoint or the MCP server is down.
SharePoint stays the single source: edit the files there, and the agent picks them up within the hour.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging

from .documents import _parse_markdown_result
from .skills import Skill, parse_skill_text

log = logging.getLogger(__name__)
MAX_FILE_CHARS = 80_000


from .config import PrimerSource as PrimerConfig  # noqa: E402  (one definition, used by AgentConfig)


class PrimerSync:
    def __init__(self, cfg: PrimerConfig, skills, openclaw, store, agent: str):
        self.cfg = cfg
        self.skills = skills  # SkillLoader
        self.openclaw = openclaw  # MCP client with sharepoint_get
        self.store = store
        self.agent = agent

    async def _fetch(self, filename: str) -> str:
        result = await self.openclaw.call_tool("sharepoint_get", {
            "site": self.cfg.site, "path": f"{self.cfg.folder}/{filename}", "max_inline_bytes": 2 * 1024 * 1024})
        body = _parse_markdown_result(result) if isinstance(result, str) else None
        if not body or not body.get("content_base64"):
            raise RuntimeError(f"{filename}: no inline content returned")
        return base64.b64decode(body["content_base64"]).decode("utf-8", errors="replace")[:MAX_FILE_CHARS]

    async def refresh(self) -> int:
        """Fetch every file; on any failure keep (or restore) the last good copy. Returns files fetched."""
        files: dict[str, str] = {}
        try:
            for name in [*self.cfg.always, *self.cfg.on_demand]:
                files[name] = await self._fetch(name)
        except Exception as exc:
            log.warning("primer refresh failed (%s); using the last good copy", exc)
            saved = await self.store.get_pref(f"primer:{self.agent}", "files")
            if saved and not self._applied():
                self._apply(saved)
            return 0
        self._apply(files)
        await self.store.set_pref(f"primer:{self.agent}", "files", files)
        log.info("primer: %d files loaded (%d always, %d on demand)", len(files), len(self.cfg.always),
                 len(self.cfg.on_demand))
        return len(files)

    def _applied(self) -> bool:
        return "primer" in getattr(self.skills, "dynamic", {}).values()

    def _apply(self, files: dict[str, str]) -> None:
        always: list[Skill] = []
        on_demand: list[Skill] = []
        for filename, text in files.items():
            try:
                skill = parse_skill_text(text, default_name=filename.rsplit(".", 1)[0])
            except Exception as exc:
                log.warning("primer: %s skipped (%s)", filename, exc)
                continue
            (always if filename in self.cfg.always else on_demand).append(skill)
        self.skills.set_dynamic(always, on_demand, source="primer")

    async def run_forever(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.refresh_minutes * 60)
            try:
                await self.refresh()
            except Exception:
                log.exception("primer refresh crashed")


def describe(files: dict[str, str]) -> str:
    return json.dumps({k: len(v) for k, v in files.items()})
