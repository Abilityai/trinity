"""Trinity MCP → OpenCode ``mcp:`` config mapping (Abilityai/lilu#257 step 2).

OpenCode does not read Claude's ``.mcp.json``. It reads an ``mcp`` object in
``opencode.json`` with a *different* shape, verified against the published
config schema for the pinned v1.18.32 (``https://opencode.ai/config.json``,
``McpLocalConfig`` / ``McpRemoteConfig``) rather than assumed::

    "mcp": {
      "trinity":  {"type": "remote", "url": "http://mcp-server:8080/mcp",
                   "headers": {"Authorization": "Bearer …"}, "enabled": true},
      "context7": {"type": "local", "command": ["npx", "-y", "@upstash/context7-mcp@latest"],
                   "environment": {…}, "enabled": true}
    }

Source of truth stays ``~/.mcp.json``
-------------------------------------
Trinity already writes every agent's MCP servers there — ``trinity_mcp``'s
startup injection, the template's ``mcp_servers``, and the credential-injection
path (``POST /api/agents/{name}/credentials/inject``, content-validated by
``mcp_validator``). Giving OpenCode a *second* persistent store would mean two
files that can disagree, and a re-injected Trinity key that only reaches one of
them. So nothing new is persisted: ``.mcp.json`` is read and TRANSLATED into
each turn's private config, next to the provider block and for the same reason
(#1999 — the agent's ``.env`` is re-read at every spawn, so a rotated key or a
newly injected server takes effect on the next turn with no restart).

Shape mapping
-------------
======================================  ==========================================
``.mcp.json`` entry                      OpenCode entry
======================================  ==========================================
``{"type":"http"|"sse"|…, "url", "headers"}``  ``{"type":"remote","url","headers"}``
``{"command","args","env"}``                   ``{"type":"local","command":[cmd,*args],"environment"}``
``{"enabled": false, …}``                      ``{"enabled": false}`` (OpenCode's disabled shape)
======================================  ==========================================

``url`` decides remote even when ``type`` is absent, because the Claude shape
makes ``type`` optional for stdio servers and Trinity's own template omits it.
An entry that is neither (no ``url``, no ``command``) is **skipped with a named
warning** — never emitted half-formed, which OpenCode would reject at config
parse time and take the whole turn down with it, MCP or not.

Precedence: an operator-declared ``mcp.<name>`` in the shared config WINS. The
env/`.mcp.json` side is a default, exactly as ``opencode_provider`` treats
``provider.ollama``.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Dict, Mapping, Optional

logger = logging.getLogger(__name__)

_AGENT_HOME = "/home/developer"

#: Override for tests and for a non-default agent home.
ENV_MCP_CONFIG_PATH = "TRINITY_MCP_CONFIG_PATH"

#: Transports the Claude shape uses for a network server. Anything with a
#: ``url`` maps to OpenCode ``remote`` regardless — this set exists only to
#: make the intent readable and to spot a truly unknown type in the logs.
_REMOTE_TYPES = frozenset({"http", "sse", "http-stream", "streamable-http", "remote"})


def mcp_config_path() -> str:
    """Path to the agent's ``.mcp.json``."""
    return os.environ.get(ENV_MCP_CONFIG_PATH) or os.path.join(_AGENT_HOME, ".mcp.json")


def load_mcp_servers(path: Optional[str] = None) -> Dict[str, dict]:
    """Read ``mcpServers`` out of ``.mcp.json``.

    Returns ``{}`` — never raises — when the file is missing, unreadable or
    malformed. A broken MCP config must degrade to "no MCP tools this turn",
    not to a failed turn: the agent can still answer, and the reason is in the
    log. (Graceful degradation, quality bar 5.)
    """
    target = path or mcp_config_path()
    try:
        with open(target, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        logger.info("[OpenCode] no %s — turn runs without MCP servers", target)
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("[OpenCode] %s unreadable (%s) — turn runs without MCP", target, exc)
        return {}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        logger.warning("[OpenCode] %s has no usable `mcpServers` object", target)
        return {}
    return servers


def _map_one(name: str, entry: Mapping) -> Optional[dict]:
    """Translate a single ``.mcp.json`` server. ``None`` ⇒ skip (logged)."""
    if not isinstance(entry, Mapping):
        logger.warning("[OpenCode] MCP server %r is not an object — skipped", name)
        return None

    # An explicitly disabled server is carried across as OpenCode's
    # enabled-only shape rather than dropped, so `enabled: true` later in the
    # shared config is the only thing needed to turn it back on.
    if entry.get("enabled") is False:
        return {"enabled": False}

    url = entry.get("url")
    if url:
        declared = str(entry.get("type") or "").lower()
        if declared and declared not in _REMOTE_TYPES:
            logger.info(
                "[OpenCode] MCP server %r declares type=%r; mapped as `remote` on its url",
                name, declared,
            )
        mapped: dict = {"type": "remote", "url": str(url), "enabled": True}
        headers = entry.get("headers")
        if isinstance(headers, Mapping) and headers:
            mapped["headers"] = {str(k): str(v) for k, v in headers.items()}
        return mapped

    command = entry.get("command")
    if command:
        argv = [str(command)]
        args = entry.get("args")
        if isinstance(args, (list, tuple)):
            argv.extend(str(a) for a in args)
        mapped = {"type": "local", "command": argv, "enabled": True}
        env = entry.get("env")
        if isinstance(env, Mapping) and env:
            # OpenCode calls it `environment`; Claude calls it `env`.
            mapped["environment"] = {str(k): str(v) for k, v in env.items()}
        return mapped

    logger.warning(
        "[OpenCode] MCP server %r has neither `url` nor `command` — skipped "
        "(an incomplete entry would fail OpenCode's config parse and take the "
        "whole turn down)", name,
    )
    return None


def map_mcp_servers(servers: Mapping) -> Dict[str, dict]:
    """Translate a whole ``mcpServers`` mapping. Pure; skips what it cannot map."""
    mapped: Dict[str, dict] = {}
    for name, entry in (servers or {}).items():
        one = _map_one(str(name), entry)
        if one is not None:
            mapped[str(name)] = one
    return mapped


def apply_mcp_config(
    config: Dict[str, object], servers: Optional[Mapping] = None
) -> Dict[str, object]:
    """Merge the translated servers into ``config['mcp']`` in place.

    ``servers`` defaults to the agent's ``.mcp.json``. An entry already present
    in ``config['mcp']`` (i.e. declared by the operator in the shared
    ``opencode.json``) is left untouched.
    """
    translated = map_mcp_servers(
        servers if servers is not None else load_mcp_servers()
    )
    if not translated:
        return config

    existing = config.get("mcp")
    merged: Dict[str, object] = dict(existing) if isinstance(existing, dict) else {}
    added = []
    for name, entry in translated.items():
        if name in merged:
            logger.info("[OpenCode] MCP server %r declared in config — kept as-is", name)
            continue
        merged[name] = entry
        added.append(name)
    config["mcp"] = merged
    if added:
        logger.info("[OpenCode] wired %d MCP server(s): %s", len(added), ", ".join(sorted(added)))
    return config
