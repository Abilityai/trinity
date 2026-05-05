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

echo "========================================="
echo "  TRINITY INTEGRATION TESTS"
echo "  Requires: live Docker stack"
echo "========================================="
echo ""

time python -m pytest -m integration -v --tb=short "$@"
