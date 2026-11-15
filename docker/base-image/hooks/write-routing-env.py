#!/usr/bin/env python3
"""Pin the traffic-routing env names in Claude Code's managed settings to this
container's own values (trinity-enterprise#1001).

Claude Code applies the `env` block of every settings file it reads, and two of
them are agent-writable: `~/.claude/settings.json` and the workspace's
`.claude/settings.json`. Either one could set `ANTHROPIC_BASE_URL`, a proxy or a
TLS trust override, and the CLI would send its traffic, with the API key or
OAuth token on it, wherever that points. Policy `env` from
`/etc/claude-code/managed-settings.d/` beats user and project settings and the
process env, so this drop-in pins each name to the value the container was
started with, or to "" when it has none. A proxy the operator set on the
container keeps working; no file the agent can write moves it.

Run twice:
- at image build under `env -i`, which ships the all-empty default, so the
  names stay pinned even if the start-time write below never happens;
- by startup.sh through `sudo -E` before the agent server starts, so the values
  are the container baseline (Docker `Config.Env`), the same values the agent
  server captures as `execution_env.INITIAL_ENV`.

Its own drop-in file: the CLI validates each `managed-settings.d` file on its
own and voids only the file that fails, so this one can never take the
GUARD-002 registration or the skill gate with it.
"""
import json
import os
import sys

DROP_IN = "/etc/claude-code/managed-settings.d/60-routing-env.json"

# `execution_env.routing_pins("claude")`. This script runs as root and must
# not import code from the agent-writable `/app` tree, so the set is copied
# here and kept equal by tests/unit/test_ent1001_endpoint_env.py.
ROUTING_NAMES = (
    "ALL_PROXY",
    "ANTHROPIC_AWS_BASE_URL",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_BEDROCK_MANTLE_BASE_URL",
    "ANTHROPIC_FOUNDRY_BASE_URL",
    "ANTHROPIC_FOUNDRY_RESOURCE",
    "ANTHROPIC_GOOGLE_CLOUD_BASE_URL",
    "ANTHROPIC_VERTEX_BASE_URL",
    "CLAUDE_CODE_API_BASE_URL",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NODE_EXTRA_CA_CERTS",
    "NODE_TLS_REJECT_UNAUTHORIZED",
    "NO_PROXY",
    "all_proxy",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)


def pins(environ) -> dict:
    """``{"env": {name: baseline value or ""}}`` for every routing name."""
    return {"env": {name: environ.get(name, "") for name in ROUTING_NAMES}}


def write(path: str, content: dict) -> None:
    """Atomic root-owned 0444 write: a temp file in the same directory, then
    ``os.replace``, so the CLI never reads a half-written drop-in."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(content, f, indent=2, sort_keys=True)
        f.write("\n")
    os.chmod(tmp, 0o444)
    os.replace(tmp, path)


def main() -> int:
    try:
        write(DROP_IN, pins(os.environ))
    except OSError as e:
        print(f"routing env: cannot write {DROP_IN}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
