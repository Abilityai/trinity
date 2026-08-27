#!/usr/bin/env bash
# Trinity Full Test Suite — one command, an HONEST full-suite verdict (#2080).
#
# "Honest" is the whole point, and it means four specific things:
#
#   1. EVERY tier actually runs. A tier that cannot run is a FAILURE, never a
#      silent skip — the previous version aborted at pytest collection before a
#      single test executed and still looked like "a test run that went badly".
#   2. Harness breakage cannot masquerade as test results. Collection errors,
#      an unusable venv, and a missing Postgres are each reported as harness
#      failures with their own exit status, not as red tests.
#   3. A skip outside the named allowlist FAILS the run (see skip-audit below).
#      An unexplained skip is the failure mode this suite exists to catch: it
#      is indistinguishable from a pass in every summary line pytest prints.
#   4. Nothing can hang the run. Every tier carries a timeout and
#      `--timeout-method=thread`, so one blocking socket costs one test, not
#      the afternoon.
#
# Tiers run in sequence; ALL of them run even after one fails (a failing tier
# must not hide the state of the others), and the script exits non-zero if any
# tier failed. Per-tier results are summarised at the end.
#
# Usage:
#   tests/run-full.sh                 # everything
#   tests/run-full.sh --tier unit     # one tier (repeatable)
#   tests/run-full.sh --no-pg         # skip the disposable-Postgres tier
#   tests/run-full.sh -k pattern      # extra args are passed to pytest

set -uo pipefail   # deliberately NOT -e: see the tier loop below.

cd "$(dirname "$0")"
TESTS_DIR="$(pwd)"
REPO_ROOT="$(cd .. && pwd)"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
REPORT_DIR="${TESTS_DIR}/reports"
LOG_DIR="${REPORT_DIR}/full-${TIMESTAMP}"
mkdir -p "$LOG_DIR"

# ---------------------------------------------------------------------------
# Interpreter parity (#1891 single-declaration rule)
# ---------------------------------------------------------------------------
# The image pin is declared in exactly one place. Read it rather than restating
# it here — a second copy is how the venv drifted to 3.14 while the images ran
# 3.13, which is what produced the pytest-timeout signal-reentrancy
# INTERNALERROR when a hung test was interrupted.
PINNED_PY="$(grep -oE 'python:3\.[0-9]+' "${REPO_ROOT}/docker/backend/Dockerfile" | head -1 | cut -d: -f2)"

RESULTS=()          # "name<TAB>status<TAB>detail"
FAILED=0

log()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
note() { printf '   %s\n' "$*"; }

record() {  # name status detail
    RESULTS+=("$1	$2	$3")
    [ "$2" = "PASS" ] || FAILED=1
}

# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------
WANTED_TIERS=()
RUN_PG=1
PYTEST_EXTRA=()
while [ $# -gt 0 ]; do
    case "$1" in
        --tier)   WANTED_TIERS+=("$2"); shift 2 ;;
        --no-pg)  RUN_PG=0; shift ;;
        *)        PYTEST_EXTRA+=("$1"); shift ;;
    esac
done

