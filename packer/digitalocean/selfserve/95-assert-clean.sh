#!/bin/bash
# LAST build step of the self-serve snapshot (trinity-enterprise#1347). It only
# reads: it runs after DigitalOcean's cleanup and img_check, and nothing may
# write to the image after those.
#
# Fails the build, naming every finding, unless the snapshot:
#   - holds no secrets: no .env, no git credentials, no token in either repository;
#   - holds no SSH host keys and an empty machine id;
#   - holds no database that has ever started: no containers, no volumes, no
#     data directory with anything in it;
#   - holds no setup claim;
#   - IS the commit it claims to be, with the enterprise module at that commit's
#     pin and every Trinity image at sha-<short7>.
#
# SNAPSHOT_ROOT prefixes every path, for the unit tests only.
set -uo pipefail

R="${SNAPSHOT_ROOT:-}"
T="${R}/opt/trinity"
ENT="${T}/src/backend/enterprise"
FAILED=0

bad() { echo "[assert-clean] FAIL: $*" >&2; FAILED=1; }

# --- identity of the machine -------------------------------------------------
for k in "${R}"/etc/ssh/ssh_host_*; do
    [ -e "$k" ] && bad "SSH host key present: ${k#"$R"}"
done
if [ -s "${R}/etc/machine-id" ]; then
    bad "/etc/machine-id is not empty"
fi
if [ -f "${R}/var/lib/dbus/machine-id" ] && [ ! -L "${R}/var/lib/dbus/machine-id" ] \
        && [ -s "${R}/var/lib/dbus/machine-id" ]; then
    bad "/var/lib/dbus/machine-id holds a machine id"
fi

# --- secrets -----------------------------------------------------------------
[ -e "${T}/.env" ] && bad ".env present: secrets would be shared by every instance"
[ -e "${R}/root/.git-credentials" ] && bad "git credential store present: /root/.git-credentials"
for cfg in "${T}/.git/config" "${ENT}/.git/config" "${R}/root/.gitconfig" "${R}/etc/gitconfig"; do
    [ -f "$cfg" ] || continue
    if grep -qiE 'extraheader|credential|://[^/@[:space:]]+@' "$cfg"; then
        bad "git credential in ${cfg#"$R"}"
    fi
done
if grep -rqsE 'x-access-token|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}' \
        "${T}/.git" "${ENT}/.git" "${R}/etc/trinity" 2>/dev/null; then
    bad "a GitHub token or credential header is in the repositories or /etc/trinity"
fi

# --- no database that has ever started --------------------------------------
data="${T}/trinity-data"
if [ -d "$data" ] && [ -n "$(ls -A "$data" 2>/dev/null)" ]; then
    bad "data directory ${data#"$R"} is not empty"
fi
if [ -n "$(docker volume ls -q 2>/dev/null)" ]; then
    bad "Docker volume present: $(docker volume ls -q | tr '\n' ' ')"
fi
if [ -n "$(docker ps -aq 2>/dev/null)" ]; then
    bad "Docker container present: something ran during the build"
fi

# --- no claim ----------------------------------------------------------------
claims="$(find "${R}/etc/trinity" "$T" -name 'setup-claim*' -not -path '*/.git/*' 2>/dev/null)"
[ -z "$claims" ] || bad "setup-claim present: ${claims}"

# --- what was baked ----------------------------------------------------------
commit="$(cat "${R}/etc/trinity/baked-commit" 2>/dev/null || true)"
pin="$(cat "${R}/etc/trinity/baked-enterprise-pin" 2>/dev/null || true)"
tag="$(cat "${R}/etc/trinity/baked-image-tag" 2>/dev/null || true)"
if [ "$(git -C "$T" rev-parse HEAD 2>/dev/null)" != "$commit" ] || [ -z "$commit" ]; then
    bad "/opt/trinity is not at the baked commit '${commit}'"
fi
gitlink="$(git -C "$T" ls-tree HEAD src/backend/enterprise 2>/dev/null | awk '$2 == "commit" { print $3 }')"
if [ -z "$pin" ] || [ "$gitlink" != "$pin" ]; then
    bad "enterprise pin '${pin}' is not the commit's pin '${gitlink}'"
fi
if [ ! -f "${ENT}/backend/__init__.py" ]; then
    bad "the enterprise module is missing"
elif [ "$(git -C "$ENT" rev-parse HEAD 2>/dev/null)" != "$gitlink" ]; then
    bad "the enterprise module is not at the pin ${gitlink}"
fi
if [ "$tag" != "sha-${commit:0:7}" ]; then
    bad "baked image tag '${tag}' does not match the commit"
fi
for img in backend frontend scheduler mcp-server agent-base; do
    docker image inspect "ghcr.io/abilityai/trinity-${img}:${tag}" >/dev/null 2>&1 \
        || bad "image missing: ghcr.io/abilityai/trinity-${img}:${tag}"
done

if [ "$FAILED" != 0 ]; then
    echo "[assert-clean] the snapshot is not clean; the build fails." >&2
    exit 1
fi
echo "[assert-clean] clean: ${commit} (${tag}), enterprise ${pin}"
