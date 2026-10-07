#!/usr/bin/env bash
# Run the 1.0 system chain tests (trinity-enterprise#794) against a live stack —
# a release candidate, or any instance you can afford to mutate.
#
#   TRINITY_API_URL=http://localhost:8000 \
#   TRINITY_TEST_PASSWORD=<admin password> \
#   [CHAIN_SKILLS_REPO=owner/name CHAIN_SKILLS_GITHUB_TOKEN=…] \
#   scripts/system/run_chains.sh [--report-dir DIR] [pytest args…]
#
# Writes DIR/chain-report.json and DIR/chain-report.md (default ./chain-report):
# per chain, passed / failed at a named step / partial / not run. The markdown
# is what you attach to #783 as go / no-go evidence.
#
# Exit code: 0 only when EVERY chain passed. A chain that did not run, or ran
# only in part, is not a pass — so the exit is non-zero while any chain is
# blocked; read the report for why.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REPORT_DIR="${CHAIN_REPORT_DIR:-$PWD/chain-report}"
if [ "${1:-}" = "--report-dir" ]; then REPORT_DIR="$2"; shift 2; fi
mkdir -p "$REPORT_DIR"
REPORT_DIR="$(cd "$REPORT_DIR" && pwd)"

: "${TRINITY_API_URL:=http://localhost:8000}"
if [ -z "${TRINITY_TEST_PASSWORD:-}" ] && [ -z "${ADMIN_PASSWORD:-}" ]; then
  echo "run_chains: set TRINITY_TEST_PASSWORD (or ADMIN_PASSWORD) to the target's admin password" >&2
  exit 2
fi

export TRINITY_CHAIN_TESTS=1 TRINITY_API_URL CHAIN_REPORT_DIR="$REPORT_DIR"

cd "$REPO_ROOT/tests"
rc=0
python -m pytest system_chains -v -rs -p no:randomly --tb=short "$@" || rc=$?
if [ "$rc" = "5" ]; then
  echo "run_chains: no chains collected — nothing ran, which is not a pass" >&2
  exit 1
fi

if [ ! -f "$REPORT_DIR/chain-report.json" ]; then
  echo "run_chains: no report was written — treat this run as failed" >&2
  exit 1
fi
cat "$REPORT_DIR/chain-report.md"
python - "$REPORT_DIR/chain-report.json" <<'PY'
import json, sys
s = json.load(open(sys.argv[1]))["summary"]
sys.exit(0 if s["all_passed"] else 3)
PY
