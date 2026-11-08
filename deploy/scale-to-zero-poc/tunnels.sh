#!/usr/bin/env bash
#
# tunnels.sh — (re)open the local tunnels to the scale-to-zero PoC cluster
#
# Opens, when not already working: the Kubernetes API on 127.0.0.1:16443 (SSH through the libvirt host), the
# OpenShell gateway on 127.0.0.1:17680 and the image registry on 127.0.0.1:15000 (kubectl port-forwards, kept
# alive in a loop because a port-forward ends whenever its pod or the API connection goes away).
#
# Usage: ./tunnels.sh [-H USER@HOST] [stop]
#
# Environment: LIBVIRT_HOST, VM_NET_BASE (192.168.122), VM_FIRST_OCTET (11)

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
HOST="${LIBVIRT_HOST:-}"
KEY="${HOME}/.ssh/s2z_poc_ed25519"
VM1="${VM_NET_BASE:-192.168.122}.${VM_FIRST_OCTET:-11}"
export KUBECONFIG="${HERE}/kubeconfig"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        -H|--host) HOST="$2"; shift 2 ;;
        -h|--help) sed -n '3,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        stop) pkill -f "s2z-tunnel" || true; pkill -f "16443:127.0.0.1:6443" || true; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

if ! curl -sk -m 3 -o /dev/null https://127.0.0.1:16443/; then
    [ -n "$HOST" ] || die "set --host USER@HOST or LIBVIRT_HOST"
    ssh -f -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -i "$KEY" \
        -o UserKnownHostsFile="${HERE}/.known_hosts" -J "$HOST" -L 16443:127.0.0.1:6443 "ubuntu@${VM1}"
fi
kubectl get --raw /readyz >/dev/null || die "Kubernetes API not reachable"

# A port-forward that restarts itself; the process title carries s2z-tunnel so 'stop' can find it.
forward() {
    local ns="$1" svc="$2" ports="$3"
    pgrep -f "s2z-tunnel ${ns}/${svc}" >/dev/null && return 0
    nohup bash -c "exec -a 's2z-tunnel ${ns}/${svc}' bash -c 'while true; do
        kubectl -n ${ns} port-forward --address 127.0.0.1 svc/${svc} ${ports} >/dev/null 2>&1; sleep 1; done'" \
        >/dev/null 2>&1 &
}
forward openshell openshell 17680:8080
forward registry registry 15000:5000
sleep 2
echo "API 127.0.0.1:16443, OpenShell 127.0.0.1:17680, registry 127.0.0.1:15000"
