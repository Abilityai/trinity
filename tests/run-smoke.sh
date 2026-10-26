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

# #2891: deterministic order, pinned explicitly. pytest auto-discovers an installed
# pytest-randomly and would silently shuffle this run; `-p no:randomly` is a
# no-op when the plugin is absent and the tests/conftest guard refuses an
# unannounced shuffle either way. Randomized runs are CI's seed matrix
# (`-p randomly --randomly-seed=N`), never a local script default.
time python -m pytest -p no:randomly -m smoke -v --tb=short "$@"
