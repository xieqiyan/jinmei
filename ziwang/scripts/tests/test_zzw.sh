#!/bin/bash
set -Eeuo pipefail

DEVICE=zzw
IMAGE="${RADIO_IMAGE:-jm-zzw:v1}"
DAEMON=zzw_daemon
CONFIG_FILE=/tmp/radio_test/zzw_config.cfg
NODE_KEY=nodeNum
MATCH_KEY=freq
BASE_MATCH=300.0
ALT_MATCH=301.0
ACK_COUNT=7
EXPECTED_QUERY_LEN=5682
ROUTE_NUM=2
OTHER_DEVICE=ckl

protocol_injection_args() {
    local node_id="$1"
    local host_prime="$2"
    local frequency="$3"
    INJECTION_ARGS=(
        --set "nodeNum=$node_id"
        --set radioPower=1
        --set freqType=1
        --set "freq=$frequency"
        --set userRate=1
        --set superiorNetNo=0
        --set "hostPrime=$host_prime"
    )
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/test_common.sh"
run_protocol_suite
