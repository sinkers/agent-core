#!/usr/bin/env bash
#
# cluster.sh — a standalone 3-node k3s cluster in KVM virtual machines, for the scale-to-zero PoC
#
# Creates three Ubuntu 24.04 VMs on a libvirt host (reached over SSH), then installs k3s on them as three
# servers with embedded etcd, so the PoC runs on a multi-node cluster (docs/plans/scale-to-zero.md, section 3)
# without touching anything else on the host. The VMs sit on libvirt's NAT network; reach them through the host
# (ssh -J). The kubeconfig is written to ./kubeconfig with a tunnel command to use it.
#
# Usage: ./cluster.sh [options] <up|k3s|status|kubeconfig|registry|down>
#
#   up          create the VMs (idempotent) and install k3s
#   k3s         install or repair k3s only
#   status      list the VMs and nodes
#   kubeconfig  write ./kubeconfig (API reached through an SSH tunnel on localhost:16443)
#   registry    point every node at the in-cluster registry (registry.poc:5000)
#   down        delete the VMs and their disks
#
# Options:
#   -H, --host USER@HOST  the libvirt host (or LIBVIRT_HOST)
#   -k, --key PATH        SSH key for the VMs (default ~/.ssh/s2z_poc_ed25519; created if missing)
#   -n, --dry-run         print what would be run
#   -h, --help            show this help
#
# Environment: VM_PREFIX (s2z), VM_CPUS (4), VM_MEM_MB (8192), VM_DISK_GB (40), VM_NET_BASE (192.168.122),
#              VM_FIRST_OCTET (11), K3S_VERSION (v1.36.4+k3s1)
#
# Prerequisites: on the host, qemu-kvm, libvirt (default network active), virtinst, cloud-image-utils;
#                the SSH user in group libvirt with passwordless sudo. Locally: ssh, kubectl.

set -euo pipefail
shopt -s extglob

HOST="${LIBVIRT_HOST:-}"
KEY="${HOME}/.ssh/s2z_poc_ed25519"
DRY_RUN=false
PREFIX="${VM_PREFIX:-s2z}"
CPUS="${VM_CPUS:-4}"
MEM="${VM_MEM_MB:-8192}"
DISK="${VM_DISK_GB:-40}"
NET="${VM_NET_BASE:-192.168.122}"
FIRST="${VM_FIRST_OCTET:-11}"
K3S_VERSION="${K3S_VERSION:-v1.36.4+k3s1}"
IMAGE_URL="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
POOL="/var/lib/libvirt/images/${PREFIX}"
HERE="$(cd "$(dirname "$0")" && pwd)"
NODES=(1 2 3)

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

show_usage() { sed -n '3,30p' "$0" | sed 's/^# \{0,1\}//'; }

# Run a command on the libvirt host.
host() {
    if [ "$DRY_RUN" = true ]; then echo "[host] $*"; [[ "$*" != *dominfo* ]]; return; fi
    ssh -o BatchMode=yes "$HOST" "$@"
}

# Run a command on VM number $1.
vm() {
    local n="$1"; shift
    if [ "$DRY_RUN" = true ]; then echo "[vm$n] ${*//K3S_TOKEN=+([^ ])/K3S_TOKEN=***}"; return 0; fi
    ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="${HERE}/.known_hosts" \
        -i "$KEY" -J "$HOST" "ubuntu@$(ip_of "$n")" "$@"
}

ip_of() { echo "${NET}.$((FIRST + $1 - 1))"; }
name_of() { echo "${PREFIX}-$1"; }

