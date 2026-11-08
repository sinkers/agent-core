#!/usr/bin/env bash
#
# platform.sh — install the shared platform on the scale-to-zero PoC cluster
#
# Installs, idempotently: the Agent Sandbox controller, CloudNativePG with a 3-instance Postgres cluster
# (databases agents and openshell), and the OpenShell gateway (2 replicas on that Postgres, sandboxes in
# namespace "sandboxes"). Versions are pinned below; test an upgrade here before anywhere else.
#
# Usage: ./platform.sh [-n|--dry-run] [-h|--help]
#
# Environment: POC_KUBECONFIG (default ./kubeconfig; $KUBECONFIG is ignored so this never acts on another cluster)
#
# Prerequisites: ./cluster.sh up, the API tunnel open, kubectl and helm (v3.14+), openssl.

set -euo pipefail

AGENT_SANDBOX_VERSION="v1.0.9"
CNPG_VERSION="1.30.1"
OPENSHELL_VERSION="0.1.6"
HERE="$(cd "$(dirname "$0")" && pwd)"
export KUBECONFIG="${POC_KUBECONFIG:-${HERE}/kubeconfig}"  # never the caller's cluster
DRY_RUN=false

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }
show_usage() { sed -n '3,13p' "$0" | sed 's/^# \{0,1\}//'; }
run() { if [ "$DRY_RUN" = true ]; then echo "would run: $*"; else "$@"; fi; }

# Create a Secret once, with a random password; never print it.
ensure_password_secret() {
    local ns="$1" name="$2" user="$3"
    kubectl -n "$ns" get secret "$name" >/dev/null 2>&1 && return 0
    # The password goes in on stdin, so it never appears in a process list.
    [ "$DRY_RUN" = true ] && { echo "would create secret ${ns}/${name}"; return 0; }
    openssl rand -hex 24 | tr -d '\n' | kubectl -n "$ns" create secret generic "$name" \
        --type=kubernetes.io/basic-auth --from-literal=username="$user" --from-file=password=/dev/stdin
}

while [ $# -gt 0 ]; do
    case "$1" in
        -n|--dry-run) DRY_RUN=true; shift ;;
        -h|--help) show_usage; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done
kubectl get nodes >/dev/null || die "cluster not reachable (open the tunnel; see cluster.sh kubeconfig)"

log "Agent Sandbox ${AGENT_SANDBOX_VERSION}"
run kubectl apply --server-side -f "https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${AGENT_SANDBOX_VERSION}/sandbox.yaml"
# Two controllers on different nodes (it elects a leader): with one, a node going down stops every new sandbox until
# Kubernetes evicts the pod (5 minutes by default). Found by the PoC's power-off test.
run kubectl -n agent-sandbox-system patch deploy agent-sandbox-controller --type merge -p '{"spec":{"replicas":2,"template":{"spec":{"affinity":{"podAntiAffinity":{"requiredDuringSchedulingIgnoredDuringExecution":[{"labelSelector":{"matchLabels":{"app":"agent-sandbox-controller"}},"topologyKey":"kubernetes.io/hostname"}]}}}}}}'
run kubectl -n agent-sandbox-system rollout status deploy --timeout=180s

log "Image registry (registry.poc:5000)"
run kubectl apply -f "${HERE}/platform/registry.yaml"
run kubectl -n registry rollout status deploy/registry --timeout=180s

log "CloudNativePG ${CNPG_VERSION}"
run kubectl apply --server-side -f "https://github.com/cloudnative-pg/cloudnative-pg/releases/download/v${CNPG_VERSION}/cnpg-${CNPG_VERSION}.yaml"
run kubectl -n cnpg-system rollout status deploy/cnpg-controller-manager --timeout=180s

log "Postgres (3 instances)"
run kubectl create namespace platform --dry-run=client -o yaml | run kubectl apply -f -
ensure_password_secret platform pg-openshell openshell
run kubectl apply -f "${HERE}/platform/postgres.yaml"
run kubectl -n platform wait cluster/pg --for=condition=Ready --timeout=600s

log "OpenShell ${OPENSHELL_VERSION}"
run kubectl create namespace openshell --dry-run=client -o yaml | run kubectl apply -f -
run kubectl create namespace sandboxes --dry-run=client -o yaml | run kubectl apply -f -
if ! kubectl -n openshell get secret openshell-db >/dev/null 2>&1 && [ "$DRY_RUN" = false ]; then
    pw="$(kubectl -n platform get secret pg-openshell -o jsonpath='{.data.password}' | base64 -d)"
    printf 'postgresql://openshell:%s@pg-rw.platform.svc.cluster.local:5432/openshell' "$pw" |
        kubectl -n openshell create secret generic openshell-db --from-file=uri=/dev/stdin
    unset pw
fi
run helm upgrade --install openshell oci://ghcr.io/nvidia/openshell/helm-chart --version "$OPENSHELL_VERSION" \
    -n openshell -f "${HERE}/platform/openshell-values.yaml" --wait --timeout 10m
# Sandbox supervisors in their own namespace mount the gateway's client TLS secret; the chart creates it only
# in the release namespace, so copy it (again after the chart rotates it).
if [ "$DRY_RUN" = false ]; then
    kubectl -n openshell get secret openshell-client-tls -o json | python3 -c 'import json, sys
s = json.load(sys.stdin)
s["metadata"] = {"name": s["metadata"]["name"], "namespace": "sandboxes", "labels": {"app.kubernetes.io/managed-by": "platform.sh"}}
print(json.dumps(s))' | kubectl apply -f -
fi
run kubectl -n openshell get pods -o wide
