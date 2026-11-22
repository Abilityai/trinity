#!/bin/bash
# Boot a throwaway droplet from a self-serve snapshot and prove a fresh instance
# from it is unclaimed and built from the right commit (trinity-enterprise#1347).
#
#   DIGITALOCEAN_TOKEN=dop_v1_... TRINITY_SSH_KEY_ID=<id> TRINITY_SSH_KEY_OP=<op://...> \
#     bash selfserve-boot-check.sh <snapshot-id> <commit>
#
# User-data carries what an instance creator sends: a public name, plus a
# planted setup claim and admin password, which first boot must discard.
# There is no DNS record for the name, so the name's certificate is not checked
# here; the IP's certificate is, and every off-box call validates it.
#
# Checks: first boot and cloud-init finish; /api/setup/status over HTTPS answers
# setup_completed=false with claim_required=instance-id; the planted claim and
# password are gone; .env has the claim-mode keys, the public name, provenance
# `script` and generated secrets; machine id and host keys are present on the
# instance; images are sha-<short7>; the backend uses PostgreSQL; the
# enterprise module is mounted and registered; no service restarted.
#
# The droplet is destroyed on every exit a shell can trap, and powers itself
# off after 45 minutes when it cannot be. A powered-off droplet still bills.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

SNAPSHOT="${1:-}"
COMMIT="${2:-}"
if ! [[ "$SNAPSHOT" =~ ^[0-9]+$ && "$COMMIT" =~ ^[0-9a-f]{7,40}$ ]]; then
    echo "usage: selfserve-boot-check.sh <snapshot-id> <commit>" >&2
    exit 2
fi
. "$HERE/mp-lib.sh"
. "$HERE/boot-lib.sh"

TIMEOUT="${TRINITY_BOOT_CHECK_TIMEOUT:-900}"
POLL="${TRINITY_BOOT_CHECK_POLL:-5}"
SIZE="${TRINITY_BOOT_CHECK_SIZE:-s-4vcpu-8gb}"
TAG="sha-${COMMIT:0:7}"
PUBLIC_NAME="boot-check-$(date +%s).example.invalid"
PLANTED_CLAIM="email-bound:$(openssl rand -hex 32)"

say()  { echo "[selfserve-boot-check] $*"; }
fail() { echo "[selfserve-boot-check] FAIL: $*" >&2; exit 1; }

: "${DIGITALOCEAN_TOKEN:?DIGITALOCEAN_TOKEN must be set}"
: "${TRINITY_SSH_KEY_ID:?TRINITY_SSH_KEY_ID must be set: an SSH key ID on the DigitalOcean team}"
export DIGITALOCEAN_ACCESS_TOKEN="$DIGITALOCEAN_TOKEN"

info="$(doctl compute snapshot get "$SNAPSHOT" --format Name,Regions --no-header)" \
    || fail "snapshot ${SNAPSHOT} not found on this token's team"
