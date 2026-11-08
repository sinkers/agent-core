# Scale-to-zero PoC environment

**A trial, not a production setup.** See the gaps in `SECURITY.md` before running it. The failure tests in `poc_tests.py` need `WORKER_TEST_HOOKS=1` in `poc.env`; leave it unset otherwise, as it lets anyone who can message the agent crash its workers.

A standalone 3-node k3s cluster in KVM virtual machines for the scale-to-zero PoC
(`docs/plans/scale-to-zero.md`, section 11). Nothing here touches any other cluster: the scripts use
`./kubeconfig` and ignore `$KUBECONFIG`.

| Script | What it does |
|---|---|
| `cluster.sh --host USER@HOST up` | Creates 3 Ubuntu VMs on a libvirt host (4 vCPU, 8 GiB, 40 GB each) and installs k3s as 3 servers with embedded etcd; points every node at the in-cluster registry; writes `./kubeconfig` |
| `tunnels.sh --host USER@HOST` | Opens the API (127.0.0.1:16443), the OpenShell gateway (127.0.0.1:17680) and the registry (127.0.0.1:15000), restarting port-forwards that drop |
| `platform.sh` | Installs the registry, Agent Sandbox, CloudNativePG (3 instances) and the OpenShell gateway (2 replicas on Postgres), with pinned versions |
| `frontdoor.sh up` | Builds the PoC image (agent-core, the OpenShell SDK, Probe), pushes it, creates the secrets and runs the front door (2 replicas) with a worker-image pre-pull on every node |
| `frontdoor.sh voxhelm-up` | Builds voxhelm's voice-gateway for amd64 and runs it (text chat only) |
| `poc_tests.py` | The PoC's success criteria and attack tests against the cluster |
| `cluster.sh --host USER@HOST down` | Deletes the VMs and their disks |

The libvirt host needs `qemu-kvm`, `libvirt-daemon-system`, `virtinst`, `cloud-image-utils` and the default
network active; nodes need Linux 6.2+ with Landlock (Ubuntu 24.04 qualifies).

## Using OpenShell

