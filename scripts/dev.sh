#!/usr/bin/env bash
# Developer helper. Everything runs in Docker, so no local Python/Node toolchain is needed.
# Usage: scripts/dev.sh <build|push|test|test-api|test-ui|run-local>
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE="${IMAGE:-ingress-query}"
TAG="${TAG:-0.1.0}"
PORT="${PORT:-8080}"

build() { docker build -t "$IMAGE:$TAG" .; }

push() { build; docker push "$IMAGE:$TAG"; }

test_api() {
  echo "== API tests (pytest, mocked Kubernetes API)"
  docker run --rm -v "$PWD":/src:ro -w /src -e PYTHONDONTWRITEBYTECODE=1 python:3.13-slim sh -c \
    "pip install -q --root-user-action=ignore --disable-pip-version-check -r requirements-dev.txt \
     && python -m pytest -q -p no:cacheprovider tests/"
}

test_ui() {
  echo "== UI tests (jsdom)"
  docker run --rm -v "$PWD":/src:ro node:22-alpine sh -c \
    "mkdir -p /w/tests /w/app && cp -r /src/tests/ui /w/tests/ && cp -r /src/app/static /w/app/ \
     && cd /w/tests/ui && npm install -s --no-audit --no-fund >/dev/null && npm test -s"
}

run_local() {
  # Uses your kubeconfig (mounted read-only). Contexts that rely on exec/auth plugins may not work inside the container.
  docker run --rm -p "$PORT":8080 -v "$HOME/.kube/config":/kube/config:ro -e KUBECONFIG=/kube/config "$IMAGE:$TAG"
}

case "${1:-}" in
  build) build ;;
  push) push ;;
  test) test_api; test_ui ;;
  test-api) test_api ;;
  test-ui) test_ui ;;
  run-local) run_local ;;
  *) echo "Usage: $0 <build|push|test|test-api|test-ui|run-local>" >&2; exit 1 ;;
esac
