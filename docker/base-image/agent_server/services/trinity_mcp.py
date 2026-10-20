"""
Trinity MCP injection service for agent-to-agent collaboration.

Supports both Claude Code (.mcp.json) and Gemini CLI (gemini mcp add).
"""
import os
import json
import logging
import subprocess
import tomllib  # py3.11+; agent base image is python 3.13
from pathlib import Path

logger = logging.getLogger(__name__)

# #2392: every Trinity MCP call carries the id of the execution that spawned the
# CLI, so the backend can attribute tool calls. The value is expanded by the CLI
# itself from its own process env (TRINITY_EXECUTION_ID is set per spawn by the
# agent server), so one config file serves every concurrent execution.
EXECUTION_ID_HEADER = "X-Trinity-Execution-Id"
EXECUTION_ID_HEADER_VALUE = "${TRINITY_EXECUTION_ID:-manual}"


def inject_trinity_mcp_if_configured() -> bool:
    """
    Inject Trinity MCP server - runtime aware.

    This enables agent-to-agent communication via the Trinity platform.
    Called on agent startup.

    For Claude Code: Writes to ~/.mcp.json
    For Gemini CLI: Writes to ~/.gemini/settings.json
    """
    trinity_mcp_url = os.getenv("TRINITY_MCP_URL")
    trinity_mcp_api_key = os.getenv("TRINITY_MCP_API_KEY")

    if not trinity_mcp_url or not trinity_mcp_api_key:
        logger.info("Trinity MCP not configured - skipping injection")
        return False

    runtime = os.getenv("AGENT_RUNTIME", "claude-code").lower()

    if runtime == "codex":
        return _inject_codex_mcp(trinity_mcp_url, trinity_mcp_api_key)
    if runtime == "gemini-cli":
        return _inject_gemini_mcp(trinity_mcp_url, trinity_mcp_api_key)
    else:
        return _inject_claude_mcp(trinity_mcp_url, trinity_mcp_api_key)


def _inject_claude_mcp(trinity_mcp_url: str, trinity_mcp_api_key: str) -> bool:
    """Inject Trinity MCP into Claude Code's .mcp.json file."""
    home_dir = Path("/home/developer")
    mcp_file = home_dir / ".mcp.json"

    # Trinity MCP server configuration using HTTP transport
    trinity_mcp_entry = {
        "trinity": {
            "type": "http",
            "url": trinity_mcp_url,
            "headers": {
                "Authorization": f"Bearer {trinity_mcp_api_key}",
                # Claude Code expands ${VAR:-default} in http MCP headers.
                EXECUTION_ID_HEADER: EXECUTION_ID_HEADER_VALUE,
            }
        }
    }

    try:
        # Read existing .mcp.json if it exists
        if mcp_file.exists():
            content = mcp_file.read_text()
            if content.strip():
                mcp_config = json.loads(content)
            else:
                mcp_config = {"mcpServers": {}}
        else:
            mcp_config = {"mcpServers": {}}

        # Ensure mcpServers key exists
        if "mcpServers" not in mcp_config:
            mcp_config["mcpServers"] = {}

        # Add Trinity MCP (overwrite if exists)
        mcp_config["mcpServers"]["trinity"] = trinity_mcp_entry["trinity"]

        # Write back to file
        mcp_file.write_text(json.dumps(mcp_config, indent=2))
        logger.info(f"Injected Trinity MCP server into {mcp_file} (Claude Code)")
        return True

    except Exception as e:
        logger.warning(f"Failed to inject Trinity MCP for Claude Code: {e}")
        return False


