#!/bin/bash
set -Eeuo pipefail

DEVICE=xtl
IMAGE="${RADIO_IMAGE:-jm-xtl:v1}"
DAEMON=xtl_daemon
CONFIG_FILE=/tmp/radio_test/xtl_config.cfg
NODE_KEY=nodeNum
MATCH_KEY=freq
BASE_MATCH=300.0
ALT_MATCH=301.0
ACK_COUNT=7
EXPECTED_QUERY_LEN=6173
ROUTE_NUM=4
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
        --set currentChannelNo=0
        --set "hostPrime=$host_prime"
    )
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/test_common.sh"
run_protocol_suite
