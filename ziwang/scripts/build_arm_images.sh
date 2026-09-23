#!/bin/bash
set -euo pipefail

TARGET_PLATFORM="${TARGET_PLATFORM:-linux/arm64}"
BUILD_NETWORK="${BUILD_NETWORK:-host}"
BASE_IMAGE="${BASE_IMAGE:-}"
IMAGE_PREFIX="${IMAGE_PREFIX:-}"
IMAGE_TAG="${IMAGE_TAG:-v1-arm64}"
OUTPUT_MODE="${OUTPUT_MODE:-load}"
OUTPUT_DIR="${OUTPUT_DIR:-}"
BUILDER="${BUILDER:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if ! docker buildx version >/dev/null 2>&1; then
    echo "error: docker buildx is required" >&2
    exit 1
fi

if [ -n "$BUILDER" ]; then
    docker buildx inspect "$BUILDER" --bootstrap >/dev/null
else
    docker buildx inspect --bootstrap >/dev/null
fi

case "$OUTPUT_MODE" in
    load|push)
        ;;
    docker|oci)
        if [ -z "$OUTPUT_DIR" ]; then
            OUTPUT_DIR="$REPO_ROOT/dist/arm-images"
        fi
        mkdir -p "$OUTPUT_DIR"
        ;;
    *)
        echo "error: OUTPUT_MODE must be load, push, docker, or oci" >&2
        exit 2
        ;;
esac

platform_name="${TARGET_PLATFORM//\//-}"

for device in ckl xtl zzw; do
    context="$REPO_ROOT/$device"
    image="${IMAGE_PREFIX}jm-${device}:${IMAGE_TAG}"
    cmd=(
        docker buildx build
        --platform "$TARGET_PLATFORM"
        --network "$BUILD_NETWORK"
        -t "$image"
        -f "$context/Dockerfile"
    )

    if [ -n "$BUILDER" ]; then
        cmd+=(--builder "$BUILDER")
    fi
    if [ -n "$BASE_IMAGE" ]; then
        cmd+=(--build-arg "BASE_IMAGE=$BASE_IMAGE")
    fi
    if [ "${PULL:-0}" = "1" ]; then
        cmd+=(--pull)
    fi
    if [ "${NO_CACHE:-0}" = "1" ]; then
        cmd+=(--no-cache)
    fi

    case "$OUTPUT_MODE" in
        load)
            cmd+=(--load)
            ;;
        push)
            cmd+=(--push)
            ;;
        docker)
            cmd+=(--output "type=docker,dest=$OUTPUT_DIR/jm-${device}-${IMAGE_TAG}-${platform_name}.tar")
            ;;
        oci)
            cmd+=(--output "type=oci,dest=$OUTPUT_DIR/jm-${device}-${IMAGE_TAG}-${platform_name}.oci.tar")
            ;;
    esac

    cmd+=("$context")

    echo "========== Building $image for $TARGET_PLATFORM =========="
    "${cmd[@]}"

    if [ "$OUTPUT_MODE" = "load" ]; then
        actual_platform="$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$image")"
        echo "loaded $image platform=$actual_platform"
    fi
done

echo "========== ARM image build completed =========="
echo "platform: $TARGET_PLATFORM"
echo "output mode: $OUTPUT_MODE"
if [ "$OUTPUT_MODE" = "docker" ] || [ "$OUTPUT_MODE" = "oci" ]; then
    echo "output directory: $OUTPUT_DIR"
fi
