"""
MCP server config validator (#598, Layer 2 of AISEC-C2 closure).

Re-allows `.mcp.json` content through `POST /api/agents/{name}/credentials/inject`
ONLY when the structure passes strict validation. Layer 1 (#590) closed the
RCE-by-config bypass by removing `.mcp.json` from the inject allowlist; this
module restores the legitimate use case (owners adding/editing MCP servers
post-deploy) while keeping the attack surface closed.

Public API:
    validate_mcp_config(content: str) -> None
        Raises McpValidationError on any rejection.

    class McpValidationError(ValueError):
        Distinct exception for the router to surface as 400 Bad Request.

Threat model:
    - Attacker is an authenticated agent OWNER (already has the JWT).
    - Goal: defense in depth against shell-injection patterns and the
      AISEC-C2 exact reproduction. Does NOT prevent owners from running
      malicious code via approved runtimes (npx <evil-package>) — that's
      Layer 3 (sandbox MCP execution).

Architecture (SOLID at appropriate scale — single file, internal classes):
    validate_mcp_config()
      └─ _validate_servers_dict()       schema + per-entry dispatch
            └─ _ENTRY_VALIDATORS_BY_TRANSPORT[transport].validate(name, server)
                  ├─ _StdioValidator   command + args + env
                  ├─ _HttpValidator    url + headers + env (+ SSRF)
                  └─ _SseValidator     subclass of _HttpValidator (semantically
                                       distinct, same rules)
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
from typing import Mapping
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------


class McpValidationError(ValueError):
    """Raised when an MCP server config fails validation.

    Routers translate this to HTTP 400. The message is included verbatim in
    the response, so it must be safe to surface to the caller (no internal
    paths or stack traces).
    """


# ---------------------------------------------------------------------------
# Constants — tunable in one place
# ---------------------------------------------------------------------------

# Maximum size of the rendered `.mcp.json` content. 64KB is ~10x what a
# realistic config needs and keeps validation O(n) bounded.
MAX_CONTENT_BYTES = 64 * 1024

# Maximum number of mcpServers entries. Real configs have <10; cap defends
# against pathological JSON.
MAX_SERVER_COUNT = 32

# Server name (the dict key under mcpServers): conservative ASCII rule. Long
# enough for realistic identifiers, short enough to be UI-safe.
_SERVER_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

# Reserved server names — owners cannot overwrite Trinity's auto-injected
# entry with a different SHAPE (which would break agent-to-agent
# collaboration or open an RCE-by-config vector). The trinity entry IS
# allowed through if its shape matches what the agent server's
# inject_trinity_mcp_if_configured() writes — this lets owners legitimately
# update the bearer token (e.g. rotating their MCP API key) without
# tripping the reserved-name reject. See _is_canonical_trinity_entry.
RESERVED_SERVER_NAMES = frozenset({"trinity"})

# Canonical Trinity-MCP entry shape (mirrors
# docker/base-image/agent_server/services/trinity_mcp.py:_inject_claude_mcp).
# url is whatever the agent's TRINITY_MCP_URL env points to; we accept both
# the runtime value (so admins can override it cluster-wide) and the
# documented default for fresh installs.
_TRINITY_DEFAULT_URL = "http://mcp-server:8080/mcp"
_TRINITY_BEARER_RE = re.compile(r"^Bearer\s+trinity_mcp_[A-Za-z0-9_-]{1,200}$")


def _is_canonical_trinity_entry(server: dict) -> bool:
    """Return True iff ``server`` looks exactly like the Trinity-injected
    entry: only ``type``, ``url``, ``headers`` keys; ``type=http``; ``url``
    matches the configured Trinity MCP URL; ``headers`` contains only
    ``Authorization`` with a ``Bearer trinity_mcp_…`` token of the right
    shape.

    Strict allowlist — any extra key, any wrong value, any extra header,
    any non-bearer auth scheme is rejected. Owners who want to redefine
    trinity with a different shape (e.g. as a stdio server) hit the
    reserved-name reject; the canonical shape is the only escape.
    """
    if set(server.keys()) != {"type", "url", "headers"}:
        return False
    if server.get("type") != "http":
        return False

    url = server.get("url")
    if not isinstance(url, str):
        return False
    expected_url = os.getenv("TRINITY_MCP_URL", _TRINITY_DEFAULT_URL)
    if url not in (expected_url, _TRINITY_DEFAULT_URL):
        return False

    headers = server.get("headers")
    if not isinstance(headers, dict) or list(headers.keys()) != ["Authorization"]:
        return False
    auth = headers.get("Authorization")
    if not isinstance(auth, str) or not _TRINITY_BEARER_RE.match(auth):
        return False

    return True

# Allowed values for the `transport` field. `stdio` is implicit when only
# `command` is present (we set transport explicitly during validation).
ALLOWED_TRANSPORTS = frozenset({"stdio", "http", "sse"})

# Stdio runtime allowlist. Each entry is the EXACT command name the user can
# specify; absolute paths and alternates are rejected. `python3` listed
# separately from `python` because both are real on different distros.
COMMAND_ALLOWLIST = frozenset({
    "npx", "uvx", "python", "python3", "node", "bun", "deno", "docker",
})

# Per-runtime "execution flags" that turn the runtime into a shell. We block
# these as the FIRST positional arg (which would replace the script/package
# arg with inline code). Owners with a real need (`-c "import …"`) should
# package as a script and reference it instead.
_INLINE_EXEC_FLAGS_BY_COMMAND: Mapping[str, frozenset[str]] = {
    "python": frozenset({"-c", "--command"}),
    "python3": frozenset({"-c", "--command"}),
    "node": frozenset({"-e", "--eval", "-p", "--print"}),
    "bun": frozenset({"-e", "--eval"}),
    "deno": frozenset({"eval"}),
    # npx / uvx don't have an inline-exec flag; the first positional IS the
    # package name. Likewise docker.
}

# Shell metacharacters that have no business in any arg passed to a runtime
# allowlisted above. Each runtime invokes its target via execve, not a shell,
# so these characters never need to appear unescaped in real configs.
_SHELL_METACHARS_RE = re.compile(r"[;&|<>`$\n\r\x00]")

# Substring patterns that indicate command substitution. `re.search` finds
# them anywhere in the arg.
_COMMAND_SUBSTITUTION_RE = re.compile(r"\$\([^)]+\)|`[^`]+`")

# Env var names allowed as `${VAR}` references in args/env values. ASCII
# uppercase + digits + underscore, must start with a letter — standard
# POSIX shape; rejects `${PATH}` at a separate gate.
_ENV_VAR_REF_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Env vars the user must NOT reference — overriding any of these from an
# attacker-controlled `.mcp.json` could change library/binary loading,
# attach an interpreter, or hijack subsequent process launches.
RESERVED_ENV_REFS = frozenset({
    "PATH", "HOME", "USER", "SHELL",
    "LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES",
    "PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME",
    "NODE_OPTIONS", "NODE_PATH",
    # Trinity-internal — the agent server reads these on startup
    "TRINITY_MCP_API_KEY", "TRINITY_MCP_URL", "ADMIN_PASSWORD",
    "SECRET_KEY", "INTERNAL_API_SECRET", "CREDENTIAL_ENCRYPTION_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY",
})

# Patterns that look like raw secrets accidentally pasted into a config —
# reject with a clear message rather than silently accepting them. Mirrors
# `credential_patterns` in docker/base-image/hooks/guardrails-baseline.json.
_LITERAL_SECRET_PATTERNS = (
    re.compile(r"sk-ant-[a-zA-Z0-9_-]{20,}"),
    re.compile(r"sk-ant-oat01-[a-zA-Z0-9_-]{20,}"),
    re.compile(r"sk-(?:proj-)?[a-zA-Z0-9]{32,}"),
    re.compile(r"ghp_[a-zA-Z0-9]{30,}"),
    re.compile(r"github_pat_[a-zA-Z0-9_]{40,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[0-9a-zA-Z-]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{35}"),
)

# Header names allowed in http/sse server entries. Limit to the small set
# real servers actually use to avoid weird header smuggling vectors.
_ALLOWED_HEADER_NAMES = frozenset({
    "authorization", "x-api-key", "user-agent", "accept", "content-type",
})


# ---------------------------------------------------------------------------
# Helpers (private)
# ---------------------------------------------------------------------------


def _is_printable_ascii(s: str) -> bool:
    """Strict ASCII printable check (defeats Unicode lookalikes + null bytes)."""
    return all(0x20 <= ord(c) <= 0x7E for c in s)


# `${VAR}` substring used to extract refs from values like `Bearer ${TOKEN}`.
# Any reference found must satisfy `_ENV_VAR_REF_RE` shape AND not be in
# `RESERVED_ENV_REFS`. The remaining literal portion (with refs stripped)
# is then checked for shell metacharacters.
_ENV_VAR_SUBSTRING_RE = re.compile(r"\$\{([^}]*)\}")


def _validate_env_value(server_name: str, key: str, value: object) -> None:
    """Validate one env value.

    Allowed shapes (covers real-world cases like `Bearer ${API_TOKEN}`,
    `${OPENAI_BASE_URL}/v1`, plain literal URLs, plain `${VAR}` refs):
      - Any number of `${VAR}` substring references, each with a valid
        var name that is NOT in RESERVED_ENV_REFS
      - The remaining literal portion (refs stripped) is checked for shell
        metacharacters and command substitution

    Reject:
      - non-string values, oversize values
      - command substitution patterns (`$(…)` or backticks)
      - shell metacharacters in the literal portion
      - literal secrets (defense against accidental paste)
      - malformed or reserved `${VAR}` references
    """
    if not isinstance(value, str):
        raise McpValidationError(
            f"Server '{server_name}': env['{key}'] must be a string"
        )
    if len(value) > 4096:
        raise McpValidationError(
            f"Server '{server_name}': env['{key}'] exceeds 4096 chars"
        )

    # Pull out every `${VAR}` reference; each must be safe.
    refs = _ENV_VAR_SUBSTRING_RE.findall(value)
    for var_name in refs:
        if not _ENV_VAR_REF_RE.match(var_name):
            raise McpValidationError(
                f"Server '{server_name}': env['{key}'] references malformed "
                f"variable name '{var_name}'"
            )
        if var_name in RESERVED_ENV_REFS:
            raise McpValidationError(
                f"Server '{server_name}': env['{key}'] references reserved "
                f"variable '{var_name}'"
            )

    # Strip refs to get the literal portion, then apply shell-safety checks
    # to it. This way `Bearer ${TOKEN}` validates as `Bearer ` (safe).
    literal = _ENV_VAR_SUBSTRING_RE.sub("", value)

    if _COMMAND_SUBSTITUTION_RE.search(literal):
        raise McpValidationError(
            f"Server '{server_name}': env['{key}'] contains command "
            f"substitution"
        )
    if _SHELL_METACHARS_RE.search(literal):
        raise McpValidationError(
            f"Server '{server_name}': env['{key}'] contains shell "
            f"metacharacters"
        )
    for pat in _LITERAL_SECRET_PATTERNS:
        if pat.search(literal):
            raise McpValidationError(
                f"Server '{server_name}': env['{key}'] looks like a literal "
                f"secret — store it in .env and reference as ${{VAR}}"
            )


def _resolves_to_private_ip(hostname: str) -> bool:
    """Best-effort DNS check (mirrors SEC-179 / #179).

    Returns True if the hostname resolves to ANY private/loopback/link-local/
    multicast IP. On DNS failure: True (fail closed). Used to block IMDS,
    localhost, RFC 1918, and similar SSRF targets.
    """
    try:
        # getaddrinfo returns a list of (family, type, proto, canonname, sockaddr).
        # We only need the IP from sockaddr.
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return True  # fail closed
    for info in infos:
        sockaddr = info[4]
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except (ValueError, IndexError):
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# Transport validators
# ---------------------------------------------------------------------------


class _StdioValidator:
    """Stdio-transport server: command + args + env."""

    @staticmethod
    def validate(server_name: str, server: dict) -> None:
        if "command" not in server:
            raise McpValidationError(
                f"Server '{server_name}' (stdio): missing required field 'command'"
            )
        command = server["command"]
        if not isinstance(command, str) or not command:
            raise McpValidationError(
                f"Server '{server_name}': command must be a non-empty string"
            )
        if "/" in command or "\\" in command:
            raise McpValidationError(
                f"Server '{server_name}': command must be a name, not a path "
                f"(got '{command}')"
            )
        if not _is_printable_ascii(command):
            raise McpValidationError(
                f"Server '{server_name}': command contains non-ASCII or "
                f"control characters"
            )
        if command not in COMMAND_ALLOWLIST:
            raise McpValidationError(
                f"Server '{server_name}': command '{command}' not in allowlist. "
                f"Allowed: {sorted(COMMAND_ALLOWLIST)}"
            )

        args = server.get("args", [])
        if not isinstance(args, list):
            raise McpValidationError(
                f"Server '{server_name}': args must be a list"
            )
        if len(args) > 64:
            raise McpValidationError(
                f"Server '{server_name}': args list too long (max 64)"
            )

        # Block the inline-exec flag as the FIRST positional. Later positions
        # would be bare strings the runtime treats as data.
        inline_flags = _INLINE_EXEC_FLAGS_BY_COMMAND.get(command, frozenset())
        for i, arg in enumerate(args):
            if not isinstance(arg, str):
                raise McpValidationError(
                    f"Server '{server_name}': args[{i}] must be a string"
                )
            if len(arg) > 1024:
                raise McpValidationError(
                    f"Server '{server_name}': args[{i}] exceeds 1024 chars"
                )
            if "\x00" in arg:
                raise McpValidationError(
                    f"Server '{server_name}': args[{i}] contains null byte"
                )
            if _COMMAND_SUBSTITUTION_RE.search(arg):
                raise McpValidationError(
                    f"Server '{server_name}': args[{i}] contains command "
                    f"substitution"
                )
            if _SHELL_METACHARS_RE.search(arg):
                raise McpValidationError(
                    f"Server '{server_name}': args[{i}] contains shell "
                    f"metacharacters"
                )
            if i == 0 and arg in inline_flags:
                raise McpValidationError(
                    f"Server '{server_name}': inline-exec flag '{arg}' not "
                    f"allowed; package the code as a script and reference its path"
                )

        env = server.get("env", {})
        if not isinstance(env, dict):
            raise McpValidationError(
                f"Server '{server_name}': env must be an object"
            )
        if len(env) > 64:
            raise McpValidationError(
                f"Server '{server_name}': env has too many entries (max 64)"
            )
        for key, value in env.items():
            if not isinstance(key, str) or not _ENV_VAR_REF_RE.match(key):
                raise McpValidationError(
                    f"Server '{server_name}': env key '{key}' must match "
                    f"^[A-Z][A-Z0-9_]*$"
                )
            _validate_env_value(server_name, key, value)


class _HttpValidator:
    """HTTP-transport server: url + headers + env."""

    transport_label = "http"

    @classmethod
    def validate(cls, server_name: str, server: dict) -> None:
        if "url" not in server:
            raise McpValidationError(
                f"Server '{server_name}' ({cls.transport_label}): "
                f"missing required field 'url'"
            )
        url = server["url"]
        if not isinstance(url, str) or len(url) > 2048:
            raise McpValidationError(
                f"Server '{server_name}': url must be a string < 2048 chars"
            )

        try:
            parsed = urlparse(url)
        except ValueError as e:
            raise McpValidationError(
                f"Server '{server_name}': invalid url ({e})"
            )

        if parsed.scheme != "https":
            raise McpValidationError(
                f"Server '{server_name}': url must use https (got '{parsed.scheme}')"
            )
        # Reject userinfo (`https://user:pass@evil.com/...`) which can confuse
        # display-only auditors and is never needed for MCP.
        if "@" in (parsed.netloc or ""):
            raise McpValidationError(
                f"Server '{server_name}': url must not contain userinfo (@)"
            )
        hostname = (parsed.hostname or "").lower()
        if not hostname:
            raise McpValidationError(
                f"Server '{server_name}': url missing hostname"
            )
        if not _is_printable_ascii(hostname):
            raise McpValidationError(
                f"Server '{server_name}': url hostname contains non-ASCII "
                f"(possible homograph)"
            )
        if _resolves_to_private_ip(hostname):
            raise McpValidationError(
                f"Server '{server_name}': url hostname '{hostname}' resolves "
                f"to a private/loopback/link-local address (SSRF guard)"
            )

        headers = server.get("headers", {})
        if not isinstance(headers, dict):
            raise McpValidationError(
                f"Server '{server_name}': headers must be an object"
            )
        if len(headers) > 16:
            raise McpValidationError(
                f"Server '{server_name}': headers has too many entries (max 16)"
            )
        for key, value in headers.items():
            if not isinstance(key, str):
                raise McpValidationError(
                    f"Server '{server_name}': header name must be a string"
                )
            if key.lower() not in _ALLOWED_HEADER_NAMES:
                raise McpValidationError(
                    f"Server '{server_name}': header '{key}' not in allowlist. "
                    f"Allowed: {sorted(_ALLOWED_HEADER_NAMES)}"
                )
            # Header values reuse the env-value rules: ${VAR} or safe literal.
            _validate_env_value(server_name, f"headers.{key}", value)

        # http/sse can also carry env (rare but allowed by the MCP spec).
        env = server.get("env", {})
        if not isinstance(env, dict):
            raise McpValidationError(
                f"Server '{server_name}': env must be an object"
            )
        for key, value in env.items():
            if not isinstance(key, str) or not _ENV_VAR_REF_RE.match(key):
                raise McpValidationError(
                    f"Server '{server_name}': env key '{key}' must match "
                    f"^[A-Z][A-Z0-9_]*$"
                )
            _validate_env_value(server_name, key, value)


class _SseValidator(_HttpValidator):
    """SSE-transport server. Identical rules to HTTP; separate class for
    diagnostic clarity in error messages.
    """
    transport_label = "sse"


# Dispatch table — Open-Closed: add a new transport by adding a class and
# one entry here, no edits elsewhere.
_ENTRY_VALIDATORS_BY_TRANSPORT = {
    "stdio": _StdioValidator,
    "http": _HttpValidator,
    "sse": _SseValidator,
}


# ---------------------------------------------------------------------------
# Entry / config orchestration
# ---------------------------------------------------------------------------


def _resolve_transport(server_name: str, server: dict) -> str:
    """Determine the transport for an entry.

    The MCP config spec lets transport be implicit:
      - stdio: presence of `command`
      - http/sse: presence of `url` + explicit `type` field
    We require the `type` field for http/sse to avoid ambiguity, and accept
    `command` as an implicit stdio signal.
    """
    explicit = server.get("type")
    if explicit is not None:
        if not isinstance(explicit, str) or explicit not in ALLOWED_TRANSPORTS:
            raise McpValidationError(
                f"Server '{server_name}': type must be one of "
                f"{sorted(ALLOWED_TRANSPORTS)}"
            )
        return explicit
    # No explicit type → infer
    if "command" in server:
        return "stdio"
    if "url" in server:
        raise McpValidationError(
            f"Server '{server_name}': url provided without 'type' field; "
            f"set type to 'http' or 'sse'"
        )
    raise McpValidationError(
        f"Server '{server_name}': cannot determine transport (no command, "
        f"no url, no type)"
    )


def _validate_entry(server_name: str, server: object) -> None:
    """Validate a single MCP server entry."""
    if not isinstance(server_name, str):
        raise McpValidationError("MCP server name must be a string")
    if not _SERVER_NAME_RE.match(server_name):
        raise McpValidationError(
            f"MCP server name '{server_name}' invalid; must match "
            f"^[a-zA-Z0-9_-]{{1,64}}$"
        )
    if not isinstance(server, dict):
        raise McpValidationError(
            f"MCP server '{server_name}' must be a JSON object"
        )
    if server_name in RESERVED_SERVER_NAMES:
        # Allow the reserved trinity entry through ONLY if its shape
        # matches the agent server's auto-inject — owners legitimately
        # need to edit the bearer token to rotate their MCP API key.
        # Any other shape (different url, extra headers, redefined as
        # stdio, etc.) hits the reserved-name reject.
        if server_name == "trinity" and _is_canonical_trinity_entry(server):
            return
        raise McpValidationError(
            f"MCP server name '{server_name}' is reserved by Trinity"
        )

    transport = _resolve_transport(server_name, server)
    validator = _ENTRY_VALIDATORS_BY_TRANSPORT[transport]
    validator.validate(server_name, server)

    # Reject any unknown top-level fields. Closed schema = no surprise fields
    # that future MCP versions might interpret in unexpected ways.
    allowed_keys = {"command", "args", "env", "url", "headers", "type"}
    extra = set(server.keys()) - allowed_keys
    if extra:
        raise McpValidationError(
            f"Server '{server_name}': unknown field(s) {sorted(extra)}; "
            f"allowed: {sorted(allowed_keys)}"
        )


def _validate_servers_dict(servers: dict) -> None:
    """Validate the top-level mcpServers dict."""
    if len(servers) > MAX_SERVER_COUNT:
        raise McpValidationError(
            f"Too many MCP servers ({len(servers)}); max {MAX_SERVER_COUNT}"
        )
    for name, entry in servers.items():
        _validate_entry(name, entry)


def validate_mcp_config(content: str) -> None:
    """Validate the rendered `.mcp.json` content string.

    Raises McpValidationError with a single human-readable message on the
    first failure. Routers should surface the message in the HTTP 400 body.
    """
    if not isinstance(content, str):
        raise McpValidationError(".mcp.json content must be a string")
    if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise McpValidationError(
            f".mcp.json content exceeds {MAX_CONTENT_BYTES} bytes"
        )

    try:
        config = json.loads(content)
    except json.JSONDecodeError as e:
        raise McpValidationError(f".mcp.json is not valid JSON: {e.msg}")

    if not isinstance(config, dict):
        raise McpValidationError(".mcp.json root must be a JSON object")

    # Allow only `mcpServers` at the root. Other top-level keys (e.g. legacy
    # `inputs`, future spec fields) are rejected to keep the schema closed.
    extra_root = set(config.keys()) - {"mcpServers"}
    if extra_root:
        raise McpValidationError(
            f".mcp.json has unknown top-level field(s) {sorted(extra_root)}; "
            f"only 'mcpServers' is allowed"
        )

    servers = config.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise McpValidationError(".mcp.json mcpServers must be an object")

    _validate_servers_dict(servers)
