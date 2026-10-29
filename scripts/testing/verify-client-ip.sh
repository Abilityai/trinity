#!/bin/bash
# Prove trinity-enterprise#873 (client address behind the bundled proxies) on
# REAL Linux networking: a disposable docker:dind daemon, the production compose
# file, the production images built from this checkout, and Caddy 2.11.4.
#
#   bash scripts/testing/verify-client-ip.sh [--project NAME] [--keep] [--outer-subnet CIDR]
#
# Why a second daemon: Docker Desktop's port publishing is not Linux's
# docker-proxy/DNAT, so a Mac cannot show what a provisioned Linux host does to
# a client address. Inside dind it is the real thing: iptables, bridges,
# docker-proxy for loopback publication, and an outer network from which two
# client containers reach the dind host's published ports with their own
# addresses.
#
# Everything this script creates carries the label below and is removed on exit
# (unless --keep): the dind container (and with it every inner image, volume
# and network), the outer client/operator-proxy/Caddy containers and the outer
# network. It never touches any other container, network or daemon state, and
# the dind daemon's API is a unix socket inside its own container only.
#
# Exit status: 0 only when every check passed. Each check prints PASS/FAIL and
# the evidence it read (limiter keys, audit rows, access-log lines).
set -uo pipefail

PROJECT="trinity-ent873-test"
KEEP=0
OUTER_SUBNET="10.231.73.0/24"
while [ $# -gt 0 ]; do
    case "$1" in
        --project) PROJECT="$2"; shift ;;
        --keep) KEEP=1 ;;
        --outer-subnet) OUTER_SUBNET="$2"; shift ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
    shift
done

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
FIX="$ROOT/scripts/testing/client-ip"
LABEL="com.trinity.verify-client-ip=${PROJECT}"
# Unique to THIS run. Cleanup removes only what carries it, so a run that aborts
# on a name collision never touches a previous run's retained (--keep) resources.
RUN_LABEL="com.trinity.verify-client-ip.run=${PROJECT}-$(date +%s)-$$"
LABELS=(--label "$LABEL" --label "$RUN_LABEL")
NET="${PROJECT}-outer"
DIND="${PROJECT}-dind"
CADDY_IMAGE="caddy:2.11.4-alpine"
CURL_IMAGE="curlimages/curl:8.11.1"
LB_IMAGE="nginx:1.27-alpine"
O3="${OUTER_SUBNET%.*/*}"            # 10.231.73
DIND_IP="${O3}.10"
CLIENT_A="${O3}.21"
CLIENT_B="${O3}.22"
LB_IP="${O3}.30"
PREFIX="172.30.0"
ADMIN_PASSWORD="Ent873!$(openssl rand -hex 6)Aa"
REDIS_PASSWORD="$(openssl rand -hex 16)"
REDIS_BACKEND_PASSWORD="$(openssl rand -hex 16)"
FAILS=0
EVIDENCE="${ROOT}/.plan/issue-ent873-dind-evidence.txt"
mkdir -p "$(dirname "$EVIDENCE")"
: > "$EVIDENCE"

log() { printf '%s\n' "$*" | tee -a "$EVIDENCE"; }
pass() { log "PASS  $*"; }
fail() { log "FAIL  $*"; FAILS=$((FAILS + 1)); }
# A probe that could not run (docker, curl or nc failing for a reason other than
# the connection being refused or timing out) proves nothing either way.
error() { log "ERROR $*"; FAILS=$((FAILS + 1)); }
check() { local name="$1"; shift; if "$@"; then pass "$name"; else fail "$name"; fi; }
die() { log "ABORT $*"; exit 1; }
# RFC 6455 4.1: a fresh nonce per connection, 16 random bytes base64-encoded.
# The checks below read the status and access log, not Sec-WebSocket-Accept.
ws_nonce() { openssl rand -base64 16; }

cleanup() {
    [ "$KEEP" = "1" ] && { log "--keep: leaving ${PROJECT} resources up"; return; }
    local ids
    ids=$(docker ps -aq --filter "label=${RUN_LABEL}")
    [ -n "$ids" ] && docker rm -f -v $ids >/dev/null 2>&1
    # Only the network THIS run created, by ID: an early abort (before the
    # collision check or the create) must never remove a pre-existing network
    # that happens to share the name.
    [ -n "${NET_ID:-}" ] && docker network rm "$NET_ID" >/dev/null 2>&1
    log "teardown: removed $(echo "$ids" | grep -c . ) container(s) and network ${NET_ID:-<none created>}"
}
NET_ID=""

inner() { docker exec "$DIND" "$@"; }
ish() { docker exec "$DIND" sh -c "$1"; }
compose() { inner docker compose --project-directory /src -f /src/docker-compose.prod.yml -p "$PROJECT" "$@"; }

# TCP reachability, classified. curl's telnet:// with no input connects and
# leaves; %{time_connect} stays 0 unless a connection was ESTABLISHED. That
# separates "unreachable" from "reachable but refused the request", which a
# plain HTTP probe of a PROXY-protocol listener cannot. Prints one of
#   CONNECTED <time>        a TCP connection was established
#   REFUSED <curl rc>       curl 7 (refused) or 28 (timed out/dropped), nothing connected
#   ERROR <rc> <diagnostic> anything else: docker (125/126/127), DNS, a missing
#                           binary... — the probe did not run, so it proves nothing
classify_connect() {   # <rc> <time_connect> <stderr file>
    local rc="$1" t="$2" errf="$3"
    if [ -n "$t" ] && [ "$t" != "0.000000" ]; then
        echo "CONNECTED $t"
    elif [ "$t" = "0.000000" ] && { [ "$rc" = 7 ] || [ "$rc" = 28 ]; }; then
        echo "REFUSED $rc"
    else
        echo "ERROR $rc $(head -c 300 "$errf" | tr '\n' ' ')"
    fi
}
connect_from_net() {     # <inner network> <host> <port>
    local t rc errf; errf="$(mktemp)"
    t=$(inner docker run --rm --network "$1" "$CURL_IMAGE" -sS -o /dev/null -w '%{time_connect}' \
        --connect-timeout 3 --max-time 4 "telnet://$2:$3" </dev/null 2>"$errf"); rc=$?
    classify_connect "$rc" "$t" "$errf"; rm -f "$errf"
}
connect_from_client() {  # <client a|b> <host> <port>
    local t rc errf; errf="$(mktemp)"
    t=$(docker exec "${PROJECT}-client-$1" curl -sS -o /dev/null -w '%{time_connect}' \
        --connect-timeout 3 --max-time 4 "telnet://$2:$3" </dev/null 2>"$errf"); rc=$?
    classify_connect "$rc" "$t" "$errf"; rm -f "$errf"
}
# expect_unreachable <check name> <CONNECTED|REFUSED|ERROR ...>: only a genuine
# refusal or timeout passes.
expect_unreachable() {
    case "$2" in
        REFUSED*) pass "$1 ($2)" ;;
        CONNECTED*) fail "$1 ($2)" ;;
        *) error "$1 — probe failed: $2" ;;
    esac
}

