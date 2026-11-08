"""Agent configuration: one YAML file per agent.

Nothing about models, tools or skills is hard-coded in agent logic; it all comes
from the agent's YAML plus the shared pricing table.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from .org import org
from pydantic import BaseModel, Field, field_validator, model_validator

Tier = Literal["fast", "default", "deep", "frontier"]
TIERS: tuple[Tier, ...] = ("fast", "default", "deep")  # required; "frontier" is optional


class AgentIdentity(BaseModel):
    name: str
    display_name: str
    role: str
    owner: str
    owner_email: str
    upn: str
    enabled: bool = True  # feature flag: a disabled agent answers health checks only
    # The "<name> is an AI agent. Owner: ..." line under each chat reply. Off where the app already shows it is an
    # AI agent. Emails and messages the agent starts to other people keep it either way.
    chat_footer: bool = True


class TierConfig(BaseModel):
    model_id: str
    max_tokens: int = 4096
    # bedrock (default): a Bedrock model id or inference profile. openrouter: an OpenRouter model id such as
    # anthropic/claude-sonnet-4.5 (openrouter.py).
    provider: Literal["bedrock", "openrouter"] = "bedrock"


class OpenRouterSettings(BaseModel):
    """OpenRouter for tiers with `provider: openrouter` (openrouter.py)."""

    api_key_env: str = "OPENROUTER_API_KEY"
    base_url: str = "https://openrouter.ai/api/v1"
    data_collection: Literal["deny", "allow"] = "deny"  # deny: never to providers that may train on or keep prompts
    zdr: bool = False  # only zero-data-retention endpoints
    providers: list[str] = []  # preferred providers, in order (e.g. [anthropic, amazon-bedrock])
    allow_fallbacks: bool = True  # with `providers`: False means only those
    app_name: str = "agent-core"


REGION_PREFIXES = ("au", "apac", "global", "us", "eu", "jp", "ca", "us-gov")


def family_of(model_id: str) -> str:
    """Model maker for a Bedrock model id or inference profile, e.g. 'au.anthropic.claude-…' -> 'anthropic', or
    an OpenRouter id, e.g. 'anthropic/claude-sonnet-4.5' -> 'anthropic'."""
    if "/" in model_id:
        return {"moonshotai": "moonshot", "x-ai": "xai", "meta-llama": "meta"}.get(model_id.split("/")[0],
                                                                                  model_id.split("/")[0])
    parts = model_id.split(".")
    if len(parts) > 1 and parts[0] in REGION_PREFIXES:
        parts = parts[1:]
    return {"moonshotai": "moonshot"}.get(parts[0], parts[0])


class ReviewerConfig(BaseModel):
    """A model from another maker, used to review work so the reviewer isn't the model that wrote it."""

    model_id: str
    max_tokens: int = 8192
    label: str = ""
    # True for global inference profiles: processed outside Australia (like Fable). Only used when
    # ModelsConfig.allow_global_reviewers is on.
    global_routing: bool = False

    @property
    def family(self) -> str:
        return family_of(self.model_id)


class WebConfig(BaseModel):
    """Web search and public page reading (web.py). The key comes from the environment, never config."""

    # searxng: our own SearXNG in the agents' namespace (charts/agent-platform, no key, no cost). brave, tavily, exa: a paid
    # provider's API with the key in WEB_SEARCH_API_KEY.
    provider: str = Field(default="searxng", pattern="^(searxng|brave|tavily|exa)$")
    searxng_url: str = "http://agents-searxng:8080"
    api_key_env: str = "WEB_SEARCH_API_KEY"
    searches_per_day: int = 100   # per agent; with a paid provider a search costs about US$0.005-0.01
    max_results: int = 6


class EscalationConfig(BaseModel):
    """Escalation from `default` to `deep`."""

    max_tool_steps: int = 8
    urgency_terms: list[str] = Field(
        default_factory=lambda: ["urgent", "important", "pressing", "asap", "high priority"]
    )
    class1_doc_types: list[str] = Field(default_factory=lambda: ["REQ", "RDS", "BOD"])
    class1_terms: list[str] = Field(default_factory=lambda: ["class 1"])


