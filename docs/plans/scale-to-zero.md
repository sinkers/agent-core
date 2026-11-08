# Agents that use no memory while idle, at hundreds of agents

Status: design chosen (option 1, workers in OpenShell sandboxes); PoC built and tested 3 Nov 2026 (section 11). Revised 2 Nov 2026 with the owner's answers (below), for a multi-node cluster (section 3) and for OpenShell (section 4).

**Goal.** Hundreds of agents, most of them idle most of the time, each holding no memory until there is work for
it:
- wake on a web request
- run long work as a Kubernetes Job (an ECS task or Lambda on AWS)
- run scheduled work, including schedules an agent sets for itself

The same design should run on our own cluster and on AWS.

**Owner's answers so far (2 Nov 2026):**

| Topic | Answer |
|---|---|
| Waking up | 5–20 s is acceptable. The front door covers the wait: it says "just a moment, waking up", or answers the first message straight from the model |
| Start-up | Make it as fast as possible: MCP servers and everything else load lazily |
| Chat and voice sessions | Look at voxhelm's voice-gateway, which keeps chat sessions open and makes agents look online |
| Which agents stay warm for calls, and when | Configurable per agent |
| Hindsight | May be a shared service, provided agents stay separated from each other |
| Admin setup | Jira and every other admin task is a separate process: we send the admin a link, and a feature matrix maps every such task (section 9) |

## 1. What we measured

| | Idle memory |
|---|---|
| A 3-agent deployment in total | **about 3.3 Gi** |
| Each agent pod | 108–182 Mi |
| Per-agent MCP server pods (8 of them) | about 470 Mi in total |
| Hindsight (local embedding and reranker models, plus its embedded Postgres) | 2.0 Gi |
| Postgres and SearXNG | about 115 Mi each |
| Probe (`deploy/test-agent`, no MCP pods) | 78 Mi |

**Cold start (Probe):** about 3 s from container start to ready. Agents that load a primer, the directory and MCP
servers at start take longer, which is why lazy loading matters (section 6).

**Where the memory goes:** shared services use more than the agents. That shapes the design at scale: the parts
that are always on must be shared, small and grow slowly with the number of agents. Each agent should cost nothing
while idle.

## 2. The design at a glance

```
             Teams / Graph / Jira / GitHub webhooks         app chat, voice (WebSocket / WebRTC)
                              │                                       │
                              ▼                                       ▼
   ┌──────────────── front door (always on, shared, 2+ replicas) ─────────────────┐
   │ check signature/JWT → inbox row (= work item) → 200/202 + "waking up"        │
   │ presence: available = enabled ∧ kill switch off ∧ budget OK (from Postgres)  │
   │ ticker: due schedules → work items       kill switch for every agent         │
   │ launcher: Launcher.start(agent, work_item) → OpenShell sandbox | K8s Job |   │
   │           Lambda | ECS task                                                  │
   │ work API: conversations, messages, usage, results (workers have no DB login) │
   │ bridge turn (optional): first reply straight from the model while it starts  │
   │ token minting: short-lived JWT per worker {org, agent, banks, scopes}        │
   └─────────────────────────────┬────────────────────────────────────────────────┘
                                 ▼
   ┌─ OpenShell sandbox (section 4): Landlock + seccomp, egress only by policy ─────┐
   │ worker (one agent, one conversation or task): start → claim its work item     │
   │   → turn (MCP servers connected on first use) → deliver → stay up N min → exit│
   │ placeholders only: the supervisor's proxy signs Bedrock, adds tokens          │
   └────────────────────────────────┬──────────────────────────────────────────────┘
                                 │
   shared, always on, each 2+ replicas across nodes (section 3):
   Postgres (CloudNativePG, 3 instances) + pgbouncer, Hindsight (slim, authenticated), TEI embeddings,
   SearXNG, object storage (MinIO or S3)
```

**Voice** is the exception. An agent configured to take calls stays warm in its configured hours, and outside them
a call wakes it while the edge greets the caller (section 7).

## 3. Redundancy: a multi-node cluster

We assume at least three nodes. **Losing any one node, or draining it for maintenance, must not stop any agent.**
With scale-to-zero this is mostly about the always-on shared parts, because the workers are short-lived and can run
on any node.

**Principles:**
1. **Every always-on part runs as 2+ replicas on different nodes.** It has `topologySpreadConstraints` or pod
   anti-affinity by hostname, and a `PodDisruptionBudget` of `minAvailable: 1` (2 of 3 for anything with a quorum).
2. **No state on a node's disk** except in a replicated database. Workers keep only scratch files (an `emptyDir`);
   anything kept goes to Postgres or object storage.
3. **Postgres is the coordination point.** Any replica of the front door can do any job, and `SKIP LOCKED`,
   leases and advisory locks make sure each piece of work is done once. There are no in-memory singletons.
4. **Every step can be repeated safely.** A webhook received twice, a Job launched twice, or a worker that dies
   mid-turn must not produce two replies or two emails.
   - Webhooks are de-duplicated by their delivery id.
   - Work items carry an idempotency key.
   - Sends are recorded before they are retried.

### Each part

| Part | Replicas | How it fails over | Notes |
|---|---|---|---|
| **Control plane (k3s)** | 3 server nodes, embedded etcd | etcd quorum survives one node | Workloads can run on the server nodes in a 3-node cluster |
| **Ingress** | Traefik on every node, a floating IP (kube-vip or MetalLB) or an external load balancer | The IP moves to a healthy node in seconds | Webhook URLs (Teams, Graph, Jira, GitHub) point at one DNS name; a node outage must not change it |
| **Front door** | 2–3, spread across nodes | Stateless: any replica takes any request. The ticker and launcher run on whichever replica holds a Postgres advisory lock (leader election), and another takes over within one tick | Webhook receipt is a single `INSERT … ON CONFLICT DO NOTHING` on the delivery id, so a retry hitting another replica isn't processed twice |
| **Chat sessions (WebSocket)** | On the front door | A client whose replica goes away reconnects to another and gets pending messages from the inbox (Postgres). Replicas pass live messages to each other with `LISTEN/NOTIFY` | Nothing about a session lives only in one replica's memory |
| **Voice edge** | 2+ | A call in progress on a failed node drops and the caller has to call back. New calls go to healthy replicas | Live audio can't move between processes. Keep it to "calls survive a drain": on `SIGTERM` stop taking new calls and let current ones finish (grace period ≥ the longest call, or a timed handover) |
| **Workers (Jobs)** | 1 per work item, any node | If a node dies mid-turn, the lease on the work item expires (2–10 min) and the launcher starts it again elsewhere. Retries are capped; then it goes to a dead letter with an alert | The resume logic (`resume.py`) becomes "work item not finished, lease expired: retry or tell the person". A turn that already sent a message isn't sent again |
| **Warm voice agents** | 1 per agent in its hours, as a Deployment | Restarted on another node by Kubernetes (about 30–60 s after the node is marked not ready) | For agents where that gap matters, 2 replicas; the voice edge picks a healthy one |
| **Postgres** | **CloudNativePG**: 1 primary + 2 replicas, one per node, synchronous replication to at least one | Automatic failover to a replica in about 10–30 s; the `-rw` service follows the primary | Local disk on each node is fine because CloudNativePG replicates. Continuous backup (WAL archiving) to object storage, tested restores. The front door and workers retry a dropped connection |
| **pgbouncer** | 2+ (CloudNativePG `Pooler`) | Stateless | Workers connect through it |
| **Hindsight** | 2 API replicas (stateless) + its worker | API: either replica. Worker: Postgres-based task queue, one active at a time | Shares the CloudNativePG cluster (with pgvector) |
| **TEI embeddings** | 2 | Stateless | If both are down, recall fails quickly and the turn goes on without memories (as today) |
| **SearXNG** | 2 | Stateless | — |
| **Object storage** (agent files, backups) | MinIO in distributed mode (4+ drives across nodes), or S3 | Erasure coding survives a node | Today agent files are kept base64 in Postgres. Move them to object storage: it keeps the database small and fast to fail over |
| **Secrets** | External Secrets operator (2 replicas), from 1Password Connect or AWS Secrets Manager | Secrets already synced keep working if the source is briefly unreachable | Workers read their own secret at start |
| **Kill switch** | Postgres row, cached by every front-door replica for 5 s | Fails safe: if a replica can't read it, it dispatches nothing | Engaging it also deletes running Jobs |
| **Images** | One agent image, pre-pulled on every node by a DaemonSet; a registry mirror | A new node pulls once | Avoids slow first starts after a node is replaced |

