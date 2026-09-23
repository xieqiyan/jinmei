#!/bin/bash
set -Eeuo pipefail

RELAY_IP="${RELAY_IP:-10.88.0.101/24}"
NS_NET="${NS_NET:-10.88.0.0/24}"
DEVICE_IPS=(
    "${CKL_IP:-10.89.1.11}"
    "${XTL_IP:-10.89.2.11}"
    "${ZZW_IP:-10.89.3.11}"
)
NS_IF="${NS_IF:-relay-ns}"
IFS=',' read -r -a DEVICE_IFS <<< "${DEVICE_IFS:-br-ckl,br-xtl,br-zzw}"
if [[ "${#DEVICE_IFS[@]}" -ne 3 ]]; then
    echo "DEVICE_IFS must contain CKL,XTL,ZZW interfaces" >&2
    exit 1
fi

ip link show "$NS_IF" >/dev/null 2>&1 || {
    echo "missing NS-side interface: $NS_IF" >&2
    exit 1
}

ip addr add "$RELAY_IP" dev "$NS_IF" 2>/dev/null || true
for device_if in "${DEVICE_IFS[@]}"; do
    ip link show "$device_if" >/dev/null 2>&1 || {
        echo "missing device-side interface: $device_if" >&2
        exit 1
    }
    ip link set "$device_if" up
done
ip link set "$NS_IF" up
sysctl -q -w net.ipv4.ip_forward=1
sysctl -q -w net.ipv4.conf.all.rp_filter=0
sysctl -q -w "net.ipv4.conf.${NS_IF}.rp_filter=0"
for device_if in "${DEVICE_IFS[@]}"; do
    sysctl -q -w "net.ipv4.conf.${device_if}.rp_filter=0"
done

# Packets to the local relay address are captured by the userspace AF_PACKET
# relay. Install one host route per protocol endpoint on its own device link.
for index in "${!DEVICE_IPS[@]}"; do
    device_ip="${DEVICE_IPS[$index]}"
    device_if="${DEVICE_IFS[$index]}"
    ip route replace "${device_ip}/32" dev "$device_if"
done
ip route replace "$NS_NET" dev "$NS_IF"

echo "relay network ready: ns=$NS_IF devices=${DEVICE_IPS[*]} relay=$RELAY_IP"
