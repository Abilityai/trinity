#!/bin/bash
# Submit a boot-checked snapshot to the DigitalOcean Vendor Portal.
#
#   TRINITY_DO_APP_ID=<app_id> DIGITALOCEAN_TOKEN=dop_v1_... \
#     bash mp-submit.sh <snapshot-id>
#
# Submitting consumes the snapshot and starts vendor review, so this refuses a
# snapshot with no pass record from boot-check.sh, and any listing state other
# than clear. app_id comes from the listing URL in the Vendor Portal.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/mp-lib.sh"

SNAPSHOT="${1:-}"
if ! [[ "$SNAPSHOT" =~ ^[0-9]+$ ]]; then
    echo "usage: mp-submit.sh <snapshot-id>" >&2
    exit 2
fi
: "${TRINITY_DO_APP_ID:?TRINITY_DO_APP_ID must be set}"
: "${DIGITALOCEAN_TOKEN:?DIGITALOCEAN_TOKEN must be set}"

RECORD="${HOME}/.trinity-marketplace/${SNAPSHOT}.pass"
if [ ! -f "$RECORD" ]; then
    echo "[mp-submit] no pass record for ${SNAPSHOT}. Run boot-check.sh on it first." >&2
    exit 1
fi
TAG="$(sed -n 's/^tag=//p' "$RECORD")"
[ -n "$TAG" ] || { echo "[mp-submit] ${RECORD} names no tag" >&2; exit 1; }

bash "$HERE/mp-status.sh"
status=$?
if [ "$status" != 0 ]; then
    echo "[mp-submit] listing is not clear to submit (mp-status exit ${status}). The snapshot is kept." >&2
    exit "$status"
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

code="$(mp_api GET "$MP_API" "$WORK/listing.json")" || exit 1
[ "$code" = "200" ] || { echo "[mp-submit] listing read returned ${code}" >&2; exit 1; }

# The live entries are kept, so a release only moves the Trinity version.
jq --arg id "$TRINITY_DO_APP_ID" --arg version "${TAG#v}" --arg tag "$TAG" --argjson image "$SNAPSHOT" '
    .apps[] | select(.current.appId == $id) | (.current.customData.softwareIncluded // []) as $sw
    | {
        reasonForUpdate: "Trinity \($tag)",
        osVersion: "Ubuntu 24.04",
        imageId: $image,
        softwareIncluded: (
            if any($sw[]; .name == "Trinity")
            then $sw | map(if .name == "Trinity" then .version = $version else . end)
            else $sw + [{name: "Trinity", version: $version,
                         website: "https://github.com/abilityai/trinity",
                         licenseType: "Apache-2.0"}]
            end)
      }' "$WORK/listing.json" > "$WORK/body.json" || exit 1

echo "[mp-submit] submitting snapshot ${SNAPSHOT} (${TAG}) to app ${TRINITY_DO_APP_ID}"
code="$(mp_api PATCH "${MP_API}/${TRINITY_DO_APP_ID}" "$WORK/response.json" "$WORK/body.json")" || exit 1

if [ "$code" != "200" ]; then
    echo "[mp-submit] FATAL: DigitalOcean returned ${code}" >&2
    cat "$WORK/response.json" >&2
    case "$code" in
      400) echo "[mp-submit] 400 usually means the listing is still in 'pending' or 'in review'." >&2 ;;
      403) echo "[mp-submit] 403 means the token is invalid, or the app or image is on another team." >&2 ;;
    esac
    exit 1
fi

rm -f "$RECORD"
echo "[mp-submit] submitted; listing status:"
jq -r '.status | "  \(.value) (\(.reason // "no reason given"))"' "$WORK/response.json" 2>/dev/null \
    || cat "$WORK/response.json"
