"""The organisation the agents work for: everything about the deploying organisation that the engine needs but must
not hard-code (time zone, public holidays, email domains, web addresses, call hours, phone country, the brand pack,
the method source). One file, `org.yaml`, beside the agents' configs; its path is ORG_FILE (default /app/org.yaml).
Without it, neutral defaults apply: UTC, no holidays, no internal domains, the core's plain brand.

    name: Example Pty Ltd
    timezone: Australia/Hobart          # IANA; windows_timezone is the Exchange/Graph name for the same zone
    windows_timezone: Tasmania Standard Time
    holidays: tas-south                 # a region plan/calendar.py knows, or "none"
    holidays_label: Tasmanian holidays  # how the holiday calendar is named to people
    register_keeper: Ada                # who allocates document numbers ("ask Ada for one")
    library_scope: legislation, codes of practice and similar   # what the offline reference library holds
    internal_domains: [example.com]     # mail from these is internal (mailbox, calendar, files)
    agent_mailboxes: [ada@example.com]  # the agents' own mailboxes: mail between agents is never auto-replied to
    website: example.com
    country_code: "61"                  # phone numbers written locally ("0412 …") are in this country
    call_hours: [7, 21]                 # local hours for calls someone else asked for
    user_agent: "example-agents/1.0 (+https://example.com)"
    sharepoint_url: https://example.sharepoint.com
    apps_url: https://apps.example.com  # where agents publish pages (plans) through the apps publisher
    brand_dir: /app/brand               # the brand pack (brand.py): brand.yaml, Office templates, fonts
    method:                             # the organisation's delivery method, if any (method.py)
      repo: https://github.com/example/method.git
      doc_number: EX-GEN-RPT-0001
      approved_site: https://apps.example.com/method/
      draft_site: https://apps.example.com/method-dev/
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field


class MethodSource(BaseModel):
    repo: str = ""
    doc_number: str = ""
    approved_site: str = ""
    draft_site: str = ""


class OrgConfig(BaseModel):
    name: str = "Example"
    timezone: str = "UTC"
    place: str = ""  # how local time is labelled in messages ("Hobart"); default the time zone
    windows_timezone: str = "UTC"
    holidays: str = "none"
    holidays_label: str = "public holidays"
    register_keeper: str = "the document controller"
    library_scope: str = "legislation, codes of practice and similar"
    calendar_name: str = ""
    admin: str = "the agents' administrator"  # who changes the agents' access and setup ("ask A. Smith")
    admin_first: str = ""  # how people name them in conversation ("ask Ann"); default `admin`
    jira_url: str = ""  # https://<site>.atlassian.net: links from plans to Jira issues
    apps_publish_tool: str = "apps_publish"  # the apps publisher's tool on the `openclaw` MCP server
    progress_phrases: dict[str, str] = Field(default_factory=dict)  # tool -> progress wording, over progress.py  # the working calendar's name in MS Project files; default "<name> <place>"  # what the offline reference library holds
    internal_domains: list[str] = Field(default_factory=list)
    agent_mailboxes: list[str] = Field(default_factory=list)  # the agents' own addresses: never auto-replied to
    website: str = ""
    country_code: str = ""
    call_hours: tuple[int, int] = (8, 18)
    user_agent: str = "agent-core/1.0"
    sharepoint_url: str = ""
    apps_url: str = ""
    brand_dir: str = ""
    method: MethodSource = Field(default_factory=MethodSource)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def org_file() -> Path | None:
    p = os.environ.get("ORG_FILE") or "/app/org.yaml"
    return Path(p) if Path(p).exists() else None


@lru_cache(maxsize=1)
def org() -> OrgConfig:
    path = org_file()
    if not path:
        return OrgConfig()
    return OrgConfig.model_validate(yaml.safe_load(path.read_text()) or {})


def slug() -> str:
    """'acme': the organisation's name as an id prefix (memory banks, directives, tags)."""
    return re.sub(r"[^a-z0-9]+", "-", org().name.lower()).strip("-") or "org"


def admin_first() -> str:
    return org().admin_first or org().admin


def apps_host() -> str:
    """'apps.example.com': the apps publisher's host, as named to people."""
    return org().apps_url.split("://", 1)[-1].rstrip("/") or "the apps site"


def localise(value):
    """Fills {org}, {place}, {holidays_label}, {register_keeper}, {apps_host}, {library_scope} and {method_doc} in
    tool descriptions and fixed
    messages. A plain replace, not str.format, so the JSON-like braces in descriptions are left alone."""
    if isinstance(value, str):
        o = org()
        for key, text in (("{org}", o.name), ("{place}", place()), ("{holidays_label}", o.holidays_label),
                          ("{register_keeper}", o.register_keeper), ("{apps_host}", apps_host()),
                          ("{library_scope}", o.library_scope), ("{method_doc}", o.method.doc_number),
                          ("{admin}", o.admin), ("{admin_first}", admin_first())):
            value = value.replace(key, text)
        return value
    if isinstance(value, list):
        return [localise(v) for v in value]
    if isinstance(value, tuple):
        return tuple(localise(v) for v in value)
    if isinstance(value, dict):
        return {k: localise(v) for k, v in value.items()}
    return value


def tz() -> ZoneInfo:
    return org().tz


def place() -> str:
    """'Hobart': the label for local times in messages."""
    return org().place or org().timezone


class AgentIdentity(BaseModel):
    """Who this process is (one agent per process): set by the app at start from the agent's config."""
    display_name: str = "the assistant"
    owner: str = "the owner"

    @property
    def short_name(self) -> str:
        return self.display_name.split(" (")[0]

    @property
    def owner_first(self) -> str:
        """'Ada' for 'A. Lovelace' isn't knowable; the owner's name as written, without initials."""
        parts = [p for p in self.owner.replace(".", ". ").split() if not p.endswith(".")]
        return parts[0] if parts else self.owner


_AGENT = AgentIdentity()


def set_agent(display_name: str, owner: str) -> None:
    global _AGENT
    _AGENT = AgentIdentity(display_name=display_name or "the assistant", owner=owner or "the owner")


def agent() -> AgentIdentity:
    return _AGENT