Install the `openshell` CLI from the [release](https://github.com/NVIDIA/OpenShell/releases) matching
`platform.sh`, copy the client bundle and register the gateway:

```shell
D=~/.config/openshell/gateways/s2z/mtls; mkdir -p $D
for k in ca.crt tls.crt tls.key; do
  kubectl --kubeconfig kubeconfig -n openshell get secret openshell-client-tls \
    -o jsonpath="{.data.${k//./\\.}}" | base64 -d > $D/$k
done
openshell gateway add https://127.0.0.1:17680 --local --name s2z && openshell gateway select s2z
```

Images: push with `crane push --insecure image.tar 127.0.0.1:15000/NAME:TAG`; sandboxes use
`registry.poc:5000/NAME:TAG`.

AWS for Bedrock: create the provider from environment variables so no key is on a command line:
`openshell provider create --name aws-agents --type aws --credential AWS_ACCESS_KEY_ID --credential AWS_SECRET_ACCESS_KEY`
(after `openshell profile import --url .../v0.1.6/providers/aws.yaml --global`). `policies/bedrock-only.yaml`
allows Converse in ap-southeast-2 and nothing else.

## The front door and Probe

`frontdoor.sh up` runs `agentcore.frontdoor` in namespace `agents`. It keeps the queue in the `agents` database
(CloudNativePG) and starts a Probe worker in an OpenShell sandbox only while there is work
(`policies/probe-worker.yaml`: Bedrock through the proxy and the front door's work API, nothing else). Probe sleeps
again 10 minutes after its last message.

```shell
./frontdoor.sh chat probe "hello"     # as an API client; prints the holding line, then the answer
./frontdoor.sh status                  # workers, queue, leader
./frontdoor.sh events                  # timings: intake, launched, worker_ready, claimed, done, delivered
kubectl --kubeconfig kubeconfig -n agents patch configmap agents-killswitch -p '{"data":{"probe":"stop"}}'  # kill switch
```

Admin and client calls run inside a front door pod with that pod's tokens, so no token is copied out.

### Telegram

1. Create a bot with @BotFather (`/newbot`) and keep its token in a password manager.
2. `TELEGRAM_BOT_TOKEN_PROBE=... ./frontdoor.sh telegram-token` (or run it with no variable to be prompted). The
   token goes to the `frontdoor-channels` secret over stdin.
3. `./frontdoor.sh pair probe` gives a one-time link, valid 60 minutes: open it on the phone to pair. Only paired
   people get answers; group chats are ignored.
4. Text and voice notes both work. Voice notes are transcribed by the front door (ElevenLabs Scribe), answered in
   text, and the answer is also sent as a voice note. Run `./frontdoor.sh secrets` with `ELEVENLABS_API_KEY` set
   to enable speech.

The bot long-polls Telegram from the leader replica, so no public endpoint is needed.

### voxhelm

`./frontdoor.sh voxhelm-up` builds voice-gateway from its source folder (`VOXHELM_GATEWAY_SRC`) and runs it in
namespace `voxhelm`. The front door holds Probe's agent socket for user `poc-user`, so Probe shows online while it
sleeps and a message wakes it. `./frontdoor.sh voxhelm-chat "hello" "and again"` chats as a voxhelm app would.
Voice through voice-gateway needs a Deepgram key and LiveKit; this runs text chat only, inside the cluster.

## Memory (Hindsight behind memory-proxy)

`memory/memory.sh up` deploys namespace `memory`:
- **Postgres:** `memory-pg`, CloudNativePG with 2 instances, pgvector, 5Gi local-path each.
- **Hindsight 0.10.0:** API-key auth, MCP and file upload off, Bedrock Haiku for extraction.
- **memory-proxy:** 2 replicas of `agentcore.memproxy`.
- **NetworkPolicies:** deny by default.
  - The proxy takes traffic from sandboxes and the front door, and may only reach Hindsight.
  - Hindsight takes traffic only from the proxy, and may reach its Postgres and outside HTTPS.
  - Postgres takes traffic from Hindsight, the backup job and CloudNativePG.
- **Nightly backup:** `pg_dump`, encrypted with `age` to a public key, then uploaded to S3.

Credentials, created once and never printed:
- **AWS IAM users:**
  - `agentcore-poc-hindsight` may only call Bedrock Haiku in ap-southeast-2. Its key is in Secret `hindsight-bedrock`.
  - `agentcore-poc-memory-backup` may only `s3:PutObject` under `memory-pg/` in the backup bucket. Its key is in Secret `memory-backup-aws`. The bucket name is in `memory/.bucket` (gitignored).
- **Backup bucket:** versioned, SSE-S3, public access blocked. Objects expire after 30 days; old versions after 7.
- **Backup encryption:** the age public key is in ConfigMap `memory-backup-recipient`. The private key is only in 1Password (Automation vault, "agent-core PoC memory backups age key").
- **Hindsight's API key:** held only by Hindsight (`hindsight-api-key`) and the proxy (`memproxy-upstream`).
- **Per-agent tokens:**
  - The proxy stores only each token's SHA-256 and its bank (`memproxy-grants`: `[{"name","bank","sha256"}]`).
  - The token itself is an OpenShell provider credential: profile `providers/agentcore-memory.yaml`, provider `memory-probe`.
  - The sandbox sees a placeholder. OpenShell adds the real token only on requests to memory-proxy.

To add an agent:
1. Mint a token.
2. Add `{name, bank, sha256}` to `memproxy-grants`.
3. Create its own provider (`openshell provider create --name memory-<agent> --type agentcore-memory --credential HINDSIGHT_API_KEY`, with the token in the environment).
4. Give it a pool marked `only_for: [<agent>]`. A pool that carries one agent's token must never serve another.

To restore: fetch a `.dump.age` from the bucket, then `age -d -i key.txt file | pg_restore -d <url>`.
