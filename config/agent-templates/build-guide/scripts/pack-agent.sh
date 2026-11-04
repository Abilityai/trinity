#!/usr/bin/env bash
# Pack an agent directory for mcp__trinity__deploy_local_agent: write the integrity
# manifest the platform verifies (#2060), tar with the same excludes, print base64.
# Usage: scripts/pack-agent.sh <agent-dir>   (never packs a .env)
set -euo pipefail
SRC="${1:?usage: pack-agent.sh <agent-dir>}"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
NAME="$(basename "$SRC")"
cp -R "$SRC" "$WORK/$NAME"
cd "$WORK/$NAME"
rm -f .env
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
base64 < agent.tar.gz | tr -d '\n'
