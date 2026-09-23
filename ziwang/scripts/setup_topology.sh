#!/bin/bash
# Mixed topology: four NS groups. Each group has one Relay and one
# CKL, XTL and ZZW device. Protocol-type devices are joined only to their
# own type bridge, so CKL/XTL/ZZW form separate L2 subnets.
set -Eeuo pipefail
trap 'echo "error at ${BASH_SOURCE[0]}:${LINENO}: ${BASH_COMMAND}" >&2' ERR

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOPOLOGY_GROUPS=(A B C D)
NS_IPS=(10.88.0.1 10.88.0.2 10.88.0.3 10.88.0.4)
RELAY_NS_IPS=(10.88.0.101 10.88.0.102 10.88.0.103 10.88.0.104)
NS_NET=10.88.0.0/24
TYPE_NETS=(10.89.1.0/24 10.89.2.0/24 10.89.3.0/24)
TYPES=(ckl xtl zzw)
IMAGES=("${CKL_IMAGE:-jm-ckl:v1}" "${XTL_IMAGE:-jm-xtl:v1}" "${ZZW_IMAGE:-jm-zzw:v1}")
BUILD_IMAGES="${BUILD_IMAGES:-0}"

need_root() { [[ "$EUID" == 0 ]] || { echo 'run as root' >&2; exit 1; }; }
del_link() { ip link del "$1" 2>/dev/null || true; }
relay_pid_file() { echo "/tmp/radio-relay-$1.pid"; }

