#!/bin/bash
# Boot a throwaway droplet from a snapshot and test it the way a customer uses it.
#
#   DIGITALOCEAN_TOKEN=dop_v1_... TRINITY_SSH_KEY_ID=<id> TRINITY_SSH_KEY_FILE=<path> \
#     bash boot-check.sh <snapshot-id> <tag>
#
# A pass writes ~/.trinity-marketplace/<snapshot-id>.pass, which mp-submit.sh
# requires. Any failure, and any check that cannot run, exits non-zero. The droplet
# is destroyed on every exit a shell can trap, and powers itself off after 45
# minutes when it cannot be (a closed laptop). A powered-off droplet still bills.
# Every run first removes the snapshot's earlier pass, so the newest result decides.
# Chat with an agent is not tested, because it needs a model credential.
set -uo pipefail
. "$(dirname "$0")/mp-lib.sh"

TIMEOUT="${TRINITY_BOOT_CHECK_TIMEOUT:-900}"
POLL="${TRINITY_BOOT_CHECK_POLL:-5}"
SIZE="s-4vcpu-8gb"   # the listing's recommended size; setup seeds a starter fleet
AGENT="boot-check-agent"

say()  { echo "[boot-check] $*"; }
fail() { echo "[boot-check] FAIL: $*" >&2; exit 1; }