wants() {  # tier name -> 0 if it should run
    [ ${#WANTED_TIERS[@]} -eq 0 ] && return 0
    local t
    for t in "${WANTED_TIERS[@]}"; do [ "$t" = "$1" ] && return 0; done
    return 1
}

# ---------------------------------------------------------------------------
# Venv self-heal
# ---------------------------------------------------------------------------
log "venv"
if [ ! -d .venv ]; then
    note "creating tests/.venv"
    python3 -m venv .venv || { echo "could not create venv"; exit 2; }
fi
# shellcheck disable=SC1091
source .venv/bin/activate

VENV_PY="$(python -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
PY_MISMATCH=0
if [ -n "$PINNED_PY" ] && [ "$VENV_PY" != "$PINNED_PY" ]; then
    # A harness failure, not a test failure — and loud, because a mismatched
    # interpreter changes WHICH tests exist (`audioop` and `crypt` were both
    # removed in 3.13) without changing any result line. The suite can look
    # identically green while covering less.
    echo "HARNESS: tests/.venv runs Python ${VENV_PY} but the images pin ${PINNED_PY}." >&2
    echo "         Recreate it:  rm -rf tests/.venv && python${PINNED_PY} -m venv tests/.venv" >&2
    if [ "${TRINITY_TEST_ALLOW_PY_MISMATCH:-0}" = "1" ]; then
        # An explicit, printed opt-out — not a silent one. It is carried into
        # the final summary so a run made under it can never be quoted as a
        # clean full-suite result.
        echo "         TRINITY_TEST_ALLOW_PY_MISMATCH=1 — continuing under protest" >&2
        PY_MISMATCH=1
    else
        exit 2
    fi
else
    note "python ${VENV_PY} (matches the image pin)"
fi

# Idempotent: pip is a no-op when everything is satisfied, and this is what
# stops a newly-declared dependency (hypothesis, audioop-lts, openpyxl,
# reportlab, alembic, psycopg2-binary) from aborting a whole tier at collection.
note "syncing requirements-test.txt"
if ! pip install -q -r requirements-test.txt 2>"${LOG_DIR}/pip.log"; then
    echo "HARNESS: pip install -r tests/requirements-test.txt failed:" >&2
    tail -20 "${LOG_DIR}/pip.log" >&2
    exit 2
fi

# shellcheck disable=SC1091
source ./setup-env.sh

# ---------------------------------------------------------------------------
# A live test agent, so agent-dependent tests RUN instead of skipping
# ---------------------------------------------------------------------------
if [ -z "${TEST_AGENT_NAME:-}" ]; then
    if ensured="$(python ./harness/ensure_test_agent.py 2>"${LOG_DIR}/agent-setup.log")"; then
        export TEST_AGENT_NAME="$ensured"
        note "TEST_AGENT_NAME=${TEST_AGENT_NAME}"
    else
        note "no live backend for a test agent — agent-dependent skips will be audited"
        tail -3 "${LOG_DIR}/agent-setup.log" 2>/dev/null | sed 's/^/     /'
    fi
fi

# ---------------------------------------------------------------------------
# Tier runner
# ---------------------------------------------------------------------------
# `-rs` is mandatory on every tier: the skip audit reads it. Without it a skip
# prints as a dot and the audit has nothing to check.
run_tier() {  # name timeout_s paths...
    local name="$1" tmo="$2"; shift 2
    wants "$name" || return 0
    log "tier: ${name}"
    local out="${LOG_DIR}/${name}.log"
    python -m pytest "$@" \
        -rs --tb=short -q \
        --timeout="$tmo" --timeout-method=thread \
        "${PYTEST_EXTRA[@]+"${PYTEST_EXTRA[@]}"}" 2>&1 | tee "$out"
    local rc=${PIPESTATUS[0]}
    local summary
    # pytest's own counts line only — `grep passed|failed` also matches a
    # deprecation warning that happens to contain the word, which is how the
    # summary column filled up with Pydantic noise on the first run.
    summary="$(grep -oE '[0-9]+ (passed|failed|error|skipped)[^|]*' "$out" | tail -1)"
    case "$rc" in
        0) record "$name" "PASS" "${summary:-ok}" ;;
        5) record "$name" "FAIL" "no tests collected — a tier that runs nothing is not a pass" ;;
        2) record "$name" "FAIL" "COLLECTION/INTERNAL error — harness breakage, not a test result" ;;
        *) record "$name" "FAIL" "${summary:-exit ${rc}}" ;;
    esac
}

