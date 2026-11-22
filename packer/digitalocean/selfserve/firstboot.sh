#!/bin/bash
# First boot of an instance made from the self-serve snapshot
# (trinity-enterprise#1347). Installed by 80-finish.sh at the path the
# per-instance cloud-init hook calls, /opt/trinity-firstboot/firstboot.sh, in
# place of the Marketplace first boot. The Marketplace image is unaffected.
#
# Everything was installed at build time. What is left here is only what is
# unique to this instance:
#
#   1. its public name, from user-data (/etc/trinity/public-name);
#   2. setup by claim: a blank admin password and ADMIN_PASSWORD_SOURCE=instance-id
#      in .env BEFORE the first start, with no claim file. The claim is written
#      after boot over SSH by whoever created the instance. Until then /setup
#      refuses everyone;
#   3. start.sh's site phase and install, which generates this instance's own
#      secrets, as provenance `script`;
#   4. the enterprise module, mounted into the backend when the snapshot has it.
#
# User-data supplies the public name as #cloud-config, NOT as a shell script:
# per-instance scripts run before user scripts, so a script would run after
# this one had already started Trinity.
#
#   #cloud-config
#   write_files:
#     - path: /etc/trinity/public-name
#       content: "instance-name.example.com\n"
#
# User-data is readable by every process on the droplet through the metadata
# service, so nothing here takes a claim or an admin password from it. Any such
# file present before the first start is deleted.
#
# The certificate for the public name is Caddy's on-demand TLS. The backend
# authorises exactly the host in PUBLIC_CHAT_URL (/api/public/tls-allowed), so
# no DNS credential is ever on the box. The DNS record and the first request
# that triggers issuance come from whoever created the instance.
set -euo pipefail
export PATH="/usr/local/sbin:/usr/sbin:/sbin:${PATH}"

# Overridable for the unit tests only.
STATE_DIR="${TRINITY_STATE_DIR:-/etc/trinity}"
TRINITY_DIR="${TRINITY_DIR:-/opt/trinity}"
LOG="${TRINITY_FIRSTBOOT_LOG:-/var/log/trinity-firstboot.log}"
NAME_FILE="${STATE_DIR}/public-name"

touch "$LOG"
chmod 0600 "$LOG"
exec > >(tee -a "$LOG") 2>&1
echo "=== Trinity self-serve first boot: $(date -u +%FT%TZ) ==="

die() { echo "FATAL: $*" >&2; exit 1; }

# KEY=VALUE in .env, replacing any existing line for KEY. Written through the
# same inode so the file keeps its 0600 mode.
set_key() {
    local key="$1" value="$2" tmp
    tmp="$(mktemp "${TRINITY_DIR}/.env.XXXXXX")"
    grep -v "^${key}=" .env > "$tmp" || true
    printf '%s=%s\n' "$key" "$value" >> "$tmp"
    cat "$tmp" > .env
    rm -f "$tmp"
}
env_value() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- || true; }

# --- 1. Public name ----------------------------------------------------------
# 2 to 6 labels: the provisioned Caddyfile's on-demand site lists wildcards of
# exactly that depth, so any other name would never get a certificate.
NAME=""
if [ -f "$NAME_FILE" ]; then
    NAME="$(head -c 256 "$NAME_FILE" | tr -d '\r\n' | tr '[:upper:]' '[:lower:]')"
    NAME="${NAME%.}"
fi
LABEL='[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?'
if [ -z "$NAME" ]; then
    die "no public name. Supply it in user-data as #cloud-config write_files to ${NAME_FILE}."
fi
if ! [[ "$NAME" =~ ^(${LABEL}\.){1,5}${LABEL}$ ]] || [[ "$NAME" =~ ^[0-9.]+$ ]] || [ "${#NAME}" -gt 253 ]; then
    die "the public name '${NAME}' is not a hostname of 2 to 6 labels."
fi
echo "→ Public name: ${NAME}"

mkdir -p "$STATE_DIR"
chmod 0700 "$STATE_DIR"
rm -f "${STATE_DIR}/admin-password"

# --- 2. .env before the first start -----------------------------------------
cd "$TRINITY_DIR"
umask 077
[ -f .env ] || cp .env.example .env
chmod 0600 .env

DATA_PATH="$(env_value TRINITY_DATA_PATH)"
DATA_PATH="${DATA_PATH:-./trinity-data}"
# The snapshot ships no claim (95-assert-clean.sh), so one here came from
# user-data. A claim that every process on the box could read is no claim.
for f in "${DATA_PATH}"/setup-claim "${DATA_PATH}"/setup-claim.*; do
    if [ -e "$f" ] || [ -L "$f" ]; then
        rm -f -- "$f"
        echo "→ Removed ${f}: a claim is never taken from user-data."
    fi
done

set_key ADMIN_PASSWORD ""
set_key ADMIN_PASSWORD_SOURCE instance-id
set_key PUBLIC_CHAT_URL "https://${NAME}"
set_key MCP_EXTERNAL_ACCESS true
TRINITY_IMAGE_TAG="$(cat "${STATE_DIR}/baked-image-tag" 2>/dev/null || true)"
[ -n "$TRINITY_IMAGE_TAG" ] || die "${STATE_DIR}/baked-image-tag is missing: this is not a self-serve snapshot."
set_key TRINITY_IMAGE_TAG "$TRINITY_IMAGE_TAG"
export TRINITY_IMAGE_TAG
unset ADMIN_PASSWORD ADMIN_PASSWORD_SOURCE

# --- 3. Firewall, site phase and install ------------------------------------
if ! "${TRINITY_DIR}/scripts/deploy/docker-firewall.sh"; then
    echo "WARNING: could not apply the DOCKER-USER rules. Re-run: ${TRINITY_DIR}/scripts/deploy/docker-firewall.sh" >&2
fi

./scripts/deploy/start.sh \
    --provision --cloud digitalocean --site-only --provenance script \
    --hosted --unattended

# --- 4. Enterprise module ----------------------------------------------------
# start.sh --hosted runs the base file set only. The module is a read-only
# bind mount (docker-compose.prod.enterprise.yml), so the backend is recreated
# with it, from the image it already has.
if [ -f src/backend/enterprise/backend/__init__.py ]; then
    echo "→ Mounting the enterprise module"
    docker compose -f docker-compose.hosted.yml -f docker-compose.prod.enterprise.yml \
        up -d --no-deps --pull never backend
    mounted=0
    for _ in $(seq 1 36); do
        if docker exec trinity-backend test -f /app/enterprise/backend/__init__.py; then
            mounted=1
            break
        fi
        sleep 5
    done
    [ "$mounted" = 1 ] || die "the backend did not come back with /app/enterprise mounted."
fi

echo "=== Trinity self-serve first boot complete ==="