# Create the VMs: one base image, a copy-on-write disk and a cloud-init seed per VM, a fixed DHCP address.
create_vms() {
    local pub; pub="$(cat "${KEY}.pub")"
    host "sudo mkdir -p ${POOL} && cd ${POOL} && { test -f base.img || sudo curl -fsSL -o base.img ${IMAGE_URL}; }"
    for n in "${NODES[@]}"; do
        local name ip mac
        name="$(name_of "$n")"; ip="$(ip_of "$n")"; mac="52:54:00:5a:00:0${n}"
        if host "sudo virsh dominfo ${name} >/dev/null 2>&1"; then log "${name} exists"; continue; fi
        log "creating ${name} (${ip})"
        host "sudo virsh net-update default add ip-dhcp-host \"<host mac='${mac}' name='${name}' ip='${ip}'/>\" --live --config 2>/dev/null || true"
        host "cd ${POOL} && sudo qemu-img create -q -f qcow2 -F qcow2 -b base.img ${name}.qcow2 ${DISK}G"
        host "cat > /tmp/${name}-user-data <<EOF
#cloud-config
hostname: ${name}
users:
  - name: ubuntu
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    ssh_authorized_keys: [\"${pub}\"]
package_update: false
EOF
sudo cloud-localds ${POOL}/${name}-seed.iso /tmp/${name}-user-data && rm /tmp/${name}-user-data"
        host "sudo virt-install --name ${name} --vcpus ${CPUS} --memory ${MEM} --cpu host-passthrough \
            --disk ${POOL}/${name}.qcow2,bus=virtio --disk ${POOL}/${name}-seed.iso,device=cdrom \
            --network network=default,mac=${mac},model=virtio --os-variant ubuntu24.04 \
            --graphics none --noautoconsole --import --autostart >/dev/null"
    done
    for n in "${NODES[@]}"; do wait_ssh "$n"; done
}

wait_ssh() {
    [ "$DRY_RUN" = true ] && return 0
    local n="$1"
    for _ in $(seq 1 60); do
        if vm "$n" true 2>/dev/null; then log "$(name_of "$n") reachable"; return 0; fi
        sleep 5
    done
    die "$(name_of "$n") not reachable over SSH"
}

# k3s: node 1 starts the embedded etcd cluster, nodes 2 and 3 join as servers.
install_k3s() {
    local token_file="${HERE}/.k3s-token" token
    [ -f "$token_file" ] || { head -c 24 /dev/urandom | base64 | tr -d '/+=' > "$token_file"; chmod 600 "$token_file"; }
    token="$(cat "$token_file")"
    local common="INSTALL_K3S_VERSION=${K3S_VERSION} K3S_TOKEN=${token}"
    local args="--disable traefik --write-kubeconfig-mode 600 --tls-san 127.0.0.1"
    log "k3s on $(name_of 1) (cluster-init)"
    vm 1 "systemctl is-active -q k3s || curl -sfL https://get.k3s.io | sudo ${common} sh -s - server --cluster-init ${args} --node-ip $(ip_of 1)"
    for n in 2 3; do
        log "k3s on $(name_of "$n") (join)"
        vm "$n" "systemctl is-active -q k3s || curl -sfL https://get.k3s.io | sudo ${common} sh -s - server --server https://$(ip_of 1):6443 ${args} --node-ip $(ip_of "$n")"
    done
    vm 1 "sudo k3s kubectl wait --for=condition=Ready node --all --timeout=180s && sudo k3s kubectl get nodes -o wide"
    registry_mirror
}

# Every node pulls registry.poc:5000/... from the in-cluster registry's NodePort (platform/registry.yaml).
registry_mirror() {
    local n
    for n in "${NODES[@]}"; do
        vm "$n" "test -f /etc/rancher/k3s/registries.yaml && grep -q registry.poc /etc/rancher/k3s/registries.yaml || {
            printf 'mirrors:\n  registry.poc:5000:\n    endpoint: [\"http://127.0.0.1:30500\"]\n' | sudo tee /etc/rancher/k3s/registries.yaml >/dev/null
            sudo systemctl restart k3s; }"
    done
}

write_kubeconfig() {
    [ "$DRY_RUN" = true ] && { echo "[local] write ${HERE}/kubeconfig"; return 0; }
    vm 1 "sudo cat /etc/rancher/k3s/k3s.yaml" | sed "s#https://127.0.0.1:6443#https://127.0.0.1:16443#" > "${HERE}/kubeconfig"
    chmod 600 "${HERE}/kubeconfig"
    log "wrote ${HERE}/kubeconfig; open the tunnel with:"
    echo "  ssh -f -N -i ${KEY} -J ${HOST} -L 16443:127.0.0.1:6443 ubuntu@$(ip_of 1)"
}

status() {
    host "sudo virsh list --all | grep ${PREFIX}- || true"
    vm 1 "sudo k3s kubectl get nodes -o wide" || true
}

down() {
    for n in "${NODES[@]}"; do
        local name; name="$(name_of "$n")"
        log "deleting ${name}"
        host "sudo virsh destroy ${name} 2>/dev/null; sudo virsh undefine ${name} --remove-all-storage 2>/dev/null; sudo rm -f ${POOL}/${name}-seed.iso; true"
        host "sudo virsh net-update default delete ip-dhcp-host \"<host mac='52:54:00:5a:00:0${n}'/>\" --live --config 2>/dev/null || true"
    done
    rm -f "${HERE}/kubeconfig" "${HERE}/.known_hosts"
}

CMD=""
while [ $# -gt 0 ]; do
    case "$1" in
        -H|--host) HOST="$2"; shift 2 ;;
        -k|--key) KEY="$2"; shift 2 ;;
        -n|--dry-run) DRY_RUN=true; shift ;;
        -h|--help) show_usage; exit 0 ;;
        up|k3s|status|kubeconfig|registry|down) CMD="$1"; shift ;;
        *) die "unknown argument: $1 (see --help)" ;;
    esac
done
[ -n "$CMD" ] || { show_usage; exit 1; }
[ -n "$HOST" ] || die "set the libvirt host with --host USER@HOST or LIBVIRT_HOST"
[ -f "$KEY" ] || { log "creating SSH key ${KEY}"; ssh-keygen -q -t ed25519 -N "" -C "${PREFIX}-poc" -f "$KEY"; }

case "$CMD" in
    up) create_vms; install_k3s; write_kubeconfig ;;
    k3s) install_k3s; write_kubeconfig ;;
    status) status ;;
    kubeconfig) write_kubeconfig ;;
    registry) registry_mirror ;;
    down) down ;;
esac