class ModelsConfig(BaseModel):
    region: str = "ap-southeast-2"
    tiers: dict[Tier, TierConfig]
    escalation: EscalationConfig = Field(default_factory=EscalationConfig)
    prompt_caching: bool = True
    openrouter: OpenRouterSettings = Field(default_factory=OpenRouterSettings)
    # First responder: the fast tier answers simple messages itself, or sends a short
    # acknowledgement and hands the work to default/deep/frontier.
    triage: bool = False
    # Independent reviewers from other model makers, in order of preference (review.py).
    reviewers: dict[str, ReviewerConfig] = Field(default_factory=dict)
    allow_global_reviewers: bool = False

    @field_validator("tiers")
    @classmethod
    def _all_tiers(cls, v: dict[str, TierConfig]) -> dict[str, TierConfig]:
        missing = [t for t in TIERS if t not in v]
        if missing:
            raise ValueError(f"missing model tiers: {missing}")
        return v


class BudgetConfig(BaseModel):
    """Per-agent USD budgets."""

    daily_usd: float
    monthly_usd: float
    alert_at: float = 0.8


class SkillsConfig(BaseModel):
    always: list[str] = Field(default_factory=list)
    on_demand: list[str] = Field(default_factory=list)


class McpServer(BaseModel):
    url: str | None = None
    url_env: str | None = None

    @model_validator(mode="after")
    def _one_source(self) -> "McpServer":
        if not (self.url or self.url_env):
            raise ValueError("mcp server needs url or url_env")
        return self


class ToolRule(BaseModel):
    """One allow-listed tool. `confirm` marks destructive tools."""

    server: str
    name: str
    confirm: bool = False
    # Sends, writes or calls anything outside the agent. Blocked by the kill switch.
    writes: bool = False
    # Argument names whose values must already appear in the conversation, otherwise
    # the call needs a confirm (scope check).
    scope_args: list[str] = Field(default_factory=list)
    # SharePoint site scoping: when set, every site argument present (site_args) must name one of these
    # sites, or the call is refused outright (tools.site_allowed).
    sites: list[str] = Field(default_factory=list)
    site_args: list[str] = Field(default_factory=lambda: ["site"])
    # Path scoping inside those sites: when set, the file the call writes (folder_path + filename, or path /
    # dest_path) must match one of these patterns (fnmatch, case-insensitive; "*" also crosses folders), or the
    # call is refused outright. E.g. the document controller may write only a project's register and Working folders.
    paths: list[str] = Field(default_factory=list)
    # Replacing an existing file needs a yes: unconfirmed calls run with this argument forced to false, and an
    # "already exists" refusal becomes a confirm request (the confirmed retry uses the original arguments).
    confirm_overwrite_arg: str | None = None
    # A yes is needed only when this argument is truthy (e.g. plan_to_jira's apply: the preview runs freely).
    confirm_when_arg: str | None = None

    @property
    def qualified(self) -> str:
        return f"{self.server}__{self.name}"


class ToolsConfig(BaseModel):
    allow: list[ToolRule] = Field(default_factory=list)
    # Most tool steps in one turn; at the limit the agent stops calling tools and reports where it got to.
    max_steps: int = Field(default=24, ge=4, le=200)
    # Qualified tool names that may run without an in-conversation confirm.
    standing_rules: list[str] = Field(default_factory=list)


class TeamsConfig(BaseModel):
    app_id_env: str = "MICROSOFT_APP_ID"
    app_password_env: str = "MICROSOFT_APP_PASSWORD"
    tenant_id_env: str = "MICROSOFT_APP_TENANT_ID"


class VoiceConfig(BaseModel):
    """Interim Graph calling + AWS speech."""

    enabled: bool = False
    public_base_url_env: str = "PUBLIC_BASE_URL"
    polly_voice: str = "Olivia"
    polly_engine: str = "neural"
    elevenlabs_voice_id: str | None = None
    elevenlabs_model: str = "eleven_flash_v2_5"
    language: str = "en-AU"
    max_call_minutes: int = 60
    # Names that count as addressing the agent in a meeting (speech-to-text spells names variously).
    wake_words: list[str] = Field(default_factory=list)


