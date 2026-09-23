#!/bin/bash
set -Eeuo pipefail

DEVICE=ckl
IMAGE="${RADIO_IMAGE:-jm-ckl:v1}"
DAEMON=ckl_daemon
CONFIG_FILE=/tmp/radio_test/ckl_config.cfg
NODE_KEY=nodeNo
MATCH_KEY=radioFreq
BASE_MATCH=1000
ALT_MATCH=1001
ACK_COUNT=6
EXPECTED_QUERY_LEN=750
ROUTE_NUM=3
OTHER_DEVICE=xtl

protocol_injection_args() {
    local node_id="$1"
    local host_prime="$2"
    local radio_freq="$3"
    INJECTION_ARGS=(
        --set "nodeNo=$node_id"
        --set "radioFreq=$radio_freq"
        --set radioPower=1
        --set workFreqMode=1
        --set radioRate=1
        --set "hostPrime=$host_prime"
    )
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/test_common.sh"
run_protocol_suite