def _inject_gemini_mcp(trinity_mcp_url: str, trinity_mcp_api_key: str) -> bool:
    """
    Inject Trinity MCP into Gemini CLI by writing to settings.json.

    Note: `gemini mcp add --transport http` has a bug where it creates a 'type' field
    that the config parser rejects as unrecognized. We work around this by writing
    directly to ~/.gemini/settings.json with the correct format.

    The correct format for HTTP/SSE MCP servers uses 'url' and 'headers' fields.
    """
    try:
        home_dir = Path("/home/developer")
        gemini_dir = home_dir / ".gemini"
        settings_file = gemini_dir / "settings.json"

        # Ensure .gemini directory exists
        gemini_dir.mkdir(parents=True, exist_ok=True)

        # Read existing settings or create new
        if settings_file.exists():
            content = settings_file.read_text()
            settings = json.loads(content) if content.strip() else {}
        else:
            settings = {}

        # Ensure mcpServers key exists
        if "mcpServers" not in settings:
            settings["mcpServers"] = {}

        # Add/update Trinity MCP server with HTTP transport and auth header
        # Using 'url' and 'headers' format (NOT 'type' which causes parser errors)
        settings["mcpServers"]["trinity"] = {
            "url": trinity_mcp_url,
            "headers": {
                "Authorization": f"Bearer {trinity_mcp_api_key}",
                # Gemini CLI expands $VAR / ${VAR} / ${VAR:-default} across
                # settings.json at load (packages/cli/src/utils/envVarResolver.ts).
                EXECUTION_ID_HEADER: EXECUTION_ID_HEADER_VALUE,
            }
        }

        # Write settings back
        settings_file.write_text(json.dumps(settings, indent=2))

        logger.info(f"Injected Trinity MCP server into {settings_file} (Gemini CLI)")
        return True

    except Exception as e:
        logger.warning(f"Failed to inject Trinity MCP for Gemini CLI: {e}")
        return False


def configure_mcp_servers(mcp_servers: dict) -> bool:
    """
    Configure additional MCP servers for the agent - runtime aware.

    Args:
        mcp_servers: Dict of server configs from template
                     {"server_name": {"command": "...", "args": [...]}}
    """
    if not mcp_servers:
        return True

    runtime = os.getenv("AGENT_RUNTIME", "claude-code").lower()

    if runtime == "codex":
        return _configure_codex_mcp_servers(mcp_servers)
    if runtime == "gemini-cli":
        return _configure_gemini_mcp_servers(mcp_servers)
    else:
        return _configure_claude_mcp_servers(mcp_servers)


def _configure_claude_mcp_servers(mcp_servers: dict) -> bool:
    """Configure MCP servers for Claude Code via .mcp.json."""
    home_dir = Path("/home/developer")
    mcp_file = home_dir / ".mcp.json"

    try:
        if mcp_file.exists():
            content = mcp_file.read_text()
            mcp_config = json.loads(content) if content.strip() else {"mcpServers": {}}
        else:
            mcp_config = {"mcpServers": {}}

        if "mcpServers" not in mcp_config:
            mcp_config["mcpServers"] = {}

        # Add each MCP server
        for server_name, config in mcp_servers.items():
            mcp_config["mcpServers"][server_name] = config

        mcp_file.write_text(json.dumps(mcp_config, indent=2))
        logger.info(f"Configured {len(mcp_servers)} MCP servers for Claude Code")
        return True

    except Exception as e:
        logger.warning(f"Failed to configure MCP servers for Claude Code: {e}")
        return False


def _configure_gemini_mcp_servers(mcp_servers: dict) -> bool:
    """Configure MCP servers for Gemini CLI via `gemini mcp add` commands."""
    success_count = 0

    for server_name, config in mcp_servers.items():
        try:
            command = config.get("command", "")
            args = config.get("args", [])

            if not command:
                logger.warning(f"Skipping MCP server '{server_name}': no command specified")
                continue

            # Build the gemini mcp add command with --scope user for home directory
            cmd = ["gemini", "mcp", "add", "--scope", "user", server_name, command] + args

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode == 0:
                logger.info(f"Added MCP server '{server_name}' for Gemini CLI")
                success_count += 1
            else:
                logger.warning(f"Failed to add MCP server '{server_name}': {result.stderr}")

        except Exception as e:
            logger.warning(f"Error adding MCP server '{server_name}': {e}")

    logger.info(f"Configured {success_count}/{len(mcp_servers)} MCP servers for Gemini CLI")
    return success_count > 0 or len(mcp_servers) == 0