class MemoryConfig(BaseModel):
    """Hindsight long-term memory."""

    enabled: bool = False
    url_env: str = "HINDSIGHT_URL"
    # A per-agent token for a memory proxy in front of Hindsight (memproxy.py); unset: no Authorization header.
    api_key_env: str = "HINDSIGHT_API_KEY"
    bank_id: str | None = None  # default <org slug>-<agent name>
    # True: one memory for the whole team; 1:1 conversations are recalled for anyone in the organisation (memory.py).
    shared_across_team: bool = False


# The organisation's own email domains (org.yaml): mail from these is internal.
INTERNAL_DOMAINS = tuple(org().internal_domains)


class MailboxConfig(BaseModel):
    """Incoming email on the agent's own mailbox (mailbox.py)."""

    enabled: bool = False
    poll_seconds: int = 60  # token-free Graph check; the model only runs when new mail arrives
    internal_domains: list[str] = Field(default_factory=lambda: list(INTERNAL_DOMAINS))
    # Other agents' mailboxes: never auto-replied to, so two agents can't email each other in a loop.
    ignore_senders: list[str] = Field(default_factory=lambda: list(org().agent_mailboxes))


class JiraWatchConfig(BaseModel):
    """Answer Jira comments that @mention the agent (jira_watch.py)."""

    enabled: bool = False
    poll_seconds: int = 300  # polling no faster than every 5 minutes
    backfill_hours: int = 36  # the first check looks this far back


class GitHubRepo(BaseModel):
    mode: str = "auto"         # auto: every PR opened, ready or pushed to; label: only PRs labelled `label`
    label: str = "ai-review"


class GitHubLimits(BaseModel):
    reviews_per_day: int = 25
    repo_reviews_per_day: int = 10
    daily_usd: float = 15
    max_inline: int = 15


class GitHubConfig(BaseModel):
    """Automated PR reviews from GitHub webhooks (github_hooks.py): `command` in a PR comment asks a follow-up;
    a PR labelled `skip_label` is never reviewed."""

    enabled: bool = False
    command: str = "/review"
    skip_label: str = "no-ai-review"
    extra_review_tools: list[str] = Field(default_factory=list)  # read tools a review may use beyond github_review
    repos: dict[str, GitHubRepo] = Field(default_factory=dict)
    skip_authors: list[str] = Field(default_factory=lambda: ["dependabot[bot]", "renovate[bot]"])
    exclude: list[str] = Field(default_factory=lambda: ["*.lock", "*package-lock.json", "*.min.*", "vendor/*",
                                                        "dist/*", "*.tgz", "*.snap"])
    review_event: str = "COMMENT"   # or REQUEST_CHANGES (never APPROVE: github_app refuses it)
    # Who drafted a PR when nothing names another AI: the organisation's code is written with Claude,
    # so the review goes to another maker's model. "" treats an unmarked PR as unknown.
    assume_drafted_by: str = "Claude"
    debounce_s: int = 90
    reconcile_minutes: int = 15
    limits: GitHubLimits = Field(default_factory=GitHubLimits)


class FactApprover(BaseModel):
    """Someone who may approve sales facts for customers (facts.py). Matched by any of the ids given."""

    name: str
    email: str | None = None
    aad_id: str | None = None           # Teams / Entra object id
    jira_account_id: str | None = None  # approving on a Jira ticket
    areas: list[str] = Field(default_factory=list)  # product areas; empty = every area


class Sweep(BaseModel):
    """Standing scheduled work (scheduler.ensure_sweeps): what to do, on a local-time cron."""

    name: str
    cron: str
    instruction: str


