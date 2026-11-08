#!/usr/bin/env bash
#
# memory.sh — shared, secure memory for the PoC: Hindsight behind memory-proxy (namespace memory).
#
# Builds the backup image, renders memory.yaml and applies it. Secrets (Bedrock key, backup writer key, the shared
# Hindsight key, proxy grants) and the backup recipient ConfigMap are created separately and never printed; see
# README.md "Memory".
#
# Usage: ./memory.sh [up|status|backup-now] [--dry-run]
# Prerequisites: kubectl, docker buildx, crane, envsubst; tunnels up (../tunnels.sh); ../.last-tag from frontdoor.sh build
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
POC="$(dirname "$HERE")"
export KUBECONFIG="${POC}/kubeconfig"
REGISTRY_PUSH=127.0.0.1:15000
REGISTRY_PULL=registry.poc:5000
DRY_RUN=false
CMD="${1:-up}"; [ "${2:-}" = "--dry-run" ] && DRY_RUN=true

show_usage() { sed -n '3,12p' "$0" | sed 's/^# \{0,1\}//'; }
log() { echo "==> $*"; }
die() { echo "error: $*" >&2; exit 1; }

up() {
    local tag bucket
    tag="$(cat "${POC}/.last-tag" 2>/dev/null)" || die "no ../.last-tag: run ../frontdoor.sh build first"
    bucket="$(cat "${HERE}/.bucket" 2>/dev/null)" || die "no .bucket file (the backup bucket's name)"
    export PROXY_IMAGE="${REGISTRY_PULL}/agent-core-poc:${tag}"
    export BACKUP_IMAGE="${REGISTRY_PULL}/memory-backup:1"
    export BUCKET="$bucket"
    if [ "$DRY_RUN" = true ]; then envsubst < "${HERE}/memory.yaml"; return; fi
    log "backup image"
    docker buildx build --builder "${BUILDX_BUILDER:-orbstack}" --platform linux/amd64 --load -t memory-backup:1 -f "${HERE}/Dockerfile.backup" "$HERE" >/dev/null
    local tar; tar="$(mktemp -t memory-backup).tar"
    docker save -o "$tar" memory-backup:1 && crane push --insecure "$tar" "${REGISTRY_PUSH}/memory-backup:1" >/dev/null
    rm -f "$tar"
    log "applying (proxy image ${PROXY_IMAGE})"
    envsubst '${PROXY_IMAGE} ${BACKUP_IMAGE} ${BUCKET}' < "${HERE}/memory.yaml" | kubectl apply -f -
}

case "$CMD" in
    up) up ;;
    status) kubectl -n memory get cluster,pods,svc,networkpolicy,cronjob ;;
    backup-now) kubectl -n memory create job --from=cronjob/memory-backup "memory-backup-$(date +%s)" ;;
    -h|--help) show_usage ;;
    *) show_usage; exit 1 ;;
esac
