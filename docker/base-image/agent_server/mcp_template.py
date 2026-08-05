"""Render `.mcp.json.template` into `.mcp.json` at container startup (#2007).

`docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md` tells template authors that Trinity
replaces `${VAR}` in `.mcp.json.template` with values from the credential
store. For a `github:` template nothing did: the backend's renderer
(`template_service.generate_credential_files`) reads `.mcp.json` — not the
`.template` — and is `local:`-only anyway, while a `github:` agent's files only
exist **after** `startup.sh` clones the repo *inside* the container. So the sole
writer of `~/.mcp.json` on a `github:` agent was
`inject_trinity_mcp_if_configured()`, and every declared server was silently
absent. A freshly-seeded Cornelius shipped three servers and ran with none.

This module is that missing renderer, and it runs in-container because that is
the only place the files exist.

## What it substitutes, and why only there

`${VAR}` is expanded **inside `env` blocks only**. That is not a simplification
— it is the only form `mcp_validator` accepts. A `${VAR}` in `args` is rejected
outright (a bare `$` is a shell metacharacter and args are never ref-stripped),
and `command` must be a literal allowlist entry, so substituting there is the
RCE-by-config class #590 closed: a credential value becoming the executed
command. The published guide's own example only shows `env` placeholders.

## Refuse, never blank

A `${VAR}` with no value is **not** blanked to `""`. Blanking is what makes a
broken config look healthy — #1929's defect, and the reason the deploy path's
laundering (#2006) hid a rejected config. A server whose placeholders cannot be
resolved is withheld with a named reason on stdout (captured by Vector), and
the rest are installed.

## Merge, never clobber

Servers are only **added** when absent from `~/.mcp.json`. An entry already
there — the `trinity` entry this agent's server injects, or one an owner edited
— is left exactly as it is. That makes this idempotent across restarts and
order-independent with respect to `inject_trinity_mcp_if_configured()`, which
may create the file before or after this runs.

Every candidate is validated **individually** through the vendored
`mcp_validator` (byte-identical to the backend copy, Invariant #5) before it is
allowed in, so one bad server does not cost the agent the good ones.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:  # package import (normal) …
    from .mcp_validator import McpValidationError, validate_mcp_config
except ImportError:  # pragma: no cover — … or standalone, for a script run
    from mcp_validator import McpValidationError, validate_mcp_config  # type: ignore

AGENT_HOME = Path("/home/developer")
TEMPLATE_FILE = AGENT_HOME / ".mcp.json.template"
CONFIG_FILE = AGENT_HOME / ".mcp.json"
ENV_FILE = AGENT_HOME / ".env"

# `${VAR}` and `${VAR:-default}`. The default form is honoured because template
# authors use it for optional paths; an unresolvable ref is still refused
# rather than blanked.
_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Both files are agent-writable; cap the reads so a runaway file cannot be
# loaded whole at every container start.
_MAX_BYTES = 1024 * 1024


def _log(message: str) -> None:
    """stdout, so it lands in the container log Vector captures."""
    print(f"[mcp-template] {message}", flush=True)


def parse_env_file(path: Path = ENV_FILE) -> Dict[str, str]:
    """Read `.env` into a dict. Never raises.

    Mirrors the writer in `routers/credentials.py` (`KEY="value"`, embedded `"`
    backslash-escaped) while tolerating the hand-written shapes: `export KEY=v`,
    single quotes, bare values, comments, CRLF.
    """
    try:
        if not path.is_file() or path.stat().st_size > _MAX_BYTES:
            return {}
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}

    out: Dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not _ENV_KEY_RE.match(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            inner = value[1:-1]
            value = inner.replace('\\"', '"') if value[0] == '"' else inner
        out[key] = value
    return out


def _resolve_refs(value: str, values: Dict[str, str]) -> Tuple[Optional[str], Optional[str]]:
    """Expand every `${VAR}` in `value`. Returns `(resolved, None)` or `(None, reason)`.

    An empty stored value counts as unset: a credential the operator has not
    filled in yet is exactly the case that must not silently produce a server
    configured with `""`.
    """
    missing: List[str] = []

    def sub(match: re.Match) -> str:
        name, default = match.group(1), match.group(2)
        resolved = values.get(name) or ""
        if resolved:
            return resolved
        if default is not None:
            return default
        missing.append(name)
        return ""

    expanded = _REF_RE.sub(sub, value)
    if missing:
        return None, f"unresolved placeholder(s): {', '.join(sorted(set(missing)))}"
    return expanded, None


def render_server(config: dict, values: Dict[str, str]) -> Tuple[Optional[dict], Optional[str]]:
    """Render ONE server entry. Returns `(rendered, None)` or `(None, reason)`.

    Substitution is confined to `env`. A `${...}` anywhere else is reported —
    not expanded — because the validator would reject the expansion anyway and
    a silent expansion into `command` is the #590 class.
    """
    if not isinstance(config, dict):
        return None, "entry is not an object"

    rendered = json.loads(json.dumps(config))  # deep copy, JSON-only by construction

    env = rendered.get("env")
    if env is not None:
        if not isinstance(env, dict):
            return None, "'env' is not an object"
        for key, value in list(env.items()):
            if not isinstance(value, str):
                continue
            resolved, reason = _resolve_refs(value, values)
            if reason:
                return None, reason
            env[key] = resolved

    for field in ("command", "url"):
        value = rendered.get(field)
        if isinstance(value, str) and _REF_RE.search(value):
            return None, (
                f"'{field}' contains a ${{VAR}} placeholder; Trinity substitutes "
                f"only inside 'env' (a credential value must never become the "
                f"executed command)"
            )
    args = rendered.get("args")
    if isinstance(args, list):
        for arg in args:
            if isinstance(arg, str) and _REF_RE.search(arg):
                return None, (
                    "'args' contains a ${VAR} placeholder; Trinity substitutes "
                    "only inside 'env', and the validator rejects placeholders "
                    "in args"
                )

    try:
        validate_mcp_config(json.dumps({"mcpServers": {"_probe": rendered}}))
    except McpValidationError as e:
        return None, str(e)
    return rendered, None


def render(
    template_file: Path = TEMPLATE_FILE,
    config_file: Path = CONFIG_FILE,
    env_file: Path = ENV_FILE,
) -> Dict[str, object]:
    """Render the template and merge the result into `.mcp.json`.

    Never raises: this runs on the container startup path, where an exception
    would cost the agent its boot over a malformed optional file.
    """
    result: Dict[str, object] = {"added": [], "skipped": {}, "status": "ok"}

    try:
        if not template_file.is_file():
            result["status"] = "no_template"
            return result
        if template_file.stat().st_size > _MAX_BYTES:
            _log(f"{template_file.name} is too large; ignoring")
            result["status"] = "template_too_large"
            return result
        template_raw = template_file.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        _log(f"could not read {template_file}: {e}")
        result["status"] = "unreadable"
        return result

    try:
        template = json.loads(template_raw)
    except ValueError as e:
        _log(f"{template_file.name} is not valid JSON ({e}); no servers rendered")
        result["status"] = "invalid_json"
        return result

    declared = template.get("mcpServers") if isinstance(template, dict) else None
    if not isinstance(declared, dict) or not declared:
        result["status"] = "no_servers"
        return result

    existing: dict = {}
    if config_file.is_file():
        try:
            loaded = json.loads(config_file.read_text(encoding="utf-8", errors="replace"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, ValueError) as e:
            # A .mcp.json we cannot parse is not ours to repair — and merging
            # into it blind would destroy whatever is there.
            _log(f"existing .mcp.json is unreadable/invalid ({e}); leaving it alone")
            result["status"] = "existing_unreadable"
            return result

    servers = existing.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        _log("existing .mcp.json has a non-object 'mcpServers'; leaving it alone")
        result["status"] = "existing_unreadable"
        return result

    values = parse_env_file(env_file)
    added: List[str] = []
    skipped: Dict[str, str] = {}

    for name, config in declared.items():
        if name in servers:
            continue  # never clobber an entry that is already installed
        rendered, reason = render_server(config, values)
        if reason:
            skipped[name] = reason
            continue
        servers[name] = rendered
        added.append(name)

    for name, reason in sorted(skipped.items()):
        _log(f"withheld MCP server '{name}': {reason}")

    if added:
        try:
            tmp = config_file.with_name(config_file.name + ".tmp")
            tmp.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, config_file)  # atomic: never a half-written config
        except OSError as e:
            _log(f"could not write {config_file}: {e}")
            result["status"] = "write_failed"
            return result
        _log(f"rendered MCP server(s) into .mcp.json: {', '.join(added)}")

    result["added"] = added
    result["skipped"] = skipped
    return result


def main() -> int:
    render()
    return 0  # startup must continue regardless


if __name__ == "__main__":  # pragma: no cover — exercised via startup.sh
    sys.exit(main())
