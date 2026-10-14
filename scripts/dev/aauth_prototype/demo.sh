#!/usr/bin/env bash
# ent#623 — AAuth cross-instance A2A demo on one machine (prototype, flag OFF by default).
#
# Two INDEPENDENT Trinity instances, no bearer secret of either configured on
# the other:
#   A = the local dev stack (http://localhost:8000), the caller;
#   B = a second stack inside its own Docker daemon (docker:dind), the callee,
#       with its own DB, Redis, platform secrets and signing key. Its own daemon
#       matters: a second backend on the SAME daemon sees A's agent containers
#       and volumes as its own, and its cleanup sweep would delete A's
#       unattached agent volumes about 15 minutes in.
# Each instance's AAuth issuer is a public HTTPS name from a Cloudflare quick
# tunnel, fronted by an nginx that passes ONLY /.well-known/aauth-* and /a2a/*.
#
# Usage:
#   scripts/dev/aauth_prototype/demo.sh up        # tunnels + fronts + B + wire both sides
#   scripts/dev/aauth_prototype/demo.sh rewire    # new tunnel URLs → re-point issuers/allow-list/endpoint
#   scripts/dev/aauth_prototype/demo.sh run       # the scenario (see below); step 7 waits for the owner
#   scripts/dev/aauth_prototype/demo.sh rehearse  # steps 1–8 with no pauses (binding asserted, not performed)
#   scripts/dev/aauth_prototype/demo.sh status
#   scripts/dev/aauth_prototype/demo.sh down      # tunnels, fronts, B (A's flag switched back off)
#   scripts/dev/aauth_prototype/bind.sh <agent> <resource>   # ent#878: bind an agent to its owner at the
#                                                            # person server (link + code), live
#   scripts/dev/aauth_prototype/senzing_check.py --agent <a> --key <the agent's MCP key> --name … --country …
#                                                            # ent#879: the sanctions check from a laptop
#
# Person server (ent#878): `up`/`wire` set `aauth_person_server` on A (AAUTH_PERSON_SERVER,
# default https://person.hello.coop). Do NOT `rewire` after binding an agent with bind.sh:
# a new tunnel host is a new issuer, and the person server sees a NEW agent
# (aauth:<agent>@<new-host>) — the binding must be redone.
# Resources (ent#879): `up`/`wire` set `aauth_resource_allowlist` on A to the Senzing
# sanctions resource + whoami (AAUTH_SENZING, AAUTH_WHOAMI); `down` clears both keys.
# Step 8 passes `execution_id` + a per-call `dedup_label` (the #1084 effect guard's
# identity). The guard ARMS only for a real execution row owned by the agent — the
# MCP tool path, where the session carries the id — so a script run is honest about
# it: DEMO_EXECUTION_ID defaults to `manual` (no execution to re-deliver: proceeds
# unguarded, no alarm; an unknown id would be refused on a pull-pilot agent). Set it
# to a live execution id of the agent to watch the same label replay instead of
# re-charging. SANCTIONS_NAME / SANCTIONS_COUNTRY pick the screened name (an entry
# listed on OpenSanctions); the defaults are a well-known listed person.
#
# Scenario (`run`), all against the real endpoints:
#   1. whoami.aauth.dev echoes A's test-echo identity   (a verifier that is not ours)
#   2. A/test-echo  → B/bravo over AAuth                 (allow-listed: task runs)
#   3. A/test-counter → B/bravo over AAuth               (not listed: 403)
#   4. tampered signature                                 (401 before the body is parsed)
#   5. bearer call to B with B's own MCP key              (dual acceptance)
#   6. B's audit rows                                     (who called, verification, decision)
#   7. bind A/test-echo to its owner at the person server (ent#878: link + code, waits for `issued`)
#   8. sanctions check at senzing.aauth.dev as A/test-echo  (ent#879: the ladder — person-token →
#      auth-token → 200 — then a second call with ZERO person-server trips; A's audit rows)
#
# Requirements: docker, cloudflared, python3, the A dev stack running from this
# clone, ADMIN_PASSWORD in ./.env. State lives in $STATE (default /tmp/aauth-demo).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
HERE="$REPO/scripts/dev/aauth_prototype"
STATE="${AAUTH_DEMO_STATE:-/tmp/aauth-demo}"
A_API="http://localhost:8000"
B_API="http://127.0.0.1:8100"
DIND="trinity-b-dind"
B_DOCKER="tcp://127.0.0.1:23750"
A_AGENT="${A_AGENT:-test-echo}"
A_OTHER_AGENT="${A_OTHER_AGENT:-test-counter}"
B_AGENT="${B_AGENT:-bravo}"
PERSON_SERVER="${AAUTH_PERSON_SERVER:-https://person.hello.coop}"   # ent#878
SENZING="${AAUTH_SENZING:-https://senzing.aauth.dev}"                 # ent#879
WHOAMI="${AAUTH_WHOAMI:-https://whoami.aauth.dev}"
SANCTIONS_NAME="${SANCTIONS_NAME:-Viktor Bout}"                       # listed on OpenSanctions (override freely)
SANCTIONS_COUNTRY="${SANCTIONS_COUNTRY:-Russia}"
DEMO_EXECUTION_ID="${DEMO_EXECUTION_ID:-manual}"                      # see the header: `manual` runs unguarded, honestly
B_IMAGES=(trinity-backend:latest trinity-scheduler:latest trinity-mcp-server:latest
          trinity-agent-base:latest redis:7-alpine timberio/vector:0.43.1-alpine alpine:3.20)
