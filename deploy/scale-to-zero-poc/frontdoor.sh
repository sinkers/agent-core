#!/usr/bin/env bash
#
# frontdoor.sh — build and run the scale-to-zero front door, and talk to it, on the PoC cluster
#
# The front door (agentcore.frontdoor) holds the queue and the chat channels and starts Probe's workers in OpenShell
# sandboxes only while there is work (docs/plans/scale-to-zero.md). This script builds the image, pushes it to the
# in-cluster registry, creates the secrets and applies frontdoor/frontdoor.yaml. Admin and client calls run inside
# a front door pod, so the API tokens never leave the cluster.
#
# Usage: ./frontdoor.sh [options] <command> [args]
#
#   build                 build agent-core and the PoC image (linux/amd64) and push it to the registry
#   secrets               create or refresh the secrets (database, API tokens, OpenShell client TLS, channels)
#   telegram-token        store a Telegram bot token for Probe (read from TELEGRAM_BOT_TOKEN_PROBE or prompted)
#   deploy                apply the manifests with the image last built, and wait for the rollout
#   up                    build, secrets, deploy
#   pair [AGENT]          a one-time Telegram pairing code and link (default agent: probe)
#   chat AGENT TEXT       send TEXT as a client and wait for the answer (conversation test-cli)
#   status                workers, queue, launch state and leader
#   events [AFTER_ID]     the event log
#   reset AGENT           start launching again after repeated failed starts
#   voxhelm-up            build voxhelm's voice-gateway (VOXHELM_GATEWAY_SRC) and run it, text chat only
#   voxhelm-chat TEXT...  chat with Probe through voice-gateway as a voxhelm app would (user poc-user)
#
# Options:
#   -t, --tag TAG   image tag (default: the git commit, plus -dirty for uncommitted changes)
#   -n, --dry-run   print what would be done
#   -h, --help      show this help
#
# Prerequisites: docker with buildx, crane, kubectl, python3; ./kubeconfig and ./tunnels.sh running (API
# 127.0.0.1:16443, registry 127.0.0.1:15000). ELEVENLABS_API_KEY in the environment enables voice notes.
# VOXHELM_GATEWAY_SRC: voxhelm's voice-gateway source folder (default ~/workspace/voxhelm/voice-gateway).

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/../.." && pwd)"
export KUBECONFIG="${POC_KUBECONFIG:-${HERE}/kubeconfig}"
NS=agents
REGISTRY_PUSH=127.0.0.1:15000
REGISTRY_PULL=registry.poc:5000
DRY_RUN=false
TAG=""

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }
show_usage() { sed -n '3,32p' "$0" | sed 's/^# \{0,1\}//'; }
run() { if [ "$DRY_RUN" = true ]; then echo "[dry-run] $*"; else "$@"; fi; }

default_tag() {
    local t; t="$(git -C "$ROOT" rev-parse --short HEAD)"
    git -C "$ROOT" diff --quiet HEAD -- src deploy/scale-to-zero-poc pyproject.toml || t="${t}-dirty$(date +%H%M)"
    echo "$t"
}

# Build agent-core, then the PoC image on it, for the cluster's architecture; push through the registry tunnel.
build() {
    local core="agent-core:s2z-${TAG}" poc="agent-core-poc:${TAG}" tar
    log "building ${core} and ${poc} (linux/amd64)"
    run docker buildx build --builder "${BUILDX_BUILDER:-orbstack}" --platform linux/amd64 --load -t "$core" "$ROOT"
    run docker buildx build --builder "${BUILDX_BUILDER:-orbstack}" --platform linux/amd64 --load --build-arg "AGENT_CORE_IMAGE=${core}" \
        -t "$poc" -f "${HERE}/image/Dockerfile" "$HERE"
    tar="$(mktemp -t s2z-image).tar"
    run docker save -o "$tar" "$poc"
    log "pushing to ${REGISTRY_PULL}/agent-core-poc:${TAG}"
    run crane push --insecure "$tar" "${REGISTRY_PUSH}/agent-core-poc:${TAG}"
    rm -f "$tar"
    [ "$DRY_RUN" = true ] || echo "$TAG" > "${HERE}/.last-tag"
}

# Secrets are piped to kubectl on stdin, never put on a command line.
secret_apply() {  # $1 name; stdin: JSON object of key -> value
    local name="$1"
    if [ "$DRY_RUN" = true ]; then echo "[dry-run] secret ${NS}/${name}"; cat >/dev/null; return; fi
    python3 -c '
import base64, json, sys
name, ns = sys.argv[1], sys.argv[2]
data = {k: base64.b64encode(v.encode()).decode() for k, v in json.load(sys.stdin).items() if v}
print(json.dumps({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name, "namespace": ns}, "data": data}))
' "$name" "$NS" | kubectl apply -f - >/dev/null
}

