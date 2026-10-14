#!/usr/bin/env bash
# ent#878 — bind ONE agent to its owner at the person server (AAuth person token), live.
#
# Usage:
#   scripts/dev/aauth_prototype/bind.sh <agent> <resource>
#     <agent>     an agent on the A dev stack (container agent-<name> must be running)
#     <resource>  what the person token is for: https://<host> (a server identifier)
#
# Preconditions: `demo.sh up` (flag on, issuer set) — `up`/`wire` also set
# `aauth_person_server` (default https://person.hello.coop, override with
# AAUTH_PERSON_SERVER). The route refuses with 409 person_server_unconfigured otherwise.
#
# What it does: POSTs /api/agents/<agent>/aauth/person-token with the agent's OWN
# MCP key (agent_key(), the demo.sh helper — an admin token is refused with 403
# aauth_agent_only), prints the link + code for the OWNER to open, polls with the
# pending handle honouring retry_after, and exits 0 on `issued` printing
# jti/aud/sub/exp. The token itself never appears: the response has no field for it.
#
# Do NOT `demo.sh rewire` after binding: a new tunnel host is a new issuer, so the
# person server sees a new agent (aauth:<agent>@<new-host>) and the binding must
# be redone with this script.
set -euo pipefail

A_API="${A_API:-http://localhost:8000}"
AGENT="${1:-}"; RESOURCE="${2:-}"
[ -n "$AGENT" ] && [ -n "$RESOURCE" ] || { sed -n '2,20p' "$0"; exit 2; }
MAX_POLLS="${AAUTH_BIND_MAX_POLLS:-60}"

agent_key() { docker exec "agent-$1" printenv TRINITY_MCP_API_KEY; }

OUT=$(mktemp); trap 'rm -f "$OUT"' EXIT
KEY=$(agent_key "$AGENT")

call() {  # call JSON → prints the HTTP status; the body lands in $OUT
  curl -sS -o "$OUT" -w '%{http_code}' -X POST "$A_API/api/agents/$AGENT/aauth/person-token" \
    -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d "$1"
}
field() {  # field dotted.path → the value from $OUT, or ""
  python3 - "$OUT" "$1" <<'PY'
import json, sys
v = json.load(open(sys.argv[1]))
for k in sys.argv[2].split("."):
    v = v.get(k) if isinstance(v, dict) else None
print("" if v is None else v)
PY
}

echo "== $AGENT → person token for $RESOURCE (via $A_API)"
code=$(call "{\"resource\": \"$RESOURCE\", \"wait\": 5, \"trigger_source\": \"script\"}")
handle=""
for _ in $(seq 0 "$MAX_POLLS"); do
  if [ "$code" != "200" ]; then
    echo "HTTP $code: $(field detail.reason) — $(field detail.message)"
    [ "$code" = "429" ] && echo "  (a local rate budget; wait a minute and run again)"
    exit 1
  fi
  case "$(field outcome)" in
    issued)
      echo "issued: jti=$(field jti) aud=$(field aud) sub=$(field sub) exp=$(field exp)" \
           "cached=$(field cached) reissued_for_key=$(field reissued_for_key)"
      exit 0 ;;
    pending)
      new=$(field interaction.pending_handle)
      if [ "$new" != "$handle" ]; then
        handle=$new
        echo
        echo "OWNER — open this link, privately (whoever approves first becomes the person this agent acts for):"
        echo "  $(field interaction.link)"
        echo "  url $(field interaction.url) · code $(field interaction.code) · expires_at $(field interaction.expires_at)"
        echo
      fi
      ra=$(field interaction.retry_after)
      echo "  $(field interaction.status) — polling again in ${ra}s"
      sleep "$ra"
      code=$(call "{\"resource\": \"$RESOURCE\", \"pending_handle\": \"$handle\", \"wait\": 15, \"trigger_source\": \"script\"}")
      ;;
    refused)
      echo "refused: $(field reason) — $(field message)"
      echo "  next: $(field next_step)"
      exit 1 ;;
    *)
      echo "unexpected answer:"; cat "$OUT"; echo; exit 1 ;;
  esac
done
echo "gave up after $MAX_POLLS polls (handle $handle may still be pending; run again with the same agent + resource)"
exit 1