mkdir -p "$STATE"; chmod 700 "$STATE"

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
envval() { grep -E "^$2=" "$1" | tail -1 | cut -d= -f2-; }
bdocker() { DOCKER_HOST="$B_DOCKER" docker "$@"; }
bcompose() {
  DOCKER_HOST="$B_DOCKER" AAUTH_ISSUER="$(cat "$STATE/b.url")" \
    docker compose -p trinity --project-directory "$REPO" \
    -f "$REPO/docker-compose.yml" -f "$HERE/compose.b.yml" --env-file "$STATE/b.env" "$@"
}

token_for() {  # $1 = api base, $2 = admin password
  curl -fsS -X POST "$1/api/token" --data-urlencode "username=admin" --data-urlencode "password=$2" |
    python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])'
}
api() {  # api BASE TOKEN METHOD PATH [JSON]
  local base=$1 tok=$2 method=$3 path=$4 body=${5:-}
  if [ -n "$body" ]; then
    curl -sS -X "$method" -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' "$base$path" -d "$body"
  else
    curl -sS -X "$method" -H "Authorization: Bearer $tok" "$base$path"
  fi
}
setting() { api "$1" "$2" PUT "/api/settings/$3" "{\"value\": \"$4\"}" >/dev/null; }

start_front() {  # name host_port upstream
  docker rm -f "$1" >/dev/null 2>&1 || true
  docker run -d --name "$1" -p "127.0.0.1:$2:8080" -e "UPSTREAM=$3" \
    -v "$HERE/front.nginx.conf.template:/etc/nginx/templates/default.conf.template:ro" \
    nginx:alpine >/dev/null
}