# A FORGED PROXY v2 preamble (TCP4, claiming client 203.0.113.99 -> port 8082)
# followed by a sign-in, sent with busybox printf | nc from a container on
# <inner network> [at a static ip]. Prints one of
#   RESPONSE <status line>
#   NORESPONSE <why>        nc ran, connected or reported a refusal/timeout,
#                           and nothing answered
#   NOCONNECT <why>         busybox nc exited 1 SILENTLY. It does that for a
#                           refused connection AND for a timed-out one (observed:
#                           rc=1, empty stderr, in 0 s and in -w seconds
#                           respectively), so on its own it cannot be told apart
#                           from another failure; the caller corroborates it with
#                           the independent curl connect probe for the same target
#   ERROR <diagnostic>      the container or nc did not run as intended
FORGED_CLIENT="203.0.113.99"
_FORGED_SCRIPT='echo __ran
{ printf "\015\012\015\012\000\015\012\121\125\111\124\012\041\021\000\014\313\000\161\143\254\036\000\002\303\120\037\222"
  printf "POST /api/token HTTP/1.1\r\nHost: x\r\nContent-Type: application/x-www-form-urlencoded\r\nContent-Length: 34\r\nConnection: close\r\n\r\nusername=forged@example.com&password=bad1"
  sleep 3; } | nc -w 5 "$1" "$2" >/tmp/resp 2>/tmp/err
echo "__rc=$?"
echo "__resp=$(head -1 /tmp/resp | tr -d "\r")"
echo "__err=$(head -c 300 /tmp/err | tr "\n" " ")"'
forged_v2_from_net() {     # <inner network> <host> <port> [static ip]
    local ipopt=() out rc nrc resp err
    [ -n "${4:-}" ] && ipopt=(--ip "$4")
    out=$(inner docker run --rm --network "$1" ${ipopt[@]+"${ipopt[@]}"} --entrypoint sh "$CADDY_IMAGE" \
        -c "$_FORGED_SCRIPT" sh "$2" "$3" 2>&1); rc=$?
    case "$out" in
        __ran*) ;;
        *) echo "ERROR docker rc=${rc}: $(echo "$out" | head -c 300 | tr '\n' ' ')"; return ;;
    esac
    nrc="$(echo "$out" | sed -n 's/^__rc=//p')"
    resp="$(echo "$out" | sed -n 's/^__resp=//p')"
    err="$(echo "$out" | sed -n 's/^__err=//p')"
    if [ -n "$resp" ]; then
        echo "RESPONSE $resp"
    elif [ "$nrc" = 0 ] || echo "$err" | grep -qiE "refused|timed out|can't connect|unreachable"; then
        echo "NORESPONSE nc rc=${nrc} ${err}"
    elif [ "$nrc" = 1 ] && [ -z "$(echo "$err" | tr -d ' ')" ]; then
        echo "NOCONNECT nc rc=1, no message"
    else
        echo "ERROR nc rc=${nrc}: ${err}"
    fi
}
expect_no_response() {   # <check name> <forged result> [connect verdict for the same target]
    case "$2" in
        NORESPONSE*) pass "$1 ($2)" ;;
        NOCONNECT*)
            case "${3:-}" in
                REFUSED*) pass "$1 ($2; curl connect probe: $3)" ;;
                *) error "$1 — nc gave no connection but the connect probe says '${3:-none}': $2" ;;
            esac ;;
        RESPONSE*) fail "$1 ($2)" ;;
        *) error "$1 — probe failed: $2" ;;
    esac
}

# --------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------
log "== setup (${PROJECT})"
docker info >/dev/null 2>&1 || die "outer docker daemon not reachable"
for img in docker:dind "$CADDY_IMAGE" "$CURL_IMAGE" "$LB_IMAGE"; do
    docker image inspect "$img" >/dev/null 2>&1 || docker pull -q "$img" >/dev/null || die "cannot pull $img"
done
# The outer subnet must be free on this daemon.
if docker network ls -q | xargs docker network inspect -f '{{range .IPAM.Config}}{{.Subnet}} {{end}}' 2>/dev/null \
        | tr ' ' '\n' | grep -qx "$OUTER_SUBNET"; then
    die "outer subnet ${OUTER_SUBNET} already in use; pass --outer-subnet"
fi
docker network inspect "$NET" >/dev/null 2>&1 && die "a network named ${NET} already exists; pass another --project"
_existing="$(docker ps -a --format '{{.Names}}' | grep -E "^${PROJECT}-" || true)"
[ -z "$_existing" ] || die "containers named ${PROJECT}-* already exist ($(echo "$_existing" | tr '\n' ' ')); remove them or pass another --project"
# Installed only now: every collision check above aborts having created
# nothing, and must not run a cleanup.
trap cleanup EXIT
NET_ID="$(docker network create "${LABELS[@]}" --subnet "$OUTER_SUBNET" "$NET")" || die "outer network"
docker run -d --privileged "${LABELS[@]}" --name "$DIND" --network "$NET" --ip "$DIND_IP" \
    docker:dind dockerd --host=unix:///var/run/docker.sock >/dev/null || die "dind start"
for _ in $(seq 1 60); do inner docker info >/dev/null 2>&1 && break; sleep 1; done
inner docker info >/dev/null 2>&1 || die "inner daemon did not start"
inner apk add --no-cache bash curl >/dev/null 2>&1 || die "apk add in dind"
log "inner daemon: $(inner docker version -f '{{.Server.Version}}'), outer subnet ${OUTER_SUBNET}, dind at ${DIND_IP}"

