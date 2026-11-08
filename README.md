# agent-core

An engine for AI team agents that work alongside people: they answer in Microsoft Teams chats and meetings, email,
phone and Jira, review GitHub pull requests, and write documents, plans and decks. Each agent is configuration
(an `agent.yaml`, prompts and skills) on this engine; the organisation it works for is one `org.yaml`.

Models run on Amazon Bedrock, or on OpenRouter per tier (below). Every tool call goes through one gateway that applies the agent's allow-list,
confirmations, site and path rules and budgets, and logs it.

## What's in it

| Area | Modules |
|---|---|
| Agent loop | `agent.py` (turns, model tiers and first responder, confirmations, history), `router.py`, `progress.py`, `resume.py` |
| Tool gateway and rules | `tools.py`, `config.py` (`AgentConfig`), `budget.py`, `killswitch.py`, `pricing.yaml` |
| Channels | Teams (`teams.py`, `outreach.py`), email (`mailbox.py`, `mail.py`), calendar (`calendar_tools.py`), phone and voice (`phone.py`, `voice.py`, `voice_llm.py`, `speakers.py`), Jira (`jira_watch.py`), GitHub PR reviews (`github_*.py`), scheduled work (`scheduler.py`) |
| Memory and context | `memory.py` (Hindsight), `store.py` (Postgres), `primer.py`, `directory.py`, `transcripts.py` |
| Documents | Word, Excel and PowerPoint (`docbuild.py`, `sheets.py`, `deck.py`, `filemaker.py`), reading files (`documents.py`, `attachments.py`, `images.py`, `dxf.py`), page previews (`office.py`), vector art (`vector.py`) |
| Plans | `plan/`: scheduling, levelling, sub-plans, overview, deliverables by phase, MS Project XML, web viewer |
| Reference | offline library (`library.py`), the organisation's delivery method (`method.py`), sales facts (`facts.py`), web search (`web.py`) |
| MCP servers | `agentcore_mcp_jira`, `agentcore_mcp_confluence`, `agentcore_mcp_mail` (Exchange), `agentcore_mcp_pipedrive` |
| Skills | `plugins/agent-skills/`: an organisation-neutral skill pack (also a Claude plugin), shipped in the package; see below |
| Also | `debug-console/` (live view of each agent's turns), `teams-media-bridge/` (C#: Teams real-time audio to an ElevenLabs conversation), `charts/` (Helm) |

## A deployment

A deployment is its own repo with an image built `FROM` agent-core:

```
org.yaml                 # name, time zone, holidays, internal domains, URLs, who administers the agents (org.py)
agents/<name>/agent.yaml # identity, models, budget, skills, tools and rules, channels (config.py AgentConfig)
agents/<name>/prompts/   # system.md and friends
skills/<skill>/SKILL.md  # front matter (name, description, applies_to) + instructions; references/ optional
knowledge/               # the offline reference library (optional)
brand/brand.yaml         # palette, fonts, classification, Office templates (brandpack.py; optional)
method/                  # a snapshot of the organisation's delivery method (optional)
chart/                   # Helm: charts/agent once per agent, charts/agent-platform for the shared parts
```

```dockerfile
FROM ghcr.io/sinkers/agent-core:<version>
COPY agents /app/agents
COPY skills /app/skills
COPY knowledge /app/knowledge
COPY brand /app/templates
COPY org.yaml /app/org.yaml
```

### Models: Bedrock or OpenRouter

Each tier names its model. Bedrock is the default; a tier with `provider: openrouter` uses an OpenRouter model id,
with the key in `OPENROUTER_API_KEY` (openrouter.py):

```yaml
models:
  region: ap-southeast-2
  openrouter: {data_collection: deny, zdr: false, providers: []}   # all optional; these are the defaults
  tiers:
    fast:    {provider: openrouter, model_id: openai/gpt-5-mini, max_tokens: 1024}
    default: {model_id: au.anthropic.claude-sonnet-5, max_tokens: 4096}
    deep:    {provider: openrouter, model_id: anthropic/claude-opus-4.1, max_tokens: 4096}
```

Budgets use the cost OpenRouter reports for each call, so OpenRouter models need no `pricing.yaml` entry.
`data_collection: deny` keeps requests away from providers that may train on or keep prompts; `zdr: true` allows
zero-data-retention endpoints only. Bedrock keeps data in the agent's AWS region; OpenRouter makes no such promise.

Without `org.yaml` the engine runs neutral (UTC, no internal domains); without a brand pack, documents use a plain
built-in style. `charts/example` is a one-agent deployment to copy.

## Two ways to run an agent

- **Always on (Helm, `charts/`):** one pod per agent, plus its tool servers. Every channel above works this way, and it's how the engine has been run in real work.
- **Wake on demand (a trial, `deploy/scale-to-zero-poc/`):** a small front door queues every message, call and scheduled task and answers at once with a holding line, then starts a worker in an [NVIDIA OpenShell](https://github.com/NVIDIA/OpenShell) sandbox. The worker answers, waits a while for follow-ups and exits, so a sleeping agent holds no memory. So far it has run one test agent, over Telegram, an API and voice; Teams, email and Jira aren't in it yet. See `docs/design/wake-on-demand.md`.

The whole design, with diagrams and security, is in `docs/design/agent-core.md`.

## Try it

`deploy/test-agent/` is one agent from the engine alone, with no organisation's files and no Microsoft 365. It needs Kubernetes and Amazon Bedrock access (or an OpenRouter key), and you talk to it over `POST /internal/chat`. `charts/example` is the starting point for a deployment of your own.

## Status

Pre-1.0: configuration and APIs may change between versions. Known security gaps are listed in `SECURITY.md`. `TODO.md` is the backlog.

## Skills

The pack in `plugins/agent-skills/skills/` covers documents, reviews, Jira, boards, plans, construction delivery,
contracts, budgets and project accounting, escalation and figures. It is organisation-neutral: `{org}` and the
other `org.yaml` placeholders are filled in when an agent loads a skill. A deployment's own skills are found first;
one with `extends: core` in its front matter adds the deployment's rules to the core skill of the same name rather
than replacing it. The pack is also a Claude plugin (`.claude-plugin/plugin.json`), so people can use the same skills.

## Development

```bash
uv sync --extra test
uv run pytest                      # the core's tests run as an example organisation (tests/fixtures)
docker build -t agent-core:dev .   # the engine image
```

A deployment's own tests use `agentcore.testing` (`FakeModel`, `FakeMcp`, `make_agent`) with its real configs.

The bridge builds with the .NET 8 SDK (`dotnet test teams-media-bridge/tests/TeamsMediaBridge.Tests`); it runs on
Windows Server, where the Teams media SDK is supported.

## Contributing and security

See `CONTRIBUTING.md`. Please report vulnerabilities privately, as `SECURITY.md` describes.

## Licence

MIT (`LICENSE`). The bundled Gelasio fonts are under the SIL Open Font License (`fonts/OFL.txt`).