start_tunnel() {  # name local_port
  local log="$STATE/tunnel-$1.log"
  if [ -f "$STATE/tunnel-$1.pid" ] && kill -0 "$(cat "$STATE/tunnel-$1.pid")" 2>/dev/null; then
    kill "$(cat "$STATE/tunnel-$1.pid")" || true
  fi
  nohup cloudflared tunnel --url "http://localhost:$2" --no-autoupdate >"$log" 2>&1 &
  echo $! >"$STATE/tunnel-$1.pid"
  local url=""
  for _ in $(seq 1 45); do
    url=$(grep -aoE 'https://[a-z0-9-]+\.trycloudflare\.com' "$log" | head -1 || true)
    [ -n "$url" ] && break; sleep 1
  done
  [ -n "$url" ] || { echo "tunnel $1 did not come up (see $log)"; exit 1; }
  echo "$url" >"$STATE/$1.url"
  # A fresh quick-tunnel name takes a few seconds to exist in public DNS. Do NOT
  # look it up locally before then: the local resolver (and anything it forwards
  # to, e.g. Tailscale MagicDNS) caches the NXDOMAIN for up to the zone's
  # negative TTL, and both backends resolve through it.
  local host=${url#https://}
  for _ in $(seq 1 60); do
    [ -n "$(dig +short @1.1.1.1 "$host" 2>/dev/null | head -1)" ] && break; sleep 2
  done
  sleep 5
  for _ in $(seq 1 60); do
    [ -n "$(dig +short "$host" 2>/dev/null | head -1)" ] && break; sleep 2
  done
  [ -n "$(dig +short "$host" 2>/dev/null | head -1)" ] || echo "  WARNING: $host does not resolve locally yet"
  echo "  tunnel $1: $url"
}

b_env() {
  [ -f "$STATE/b.env" ] && return
  local r; r() { openssl rand -hex 24; }
  {
    echo "SECRET_KEY=$(r)"
    echo "CREDENTIAL_ENCRYPTION_KEY=$(openssl rand -hex 32)"
    echo "REDIS_PASSWORD=$(r)"
    echo "REDIS_BACKEND_PASSWORD=$(r)"
    echo "INTERNAL_API_SECRET=$(r)"
    echo "AGENT_AUTH_SECRET=$(r)"
    echo "ADMIN_PASSWORD=B-$(openssl rand -hex 12)"
    # The LLM provider key is the only value shared with A: it is a credential
    # for Anthropic, not for either Trinity, and creates no trust between them.
    echo "ANTHROPIC_API_KEY=$(envval "$REPO/.env" ANTHROPIC_API_KEY)"
    echo "OTEL_ENABLED=0"
    # docker:dind creates its socket as root:2375 (the group named after the port).
    echo "DOCKER_GID=2375"
  } >"$STATE/b.env"
  chmod 600 "$STATE/b.env"
}

up_b() {
  say "Instance B: own Docker daemon"
  if ! docker ps --format '{{.Names}}' | grep -qx "$DIND"; then
    docker rm -f "$DIND" >/dev/null 2>&1 || true
    docker run -d --privileged --name "$DIND" -e DOCKER_TLS_CERTDIR= \
      -v "$REPO:$REPO" -v trinity-b-dind-data:/var/lib/docker \
      -p 127.0.0.1:23750:2375 -p 127.0.0.1:8100:8000 \
      docker:dind --host=tcp://0.0.0.0:2375 --host=unix:///var/run/docker.sock >/dev/null
  fi
  for _ in $(seq 1 60); do bdocker info >/dev/null 2>&1 && break; sleep 1; done
  for img in "${B_IMAGES[@]}"; do
    if ! bdocker image inspect "$img" >/dev/null 2>&1; then
      echo "  loading $img into B's daemon"
      docker save "$img" | bdocker load >/dev/null
    fi
  done
  b_env
  bcompose up -d --no-build backend redis vector scheduler mcp-server
  for _ in $(seq 1 90); do curl -fsS "$B_API/health" >/dev/null 2>&1 && break; sleep 2; done
  curl -fsS "$B_API/health" >/dev/null || { echo "B backend did not become healthy"; exit 1; }
  echo "  B backend healthy on $B_API"
}

ensure_b_agent() {
  local tok=$1
  if ! api "$B_API" "$tok" GET "/api/agents/$B_AGENT" | grep -q "\"name\""; then
    echo "  creating agent $B_AGENT on B"
    api "$B_API" "$tok" POST /api/agents "{\"name\": \"$B_AGENT\"}" >/dev/null
  fi
  api "$B_API" "$tok" POST "/api/agents/$B_AGENT/start" >/dev/null || true
  api "$B_API" "$tok" PUT "/api/enterprise/a2a/$B_AGENT/exposure" '{"enabled": true}' >/dev/null
}

wire() {
  local a_url b_url a_tok b_tok a_host
  a_url=$(cat "$STATE/a.url"); b_url=$(cat "$STATE/b.url"); a_host=${a_url#https://}
  a_tok=$(token_for "$A_API" "$(envval "$REPO/.env" ADMIN_PASSWORD)")
  b_tok=$(token_for "$B_API" "$(envval "$STATE/b.env" ADMIN_PASSWORD)")

  say "A: issuer $a_url, person server $PERSON_SERVER, resources $SENZING + $WHOAMI, outbound on, endpoint bravo-remote → $b_url/a2a/$B_AGENT (aauth, no credential)"
  setting "$A_API" "$a_tok" aauth_issuer "$a_url"
  setting "$A_API" "$a_tok" aauth_person_server "$PERSON_SERVER"     # ent#878 (bind.sh needs it)
  setting "$A_API" "$a_tok" aauth_resource_allowlist "$SENZING,$WHOAMI"   # ent#879 (origin equality, fail-closed)
  setting "$A_API" "$a_tok" aauth_prototype_enabled true
  setting "$A_API" "$a_tok" a2a_outbound_enabled true
  api "$A_API" "$a_tok" PUT /api/settings/a2a-endpoints \
    "{\"name\": \"bravo-remote\", \"url\": \"$b_url/a2a/$B_AGENT\", \"auth_scheme\": \"aauth\"}"; echo

  say "B: issuer $b_url (env), agent $B_AGENT exposed, allow-list aauth:$A_AGENT@$a_host"
  ensure_b_agent "$b_tok"
  local current
  current=$(api "$B_API" "$b_tok" GET "/api/enterprise/a2a/$B_AGENT/config" |
    python3 -c 'import sys,json;print(json.dumps([i for i in json.load(sys.stdin)["inbound_allowlist"] if i.startswith("aauth:")]))')
  api "$B_API" "$b_tok" POST "/api/enterprise/a2a/$B_AGENT/inbound-allowlist" \
    "{\"remove\": $current, \"add\": [\"aauth:$A_AGENT@$a_host\"]}" >/dev/null || true
  api "$B_API" "$b_tok" POST "/api/enterprise/a2a/$B_AGENT/inbound-allowlist" \
    "{\"add\": [\"aauth:$A_AGENT@$a_host\"]}" | python3 -c 'import sys,json;print("  allow-list:", json.load(sys.stdin)["inbound_allowlist"])'
}

agent_key() { docker exec "agent-$1" printenv TRINITY_MCP_API_KEY; }

call_as() {  # agent → prints status + body
  local key; key=$(agent_key "$1")
  curl -sS -o "$STATE/call-$1.json" -w '%{http_code}' -X POST \
    -H "Authorization: Bearer $key" -H 'Content-Type: application/json' \
    "$A_API/api/agents/$1/a2a/call" \
    -d "{\"endpoint\": \"bravo-remote\", \"message\": \"Reply with exactly: pong from bravo\", \"dedup_label\": \"demo-$(date +%s%N)\"}"
}

step7_bind() {   # $1 = "wait" (run: bind.sh, waits for the owner) | "assert" (rehearse: one POST, must be issued)
  say "7. Bind A/$A_AGENT to its owner at $PERSON_SERVER for $SENZING (ent#878)"
  if [ "$1" = "wait" ]; then
    "$HERE/bind.sh" "$A_AGENT" "$SENZING"
    return
  fi
  local key out code
  key=$(agent_key "$A_AGENT"); out="$STATE/bind-$A_AGENT.json"
  code=$(curl -sS -o "$out" -w '%{http_code}' -X POST "$A_API/api/agents/$A_AGENT/aauth/person-token" \
    -H "Authorization: Bearer $key" -H 'Content-Type: application/json' \
    -d "{\"resource\": \"$SENZING\", \"wait\": 0, \"trigger_source\": \"script\"}")
  python3 - "$out" "$code" <<'PY7'
import json, sys
doc = json.load(open(sys.argv[1])); code = sys.argv[2]
if code == "200" and doc.get("outcome") == "issued":
    print(f"  binding present: jti={doc['jti']} sub={doc['sub']} cached={doc.get('cached')} (no pause)")
elif code == "200" and doc.get("outcome") == "pending":
    i = doc["interaction"]
    print("  NOT bound yet — rehearse asserts, it does not wait. Bind first:  bind.sh", "(owner link:", i.get("link"), ")")
else:
    print(f"  HTTP {code}: {doc.get('detail') or doc.get('reason')} — bind first with bind.sh")
PY7
}

step8_sanctions() {   # $1 = extra senzing_check.py flags ("" | "--no-wait")
  say "8. Sanctions check at $SENZING as A/$A_AGENT (ent#879): the ladder, then a cached second call"
  local a_url a_tok; a_url=$(cat "$STATE/a.url")
  echo "  identity: aauth:$A_AGENT@${a_url#https://}  (A signs as the agent; the person server names the owner)"
  # shellcheck disable=SC2086
  python3 "$HERE/senzing_check.py" --api "$A_API" --agent "$A_AGENT" --key "$(agent_key "$A_AGENT")" \
    --resource "$SENZING" --name "$SANCTIONS_NAME" --country "$SANCTIONS_COUNTRY" \
    --execution-id "$DEMO_EXECUTION_ID" $1 || echo "  (senzing_check.py exit $?)"
  say "8b. A's audit rows for the resource calls (token kind, challenges, budget — never a token)"
  a_tok=$(token_for "$A_API" "$(envval "$REPO/.env" ADMIN_PASSWORD)")
  api "$A_API" "$a_tok" GET "/api/audit-log?limit=30" >"$STATE/audit-a.json"
  python3 - "$STATE/audit-a.json" <<'PY8'
import json, sys
rows = json.load(open(sys.argv[1])).get("entries", [])
for r in reversed(rows):
    if not str(r.get("event_action", "")).startswith("aauth_resource"):
        continue
    d = r.get("details") or {}
    print("  {ts} {act:<24} outcome={o} token_kind={tk} challenges={ch} cost={c} status={st} reason={re} exec={ex}".format(
        ts=r.get("timestamp", "")[:19], act=r.get("event_action"), o=d.get("outcome"), tk=d.get("token_kind"),
        ch=d.get("challenges"), c=d.get("budget_cost"), st=d.get("status"), re=d.get("reason"), ex=d.get("execution_id")))
PY8
}

run() {
  steps_1_to_6
  step7_bind wait
  step8_sanctions ""
}

rehearse() {   # 1–8 with no human pause: step 7 asserts the binding, step 8 does not poll a consent
  steps_1_to_6
  step7_bind assert
  step8_sanctions "--no-wait"
}

steps_1_to_6() {
  local b_url b_tok; b_url=$(cat "$STATE/b.url")
  b_tok=$(token_for "$B_API" "$(envval "$STATE/b.env" ADMIN_PASSWORD)")

  say "1. whoami.aauth.dev verifies A's $A_AGENT (a verifier that is not Trinity)"
  docker exec trinity-backend python -m services.aauth.whoami "$A_AGENT" 2>/dev/null | python3 -c '
import sys, json
out = sys.stdin.read(); d = json.loads(out[out.index("{"):])
print("  status", d["status"], "->", d["body"])'

  say "2. A/$A_AGENT → B/$B_AGENT over AAuth (allow-listed)"
  echo "  HTTP $(call_as "$A_AGENT")"; python3 -m json.tool "$STATE/call-$A_AGENT.json" | sed 's/^/  /'

  say "3. A/$A_OTHER_AGENT → B/$B_AGENT over AAuth (verified, NOT allow-listed)"
  echo "  HTTP $(call_as "$A_OTHER_AGENT")"; python3 -m json.tool "$STATE/call-$A_OTHER_AGENT.json" | sed 's/^/  /'

  say "4. Tampered request straight at B (signature over a different body)"
  docker exec -i trinity-backend python - "$A_AGENT" "$b_url" <<'PY' 2>/dev/null
import sys, json, httpx
from services.aauth import signer
agent, b = sys.argv[1], sys.argv[2]
body = json.dumps({"jsonrpc": "2.0", "id": "t", "method": "message/send",
                   "params": {"message": {"messageId": "t1", "parts": [{"kind": "text", "text": "hello"}]}}}).encode()
h = signer.sign_request(agent_name=agent, method="POST", url=f"{b}/a2a/bravo", body=body,
                        content_type="application/json")
h["Content-Type"] = "application/json"
r = httpx.post(f"{b}/a2a/bravo", content=body.replace(b"hello", b"HACKED"), headers=h, timeout=20)
print("  HTTP", r.status_code, "Signature-Error:", r.headers.get("signature-error"), "->", r.text)
PY

  say "5. Bearer call to B with B's OWN agent-scoped MCP key (dual acceptance)"
  local b_key; b_key=$(bdocker exec "agent-$B_AGENT" printenv TRINITY_MCP_API_KEY)
  curl -sS -o "$STATE/bearer.json" -w '  HTTP %{http_code}\n' -X POST "$B_API/a2a/$B_AGENT" \
    -H "Authorization: Bearer $b_key" -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","id":"b1","method":"tasks/get","params":{"id":"no-such-task"}}'
  sed 's/^/  /' "$STATE/bearer.json"; echo

  say "6. B's audit trail: who called, how it was verified, what the allow-list decided"
  api "$B_API" "$b_tok" GET "/api/audit-log?limit=30" >"$STATE/audit.json"
  python3 - "$STATE/audit.json" <<'PY2'
import json, sys
rows = json.load(open(sys.argv[1])).get("entries", [])
for r in reversed(rows):
    if not str(r.get("event_action", "")).startswith("a2a"):
        continue
    d = r.get("details") or {}
    print("  {ts} {act:<18} actor={at}:{aid}  verification={v}  allowlist={al}  exec={ex}  error={err}".format(
        ts=r.get("timestamp", "")[:19], act=r.get("event_action"), at=r.get("actor_type"),
        aid=r.get("actor_id"), v=d.get("verification"), al=d.get("allowlist"),
        ex=d.get("execution_id"), err=d.get("error")))
PY2
}

status() {
  for n in a b; do
    [ -f "$STATE/$n.url" ] && echo "tunnel $n: $(cat "$STATE/$n.url")"
  done
  docker ps --format '{{.Names}}\t{{.Status}}' | grep -E "aauth-front|$DIND" || true
  bdocker ps --format '  B: {{.Names}}\t{{.Status}}' 2>/dev/null || true
}

down() {
  say "Tearing down the demo"
  for n in a b; do
    [ -f "$STATE/tunnel-$n.pid" ] && kill "$(cat "$STATE/tunnel-$n.pid")" 2>/dev/null || true
  done
  docker rm -f aauth-front-a aauth-front-b "$DIND" >/dev/null 2>&1 || true
  local a_tok; a_tok=$(token_for "$A_API" "$(envval "$REPO/.env" ADMIN_PASSWORD)" 2>/dev/null) || return 0
  setting "$A_API" "$a_tok" aauth_prototype_enabled false
  setting "$A_API" "$a_tok" a2a_outbound_enabled false
  api "$A_API" "$a_tok" DELETE /api/settings/a2a-endpoints/bravo-remote >/dev/null || true
  api "$A_API" "$a_tok" DELETE /api/settings/aauth_resource_allowlist >/dev/null || true   # ent#879
  api "$A_API" "$a_tok" DELETE /api/settings/aauth_person_server >/dev/null || true        # ent#878
  echo "  A: AAuth + outbound switched off, demo endpoint, resource allow-list and person server removed"
  echo "  (B's data volume trinity-b-dind-data kept; 'docker volume rm trinity-b-dind-data' resets B)"
}

case "${1:-}" in
  up)
    start_front aauth-front-a 8001 host.docker.internal:8000
    start_front aauth-front-b 8101 host.docker.internal:8100
    say "Tunnels"
    start_tunnel a 8001
    start_tunnel b 8101
    up_b
    wire
    ;;
  rewire)
    echo "  NOTE (ent#878): a new issuer host is a NEW agent to the person server — rebind with bind.sh afterwards"
    start_tunnel a 8001
    start_tunnel b 8101
    bcompose up -d --no-build backend   # B's issuer is env: recreate to pick up the new URL
    for _ in $(seq 1 60); do curl -fsS "$B_API/health" >/dev/null 2>&1 && break; sleep 2; done
    wire
    ;;
  wire) wire ;;
  run) run ;;
  rehearse) rehearse ;;
  status) status ;;
  down) down ;;
  *) sed -n '2,44p' "$0"; exit 2 ;;
esac