# Source in (no .git, no node_modules, no build output).
(cd "$ROOT" && tar -cf - --exclude node_modules --exclude dist --exclude __pycache__ --exclude '.pytest_cache' \
    src/backend src/frontend src/mcp-server docker config VERSION docker-compose.prod.yml scripts/deploy scripts/testing) \
    | docker exec -i "$DIND" sh -c 'mkdir -p /src && tar -C /src -xf -' || die "copy source"
# The frontend this branch replaces, for the version-skew check.
(cd "$ROOT" && git archive HEAD src/frontend docker/frontend) \
    | docker exec -i "$DIND" sh -c 'mkdir -p /baseline && tar -C /baseline -xf -' || die "copy baseline"
for img in "$CADDY_IMAGE" "$CURL_IMAGE"; do
    docker save "$img" | docker exec -i "$DIND" docker load -q >/dev/null || die "load $img"
done

cat > /tmp/.ent873-env.$$ <<ENV
SECRET_KEY=$(openssl rand -hex 32)
ADMIN_PASSWORD=${ADMIN_PASSWORD}
REDIS_PASSWORD=${REDIS_PASSWORD}
REDIS_BACKEND_PASSWORD=${REDIS_BACKEND_PASSWORD}
INTERNAL_API_SECRET=$(openssl rand -hex 16)
AGENT_AUTH_SECRET=$(openssl rand -hex 32)
CREDENTIAL_ENCRYPTION_KEY=$(openssl rand -base64 32)
DOCKER_GID=0
ENV
docker cp /tmp/.ent873-env.$$ "$DIND":/src/.env >/dev/null; rm -f /tmp/.ent873-env.$$
ish 'mkdir -p /src/trinity-data && chown -R 1000:1000 /src/trinity-data'

log "== build (inside dind; production Dockerfiles)"
compose build backend frontend mcp-server > >(tail -3 | tee -a "$EVIDENCE") 2>&1 || die "build"
inner docker build -q -f /baseline/docker/frontend/Dockerfile.prod -t trinity-frontend:baseline /baseline/src/frontend \
    >/dev/null || die "baseline frontend build"
compose config > /dev/null || die "compose config"

log "== image checks"
BE_IMG="${PROJECT}-backend"
FE_IMG="trinity-frontend:prod"
in_image() {   # in_image <image> <<'EOF' ...shell... EOF  — runs as the image's own user
    docker exec -i "$DIND" docker run --rm -i --entrypoint sh "$1" -s
}
log "      backend CMD: $(inner docker image inspect -f '{{json .Config.Cmd}}' "$BE_IMG")"
check "backend image CMD disables uvicorn forwarding" \
    sh -c "docker exec $DIND docker image inspect -f '{{json .Config.Cmd}}' $BE_IMG | grep -q -- '--no-proxy-headers' && ! docker exec $DIND docker image inspect -f '{{json .Config.Cmd}}' $BE_IMG | grep -q -- 'forwarded-allow-ips'"
check "backend image packages utils/client_ip.py and its uvicorn knows --no-proxy-headers" in_image "$BE_IMG" <<'EOF'
python -c 'import utils.client_ip' && uvicorn --help | grep -q -- '--no-proxy-headers'
EOF
check "frontend image runs as UID 101" in_image "$FE_IMG" <<'EOF'
[ "$(id -u)" = 101 ]
EOF
check "frontend nginx has the realip module" in_image "$FE_IMG" <<'EOF'
nginx -V 2>&1 | grep -q http_realip_module
EOF
check "render script substitutes only the gateway and keeps every nginx variable" in_image "$FE_IMG" <<'EOF'
TRINITY_PROXY_GATEWAY=172.30.0.1 render-nginx.sh true >/dev/null 2>&1 || exit 1
grep -q 'set_real_ip_from 172.30.0.1;' /etc/nginx/generated/default.conf || exit 1
grep -qF '${' /etc/nginx/generated/default.conf && exit 1
grep -qF 'map $http_x_forwarded_proto $fwd_proto' /etc/nginx/generated/default.conf || exit 1
grep -qF '$proxy_add_x_forwarded_for' /etc/nginx/nginx-server.conf
EOF
check "a non-IPv4 gateway value is refused before nginx starts" in_image "$FE_IMG" <<'EOF'
! TRINITY_PROXY_GATEWAY='192.0.2.66; allow all' render-nginx.sh true 2>/dev/null
EOF
check "only the generated directory is writable by the runtime user" in_image "$FE_IMG" <<'EOF'
touch /etc/nginx/generated/x && ! touch /etc/nginx/conf.d/x 2>/dev/null \
  && ! touch /etc/nginx/x 2>/dev/null && ! touch /etc/nginx/trinity/x 2>/dev/null \
  && ! touch /etc/nginx/nginx-server.conf 2>/dev/null && ! touch /etc/nginx/nginx.conf 2>/dev/null
EOF

log "== start (prod compose: backend, frontend, mcp-server and their dependencies)"
compose up -d backend frontend mcp-server > >(tail -2 | tee -a "$EVIDENCE") 2>&1 || die "compose up"
for _ in $(seq 1 120); do ish 'wget -qO- http://127.0.0.1:8000/health >/dev/null 2>&1' && break; sleep 2; done
ish 'wget -qO- http://127.0.0.1:8000/health >/dev/null 2>&1' || { compose logs backend | tail -40; die "backend never healthy"; }
for _ in $(seq 1 60); do ish 'wget -qO- http://127.0.0.1:80/ >/dev/null 2>&1' && break; sleep 2; done
log "backend command: $(inner docker inspect -f '{{json .Config.Cmd}}' trinity-backend)"
# nginx resolves its upstreams at load, so its own config test belongs inside
# the running stack, where trinity-backend and trinity-mcp-server resolve.
check "the running frontend's rendered config passes nginx -t (both listeners)" \
    sh -c "docker exec $DIND docker exec trinity-frontend nginx -t >/dev/null 2>&1 && docker exec $DIND docker exec trinity-frontend sh -c 'grep -c \"listen 8082 proxy_protocol\" /etc/nginx/generated/default.conf' | grep -qx 1"