name="${info%% *}"
region="$(printf '%s' "${info#* }" | tr -d '[] ' | cut -d, -f1)"
[[ "$name" =~ ^trinity-selfserve-${COMMIT:0:7}-[0-9]{8}-[0-9]{4}$ ]] \
    || fail "snapshot ${SNAPSHOT} is ${name}, which was not built for ${COMMIT:0:7}"
[ -n "$region" ] || fail "could not read the region of snapshot ${SNAPSHOT}"

WORK="$(mktemp -d)"
RUN_TAG="trinity-boot-check-$(date +%s)-$$"
IP=""

remote() { ssh "${SSH_OPTS[@]}" "root@${IP}" "$@"; }
rsh() { remote -n "$1"; }

cleanup() {
    local rc=$?
    trap - EXIT
    trap '' INT TERM HUP
    if [ "$rc" != 0 ] && [ -n "$IP" ]; then
        say "first boot log and containers:"
        remote -n 'tail -n 40 /var/log/trinity-firstboot.log; timeout 30 docker ps -a --format "{{.Names}} {{.Status}}"' 2>&1 | sed 's/^/  /'
    fi
    reap_tag "$RUN_TAG" || rc=1
    drop_ssh_key
    rm -rf "$WORK"
    exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

use_ssh_key || exit 1
SSH_OPTS=(-i "$TRINITY_SSH_KEY_FILE" -o BatchMode=yes -o IdentitiesOnly=yes -o ConnectTimeout=10
          -o ServerAliveInterval=15 -o ServerAliveCountMax=3
          -o StrictHostKeyChecking=accept-new -o "UserKnownHostsFile=${WORK}/known_hosts")

# write_files runs before the per-instance scripts, so first boot sees all three.
USER_DATA="#cloud-config
write_files:
  - path: /etc/trinity/public-name
    content: \"${PUBLIC_NAME}\\n\"
  - path: /etc/trinity/admin-password
    content: \"planted-password-must-be-ignored\\n\"
  - path: /opt/trinity/trinity-data/setup-claim
    content: \"${PLANTED_CLAIM}\\n\"
runcmd:
  - shutdown -P +45
"

say "creating ${SIZE} in ${region} from ${name}"
doctl compute droplet create "$RUN_TAG" \
    --image "$SNAPSHOT" --size "$SIZE" --region "$region" \
    --ssh-keys "$TRINITY_SSH_KEY_ID" --tag-names "$RUN_TAG" \
    --user-data "$USER_DATA" \
    --format ID --no-header >/dev/null || fail "droplet create failed"

DEADLINE=$((SECONDS + TIMEOUT))
read_ip() {
    IP="$(doctl compute droplet list --tag-name "$RUN_TAG" --format PublicIPv4 --no-header 2>/dev/null)"
    [ -n "$IP" ]
}
wait_for "a public IP" read_ip
say "droplet at ${IP}"

wait_for "the backend" backend_up
wait_for_first_boot
module_mounted() { rsh 'timeout 30 docker exec trinity-backend test -f /app/enterprise/backend/__init__.py'; }
wait_for "the enterprise module" module_mounted
wait_for "the backend after the enterprise mount" backend_up
[ "$(rsh 'cat /etc/trinity/tls-status')" = "ok" ] || fail "first boot reports no valid certificate for the IP"

# --- unclaimed, in claim mode, over HTTPS ------------------------------------------
BASE="https://${IP}"
status="$(curl -fsS --max-time 15 "${BASE}/api/setup/status")" || fail "${BASE}/api/setup/status did not answer with a trusted certificate"
[ "$(jq -r .setup_completed <<<"$status")" = "false" ] || fail "setup is already completed: ${status}"
[ "$(jq -r .claim_required <<<"$status")" = "instance-id" ] || fail "setup is not in claim mode: ${status}"
say "/api/setup/status: setup not completed, claim required"

# --- user-data could not supply a claim or a password ------------------------------
rsh 'ls /opt/trinity/trinity-data/setup-claim* /etc/trinity/admin-password 2>/dev/null | grep -q .' \
    && fail "a claim or password planted through user-data survived first boot"
# A password that passes validation, so only the claim decides the answer.
pw="Aa1-$(openssl rand -hex 16)"
code="$(printf '{"password":"%s","confirm_password":"%s","email":"boot-check@example.com","claim_code":"%s"}' \
        "$pw" "$pw" "${PLANTED_CLAIM#email-bound:}" \
    | curl -sS --max-time 15 -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' --data-binary @- \
        "${BASE}/api/setup/admin-password")"
[ "$code" = "403" ] || fail "setup answered ${code} to the planted claim"
status="$(curl -fsS --max-time 15 "${BASE}/api/setup/status")"
[ "$(jq -r .setup_completed <<<"$status")" = "false" ] || fail "the planted claim created an admin"
say "the claim and password planted in user-data were discarded"

# --- .env --------------------------------------------------------------------------
envv() { rsh "grep -E '^$1=' /opt/trinity/.env | tail -1 | cut -d= -f2-"; }
[ "$(envv ADMIN_PASSWORD_SOURCE)" = "instance-id" ] || fail "ADMIN_PASSWORD_SOURCE is not instance-id"
[ -z "$(envv ADMIN_PASSWORD)" ] || fail "an admin password is set"
[ "$(envv PUBLIC_CHAT_URL)" = "https://${PUBLIC_NAME}" ] || fail "PUBLIC_CHAT_URL is not the public name"
[ "$(envv TRINITY_INSTALL_SOURCE)" = "script" ] || fail "install source is not script"
for key in SECRET_KEY CREDENTIAL_ENCRYPTION_KEY INTERNAL_API_SECRET POSTGRES_PASSWORD REDIS_PASSWORD; do
    [ -n "$(envv "$key")" ] || fail "${key} was not generated"
done
rsh 'test -s /etc/machine-id && ls /etc/ssh/ssh_host_*_key >/dev/null' || fail "machine id or SSH host keys were not created"
say ".env is in claim mode for ${PUBLIC_NAME}, provenance script, secrets generated"

# --- what runs ---------------------------------------------------------------------
for service in backend frontend scheduler mcp-server; do
    image="$(rsh "timeout 30 docker inspect -f '{{.Config.Image}}' trinity-${service}")" || fail "trinity-${service} has no container"
    [ "$image" = "ghcr.io/abilityai/trinity-${service}:${TAG}" ] || fail "trinity-${service} runs ${image}"
done
rsh 'timeout 30 docker exec trinity-backend test ! -e /data/trinity.db' || fail "/data/trinity.db exists"
rsh "timeout 30 docker exec trinity-backend printenv DATABASE_URL | grep -q '^postgresql'" || fail "DATABASE_URL is not postgresql"
rsh 'timeout 30 docker logs trinity-backend 2>&1 | grep -q "Trinity Enterprise modules registered"' \
    || fail "the backend did not register the enterprise module"
[ "$(rsh 'cat /etc/trinity/baked-commit')" = "$(rsh 'git -C /opt/trinity rev-parse HEAD')" ] || fail "the checkout is not the baked commit"
[[ "$(rsh 'cat /etc/trinity/baked-commit')" == "${COMMIT}"* ]] || fail "the snapshot was baked from another commit"
say "images ${TAG}, PostgreSQL, enterprise module registered"

check_services
say "PASS: snapshot ${SNAPSHOT} (${TAG})"