secret_get() {  # $1 namespace, $2 name -> JSON of decoded data, {} if absent (on stdout, for piping only)
    local raw
    raw="$(kubectl -n "$1" get secret "$2" -o json 2>/dev/null)" || { echo '{}'; return; }
    python3 -c '
import base64, json, sys
d = json.load(sys.stdin).get("data") or {}
print(json.dumps({k: base64.b64decode(v).decode() for k, v in d.items()}))' <<<"$raw"
}

# "from env" or "kept" for a variable, never its value.
source_of() { if [ -n "${!1:-}" ]; then echo "from env"; else echo "kept/none"; fi; }

secrets() {
    run kubectl apply -f - <<<"$(printf 'apiVersion: v1\nkind: Namespace\nmetadata: {name: %s}\n' "$NS")" >/dev/null
    log "frontdoor-db (the agents database, from CloudNativePG's app secret)"
    secret_get platform pg-app | python3 -c 'import json,sys; print(json.dumps({"uri": json.load(sys.stdin)["fqdn-uri"]}))' \
        | secret_apply frontdoor-db
    log "frontdoor-api (client and admin tokens; kept if they exist)"
    secret_get "$NS" frontdoor-api | python3 -c '
import json, secrets, sys
d = json.load(sys.stdin)
print(json.dumps({"client": d.get("client") or secrets.token_urlsafe(32), "admin": d.get("admin") or secrets.token_urlsafe(32)}))' \
        | secret_apply frontdoor-api
    log "openshell-client-tls (the gateway's client certificate)"
    if [ "$DRY_RUN" = true ]; then echo "[dry-run] copy openshell/openshell-client-tls"; else
        kubectl -n openshell get secret openshell-client-tls -o json | python3 -c '
import json, sys
s = json.load(sys.stdin)
print(json.dumps({"apiVersion": "v1", "kind": "Secret", "type": s.get("type"), "data": s["data"],
                  "metadata": {"name": "openshell-client-tls", "namespace": sys.argv[1]}}))' "$NS" | kubectl apply -f - >/dev/null
    fi
    channels
}

# ELEVENLABS_API_KEY and TELEGRAM_BOT_TOKEN_PROBE from the environment; values already stored are kept.
channels() {
    log "frontdoor-channels (ElevenLabs: $(source_of ELEVENLABS_API_KEY); Telegram: $(source_of TELEGRAM_BOT_TOKEN_PROBE); voxhelm dev: $(source_of VOXHELM_DEV_TOKEN_PROBE))"
    secret_get "$NS" frontdoor-channels | python3 -c '
import json, os, sys
d = json.load(sys.stdin)
for key, env in (("elevenlabs", "ELEVENLABS_API_KEY"), ("telegram-probe", "TELEGRAM_BOT_TOKEN_PROBE"),
                 ("voxhelm-dev-probe", "VOXHELM_DEV_TOKEN_PROBE")):
    if os.environ.get(env):
        d[key] = os.environ[env]
print(json.dumps(d))' | secret_apply frontdoor-channels
}

telegram_token() {
    if [ -z "${TELEGRAM_BOT_TOKEN_PROBE:-}" ]; then
        read -rs -p "Telegram bot token for Probe (from @BotFather): " TELEGRAM_BOT_TOKEN_PROBE; echo
    fi
    [ -n "$TELEGRAM_BOT_TOKEN_PROBE" ] || die "no token given"
    export TELEGRAM_BOT_TOKEN_PROBE
    channels
    run kubectl -n "$NS" rollout restart deploy/frontdoor >/dev/null
    log "stored; the front door is restarting to pick it up"
}

deploy() {
    [ -n "$TAG" ] || TAG="$(cat "${HERE}/.last-tag" 2>/dev/null)" || die "no image built yet (run build)"
    local image="${REGISTRY_PULL}/agent-core-poc:${TAG}" rendered hash
    # poc.env (gitignored): settings that don't belong in the repo, e.g. an external voxhelm gateway's address and ids
    if [ -f "${HERE}/poc.env" ]; then set -a; . "${HERE}/poc.env"; set +a; fi
    rendered="$(IMAGE="$image" WORKER_IMAGE="$image" TAG="$TAG" python3 -c '
import os, re, sys
keep = {"CONFIG_HASH"}
print(re.sub(r"\$\{(\w+)\}", lambda m: m.group(0) if m.group(1) in keep else os.environ.get(m.group(1), ""), sys.stdin.read()))' \
        < "${HERE}/frontdoor/frontdoor.yaml")"
    hash="$(printf '%s' "$rendered" | shasum | cut -c1-12)"
    rendered="${rendered//\$\{CONFIG_HASH\}/$hash}"
    log "applying the front door with ${image}"
    if [ "$DRY_RUN" = true ]; then echo "$rendered"; return; fi
    kubectl apply -f - <<<"$rendered"
    # Created once and never re-applied: a deploy must not release an agent someone has stopped.
    kubectl -n "$NS" get configmap agents-killswitch >/dev/null 2>&1 \
        || kubectl -n "$NS" create configmap agents-killswitch --from-literal=probe=run
    kubectl -n "$NS" rollout status deploy/frontdoor --timeout=300s
}

