# agent-core: architecture and design

Status: 4 Nov 2026. The always-on engine runs production agents; the wake-on-demand front door is a PoC on branch
`feature/scale-to-zero-poc` (PR sinkers/agent-core#2). Diagrams follow the C4 model: context, containers, components,
key scenarios, deployment. Detail on the wake-on-demand PoC (measurements, tests) is in `wake-on-demand.md`; the options
behind it are in `docs/plans/scale-to-zero.md`.

Key for every diagram: dark blue = person, blue = agent-core container, light blue = component, grey = external
system, dashed box = boundary (red dashed = untrusted). Arrows read "uses / sends to", labelled with what and how.

## 1. What agent-core is

agent-core is an engine for AI employees: each agent is a Python process with its own identity (Teams bot, mailbox,
phone number, GitHub App), its own database, budget, skills and tool allow-list, driven by a model on Bedrock or
OpenRouter. An organisation builds its own image `FROM ghcr.io/sinkers/agent-core`, adding `agents/<name>/agent.yaml`,
prompts, skills, knowledge, brand templates and `org.yaml`. The engine itself is neutral (a CI check keeps
organisation-specific text out).

Two ways to run it:

| | Always-on (production today) | Wake-on-demand (PoC) |
|---|---|---|
| Process per agent | one pod, always up (~80–300 Mi idle) | none while idle; a sandboxed worker per active agent |
| Entry point | the agent pod's own HTTP endpoints | a shared front door (2 replicas) |
| Database access | direct (own database) | through the front door's work API |
| Isolation | pod security context, NetworkPolicies | OpenShell sandbox: Landlock, per-binary egress proxy, injected credentials |
| Channels | Teams, email, calls, phone, Jira, GitHub, internal API | Telegram, voxhelm chat and calls, HTTP API (Teams and Graph not yet) |

## 2. System context

```mermaid
flowchart TB
  staff["<b>Staff</b><br/>[Person]<br/>Chat, call, email, mention the agent"]:::person
  owner["<b>Agent owner / admin</b><br/>[Person]<br/>Budgets, kill switch, alerts"]:::person
  outside["<b>External caller or emailer</b><br/>[Person, untrusted]<br/>Phone and email from outside"]:::person
  subgraph sys["agent-core [Software System]"]
    ac["<b>agent-core</b><br/>[Software System]<br/>AI employees: answer, act through tools, remember, run scheduled work"]:::system
  end
  m365["<b>Microsoft 365</b><br/>[External]<br/>Teams, Graph mail, calendar, directory, calls, SharePoint"]:::ext
  atl["<b>Atlassian Cloud</b><br/>[External]<br/>Jira, Confluence"]:::ext
  gh["<b>GitHub</b><br/>[External]<br/>PR reviews"]:::ext
  models["<b>Model providers</b><br/>[External]<br/>Amazon Bedrock, OpenRouter"]:::ext
  speech["<b>Speech and telephony</b><br/>[External]<br/>ElevenLabs, Telnyx, AWS Transcribe/Polly"]:::ext
  chat["<b>Chat apps</b><br/>[External]<br/>Telegram, voxhelm (voice-gateway)"]:::ext
  web["<b>Web search</b><br/>[External]<br/>SearXNG or paid search APIs"]:::ext
  crm["<b>CRM</b><br/>[External]<br/>Pipedrive, read-only"]:::ext
  staff -- "Messages, calls, meetings, email" --> m365
  staff -- "Chats and calls" --> chat
  outside -- "Phone calls, email" --> speech
  m365 -- "Bot messages, call events, mail [webhooks, polling]" --> ac
  chat -- "Messages, transcripts [long poll, WebSocket]" --> ac
  speech -- "Custom-LLM turns [HTTPS SSE]" --> ac
  ac -- "Replies, files, events, calls [Graph, Bot Connector]" --> m365
  ac -- "Tickets, pages [REST, OAuth]" --> atl
  gh -- "PR webhooks [HMAC]" --> ac
  ac -- "Review comments [GitHub App]" --> gh
  ac -- "Converse [HTTPS]" --> models
  ac -- "Speech, outbound calls" --> speech
  ac -- "Search, fetch public pages" --> web
  ac -- "Deals, contacts" --> crm
  owner -- "Kill switch, budgets, debug console" --> ac
  ac -- "Budget alerts, messages taken" --> owner

  classDef person fill:#08427b,stroke:#052e56,color:#fff
  classDef system fill:#1168bd,stroke:#0b4884,color:#fff
  classDef container fill:#438dd5,stroke:#2e6295,color:#fff
  classDef component fill:#85bbf0,stroke:#5d82a8,color:#000
  classDef db fill:#438dd5,stroke:#2e6295,color:#fff
  classDef ext fill:#999999,stroke:#6b6b6b,color:#fff
  style sys fill:none,stroke:#1168bd,stroke-dasharray:6 4
```

Every inbound channel is authenticated by the platform before text reaches a model (section 7), and everything an agent
does outside goes through its tool gateway's allow-list and confirm rules.

## 3. Containers: always-on deployment

```mermaid
flowchart TB
  staff["<b>Staff</b><br/>[Person]"]:::person
  m365["<b>Microsoft 365</b><br/>[External]"]:::ext
  el["<b>ElevenLabs</b><br/>[External]<br/>Voice agents, phone"]:::ext
  models["<b>Bedrock / OpenRouter</b><br/>[External]"]:::ext
  atl["<b>Atlassian, Pipedrive</b><br/>[External]"]:::ext
  subgraph cluster["Kubernetes namespace: agents [Boundary]"]
    agent["<b>Agent pod</b> x1 per agent<br/>[Container: Python 3.12, FastAPI, port 8080]<br/>Agent loop, channels, in-process tools, scheduler"]:::container
    mcp["<b>MCP servers</b> (jira, confluence, m365-mail, pipedrive)<br/>[Container: Python, MCP streamable HTTP]<br/>One per agent identity, holds that API's credentials"]:::container
    files["<b>File-service MCP</b><br/>[Container, separate repo]<br/>SharePoint and the apps site"]:::container
    pg[("<b>Postgres</b><br/>[Container: PostgreSQL 17, StatefulSet]<br/>One database and role per agent")]:::db
    hs[("<b>Hindsight</b><br/>[Container: Hindsight 0.10, embedded Postgres, PVC]<br/>Long-term memory, a bank per agent")]:::db
    sx["<b>SearXNG</b><br/>[Container]<br/>Private web search"]:::container
    ks["<b>Kill switch</b><br/>[ConfigMap agents-killswitch]"]:::container
  end
  bridge["<b>Teams media bridge</b><br/>[Container: C# .NET 8, Windows VM]<br/>Answers Teams calls, relays audio"]:::container
  dbg["<b>Debug console</b><br/>[Container: static web page]<br/>Live turn view for viewers"]:::container
  staff -- "Teams chat, calls" --> m365
  m365 -- "Bot messages [HTTPS, Bot Framework JWT]" --> agent
  m365 -- "Calls and media [Graph calling]" --> bridge
  bridge -- "Session, signed URL [HTTPS, bridge token]" --> agent
  bridge -- "Audio [wss, signed URL]" --> el
  el -- "Custom-LLM turns [HTTPS SSE, token]" --> agent
  agent -- "Replies, mail, calendar, directory [Graph, app token]" --> m365
  agent -- "Tool calls [MCP HTTP, bearer]" --> mcp
  agent -- "Files [MCP HTTP, bearer]" --> files
  mcp -- "REST [OAuth client credentials, API token]" --> atl
  agent -- "History, tasks, audit, usage [SQL]" --> pg
  agent -- "Recall, retain [HTTP, no auth today]" --> hs
  agent -- "Search [HTTP]" --> sx
  agent -- "Read every 5 s [k8s API, get only]" --> ks
  agent -- "Converse [HTTPS, SigV4 / bearer]" --> models
  dbg -- "Turn events [WSS, Cognito JWT]" --> agent

  classDef person fill:#08427b,stroke:#052e56,color:#fff
  classDef system fill:#1168bd,stroke:#0b4884,color:#fff
  classDef container fill:#438dd5,stroke:#2e6295,color:#fff
  classDef component fill:#85bbf0,stroke:#5d82a8,color:#000
  classDef db fill:#438dd5,stroke:#2e6295,color:#fff
  classDef ext fill:#999999,stroke:#6b6b6b,color:#fff
  style cluster fill:none,stroke:#1168bd,stroke-dasharray:6 4
```

| Container | Responsibility | Notes |
|---|---|---|
| Agent pod (`app.py`) | All of one agent: channels in, agent loop, in-process tools, background loops | Endpoints in section 7.3; 100m/256Mi request, 4Gi limit (large drawings); 300 s grace to drain turns |
| MCP servers (`agentcore_mcp_*`) | Jira, Confluence, mail sending, Pipedrive behind MCP; credentials stay here, not in the agent | Same image, different module; one Deployment per server per agent identity |
| File-service MCP | SharePoint read/write and publishing to the apps site | Lives outside this repo |
| Postgres | Per-agent databases: messages, tasks, audit, usage, prefs, confirmations | `charts/agent-platform/_postgres.tpl` |
| Hindsight | Extracts and recalls memories (Bedrock Haiku, local embeddings) | No auth today: section 8 fixes it |
| SearXNG | Web search without a paid key | Internal only |
| Teams media bridge | Graph application-hosted media (Windows only); joins meetings, reports speakers | `teams-media-bridge/` |
| Debug console | Live stream of turns, tool calls and costs for named viewers | Secrets redacted at source (`debug.py`) |

## 4. Containers: wake-on-demand (PoC)

```mermaid
flowchart TB
  person["<b>Person</b><br/>[Person]"]:::person
  tg["<b>Telegram</b><br/>[External]"]:::ext
  vg["<b>voxhelm voice-gateway</b><br/>[External, Go]<br/>Speech-to-text, TTS, audio"]:::ext
  br["<b>Amazon Bedrock</b><br/>[External]"]:::ext
  subgraph agents["namespace agents [Boundary]"]
    fd["<b>Front door</b> x2<br/>[Container: Python, FastAPI]<br/>Intake, queue, holding lines, launches workers, delivery, kill switch"]:::container
  end
  subgraph platform["namespace platform [Boundary]"]
    pg[("<b>Postgres</b> x3<br/>[Container: CloudNativePG]<br/>Queue, per-agent schemas, OpenShell state")]:::db
  end
  subgraph os["namespace openshell [Boundary]"]
    gw["<b>OpenShell gateway</b> x2<br/>[Container]<br/>Creates sandboxes, policy, credential injection"]:::container
    subgraph sb["Sandbox [Boundary, untrusted]"]
      wk["<b>Worker</b><br/>[Container: Python, agentcore.worker]<br/>Agent loop on claimed work"]:::container
    end
  end
  person -- "Text, voice notes" --> tg
  person -- "Chat, calls" --> vg
  fd -- "Long poll [bot token]" --> tg
  fd -- "Agent socket [wss, bearer]" --> vg
  fd -- "Queue, state [SQL]" --> pg
  fd -- "Create/delete sandbox [gRPC, mTLS]" --> gw
  wk -- "Claim, results, store calls [HTTP, worker token]" --> fd
  wk -- "Converse [placeholder creds]" --> gw
  gw -- "Converse [real keys, SigV4]" --> br

  classDef person fill:#08427b,stroke:#052e56,color:#fff
  classDef system fill:#1168bd,stroke:#0b4884,color:#fff
  classDef container fill:#438dd5,stroke:#2e6295,color:#fff
  classDef component fill:#85bbf0,stroke:#5d82a8,color:#000
  classDef db fill:#438dd5,stroke:#2e6295,color:#fff
  classDef ext fill:#999999,stroke:#6b6b6b,color:#fff
  style agents fill:none,stroke:#1168bd,stroke-dasharray:6 4
  style platform fill:none,stroke:#1168bd,stroke-dasharray:6 4
  style os fill:none,stroke:#1168bd,stroke-dasharray:6 4
  style sb fill:none,stroke:#c0392b,stroke-dasharray:6 4
```

The worker runs the same `Agent` as the always-on pod, with `RemoteStore` (every store call over the work API) instead of
a database connection. A sleeping agent is a few rows in Postgres. Measured: first answer from sleep 2.1 s with a warm
pool, follow-ups 1.8–2.0 s, node power-off recovered with no lost work, 24/24 sandbox escape tests blocked.

## 5. Components

### 5.1 Inside the agent process

```mermaid
flowchart TB
  subgraph proc["Agent process [Container: agent pod or worker]"]
    ch["<b>Channels</b><br/>[Component: teams.py, mailbox.py, voice*.py, phone.py, jira_watch.py, github_*.py]<br/>Authenticate and turn events into Turns"]:::component
    loop["<b>Agent loop</b><br/>[Component: agent.py, router.py]<br/>Checks, context, first responder, model and tool steps, footer, audit"]:::component
    mc["<b>Model clients</b><br/>[Component: bedrock.py, openrouter.py]<br/>Tiers fast/default/deep/frontier"]:::component
    tg["<b>Tool gateway</b><br/>[Component: tools.py]<br/>Allow-list, confirm rules, site/path rules, MCP clients"]:::component
    inproc["<b>In-process tools</b><br/>[Component: files, plan/, docbuild, sheets, deck, documents, library, method, web, schedule, phone, escalation...]"]:::component
    sk["<b>Skills</b><br/>[Component: skills.py, primer.py]<br/>Always and on-demand instructions"]:::component
    mem["<b>Memory</b><br/>[Component: memory.py]<br/>Recall before, retain after, scope tags"]:::component
    st["<b>Store</b><br/>[Component: store.py or RemoteStore]<br/>Messages, tasks, prefs, audit, usage"]:::component
    bud["<b>Budget</b><br/>[Component: budget.py]<br/>Daily/monthly caps, 80% alert"]:::component
    ksw["<b>Kill switch</b><br/>[Component: killswitch.py]"]:::component
    sch["<b>Scheduler and loops</b><br/>[Component: scheduler.py, resume.py, transcripts.py]<br/>Due tasks, sweeps, call summaries, resume after restart"]:::component
    obs["<b>Observability</b><br/>[Component: metrics.py, telemetry.py, debug.py]"]:::component
  end
  ch -- "Turn" --> loop
  sch -- "Due task as Turn" --> loop
  loop -- "Converse" --> mc
  loop -- "Tool calls" --> tg
  tg -- "Run" --> inproc
  loop -- "Skill text" --> sk
  loop -- "Recall/retain" --> mem
  loop -- "Save, history, audit" --> st
  loop -- "Check and record spend" --> bud
  loop -- "Allowed?" --> ksw
  tg -- "Writes allowed?" --> ksw
  loop -- "Events" --> obs

  classDef component fill:#85bbf0,stroke:#5d82a8,color:#000
  style proc fill:none,stroke:#1168bd,stroke-dasharray:6 4
```

| Component | What it does |
|---|---|
| Agent loop (`agent.py` `Agent.handle`) | Saves the message; checks enabled, kill switch, budget; recalls memory; builds history plus same-person context from other private channels; resolves pending confirmations; first responder (fast tier answers or hands off); up to 24 model/tool steps; adds the platform footer; audits |
| Router (`router.py`) | Escalates default to deep on urgency, controlled-document terms, low self-reported confidence or many tool steps |
| Model clients | Bedrock Converse with prompt caching; OpenRouter via OpenAI-compatible translation (`data_collection: deny` by default); a reviewer pinned to a different maker (`review.py`) |
| Tool gateway (`tools.py`) | Only allow-listed tools; per-tool `writes`/`confirm`, `sites`/`paths`, `scope_args` (recipients must be in the conversation), overwrite and argument-triggered confirms; results wrapped as untrusted data |
| Skills (`skills.py`) | `always` skills in the prompt, `on_demand` via `skills__load`; deployment skills override or extend core ones; owner primer files from SharePoint (`primer.py`) |
| Memory (`memory.py`) | Hindsight bank per agent; `scope:shared` vs `scope:private:<user>`; blocked-content guard; self-capability claims dropped on recall |
| Store (`store.py`) | Postgres per agent (migrations 001–004); `RemoteStore` in workers |
| Budget (`budget.py`) | USD caps per day and month, hard stop at 100%, run-time overrides; an OpenRouter call with no reported cost is refused |
| Scheduler (`scheduler.py`) | Reminders, recurring and background tasks; delivers by Teams, email or call; sweeps (escalation, sales) |
| Documents | Word, Excel, PowerPoint, PDF and drawing readers/writers, brand packs, SVG cleaning, LibreOffice previews, outbox |
| Plans (`plan/`) | Project plans: scheduling and levelling, health checks, MS Project XML, HTML viewer, Jira and Confluence sync |
| Reference | Offline library (SQLite FTS5), method snapshot, approved facts, web search with private-address blocking |

### 5.2 Inside the front door

```mermaid
flowchart LR
  subgraph fdc["Front door [Container]"]
    chn["<b>Channel links</b><br/>[telegram.py, voxhelm.py, speech.py]<br/>Pairing, voice notes, calls"]:::component
    core["<b>FrontDoor core</b><br/>[core.py]<br/>Intake, holding lines, reconcile, delivery, leader lease, warm hold"]:::component
    api["<b>HTTP API</b><br/>[app.py]<br/>Client, worker, admin routes"]:::component
    db["<b>Queue and state</b><br/>[db.py]<br/>Work items, workers, bindings, events"]:::component
    la["<b>Launcher</b><br/>[launcher.py]<br/>OpenShell or local"]:::component
    ks["<b>Kill switch</b><br/>[killswitch.py]"]:::component
  end
  chn -- "intake" --> core
  api -- "claim, result, store calls" --> core
  core -- "SQL" --> db
  core -- "start/stop workers" --> la
  core -- "stopped?" --> ks
  core -- "send replies" --> chn

  classDef component fill:#85bbf0,stroke:#5d82a8,color:#000
  style fdc fill:none,stroke:#1168bd,stroke-dasharray:6 4
```

## 6. Key scenarios

### 6.1 A Teams message to an always-on agent

```mermaid
sequenceDiagram
  autonumber
  participant P as Person (Teams)
  participant T as Teams / Bot Framework
  participant A as Agent pod
  participant DB as Postgres
  participant H as Hindsight
  participant M as Bedrock
  participant X as MCP server / tool
  P->>T: message
  T->>A: POST /api/messages (Bot Framework JWT)
  A->>A: verify JWT, app id, serviceUrl
  A->>DB: save message, check budget, kill switch
  A->>H: recall (2.5 s timeout)
  A->>M: first responder (fast tier)
  alt simple
    M-->>A: answer
  else needs work
    A->>T: short acknowledgement
    loop up to 24 steps
      A->>M: converse (default/deep tier)
      M-->>A: tool use
      A->>A: allow-list, confirm and site rules
      A->>X: tool call
      X-->>A: result (wrapped as untrusted data)
    end
  end
  A->>T: reply with platform footer
  A->>DB: audit, usage
  A-)H: retain (background)
```

A write that needs confirmation stops the loop with a question; the next "yes" in the same conversation runs it
(`pending_confirmations`, 1 hour expiry).

### 6.2 A message to a sleeping agent (wake-on-demand)

```mermaid
sequenceDiagram
  autonumber
  participant U as Person
  participant F as Front door
  participant DB as Postgres
  participant W as Pooled worker (sandbox)
  participant B as Bedrock (via OpenShell)
  U->>F: message (Telegram / voxhelm / API)
  F->>DB: enqueue (dedupe key)
  F-->>U: "Just a moment, I'm waking up"
  F->>DB: assign a pooled worker to the agent
  W->>F: claim (worker token)
  F-->>W: item (lease 60 s, heartbeats)
  W->>F: store calls (history, budget)
  W->>B: converse (placeholder key swapped by proxy)
  W->>F: result
  F->>U: answer
  F->>DB: top the pool up
```

### 6.3 A voxhelm voice call

```mermaid
sequenceDiagram
  autonumber
  participant App as voxhelm app
  participant G as voice-gateway
  participant F as Front door
  participant W as Worker
  App->>G: start call, microphone audio
  G->>F: connect_to_room
  F->>G: state listening; worker held for the call
  G->>G: speech-to-text
  G->>F: transcript
  F->>G: state thinking
  F->>W: work item (channel voice)
  W-->>F: short spoken-style answer
  F->>G: state speaking, llm_response chunks (one sentence each)
  G-->>App: synthesised audio
  F->>G: state listening (after the estimated speaking time)
```

voice-gateway owns audio, speech-to-text, text-to-speech, barge-in and usage caps; agent-core owns states, the queue,
the answer, history and keeping a worker up. A turn with no answer after 5 s gets "Let me check that"; after 20 s an
apology. Teams calls (always-on) use the media bridge and ElevenLabs instead (section 3).

## 7. Security

### 7.1 Principles

1. **Model output is untrusted.** Anything the model wants to do goes through the tool gateway's allow-list and rules;
   writes and outbound messages can require confirmation; tool results are marked as untrusted data.
2. **Credentials live outside the model's reach.** API credentials sit in MCP servers or (wake-on-demand) in the
   OpenShell proxy, never in prompts; workers hold only placeholders and a short-lived token.
3. **Every inbound channel is authenticated by code**, not by the model: JWTs, HMACs, bearer tokens, pairing codes.
4. **Least privilege per agent**: own database and role, own Kubernetes service account (kill-switch read only), own MCP
   server instances, own Hindsight bank (enforced from section 8).
5. **A human can stop everything**: the kill switch blocks writes and outbound actions within 5 s (always-on) or 1.2 s
   (front door); budgets stop spend.

### 7.2 Trust boundaries

```mermaid
flowchart LR
  subgraph net["Internet [Boundary, untrusted input]"]
    in["Teams, email, phone, GitHub, Telegram, voxhelm"]:::ext
  end
  subgraph plat["Platform [Boundary, trusted code]"]
    auth["Channel authentication<br/>(JWT, HMAC, tokens, pairing)"]:::component
    gwy["Tool gateway / front door<br/>(allow-lists, confirms, kill switch, budgets)"]:::component
    cred["Credential holders<br/>(MCP servers, OpenShell proxy, k8s Secrets)"]:::component
  end
  subgraph modelz["Model context [Boundary, untrusted output]"]
    llm["Model reasoning over messages, files, tool results"]:::ext
  end
  in -- "events" --> auth
  auth -- "Turn (text only)" --> llm
  llm -- "tool requests" --> gwy
  gwy -- "allowed calls" --> cred
  cred -- "results, marked untrusted" --> llm

  classDef component fill:#85bbf0,stroke:#5d82a8,color:#000
  classDef ext fill:#999999,stroke:#6b6b6b,color:#fff
  style net fill:none,stroke:#c0392b,stroke-dasharray:6 4
  style plat fill:none,stroke:#1168bd,stroke-dasharray:6 4
  style modelz fill:none,stroke:#c0392b,stroke-dasharray:6 4
```

### 7.3 Inbound authentication (agent pod)

| Endpoint | Check |
|---|---|
| `POST /api/messages`, `/<agent>/api/messages` | Bot Framework JWT (keys, app id, serviceUrl) |
| `POST /<agent>/api/calls` | Bot Framework JWT |
| `POST /<agent>/github/webhook` | HMAC `X-Hub-Signature-256` |
| `/<agent>/voice/session` | `VOICE_BRIDGE_TOKEN` |
| `POST /<agent>/v1/chat/completions` | `CUSTOM_LLM_TOKEN` (ElevenLabs custom LLM) |
| `POST /<agent>/voice/phone-init` | `PHONE_INIT_TOKEN`; unregistered callers get a restricted mode (take a message only) |
| `/internal/*` | `INTERNAL_API_TOKEN`, constant-time compare |
| `WS /<agent>/debug/ws` | Cognito id token, viewer list and Origin check |
| `/healthz`, `/readyz`, `/metrics`, `/audio/<key>.wav` | none (audio keys are short-lived and random) |

The ingress exposes only `/<agent>/api/messages`, the debug socket and configured extra paths. Front door endpoints and
the worker chain are in `wake-on-demand.md` section 5.

### 7.4 Controls in the agent

| Control | Where |
|---|---|
| Tool allow-list; unknown tools refused | `tools.py` |
| Confirm before writes/sends where configured; argument-triggered confirms (replace, apply, external email) | `tools.py`, `agent.yaml` |
| SharePoint site and path rules; refused outright when they don't match | `tools.py` `sites`, `paths` |
| Recipients must already be in the conversation (`scope_args`) | `tools.py` |
| Unverified phone callers: only "take a message" | `tools.py`, `phone.py` |
| Per-turn tool restriction (e.g. PR reviews get read tools only; reviews never approve) | `agent.py`, `github_review.py` |
| Tool results wrapped as `untrusted_data`; security prompt against injected instructions | `tools.py`, `agent.py` |
| Downloads only from Microsoft hosts over HTTPS; web fetch refuses private addresses | `attachments.py`, `web.py` |
| Outgoing HTML escaped; SVG scripts and external links stripped | `emailfmt.py`, `vector.py` |
| Secrets redacted from PR content before any model or memory; debug stream redacts tokens and keys | `github_review.py`, `debug.py` |
| Memory: private scope per person; health, financial, HR and secrets not retained | `memory.py` |
| Budgets: hard stop; unknown cost refused | `budget.py` |
| Audit of every model call, tool call, confirm and reply (12 months) | `store.py`, `001_init.sql` |
| Pods: non-root, read-only root filesystem, no capabilities, seccomp RuntimeDefault; MCP pods without a service-account token | `charts/agent-platform` |

### 7.5 Known gaps

| Gap | Fix | Where tracked |
|---|---|---|
| Hindsight has no auth in the always-on deployment; any agent pod could read or delete any bank | memory-proxy (section 8), built in the PoC; not yet in the always-on charts | TODO.md |
| NetworkPolicies are ingress-only; none for Postgres, no egress policies | Default-deny egress per namespace, Postgres from agents only | TODO.md |
| Budget enforced in the agent process (a compromised worker could skip recording) | Enforce spend at the model proxy or front door | TODO.md |
| Front door: shared client/admin tokens; voxhelm uses an account key | Per-client identities; per-agent voxhelm credential | TODO.md |
| `/metrics` unauthenticated | Restrict by NetworkPolicy to the monitoring namespace | new |
| A new agent needs an image rebuild | Agent config at run time (scale plan §5) | scale-to-zero.md |

## 8. Shared, secure memory (built in the PoC)

```mermaid
flowchart LR
  ag["<b>Agent pod / worker</b><br/>[Container]"]:::container
  oc["<b>Other agent runtimes</b><br/>[External, later]"]:::ext
  subgraph memns["namespace memory [Boundary]"]
    px["<b>memory-proxy</b> x2<br/>[Container]<br/>Per-agent token pinned to one bank; route allow-list"]:::container
    hs["<b>Hindsight</b><br/>[Container, API key, MCP off]"]:::container
    pg[("<b>Postgres + pgvector</b><br/>[CloudNativePG x2, PVCs]")]:::db
  end
  s3[("<b>S3 backups</b><br/>[External: encrypted, versioned, put-only writer]")]:::ext
  ag -- "Recall/retain [HTTP, per-agent bearer]" --> px
  oc -- "Same API [per-agent bearer]" --> px
  px -- "Allowed routes [shared Hindsight key]" --> hs
  hs -- "SQL" --> pg
  pg -- "Base backups and WAL [barman-cloud]" --> s3

  classDef container fill:#438dd5,stroke:#2e6295,color:#fff
  classDef db fill:#438dd5,stroke:#2e6295,color:#fff
  classDef ext fill:#999999,stroke:#6b6b6b,color:#fff
  style memns fill:none,stroke:#1168bd,stroke-dasharray:6 4
```

Built and tested in the PoC (`deploy/scale-to-zero-poc/memory/`): 12/12 in-cluster checks (no/wrong token 401; other bank, delete, list, config, MCP 403; Hindsight and Postgres unreachable directly), memory recalled across conversations, and an encrypted backup restored. Backups need only `s3:PutObject`; the decryption key is kept offline.

Clients keep Hindsight's own paths (`/v1/default/banks/{bank}/...`), so existing Hindsight clients work by pointing at the
proxy with their agent's token. The bank in the path must be the token's bank; only retain, recall, directives, bank
setup and stats are allowed; deletes, bank listing, config, webhooks, import/export and MCP are refused. Workers get the
token as an OpenShell credential placeholder, resolved only for the proxy's host.

## 9. Request preprocessing and fast context (planned)

A local, CPU-only pass in the front door before anything starts (target under 20 ms), detailed in TODO.md:
clean-up (voice fillers, misheard names), spell and typo correction with protected terms (SymSpell), rule-based
detection of simple tool requests and dates, a small local embedding model with trained heads for intent, complexity
(quick / normal / long / not for us) and continuation, plus a contacts store (preferred name, time zone, last topic) so
the holding line becomes "Good morning Sam, let me get onto that for you" or "Back to the octopus question, one moment".
The preprocessor has no tools and cannot act; it only cleans text, labels it and chooses the acknowledgement.

## 10. Deployment

```mermaid
flowchart LR
  subgraph k8s["Kubernetes cluster [Deployment node]"]
    subgraph nsA["namespace agents"]
      a1["<b>agent-NAME</b><br/>[Deployment x1 per agent]"]:::container
      m1["<b>mcp-SERVER</b><br/>[Deployment per server per agent]"]:::container
      p1[("<b>agents-postgres</b><br/>[StatefulSet, PVC 10Gi]")]:::db
      h1[("<b>agents-hindsight</b><br/>[Deployment, PVC]")]:::db
      s1["<b>searxng</b><br/>[Deployment]"]:::container
      ing["<b>Ingress</b><br/>[/AGENT/api/messages, debug socket]"]:::container
    end
  end
  subgraph win["Windows VM [Deployment node]"]
    b1["<b>teams-media-bridge</b><br/>[.NET service, 443 and 8445]"]:::container
  end
  subgraph poc["PoC: 3-node k3s [Deployment node]"]
    f1["<b>frontdoor</b> x2, PDB"]:::container
    o1["<b>OpenShell gateway</b> x2, sandbox controller x2"]:::container
    c1[("<b>CloudNativePG</b> x3")]:::db
    w1["<b>worker sandboxes</b><br/>[0..n, plus warm pool]"]:::container
  end
  ing --> a1
  a1 --> m1
  a1 --> p1
  a1 --> h1
  a1 --> s1
  b1 --> a1
  f1 --> o1
  o1 --> w1
  f1 --> c1

  classDef container fill:#438dd5,stroke:#2e6295,color:#fff
  classDef db fill:#438dd5,stroke:#2e6295,color:#fff
  style k8s fill:none,stroke:#1168bd,stroke-dasharray:6 4
  style nsA fill:none,stroke:#1168bd,stroke-dasharray:6 4
  style win fill:none,stroke:#1168bd,stroke-dasharray:6 4
  style poc fill:none,stroke:#1168bd,stroke-dasharray:6 4
```

- `charts/agent` deploys one agent (ServiceAccount with kill-switch read, Deployment, Service, Ingress, Secret
  `agent-<name>`); `charts/agent-platform` is the library of shared parts (Postgres, Hindsight, MCP servers, SearXNG,
  kill switch, NetworkPolicies); `charts/example` shows an organisation's chart.
- Configuration is baked into the organisation's image (agents, skills, knowledge, brand, `org.yaml`, method); only
  budgets (database) and primer files (SharePoint) change at run time.
- The PoC's manifests and scripts are in `deploy/scale-to-zero-poc/` (`platform.sh`, `frontdoor.sh`, `poc_tests.py`).

## 11. Status

| Area | State |
|---|---|
| Always-on engine (Teams, email, calls, phone, Jira, GitHub, documents, plans, scheduler, memory) | In production for the organisation's agents (copy in step with the original repo; see PORTING.md there) |
| OpenRouter as a model provider | Built; not yet available inside sandboxes |
| Wake-on-demand front door, sandboxes, warm pool, Telegram, voxhelm chat and calls | PoC built and tested; not merged |
| Secure shared memory | Built in the PoC: memory-proxy, Hindsight on CloudNativePG, network policies, encrypted nightly backups (restore tested); Probe's memory on |
| Preprocessing, contacts, triage | Designed; backlog |
| Security gaps in 7.5 | Open; tracked in TODO.md |
