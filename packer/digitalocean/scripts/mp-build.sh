#!/bin/bash
# Build the DigitalOcean Marketplace snapshot for a release tag. Never submits.
#
#   DIGITALOCEAN_TOKEN=dop_v1_... TRINITY_SSH_KEY_ID=<id> TRINITY_SSH_KEY_FILE=<path> \
#     bash mp-build.sh <tag>
#
# Stdout carries only the snapshot ID; everything else goes to stderr. A failed build deletes the droplet
# on this run's tag only. A build killed outright leaves one tagged trinity-build-*.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/mp-lib.sh"

TAG="${1:-}"
if ! [[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[A-Za-z0-9.-]+)?$ ]]; then
    echo "usage: mp-build.sh <tag>  (a release tag such as v1.0.0)" >&2
    exit 2
fi
: "${DIGITALOCEAN_TOKEN:?DIGITALOCEAN_TOKEN must be set}"
: "${TRINITY_SSH_KEY_ID:?TRINITY_SSH_KEY_ID must be set: an SSH key ID on the DigitalOcean team}"
: "${TRINITY_SSH_KEY_FILE:?TRINITY_SSH_KEY_FILE must be set: the matching private key}"
[ -r "$TRINITY_SSH_KEY_FILE" ] || { echo "[mp-build] cannot read ${TRINITY_SSH_KEY_FILE}" >&2; exit 1; }

export DIGITALOCEAN_ACCESS_TOKEN="$DIGITALOCEAN_TOKEN"
export PKR_VAR_do_token="$DIGITALOCEAN_TOKEN"

cd "$HERE/.." || exit 1
ROOT="$(git rev-parse --show-toplevel)" || exit 1

# The image comes from the tag; these files come from this checkout. They must agree.
if ! git -C "$ROOT" rev-parse --verify --quiet "${TAG}^{commit}" >/dev/null; then
    echo "[mp-build] tag ${TAG} not found locally. Run: git fetch origin --tags" >&2
    exit 1
fi
if ! git -C "$ROOT" diff --quiet "$TAG" -- packer/digitalocean/files \
        packer/digitalocean/scripts/01-provision.sh \
        packer/digitalocean/scripts/90-cleanup-and-check.sh; then
    echo "[mp-build] the baked files in this checkout differ from ${TAG}. Build from the tag's content." >&2
    exit 1
fi

# An earlier snapshot may have failed its boot check, so none is reused automatically.
NAME_RE="^trinity-${TAG//./-}-[0-9]{8}-[0-9]{4}\$"
existing="$(doctl compute snapshot list --format ID,Name --no-header 2>/dev/null | awk -v re="$NAME_RE" '$2 ~ re')"
if [ -n "$existing" ]; then
    echo "[mp-build] existing snapshots for ${TAG}. Pass one to boot-check.sh to skip this build:" >&2
    echo "$existing" >&2
fi

RUN_TAG="trinity-build-$(date +%s)-$$"
BUILD_PID=""
cleanup() {
    local rc=$?
    [ -z "$BUILD_PID" ] || kill "$BUILD_PID" 2>/dev/null
    reap_tag "$RUN_TAG" || rc=1
    exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

packer init trinity.pkr.hcl >&2 || exit 1
packer build \
    -var "image_tag=${TAG}" \
    -var "run_tag=${RUN_TAG}" \
    -var "ssh_key_id=${TRINITY_SSH_KEY_ID}" \
    -var "ssh_private_key_file=${TRINITY_SSH_KEY_FILE}" \
    trinity.pkr.hcl >&2 &
BUILD_PID=$!
wait "$BUILD_PID"
build_rc=$?
BUILD_PID=""
[ "$build_rc" = 0 ] || exit "$build_rc"

# artifact_id is "<region>:<snapshot id>".
SNAPSHOT="$(jq -r '.builds[-1].artifact_id | split(":")[1]' manifest.json)"
if ! [[ "$SNAPSHOT" =~ ^[0-9]+$ ]]; then
    echo "[mp-build] no snapshot ID in manifest.json" >&2
    exit 1
fi
echo "$SNAPSHOT"
