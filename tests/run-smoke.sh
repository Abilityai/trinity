#!/bin/bash
# Trinity Smoke Tests (~30 seconds)
# Fast validation - no agent creation required
# Use for: Quick checks, CI pipelines, development feedback

set -e

cd "$(dirname "$0")"
source .venv/bin/activate
# Pull TRINITY_TEST_PASSWORD / REDIS_BACKEND_PASSWORD from project .env.
source "$(dirname "$0")/setup-env.sh"

echo "========================================="
echo "  TRINITY SMOKE TESTS (Tier 1)"
echo "  Expected time: ~30 seconds"
echo "========================================="
echo ""

time python -m pytest -m smoke -v --tb=short "$@"