SNAPSHOT="${1:-}"
TAG="${2:-}"
if ! [[ "$SNAPSHOT" =~ ^[0-9]+$ && "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[A-Za-z0-9.-]+)?$ ]]; then
    echo "usage: boot-check.sh <snapshot-id> <tag>" >&2
    exit 2
fi
: "${DIGITALOCEAN_TOKEN:?DIGITALOCEAN_TOKEN must be set}"
: "${TRINITY_SSH_KEY_ID:?TRINITY_SSH_KEY_ID must be set: an SSH key ID on the DigitalOcean team}"
: "${TRINITY_SSH_KEY_FILE:?TRINITY_SSH_KEY_FILE must be set: the matching private key}"
export DIGITALOCEAN_ACCESS_TOKEN="$DIGITALOCEAN_TOKEN"

RECORD="${HOME}/.trinity-marketplace/${SNAPSHOT}.pass"
rm -f "$RECORD"

info="$(doctl compute snapshot get "$SNAPSHOT" --format Name,Regions --no-header)" \
    || fail "snapshot ${SNAPSHOT} not found on this token's team"
name="${info%% *}"
region="$(printf '%s' "${info#* }" | tr -d '[] ' | cut -d, -f1)"
name_re="^trinity-${TAG//./-}-[0-9]{8}-[0-9]{4}\$"
[[ "$name" =~ $name_re ]] || fail "snapshot ${SNAPSHOT} is ${name}, which was not built for ${TAG}"
[ -n "$region" ] || fail "could not read the region of snapshot ${SNAPSHOT}"

WORK="$(mktemp -d)"
RUN_TAG="trinity-boot-check-$(date +%s)-$$"
IP=""
SSH_OPTS=(-i "$TRINITY_SSH_KEY_FILE" -o BatchMode=yes -o IdentitiesOnly=yes -o ConnectTimeout=10
          -o ServerAliveInterval=15 -o ServerAliveCountMax=3
          -o StrictHostKeyChecking=accept-new -o "UserKnownHostsFile=${WORK}/known_hosts")

remote() { ssh "${SSH_OPTS[@]}" "root@${IP}" "$@"; }

cleanup() {
    local rc=$?
    # Signals are ignored from here: nothing else deletes a billing droplet.
    trap - EXIT
    trap '' INT TERM HUP
    if [ "$rc" != 0 ] && [ -n "$IP" ]; then
        say "first boot log and containers:"
        remote -n 'tail -n 40 /var/log/trinity-firstboot.log; timeout 30 docker ps -a --format "{{.Names}} {{.Status}}"' 2>&1 | sed 's/^/  /'
    fi
    reap_tag "$RUN_TAG" || rc=1
    rm -rf "$WORK"
    exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

# runcmd runs after first boot, so a first boot that hangs never arms this.
# Do not move it to bootcmd: systemd-run there wedged boot on stock Ubuntu 24.04,
# with and without --no-block, and SSH never came up.
DEAD_MAN=$'#cloud-config\nruncmd:\n  - shutdown -P +45\n'

say "creating ${SIZE} in ${region} from ${name}"
doctl compute droplet create "$RUN_TAG" \
    --image "$SNAPSHOT" --size "$SIZE" --region "$region" \
    --ssh-keys "$TRINITY_SSH_KEY_ID" --tag-names "$RUN_TAG" \
    --user-data "$DEAD_MAN" \
    --format ID --no-header >/dev/null || fail "droplet create failed"

DEADLINE=$((SECONDS + TIMEOUT))
wait_for() {
    local what="$1"; shift
    until "$@"; do
        [ "$SECONDS" -lt "$DEADLINE" ] || fail "timed out after ${TIMEOUT}s waiting for ${what}"
        sleep "$POLL"
    done
}

read_ip() {
    IP="$(doctl compute droplet list --tag-name "$RUN_TAG" --format PublicIPv4 --no-header 2>/dev/null)"
    [ -n "$IP" ]
}
wait_for "a public IP" read_ip
say "droplet at ${IP}"

backend_up() {
    remote -n test -e /etc/trinity/firstboot-failed && fail "first boot wrote its failure marker"
    remote -n curl -fsS -m 3 http://localhost:8000/health >/dev/null 2>&1
}
wait_for "the backend" backend_up

# /setup is public until an admin exists, so it is claimed before anything else.
status="$(remote -n curl -fsS -m 5 http://localhost:8000/api/setup/status)" || fail "setup status unreadable"
[ "$(jq -r .setup_completed <<<"$status")" = "false" ] || fail "the snapshot already has an admin"
PASSWORD="Aa1-$(openssl rand -hex 16)"
code="$(printf '{"password":"%s","confirm_password":"%s","email":"boot-check@example.com"}' "$PASSWORD" "$PASSWORD" \
    | remote "curl -sS -m 30 -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' --data-binary @- http://localhost:8000/api/setup/admin-password")"
[ "$code" = "200" ] || fail "claiming /setup returned ${code}. A 403 means someone else claimed it."
say "claimed /setup"

cloud_init_done() {
    case "$(remote -n cloud-init status 2>/dev/null)" in
        *error*) fail "cloud-init reported an error" ;;
        *done*)  return 0 ;;
    esac
    return 1
}
wait_for "cloud-init" cloud_init_done

remote -n test ! -e /etc/trinity/firstboot-failed || fail "first boot wrote its failure marker"
[ "$(remote -n cat /etc/trinity/tls-status)" = "ok" ] || fail "first boot reports no valid certificate"

# No -k anywhere: every off-box call below validates the certificate.
BASE="https://${IP}"
page="$(curl -fsS -i --max-time 15 "${BASE}/")" || fail "${BASE}/ did not answer with a trusted certificate"
grep -qiE '^x-do-marketplace: *trinity' <<<"$page" || fail "${BASE}/ lacks the X-DO-MARKETPLACE header"
grep -qiE '<title>[^<]*trinity' <<<"$page" || fail "${BASE}/ does not serve Trinity"
say "HTTPS serves Trinity with a trusted certificate"

off_box="$(curl -fsS --max-time 15 "${BASE}/api/setup/status")" || fail "the API does not answer over HTTPS"
[ "$(jq -r .setup_completed <<<"$off_box")" = "true" ] || fail "HTTPS and localhost disagree on setup state"

# A file here makes the hosted compose keep SQLite on a customer's droplet.
remote -n timeout 30 docker exec trinity-backend test ! -e /data/trinity.db || fail "/data/trinity.db exists"
remote -n "timeout 30 docker exec trinity-backend printenv DATABASE_URL | grep -q '^postgresql'" || fail "DATABASE_URL is not postgresql"
remote -n "grep -qx 'TRINITY_INSTALL_SOURCE=do-marketplace' /opt/trinity/.env" || fail "install source marker missing"

for service in backend frontend scheduler mcp-server; do
    image="$(remote -n "timeout 30 docker inspect -f '{{.Config.Image}}' trinity-${service}")" || fail "trinity-${service} has no container"
    [ "$image" = "ghcr.io/abilityai/trinity-${service}:${TAG}" ] || fail "trinity-${service} runs ${image}"
done
base_local="$(remote -n timeout 30 docker image inspect -f '{{.Id}}' trinity-agent-base:latest)" || fail "trinity-agent-base:latest missing"
base_pinned="$(remote -n timeout 30 docker image inspect -f '{{.Id}}' "ghcr.io/abilityai/trinity-agent-base:${TAG}")" || fail "pinned agent base missing"
[ -n "$base_local" ] && [ "$base_local" = "$base_pinned" ] || fail "trinity-agent-base:latest is not the ${TAG} image"
say "images, database and provenance match ${TAG}"

# End to end as a customer: sign in, create an agent from the baked base image.
JWT="$(printf 'username=admin&password=%s' "$PASSWORD" \
    | curl -fsS --max-time 15 --data-binary @- -H 'Content-Type: application/x-www-form-urlencoded' "${BASE}/api/token" \
    | jq -r '.access_token // empty')"
# The token goes into a curl config below, so only JWT characters are accepted.
[[ "$JWT" =~ ^[A-Za-z0-9._-]+$ ]] || fail "sign-in with the new admin failed"
api() { printf 'header = "Authorization: Bearer %s"\n' "$JWT" | curl -fsS -K - --max-time 30 "$@"; }

api -H 'Content-Type: application/json' -d "{\"name\":\"${AGENT}\",\"template\":\"local:test-echo\"}" \
    "${BASE}/api/agents" >/dev/null || fail "creating an agent failed"
agent_running() { [ "$(api "${BASE}/api/agents/${AGENT}" | jq -r .status 2>/dev/null)" = "running" ]; }
wait_for "the agent to run" agent_running
say "signed in and created a running agent"

# Checked this late so a service that crashes during setup is caught. Nothing else
# exercises the scheduler or the MCP server.
for service in backend frontend scheduler mcp-server; do
    state="$(remote -n "timeout 30 docker inspect -f '{{.State.Status}} {{.RestartCount}}' trinity-${service}")"
    [ "$state" = "running 0" ] || fail "trinity-${service} is ${state:-unreadable}"
done
say "services are running with no restarts"

# Runs after the agent exists, so its published SSH port is probed too.
# 8081 is listed because the SPA's port decides whether TLS holds.
remote -n iptables -C DOCKER-USER -j TRINITY-FW || fail "the DOCKER-USER firewall rule is missing"
ports="$(remote -n "timeout 30 docker ps --format '{{.Ports}}'" | grep -oE '0\.0\.0\.0:[0-9]+(-[0-9]+)?' | cut -d: -f2 \
    | awk -F- '{ last = ($2 == "") ? $1 : $2; for (p = $1; p <= last; p++) print p }' | sort -un)"
[ -n "$ports" ] || fail "read no published ports from docker ps"
# The probes run from a second droplet on the same tag: a network that answers TCP
# itself makes every port look open from the laptop. 19999 has no listener, so it
# must read closed or this vantage proves nothing.
say "creating the off-box prober"
PROBER_IP="$(doctl compute droplet create "${RUN_TAG}-probe" \
    --image ubuntu-24-04-x64 --size s-1vcpu-1gb --region "$region" \
    --ssh-keys "$TRINITY_SSH_KEY_ID" --tag-names "$RUN_TAG" \
    --user-data "$DEAD_MAN" \
    --wait --format PublicIPv4 --no-header)" && [ -n "$PROBER_IP" ] || fail "prober create failed"
prober() { ssh "${SSH_OPTS[@]}" "root@${PROBER_IP}" "$@"; }
prober_up() { prober -n command -v curl >/dev/null 2>&1; }
wait_for "the prober" prober_up
# Prints "<time_connect> <curl exit code>".
probe() {
    prober -n "c=\$(curl -sS -o /dev/null -w '%{time_connect}' --connect-timeout 5 --max-time 8 http://${IP}:$1 2>/dev/null); echo \"\$c \$?\""
}
is_closed() {
    local connected rc
    read -r connected rc <<<"$1"
    [ -n "$rc" ] || return 1
    # A timeout is a closed port only when no connection ever opened.
    [ "$rc" = 7 ] || { [ "$rc" = 28 ] && [ -z "${connected//[0.]/}" ]; }
}
prober -n curl -fsS -o /dev/null --max-time 15 "${BASE}/" || fail "443 does not answer the prober, so the port probe proves nothing"
is_closed "$(probe 19999)" || fail "the prober reaches a port with no listener, so the port probe proves nothing"
for port in $(printf '%s\n' $ports 8081 | sort -un); do
    is_closed "$(probe "$port")" || fail "port ${port} is reachable from off-box"
done
say "container ports are closed from off-box"

# ponytail: local file, so a submit must run on the machine that tested. A shared
# record (a DigitalOcean tag on the image) is the upgrade when CI builds (ent#648).
mkdir -p -m 700 "${HOME}/.trinity-marketplace"
caddy="$(remote -n caddy version | awk '{print $1}' | sed 's/^v//')"
[[ "$caddy" =~ ^[0-9]+\.[0-9]+ ]] || fail "could not read the Caddy version: ${caddy}"
printf 'tag=%s\ncaddy=%s\n' "$TAG" "$caddy" > "$RECORD"
say "PASS: snapshot ${SNAPSHOT} (${TAG})"
