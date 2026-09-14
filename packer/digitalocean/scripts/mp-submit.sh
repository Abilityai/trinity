#!/bin/bash
# Submit the snapshot Packer just built to the DigitalOcean Vendor Portal.
#
# Runs as a `shell-local` post-processor after the `manifest` post-processor, so
# the snapshot id is read from the build's own record rather than copied by hand
# out of the build log. This is DigitalOcean's documented automation shape
# (marketplace-partners README, "Automating with Packer").
#
# NO-OP BY DEFAULT. A plain `packer build` — the local iteration case, and every
# build before the listing exists — must not try to submit anything, so the
# script exits 0 with an explanation when TRINITY_DO_APP_ID is unset. Submission
# is opt-in per invocation:
#
#   TRINITY_DO_APP_ID=<app_id> \
#   DIGITALOCEAN_API_TOKEN=dop_v1_... \
#   TRINITY_SUBMIT_REASON="Trinity v0.9.5" \
#     packer build -var "image_tag=v0.9.5" ... trinity.pkr.hcl
#
# app_id comes from the listing URL in the Vendor Portal.
set -euo pipefail

MANIFEST="${TRINITY_PACKER_MANIFEST:-manifest.json}"

if [ -z "${TRINITY_DO_APP_ID:-}" ]; then
    echo "[mp-submit] TRINITY_DO_APP_ID unset — snapshot built, not submitted."
    echo "[mp-submit] Submit later from the Vendor Portal, or re-run with the"
    echo "[mp-submit] variables documented at the top of this script."
    exit 0
fi

: "${DIGITALOCEAN_API_TOKEN:?DIGITALOCEAN_API_TOKEN must be set to submit}"

if [ ! -f "$MANIFEST" ]; then
    echo "[mp-submit] FATAL: $MANIFEST not found — the manifest post-processor" >&2
    echo "[mp-submit]        must run before this one." >&2
    exit 1
fi

# artifact_id is "<region>:<snapshot id>"; the API wants the numeric id alone.
IMAGE_ID="$(jq -r '.builds[-1].artifact_id | split(":")[1]' "$MANIFEST")"
if [ -z "$IMAGE_ID" ] || [ "$IMAGE_ID" = "null" ]; then
    echo "[mp-submit] FATAL: could not read a snapshot id from $MANIFEST." >&2
    exit 1
fi

REASON="${TRINITY_SUBMIT_REASON:-Trinity ${TRINITY_IMAGE_TAG:-update}}"
echo "[mp-submit] submitting snapshot ${IMAGE_ID} to app ${TRINITY_DO_APP_ID}"

# `-f` so an API error is a build failure rather than a success that printed
# JSON. The two states worth recognising in the output:
#   400 — the app is in `pending` or `in review`; a previous submission is still
#         queued and DigitalOcean refuses further updates until it clears.
#   403 — token invalid, or the app/image belongs to a different team.
HTTP_BODY="$(mktemp)"
trap 'rm -f "$HTTP_BODY"' EXIT
HTTP_CODE="$(curl -sS -o "$HTTP_BODY" -w '%{http_code}' -X PATCH \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${DIGITALOCEAN_API_TOKEN}" \
    -d "$(jq -n \
          --arg reason "$REASON" \
          --arg os "Ubuntu 24.04" \
          --arg version "${TRINITY_IMAGE_TAG:-}" \
          --argjson imageId "$IMAGE_ID" \
          '{reasonForUpdate: $reason, osVersion: $os, imageId: $imageId,
            softwareIncluded: [{name: "Trinity", version: $version,
                                website: "https://github.com/abilityai/trinity",
                                licenseType: "Apache-2.0",
                                licenseLink: "https://github.com/abilityai/trinity/blob/main/LICENSE"}]}')" \
    "https://api.digitalocean.com/api/v1/vendor-portal/apps/${TRINITY_DO_APP_ID}")"

if [ "$HTTP_CODE" != "200" ]; then
    echo "[mp-submit] FATAL: DigitalOcean returned ${HTTP_CODE}" >&2
    cat "$HTTP_BODY" >&2
    case "$HTTP_CODE" in
      400) echo "[mp-submit] 400 usually means the listing is still in 'pending'" >&2
           echo "[mp-submit] or 'in review' — it cannot be updated until that clears." >&2 ;;
      403) echo "[mp-submit] 403 means the token is invalid, or the app/image is" >&2
           echo "[mp-submit] on a different team than the token's." >&2 ;;
    esac
    exit 1
fi

echo "[mp-submit] submitted; listing status:"
jq -r '.status | "  \(.value) (\(.reason // "no reason given"))"' "$HTTP_BODY" 2>/dev/null || cat "$HTTP_BODY"
