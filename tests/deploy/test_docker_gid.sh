#!/usr/bin/env bash
# Issue #1131 regression lock — verify scripts/deploy/start.sh's
# ensure_docker_gid() picks the docker.sock group the backend must join on
# EVERY runtime, fixing the #874 macOS Docker Desktop regression (where the old
# code returned early on non-Linux and left the broken default 999).
#
# This test extracts the REAL _probe_docker_gid + ensure_docker_gid functions
# from start.sh (no copy → no drift) and drives them with shimmed `uname`,
# `getent`, and `docker` on PATH, so it runs deterministically on Linux CI and
# macOS alike. It covers all branches:
#   1.  Linux daemon up  → probe GID wins over getent (the #1131 fix: a stale
#                          host docker group must NOT override the real socket GID)
#   1b. Linux probe empty→ getent offline fallback
#   2.  non-Linux        → probed in-container GID (e.g. 0 on Docker Desktop)
#   3.  explicit DOCKER_GID=<n> in .env respected (incl. 0); no probe, no rewrite
#   4.  probe empty, no fallback (non-Linux daemon down) → WARNING + .env untouched
#   5.  `DOCKER_GID=` empty line is replaced in place (not duplicated)
#   6.  no DOCKER_GID line at all → appended
#
# Usage:  bash tests/deploy/test_docker_gid.sh
# Exit:   0 on success, 1 on failure.

set -u

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
START_SH="$REPO/scripts/deploy/start.sh"
ROOT="$(mktemp -d -t trinity-1131-XXXXXX)"
trap 'rm -rf "$ROOT"' EXIT

FAIL=0
fail() { echo "  FAIL: $*"; FAIL=1; }
ok()   { echo "  ok:   $*"; }

echo "=== #1131: ensure_docker_gid probes the in-container docker.sock GID ==="
echo "  start.sh: $START_SH"
echo "  work dir: $ROOT"
echo

