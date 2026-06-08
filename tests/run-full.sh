#!/bin/bash
# Trinity Full Test Suite (~5-8 minutes)
# Complete validation including slow chat tests
# Use for: Release validation, comprehensive testing

set -e

cd "$(dirname "$0")"
source .venv/bin/activate
# Pull TRINITY_TEST_PASSWORD / REDIS_BACKEND_PASSWORD from project .env.
source "$(dirname "$0")/setup-env.sh"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)

echo "========================================="
echo "  TRINITY FULL TEST SUITE (Tier 3)"
echo "  Expected time: 5-8 minutes"
echo "========================================="
echo ""

# Unit tests and integration tests must run in separate pytest invocations:
# unit/ installs src/backend/utils under the name `utils` in sys.modules,
# which shadows tests/utils (used by integration tests for utils.api_client).
time python -m pytest --ignore=unit --ignore=process_engine -v --tb=short \
  --html=reports/test-report-${TIMESTAMP}.html \
  --self-contained-html \
  "$@"

time python -m pytest unit/ -v --tb=short \
  --html=reports/test-report-${TIMESTAMP}-unit.html \
  --self-contained-html

echo ""
echo "HTML reports saved to: reports/test-report-${TIMESTAMP}*.html"
