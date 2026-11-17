#!/bin/bash
# Checks the DigitalOcean and AWS boot checks share. The caller defines say, fail and
# rsh "<command>" (runs one shell command on the host as root, with stdin closed), and
# sets BASE, TAG, AGENT, PASSWORD, TIMEOUT, POLL and DEADLINE before calling.

wait_for() {
    local what="$1"; shift
    until "$@"; do
        [ "$SECONDS" -lt "$DEADLINE" ] || fail "timed out after ${TIMEOUT}s waiting for ${what}"
        sleep "$POLL"
    done
}

backend_up() {
    rsh 'test -e /etc/trinity/firstboot-failed' && fail "first boot wrote its failure marker"
    rsh 'curl -fsS -m 3 http://localhost:8000/health' >/dev/null 2>&1
}

cloud_init_done() {
    case "$(rsh 'cloud-init status' 2>/dev/null)" in
        *error*) fail "cloud-init reported an error" ;;
        *done*)  return 0 ;;
    esac
    return 1
}

wait_for_first_boot() {
    wait_for "cloud-init" cloud_init_done
    rsh 'test ! -e /etc/trinity/firstboot-failed' || fail "first boot wrote its failure marker"
}

# usage: check_install <install source> — setup state, database, provenance, images.
# No -k anywhere: every off-box call validates the certificate.
check_install() {
    local source="$1" off_box service image base_local base_pinned
    off_box="$(curl -fsS --max-time 15 "${BASE}/api/setup/status")" || fail "the API does not answer over HTTPS"
    [ "$(jq -r .setup_completed <<<"$off_box")" = "true" ] || fail "HTTPS and localhost disagree on setup state"

    # A file here makes the hosted compose keep SQLite on a customer's machine.
    rsh 'timeout 30 docker exec trinity-backend test ! -e /data/trinity.db' || fail "/data/trinity.db exists"
    rsh "timeout 30 docker exec trinity-backend printenv DATABASE_URL | grep -q '^postgresql'" || fail "DATABASE_URL is not postgresql"
    rsh "grep -qx 'TRINITY_INSTALL_SOURCE=${source}' /opt/trinity/.env" || fail "install source marker missing"

    for service in backend frontend scheduler mcp-server; do
        image="$(rsh "timeout 30 docker inspect -f '{{.Config.Image}}' trinity-${service}")" || fail "trinity-${service} has no container"
        [ "$image" = "ghcr.io/abilityai/trinity-${service}:${TAG}" ] || fail "trinity-${service} runs ${image}"
    done
    base_local="$(rsh "timeout 30 docker image inspect -f '{{.Id}}' trinity-agent-base:latest")" || fail "trinity-agent-base:latest missing"
    base_pinned="$(rsh "timeout 30 docker image inspect -f '{{.Id}}' ghcr.io/abilityai/trinity-agent-base:${TAG}")" || fail "pinned agent base missing"
    [ -n "$base_local" ] && [ "$base_local" = "$base_pinned" ] || fail "trinity-agent-base:latest is not the ${TAG} image"
    say "images, database and provenance match ${TAG}"
}

api() { printf 'header = "Authorization: Bearer %s"\n' "$JWT" | curl -fsS -K - --max-time 30 "$@"; }
agent_running() { [ "$(api "${BASE}/api/agents/${AGENT}" | jq -r .status 2>/dev/null)" = "running" ]; }

# End to end as a customer: sign in, create an agent from the baked base image.
sign_in_and_create_agent() {
    JWT="$(printf 'username=admin&password=%s' "$PASSWORD" \
        | curl -fsS --max-time 15 --data-binary @- -H 'Content-Type: application/x-www-form-urlencoded' "${BASE}/api/token" \
        | jq -r '.access_token // empty')"
    # The token goes into a curl config in api, so only JWT characters are accepted.
    [[ "$JWT" =~ ^[A-Za-z0-9._-]+$ ]] || fail "sign-in with the new admin failed"
    api -H 'Content-Type: application/json' -d "{\"name\":\"${AGENT}\",\"template\":\"local:test-echo\"}" \
        "${BASE}/api/agents" >/dev/null || fail "creating an agent failed"
    wait_for "the agent to run" agent_running
    say "signed in and created a running agent"
}

# Run this late, so a service that crashes during setup is caught. Nothing else
# exercises the scheduler or the MCP server.
check_services() {
    local service state
    for service in backend frontend scheduler mcp-server; do
        state="$(rsh "timeout 30 docker inspect -f '{{.State.Status}} {{.RestartCount}}' trinity-${service}")"
        [ "$state" = "running 0" ] || fail "trinity-${service} is ${state:-unreadable}"
    done
    say "services are running with no restarts"
}
