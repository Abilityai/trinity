#!/usr/bin/env bash
# Run the J24 recovery chain (trinity-enterprise#905; ent#847 matrix row B9)
# against a live stack ON THIS HOST — the legs restart the backend container,
# force-remove agent containers and read container env over the Docker socket,
# so point it ONLY at a stack you can afford to break. Never a shared instance.
#
#   TRINITY_API_URL=http://localhost:8000 \
#   TRINITY_TEST_PASSWORD=<admin password> \
#   scripts/system/run_recovery.sh [--report-dir DIR] [pytest args…]
#
# Preconditions (the chain reports NOT RUN naming whichever is missing):
#   * the stack was booted with
#       PULL_MODE_PILOT_AGENTS=pytest-ephemeral-journey-b9pull
#     in .env (the pilot allowlist is boot-time backend env);
#   * the instance holds a real Claude credential — every leg is a real turn.
#
# Two phases, because de-piloting (leg 6) needs the backend RECREATED with the
# allowlist empty — env is fixed at container create, not at restart:
#   phase 1  legs 1–5, with CHAIN_KEEP_AGENTS=1 (leg 6 needs the pilot kept);
#   flip     comment PULL_MODE_PILOT_AGENTS out of .env, recreate the backend;
#   phase 2  leg 6, which deletes both agents whatever happens.
# .env is restored afterwards; the backend is left running with the restored
# env applied (a final recreate), so a dev stack comes back as it was.
#
# Writes DIR/phase1/chain-report.{json,md} and DIR/phase2/… . Exit 0 only when
# EVERY J24 chain row passed in both phases; 1 on any failure; 3 when nothing
# failed but a row was partial / not run.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REPORT_DIR="${RECOVERY_REPORT_DIR:-$PWD/recovery-report}"
if [ "${1:-}" = "--report-dir" ]; then REPORT_DIR="$2"; shift 2; fi
mkdir -p "$REPORT_DIR/phase1" "$REPORT_DIR/phase2"
REPORT_DIR="$(cd "$REPORT_DIR" && pwd)"

: "${TRINITY_API_URL:=http://localhost:8000}"
if [ -z "${TRINITY_TEST_PASSWORD:-}" ] && [ -z "${ADMIN_PASSWORD:-}" ]; then
  echo "run_recovery: set TRINITY_TEST_PASSWORD (or ADMIN_PASSWORD) to the target's admin password" >&2
  exit 2
fi

ENV_FILE="$REPO_ROOT/.env"
PILOT_LINE_RE='^PULL_MODE_PILOT_AGENTS='
if ! grep -q "$PILOT_LINE_RE" "$ENV_FILE" 2>/dev/null; then
  echo "run_recovery: $ENV_FILE carries no PULL_MODE_PILOT_AGENTS — boot the stack with" >&2
  echo "  PULL_MODE_PILOT_AGENTS=pytest-ephemeral-journey-b9pull" >&2
  echo "in .env (and restart the backend) before running this." >&2
  exit 2
fi

export TRINITY_CHAIN_TESTS=1 TRINITY_API_URL

wait_healthy() {
  for _ in $(seq 1 120); do
    if curl -fsS "$TRINITY_API_URL/health" >/dev/null 2>&1; then return 0; fi
    sleep 2
  done
  echo "run_recovery: backend never became healthy at $TRINITY_API_URL/health" >&2
  return 1
}

restore_env() {
  if [ -f "$ENV_FILE.j24-backup" ]; then
    mv "$ENV_FILE.j24-backup" "$ENV_FILE"
    # Apply the restored env so a dev stack is left as it was found. Best
    # effort: the CI stack is torn down right after anyway.
    (cd "$REPO_ROOT" && docker compose up -d backend >/dev/null 2>&1) || true
  fi
}
trap restore_env EXIT

cd "$REPO_ROOT/tests"

echo "run_recovery: phase 1 (pilot) — legs 1–5" >&2
rc1=0
CHAIN_KEEP_AGENTS=1 CHAIN_REPORT_DIR="$REPORT_DIR/phase1" \
  python -m pytest system_chains/test_j24_recovery_chain.py \
    -k "not depilot" -v -rs -p no:randomly --tb=short "$@" || rc1=$?

echo "run_recovery: flipping the pilot allowlist off and recreating the backend" >&2
cp "$ENV_FILE" "$ENV_FILE.j24-backup"
sed -i.bak "s|$PILOT_LINE_RE|# j24 phase 2 # PULL_MODE_PILOT_AGENTS=|" "$ENV_FILE" \
  && rm -f "$ENV_FILE.bak"
(cd "$REPO_ROOT" && docker compose up -d backend) || { echo "run_recovery: backend recreate failed" >&2; exit 1; }
wait_healthy || exit 1

echo "run_recovery: phase 2 (de-pilot) — leg 6" >&2
rc2=0
CHAIN_REPORT_DIR="$REPORT_DIR/phase2" \
  python -m pytest system_chains/test_j24_recovery_chain.py \
    -k "depilot" -v -rs -p no:randomly --tb=short "$@" || rc2=$?

# pytest rc 5 = nothing collected: nothing ran, which is not a pass.
for rc in "$rc1" "$rc2"; do
  if [ "$rc" = "5" ]; then
    echo "run_recovery: a phase collected no test — nothing ran, which is not a pass" >&2
    exit 1
  fi
done
if [ "$rc1" != "0" ] || [ "$rc2" != "0" ]; then exit 1; fi

# Green means every chain row PASSED — partial / not run is not evidence
# (chain_report.py's own rule, applied across both phase reports).
python - "$REPORT_DIR/phase1/chain-report.json" "$REPORT_DIR/phase2/chain-report.json" <<'PY'
import json, sys
verdicts = []
for path in sys.argv[1:]:
    with open(path) as fh:
        verdicts += [(c["chain"], c["verdict"]) for c in json.load(fh)["chains"]]
if not verdicts:
    print("run_recovery: no chain reported — nothing ran, which is not a pass",
          file=sys.stderr)
    sys.exit(1)
bad = [v for v in verdicts if v[1] != "passed"]
for chain, verdict in bad:
    print(f"run_recovery: {chain} is {verdict}, not passed", file=sys.stderr)
sys.exit(3 if bad else 0)
PY