class ChasingConfig(BaseModel):
    """Chasing people and escalating when they don't respond (escalation.py, the escalation skill).

    A chase counts toward escalation only if it is in `count_channels` and at least `min_hours_between_chases`
    after the last counted one. Once a person has `chases_before_warning` counted chases about one item, on at
    least `min_channels` different channels, within `window_days`, they get a warning (on `warning_channels`)
    with their record. If the item is still open `grace_hours` after the warning, it goes on the overdue
    list, which is emailed to `email_to` and posted in the Teams chat `teams_chat_id`."""

    enabled: bool = False
    count_channels: list[str] = Field(default_factory=lambda: ["teams", "email", "jira", "phone"])
    chases_before_warning: int = 3
    min_channels: int = 2
    min_hours_between_chases: float = 20
    window_days: int = 14
    warning_channels: list[str] = Field(default_factory=lambda: ["teams", "email"])
    grace_hours: float = 24
    email_to: list[str] = Field(default_factory=list)   # who gets the overdue list by email
    cc_people_listed: bool = True                         # the people on the list are copied on the email
    teams_chat_id: str | None = None                      # Teams group chat or channel the list is posted in
    require_approval: bool = True                         # an approver must say send before the list goes out
    approvers: list[FactApprover] = Field(default_factory=list)  # default: the agent's owner
    record_days: int = 90                                 # "N times in the last 90 days" on warnings and the list
    exempt: list[str] = Field(default_factory=list)       # emails never escalated (still chased)
    sweep_cron: str | None = None                         # e.g. "0 9 * * 1-5": the daily escalation sweep


class SalesConfig(BaseModel):
    """The pre-sales agent: who owns the agent day to day, who approves facts for customers, the sweeps."""

    owner: str = ""
    owner_email: str = ""
    owner_aad_id: str | None = None  # sweeps run as, and report to, the owner
    fact_approvers: list[FactApprover] = Field(default_factory=list)
    sweeps: list[Sweep] = Field(default_factory=list)


class PrimerSource(BaseModel):
    """SharePoint folder of Markdown files the agent always knows (primer.py)."""

    site: str
    folder: str
    always: list[str] = Field(default_factory=list)
    on_demand: list[str] = Field(default_factory=list)
    refresh_minutes: int = 60


class CrossChannelConfig(BaseModel):
    """One person across channels (agent.py): in a private conversation, the agent also sees what that person
    said to it recently elsewhere (Teams chat, calls, email, background tasks), so switching from chat to a
    call doesn't start blank. Group chats and meetings are never shown, and never shown into."""

    enabled: bool = True
    hours: int = 24              # how far back
    max_messages: int = 16       # across all other conversations, newest kept
    max_chars: int = 600         # per message


class AgentConfig(BaseModel):
    agent: AgentIdentity
    models: ModelsConfig
    budget: BudgetConfig
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    mcp_servers: dict[str, McpServer] = Field(default_factory=dict)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    teams: TeamsConfig = Field(default_factory=TeamsConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    primer: PrimerSource | None = None
    mailbox: MailboxConfig = Field(default_factory=MailboxConfig)
    jira_watch: JiraWatchConfig = Field(default_factory=JiraWatchConfig)
    sales: SalesConfig | None = None  # pre-sales agent only (facts.py)
    github: GitHubConfig = Field(default_factory=GitHubConfig)
    escalation: ChasingConfig = Field(default_factory=ChasingConfig)  # chasing people (escalation.py)
    web: WebConfig = Field(default_factory=WebConfig)
    directory: bool = False  # daily copy of the staff directory (directory.py); needs Graph User.Read.All
    history_messages: int = 30  # context window
    cross_channel: CrossChannelConfig = Field(default_factory=CrossChannelConfig)

    @model_validator(mode="after")
    def _tools_reference_servers(self) -> "AgentConfig":
        for rule in self.tools.allow:
            if rule.server not in self.mcp_servers:
                raise ValueError(f"tool {rule.qualified} references unknown mcp server {rule.server}")
        known = {r.qualified for r in self.tools.allow}
        for name in self.tools.standing_rules:
            if name not in known:
                raise ValueError(f"standing rule {name} is not an allow-listed tool")
        return self

    @property
    def footer(self) -> str:
        """One-line AI disclosure."""
        return f"{self.agent.display_name} is an AI agent. Owner: {self.agent.owner}."


class Price(BaseModel):
    """USD per million tokens."""

    input: float
    output: float
    cache_read: float = 0.0
    cache_write: float = 0.0
    verified: bool = True


def load_agent_config(path: str | Path) -> AgentConfig:
    with open(path, encoding="utf-8") as f:
        return AgentConfig.model_validate(yaml.safe_load(f))


PRICING_DEFAULT = Path(__file__).with_name("pricing.yaml")  # Bedrock prices; PRICING_FILE overrides


def load_pricing(path: str | Path) -> dict[str, Price]:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return {model_id: Price.model_validate(p) for model_id, p in raw.get("models", {}).items()}
