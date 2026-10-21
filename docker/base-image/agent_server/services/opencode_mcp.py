"""``.mcp.json`` → OpenCode ``mcp:`` translation, by env-var NAME (#2981).

OpenCode does not read Claude's ``.mcp.json``. It reads an ``mcp`` object in
``opencode.json`` with a different shape (``McpLocalConfig`` /
``McpRemoteConfig`` in the published v1.18.32 config schema)::

    "mcp": {
      "trinity":  {"type": "remote", "url": "http://mcp-server:8080/mcp",
                   "headers": {"Authorization": "Bearer {env:TRINITY_MCP_API_KEY}",
                               "X-Trinity-Execution-Id": "{env:TRINITY_EXECUTION_ID}"},
                   "enabled": true},
      "context7": {"type": "local", "command": ["npx", "-y", "@upstash/context7-mcp@latest"],
                   "environment": {"CONTEXT7_API_KEY": "{env:CONTEXT7_API_KEY}"},
                   "enabled": true}
    }

Source of truth stays ``~/.mcp.json``
-------------------------------------
Trinity already writes every agent's MCP servers there — ``trinity_mcp``'s
startup injection, the template's ``mcp_servers`` and the credential-injection
path. A second persistent store would be two files that can disagree, so
nothing new is persisted: ``.mcp.json`` is read and TRANSLATED into each
turn's private config (#1999 — a rotated key or a newly injected server takes
effect on the next turn, no restart).

Shape mapping
-------------
======================================  ==========================================
``.mcp.json`` entry                      OpenCode entry
======================================  ==========================================
``{"type":"http"|"sse"|…, "url", "headers"}``  ``{"type":"remote","url","headers"}``
``{"command","args","env"}``                   ``{"type":"local","command":[cmd,*args],"environment"}``
``{"enabled": false, …}``                      ``{"enabled": false}``
======================================  ==========================================

``url`` decides remote even when ``type`` is absent. An entry with neither
``url`` nor ``command`` is skipped with a named warning — never emitted
half-formed, which OpenCode would reject at config parse and take the whole
turn down with it. An operator-declared ``mcp.<name>`` already in the shared
config WINS; the ``.mcp.json`` side is a default.

The secret rule (decision 6): names, never values
-------------------------------------------------
The rendered config carries env-var NAMES. ``trinity_mcp`` writes the Trinity
server's ``Authorization`` into ``.mcp.json`` as the LITERAL ``Bearer <key>``
(Claude Code has no reference syntax for headers) and the execution-id header
as the shell placeholder ``${TRINITY_EXECUTION_ID:-manual}``. Both are
rewritten here, in this order, for every header value and every local
server's ``env`` value:

(a) every ``${VAR}`` / ``${VAR:-default}`` placeholder → OpenCode's
    ``{env:VAR}`` reference. The substitution syntax is VERIFIED against the
    pinned source, not assumed: ``packages/opencode/src/config/variable.ts``
    (v1.18.32) replaces ``/\\{env:([^}]+)\\}/g`` with
    ``(input.env?.[varName] ?? process.env[varName]) || ""`` over the config
    TEXT before it is parsed. **There is no default-value form** — a missing
    var substitutes to the empty string. So a placeholder's default is
    honoured by the RUNTIME instead: :func:`render_mcp_servers` returns the
    ``{VAR: default}`` map and ``opencode_runtime`` exports each one into the
    subprocess env when the var is unset or empty there. ``TRINITY_EXECUTION_ID``
    itself needs no default: it IS the execution tag the agent server sets on
    every spawn (``EXECUTION_TAG_NAME``).
(b) a value that contains the CURRENT value of any env var whose name
    matches ``(KEY|TOKEN|SECRET|PASSWORD)`` is rewritten to reference that
    var by name — a reverse lookup over the whole environment, never one
    hard-coded name, so a template MCP server carrying its own literal key is
    covered exactly like the Trinity server (``Bearer {env:TRINITY_MCP_API_KEY}``).
(c) a value that STILL looks secret-shaped after (a) and (b) and matches no
    env var is DROPPED with one warning naming the server and the header —
    it is never written literally. A dropped header can only mean a key that
    is not in the agent's environment, which OpenCode could not have
    substituted anyway.

The platform-prompt half (the ``mcp__trinity__<tool>`` → ``trinity_<tool>``
rewrite OpenCode's ``<server>_<tool>`` naming needs) is NOT here; it belongs
to the backend's prompt adaptation (checkpoint G of #2981).
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

_AGENT_HOME = "/home/developer"

#: Override for tests and for a non-default agent home.
ENV_MCP_CONFIG_PATH = "TRINITY_MCP_CONFIG_PATH"

#: Transports the Claude shape uses for a network server. Anything with a
#: ``url`` maps to OpenCode ``remote`` regardless — this set only makes the
#: intent readable and spots a truly unknown type in the logs.
_REMOTE_TYPES = frozenset({"http", "sse", "http-stream", "streamable-http", "remote"})

#: ``${VAR}`` / ``${VAR:-default}`` — the Claude Code / Gemini placeholder
#: shape that ``trinity_mcp`` and ``mcp_validator`` write.
_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

#: OpenCode's own reference, as it must appear in the rendered config
#: (variable.ts, v1.18.32). Used to strip references out of a value before
#: asking whether a literal secret is left over.
_ENV_REF_RE = re.compile(r"\{env:[^}]+\}")

#: Env vars whose VALUE is looked up inside header/env values (rule b).
_SECRET_ENV_NAME_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD)", re.IGNORECASE)

#: A value shorter than this is never treated as a secret for the reverse
#: lookup — `TOKEN_COUNT=5` must not rewrite every "5" in a header.
_MIN_SECRET_VALUE_LEN = 8

#: Header / env-var names that carry credentials by convention (rule c): any
#: literal residue in such a field is a secret written in the clear.
_SECRET_FIELD_RE = re.compile(
    r"(authorization|api[-_]?key|token|secret|password|credential)", re.IGNORECASE
)

#: Scheme words that may legitimately stay literal in a credential field.
_SCHEME_WORDS = frozenset({"bearer", "basic", "token"})

#: A long, space-free opaque literal in a NON-credential field (``sk-…``,
#: ``ghp_…``, a JWT segment). Dots and slashes are excluded on purpose so a
#: URL or a path in a header is not mistaken for a key.
_OPAQUE_LITERAL_RE = re.compile(r"[A-Za-z0-9_\-]{24,}")


@dataclass
class RenderedMcp:
    """Result of :func:`render_mcp_servers`."""

    #: ``name → OpenCode entry``, secrets referenced by env-var name.
    servers: Dict[str, dict] = field(default_factory=dict)
    #: ``VAR → default`` for every ``${VAR:-default}`` placeholder seen. The
    #: runtime exports these into the subprocess env when the var is unset
    #: there, because OpenCode's ``{env:}`` has no default form.
    env_defaults: Dict[str, str] = field(default_factory=dict)
    #: ``(server, field)`` pairs dropped under rule (c), for the caller's log.
    dropped: Tuple[Tuple[str, str], ...] = ()


def mcp_config_path() -> str:
    """Path to the agent's ``.mcp.json``."""
    return os.environ.get(ENV_MCP_CONFIG_PATH) or os.path.join(_AGENT_HOME, ".mcp.json")