# unit/ is its own invocation: it imports the backend's `utils` package, and a
# separate process is the only way to keep that free of the live-backend
# fixtures the root conftest installs.
run_tier unit        300 unit/
run_tier integration 600 integration/
run_tier git-sync    300 git_sync/
run_tier security    300 security/
run_tier scheduler   300 scheduler_tests/
run_tier agent-server 300 agent_server/
# Journey tier (#2335, Rail R1). Live-stack by definition: these drive the
# public API against a running instance the way a person does. Longer per-test
# timeout than any other tier because a journey legitimately waits on a
# container coming up — 90s to `running` is the platform's own bound, not this
# harness's impatience.
run_tier journeys 900 journeys/

# Root-level live-backend tests (everything not owned by a tier above).
#
# The ignore list is DERIVED, not hand-mirrored. It was written out by hand and
# `journeys` was added as a tier above without being added below, so `api`
# collected `journeys/` a second time from `.` — every journey agent created and
# torn down twice, the container time paid twice, and one journey failure
# reported under two tier names. That is the new-producer-not-in-the-consumer-
# allowlist class, and adding one `--ignore=` would leave the next tier to
# repeat it.
#
# TIER_DIRS is the single list. `NON_TIER_DIRS` are directories `api` must also
# not collect but that no tier owns: `manual/` and `deploy/` are opt-in, and
# `harness/` and the support packages hold no tests of their own.
TIER_DIRS=(unit integration git_sync security scheduler_tests agent_server journeys)
NON_TIER_DIRS=(manual deploy harness fixtures testing_utils testkit)