# Call the front door's API from inside one of its pods, with the token from that pod's environment.
api() {  # $1 METHOD, $2 PATH, $3 token env (ADMIN_API_TOKEN | CLIENT_API_TOKEN), $4 JSON body (optional)
    kubectl -n "$NS" exec deploy/frontdoor -- python3 -c '
import json, os, sys, httpx
method, path, env, body = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else None
r = httpx.request(method, "http://127.0.0.1:8080" + path, headers={"Authorization": "Bearer " + os.environ[env]},
                  json=json.loads(body) if body else None, timeout=90)
try:
    print(json.dumps(r.json(), indent=2, default=str))
except ValueError:
    print(r.status_code, r.text)' "$@"
}

chat() {  # $1 agent, $2 text
    local out id
    out="$(api POST "/v1/agents/$1/chat" CLIENT_API_TOKEN "$(python3 -c 'import json,sys; print(json.dumps({"conversation_id": "test-cli", "text": sys.argv[1], "user_id": "test-cli", "user_name": "PoC tester"}))' "$2")")"
    echo "$out"
    id="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("item_id") or "")' <<<"$out")"
    [ -n "$id" ] && api GET "/v1/work/${id}?wait=60" CLIENT_API_TOKEN
}

# voxhelm's voice-gateway, built for amd64 from its source folder (voxhelm/Dockerfile.voice-gateway).
voxhelm_up() {
    local src="${VOXHELM_GATEWAY_SRC:-${HOME}/workspace/voxhelm/voice-gateway}" tag tar image rendered
    [ -d "$src" ] || die "voice-gateway source not found at ${src} (set VOXHELM_GATEWAY_SRC)"
    tag="$(git -C "$src" rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M)"
    log "building voice-gateway ${tag} (linux/amd64)"
    run docker buildx build --builder "${BUILDX_BUILDER:-orbstack}" --platform linux/amd64 --load -t "voice-gateway:${tag}" \
        -f "${HERE}/voxhelm/Dockerfile.voice-gateway" "$src"
    tar="$(mktemp -t s2z-vg).tar"
    run docker save -o "$tar" "voice-gateway:${tag}"
    run crane push --insecure "$tar" "${REGISTRY_PUSH}/voice-gateway:${tag}"
    rm -f "$tar"
    image="${REGISTRY_PULL}/voice-gateway:${tag}"
    rendered="$(sed "s#\${VG_IMAGE}#${image}#" "${HERE}/voxhelm/voice-gateway.yaml")"
    if [ "$DRY_RUN" = true ]; then echo "$rendered"; return; fi
    kubectl apply -f - <<<"$rendered"
    kubectl -n voxhelm rollout status deploy/voice-gateway --timeout=180s
}

voxhelm_chat() {
    kubectl -n "$NS" exec -i deploy/frontdoor -- python3 - "ws://voice-gateway.voxhelm.svc.cluster.local:8080/ws" \
        poc-user probe "$@" < "${HERE}/voxhelm/chat.py"
}

CMD=""; ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        -t|--tag) TAG="$2"; shift 2 ;;
        -n|--dry-run) DRY_RUN=true; shift ;;
        -h|--help) show_usage; exit 0 ;;
        *) if [ -z "$CMD" ]; then CMD="$1"; else ARGS+=("$1"); fi; shift ;;
    esac
done
[ -n "$CMD" ] || { show_usage; exit 1; }
[ -f "$KUBECONFIG" ] || die "no ${KUBECONFIG}: run cluster.sh kubeconfig and tunnels.sh first"

case "$CMD" in
    build) TAG="${TAG:-$(default_tag)}"; build ;;
    secrets) secrets ;;
    telegram-token) telegram_token ;;
    deploy) deploy ;;
    up) TAG="${TAG:-$(default_tag)}"; build; secrets; deploy ;;
    pair) api POST "/v1/admin/agents/${ARGS[0]:-probe}/pairing-code" ADMIN_API_TOKEN ;;
    chat) [ ${#ARGS[@]} -ge 2 ] || die "usage: chat AGENT TEXT"; chat "${ARGS[0]}" "${ARGS[1]}" ;;
    status) api GET /v1/admin/status ADMIN_API_TOKEN ;;
    events) api GET "/v1/admin/events?after=${ARGS[0]:-0}" ADMIN_API_TOKEN ;;
    voxhelm-up) voxhelm_up ;;
    voxhelm-chat) [ ${#ARGS[@]} -ge 1 ] || die "usage: voxhelm-chat TEXT..."; voxhelm_chat "${ARGS[@]}" ;;
    reset) [ ${#ARGS[@]} -ge 1 ] || die "usage: reset AGENT"; api POST "/v1/admin/agents/${ARGS[0]}/reset" ADMIN_API_TOKEN ;;
    *) die "unknown command: $CMD (see --help)" ;;
esac