# ---------------------------------------------------------------------------
# Codex CLI MCP configuration (#1187 Phase F)
#
# Codex reads MCP servers from ``$CODEX_HOME/config.toml`` under
# ``[mcp_servers.<name>]``. We write that file DIRECTLY (the same approach the
# Gemini path uses for its settings.json — deterministic, avoids `codex mcp
# add` CLI-syntax drift) and MERGE so the Trinity-MCP injection and the
# template-MCP configuration (two separate calls) don't clobber each other.
#
# CODEX_HOME is the relocated, gitignored scratch path (see codex_runtime.py);
# both this config writer and the runtime resolve it via the same helper so the
# file we write is the file Codex reads.
# ---------------------------------------------------------------------------

def _codex_config_path() -> Path:
    from .codex_runtime import _codex_home  # lazy: avoid an import cycle

    return Path(_codex_home()) / "config.toml"


def _read_codex_config(path: Path) -> dict:
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except (IOError, OSError):
        return {}
    except tomllib.TOMLDecodeError as exc:
        # Do NOT silently reset on a decode error. If we returned {} here, the
        # next _upsert_codex_mcp_servers would rewrite the file from {} and
        # drop every previously-written server — including the Trinity MCP
        # wiring — with no trace. Back the bad file up and log loudly so the
        # corruption is recoverable and visible; the caller re-injects its
        # servers onto a clean slate on this run.
        try:
            backup = path.with_name(path.name + ".corrupt")
            path.replace(backup)
            logger.error(
                "Codex config.toml is malformed (%s); backed it up to %s and "
                "starting from an empty config. MCP servers are re-written this "
                "run.",
                exc, backup,
            )
        except OSError as backup_err:
            logger.error(
                "Codex config.toml is malformed (%s) and the backup also failed "
                "(%s); rewriting from an empty config.",
                exc, backup_err,
            )
        return {}


# Bare TOML keys are limited to ASCII letters, digits, '_' and '-'. Anything
# else (space, '.', ']', '#', control chars) must be a quoted basic-string key.
_BARE_KEY_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
)

# Basic-string escapes with TOML shorthand. Everything else < 0x20 (plus 0x7F)
# becomes a \uXXXX escape; otherwise an out-of-band character (e.g. a newline
# in a server name or env value) yields invalid TOML.
_TOML_SHORTHAND_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def _toml_escape(value: str) -> str:
    out: list[str] = []
    for ch in value:
        shorthand = _TOML_SHORTHAND_ESCAPES.get(ch)
        if shorthand is not None:
            out.append(shorthand)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return "".join(out)


def _toml_key(key: str) -> str:
    """Render a TOML key segment: bare when it is a valid bare key, otherwise a
    quoted basic-string key. Used for both ``key = ...`` lines and the dotted
    segments of ``[table.header]`` lines so a server name or env key with a
    space/dot/special char can't produce unparseable TOML."""
    if key and all(c in _BARE_KEY_CHARS for c in key):
        return key
    return f'"{_toml_escape(key)}"'


def _toml_scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        # A list of dicts is a TOML array-of-tables ([[name]]), which this
        # writer never emits. Stringifying the dicts would silently corrupt a
        # pre-existing config on round-trip. Raise instead: the caller
        # (_upsert_codex_mcp_servers) serializes BEFORE write_text, so a raise
        # leaves the original file intact and logs a warning — a safe no-op
        # rather than a mangled rewrite (#1187 review).
        if any(isinstance(item, dict) for item in value):
            raise TypeError(
                "codex config writer does not support TOML array-of-tables; "
                "refusing to serialize to avoid corrupting the existing file"
            )
        return "[" + ", ".join(_toml_scalar(item) for item in value) + "]"
    if isinstance(value, dict):
        # _serialize_table routes dicts to sub-tables, so a dict reaching here
        # is an unexpected nesting. Raise rather than emit a stringified dict.
        raise TypeError(
            "codex config writer received a dict where a scalar was expected"
        )
    return f'"{_toml_escape(str(value))}"'


