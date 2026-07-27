#!/bin/bash
# Trinity Integration Tests
# Requires the full Docker stack to be running (./scripts/deploy/start.sh).
# Excluded from run-smoke.sh (which has a ~30s, no-Docker contract).
#
# Includes:
#   tests/security/      — Redis network isolation + ACL enforcement (#589)
#   tests/integration/   — webhook rate-limit regression (#589) and others

set -e

cd "$(dirname "$0")"
source .venv/bin/activate
# Pull TRINITY_TEST_PASSWORD / REDIS_BACKEND_PASSWORD from project .env.
source "$(dirname "$0")/setup-env.sh"

# #1775: declare the Redis target explicitly, the same way verify-local does.
# tests/integration/conftest.py treats a caller-supplied REDIS_URL as "the
# harness knows where Redis is", so an unreachable Redis FAILS the run instead
# of silently skipping ~57 tests while pytest still exits 0. Without this line
# the two harnesses would disagree about what a Redis outage means.
if [ -z "${REDIS_URL:-}" ] && [ -n "${REDIS_BACKEND_PASSWORD:-}" ]; then
    export REDIS_URL="redis://backend:${REDIS_BACKEND_PASSWORD}@localhost:${REDIS_HOST_PORT:-6379}"
fi

echo "========================================="
echo "  TRINITY INTEGRATION TESTS"
echo "  Requires: live Docker stack"
echo "========================================="
echo ""

time python -m pytest -m integration -v --tb=short "$@"
