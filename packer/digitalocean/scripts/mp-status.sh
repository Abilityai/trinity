#!/bin/bash
# Report the DigitalOcean Marketplace listing state.
#
#   TRINITY_DO_APP_ID=<app_id> DIGITALOCEAN_TOKEN=dop_v1_... bash mp-status.sh
#
# Exit codes:
#   0  clear to submit
#   3  a submission is queued (pending or in review); the API refuses updates
#   4  rejected or unrecognised state; read the listing in the Vendor Portal
#   1  the API call failed, or the app is not on this token's team
set -uo pipefail
. "$(dirname "$0")/mp-lib.sh"

: "${TRINITY_DO_APP_ID:?TRINITY_DO_APP_ID must be set}"
: "${DIGITALOCEAN_TOKEN:?DIGITALOCEAN_TOKEN must be set}"

OUT="$(mktemp)"
trap 'rm -f "$OUT"' EXIT

code="$(mp_api GET "$MP_API" "$OUT")" || { echo "[mp-status] Vendor Portal unreachable" >&2; exit 1; }
if [ "$code" != "200" ]; then
    echo "[mp-status] Vendor Portal returned ${code}. A token from another team answers 401 or 403." >&2
    exit 1
fi

app="$(jq -c --arg id "$TRINITY_DO_APP_ID" '.apps[]? | select(.current.appId == $id)' "$OUT")"
if [ -z "$app" ]; then
    echo "[mp-status] app ${TRINITY_DO_APP_ID} is not on this token's team" >&2
    exit 1
fi

live="$(jq -r '.current.customData.version // "unknown"' <<<"$app")"
current="$(jq -r '.current.status.value // "unknown"' <<<"$app")"
queued="$(jq -r 'if .unpublished == null then "none" else (.unpublished.status.value // "unknown") end' <<<"$app")"

echo "live_version=${live} current=${current} queued=${queued}"

case "$queued" in
    none) [ "$current" = "approved" ] && exit 0; exit 4 ;;
    pending|"in review"|in_review) exit 3 ;;
    *) exit 4 ;;
esac
