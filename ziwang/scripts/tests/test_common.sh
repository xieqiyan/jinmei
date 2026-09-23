#!/bin/bash
set -Eeuo pipefail

: "${DEVICE:?DEVICE is required}"
: "${IMAGE:?IMAGE is required}"
: "${DAEMON:?DAEMON is required}"
: "${CONFIG_FILE:?CONFIG_FILE is required}"
: "${NODE_KEY:?NODE_KEY is required}"
: "${MATCH_KEY:?MATCH_KEY is required}"
: "${BASE_MATCH:?BASE_MATCH is required}"
: "${ALT_MATCH:?ALT_MATCH is required}"
: "${ACK_COUNT:?ACK_COUNT is required}"
: "${EXPECTED_QUERY_LEN:?EXPECTED_QUERY_LEN is required}"
: "${ROUTE_NUM:?ROUTE_NUM is required}"
: "${OTHER_DEVICE:?OTHER_DEVICE is required}"

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$TEST_DIR/../.." && pwd)"
CLIENT="$REPO_ROOT/tools/radio_param_client.py"
TOPOLOGY_RECEIVER="$REPO_ROOT/tools/topology_receiver.py"
SETUP_SCRIPT="$REPO_ROOT/scripts/setup_${DEVICE}_topology.sh"

CONTAINERS=("conA-${DEVICE}" "conB-${DEVICE}" "conC-${DEVICE}" "conD-${DEVICE}")
NAMESPACES=(nsA nsB nsC nsD)
RADIO_IPS=(10.88.0.101 10.88.0.102 10.88.0.103 10.88.0.104)
BUSINESS_IPS=(10.88.0.1 10.88.0.2 10.88.0.3 10.88.0.4)
NODE_IDS=(1 2 3 4)

CONVERGENCE_TIMEOUT="${CONVERGENCE_TIMEOUT:-15}"
TOPOLOGY_TIMEOUT="${TOPOLOGY_TIMEOUT:-8}"
QUERY_TIMEOUT="${QUERY_TIMEOUT:-6}"
BUILD_NETWORK="${BUILD_NETWORK:-host}"
TEST_LOG_DIR="${TEST_LOG_DIR:-$REPO_ROOT/test_logs/${DEVICE}-$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$TEST_LOG_DIR"

PASS_COUNT=0
CURRENT_CASE="initialization"

log() {
    printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"
}