### Capacity

Size the cluster so that **N − 1 nodes** can carry:
- all the always-on parts
- the warm voice agents
- the peak number of concurrent workers

Workers may cap at, for example, 2 Gi each, with large document and drawing work capped at 4 Gi.

The launcher knows free capacity from the Kubernetes API (or a simple concurrency cap per node). It queues work
rather than starting pods that can't be scheduled. A `PriorityClass` puts:
- voice and warm agents above interactive turns
- interactive turns above scheduled and background work

When a node goes down, background work then waits rather than chat.

### On AWS

The same rules map to managed services:
- **Front door and workers:** Lambda, which spans Availability Zones by itself, or ECS spread across 2–3 AZs.
- **Database:** RDS or Aurora Postgres, Multi-AZ.
- **Storage:** S3.
- **Coordination:** the ticker is one EventBridge schedule (no leader needed), and the work-item lease logic is
  unchanged.

### Testing redundancy

Before calling it done, run these against a cluster with Probe agents and synthetic load:

| Test | Expected |
|---|---|
| Drain each node in turn | Nothing lost; chat answered throughout |
| Power off a node (not drain) | Postgres fails over; work items in flight are retried once; no duplicate replies |
| Kill the front door leader | The ticker resumes on another replica within one tick |
| Restart Postgres primary during 50 concurrent turns | Retries, no lost work items |
| Restore Postgres from backup to a new cluster | Agents come back with their memory and schedules |

## 4. Security: every worker in an OpenShell sandbox