log "networks: $(inner docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}={{$v.IPAddress}} {{end}}' trinity-frontend) | backend $(inner docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}={{$v.IPAddress}} {{end}}' trinity-backend) | mcp $(inner docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}={{$v.IPAddress}} {{end}}' trinity-mcp-server)"
ish "docker exec trinity-backend python -c \"from database import db; db.set_setting('setup_completed','true')\"" >/dev/null 2>&1

for c in a b; do
    ip=$CLIENT_A; [ "$c" = b ] && ip=$CLIENT_B
    docker run -d "${LABELS[@]}" --name "${PROJECT}-client-$c" --network "$NET" --ip "$ip" \
        --entrypoint sleep "$CURL_IMAGE" infinity >/dev/null || die "client $c"
done

# --------------------------------------------------------------------------
# Helpers over the running stack
# --------------------------------------------------------------------------
redis() { inner docker exec trinity-redis redis-cli --no-auth-warning -a "$REDIS_PASSWORD" "$@"; }
reset_limits() { redis --scan --pattern 'login_attempts*' | while read -r k; do [ -n "$k" ] && redis del "$k" >/dev/null; done; }
ip_keys() { redis --scan --pattern 'login_attempts_ip:*' | sort | while read -r k; do printf '%s=%s ' "$k" "$(redis get "$k")"; done; }
audit_ips() {   # actor_ip of the last N login rows
    inner docker exec trinity-backend python -c "
import sqlite3; c = sqlite3.connect('/data/trinity.db')
rows = c.execute(\"select actor_ip from audit_log where event_action in ('login_failed','login_success') order by id desc limit $1\").fetchall()
print(' '.join(r[0] or '-' for r in reversed(rows)))"
}
access_log_tail() { inner docker logs --tail "$1" trinity-backend 2>&1 | grep -E '"(POST|GET|WebSocket) ' ; }
# login <client a|b> <url> <username> <password> [extra curl args] -> HTTP status
login() {
    local who="$1" url="$2" user="$3" pw="$4"; shift 4
    docker exec "${PROJECT}-client-${who}" curl -s -o /dev/null -w '%{http_code}' --max-time 15 \
        -X POST "$url/api/token" --data-urlencode "username=${user}" --data-urlencode "password=${pw}" "$@"
}
login_in_dind() {   # from the dind host's own namespace
    local url="$1" user="$2" pw="$3"; shift 3
    inner curl -s -o /dev/null -w '%{http_code}' --max-time 15 -X POST "$url/api/token" \
        --data-urlencode "username=${user}" --data-urlencode "password=${pw}" "$@"
}
expect_keys() { local want="$1" got; got="$(ip_keys)"; log "      keys: ${got:-<none>}"; [ "$got" = "$want" ]; }
SPOOF=(-H "X-Forwarded-For: 10.66.0.1" -H "X-Real-IP: 10.66.0.1" -H "CF-Connecting-IP: 10.66.0.1")

# --------------------------------------------------------------------------
# Path C — bare install, nginx published on :80 (inner DNAT)
# --------------------------------------------------------------------------
log "== path C: two outer clients -> dind :80 -> nginx -> backend"
reset_limits
for i in 1 2 3; do login a "http://${DIND_IP}" "c-a$i@example.com" wrong "${SPOOF[@]}" >/dev/null; done
for i in 1 2; do login b "http://${DIND_IP}" "c-b$i@example.com" wrong "${SPOOF[@]}" >/dev/null; done
check "C: each client has its own bucket, the forged header none" \
    expect_keys "login_attempts_ip:${CLIENT_A}=3 login_attempts_ip:${CLIENT_B}=2 "
log "      audit: $(audit_ips 5)"
[ "$(audit_ips 5)" = "${CLIENT_A} ${CLIENT_A} ${CLIENT_A} ${CLIENT_B} ${CLIENT_B}" ] \
    && pass "C: audit actor_ip = real clients" || fail "C: audit actor_ip = real clients"
log "      access log:"; access_log_tail 6 | tail -3 | sed 's/^/        /' | tee -a "$EVIDENCE" >/dev/null

log "== AC1 over the real stack: each client's bucket counts only its own attempts"
reset_limits
for i in $(seq 1 30); do login a "http://${DIND_IP}" "ac1-$i@example.com" wrong >/dev/null; done
st_a=$(login a "http://${DIND_IP}" "ac1-next@example.com" wrong)
st_b=$(login b "http://${DIND_IP}" admin "$ADMIN_PASSWORD")
log "      A after 30 failures -> ${st_a}; B with the right password -> ${st_b}"
[ "$st_a" = 429 ] && [ "$st_b" = 200 ] && pass "AC1: A blocked (429), B served (200)" || fail "AC1: A blocked (429), B served (200)"

# --------------------------------------------------------------------------
# Path E — direct callers to the published backend port
# --------------------------------------------------------------------------
log "== path E: direct :8000 (off-box and gateway-originated)"
reset_limits
login a "http://${DIND_IP}:8000" e-a@example.com wrong "${SPOOF[@]}" >/dev/null
check "E: an off-box direct caller is keyed on itself whatever it sends" \
    expect_keys "login_attempts_ip:${CLIENT_A}=1 "
reset_limits
login_in_dind "http://127.0.0.1:8000" e-gw@example.com wrong "${SPOOF[@]}" >/dev/null
gw_key="$(ip_keys)"
log "      gateway-originated: ${gw_key}"
case "$gw_key" in
    "login_attempts_ip:172.2"[89]".0.1=1 "|"login_attempts_ip:${PREFIX}.1=1 ") pass "E: a host-forwarded caller is keyed on the bridge gateway, never a header" ;;
    *) fail "E: a host-forwarded caller is keyed on the bridge gateway, never a header" ;;
esac

# --------------------------------------------------------------------------
# Path F — an agent container
# --------------------------------------------------------------------------
log "== path F: agent on trinity-agent-network"
reset_limits
inner docker run --rm --network trinity-agent-network --name "${PROJECT}-agent" "$CURL_IMAGE" \
    -s -o /dev/null --max-time 15 -X POST http://backend:8000/api/token \
    --data-urlencode username=f@example.com --data-urlencode password=wrong "${SPOOF[@]}" >/dev/null