# And the list is CHECKED against the tree, because a derived list is only as
# good as its inputs: a new directory under tests/ that nobody wired would still
# be silently swept into `api`. Failing here names it instead.
_unclassified=()
for _d in tests/*/; do
    _d="${_d#tests/}"; _d="${_d%/}"
    case " ${TIER_DIRS[*]} ${NON_TIER_DIRS[*]} " in
        *" ${_d} "*) ;;
        *) _unclassified+=("${_d}") ;;
    esac
done
if [ ${#_unclassified[@]} -gt 0 ]; then
    printf '\n\033[1;31m== tests/run-full.sh: unclassified test directories\033[0m\n'
    printf '   %s\n' "${_unclassified[@]}"
    printf '   Add each to TIER_DIRS (with its own run_tier line) or to NON_TIER_DIRS.\n'
    printf '   Left unwired they are collected a SECOND time by the api tier.\n\n'
    exit 1
fi

api_ignores=()
for _d in "${TIER_DIRS[@]}" "${NON_TIER_DIRS[@]}"; do api_ignores+=("--ignore=${_d}"); done
run_tier api 900 . "${api_ignores[@]}"

# Documented run-standalone tests: they assert route ORDER on a pristine
# `sys.modules`, so any earlier import of the app makes them vacuous. Separate
# invocations inside the same orchestrated run — the point of #2080 is that
# "documented as standalone" must still mean "actually executed".
if wants standalone; then
    log "tier: standalone"
    rc_total=0
    while IFS= read -r spec; do
        [ -z "$spec" ] && continue
        note "$spec"
        python -m pytest "$spec" -rs -q --timeout=120 --timeout-method=thread \
            >>"${LOG_DIR}/standalone.log" 2>&1 || rc_total=1
    done < <(python ./harness/list_standalone_tests.py)
    if [ "$rc_total" = 0 ]; then
        record standalone PASS "$(grep -cE '^[0-9]+ passed' "${LOG_DIR}/standalone.log" || true) invocation(s)"
    else
        record standalone FAIL "see ${LOG_DIR}/standalone.log"
    fi
fi

# ---------------------------------------------------------------------------
# Postgres / Alembic tier — a disposable container, locally, not just in CI
# ---------------------------------------------------------------------------
# The dual-track migration contract (Invariant #3) is the one thing a
# SQLite-only run cannot exercise at all: on SQLite these tests skip with "no
# PostgreSQL configured", which reads as green.
if [ "$RUN_PG" = 1 ] && wants postgres; then
    log "tier: postgres (disposable container + alembic upgrade head)"
    PG_NAME="trinity-tests-pg-${TIMESTAMP}"
    PG_PORT="${TRINITY_TEST_PG_PORT:-55433}"
    # Reap a container leaked by an earlier run that was killed before its trap
    # fired (Ctrl-C, a `timeout`, a crashed shell). Without this the next run
    # dies on "port is already allocated" and reports it as a Postgres failure —
    # a harness artifact wearing a tier failure's clothes. Matched by LABEL, so
    # it can only ever remove containers this script created.
    leaked="$(docker ps -aq --filter label=trinity-tests-pg=1 2>/dev/null)"
    if [ -n "$leaked" ]; then
        note "removing leaked postgres container(s) from an earlier run"
        docker rm -f $leaked >/dev/null 2>&1 || true
    fi
    if ! docker run -d --rm --name "$PG_NAME" --label trinity-tests-pg=1 \
            -e POSTGRES_PASSWORD=trinity_test -e POSTGRES_DB=trinity_test \
            -p "${PG_PORT}:5432" postgres:16-alpine >/dev/null 2>"${LOG_DIR}/pg.log"; then
        record postgres FAIL "could not start postgres:16-alpine (see ${LOG_DIR}/pg.log)"
    else
        trap 'docker rm -f "$PG_NAME" >/dev/null 2>&1 || true' EXIT INT TERM
        for _ in $(seq 1 30); do
            docker exec "$PG_NAME" pg_isready -U postgres -q 2>/dev/null && break
            sleep 1
        done
        # TWO variables, deliberately: the backend engine reads DATABASE_URL,
        # while the PG-gated tests gate on TEST_POSTGRES_URL (db_harness). The
        # first run of this tier exported only DATABASE_URL — alembic upgraded
        # a real database and every PG test still skipped with "no PostgreSQL
        # reachable", i.e. the tier passed while proving nothing. That is the
        # exact shape of the problem #2080 is about, so it is worth the comment.
        export DATABASE_URL="postgresql+psycopg2://postgres:trinity_test@localhost:${PG_PORT}/trinity_test"
        export TEST_POSTGRES_URL="postgresql://postgres:trinity_test@localhost:${PG_PORT}/trinity_test"

        note "alembic upgrade head"
        if (cd "${REPO_ROOT}/src/backend" && python -m alembic upgrade head) \
                >"${LOG_DIR}/alembic.log" 2>&1; then
            run_tier postgres 600 unit/ -k "postgres or alembic or migration or dual_backend"
        else
            record postgres FAIL "alembic upgrade head failed (see ${LOG_DIR}/alembic.log)"
            tail -15 "${LOG_DIR}/alembic.log" | sed 's/^/     /'
        fi
        unset DATABASE_URL TEST_POSTGRES_URL
        docker rm -f "$PG_NAME" >/dev/null 2>&1 || true
        trap - EXIT INT TERM
    fi
fi

# ---------------------------------------------------------------------------
# Skip audit — an unexplained skip is a failure, not a green
# ---------------------------------------------------------------------------
log "skip audit"
if python ./harness/audit_skips.py "$LOG_DIR"; then
    record skip-audit PASS "every skip reason is on the allowlist"
else
    record skip-audit FAIL "unallowlisted skip reason(s) — see above"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "========================================="
echo "  FULL SUITE SUMMARY  (${TIMESTAMP})"
echo "========================================="
for row in "${RESULTS[@]}"; do
    IFS=$'\t' read -r name status detail <<<"$row"
    printf '  %-14s %-5s %s\n' "$name" "$status" "$detail"
done
echo ""
echo "  logs: ${LOG_DIR}"
if [ "$PY_MISMATCH" = 1 ]; then
    echo "  NOTE: ran on Python ${VENV_PY}, NOT the image's ${PINNED_PY}"
    echo "        (TRINITY_TEST_ALLOW_PY_MISMATCH=1) — not a clean full-suite result"
fi
if [ "$FAILED" = 0 ]; then
    echo "  RESULT: PASS"
else
    echo "  RESULT: FAIL"
fi
exit "$FAILED"
