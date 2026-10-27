#!/usr/bin/env bash
# Build a qaas wheel from the local repo, install it in a clean image and run the smoke test.
# Usage: tests/docker_install_package/run_local_dist.sh [env-file]   (default: <repo>/.env_install_test)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
ENV_FILE="${1:-$REPO/.env_install_test}"
PYTHON="${PYTHON:-python3}"
IMAGE="qaas-local-dist-smoke:latest"

[[ -f "$ENV_FILE" ]] || { echo "Env file not found: $ENV_FILE" >&2; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Build from a clean copy so stale build/ or dist/ content can't leak into the wheel.
mkdir -p "$WORK/src" "$WORK/ctx/dist"
cp "$REPO/pyproject.toml" "$REPO/README.rst" "$REPO/LICENSE" "$WORK/src/"
cp -R "$REPO/src" "$WORK/src/src"
find "$WORK/src" \( -name "__pycache__" -o -name "*.egg-info" \) -prune -exec rm -rf {} +

"$PYTHON" -m pip wheel --no-deps --wheel-dir "$WORK/ctx/dist" "$WORK/src"
echo "Built: $(ls "$WORK/ctx/dist")"

cp "$HERE/smoke_submit.py" "$WORK/ctx/"
docker build -f "$HERE/Dockerfile.local" -t "$IMAGE" "$WORK/ctx"
docker run --rm --env-file "$ENV_FILE" "$IMAGE"
