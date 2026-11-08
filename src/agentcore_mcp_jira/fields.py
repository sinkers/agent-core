"""Jira field catalogue: find any field by its name, and turn values into plain data.

Jira keeps story points, sprint, epic link and every site-specific field under ids like
`customfield_10028`, and the id differs between sites and between company-managed
("Story Points") and team-managed ("Story point estimate") projects. The agent names
fields as people do; this module maps names to ids (cached for an hour) and back.
"""

from __future__ import annotations

import difflib
import json
import time
from typing import Any

from .adf import to_text
from .client import JiraClient, JiraError

STORY_POINT_NAMES = ("story points", "story point estimate")
STORY_POINT_ALIASES = STORY_POINT_NAMES + ("points", "story_points", "storypoints", "sp")
SPRINT_NAME = "sprint"
EPIC_LINK_NAME = "epic link"
# Internal bookkeeping fields that only add noise to an issue summary.
NOISY = {"rank", "development", "vulnerability", "flagged", "request participants", "satisfaction",
         "satisfaction date", "approvals", "request language", "organizations", "customer request type"}
NOISY_PREFIXES = ("[chart]", "sla ")
MAX_TEXT = 1000
CACHE_S = 3600


def simple(value: Any) -> Any:
    """Plain text/number/list for a Jira field value (users, options, ADF docs, sprints...)."""
    if isinstance(value, list):
        return [simple(v) for v in value]
    if isinstance(value, dict):
        if value.get("type") == "doc":
            return to_text(value).strip()[:MAX_TEXT]
        if {"id", "name", "state"} <= value.keys():  # a sprint
            return f"{value.get('name')} ({value.get('state')})"
        for k in ("displayName", "value", "name", "key", "emailAddress"):
            if k in value:
                out = value[k]
                if isinstance(value.get("child"), dict):  # cascading select
                    out = f"{out} / {simple(value['child'])}"
                return out
        text = json.dumps(value)
        return value if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "…"
    if isinstance(value, str):
        return value[:MAX_TEXT]
    return value


def is_empty(value: Any) -> bool:
    return value is None or value in ("", [], {}, "{}")


class FieldCatalog:
    def __init__(self, jira: JiraClient):
        self.jira = jira
        self._fields: list[dict] = []
        self._loaded = 0.0

    async def all(self) -> list[dict]:
        if not self._fields or time.time() - self._loaded > CACHE_S:
            self._fields = await self.jira.request("GET", "/field")
            self._loaded = time.time()
        return self._fields

    async def by_id(self) -> dict[str, dict]:
        return {f["id"]: f for f in await self.all()}

    async def named(self, lower_name: str) -> list[dict]:
        return [f for f in await self.all() if f.get("name", "").lower() == lower_name]

    async def ids_named(self, *lower_names: str) -> list[str]:
        return [f["id"] for n in lower_names for f in await self.named(n)]

    async def story_point_ids(self) -> list[str]:
        return await self.ids_named(*STORY_POINT_NAMES)

    async def resolve(self, name_or_id: str) -> dict:
        """One field for a name (any case) or id; a clear error with suggestions otherwise."""
        wanted = name_or_id.strip()
        ids = await self.by_id()
        if wanted in ids:
            return ids[wanted]
        matches = await self.named(wanted.lower())
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise JiraError(f"several fields are called {wanted!r}: "
                            + ", ".join(f"{m['id']}" for m in matches) + ". Use the id.")
        names = sorted({f.get("name", "") for f in await self.all()})
        close = difflib.get_close_matches(wanted, names, n=5, cutoff=0.5) or difflib.get_close_matches(
            wanted.title(), names, n=5, cutoff=0.5)
        hint = f" Did you mean: {', '.join(close)}?" if close else " Use list_fields to see what exists."
        raise JiraError(f"no Jira field called {wanted!r}.{hint}")

    async def start_date_id(self) -> str | None:
        """Jira's own "Start date" (the one the timeline draws): a date picker; when an admin has made another
        field with the same name, the locked one."""
        if getattr(self, "_start_id", None) is not None:
            return self._start_id or None
        named = [f for f in await self.named("start date")
                 if (f.get("schema") or {}).get("custom", "").endswith(":datepicker") or f.get("schema", {}).get("type") == "date"]
        if len(named) > 1:
            found = await self.jira.request("GET", "/field/search", params={"query": "Start date", "expand": "isLocked"})
            locked = [v["id"] for v in found.get("values", []) if v.get("isLocked") and v.get("name", "").lower() == "start date"]
            named = [f for f in named if f["id"] in locked] or named
        self._start_id = named[0]["id"] if named else ""
        return self._start_id or None

    async def story_point_field_for(self, key: str) -> str:
        """The story-point field this issue's edit screen actually has."""
        sp = await self.story_point_ids()
        if not sp:
            raise JiraError("this Jira site has no story point field")
        if len(sp) == 1:
            return sp[0]
        meta = await self.jira.request("GET", f"/issue/{key}/editmeta")
        on_screen = [f for f in sp if f in (meta.get("fields") or {})]
        if not on_screen:
            raise JiraError(f"{key} has no story point field on its edit screen; ask a Jira admin to add it")
        return on_screen[0]


def coerce(field: dict, value: Any) -> Any:
    """A value in the shape Jira's edit API expects for this field's type."""
    if value is None:
        return None
    schema = field.get("schema") or {}
    kind, items, custom = schema.get("type"), schema.get("items"), schema.get("custom", "")
    name = field.get("name", field.get("id"))
    try:
        if kind == "number":
            return float(value)
        if "gh-sprint" in custom or name.lower() == SPRINT_NAME:
            if isinstance(value, bool) or not str(value).strip().isdigit():
                raise JiraError(f"{name} takes a sprint id (a number), e.g. from an issue already in that sprint")
            return int(value)
        if kind in ("option", "option2"):
            return {"value": str(value)}
        if kind == "user":
            return {"accountId": str(value)}
        if kind == "array":
            values = value if isinstance(value, list) else [value]
            if items == "option":
                return [{"value": str(v)} for v in values]
            if items == "user":
                return [{"accountId": str(v)} for v in values]
            if items in ("version", "component"):
                return [{"name": str(v)} for v in values]
            return [str(v) for v in values]
        if kind in ("string", "date", "datetime", "any"):
            return value if isinstance(value, dict) else str(value)
    except (TypeError, ValueError) as exc:
        raise JiraError(f"{name} expects a {kind}: {exc}") from exc
    return value


def is_noisy(name: str) -> bool:
    low = name.lower()
    return low in NOISY or low.startswith(NOISY_PREFIXES)
