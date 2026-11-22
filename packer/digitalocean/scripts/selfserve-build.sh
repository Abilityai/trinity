#!/bin/bash
# Build the self-serve snapshot for one platform commit (trinity-enterprise#1347).
#
#   DIGITALOCEAN_TOKEN=dop_v1_... TRINITY_SSH_KEY_ID=<id> TRINITY_SSH_KEY_OP=<op://...> \
#   TRINITY_ENTERPRISE_TOKEN=<read-only token for the enterprise repository> \
#     bash selfserve-build.sh <commit>
#
# <commit> is a full or abbreviated SHA of abilityai/trinity. Optional:
# TRINITY_BUILD_REGION (default sfo2), the region the snapshot is created in.
#
# Refuses, before anything is created, when:
#   - the commit's five sha-<short7> images are not anonymously pullable from GHCR;
#   - the packer files baked into the image differ between this checkout and the
#     commit, because the commit's start.sh and this checkout's first boot have
#     to agree.
#
# Stdout carries only the snapshot ID; everything else goes to stderr. A failed
# build deletes the droplet on this run's tag only.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

REF="${1:-}"
if ! [[ "$REF" =~ ^[0-9a-f]{7,40}$ ]]; then
    echo "usage: selfserve-build.sh <commit>  (a commit SHA of abilityai/trinity, 7 to 40 hex characters)" >&2
    exit 2
fi
. "$HERE/mp-lib.sh"
: "${DIGITALOCEAN_TOKEN:?DIGITALOCEAN_TOKEN must be set}"
: "${TRINITY_SSH_KEY_ID:?TRINITY_SSH_KEY_ID must be set: an SSH key ID on the DigitalOcean team}"
: "${TRINITY_ENTERPRISE_TOKEN:?TRINITY_ENTERPRISE_TOKEN must be set: a read-only token for the enterprise repository}"

export DIGITALOCEAN_ACCESS_TOKEN="$DIGITALOCEAN_TOKEN"
export PKR_VAR_do_token="$DIGITALOCEAN_TOKEN"
# Through the environment, never argv.
export PKR_VAR_enterprise_git_token="$TRINITY_ENTERPRISE_TOKEN"

cd "$HERE/.." || exit 1
ROOT="$(git rev-parse --show-toplevel)" || exit 1

# --- the commit ----------------------------------------------------------------
COMMIT="$(curl -fsS --max-time 20 -H 'Accept: application/vnd.github.sha' \
    "https://api.github.com/repos/abilityai/trinity/commits/${REF}")" || {
    echo "[selfserve-build] commit ${REF} not found on abilityai/trinity" >&2
    exit 1
}
if ! [[ "$COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
    echo "[selfserve-build] could not resolve ${REF} to one commit (got '${COMMIT:0:80}')" >&2
    exit 1
fi
SHORT="${COMMIT:0:7}"
TAG="sha-${SHORT}"
echo "[selfserve-build] commit ${COMMIT}, images ${TAG}" >&2

# --- the baked files agree with the commit ----------------------------------------
if ! git -C "$ROOT" cat-file -e "${COMMIT}^{commit}" 2>/dev/null; then
    echo "[selfserve-build] ${COMMIT} is not in this checkout. Run: git fetch origin ${COMMIT}" >&2
    exit 1
fi
if ! git -C "$ROOT" diff --quiet "$COMMIT" -- packer/digitalocean/files \
        packer/digitalocean/selfserve \
        packer/digitalocean/scripts/01-provision.sh \
        packer/digitalocean/scripts/90-cleanup-and-check.sh \
        packer/digitalocean/trinity-selfserve.pkr.hcl; then
    echo "[selfserve-build] the packer files in this checkout differ from ${SHORT}. Build from that commit's content." >&2
    exit 1
fi

# --- the images exist and are public ---------------------------------------------
missing=""
for img in backend frontend scheduler mcp-server agent-base; do
    repo="abilityai/trinity-${img}"
    tok="$(curl -fsS --max-time 20 "https://ghcr.io/token?scope=repository:${repo}:pull" | jq -r '.token // empty')"
    code="$(printf 'header = "Authorization: Bearer %s"\n' "$tok" | curl -sS -K - --max-time 20 -o /dev/null -w '%{http_code}' -I \
        -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.manifest.v1+json' \
        "https://ghcr.io/v2/${repo}/manifests/${TAG}")"
    [ "$code" = "200" ] || missing="${missing} trinity-${img}"
done
if [ -n "$missing" ]; then
    echo "[selfserve-build] not on GHCR at ${TAG}:${missing}" >&2
    echo "  Publish them: gh workflow run publish-images.yml --repo abilityai/trinity --ref <branch containing ${SHORT}>" >&2
    exit 7
fi

# --- build -------------------------------------------------------------------------
RUN_TAG="trinity-build-selfserve-$(date +%s)-$$"
BUILD_PID=""
cleanup() {
    local rc=$?
    trap - EXIT
    trap '' INT TERM HUP
    [ -z "$BUILD_PID" ] || kill "$BUILD_PID" 2>/dev/null
    reap_tag "$RUN_TAG" || rc=1
    drop_ssh_key
    exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

use_ssh_key || exit 1

packer init trinity-selfserve.pkr.hcl >&2 || exit 1
packer build \
    -var "commit=${COMMIT}" \
    -var "run_tag=${RUN_TAG}" \
    -var "region=${TRINITY_BUILD_REGION:-sfo2}" \
    -var "ssh_key_id=${TRINITY_SSH_KEY_ID}" \
    -var "ssh_private_key_file=${TRINITY_SSH_KEY_FILE}" \
    trinity-selfserve.pkr.hcl >&2 &
BUILD_PID=$!
wait "$BUILD_PID"
build_rc=$?
BUILD_PID=""
[ "$build_rc" = 0 ] || exit "$build_rc"

SNAPSHOT="$(jq -r '.builds[-1].artifact_id | split(":")[1]' manifest-selfserve.json)"
if ! [[ "$SNAPSHOT" =~ ^[0-9]+$ ]]; then
    echo "[selfserve-build] no snapshot ID in manifest-selfserve.json" >&2
    exit 1
fi
echo "$SNAPSHOT"