agent_key="$(ip_keys)"; log "      agent: ${agent_key}"
case "$agent_key" in "login_attempts_ip:172.28."*"=1 ") pass "F: an agent is keyed on its own address" ;; *) fail "F: an agent is keyed on its own address" ;; esac
for target in "172.28.0.1:18082" "${PREFIX}.2:8082" "${PREFIX}.2:8080" "${PREFIX}.1:18082"; do
    expect_unreachable "F: agent gets no TCP connection to ${target}" \
        "$(connect_from_net trinity-agent-network "${target%:*}" "${target#*:}")"
done
# And a forged PROXY preamble from the agent is never honoured: no response,
# and no bucket for the address it claimed.
reset_limits
for target in "${PREFIX}.2:8082" "172.28.0.1:18082"; do
    expect_no_response "F: a forged PROXY preamble from an agent to ${target} gets no response" \
        "$(forged_v2_from_net trinity-agent-network "${target%:*}" "${target#*:}")" \
        "$(connect_from_net trinity-agent-network "${target%:*}" "${target#*:}")"
done
check "F: the forged client address never became a key" expect_keys ""
# A peer that CAN reach the PROXY listener but is not the gateway (a pool
# address on the ingress network): nginx parses the preamble but does not
# believe it, so the request is attributed to that peer.
reset_limits
st="$(forged_v2_from_net trinity-proxy-network "${PREFIX}.2" 8082 "${PREFIX}.201")"
log "      pool peer ${PREFIX}.201 forged PROXY v2 -> ${PREFIX}.2:8082: ${st}"
case "$st" in
    "RESPONSE HTTP/1.1 401"*)
        check "F: a forged PROXY preamble from a non-gateway peer is attributed to that peer" \
            expect_keys "login_attempts_ip:${PREFIX}.201=1 " ;;
    ERROR*) error "F: forged preamble from a non-gateway peer — probe failed: $st" ;;
    *) fail "F: a forged PROXY preamble from a non-gateway peer is served and attributed to it ($st)" ;;
esac

# --------------------------------------------------------------------------
# Path H — the MCP server
# --------------------------------------------------------------------------
log "== path H: MCP server -> backend"
reset_limits
inner docker exec trinity-mcp-server wget -q -O /dev/null --header "X-Forwarded-For: 10.66.0.1" \
    --post-data "username=h@example.com&password=wrong" http://backend:8000/api/token >/dev/null 2>&1
mcp_key="$(ip_keys)"; mcp_ips="$(inner docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' trinity-mcp-server)"
log "      mcp: ${mcp_key} (mcp addresses: ${mcp_ips})"
case " $mcp_ips" in *" ${mcp_key#login_attempts_ip:}"*) ;; esac
k="${mcp_key#login_attempts_ip:}"; k="${k%=1 }"
case " $mcp_ips " in *" $k "*) pass "H: MCP requests are keyed on the MCP container, never a header" ;; *) fail "H: MCP requests are keyed on the MCP container, never a header" ;; esac

# --------------------------------------------------------------------------
# Path B — tunnel stand-in at the reserved cloudflared address
# --------------------------------------------------------------------------
log "== path B: tunnel stand-in at ${PREFIX}.3 (both routing shapes)"
docker cp "$FIX/relay.Caddyfile" "$DIND":/src/relay.Caddyfile >/dev/null
inner docker run -d --name "${PROJECT}-relay" --network trinity-proxy-network --ip "${PREFIX}.3" \
    --add-host "backend:${PREFIX}.4" --add-host "trinity-backend:${PREFIX}.4" \
    -p 9001-9005:9001-9005 -v /src/relay.Caddyfile:/etc/caddy/Caddyfile:ro "$CADDY_IMAGE" >/dev/null
# Like the compose service: also on the platform and agent networks, so the
# host mapping (not network order) is what routes `backend` to the ingress hop.
inner docker network connect trinity-platform-network "${PROJECT}-relay"
inner docker network connect trinity-agent-network "${PROJECT}-relay"
sleep 2
reset_limits
login a "http://${DIND_IP}:9001" b1@example.com wrong >/dev/null
login b "http://${DIND_IP}:9002" b2@example.com wrong >/dev/null
check "B: catch-all (A) and direct-backend (B) rules each key on the edge's client" \
    expect_keys "login_attempts_ip:${CLIENT_A}=1 login_attempts_ip:${CLIENT_B}=1 "
reset_limits
login a "http://${DIND_IP}:9003" b3@example.com wrong >/dev/null
login a "http://${DIND_IP}:9005" b5@example.com wrong >/dev/null
check "B: absent client identity falls back to the proxy hop, never the XFF prefix" \
    expect_keys "login_attempts_ip:${PREFIX}.2=1 login_attempts_ip:${PREFIX}.3=1 "
st=$(login a "http://${DIND_IP}:9004" b4@example.com wrong)
log "      malformed CF-Connecting-IP -> ${st}"
[ "$st" = 400 ] && pass "B: malformed required identity is refused (400)" || fail "B: malformed required identity is refused (400)"
ws_bad=$(docker exec "${PROJECT}-client-a" curl -s -o /dev/null -w '%{http_code}' --max-time 10 --http1.1 \
    -H 'Connection: Upgrade' -H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' \
    -H "Sec-WebSocket-Key: $(ws_nonce)" "http://${DIND_IP}:9004/ws")
ws_ok=$(docker exec "${PROJECT}-client-b" curl -s -o /dev/null -w '%{http_code}' --max-time 10 --http1.1 \
    -H 'Connection: Upgrade' -H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' \
    -H "Sec-WebSocket-Key: $(ws_nonce)" "http://${DIND_IP}:9002/ws")
ws_line="$(inner docker logs --tail 30 trinity-backend 2>&1 | grep 'WebSocket /ws' | tail -2 | tr '\n' '|')"
log "      ws malformed -> ${ws_bad}; ws valid -> ${ws_ok}; access log: ${ws_line}"
[ "$ws_bad" = 403 ] && pass "B: a WebSocket with malformed identity is refused before accept" || fail "B: a WebSocket with malformed identity is refused before accept"
case "$ws_line" in *"${CLIENT_B}:0 - \"WebSocket /ws"*) pass "B: the access log names the resolved WebSocket client" ;; *) fail "B: the access log names the resolved WebSocket client" ;; esac