def _serialize_table(path: list[str], table: dict, lines: list[str]) -> None:
    """Recursively emit a TOML table. ``path`` is the header segments (empty for
    the document root). Scalar keys are always emitted before any nested-table
    headers (TOML requires it). A table with only sub-tables is left as an
    implicit super-table (no redundant ``[parent]`` header), matching the
    hand-written output this replaced."""
    scalars = {k: v for k, v in table.items() if not isinstance(v, dict)}
    sub_tables = {k: v for k, v in table.items() if isinstance(v, dict)}
    # Emit a header for a non-root table that has its own scalar keys, or that
    # is entirely empty (so an explicit empty table round-trips). Skip it for a
    # pure super-table whose only contents are nested tables.
    emit_header = bool(path) and (bool(scalars) or not sub_tables)
    if emit_header:
        lines.append("[" + ".".join(_toml_key(seg) for seg in path) + "]")
    for key, value in scalars.items():
        lines.append(f"{_toml_key(key)} = {_toml_scalar(value)}")
    if emit_header or (scalars and sub_tables):
        lines.append("")
    for key, sub in sub_tables.items():
        _serialize_table(path + [key], sub, lines)


def _serialize_codex_config(config: dict) -> str:
    """Serialize the codex config we manage to TOML: arbitrary top-level scalars
    and tables (preserved on round-trip) plus the ``[mcp_servers.<name>]``
    tables we write. Table/key names and string values are quoted/escaped so a
    special character can never yield unparseable TOML."""
    lines: list[str] = []
    _serialize_table([], config, lines)
    return "\n".join(lines).rstrip() + "\n"


def _upsert_codex_mcp_servers(servers: dict) -> bool:
    """Merge ``servers`` into ``$CODEX_HOME/config.toml`` ``[mcp_servers.*]``,
    preserving any existing servers + top-level settings."""
    path = _codex_config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        config = _read_codex_config(path)
        config.setdefault("mcp_servers", {})
        config["mcp_servers"].update(servers)
        path.write_text(_serialize_codex_config(config))
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Failed to write codex config.toml: {e}")
        return False


def _inject_codex_mcp(trinity_mcp_url: str, trinity_mcp_api_key: str) -> bool:
    """Wire the Trinity HTTP MCP server into the codex config.

    The bearer token is referenced by ENV VAR (``bearer_token_env_var``), NOT
    written as a literal — the secret stays in the agent's environment and is
    never persisted to config.toml (#1187 Phase F).
    """
    # trinity_mcp_api_key is intentionally unused: Codex reads it from the
    # TRINITY_MCP_API_KEY env var at run time. Accepting it keeps the
    # _inject_*_mcp signatures uniform across runtimes.
    del trinity_mcp_api_key
    server = {
        "url": trinity_mcp_url,
        "bearer_token_env_var": "TRINITY_MCP_API_KEY",
        # #2392: Codex has no ${VAR} expansion; env_http_headers maps header ->
        # env var name, read from the codex process env. When the var is unset
        # or blank Codex OMITS the header (no `manual` default); the backend
        # treats an absent id as absent.
        "env_http_headers": {EXECUTION_ID_HEADER: "TRINITY_EXECUTION_ID"},
    }
    if _upsert_codex_mcp_servers({"trinity": server}):
        logger.info("Injected Trinity MCP server into codex config.toml")
        return True
    return False


def _configure_codex_mcp_servers(mcp_servers: dict) -> bool:
    """Configure template-supplied MCP servers for Codex via config.toml.

    Stdio servers (command + args) are supported, matching the Gemini path's
    scope. A server with no command is skipped with a warning.
    """
    servers: dict = {}
    for server_name, config in mcp_servers.items():
        command = config.get("command", "")
        if not command:
            logger.warning(f"Skipping MCP server '{server_name}': no command specified")
            continue
        entry: dict = {"command": command}
        args = config.get("args")
        if args:
            entry["args"] = args
        env = config.get("env")
        if isinstance(env, dict) and env:
            entry["env"] = env
        servers[server_name] = entry

    if not servers:
        return len(mcp_servers) == 0

    ok = _upsert_codex_mcp_servers(servers)
    logger.info(
        f"Configured {len(servers)}/{len(mcp_servers)} MCP servers for Codex"
    )
    return ok
