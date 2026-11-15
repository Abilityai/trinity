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

# Unit and integration tests run in separate pytest invocations. Note the
# ORIGINAL reason is gone: `tests/utils` shadowed the backend's `utils` package
# and was renamed to `tests/testkit` in #2080, so `utils` now unambiguously
# means src/backend/utils. They stay separate because unit/ must not inherit
# the root conftest's live-backend fixtures — a different, still-valid reason.
# #2891: deterministic order, pinned explicitly. pytest auto-discovers an installed
# pytest-randomly and would silently shuffle this run; `-p no:randomly` is a
# no-op when the plugin is absent and the tests/conftest guard refuses an
# unannounced shuffle either way. Randomized runs are CI's seed matrix
# (`-p randomly --randomly-seed=N`), never a local script default.
time python -m pytest -p no:randomly -m "not slow" --ignore=unit -v --tb=short "$@"
time python -m pytest -p no:randomly unit/ -m "not slow" -v --tb=short
