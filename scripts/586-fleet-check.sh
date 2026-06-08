#!/bin/bash
# 586-fleet-check.sh — fleet-wide audit for Issue #586 close-out.
#
# Searches the last 7 days of Vector-aggregated agent logs for the bug-class
# signals listed in the #586 close-out plan and prints a per-container
# summary. Returns non-zero if "still stuck after Ns" or "no result message
# after" events are found post-#728 deploy, which indicates the platform
# fix did not fully cover the case and the issue should NOT be closed.
#
# Usage: ./scripts/586-fleet-check.sh [DAYS]
#   DAYS — optional lookback window (default 7).

set -euo pipefail

DAYS="${1:-7}"
SINCE=$(date -u -d "${DAYS} days ago" +%Y-%m-%dT%H:%M:%SZ)
OUT=$(mktemp -t 586-fleet.XXXXXX.json)
trap 'rm -f "${OUT}"' EXIT

# Container is Linux (Alpine); date syntax is GNU. Vector writes daily-
# rotated files at /data/logs/agents-YYYY-MM-DD.json (config/vector.yaml).
# Regex notes: %.1fs decimals → [0-9.]+s; "(Ns) exceeded" needs escaped parens.
docker exec trinity-vector sh -c "
  cat /data/logs/agents-*.json | \
  jq -rc --arg since '${SINCE}' '
    select(.timestamp >= \$since) |
    select(.message | test(\"still stuck after [0-9.]+s|no result message after|Drain budget \\\\([0-9]+s\\\\) exceeded|Killed [0-9]+ orphan|Orphan pipe-writer SIGKILL\")) |
    {ts: .timestamp, container: .container_name, msg: .message}
  '
" | tee "${OUT}"

echo
echo "Per-container summary:"
jq -src 'group_by(.container) | map({container: .[0].container, count: length})' "${OUT}"

# Gate the close: any "still stuck after Ns" or "no result message after"
# match means the platform fix did not fully cover the case.
if jq -r 'select(.msg | test("still stuck after [0-9.]+s|no result message after")) | .msg' "${OUT}" \
     | grep -q .; then
    echo "FAIL: residual #586-class events found — DO NOT close." >&2
    exit 1
fi

echo "PASS: no residual #586-class events in last ${DAYS} days."
