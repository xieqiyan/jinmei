#!/bin/bash
set -euo pipefail

BUILD_NETWORK="${BUILD_NETWORK:-host}"
PLATFORM="${PLATFORM:-}"
BASE_IMAGE="${BASE_IMAGE:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

for device in ckl xtl zzw; do
    context="$REPO_ROOT/$device"
    cmd=(docker build --network="${BUILD_NETWORK}" -t "jm-${device}:v1" -f "$context/Dockerfile")
    if [ -n "$PLATFORM" ]; then
        cmd+=(--platform "$PLATFORM")
    fi
    if [ -n "$BASE_IMAGE" ]; then
        cmd+=(--build-arg "BASE_IMAGE=${BASE_IMAGE}")
    fi
    cmd+=("$context")
    "${cmd[@]}"
done