def load_mcp_servers(path: Optional[str] = None) -> Dict[str, dict]:
    """Read ``mcpServers`` out of ``.mcp.json``.

    Returns ``{}`` — never raises — when the file is missing, unreadable or
    malformed. A broken MCP config must degrade to "no MCP tools this turn",
    not to a failed turn: the agent can still answer, and the reason is in
    the log (graceful degradation, quality bar 5).
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


# ---------------------------------------------------------------------------
# the secret rule
# ---------------------------------------------------------------------------

def _secret_env_items(environ: Mapping[str, str]):
    """``(name, value)`` for every secret-named, non-trivial env var, longest
    value first so a key that contains another key is rewritten whole."""
    items = [
        (name, value)
        for name, value in environ.items()
        if isinstance(value, str)
        and len(value) >= _MIN_SECRET_VALUE_LEN
        and _SECRET_ENV_NAME_RE.search(name)
    ]
    items.sort(key=lambda kv: len(kv[1]), reverse=True)
    return items


def _has_literal_secret(field_name: str, value: str) -> bool:
    """Rule (c): is there a secret still written in the clear in ``value``?"""
    residue = _ENV_REF_RE.sub("", value)
    if _SECRET_FIELD_RE.search(field_name):
        words = [w for w in residue.split() if w.lower() not in _SCHEME_WORDS]
        return bool("".join(words).strip())
    return bool(_OPAQUE_LITERAL_RE.search(residue))


def render_secret_value(
    value: str,
    *,
    server: str,
    field_name: str,
    environ: Mapping[str, str],
    env_defaults: Dict[str, str],
) -> Optional[str]:
    """Apply rules (a), (b), (c) to one header / env value.

    Returns the rendered value, or ``None`` when the value had to be dropped
    (rule c) — the caller omits the field and the warning has been logged.
    """
    # (a) ${VAR} / ${VAR:-default} → {env:VAR}; the default is the runtime's
    #     job, since the pinned OpenCode has no default form.
    def _placeholder(match: "re.Match[str]") -> str:
        name, default = match.group(1), match.group(2)
        if default is not None:
            env_defaults.setdefault(name, default)
        return "{env:%s}" % name

    rendered = _PLACEHOLDER_RE.sub(_placeholder, value)

    # (b) reverse lookup: a literal equal to a secret-named env var's value
    #     becomes a reference to that var, whichever var it is.
    for name, secret in _secret_env_items(environ):
        if secret in rendered:
            rendered = rendered.replace(secret, "{env:%s}" % name)

    # (c) anything secret-shaped left over is not written. Ever.
    if _has_literal_secret(field_name, rendered):
        logger.warning(
            "[OpenCode] MCP server %r: %s carries a literal secret that matches "
            "no environment variable — dropped from the turn config (the "
            "rendered config carries env-var names only; inject the value as a "
            "credential so it can be referenced by name)",
            server, field_name,
        )
        return None
    return rendered


def _render_fields(
    raw: Mapping, *, server: str, environ: Mapping[str, str],
    env_defaults: Dict[str, str], dropped: list,
) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key, value in raw.items():
        rendered = render_secret_value(
            str(value), server=server, field_name=str(key),
            environ=environ, env_defaults=env_defaults,
        )
        if rendered is None:
            dropped.append((server, str(key)))
            continue
        out[str(key)] = rendered
    return out


# ---------------------------------------------------------------------------
# shape mapping
# ---------------------------------------------------------------------------

def _map_one(
    name: str, entry: Mapping, *, environ: Mapping[str, str],
    env_defaults: Dict[str, str], dropped: list,
) -> Optional[dict]:
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
            rendered = _render_fields(
                headers, server=name, environ=environ,
                env_defaults=env_defaults, dropped=dropped,
            )
            if rendered:
                mapped["headers"] = rendered
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
            rendered = _render_fields(
                env, server=name, environ=environ,
                env_defaults=env_defaults, dropped=dropped,
            )
            if rendered:
                mapped["environment"] = rendered
        return mapped

    logger.warning(
        "[OpenCode] MCP server %r has neither `url` nor `command` — skipped "
        "(an incomplete entry would fail OpenCode's config parse and take the "
        "whole turn down)", name,
    )
    return None


def render_mcp_servers(
    servers: Mapping, environ: Optional[Mapping[str, str]] = None
) -> RenderedMcp:
    """Translate a whole ``mcpServers`` mapping under the secret rule. Pure
    over its inputs; ``environ`` defaults to the process environment."""
    env = os.environ if environ is None else environ
    env_defaults: Dict[str, str] = {}
    dropped: list = []
    mapped: Dict[str, dict] = {}
    for name, entry in (servers or {}).items():
        one = _map_one(
            str(name), entry, environ=env, env_defaults=env_defaults, dropped=dropped,
        )
        if one is not None:
            mapped[str(name)] = one
    return RenderedMcp(servers=mapped, env_defaults=env_defaults, dropped=tuple(dropped))


def map_mcp_servers(
    servers: Mapping, environ: Optional[Mapping[str, str]] = None
) -> Dict[str, dict]:
    """The ``servers`` half of :func:`render_mcp_servers`."""
    return render_mcp_servers(servers, environ).servers


def apply_mcp_config(
    config: Dict[str, object],
    servers: Optional[Mapping] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Merge the translated servers into ``config['mcp']`` in place.

    ``servers`` defaults to the agent's ``.mcp.json``. An entry already
    present in ``config['mcp']`` (declared by the operator in the shared
    ``opencode.json``) is left untouched. Returns the ``{VAR: default}`` map
    the runtime must export into the subprocess env (see the module
    docstring, rule a).
    """
    rendered = render_mcp_servers(
        servers if servers is not None else load_mcp_servers(), environ,
    )
    if not rendered.servers:
        return rendered.env_defaults

    existing = config.get("mcp")
    merged: Dict[str, object] = dict(existing) if isinstance(existing, dict) else {}
    added = []
    for name, entry in rendered.servers.items():
        if name in merged:
            logger.info("[OpenCode] MCP server %r declared in config — kept as-is", name)
            continue
        merged[name] = entry
        added.append(name)
    config["mcp"] = merged
    if added:
        logger.info("[OpenCode] wired %d MCP server(s): %s", len(added), ", ".join(sorted(added)))
    return rendered.env_defaults
