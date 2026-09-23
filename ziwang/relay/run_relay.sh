#!/bin/bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="${RELAY_CONFIG:-$SCRIPT_DIR/relay_config.json}"
LOG_LEVEL="${RELAY_LOG_LEVEL:-INFO}"

exec python3 "$SCRIPT_DIR/relay.py" --config "$CONFIG" --log-level "$LOG_LEVEL"
