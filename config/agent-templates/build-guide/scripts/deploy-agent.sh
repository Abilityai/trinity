#!/usr/bin/env bash
# Create an agent on this Trinity from a folder: write the integrity manifest the platform
# verifies (#2060), pack the folder, and post it to /api/agents/deploy-local with this
# agent's own key. The archive never passes through a chat or a tool call, where its bytes
# get altered on the way. Prints the platform's JSON answer.
# Usage: scripts/deploy-agent.sh <agent-dir>   (never packs a .env)
set -euo pipefail
SRC="${1:?usage: deploy-agent.sh <agent-dir>}"
: "${TRINITY_BACKEND_URL:?TRINITY_BACKEND_URL is not set}"
: "${TRINITY_MCP_API_KEY:?TRINITY_MCP_API_KEY is not set}"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
NAME="$(basename "$SRC")"
cp -R "$SRC" "$WORK/$NAME"
cd "$WORK/$NAME"
rm -f .env .trinity-manifest.json
python3 - <<'PY'
import hashlib, json, os
ex = {'.git', 'node_modules', '__pycache__', '.venv'}
es = []
for dp, dn, fn in os.walk('.'):
    dn[:] = [d for d in dn if d not in ex]
    for d in list(dn):
        p = os.path.join(dp, d)
        if os.path.islink(p):
            es.append({'path': os.path.relpath(p, '.'), 'link_target': os.readlink(p)}); dn.remove(d)
    for f in fn:
        p = os.path.join(dp, f); r = os.path.relpath(p, '.')
        if r == '.trinity-manifest.json' or f.startswith('._'):
            continue
        if os.path.islink(p):
            es.append({'path': r, 'link_target': os.readlink(p)})
        elif os.path.isfile(p):
            es.append({'path': r, 'sha256': hashlib.sha256(open(p, 'rb').read()).hexdigest()})
json.dump(es, open('.trinity-manifest.json', 'w'))
PY
cd "$WORK"
COPYFILE_DISABLE=1 tar -czf agent.tar.gz --exclude='.git' --exclude='node_modules' \
  --exclude='__pycache__' --exclude='.venv' -C "$WORK" "$NAME"
python3 - "$WORK/agent.tar.gz" <<'PY'
import base64, json, sys
json.dump({"archive": base64.b64encode(open(sys.argv[1], "rb").read()).decode("ascii"), "require_manifest": True},
          open(sys.argv[1] + ".json", "w"))
PY
curl -sS -X POST "${TRINITY_BACKEND_URL%/}/api/agents/deploy-local" \
  -H "Authorization: Bearer $TRINITY_MCP_API_KEY" -H "Content-Type: application/json" \
  --data-binary @"$WORK/agent.tar.gz.json" -w "\n"