ensure_images() {
    local missing=()
    for image in "${IMAGES[@]}"; do
        if ! docker image inspect "$image" >/dev/null 2>&1; then
            missing+=("$image")
        fi
    done
    ((${#missing[@]} == 0)) && return 0

    if [[ "$BUILD_IMAGES" == "1" ]]; then
        echo "missing local protocol images: ${missing[*]}"
        echo "building images locally (this may require access to the configured apt/base-image mirrors)"
        "$ROOT/scripts/build_images.sh"
        for image in "${missing[@]}"; do
            docker image inspect "$image" >/dev/null 2>&1 || {
                echo "image is still unavailable after build: $image" >&2
                exit 1
            }
        done
        return 0
    fi

    cat >&2 <<EOF
missing local protocol image(s): ${missing[*]}
Build them before starting the topology:
  sudo bash $ROOT/scripts/build_images.sh

Or let this script build them explicitly:
  sudo BUILD_IMAGES=1 bash $ROOT/scripts/setup_topology.sh

The topology uses --pull=never and will not pull images from Docker Hub.
EOF
    exit 1
}

cleanup() {
    for group in "${TOPOLOGY_GROUPS[@]}"; do
        pid_file="$(relay_pid_file "$group")"
        if [[ -s "$pid_file" ]]; then kill "$(cat "$pid_file")" 2>/dev/null || true; rm -f "$pid_file"; fi
        ip netns del "ns$group" 2>/dev/null || true
        for type in "${TYPES[@]}"; do
            docker rm -f "con${group}-${type}" 2>/dev/null || true
            del_link "relay-${group}-${type}"
            del_link "veth-${group}-${type}"
        done
        del_link "relay-${group}-ns"
        del_link "veth-${group}-ns"
        del_link "br-${group}-ns"
    done
    # Remove artifacts created by older versions that accidentally expanded
    # Bash's special GROUPS variable to the root group id (0).
    legacy_pid_file="$(relay_pid_file 0)"
    if [[ -s "$legacy_pid_file" ]]; then
        kill "$(cat "$legacy_pid_file")" 2>/dev/null || true
        rm -f "$legacy_pid_file"
    fi
    ip netns del ns0 2>/dev/null || true
    for type in "${TYPES[@]}"; do
        docker rm -f "con0-${type}" 2>/dev/null || true
        del_link "relay-0-${type}"
        del_link "veth-0-${type}"
    done
    del_link relay-0-ns
    del_link veth-0-ns
    del_link br-0-ns
    for type in "${TYPES[@]}"; do del_link "br-${type}"; done
}

verify_topology() {
    local group type name pid_file daemon ready attempt up_ofport flows
    for group in "${TOPOLOGY_GROUPS[@]}"; do
        ip netns list | awk '{print $1}' | grep -Fxq "ns${group}" || {
            echo "topology verification failed: missing namespace ns${group}" >&2
            return 1
        }
        pid_file="$(relay_pid_file "$group")"
        [[ -s "$pid_file" ]] && kill -0 "$(cat "$pid_file")" 2>/dev/null || {
            echo "topology verification failed: relay-${group} is not running" >&2
            return 1
        }
        for type in "${TYPES[@]}"; do
            name="con${group}-${type}"
            [[ "$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null)" == true ]] || {
                echo "topology verification failed: container $name is not running" >&2
                return 1
            }
            daemon="${type}_daemon"
            ready=0
            for attempt in {1..20}; do
                if docker exec "$name" pgrep -x "$daemon" >/dev/null 2>&1 && \
                   docker exec "$name" pgrep -f '^python3 /usr/local/bin/radio_param_service.py' >/dev/null 2>&1 && \
                   docker exec "$name" test -s "/tmp/radio_test/${type}_config.cfg"; then
                    up_ofport="$(docker exec "$name" ovs-vsctl get Interface eth-up ofport 2>/dev/null | tr -d '\r\"')"
                    flows="$(docker exec "$name" ovs-ofctl dump-flows br0 2>/dev/null || true)"
                    if [[ "$up_ofport" =~ ^[0-9]+$ && "$up_ofport" -gt 0 ]] && \
                       grep -Eq "priority=270,in_port=LOCAL,ip,nw_src=[^ ]+ actions=output:${up_ofport}([, ]|$)" <<<"$flows" && \
                       grep -Eq "priority=270,in_port=LOCAL,arp,arp_spa=[^ ]+ actions=output:${up_ofport}([, ]|$)" <<<"$flows"; then
                        ready=1
                        break
                    fi
                fi
                sleep 0.5
            done
            ((ready == 1)) || {
                echo "topology verification failed: $name business services are not ready or response flows do not output to eth-up (ofport ${up_ofport:-unknown})" >&2
                docker exec "$name" ovs-ofctl dump-flows br0 >&2 || true
                return 1
            }
        done
    done
}

create_ns_network() {
    for index in "${!TOPOLOGY_GROUPS[@]}"; do
        group="${TOPOLOGY_GROUPS[$index]}"
        bridge="br-${group}-ns"
        ip link add "$bridge" type bridge
        ip link set "$bridge" up
        ip addr add "${RELAY_NS_IPS[$index]}/24" dev "$bridge"
        ns="ns${group}"
        ip netns add "$ns"
        ip link add "veth-${group}-ns" type veth peer name "relay-${group}-ns"
        ip link set "veth-${group}-ns" master "$bridge"
        ip link set "veth-${group}-ns" up
        ip link set "relay-${group}-ns" netns "$ns"
        ip netns exec "$ns" ip link set lo up
        ip netns exec "$ns" ip link set "relay-${group}-ns" up
        ip netns exec "$ns" ip addr add "${NS_IPS[$index]}/32" dev "relay-${group}-ns"
        # Keep the NS address off-link: protocol traffic is explicitly sent to
        # this group's Relay, so another NS cannot be reached by ARP/direct L2.
        ip netns exec "$ns" ip route replace "${RELAY_NS_IPS[$index]}/32" dev "relay-${group}-ns" src "${NS_IPS[$index]}"
    done
}

create_type_networks() {
    for type_index in "${!TYPES[@]}"; do
        type="${TYPES[$type_index]}"
        bridge="br-${type}"
        ip link add "$bridge" type bridge
        ip link set "$bridge" up
        for index in "${!TOPOLOGY_GROUPS[@]}"; do
            ip addr add "10.89.$((type_index + 1)).$((index + 101))/24" dev "$bridge"
        done
    done
}

make_group() {
    local index="$1" group="${TOPOLOGY_GROUPS[$1]}" ns="ns${TOPOLOGY_GROUPS[$1]}" ns_ip="${NS_IPS[$1]}"
    local relay_ns_ip="${RELAY_NS_IPS[$1]}"
    local ns_bridge="br-${group}-ns"
    gateway="$relay_ns_ip"

    for type_index in "${!TYPES[@]}"; do
        type="${TYPES[$type_index]}"
        container="con${group}-${type}"
        # Each group's device address is .11/.12/.13 on the type subnet.
        device_ip="10.89.$((type_index + 1)).$((index + 11))"
        bridge="br-${type}"
        device_gateway="10.89.$((type_index + 1)).$((index + 101))"
        relay_mac="$(cat "/sys/class/net/${bridge}/address")"
        docker run --pull=never -dit --privileged --network none --name "$container" "${IMAGES[$type_index]}" /bin/bash >/dev/null
        pid="$(docker inspect -f '{{.State.Pid}}' "$container")"
        ip link add "veth-${group}-${type}" type veth peer name eth-up
        ip link set "veth-${group}-${type}" master "$bridge"
        ip link set "veth-${group}-${type}" up
        ip link set eth-up netns "$pid"
        docker exec -i "$container" env DEVICE="$type" DEVICE_IP="$device_ip" NS_IP="$ns_ip" \
            RELAY_IP="$device_gateway" RELAY_MAC="$relay_mac" NODE_NO="$((index + 1))" \
            bash -s <<'CONTAINER'
set -Eeuo pipefail
ovs-vsctl --may-exist add-br br0
ovs-ofctl add-flow br0 action=NORMAL 2>/dev/null || true
ip link set eth-up up
ovs-vsctl --may-exist add-port br0 eth-up
ip link add eth-ns type dummy 2>/dev/null || true
ip link set eth-ns up
ovs-vsctl --may-exist add-port br0 eth-ns
ip link set br0 up
ip addr flush dev br0
ip addr add "$DEVICE_IP/24" dev br0
ip neigh replace "$RELAY_IP" lladdr "$RELAY_MAC" dev br0 nud permanent
ip route replace "$NS_IP/32" via "$RELAY_IP" dev br0
# The protocol daemon's normal policy is designed for a software-side
# eth-ns port. In the Relay topology eth-ns is intentionally isolated, so
# locally generated device responses must leave through the real eth-up link.
UP_OFPORT="$(ovs-vsctl get Interface eth-up ofport | tr -d '"')"
[[ "$UP_OFPORT" =~ ^[0-9]+$ && "$UP_OFPORT" -gt 0 ]]
ovs-ofctl add-flow br0 "priority=285,ip,in_port=LOCAL,nw_src=$DEVICE_IP,actions=output:$UP_OFPORT"
ovs-ofctl add-flow br0 "priority=285,arp,in_port=LOCAL,arp_spa=$DEVICE_IP,actions=output:$UP_OFPORT"
mkdir -p /tmp/radio_test
if [[ "$DEVICE" == ckl ]]; then
  printf 'nodeNo=%s\nbusinessIp=%s\nradioFreq=1000\nradioPower=1\nworkFreqMode=1\nradioRate=1\nhostPrime=1\n' "$NODE_NO" "$NS_IP" >/tmp/radio_test/ckl_config.cfg
  config=/tmp/radio_test/ckl_config.cfg; daemon=ckl_daemon
elif [[ "$DEVICE" == xtl ]]; then
  printf 'nodeNum=%s\nbusinessIp=%s\nradioPower=1\nfreqType=1\nfreq=300.0\nuserRate=1\ncurrentChannelNo=0\nhostPrime=1\n' "$NODE_NO" "$NS_IP" >/tmp/radio_test/xtl_config.cfg
  config=/tmp/radio_test/xtl_config.cfg; daemon=xtl_daemon
else
  printf 'nodeNum=%s\nbusinessIp=%s\nradioPower=1\nfreqType=1\nfreq=300.0\nuserRate=1\nsuperiorNetNo=0\nhostPrime=1\n' "$NODE_NO" "$NS_IP" >/tmp/radio_test/zzw_config.cfg
  config=/tmp/radio_test/zzw_config.cfg; daemon=zzw_daemon
fi
nohup python3 /usr/local/bin/radio_param_service.py --device "$DEVICE" --config-file "$config" >/tmp/radio_test/radio_param_service.log 2>&1 &
nohup "$daemon" -i br0 -b br0 -n eth-ns -u eth-up >/tmp/radio_test/${DEVICE}_daemon.log 2>&1 &
nohup python3 /usr/local/lib/radio_protocol/topology_reporter.py --config-file "$config" --dest-ip "$RELAY_IP" >/tmp/radio_test/topology_reporter.log 2>&1 &
CONTAINER
    done
    # Relay is one process per NS group, with three independent device links.
    config="/tmp/radio-relay-${group}.json"
    cat >"$config" <<EOF
{"relay":{"listenIp":"$gateway","nsCidr":"${NS_NET}","nsIp":"$ns_ip","requestPort":10001,"responsePort":10009,"eventPort":8882,"nsInterface":"$ns_bridge","logFile":"/tmp/radio-relay-${group}.log","devices":{"ckl":"10.89.1.$((index + 11))","xtl":"10.89.2.$((index + 11))","zzw":"10.89.3.$((index + 11))"},"deviceInterfaces":{"ckl":"br-ckl","xtl":"br-xtl","zzw":"br-zzw"},"deviceListenIps":{"ckl":"10.89.1.$((index + 101))","xtl":"10.89.2.$((index + 101))","zzw":"10.89.3.$((index + 101))"}}}
EOF
    RELAY_CONFIG="$config" nohup "$ROOT/relay/run_relay.sh" >/tmp/radio-relay-${group}.stdout.log 2>&1 &
    echo $! >"$(relay_pid_file "$group")"
}

main() {
    need_root
    ensure_images
    cleanup
    create_ns_network
    create_type_networks
    for index in "${!TOPOLOGY_GROUPS[@]}"; do make_group "$index"; done
    verify_topology
    echo 'topology ready: nsA..nsD each have one relay (.101..104) and CKL/XTL/ZZW subnets'
}
main "$@"
