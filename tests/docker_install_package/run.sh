#!/usr/bin/env bash
# Build a clean image with qaas installed from the package index and run the smoke test.
# Usage: tests/docker_install_package/run.sh [-v|--version X.Y.Z|latest] [env-file]
#   version   qaas version to install (default: latest = newest release)
#   env-file  defaults to <repo>/.env_install_test
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
VERSION="${QAAS_VERSION:-latest}"
ENV_FILE="$REPO/.env_install_test"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -v|--version) VERSION="${2:?missing value for $1}"; shift 2 ;;
    -h|--help) sed -n '2,5p' "$0"; exit 0 ;;
    *) ENV_FILE="$1"; shift ;;
  esac
done

[[ -f "$ENV_FILE" ]] || { echo "Env file not found: $ENV_FILE" >&2; exit 1; }
[[ "$VERSION" == "latest" ]] && SPEC="" || SPEC="$VERSION"
IMAGE="qaas-pypi-smoke:$VERSION"

echo "Installing qaas ${VERSION} from the package index"
docker build --build-arg QAAS_VERSION="$SPEC" -t "$IMAGE" "$HERE"
docker run --rm --env-file "$ENV_FILE" "$IMAGE"