fail() {
    log "ERROR: $*"
    return 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

dump_diagnostics() {
    local diagnostic_file="$TEST_LOG_DIR/diagnostics.log"
    set +e
    {
        echo "case=$CURRENT_CASE"
        echo "device=$DEVICE"
        date
        for container in "${CONTAINERS[@]}"; do
            echo
            echo "===== $container processes ====="
            docker exec "$container" ps -ef 2>&1
            echo "===== $container config ====="
            docker exec "$container" cat "$CONFIG_FILE" 2>&1
            echo "===== $container peers ====="
            docker exec "$container" cat /tmp/radio_test/peers.json 2>&1
            echo "===== $container flows ====="
            docker exec "$container" ovs-ofctl dump-flows br0 2>&1
            echo "===== $container service log ====="
            docker exec "$container" tail -n 100 /tmp/radio_test/radio_param_service.log 2>&1
            echo "===== $container daemon log ====="
            docker exec "$container" tail -n 100 "/tmp/radio_test/${DEVICE}_daemon.log" 2>&1
            echo "===== $container topology log ====="
            docker exec "$container" tail -n 100 /tmp/radio_test/topology_reporter.log 2>&1
        done
    } >"$diagnostic_file" 2>&1
    set -e
    log "diagnostics written to $diagnostic_file"
}

run_case() {
    local case_name="$1"
    shift
    local case_id
    local case_log
    local status

    CURRENT_CASE="$case_name"
    case_id="$(printf '%02d-%s' "$((PASS_COUNT + 1))" "$case_name" | tr ' /:' '___')"
    case_log="$TEST_LOG_DIR/${case_id}.log"
    echo
    log "CASE: $case_name"

    set +e
    (
        set -Eeuo pipefail
        "$@"
    ) > >(tee "$case_log") 2>&1
    status=$?
    set -e

    if ((status != 0)); then
        log "FAIL: $case_name"
        dump_diagnostics
        exit "$status"
    fi

    PASS_COUNT=$((PASS_COUNT + 1))
    log "PASS: $case_name"
}

wait_until() {
    local timeout="$1"
    local description="$2"
    shift 2
    local deadline=$((SECONDS + timeout))

    while ((SECONDS <= deadline)); do
        if "$@"; then
            return 0
        fi
        sleep 1
    done
    fail "timeout waiting for $description"
}

build_image() {
    if [[ "${SKIP_BUILD:-0}" == "1" ]]; then
        log "SKIP_BUILD=1, using existing image $IMAGE"
        docker image inspect "$IMAGE" >/dev/null
        return
    fi

    local command=(docker build --network="$BUILD_NETWORK" -t "$IMAGE")
    if [[ -n "${PLATFORM:-}" ]]; then
        command+=(--platform "$PLATFORM")
    fi
    if [[ -n "${BASE_IMAGE:-}" ]]; then
        command+=(--build-arg "BASE_IMAGE=$BASE_IMAGE")
    fi
    command+=("$REPO_ROOT/$DEVICE")
    "${command[@]}"
}

setup_topology() {
    SKIP_CONNECTIVITY_TEST=1 RADIO_IMAGE="$IMAGE" bash "$SETUP_SCRIPT"
}

processes_running() {
    local container
    for container in "${CONTAINERS[@]}"; do
        docker exec "$container" pgrep -x "$DAEMON" >/dev/null 2>&1 || return 1
        docker exec "$container" pgrep -f '^python3 /usr/local/bin/radio_param_service.py( |$)' >/dev/null 2>&1 || return 1
        docker exec "$container" pgrep -f '^python3 /usr/local/lib/radio_protocol/topology_reporter.py( |$)' >/dev/null 2>&1 || return 1
        docker exec "$container" test -s "$CONFIG_FILE" || return 1
        docker exec "$container" test -s /tmp/radio_test/peers.json || return 1
    done
}

peer_reason_equals() {
    local container="$1"
    local peer_ip="$2"
    local expected="$3"
    docker exec "$container" cat /tmp/radio_test/peers.json 2>/dev/null |
        python3 -c 'import json,sys; data=json.load(sys.stdin); peer=next((item for item in data.get("peers", []) if item.get("businessIp")==sys.argv[1]), None); raise SystemExit(0 if peer and peer.get("reason")==sys.argv[2] else 1)' "$peer_ip" "$expected"
}

peer_absent() {
    local container="$1"
    local peer_ip="$2"
    docker exec "$container" cat /tmp/radio_test/peers.json 2>/dev/null |
        python3 -c 'import json,sys; data=json.load(sys.stdin); raise SystemExit(0 if all(item.get("businessIp")!=sys.argv[1] for item in data.get("peers", [])) else 1)' "$peer_ip"
}

peer_count_equals() {
    local container="$1"
    local expected="$2"
    docker exec "$container" cat /tmp/radio_test/peers.json 2>/dev/null |
        python3 -c 'import json,sys; data=json.load(sys.stdin); raise SystemExit(0 if len(data.get("peers", []))==int(sys.argv[1]) else 1)' "$expected"
}

flow_count_equals() {
    local container="$1"
    local expected="$2"
    local count
    count="$(docker exec "$container" ovs-ofctl dump-flows br0 2>/dev/null | grep -c 'priority=200' || true)"
    [[ "$count" == "$expected" ]]
}

ping_once() {
    local namespace="$1"
    local destination="$2"
    ip netns exec "$namespace" ping -c 1 -W 1 "$destination" >/dev/null 2>&1
}

full_mesh_up() {
    local source_index
    local destination_index
    for source_index in 0 1 2 3; do
        for destination_index in 0 1 2 3; do
            if [[ "$source_index" == "$destination_index" ]]; then
                continue
            fi
            ping_once "${NAMESPACES[$source_index]}" "${BUSINESS_IPS[$destination_index]}" || return 1
        done
    done
}

assert_ping_down() {
    local namespace="$1"
    local destination="$2"
    if ping_once "$namespace" "$destination"; then
        fail "unexpected connectivity: $namespace -> $destination"
    fi
}

config_value() {
    local container="$1"
    local key="$2"
    docker exec "$container" awk -F= -v key="$key" '$1 == key { print substr($0, index($0, "=") + 1); exit }' "$CONFIG_FILE"
}

config_number_equals() {
    local container="$1"
    local key="$2"
    local expected="$3"
    local actual
    actual="$(config_value "$container" "$key")"
    python3 -c 'import math,sys; raise SystemExit(0 if math.isclose(float(sys.argv[1]), float(sys.argv[2]), rel_tol=0.0, abs_tol=1e-4) else 1)' "$actual" "$expected"
}

config_text_equals() {
    local container="$1"
    local key="$2"
    local expected="$3"
    [[ "$(config_value "$container" "$key")" == "$expected" ]]
}

query_node() {
    local index="$1"
    local fragment="${2:-0}"
    local output="$TEST_LOG_DIR/query-${index}-fragment-${fragment}.out"
    local command=(ip netns exec "${NAMESPACES[$index]}" python3 "$CLIENT" query "${RADIO_IPS[$index]}" "$DEVICE" --summary --timeout "$QUERY_TIMEOUT")
    if [[ "$fragment" == "1" ]]; then
        command+=(--fragment --max-frag-payload 700)
    fi
    if ! "${command[@]}" >"$output" 2>&1; then
        cat "$output"
        return 1
    fi
    cat "$output"
    grep -q "len=$EXPECTED_QUERY_LEN expected=$EXPECTED_QUERY_LEN" "$output"
}

query_all_nodes() {
    local index
    for index in 0 1 2 3; do
        query_node "$index" 0 || return 1
    done
}

inject_node() {
    local index="$1"
    local host_prime="$2"
    local match_value="$3"
    local output="$TEST_LOG_DIR/inject-${index}-${host_prime}-${match_value}.out"
    local success_count

    protocol_injection_args "${NODE_IDS[$index]}" "$host_prime" "$match_value"
    if ! ip netns exec "${NAMESPACES[$index]}" python3 "$CLIENT" inject "${RADIO_IPS[$index]}" "$DEVICE" "${INJECTION_ARGS[@]}" --timeout "$QUERY_TIMEOUT" >"$output" 2>&1; then
        cat "$output"
        return 1
    fi
    cat "$output"
    success_count="$(grep -c 'result=success' "$output" || true)"
    [[ "$success_count" == "$ACK_COUNT" ]] || fail "expected $ACK_COUNT successful ACKs, got $success_count"
    if grep -q 'result=failed' "$output"; then
        fail "injection returned a failed ACK"
    fi
    config_number_equals "${CONTAINERS[$index]}" "$NODE_KEY" "${NODE_IDS[$index]}" || return 1
    config_number_equals "${CONTAINERS[$index]}" hostPrime "$host_prime" || return 1
    config_number_equals "${CONTAINERS[$index]}" "$MATCH_KEY" "$match_value" || return 1
    config_text_equals "${CONTAINERS[$index]}" businessIp "${BUSINESS_IPS[$index]}"
}

assert_topology_report() {
    local namespace_index="$1"
    local expected_count="$2"
    local expected_sync="$3"
    local output="$TEST_LOG_DIR/topology-${namespace_index}-${expected_count}-${expected_sync}.out"

    if ! timeout "$TOPOLOGY_TIMEOUT" ip netns exec "${NAMESPACES[$namespace_index]}" python3 "$TOPOLOGY_RECEIVER" >"$output" 2>&1; then
        cat "$output"
        return 1
    fi
    cat "$output"
    python3 - "$output" "${BUSINESS_IPS[$namespace_index]}" "$ROUTE_NUM" "$expected_count" "$expected_sync" <<'PY'
import json
import pathlib
import sys

text = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
start = text.find("{")
if start < 0:
    raise SystemExit("topology JSON not found")
data = json.loads(text[start:])
expected_node = sys.argv[2]
expected_route = int(sys.argv[3])
expected_count = int(sys.argv[4])
expected_sync = int(sys.argv[5])
sync_count = sum(1 for link in data.get("links", []) if link.get("isSync") == 1)
if data.get("nodeIP") != expected_node:
    raise SystemExit(f"nodeIP={data.get('nodeIP')} expected={expected_node}")
if data.get("routeNum") != expected_route:
    raise SystemExit(f"routeNum={data.get('routeNum')} expected={expected_route}")
if data.get("chainPathNum") != expected_count:
    raise SystemExit(f"chainPathNum={data.get('chainPathNum')} expected={expected_count}")
if sync_count != expected_sync:
    raise SystemExit(f"sync_count={sync_count} expected={expected_sync}")
PY
}

malformed_injection_rejected() {
    ip netns exec nsA python3 - "${RADIO_IPS[0]}" <<'PY'
import socket
import sys
import time

radio_ip = sys.argv[1]
deadline = time.monotonic() + 4.0
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", 8882))
    sock.sendto(b"\x00\x01\x02", (radio_ip, 10001))
    while time.monotonic() < deadline:
        sock.settimeout(max(0.1, deadline - time.monotonic()))
        data, _ = sock.recvfrom(65535)
        if data == b"\x13\x01":
            raise SystemExit(0)
raise SystemExit("failed injection ACK not received")
PY
}

stop_daemon() {
    local container="$1"
    docker exec "$container" bash -c "pkill -x '$DAEMON'"
}

start_daemon() {
    local container="$1"
    docker exec "$container" bash -c "nohup '$DAEMON' > '/tmp/radio_test/${DEVICE}_daemon.log' 2>&1 &"
    wait_until 5 "$container daemon restart" docker exec "$container" pgrep -x "$DAEMON" >/dev/null
}

stop_service() {
    local container="$1"
    docker exec "$container" bash -c "pkill -f '^python3 /usr/local/bin/radio_param_service.py( |$)'"
}

start_service() {
    local container="$1"
    docker exec "$container" bash -c "nohup python3 /usr/local/bin/radio_param_service.py > /tmp/radio_test/radio_param_service.log 2>&1 &"
    wait_until 5 "$container service restart" docker exec "$container" pgrep -f '^python3 /usr/local/bin/radio_param_service.py( |$)' >/dev/null
    sleep 1
}

test_prerequisites() {
    [[ "$EUID" -eq 0 ]] || fail "run this test as root"
    require_command docker
    require_command ip
    require_command ping
    require_command python3
    require_command timeout
    require_command grep
    require_command awk
    [[ -f "$CLIENT" ]]
    [[ -f "$TOPOLOGY_RECEIVER" ]]
    [[ -f "$SETUP_SCRIPT" ]]
}

test_initial_state() {
    wait_until "$CONVERGENCE_TIMEOUT" "all container processes" processes_running
    wait_until "$CONVERGENCE_TIMEOUT" "three peers on ${CONTAINERS[0]}" peer_count_equals "${CONTAINERS[0]}" 3
    wait_until "$CONVERGENCE_TIMEOUT" "initial full mesh" full_mesh_up
    wait_until "$CONVERGENCE_TIMEOUT" "initial ${CONTAINERS[0]} whitelist flows" flow_count_equals "${CONTAINERS[0]}" 12
}

test_queries() {
    query_all_nodes
    query_node 0 1
}

test_wrong_device_query_rejected() {
    local output="$TEST_LOG_DIR/wrong-device-query.out"
    if timeout 4 ip netns exec nsA python3 "$CLIENT" query "${RADIO_IPS[1]}" "$DEVICE" --timeout 2 >"$output" 2>&1; then
        cat "$output"
        fail "wrong-device query unexpectedly succeeded"
    fi
    cat "$output"
}

test_cross_relay_and_device_isolation() {
    local output="$TEST_LOG_DIR/cross-relay-isolation.out"
    if timeout 4 ip netns exec nsA python3 "$CLIENT" query "${RADIO_IPS[1]}" "$DEVICE" --timeout 2 >"$output" 2>&1; then
        cat "$output"
        fail "nsA unexpectedly used nsB's Relay"
    fi
    # NS namespaces only have the shared 10.88.0.0/24 route; protocol
    # subnets must not become directly reachable from an NS.
    if timeout 4 ip netns exec nsA python3 "$CLIENT" query 10.89.1.12 "$DEVICE" --timeout 2 >>"$output" 2>&1; then
        cat "$output"
        fail "nsA unexpectedly reached another group's protocol device"
    fi
    cat "$output"
}

test_valid_injection_and_persistence() {
    inject_node 1 0 "$BASE_MATCH"
    query_node 1 1
}

test_malformed_injection() {
    local before
    before="$(config_value "${CONTAINERS[0]}" "$MATCH_KEY")"
    malformed_injection_rejected
    [[ "$(config_value "${CONTAINERS[0]}" "$MATCH_KEY")" == "$before" ]]
}

test_initial_topology_report() {
    wait_until "$CONVERGENCE_TIMEOUT" "initial OK peer state" peer_reason_equals "${CONTAINERS[0]}" "${BUSINESS_IPS[1]}" OK
    assert_topology_report 0 3 3
}

test_parameter_mismatch() {
    inject_node 1 0 "$ALT_MATCH"
    wait_until "$CONVERGENCE_TIMEOUT" "B mismatch visible on A" peer_reason_equals "${CONTAINERS[0]}" "${BUSINESS_IPS[1]}" PARAM_MISMATCH
    wait_until "$CONVERGENCE_TIMEOUT" "A keeps two whitelist peers" flow_count_equals "${CONTAINERS[0]}" 8
    wait_until "$CONVERGENCE_TIMEOUT" "B has no whitelist peers" flow_count_equals "${CONTAINERS[1]}" 0
    assert_ping_down nsA "${BUSINESS_IPS[1]}"
    wait_until "$CONVERGENCE_TIMEOUT" "A to C remains reachable" ping_once nsA "${BUSINESS_IPS[2]}"
    assert_topology_report 0 3 2
}

test_parameter_restore() {
    inject_node 1 0 "$BASE_MATCH"
    wait_until "$CONVERGENCE_TIMEOUT" "B restored on A" peer_reason_equals "${CONTAINERS[0]}" "${BUSINESS_IPS[1]}" OK
    wait_until "$CONVERGENCE_TIMEOUT" "restored full mesh" full_mesh_up
    wait_until "$CONVERGENCE_TIMEOUT" "restored whitelist flows" flow_count_equals "${CONTAINERS[0]}" 12
}

test_no_master() {
    inject_node 0 0 "$BASE_MATCH"
    wait_until "$CONVERGENCE_TIMEOUT" "NO_MASTER state" peer_reason_equals "${CONTAINERS[0]}" "${BUSINESS_IPS[1]}" NO_MASTER
    wait_until "$CONVERGENCE_TIMEOUT" "no-master whitelist removal" flow_count_equals "${CONTAINERS[0]}" 0
    assert_ping_down nsA "${BUSINESS_IPS[1]}"
    assert_ping_down nsB "${BUSINESS_IPS[2]}"
    assert_topology_report 0 3 0
}

test_master_migration() {
    inject_node 2 1 "$BASE_MATCH"
    wait_until "$CONVERGENCE_TIMEOUT" "master migration convergence" peer_reason_equals "${CONTAINERS[0]}" "${BUSINESS_IPS[1]}" OK
    wait_until "$CONVERGENCE_TIMEOUT" "master migration full mesh" full_mesh_up
    wait_until "$CONVERGENCE_TIMEOUT" "master migration whitelist" flow_count_equals "${CONTAINERS[0]}" 12
    assert_topology_report 0 3 3
}

test_multiple_masters() {
    inject_node 0 1 "$BASE_MATCH"
    wait_until "$CONVERGENCE_TIMEOUT" "MULTI_MASTER state" peer_reason_equals "${CONTAINERS[0]}" "${BUSINESS_IPS[1]}" MULTI_MASTER
    wait_until "$CONVERGENCE_TIMEOUT" "multi-master whitelist removal" flow_count_equals "${CONTAINERS[0]}" 0
    assert_ping_down nsA "${BUSINESS_IPS[1]}"
    assert_topology_report 0 3 0
}

test_single_master_recovery() {
    inject_node 2 0 "$BASE_MATCH"
    wait_until "$CONVERGENCE_TIMEOUT" "single-master recovery" peer_reason_equals "${CONTAINERS[0]}" "${BUSINESS_IPS[1]}" OK
    wait_until "$CONVERGENCE_TIMEOUT" "single-master full mesh" full_mesh_up
    wait_until "$CONVERGENCE_TIMEOUT" "single-master whitelist" flow_count_equals "${CONTAINERS[0]}" 12
}

test_rolling_match_migration() {
    inject_node 0 1 "$ALT_MATCH"
    wait_until "$CONVERGENCE_TIMEOUT" "master isolated after match change" peer_reason_equals "${CONTAINERS[0]}" "${BUSINESS_IPS[1]}" PARAM_MISMATCH
    wait_until "$CONVERGENCE_TIMEOUT" "old subnet loses master" peer_reason_equals "${CONTAINERS[1]}" "${BUSINESS_IPS[2]}" NO_MASTER
    wait_until "$CONVERGENCE_TIMEOUT" "isolated master has no whitelist" flow_count_equals "${CONTAINERS[0]}" 0
    assert_ping_down nsA "${BUSINESS_IPS[1]}"
    assert_ping_down nsB "${BUSINESS_IPS[2]}"
    assert_topology_report 0 3 0

    inject_node 1 0 "$ALT_MATCH"
    wait_until "$CONVERGENCE_TIMEOUT" "A and B form migrated subnet" peer_reason_equals "${CONTAINERS[0]}" "${BUSINESS_IPS[1]}" OK
    wait_until "$CONVERGENCE_TIMEOUT" "A has one migrated whitelist peer" flow_count_equals "${CONTAINERS[0]}" 4
    wait_until "$CONVERGENCE_TIMEOUT" "A to B migrated connectivity" ping_once nsA "${BUSINESS_IPS[1]}"
    assert_ping_down nsA "${BUSINESS_IPS[2]}"

    inject_node 2 0 "$ALT_MATCH"
    wait_until "$CONVERGENCE_TIMEOUT" "C joins migrated subnet" peer_reason_equals "${CONTAINERS[0]}" "${BUSINESS_IPS[2]}" OK
    wait_until "$CONVERGENCE_TIMEOUT" "A has two migrated whitelist peers" flow_count_equals "${CONTAINERS[0]}" 8
    assert_ping_down nsA "${BUSINESS_IPS[3]}"

    inject_node 3 0 "$ALT_MATCH"
    wait_until "$CONVERGENCE_TIMEOUT" "all nodes join migrated subnet" full_mesh_up
    wait_until "$CONVERGENCE_TIMEOUT" "migrated full whitelist" flow_count_equals "${CONTAINERS[0]}" 12
    assert_topology_report 0 3 3
}

test_neighbor_timeout_and_rejoin() {
    stop_daemon "${CONTAINERS[3]}"
    wait_until "$CONVERGENCE_TIMEOUT" "D neighbor aging" peer_absent "${CONTAINERS[0]}" "${BUSINESS_IPS[3]}"
    wait_until "$CONVERGENCE_TIMEOUT" "D whitelist removal" flow_count_equals "${CONTAINERS[0]}" 8
    assert_ping_down nsA "${BUSINESS_IPS[3]}"
    wait_until "$CONVERGENCE_TIMEOUT" "remaining nodes stay connected" ping_once nsA "${BUSINESS_IPS[1]}"

    start_daemon "${CONTAINERS[3]}"
    wait_until "$CONVERGENCE_TIMEOUT" "D rejoin" peer_reason_equals "${CONTAINERS[0]}" "${BUSINESS_IPS[3]}" OK
    wait_until "$CONVERGENCE_TIMEOUT" "D connectivity recovery" full_mesh_up
    wait_until "$CONVERGENCE_TIMEOUT" "D whitelist recovery" flow_count_equals "${CONTAINERS[0]}" 12
}

test_service_restart_persistence() {
    local output="$TEST_LOG_DIR/service-down-query.out"
    stop_service "${CONTAINERS[1]}"
    if timeout 4 ip netns exec nsB python3 "$CLIENT" query "${RADIO_IPS[1]}" "$DEVICE" --timeout 2 >"$output" 2>&1; then
        cat "$output"
        fail "query unexpectedly succeeded while service was stopped"
    fi
    start_service "${CONTAINERS[1]}"
    query_node 1 1
    config_number_equals "${CONTAINERS[1]}" "$MATCH_KEY" "$ALT_MATCH"
    config_number_equals "${CONTAINERS[1]}" hostPrime 0
    config_text_equals "${CONTAINERS[1]}" businessIp "${BUSINESS_IPS[1]}"
}

test_final_state() {
    wait_until "$CONVERGENCE_TIMEOUT" "final processes" processes_running
    query_all_nodes
    wait_until "$CONVERGENCE_TIMEOUT" "final full mesh" full_mesh_up
    wait_until "$CONVERGENCE_TIMEOUT" "final peer count" peer_count_equals "${CONTAINERS[0]}" 3
    wait_until "$CONVERGENCE_TIMEOUT" "final whitelist" flow_count_equals "${CONTAINERS[0]}" 12
    assert_topology_report 0 3 3
}

run_protocol_suite() {
    run_case prerequisites test_prerequisites
    run_case image_build build_image
    run_case topology_setup setup_topology
    run_case initial_processes_and_connectivity test_initial_state
    run_case parameter_queries test_queries
    run_case wrong_device_query_rejection test_wrong_device_query_rejected
    run_case cross_relay_and_device_isolation test_cross_relay_and_device_isolation
    run_case valid_injection_and_config_persistence test_valid_injection_and_persistence
    run_case malformed_injection_rejection test_malformed_injection
    run_case initial_topology_report test_initial_topology_report
    run_case parameter_mismatch_isolation test_parameter_mismatch
    run_case parameter_restore test_parameter_restore
    run_case no_master_isolation test_no_master
    run_case master_migration test_master_migration
    run_case multiple_master_isolation test_multiple_masters
    run_case single_master_recovery test_single_master_recovery
    run_case rolling_match_parameter_migration test_rolling_match_migration
    run_case neighbor_timeout_and_rejoin test_neighbor_timeout_and_rejoin
    run_case service_restart_and_state_persistence test_service_restart_persistence
    run_case final_business_validation test_final_state

    echo
    log "ALL $PASS_COUNT CASES PASSED for $DEVICE"
    log "logs: $TEST_LOG_DIR"
}