log "== cloudflared image: host-file resolution order"
inner docker pull -q cloudflare/cloudflared:latest >/dev/null 2>&1
inner docker create --name "${PROJECT}-cfd" cloudflare/cloudflared:latest >/dev/null 2>&1
nss="$(inner docker cp "${PROJECT}-cfd":/etc/nsswitch.conf - 2>/dev/null | tar -xO 2>/dev/null | grep '^hosts:')"
inner docker rm -f "${PROJECT}-cfd" >/dev/null 2>&1
log "      cloudflared /etc/nsswitch.conf: ${nss:-<absent>}"
case "$nss" in "hosts:"*files*dns*) pass "B: cloudflared's image resolves /etc/hosts before DNS" ;; *) fail "B: cloudflared's image resolves /etc/hosts before DNS (nsswitch: ${nss:-absent})" ;; esac

# --------------------------------------------------------------------------
# Path D — an operator's own proxy, trusted and not
# --------------------------------------------------------------------------
log "== path D: operator proxy at ${LB_IP} in front of :80"
sed "s/UPSTREAM/${DIND_IP}:80/" "$FIX/operator-proxy.conf" > /tmp/.ent873-lb.$$
# Configured before it starts: a reload racing nginx's own start is lost.
docker create "${LABELS[@]}" --name "${PROJECT}-lb" --network "$NET" --ip "$LB_IP" "$LB_IMAGE" >/dev/null
docker cp /tmp/.ent873-lb.$$ "${PROJECT}-lb":/etc/nginx/conf.d/default.conf >/dev/null; rm -f /tmp/.ent873-lb.$$
docker start "${PROJECT}-lb" >/dev/null
for _ in $(seq 1 20); do docker exec "${PROJECT}-client-a" curl -s -o /dev/null --max-time 2 "http://${LB_IP}:8088/" && break; sleep 1; done
reset_limits
login a "http://${LB_IP}:8088" d1@example.com wrong -H "X-Forwarded-For: 10.66.0.1" >/dev/null
check "D: without TRUSTED_PROXIES the operator proxy is just a client" \
    expect_keys "login_attempts_ip:${LB_IP}=1 "
ish "echo TRUSTED_PROXIES=${LB_IP} >> /src/.env"
compose up -d --no-deps backend > /dev/null 2>&1
for _ in $(seq 1 90); do ish 'wget -qO- http://127.0.0.1:8000/health >/dev/null 2>&1' && break; sleep 2; done
reset_limits
login a "http://${LB_IP}:8088" d2@example.com wrong -H "X-Forwarded-For: 10.66.0.1" >/dev/null
check "D: with TRUSTED_PROXIES the client it appended is the key, the forged prefix is not" \
    expect_keys "login_attempts_ip:${CLIENT_A}=1 "
ish "sed -i 's/^TRUSTED_PROXIES=.*/TRUSTED_PROXIES=0.0.0.0\\/0/' /src/.env"
compose up -d --no-deps backend > /dev/null 2>&1
refusal=""
for _ in $(seq 1 45); do
    refusal="$(inner docker logs trinity-backend 2>&1 | grep -m1 'ClientIPConfigError: TRUSTED_PROXIES entry')"
    [ -n "$refusal" ] && break
    sleep 2
done
log "      backend state: $(inner docker inspect -f '{{.State.Status}} restarts={{.RestartCount}}' trinity-backend)"
health="$(ish 'wget -qO- -T 3 http://127.0.0.1:8000/health >/dev/null 2>&1 && echo answers || echo silent')"
log "      startup with TRUSTED_PROXIES=0.0.0.0/0: ${refusal} | /health ${health}"
case "$refusal|$health" in *"'0.0.0.0/0'"*"|silent") pass "D: an invalid trust list stops the backend serving, naming the entry" ;; *) fail "D: an invalid trust list stops the backend serving, naming the entry" ;; esac
ish "sed -i '/^TRUSTED_PROXIES=/d' /src/.env"
compose up -d --no-deps backend > /dev/null 2>&1
for _ in $(seq 1 90); do ish 'wget -qO- http://127.0.0.1:8000/health >/dev/null 2>&1' && break; sleep 2; done

# --------------------------------------------------------------------------
# Path A — a provisioned host: Caddy on the host, web listener on loopback
# --------------------------------------------------------------------------
log "== path A: provisioned layout (FRONTEND_PORT=8081 on 127.0.0.1, PROXY listener on 127.0.0.1:18082)"
ish "printf 'FRONTEND_PORT=8081\nFRONTEND_BIND_ADDRESS=127.0.0.1\n' >> /src/.env"
compose up -d --no-deps frontend > /dev/null 2>&1
for _ in $(seq 1 60); do ish 'wget -qO- http://127.0.0.1:8081/ >/dev/null 2>&1' && break; sleep 2; done
log "      published: $(inner docker port trinity-frontend | tr '\n' ' ')"
check "A: the ordinary listener is loopback-only now" \
    sh -c "! docker exec ${PROJECT}-client-a curl -s -o /dev/null --max-time 5 http://${DIND_IP}:8081/"
expect_unreachable "A: the PROXY listener gets no TCP connection from off-box" \
    "$(connect_from_client a "$DIND_IP" 18082)"
expect_unreachable "A: the ordinary listener gets no TCP connection from off-box either" \
    "$(connect_from_client a "$DIND_IP" 8081)"
reset_limits
_errf="$(mktemp)"
st=$(docker exec "${PROJECT}-client-a" curl -sS -o /dev/null -w '%{http_code}' --max-time 5 \
    --haproxy-clientip "$FORGED_CLIENT" -X POST "http://${DIND_IP}:18082/api/token" \
    --data-urlencode username=forged-offbox@example.com --data-urlencode password=wrong 2>"$_errf"); rc=$?
log "      off-box forged PROXY preamble -> ${DIND_IP}:18082: status=${st:-<none>} rc=${rc} $(head -c 200 "$_errf" | tr '\n' ' ')"
rm -f "$_errf"
# curl 7 refused, 28 timed out, 52 empty reply, 56 reset: nothing answered.
case "${st}|${rc}" in
    000\|7|000\|28|000\|52|000\|56) pass "A: an off-box forged PROXY preamble gets no response" ;;
    000\|*|\|*) error "A: off-box forged preamble — probe failed (status=${st:-none}, rc=${rc})" ;;
    *) fail "A: an off-box forged PROXY preamble gets no response (status ${st})" ;;