# --- Extract the two functions verbatim from start.sh -----------------------
# Print from `_probe_docker_gid() {` up to (but excluding) the standalone
# `ensure_docker_gid` invocation line. Couples intentionally to start.sh's
# structure: if either function or the call is renamed, extraction fails loudly.
FUNCS_FILE="$ROOT/funcs.sh"
awk '
    /^_probe_docker_gid\(\) \{/ { f=1 }
    /^ensure_docker_gid$/       { f=0 }
    f                           { print }
' "$START_SH" > "$FUNCS_FILE"

# shellcheck source=/dev/null
source "$FUNCS_FILE"
if ! declare -F _probe_docker_gid >/dev/null || ! declare -F ensure_docker_gid >/dev/null; then
    echo "FAIL: could not extract _probe_docker_gid/ensure_docker_gid from start.sh"
    echo "      (function names or the standalone call line changed?)"
    exit 1
fi

# --- Build the shim toolbox -------------------------------------------------
# Each shim is driven by env vars set per-case. `docker run` touches a marker
# so we can assert when the probe was (not) invoked.
BIN="$ROOT/bin"
mkdir -p "$BIN"

cat > "$BIN/uname" <<'EOF'
#!/bin/sh
# Honor `uname -s`; SHIM_OS selects the reported OS (default Linux).
[ "$1" = "-s" ] && { echo "${SHIM_OS:-Linux}"; exit 0; }
echo "${SHIM_OS:-Linux}"
EOF

cat > "$BIN/getent" <<'EOF'
#!/bin/sh
# Emulate `getent group docker`. SHIM_GETENT is the full line (e.g.
# "docker:x:570:"); empty SHIM_GETENT means "no docker group" (exit 2).
if [ "$1" = "group" ] && [ "$2" = "docker" ]; then
    [ -n "${SHIM_GETENT:-}" ] && { echo "$SHIM_GETENT"; exit 0; }
    exit 2
fi
exit 2
EOF

cat > "$BIN/docker" <<'EOF'
#!/bin/sh
# Emulate the probe `docker run ... alpine stat -c %g ...`. Record the call,
# then echo SHIM_PROBE_GID (empty = daemon down / unpullable image → nothing
# on stdout, exit non-zero like a real failure the function pipes to `tr`).
if [ "$1" = "run" ]; then
    [ -n "${SHIM_DOCKER_MARKER:-}" ] && : > "$SHIM_DOCKER_MARKER"
    if [ -n "${SHIM_PROBE_GID:-}" ]; then echo "$SHIM_PROBE_GID"; exit 0; fi
    exit 1
fi
exit 0
EOF

chmod +x "$BIN/uname" "$BIN/getent" "$BIN/docker"
export PATH="$BIN:$PATH"

# --- Per-case harness -------------------------------------------------------
# Runs ensure_docker_gid in an isolated cwd with a fresh .env + probe marker.
# Captures stdout/stderr; leaves $CASE_ENV / $CASE_OUT / $CASE_MARKER for asserts.
setup_case() {
    CASE_DIR="$ROOT/$1"; mkdir -p "$CASE_DIR"
    CASE_ENV="$CASE_DIR/.env"
    CASE_OUT="$CASE_DIR/out.txt"
    CASE_MARKER="$CASE_DIR/probe-called"
    printf '%s' "$2" > "$CASE_ENV"   # initial .env contents (may be empty)
    rm -f "$CASE_MARKER"
    export SHIM_DOCKER_MARKER="$CASE_MARKER"
}
run_case() { ( cd "$CASE_DIR" && ensure_docker_gid ) >"$CASE_OUT" 2>&1; }
env_line() { grep -c '^DOCKER_GID=' "$CASE_ENV" 2>/dev/null || echo 0; }

# === Case 1: Linux daemon up → probe value wins over getent =================
# The #1131 fix: on Linux the probe is authoritative; a stale host `docker`
# group (getent=570) must NOT override the real in-container socket GID (999).
echo "[1] Linux daemon up → probe GID wins over getent"
SHIM_OS=Linux SHIM_GETENT="docker:x:570:" SHIM_PROBE_GID="999"
export SHIM_OS SHIM_GETENT SHIM_PROBE_GID
setup_case case1 $'FOO=bar\nDOCKER_GID=\n'
run_case
grep -qx 'DOCKER_GID=999' "$CASE_ENV" && ok "probe GID 999 wins (getent 570 ignored)" || fail "expected DOCKER_GID=999 (probe), got: $(grep '^DOCKER_GID' "$CASE_ENV")"
[ -f "$CASE_MARKER" ] && ok "probe invoked first on Linux (authoritative)" || fail "probe should run first on Linux"

# === Case 1b: Linux probe empty → getent offline fallback ===================
echo "[1b] Linux probe empty (daemon down) → getent fallback"
SHIM_OS=Linux SHIM_GETENT="docker:x:570:" SHIM_PROBE_GID=""
export SHIM_OS SHIM_GETENT SHIM_PROBE_GID
setup_case case1b $'DOCKER_GID=\n'
run_case
grep -qx 'DOCKER_GID=570' "$CASE_ENV" && ok "fell back to getent GID 570" || fail "expected getent fallback DOCKER_GID=570, got: $(grep '^DOCKER_GID' "$CASE_ENV")"
[ -f "$CASE_MARKER" ] && ok "probe attempted before fallback" || fail "probe should be attempted first"

# === Case 2: non-Linux → probed in-container GID ============================
echo "[2] Docker Desktop (Darwin) → probed GID 0"
SHIM_OS=Darwin SHIM_GETENT="" SHIM_PROBE_GID="0"
export SHIM_OS SHIM_GETENT SHIM_PROBE_GID
setup_case case2 $'DOCKER_GID=\n'
run_case
grep -qx 'DOCKER_GID=0' "$CASE_ENV" && ok "wrote DOCKER_GID=0" || fail "expected DOCKER_GID=0, got: $(grep '^DOCKER_GID' "$CASE_ENV")"
[ -f "$CASE_MARKER" ] && ok "probe invoked on non-Linux" || fail "probe should have run on Darwin"

# === Case 3: explicit override respected (incl. 0) =========================
echo "[3] explicit DOCKER_GID respected — no probe, no rewrite"
SHIM_OS=Darwin SHIM_GETENT="" SHIM_PROBE_GID="0"
export SHIM_OS SHIM_GETENT SHIM_PROBE_GID
setup_case case3 $'DOCKER_GID=999\n'
run_case
grep -qx 'DOCKER_GID=999' "$CASE_ENV" && ok "kept DOCKER_GID=999" || fail "override clobbered: $(grep '^DOCKER_GID' "$CASE_ENV")"
[ -f "$CASE_MARKER" ] && fail "probe ran despite explicit override" || ok "probe not invoked"
# explicit 0 is digits too → must be respected, not re-probed
SHIM_PROBE_GID="42"; export SHIM_PROBE_GID
setup_case case3b $'DOCKER_GID=0\n'
run_case
grep -qx 'DOCKER_GID=0' "$CASE_ENV" && ok "explicit DOCKER_GID=0 respected" || fail "DOCKER_GID=0 not respected: $(grep '^DOCKER_GID' "$CASE_ENV")"

# === Case 4: non-Linux probe empty, no fallback → WARNING + .env untouched ==
echo "[4] non-Linux probe empty (daemon down) → WARNING, no write"
SHIM_OS=Darwin SHIM_GETENT="" SHIM_PROBE_GID=""
export SHIM_OS SHIM_GETENT SHIM_PROBE_GID
setup_case case4 $'DOCKER_GID=\n'
run_case
grep -qi 'WARNING' "$CASE_OUT" && ok "emitted WARNING" || fail "no WARNING in output: $(cat "$CASE_OUT")"
grep -qx 'DOCKER_GID=' "$CASE_ENV" && ok ".env left untouched (still blank)" || fail ".env was modified: $(grep '^DOCKER_GID' "$CASE_ENV")"
[ "$(grep -c '^DOCKER_GID=' "$CASE_ENV")" -eq 1 ] && ok "no duplicate line appended" || fail "unexpected DOCKER_GID line count"

# === Case 5: empty `DOCKER_GID=` line replaced in place (not appended) =====
echo "[5] blank DOCKER_GID= line replaced in place (single line)"
SHIM_OS=Linux SHIM_GETENT="docker:x:570:" SHIM_PROBE_GID=""
export SHIM_OS SHIM_GETENT SHIM_PROBE_GID
setup_case case5 $'A=1\nDOCKER_GID=\nB=2\n'
run_case
[ "$(env_line)" -eq 1 ] && ok "exactly one DOCKER_GID line" || fail "expected 1 DOCKER_GID line, got $(env_line)"
grep -qx 'DOCKER_GID=570' "$CASE_ENV" && ok "replaced in place" || fail "not replaced: $(grep '^DOCKER_GID' "$CASE_ENV")"
grep -qx 'B=2' "$CASE_ENV" && ok "surrounding lines preserved" || fail "clobbered surrounding lines"
[ -f "$CASE_ENV.bak" ] && fail "left a .env.bak behind" || ok "no .env.bak residue"

# === Case 6: no DOCKER_GID line at all → appended ==========================
echo "[6] no DOCKER_GID line → appended"
SHIM_OS=Linux SHIM_GETENT="docker:x:570:" SHIM_PROBE_GID=""
export SHIM_OS SHIM_GETENT SHIM_PROBE_GID
setup_case case6 $'A=1\nB=2\n'
run_case
[ "$(env_line)" -eq 1 ] && ok "appended exactly one DOCKER_GID line" || fail "expected 1 DOCKER_GID line, got $(env_line)"
grep -qx 'DOCKER_GID=570' "$CASE_ENV" && ok "appended DOCKER_GID=570" || fail "not appended: $(cat "$CASE_ENV")"

echo
echo "=== RESULT ==="
if [ "$FAIL" = "0" ]; then
    echo "OK: ensure_docker_gid resolves the docker.sock GID correctly on every runtime."
    echo "    The #874 macOS Docker Desktop regression (#1131) is locked."
    exit 0
else
    echo "The #1131 invariant is violated — ensure_docker_gid mis-detects the GID."
    exit 1
fi