Workers run agent turns that read untrusted text (email, chat, web pages, documents) and call tools. We treat a
worker as something that may be talked into misbehaving, and limit what it could do if it were: **a worker holds no
real credentials, can reach only what its policy allows, and can't see other agents.** We get this from
[NVIDIA OpenShell](https://github.com/NVIDIA/OpenShell) (Apache 2.0), the sandbox runtime under NemoClaw, rather
than building it.

### What OpenShell gives us

| Layer | How |
|---|---|
| **Kernel sandbox** | The worker runs as a non-root user with no Linux capabilities. Landlock limits files (read-write only in `/sandbox` and `/tmp`) and seccomp limits system calls. Needs Linux 6.2+ with Landlock on every node. gVisor or Kata can be added per sandbox (`runtime_class_name`) for a second wall |
| **Deny-by-default egress** | The workload pod can reach only its OpenShell supervisor. The supervisor's proxy checks every connection against the sandbox's policy: host, port, the calling binary, and for HTTP the method and path. For MCP it can check the tool name. Network rules can change while a sandbox runs |
| **No real keys in the sandbox** | The sandbox sees placeholders. The proxy puts the real token into headers on the way out, only for endpoints that provider allows. **Bedrock:** the proxy re-signs each request with SigV4, so the worker's boto3 client has placeholder AWS keys. **Graph:** the gateway refreshes OAuth tokens itself |
| **Secrets store** | The OpenShell gateway keeps provider secrets encrypted in its database, or in Kubernetes Secrets or Vault |
| **Audit** | Every allowed and denied connection is logged (OCSF JSON) |

### What changes in our design

- **The launcher creates OpenShell sandboxes**, not plain Jobs: a gRPC `CreateSandbox` with the agent image, the
  work item in the environment, the agent's policy and providers, and `retention: ephemeral` so the sandbox is
  deleted when the worker exits. The `Launcher` interface is unchanged (`OpenShellLauncher` beside
  `K8sJobLauncher`), so a cluster without OpenShell still works.
- **Workers don't connect to Postgres.** OpenShell can't put a credential into a raw TCP connection, so a worker
  that talked to Postgres would hold a database password. Instead the worker calls the **front door's work API**
  over HTTP (load the conversation, save messages, record usage, finish the work item), with a short-lived token
  the proxy adds. The front door checks that the token's agent owns the data. This also means a compromised
  worker can't read another agent's rows.
- **The model is called through the proxy.** `inference.local` was removed in OpenShell 0.1.0, so the worker calls
  Bedrock directly and the proxy signs. Budgets stay in our code; the front door also totals usage from the work
  API, so a worker can't hide spending.
- **Each agent's policy lives with its configuration** (`policy.yaml` beside `agent.yaml`): Bedrock, the front
  door, its MCP servers and tools, and the outside hosts its tools need (SearXNG, Jira, Graph). Rules use
  `enforcement: enforce` (the default is audit only).
- **One namespace per organisation** for sandboxes, so network rules and quotas separate organisations as well as
  agents.

### Costs and limits

- **Pods per worker:** each sandbox is a workload pod plus a supervisor pod (an experimental option shares one
  supervisor across sandboxes), two Services, a NetworkPolicy, two Secrets and a workspace volume. At hundreds of
  workers that is real load on the API server. The PoC measures it.
- **Start-up:** OpenShell publishes no start-up times, has no warm pool, and calls per-sandbox supervisors slow
  to start. The holding line covers the wait; the PoC measures it.
- **Young software:** 0.x with weekly releases; 0.1.0 broke the API. Pin versions and test each upgrade on the PoC
  cluster first.
- **Same binary, same rights:** policy matches the calling program, and our agent and any code it runs are both
  Python, so tools inherit the agent's network rights. Tools that run code need their own sandbox.
- **AWS:** OpenShell targets Kubernetes. On AWS the same separation comes from Lambda or Fargate isolation, a
  per-task IAM role allowed only Bedrock, and an egress allow-list (AWS Network Firewall or the same proxy).

## 5. Pros and cons at hundreds of agents

### The options compared

| | 1. Front door + Jobs (recommended) | 2. KEDA on Deployments | 3. Knative | 4. Durable-execution engine | 5. AWS only |
|---|---|---|---|---|---|
| Idle memory per agent | 0 | 0 (after cooldown) | 0 | Worker pools stay up | 0 |
| Fixed cost at 300 agents | Front door + shared services, flat | KEDA + interceptor, flat, but **one Deployment, Service and ScaledObject per agent** | Activator, autoscaler, controller + networking; 6 CPU / 6 GB minimum | Engine server + database + workers | Per-request; NAT and RDS dominate |
| Kubernetes objects at 300 agents | About 0 per agent; Jobs come and go | About 1,200 standing objects | About 900+ per agent revision | Small | n/a |
| Agent-created schedules | Postgres rows, no new permissions | Postgres + KEDA scaler query | One PingSource object per schedule | Native, at runtime | EventBridge Scheduler, or Postgres + tick |
| Cold-start handling | Front door answers at once | Interceptor holds the request; no "waking up" message | Activator holds it | n/a | Lambda fast; Fargate 30–45 s |
| Bursts (300 agents scheduled at 09:00) | Launcher queues, caps concurrency, adds jitter | 300 Deployments scale at once | Same | Engine queues | Lambda concurrency limit; Fargate start limits |
| Runs on both backends | Yes (Launcher interface) | Kubernetes only | Kubernetes only | Yes | AWS only |
| Maturity | Plain Kubernetes; our code | Core mature, HTTP add-on beta | Mature, heavy | Mature, another platform | Mature |
| Build effort | Highest (about 5–8 weeks in all) | Lowest (1–2 weeks after polling is removed) | Medium | Medium to high (rewrite the loop) | Medium (one backend of option 1) |

### Option 1: front door plus Jobs

✅ **Idle cost is flat.** The fixed footprint (front door, Postgres, Hindsight, TEI, SearXNG) doesn't grow with the
number of agents. An idle agent is a few database rows, so going from 30 to 300 agents adds storage, not memory.

✅ **One record of everything.** Inbox, schedules, presence, budgets and audit all live in Postgres. The kill
switch, the "only the creator can cancel" rule and the 50-task cap stay in one place, and agents need no permission
to create Kubernetes objects.

✅ **Bursts are controlled centrally.** The launcher holds the queue, so it can cap concurrent workers per agent,
per organisation and per node. It can add jitter so that "every weekday at 09:00" doesn't start 300 pods in the same
second, and slow down when Bedrock starts throttling.

✅ **Heavy work pays only while it runs.** A Job requests memory for that piece of work; a 4 Gi drawing job doesn't
reserve 4 Gi all day.

✅ **The same code on our cluster and on AWS.** Only the `Launcher` and the tick differ.

✅ **Waiting is ours to shape.** The front door can say "waking up", show typing, and (optionally) answer the first
message straight from the model. It meets Graph's 3 s and Teams' 15 s answer limits regardless of cold starts.

❌ **We build and run the front door.** It becomes the one critical component: if it is down, every agent is
unreachable. It needs two or more replicas, health checks, and the same care as the database.

❌ **Cold starts are visible.** The first message after a quiet spell waits 5–20 s, or gets a holding reply. If
workers stay up a few minutes after a turn, conversations stay quick, but each worker uses memory for that time.

❌ **Many short-lived pods stress the cluster.** Hundreds of Jobs a day mean pod churn, image pulls, scheduler load
and more log volume.
  - One shared image with agent configuration loaded at run time (below) means one pull per node.
  - `ttlSecondsAfterFinished` keeps finished Jobs from piling up.

❌ **Failures can loop.** A crash-looping Job, relaunched without limit, burns money and floods the API. A
long-lived agent container service elsewhere ran up 22,784 failed tasks in 18 days this way. The launcher needs a
retry cap per work item, backoff, a dead-letter state and an alert.

❌ **More moving parts for each turn.** Postgres connections, Hindsight tokens and MCP sidecars are now set up per
worker rather than once per pod. Lazy loading (section 6) and pgbouncer limit the cost, but there is more to get
right.

### Option 2: KEDA on today's Deployments

✅ **Quickest way to save memory now.** Little new code, a graduated CNCF project, and the PostgreSQL scaler fits our
task table.

✅ **Good as a stage.** It can scale workers from the same queue that option 1 uses, so the work isn't thrown away.

❌ **Every agent is still a standing Deployment.** At 300 agents that is about 1,200 standing objects, plus 300
configurations to roll out on every release.

❌ **No "waking up" message.** The HTTP interceptor holds the request silently, and the add-on is still beta.
Graph's 3 s rule still needs a separate receiver.

❌ **Kubernetes only, and bursts are uncontrolled.** There is no AWS equivalent, and nothing caps 300 agents scaling
up at 09:00.

### Option 3: Knative

✅ Mature scale-to-zero, with an activator that buffers requests.

❌ Heavy for a small cluster (6 CPU / 6 GB minimum). One object per agent-created schedule. Doesn't cover long jobs.
No AWS equivalent.

### Option 4: a durable-execution engine (Temporal, Hatchet)

✅ Retries, timers and checkpoints, and runtime schedules built in.

❌ Workers are long-running pollers, so idle memory doesn't drop without option 1 or 2 underneath. It adds another
platform to run. The agent loop would need rewriting as checkpointed steps.

### Option 5: AWS only

✅ Pay per request, EventBridge Scheduler for runtime schedules, Lambda containers up to 10 GB.

❌ AWS only. Fargate starts take 30–45 s. Voice still needs something warm. Account quotas cover every agent at
once.

### What 100s of agents needs whatever the option

| Need | Today | At hundreds of agents |
|---|---|---|
| Agent configuration | Baked into the image, so a new agent means a rebuild and redeploy | **Loaded at run time** from Postgres or an object store (agent.yaml, prompts, skills), versioned and validated, with one image for every agent. This is already on the agent-core list ("runtime agent setup") |
| Postgres | One database and role per agent, one instance | One database per **organisation** with a schema per agent, or keep a database per agent behind **pgbouncer**. 300 workers × several connections would exhaust Postgres without pooling |
| Secrets | One Kubernetes Secret per agent | Per-agent secrets in a store (1Password Connect, External Secrets, or AWS Secrets Manager), fetched by the worker at start through its own identity, never put in Job specs |
| Model quotas | One IAM user, plenty of headroom | Bedrock tokens-per-minute quotas apply per account and region **across every agent**. The launcher must back off when throttled, and per-organisation quotas need deciding |
| Teams | One Entra app and bot per agent | Hundreds of apps is heavy for admins. Options: one multi-agent bot per organisation (one app; agents as personas the front door routes to), or keep one per agent and automate creation. A decision for later (section 9) |
| Kill switch | A ConfigMap per agent, read every 5 s by every pod | A row per agent plus one per organisation and one global, read by the front door and checked by workers. Engaging it also deletes running Jobs |
| Observability | Logs per pod | Every log line and trace carries `org`, `agent`, `work_item` and `trace_id`; dashboards per organisation; cost per agent per day from the audit log |
| Budgets | Per agent | Per agent and per organisation, checked before a worker is launched (a refused launch costs nothing) |

## 6. Fast start, and "waking up"

**Target:** a worker answering within about 5 s of launch, with the image already on the node.

**What the front door does while the worker starts:**
1. Within 1 s: acknowledge the webhook, show typing (Teams) or send `state: waking` (app chat).
2. Choose what to say:
   - **Holding line (default):** a short, configurable message such as "Just a moment, I'm waking up." Free, and
     never wrong.
   - **Bridge turn (per-agent flag):** the front door calls the model directly. It sends a slim persona and the
     last few messages, with an explicit instruction that it has no tools or memory, so it may acknowledge,
     clarify or answer something trivial, and must not promise actions. The reply is marked provisional, written
     into the conversation (so the worker sees it and doesn't contradict it) and charged to the agent's budget.
     The risk is a confident answer given without the agent's tools or memory. Ship the holding line first.
3. When the worker has claimed the message, it answers normally.

**What makes the worker start fast:**

| Change | Saves |
|---|---|
| **MCP servers connect on first use**, not at start. Tool lists are cached in Postgres per server and image version, so the model sees every tool from the first turn without any server being contacted | The largest saving; today discovery calls every server one after another |
| MCP servers as native sidecars that start in parallel with the agent, or shared per organisation (section 8) | No per-agent MCP pods left running |
| Primer, staff directory and skills loaded when first needed, from a cache in Postgres | Network calls at start |
| Document libraries (LibreOffice, matplotlib, ezdxf, Office) imported only when a tool needs them | Several seconds of imports for a chat turn that makes no document |
| Bot Framework and Graph tokens cached centrally by the front door (short-lived) | Token round trips |
| Readiness probe every 1 s (today 10 s), or no probe at all for Jobs | Up to 10 s of apparent start time |
| One image for every agent, pre-pulled on each node (a DaemonSet) | Image pulls of 1–2 GB |

**Holding the worker warm.** After a turn the worker stays up for a configurable time (default 10 min) and takes
follow-ups in the same conversation through Postgres `LISTEN/NOTIFY`. During a back-and-forth chat only the first
message waits.

## 7. Chat sessions, presence and voice: voxhelm's voice-gateway

We reviewed voxhelm's `voice-gateway` (Go, about 8.6k lines and 32 test files; last feature July 2026).

**What it does well, and what we can reuse:**
- Voice over WebRTC (LiveKit) or raw PCM over a WebSocket, with speech-to-text, chunked text-to-speech, barge-in
  and visemes.
- A simple message envelope (`chat_message`, `chat_response`, `state_update`, …) with AsyncAPI docs.
- Agents **dial out** to the gateway and register. A newly started worker can therefore attach to a session that is
  already open. That suits cold workers.
- An auth client with caching, a pending-call record with a 60 s TTL (the right shape for "call is being set up
  while the agent wakes"), and usage caps.
- A designed but unbuilt **REST connector**: the gateway POSTs the transcript to a stateless endpoint instead of
  holding a socket to the agent. That is a good fit for workers and for agent-core's existing OpenAI-compatible turn
  endpoint.

**What stops it being used as-is:**
- **"Online" means a live socket.** An agent counts as online only while its WebSocket is connected; otherwise chat
  and calls get 503 and messages are dropped. Nothing is buffered and nothing wakes the agent.
- **One voice call per process.** Room, call handler and usage cap are single fields, and a second call replaces
  the first. Its own docs list "single replica, in-memory session state" as the top production risk.
- **Security:**
  - `POST /room` and `POST /chat` are unauthenticated.
  - Auth turns itself off when its URL is unset.
  - Any origin is accepted.
  - `/room` takes a LiveKit server address from the caller (server-side request forgery).
  - `/call` and `/webclient` mint 6-hour tokens.

**Proposed fit:**
1. **The front door owns sessions and presence for chat.** It stays Python, in agent-core:
   - **Presence:** "available" (what people see as online) = agent enabled, kill switch off, budget OK. It is
     read from Postgres, so hundreds of sleeping agents all show as available.
   - **Warm:** a worker holds a lease. This is only a hint ("may answer faster").
   - **Messages:** go to the inbox, wake the agent, and the client gets `state: waking` straight away.
2. **voice-gateway becomes the voice edge**, after the fixes below. Its text-to-speech accepts text from any
   source, so on a call to a cold agent it can:
   - greet the caller at once
   - keep transcribing while they speak
   - send the first reply from the bridge turn
   - hand over to the worker when it registers

   Agents configured to take calls stay warm in their configured hours, because silence mid-call is not
   acceptable.
3. **Work needed on voice-gateway:**

   | Change | Effort |
   |---|---|
   | Security fixes (service token on `/room` and `/chat`, fail closed, restrict origins, no caller-supplied LiveKit address, remove or flag off the test pages) | 3–4 days |
   | State per session instead of one call per process | 1–2 weeks, the risky part |
   | Inbox and wake hook | about 1 week |
   | REST connector to agent-core turns | 3–5 days |
   | More than one replica (routing through Redis or Postgres `LISTEN/NOTIFY`) | 1–2 weeks |

   **Total: about 4–6 weeks.** Build chat into the front door first (1–2 weeks); bring in voice-gateway for voice
   afterwards.

Also reviewed: voxhelm's `orchestrator` (starts per-user containers on ECS or Kubernetes). Its `run_task` call and
its ECS task-state event handling are worth copying into `EcsRunTaskLauncher`. The service itself keeps containers
running and passes secrets in environment overrides, so it is the opposite of this design. `auth-gateway`,
`llm-router` and `agent-bank` have nothing better than what agent-core already has for agent identity and budgets.

## 8. Shared services: Hindsight, MCP servers

### Hindsight, shared and secure

**Today it is not secure between agents.**
- agent-core calls Hindsight with no credentials and chooses the bank by name. Any agent pod that can reach it can
  read, write or delete any bank.
- The only protection is a NetworkPolicy that admits all agent pods.
- `shared_across_team` makes one agent's whole bank recallable for anyone in the organisation. Per-person privacy
  is enforced only by the agent process, not by Hindsight.

**What Hindsight 1.x offers.** We run 0.10.0. Version 1.0 and later add server-side extension hooks:
- a **tenant extension**, which authenticates each request and chooses a Postgres schema
- an **operation validator** with a check on every read and write of a bank, including recall, retain, delete,
  aliases, webhooks and import
- server-side tag scoping

Per-bank API keys exist only in Hindsight Cloud.

**Proposal: one shared Hindsight with our own extension.**
- **Image and storage:** Hindsight 1.1.x, slim image (no local models; 512 MB minimum, 1 GB recommended), one or two
  replicas kept warm. It moves off its embedded Postgres onto the shared Postgres (with pgvector).
- **Identity:** the front door mints a short-lived JWT (Ed25519) for each worker:
  `{org, agent, banks: [own (rw), team (r)], exp}`. It works the same for Kubernetes Jobs and for Lambda/ECS.
- **Tenant extension:** checks the token and sets the schema to `org_<slug>`. Each organisation's memories are then
  in a separate schema: customers are separated by the database, not only by code.
- **Operation validator:**
  - allows only the agent's own bank, plus the organisation's team bank read-only, and write only where the agent
    is configured to share
  - refuses aliases, webhooks, bank deletion and import from agents (admin only)
  - lists only the agent's own banks
- **Defence in depth:** keep the NetworkPolicy. Give Hindsight its own IAM role, limited to the `au.` Haiku profile
  and the embedding model.

About 150–250 lines of extension, plus a custom image. Hindsight releases every week or two, so pin the version and
run the extension's tests on every upgrade.

**Alternatives:**

| Alternative | Verdict |
|---|---|
| An external authorizing proxy | Brittle: 100+ routes, aliases and an MCP endpoint, and one missed route leaks data. Useful as an extra gateway, not as the only control |
| One Hindsight per organisation | Strongest isolation, but about 0.3–0.5 Gi idle each. Fine for tens of organisations, wasteful for hundreds |

### Embeddings and data residency (question 3, elaborated)

Hindsight uses models in two places:
- **An LLM** to extract facts from each conversation and to consolidate them (and for `reflect`). Ours is Bedrock
  Haiku 4.5 through the `au.` inference profile. AWS states that requests from Sydney route only to Sydney or
  Melbourne. **This already stays in Australia.**
  - Trap: agent-core's chart falls back to `us-east-1` when no region is set. Make the region required.
- **An embedding model** to turn each fact and each query into a vector, and optionally a **reranker** to reorder
  the results. Today both run inside the Hindsight pod (`bge-small-en-v1.5` and a MiniLM cross-encoder), which is
  why it uses 2 Gi.

**The choice is where those two models run.** Every option must keep text in Australia.

| Option | Stays in Australia | Idle memory | Cost | Recall quality | Re-embed stored memories? |
|---|---|---|---|---|---|
| **Shared TEI server with the same `bge-small-en-v1.5`, reranker → `rrf`** (recommended start) | Yes, in our cluster | TEI about 0.2–0.4 Gi, shared by everyone (estimate) | CPU only | Same embeddings as today. Losing the cross-encoder lowers precision somewhat (unmeasured) | **Probably not.** Same model; check on a sample that the vectors match |
| Same, plus a TEI reranker (MiniLM or bge-reranker-base) | Yes | About 0.3–1 Gi more | CPU only | As today | No |
| Bedrock Titan Text Embeddings V2 (Sydney, in-region) | Yes | 0 | About US$0.02 per 1M tokens (unverified) | Not benchmarked against today's model | **Yes.** Ties our own cluster to AWS |
| Bedrock Cohere Embed English v3 (Sydney, in-region) | Yes | 0 | About US$0.10 per 1M tokens (unverified) | Generally stronger than bge-small on public benchmarks | **Yes** |
| Cohere Embed v4 on Bedrock, OpenAI, OpenRouter | **No.** v4 is offered in Sydney only through global routing | 0 | — | — | Ruled out |

Rerankers on Bedrock (Amazon Rerank, Cohere Rerank) aren't available in Sydney, so reranking is either `rrf` (no
model) or self-hosted in TEI.

**What re-embedding means.**
- Each embedding model produces its own kind of vector, so switching model makes every stored vector meaningless to
  new queries. Every stored fact must be embedded again with the new model.
- The facts themselves are kept: the LLM doesn't re-extract them.
- Hindsight does this with export and import between an old and a new instance.
- The model is a server-wide setting, so a shared Hindsight switches **every bank of every organisation at once**.
  That is the main reason to choose the long-term model now, while there are three agents rather than three
  hundred.

Rough sizes:
- Today: a few thousand facts. Re-embedding costs cents and takes minutes.
- 500 agents × 20,000 facts: about 600M tokens, roughly US$12 on Titan or about 3 hours on one TEI server, plus
  Bedrock quota limits.

**Recommendation:**
1. Move to a shared TEI with the same model and `rrf` now: no re-embedding, about 1.5 Gi saved.
2. Test recall quality against Cohere Embed v3 on Bedrock on a copy of real banks.
3. If Cohere v3 is clearly better, switch once, before the agent count grows.

**Decision needed:** stay on our own TEI (no AWS dependency for memory on our own cluster), or accept a Bedrock
embedding model in Sydney?

### MCP servers

Today each agent has its own pod per MCP server, holding that agent's credentials (about 60 Mi each, always on). At
hundreds of agents:

| Option | How | Pros | Cons |
|---|---|---|---|
| **Sidecars in the worker pod** | Native sidecars, started with the worker and stopped with it | Credentials stay per agent, as today | About 1–2 s more start-up per worker (in parallel with the agent's own start) |
| **One shared server per kind** (Jira, mail, Confluence) per organisation | The worker's JWT says which agent is calling; the server picks that agent's credentials | One pod per kind per organisation | Credentials of many agents in one process; needs the same token checks as Hindsight |

Recommendation: sidecars first (no change to the security model). Shared servers later for the busiest kinds, with
token checks.

## 9. Admin setup: feature matrix

Every capability that needs an administrator outside our cluster. The aim is that setting one up is a link we send
to the right admin, plus a check that it worked. The admin portal (to build) lists each item per organisation and
per agent with its state: not started, link sent, done, failing.

| Capability | Admin needed | Done once per | What we send | After setup, automatic | How it wakes the agent |
|---|---|---|---|---|---|
| **Teams chat** | Entra app admin (app and bot registration); Teams admin (allow or publish the app) | Agent (one bot per agent today), or organisation (one multi-agent bot, to decide) | Admin consent link `login.microsoftonline.com/<tenant>/adminconsent?client_id=…`; Teams admin centre link to approve the app; app package to upload | Proactive install for users (`TeamsAppInstallation.ReadWriteSelfForUser.All`) | Bot Framework webhook to the front door |
| **Staff directory and people lookup** | Global or privileged role admin: `User.Read.All` (or `User.ReadBasic.All`) | Organisation | Admin consent link | Daily directory sync (cron task) | n/a (scheduled) |
| **Email: read and send from own mailbox** | M365 user and licence admin (mailbox); Exchange admin (RBAC for Applications, scoped to the agent's mailbox) | Agent | Mailbox request; PowerShell script for the Exchange scope (no link exists for this) | **Graph change notifications** created and renewed by the front door | Graph webhook to the front door (3 s answer) |
| **Calendar and meeting joins** | As email, plus calendar permissions for the agent's mailbox | Agent | As above | Meetings become one-off scheduled tasks | Scheduled task at start time |
| **Teams calls (voice)** | Global admin: `Calls.*` application permissions; bot calling webhook | Agent | Admin consent link | — | Calling webhook; agent kept warm in its configured hours |
| **Phone number** | Telephony or ACS owner; voice vendor account | Agent | Vendor setup steps | — | Phone webhook to the voice edge |
| **Jira** | Atlassian org admin: service account and OAuth client credentials; **Jira admin: webhook** | Agent (credentials); organisation (one webhook for all agents' projects, routed by the front door) | Service account request with the exact scope list; link to Jira system webhooks with the URL, events and JQL to paste. Or, if the credential has `manage:jira-webhook`, a dynamic webhook registered by us (to verify) | Webhook refreshed before expiry if dynamic | Jira webhook to the front door |
| **Confluence** | Atlassian org admin: OAuth credential | Agent | Scope list | — | n/a (used in turns) |
| **GitHub PR reviews** | GitHub org owner: install the GitHub App | Organisation (repositories chosen at install) | Install link `github.com/apps/<app>/installations/new` | — | GitHub webhook to the front door |
| **SharePoint / OneDrive files** | SharePoint admin and global admin (`Sites.Selected` per site) | Organisation and site | Admin consent link plus a per-site grant script | — | n/a |
| **CRM (Pipedrive)** | Pipedrive admin: API token or OAuth app | Organisation | OAuth install link | — | Optional CRM webhook |
| **Models (Bedrock)** | AWS admin: model access, IAM role or user per organisation, region | Organisation (deployment) | IAM policy to apply (Terraform) | — | n/a |
| **Public front door** | DNS and infrastructure admin | Deployment | DNS record and certificate | — | Receives every webhook |
| **Web search** | None (our own SearXNG); optional paid provider key | Deployment | — | — | n/a |

Each row becomes a setup step with:
- a generated link or script
- the exact permissions requested, written for the admin, with the reason
- a check that it worked (for example a consent present, a webhook delivered, a test email received)
- an expiry reminder (client secrets, webhooks, subscriptions)

Missing setup shows on the agent's status page; the agent itself says what's missing instead of failing.

## 10. Where schedules live

Postgres stays the only record of schedules. One ticker reads it: the front door on our own cluster, or one
EventBridge `rate(1 minute)` schedule on AWS. The shortest repeat is 15 minutes.

Agent-created schedules never become Kubernetes CronJobs or per-schedule EventBridge objects. That would mean giving
agents power to create pods, splitting the record, and re-implementing the creator and 50-task rules elsewhere.

At hundreds of agents the ticker also spreads load:
- jitter on popular times
- per-organisation concurrency caps
- backoff when Bedrock throttles

## 11. Stages

Each stage ships on its own and keeps today's agents working.

| Stage | Work | Effort |
|---|---|---|
| 0 | **Cluster foundation:** 3-node k3s (embedded etcd), floating ingress IP, CloudNativePG (3 instances, backups to object storage, restore tested), MinIO or S3, External Secrets, image pre-pull; move today's shared parts onto it | 1–2 weeks |
| 0b | **Hindsight:** 1.1.x slim on the shared Postgres, shared TEI with the same model, `rrf`; then the auth extension and worker tokens | 1–2 weeks |
| 1 | **Fast start:** lazy MCP connection with cached tool lists, lazy primer, directory and document imports; `agentcore.worker` entry point (one work item, stay up N min, exit); readiness 1 s; Probe as the test bed | 1–2 weeks |
| 2 | **Runtime agent configuration:** agents loaded from Postgres or an object store, one image for all | 1–2 weeks |
| 3 | **Events instead of polling:** Graph mail subscriptions, Jira webhook, GitHub debounce and reconcile as tasks, timers as cron tasks; still inside today's pods | 1–2 weeks |
| 4 | **Front door:** webhooks, inbox, presence, holding line, ticker, kill switch, launcher with retry caps, 2+ replicas with leader election and de-duplicated webhooks; agents keep running and take work from the queue (shadow mode) | 2–3 weeks |
| 5 | **Jobs in sandboxes:** `OpenShellLauncher` (and `K8sJobLauncher` for clusters without it), the worker work API, per-agent policies and providers, MCP sidecars, pgbouncer; agents without voice go to zero | 2–3 weeks |
| 6 | **Setup portal:** the feature matrix as setup steps with links and checks | 2 weeks |
| 7 | **Voice:** voice-gateway fixes and REST connector; warm hours per agent; bridge turn behind a flag | 4–6 weeks |
| 8 | **AWS backend:** `LambdaLauncher` / `EcsRunTaskLauncher`, tick Lambda, Multi-AZ RDS; Probe first | 2 weeks |
| Every stage | **Redundancy tests** from section 3 (drain, power-off, leader kill, database failover) before the stage counts as done | — |

### The PoC (started 2 Nov 2026)

A standalone environment, apart from today's agents: three KVM virtual machines running k3s as three servers with
embedded etcd (`deploy/scale-to-zero-poc/cluster.sh`). It proves the design end to end on Probe:

| # | Work | Days |
|---|---|---|
| 1 | Platform: Agent Sandbox controller, CloudNativePG (3 instances), OpenShell gateway (2 replicas on Postgres), SearXNG | 1–2 |
| 2 | OpenShell checks: sandbox start time, direct egress blocked, boto3 Bedrock through SigV4 re-signing, pods and volumes per sandbox | 1–2 |
| 3 | Worker entry point (`agentcore.worker`): one work item, stay up N min, exit; lazy MCP and imports; store over the work API | 3–4 |
| 4 | Front door: chat endpoint with holding line, inbox, work API, ticker, kill switch, `OpenShellLauncher` with retry cap, backoff, dead letter and concurrency cap | 4–5 |
| 5 | Tests and write-up: the success criteria below, and the attack tests | 2–3 |

**Success criteria:** Probe uses 0 Mi while idle; holding line in under 1 s; cold answer in under 10 s (target
5 s); warm follow-up in under 3 s; a schedule Probe sets fires and delivers; a task longer than 15 minutes
completes; a crash-looping worker stops after 3 tries; 20 schedules due at once start with jitter under the cap;
the kill switch stops running workers; a node powered off loses no work.

**Attack tests:** from inside a worker, try to reach a host not in its policy, reach Postgres or another worker,
read another agent's conversation through the work API, find a real credential anywhere in the sandbox, and write
outside `/sandbox` and `/tmp`. Each must fail and be logged.

**Platform checks (2 Nov 2026)**, OpenShell 0.1.6 on the 3-node PoC cluster:

| Check | Result |
|---|---|
| Platform | Agent Sandbox v1.0.9, CloudNativePG 1.30.1 (3 instances, one per node), OpenShell gateway with 2 replicas on that Postgres, image registry. A separate sandbox namespace needs the gateway's client TLS secret copied into it (`platform.sh` does this) |
| Inside a sandbox | uid 10001, no capabilities, `NoNewPrivs`, seccomp filtering; `/etc` and `/usr` read-only; `/sandbox` and `/tmp` writable |
| Egress with the default policy | Internet, Postgres, the Kubernetes API, cloud metadata and the gateway all blocked |
| Second wall | From the pod's own network namespace (outside OpenShell's syscall filter), k3s network policy also blocks all of the above |
| Bedrock through the proxy | boto3 `converse` (0.6 s) and `converse_stream` (0.5 s) work with placeholder keys only; the proxy signs. `InvokeModel` (not in the policy) gets 403; STS and the Bedrock control plane are unreachable. No `AWS_CA_BUNDLE` needed |
| Start-up, images cached on the node | Ready in 10–14 s (about 4–5 s of it before the pod is allocated); first `exec` 0.2 s later |
| Start-up, image not on the node | 48 s for the 479 MB agent image; pre-pulling on every node is required |
| Per sandbox | A workload pod (about 5 Mi idle) and a supervisor pod (about 14 Mi), 2 Services, 2 Secrets, a 1 Gi volume, a network policy. Gateway replicas 50–90 Mi each |
| One-shot `--no-keep` from the CLI | The command was sent before the sandbox was ready and failed; the launcher should create, wait for ready, then start the worker |

**Results, end to end (3 Nov 2026).** Items 3 and 4 are built (`agentcore.worker`, `agentcore.frontdoor`; how to
run them: `deploy/scale-to-zero-poc/README.md`). Probe runs only as workers in OpenShell sandboxes, reached by an
API client, Telegram (text and voice notes) and voxhelm chat. Measured with `poc_tests.py`:

| Criterion | Target | Result | |
|---|---|---|---|
| Memory while idle | 0 Mi | 0: no worker, no sandbox pods, 10 min after the last message | pass |
| Holding line | < 1 s | 10–16 ms to queue and answer the API; Telegram shows it about 0.5–1.1 s later (Telegram's own API) | pass |
| Cold answer | < 10 s (target 5 s) | **2.2 s with a warm pool** (below). Without one, 10.7–14.7 s: sandbox ~8–10 s, Python and imports ~2 s, Probe built ~1 s, turn ~2 s | pass (pool) |
| Warm follow-up | < 3 s | 1.8–2.0 s through the API (one model turn); 3.2–3.5 s to the phone on Telegram | pass |
| Probe's own schedule fires and is delivered | | set through the model for 2 min later; ran in a worker and was handed over for delivery | pass |
| 20 schedules due at once | under the cap | all 20 done in 34 s; due times spread over 0.3–9.9 s; never more than 2 workers (Probe's cap) | pass |
| Crash-looping worker | stops after 3 tries | 3 tries, then dead-lettered with an apology to the person | pass |
| Kill switch | stops running workers | worker cut off (ended, token revoked, work requeued) 1.2 s after the flip; its sandbox pods gone after ~36 s | pass |
| Task longer than 15 minutes | completes | a 16-minute task finished on one worker in one attempt (heartbeats every 15 s keep its 60 s lease); another ran on through a front door redeploy: workers don't depend on the replica that started them | pass |
| Node powered off mid-task | no work lost | two runs: the task came back after its 60 s lease and finished on another node (85 s and 138 s after the power-off). In the second the leader was on that node: the other replica took over within the 15 s lease and reconnected Telegram and both voxhelm sockets (after the two fixes below) | pass |

**Attack tests**, run from inside a worker's sandbox (24 checks, all as intended):
- Blocked: the internet (through the proxy and directly), the Bedrock control plane, STS, the Kubernetes API, cloud
  metadata, the OpenShell gateway, Postgres by name and by address.
- The work API: the front door's admin and client APIs refused (403); a store call outside the allow-list refused
  (403, so a worker can't claim schedules); a forged worker token refused (401); a path escape refused.
- No real AWS key in the environment (placeholders only) or in any of 534 readable files.
- Writes refused everywhere except `/sandbox` and `/tmp`.
- A credential placeholder stored through the work API comes back unresolved: the proxy fills in credentials only
  for the endpoint they are bound to. A malformed placeholder sent anywhere is refused (403, fail closed).
- Not possible to test in a single-agent PoC, but true by construction: a worker's token is bound to one agent, and
  the work API has no way to name another agent's data.

**Found and fixed on the way:**
- **Leader election:** a Postgres advisory lock held by a replica on a node that loses power stays held until TCP
  gives up (hours by default). Replaced by a 15 s lease renewed every 3 s; a replica that can't renew steps down.
- **The kill switch was slow (32 s):** deleting a sandbox waits for its pods to go, and the ticker waited for the
  delete. Workers are now ended first and sandboxes deleted in the background; the worker also handles SIGTERM.
- **Races:** a follow-up sent the instant an answer appeared could start a second worker; fixed by marking the
  worker ready before the item done, and reading workers before the queue.
- **A blocking worker looks dead:** synchronous work on the worker's event loop stopped its heartbeats, and the front
  door (correctly) replaced it. Blocking work must run in a thread.
- **OpenShell:** the worker runs as the sandbox's own process (`command` in the sandbox spec), so it doesn't depend
  on the front door replica that started it. The Python SDK takes the policy as a message: the CLI's YAML converts
  with two renames (`filesystem_policy`, enum prefixes). An exact hostname may reach a private address (the front
  door's Service); `allowed_ips` pins it to the service range.
- **Power-off (first run failed):** the Agent Sandbox controller ran as one replica, on the node switched off, so
  no sandbox could start until Kubernetes moved it (5 min). It now runs as two, on different nodes (it elects a
  leader). And the front door counted the gateway's timeouts during the failover as failed worker starts, hit
  the cap and dead-lettered the work: launcher errors now back off (up to 60 s) and keep the work queued; only
  workers that start and never come up count towards giving up.
- **Idle workers for blocked work:** work waiting behind a turn in its own conversation started a worker that could
  not take it; such work no longer counts as waiting.
- **voice-gateway showed Probe offline after the failover** although the front door had reconnected: closing the
  old socket removed the new connection's entry on the gateway (a race in voice-gateway, now fixed there in
  dev-67613a1). The front door side was already right (reconnect, own pings).
- **Telegram:** no public endpoint needed (long polling from the leader). Pairing codes now last 60 minutes and the
  bot says when one has expired.

**Speech (Telegram voice notes):** the front door transcribes (ElevenLabs Scribe, 0.6 s for a short note) and
speaks the reply (ElevenLabs, then ffmpeg to Ogg Opus: 3.7 s), so workers need no speech provider or its key.

**voxhelm:** the front door holds Probe's agent socket on voice-gateway (the PoC's own copy, and voxhelm's dev
gateway as a standalone agent record, tested from the voxhelm app: 2.2 s warm), so Probe shows online while asleep and a
message wakes it (holding line, then the answer: 11.9 s cold, 1.9 s warm). This removes voice-gateway's "online
means a live socket" limit without changing voice-gateway. Its other gaps (section 7) remain; it runs in the PoC
inside the cluster only, text chat only.

**Where a cold answer's time goes** (one measured through voxhelm dev from the app: holding line 0.1 s, answer
14.7 s, follow-up 2.0 s):

| Step | Seconds |
|---|---|
| Front door: CreateSandbox call to OpenShell | 1.0 |
| OpenShell holds the pod until its supervisor and config are ready | 4.3 |
| Pod set-up (init containers, network) until the worker starts | 5.0 |
| Python start and imports, then the agent built | 2.1 |
| Claim | 0.1 |
| Model turn (Bedrock) | 2.1 |

Sandbox creation is 10 s of it (8–9 s in quieter runs).

**Warm pool (3 Nov 2026, approved by the owner).** Workers start ahead of time with no agent, import what they
need, and wait at the front door; when work arrives for an agent with none of its own, the front door gives it a
pooled worker (which builds the agent in ~0.4 s and claims the work) and starts a replacement in the background.
A sandbox's policy and credentials are fixed when it is created, so pools are per profile (image, policy,
providers), and each worker serves one agent for its whole life: an agent never inherits another's sandbox.

| | Before | With a pool of 1 |
|---|---|---|
| Cold answer | 10.7–14.7 s | **2.2 s** (holding line 12 ms; assignment, agent built and claim 0.4 s; turn 1.8 s). From the voxhelm app through its dev gateway: holding line 0.1 s, answer 2.1 s, follow-ups 1.8–2.0 s |
| Warm follow-up | 1.8–2.0 s | 1.8 s |
| Cost while idle | 0 | 51 Mi per waiting pooled worker, shared by every agent with that profile |

A pooled worker's token can do nothing but wait for an assignment (409 otherwise). Pool size is the number of
agents expected to wake at once within one sandbox start (~10 s); beyond it, agents fall back to a fresh sandbox
(the old path), so a burst is slower but never fails.

**Shared supervisor** (`supervisor_topology: gateway`, two members): no faster to start (ready ~9 s either way;
the 4–5 s hold is OpenShell preparing the sandbox's configuration, not starting its supervisor), but one pod per
sandbox instead of two, and two supervisors (25–47 Mi in all) instead of ~14 Mi per sandbox. Kept; still marked
experimental by OpenShell.

**What it means.** The design works end to end and holds up under the failure and attack tests. Without a pool the
cold answer missed the 10 s target (10.7–14.7 s); a pool of one waiting worker brings it to 2.2 s. Other ways to
shorten a fresh sandbox start, for agents outside a pool:
1. A small pool of ready, unassigned sandboxes per node, given an agent's configuration when work arrives (saves
   the ~7 s sandbox start; needs runtime agent configuration, stage 2).
2. OpenShell's shared supervisor (`supervisor_topology: gateway`, experimental): one pod per sandbox instead of two.
3. A slimmer worker image and start: no LibreOffice for agents that make no documents; lazy imports (~1–2 s).

**Models through OpenRouter (3 Nov 2026).** Any tier can now name an OpenRouter model (`provider: openrouter`),
with budgets taken from OpenRouter's reported cost per call and `data_collection: deny` by default. For workers
in sandboxes this needs an OpenShell provider and policy entry for `openrouter.ai` (the key injected by the proxy,
like the AWS keys): not done in the PoC, which uses Bedrock.

## 12. Decisions still needed

1. **Embeddings:** our own TEI only, or a Bedrock embedding model in Sydney (Cohere Embed v3 or Titan V2)? Should we
   run the recall test before deciding?
2. **Teams at scale:** one bot per agent (as today), or one multi-agent bot per organisation?
3. **Tenancy:** several customer organisations on one deployment (schema per organisation in Postgres and
   Hindsight), or one deployment per customer?
4. **Bridge turn:** allowed at all, or the holding line only?
5. **Front door exposure:** one public endpoint for all agents and organisations?
6. **Second wall:** gVisor or Kata under OpenShell for every worker, or only for agents that run code?

**Not yet verified:**
- slim Hindsight and TEI memory on our nodes
- whether TEI's vectors match today's closely enough to skip re-embedding
- how much recall `rrf` loses compared with the cross-encoder
- Bedrock embedding prices
- whether an Atlassian service account with `manage:jira-webhook` can register dynamic webhooks
- KEDA's current footprint
- start-to-ready time for a full agent with lazy loading
- OpenShell: sandbox start time and pods per worker at 100–300 workers (the PoC ran at most 3 at once)

## Sources

**Scale-to-zero and AWS:**
- KEDA: [PostgreSQL scaler](https://keda.sh/docs/2.18/scalers/postgresql/),
  [ScaledJob](https://keda.sh/docs/2.18/reference/scaledjob-spec/),
  [HTTP add-on](https://github.com/kedacore/http-add-on)
- [Knative install](https://knative.dev/docs/install/yaml-install/serving/install-serving-with-yaml/)
- Microsoft Graph: [change notifications](https://learn.microsoft.com/en-us/graph/change-notifications-overview),
  [webhook delivery](https://learn.microsoft.com/en-us/graph/change-notifications-delivery-webhooks)
- [Teams async replies](https://learn.microsoft.com/en-us/microsoft-365-copilot/extensibility/custom-engine-agent-asynchronous-flow)
- [Hatchet crons](https://docs.hatchet.run/v1/cron-runs), [Temporal schedules](https://docs.temporal.io/develop/python/schedules)
- AWS Lambda: [quotas](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html),
  [Lambda Web Adapter](https://awslabs.github.io/aws-lambda-web-adapter/features/non-http-events.html)
- [EventBridge Scheduler CreateSchedule](https://docs.aws.amazon.com/scheduler/latest/APIReference/API_CreateSchedule.html)

**Hindsight and Bedrock:**
- [Hindsight](https://github.com/vectorize-io/hindsight), including
  `hindsight-docs/docs/developer/{extensions.md,configuration.mdx,admin-cli.md,services.md,retrieval.md}`
- [Bedrock models by region](https://docs.aws.amazon.com/bedrock/latest/userguide/models-region-compatibility.html)
- [Bedrock cross-region inference in Australia](https://aws.amazon.com/blogs/machine-learning/introducing-amazon-bedrock-cross-region-inference-for-claude-sonnet-4-5-and-haiku-4-5-in-japan-and-australia)

**Local code reviewed:**
- voxhelm `voice-gateway` (`internal/server/server.go`, `internal/hub/hub.go`, `internal/handler/`)
- voxhelm `orchestrator` (`app/services/{ecs,kubernetes}.py`)
- voxhelm `welcome-bot/SPEC.md` (REST connector)