esac
check "A: no bucket for the forged off-box client" expect_keys ""

# Run start.sh's OWN functions inside the dind host namespace (where the stack's
# ports are published). $1 = function-extraction spec, rest = shell to append.
run_startsh_fns() {
    local tmp; tmp="$(mktemp)"
    {
        sed -n '/^env_value() {/,/^}/p' "$ROOT/scripts/deploy/start.sh"
        cat
    } > "$tmp"
    # Not /tmp: dind mounts a tmpfs there, which `docker cp` writes underneath.
    docker cp "$tmp" "$DIND":/src/.startsh-fns.sh >/dev/null; rm -f "$tmp"
    inner sh -c 'cd /src && bash /src/.startsh-fns.sh'
}
target_fns() {
    awk '/^provision_proxy_port\(\) \{/{f=1} /^provision_reverse_proxy_block\(\) \{/{f=0} f' "$ROOT/scripts/deploy/start.sh"
}

# The shipped Caddy pieces: start.sh's own block builder, and the full rendered
# Caddyfile validated by Caddy 2.11.4 in both modes.
eval "$(sed -n '/^provision_reverse_proxy_block() {/,/^}/p' "$ROOT/scripts/deploy/start.sh")"
caddy_front() {   # $1 = port, $2 = 1 for PROXY transport
    printf '{\n\tauto_https off\n\tadmin localhost:2019\n}\n:8443 {\n    %s\n}\n' "$(provision_reverse_proxy_block '    ' "$1" "$2")"
}
render_full() {   # the real renderer, host stubbed, probe answer $1
    local src out
    src="$ROOT/scripts/deploy/start.sh"
    out="$(mktemp)"
    bash -c "
$(sed -n '/^provision_private_cidrs() {/,/^}/p' "$src")
$(awk '/^provision_caddyfile\(\) \{/{f=1} /^provision_site\(\) \{/{f=0} f' "$src" | sed "s#/etc/caddy/Caddyfile#${out}#g")
env_value() { [ \"\$1\" = PRIVATE_NETWORK_CIDRS ] && printf '%s' 100.64.0.0/10; return 0; }
systemctl() { :; }; caddy() { :; }
provision_proxy_listener_ready() { return $1; }
provision_caddyfile 203.0.113.10 do-marketplace" >/dev/null 2>&1
    cat "$out"; rm -f "$out"
}
for mode in 0 1; do
    # A fresh file per mode, mounted read-only by a fresh path: a re-created
    # file at a reused path can reach the container stale through Desktop's
    # file sharing.
    cf="$(mktemp "${TMPDIR:-/tmp}/ent873-caddy-${mode}.XXXXXX")"
    render_full "$mode" > "$cf"
    if v="$(docker run --rm -v "$cf":/etc/caddy/Caddyfile:ro "$CADDY_IMAGE" \
            caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile 2>&1)"; then
        pass "A: Caddy 2.11.4 validates the rendered Caddyfile (probe rc=${mode}, $(grep -c proxy_protocol "$cf") PROXY transports)"
    else
        fail "A: Caddy 2.11.4 validates the rendered Caddyfile (probe rc=${mode}): $(echo "$v" | tail -2 | tr '\n' ' ')"
    fi
    rm -f "$cf"
done

caddy_front 8081 0 > /tmp/.ent873-front.$$
docker run -d "${LABELS[@]}" --name "${PROJECT}-caddy" --network "container:${DIND}" \
    -v /tmp/.ent873-front.$$:/etc/caddy/Caddyfile "$CADDY_IMAGE" >/dev/null
sleep 2
reset_limits
login a "http://${DIND_IP}:8443" a-legacy1@example.com wrong >/dev/null
login b "http://${DIND_IP}:8443" a-legacy2@example.com wrong >/dev/null
check "A (ordinary listener, compatibility fallback): both clients are attributed to the gateway" \
    expect_keys "login_attempts_ip:${PREFIX}.1=2 "

# The probe start.sh runs, against the live stack, from the host namespace.
probe_ok=$(ish 'cd /src && bash scripts/deploy/probe-proxy-listener.sh 18082 && echo yes || echo no')
probe_plain=$(ish 'cd /src && bash scripts/deploy/probe-proxy-listener.sh 8081 && echo yes || echo no')
target=$({ target_fns; echo 'provision_caddy_target'; } | run_startsh_fns 2>/dev/null)
log "      probe 18082 -> ${probe_ok}; probe 8081 -> ${probe_plain}; start.sh target -> ${target}"
[ "$probe_ok" = yes ] && [ "$probe_plain" = no ] && [ "$target" = "18082 proxy" ] \
    && pass "A: the live probe accepts the PROXY listener and rejects the ordinary one" \
    || fail "A: the live probe accepts the PROXY listener and rejects the ordinary one"

caddy_front 18082 1 > /tmp/.ent873-front.$$
docker exec "${PROJECT}-caddy" caddy reload --config /etc/caddy/Caddyfile >/dev/null 2>&1
sleep 1
reset_limits
login a "http://${DIND_IP}:8443" a1@example.com wrong >/dev/null
login b "http://${DIND_IP}:8443" a2@example.com wrong >/dev/null
check "A (PROXY listener): each client keeps its own address through Docker publishing" \
    expect_keys "login_attempts_ip:${CLIENT_A}=1 login_attempts_ip:${CLIENT_B}=1 "
reset_limits
for i in $(seq 1 30); do login a "http://${DIND_IP}:8443" "a-ex$i@example.com" wrong >/dev/null; done
st_a=$(login a "http://${DIND_IP}:8443" a-next@example.com wrong)
st_b=$(login b "http://${DIND_IP}:8443" admin "$ADMIN_PASSWORD")
log "      via Caddy: A after 30 -> ${st_a}; B -> ${st_b}; audit: $(audit_ips 2)"
[ "$st_a" = 429 ] && [ "$st_b" = 200 ] && pass "AC1 via Caddy: A blocked, B served" || fail "AC1 via Caddy: A blocked, B served"
[ "$(audit_ips 1)" = "$CLIENT_B" ] && pass "A: the audit row names the real client" || fail "A: the audit row names the real client"
reset_limits
st6=$(login_in_dind "http://[::1]:8443" a6@example.com wrong -g)
check "A: an IPv6 client crosses PROXY v2 intact (status ${st6})" expect_keys "login_attempts_ip:::1=1 "
ws=$(docker exec "${PROJECT}-client-a" curl -s -o /dev/null -w '%{http_code}' --max-time 10 --http1.1 \
    -H 'Connection: Upgrade' -H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' \
    -H "Sec-WebSocket-Key: $(ws_nonce)" "http://${DIND_IP}:8443/ws")
ws_line="$(inner docker logs --tail 20 trinity-backend 2>&1 | grep 'WebSocket /ws' | tail -1)"
log "      WebSocket via Caddy+PROXY -> ${ws}; access log: ${ws_line}"
case "$ws_line" in *"${CLIENT_A}:0 - \"WebSocket /ws"*) pass "A: WebSocket upgrades reach the backend attributed to the client" ;; *) fail "A: WebSocket upgrades reach the backend attributed to the client" ;; esac
# A COMPLETED upgrade over the PROXY transport (keepalive off, HTTP/1.1):
# sign in, mint a WebSocket ticket, connect, and read the 101.
token=$(docker exec "${PROJECT}-client-b" curl -s --max-time 15 -X POST "http://${DIND_IP}:8443/api/token" \
    --data-urlencode username=admin --data-urlencode "password=${ADMIN_PASSWORD}" | sed -n 's/.*"access_token":"\([^"]*\)".*/\1/p')
ticket=$(docker exec "${PROJECT}-client-b" curl -s --max-time 15 -X POST -H "Authorization: Bearer ${token}" \
    "http://${DIND_IP}:8443/api/ws/ticket" | sed -n 's/.*"ticket":"\([^"]*\)".*/\1/p')
ws101=$(docker exec "${PROJECT}-client-b" curl -s -o /dev/null -w '%{http_code}' --max-time 4 --http1.1 \
    -H 'Connection: Upgrade' -H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' \
    -H "Sec-WebSocket-Key: $(ws_nonce)" "http://${DIND_IP}:8443/ws?ticket=${ticket}")
ws_ok_line="$(inner docker logs --tail 20 trinity-backend 2>&1 | grep 'WebSocket /ws?ticket' | tail -1 | sed 's/ticket=[^ "]*/ticket=<redacted>/')"
log "      authenticated WebSocket via Caddy+PROXY -> ${ws101}; access log: ${ws_ok_line}"
case "${ws101}|${ws_ok_line}" in 101\|*"${CLIENT_B}:0 - \"WebSocket /ws?ticket"*accepted*) pass "A: a WebSocket upgrade completes (101) through the PROXY transport, attributed to the client" ;;
    *) fail "A: a WebSocket upgrade completes (101) through the PROXY transport, attributed to the client" ;; esac
