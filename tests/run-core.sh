#!/bin/bash
# Trinity Core Tests (~3-5 minutes)
# Standard validation with module-scoped agents
# Use for: Pre-commit checks, feature verification

set -e

cd "$(dirname "$0")"
source .venv/bin/activate
# Pull TRINITY_TEST_PASSWORD / REDIS_BACKEND_PASSWORD from project .env.
source "$(dirname "$0")/setup-env.sh"

echo "========================================="
echo "  TRINITY CORE TESTS (Tier 2)"
echo "  Expected time: 3-5 minutes"
echo "========================================="
echo ""

# Unit tests and integration tests must run in separate pytest invocations:
# unit/ installs src/backend/utils under the name `utils` in sys.modules,
# which shadows tests/utils (used by integration tests for utils.api_client).
time python -m pytest -m "not slow" --ignore=unit --ignore=process_engine -v --tb=short "$@"
time python -m pytest unit/ -m "not slow" -v --tb=short