nginx_plain=$(docker exec "${PROJECT}-client-a" curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://${DIND_IP}:8443/")
[ "$nginx_plain" = 200 ] && pass "A: the SPA is served through the PROXY listener" || fail "A: the SPA is served through the PROXY listener (${nginx_plain})"

# --------------------------------------------------------------------------
# Version skew — this checkout, the previous web image
# --------------------------------------------------------------------------
log "== skew: new compose + start.sh, previous frontend image"
inner docker tag trinity-frontend:prod trinity-frontend:ent873-new
inner docker tag trinity-frontend:baseline trinity-frontend:prod
compose up -d --no-deps --no-build frontend > /dev/null 2>&1
for _ in $(seq 1 60); do ish 'wget -qO- http://127.0.0.1:8081/ >/dev/null 2>&1' && break; sleep 2; done
target=$({ target_fns; echo 'provision_caddy_target'; } | run_startsh_fns 2>/dev/null)
log "      start.sh target against the previous image -> ${target}"
[ "$target" = "8081 plain" ] && pass "skew: the probe refuses a web image without the listener" || fail "skew: the probe refuses a web image without the listener"
caddy_front 8081 0 > /tmp/.ent873-front.$$
docker exec "${PROJECT}-caddy" caddy reload --config /etc/caddy/Caddyfile >/dev/null 2>&1; sleep 1
st=$(docker exec "${PROJECT}-client-a" curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://${DIND_IP}:8443/")
[ "$st" = 200 ] && pass "skew: the site stays up on the ordinary listener" || fail "skew: the site stays up on the ordinary listener (${st})"
rm -f /tmp/.ent873-front.$$
inner docker tag trinity-frontend:ent873-new trinity-frontend:prod

# --------------------------------------------------------------------------
# start.sh's subnet preflight against a real daemon
# --------------------------------------------------------------------------
log "== preflight: ingress subnet vs. existing networks (inside dind)"
pf() {
    { echo "HOSTED=1; TRINITY_PROXY_PREFIX=$1"
      awk '/^_ip4_int\(\) \{/{f=1} /^preflight_proxy_network \|\| exit 1/{f=0} f' "$ROOT/scripts/deploy/start.sh"
      echo 'preflight_proxy_network; echo rc=$?'; } | run_startsh_fns 2>&1
}
out=$(pf 172.30.0); log "      owned network (running stack): ${out##*$'\n'}"
case "$out" in *rc=0) pass "preflight: the network this install owns is accepted" ;; *) fail "preflight: owned network accepted: $out" ;; esac
inner docker network create --label "$LABEL" --subnet 10.99.0.0/16 "${PROJECT}-conflict" >/dev/null
out=$(pf 10.99.4); log "      conflicting: $(echo "$out" | grep -m1 overlaps)"
case "$out" in *"${PROJECT}-conflict"*"10.99.0.0/16"*rc=1) pass "preflight: an overlapping network is refused by name" ;; *) fail "preflight: overlapping refused: $out" ;; esac
out=$(pf 10.98.4)
case "$out" in *"trinity-proxy-network exists with a different subnet"*rc=1) pass "preflight: a changed prefix is refused while the old network still exists" ;; *) fail "preflight: changed prefix: $out" ;; esac
inner docker network rm "${PROJECT}-conflict" >/dev/null

log "== result: $FAILS failure(s)"
[ "$FAILS" = 0 ]
