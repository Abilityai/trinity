"""
Agent Service Read-Only Mode - Code protection for deployed agents.

Handles read-only mode toggle which prevents agents from modifying
source code, instructions, or configuration files while allowing
output to designated directories.

The guard is baked into the base image at /opt/trinity/hooks/read-only-guard.py
and registered in ~/.claude/settings.json (claude-settings.json).  The
platform controls whether the guard is active by writing (or clearing)
~/.trinity/read-only-config.json inside the agent container.

GUARD-001 / GUARD-002 invariant: the guard script is root-owned in
/opt/trinity/hooks/ and cannot be overwritten by the agent.
"""
import json
import logging

from fastapi import HTTPException

from models import User
from database import db
from services.docker_service import get_agent_container
from services.agent_client import get_agent_client

logger = logging.getLogger(__name__)

# Default patterns for read-only mode
DEFAULT_BLOCKED_PATTERNS = [
    "*.py", "*.js", "*.ts", "*.jsx", "*.tsx", "*.vue", "*.svelte",
    "*.go", "*.rs", "*.rb", "*.java", "*.c", "*.cpp", "*.h",
    "*.sh", "*.bash", "Makefile", "Dockerfile",
    "CLAUDE.md", "README.md", ".claude/*", ".env", ".env.*",
    "template.yaml", "*.yaml", "*.yml", "*.json", "*.toml"
]

DEFAULT_ALLOWED_PATTERNS = [
    "content/*", "output/*", "reports/*", "exports/*",
    "*.log", "*.txt",
    ".trinity/operator-queue.json"
]

_CONFIG_PATH = ".trinity/read-only-config.json"


def get_default_config() -> dict:
    """Get default read-only configuration."""
    return {
        "blocked_patterns": DEFAULT_BLOCKED_PATTERNS.copy(),
        "allowed_patterns": DEFAULT_ALLOWED_PATTERNS.copy()
    }


async def get_read_only_status_logic(
    agent_name: str,
    current_user: User
) -> dict:
    """Get the read-only mode status for an agent."""
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    read_only_data = db.get_read_only_mode(agent_name)

    return {
        "agent_name": agent_name,
        "enabled": read_only_data["enabled"],
        "config": read_only_data["config"] or get_default_config()
    }


async def set_read_only_status_logic(
    agent_name: str,
    body: dict,
    current_user: User
) -> dict:
    """
    Set the read-only mode status for an agent.

    When enabling:
    - Configuration is saved to database
    - If agent is running, config file is written immediately

    When disabling:
    - Setting is saved to database
    - Config file is cleared (written with enabled: false) so the
      baked-in guard exits 0 on the next invocation

    Body:
    - enabled: True to enable read-only mode, False to disable
    - config: Optional dict with 'blocked_patterns' and 'allowed_patterns'
    """
    if not db.can_user_share_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="Only the owner can modify read-only settings")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    if db.is_system_agent(agent_name):
        raise HTTPException(status_code=403, detail="Cannot modify read-only mode for system agent")

    enabled = body.get("enabled")
    if enabled is None:
        raise HTTPException(status_code=400, detail="enabled is required")

    enabled = bool(enabled)
    config = body.get("config")

    if config is not None:
        if not isinstance(config, dict):
            raise HTTPException(status_code=400, detail="config must be an object")
        if "blocked_patterns" in config and not isinstance(config["blocked_patterns"], list):
            raise HTTPException(status_code=400, detail="blocked_patterns must be a list")
        if "allowed_patterns" in config and not isinstance(config["allowed_patterns"], list):
            raise HTTPException(status_code=400, detail="allowed_patterns must be a list")

    if enabled and config is None:
        config = get_default_config()

    db.set_read_only_mode(agent_name, enabled, config)

    config_written = False
    if container.status == "running":
        try:
            if enabled:
                result = await inject_read_only_hooks(agent_name, config)
            else:
                result = await remove_read_only_hooks(agent_name)
            config_written = result.get("success", False)
        except Exception as e:
            logger.warning(f"Failed to update read-only config in running agent {agent_name}: {e}")

    logger.info(
        f"Read-only mode {'enabled' if enabled else 'disabled'} for agent {agent_name} "
        f"by {current_user.username}. Config written: {config_written}"
    )

    return {
        "status": "updated",
        "agent_name": agent_name,
        "enabled": enabled,
        "config": config,
        "hooks_injected": config_written,
        "message": f"Read-only mode {'enabled' if enabled else 'disabled'}." + (
            " Config will be applied on next agent start." if enabled and not config_written else ""
        )
    }


async def inject_read_only_hooks(agent_name: str, config: dict | None = None) -> dict:
    """
    Write ~/.trinity/read-only-config.json into the running agent container.

    The baked-in guard script (/opt/trinity/hooks/read-only-guard.py) reads
    this file on every PreToolUse event. Writing the file with enabled=true
    activates the guard; the platform does not need to modify settings.local.json.

    Args:
        agent_name: Name of the agent
        config: Read-only configuration (blocked/allowed patterns).
                Loaded from DB when None.

    Returns:
        dict with success status and details
    """
    if config is None:
        read_only_data = db.get_read_only_mode(agent_name)
        if not read_only_data["enabled"]:
            return {"success": True, "skipped": True, "reason": "read_only_mode_disabled"}
        config = read_only_data["config"] or get_default_config()

    client = get_agent_client(agent_name)

    payload = {"enabled": True, **config}
    result = await client.write_file(
        _CONFIG_PATH,
        json.dumps(payload, indent=2),
        platform=True
    )
    if not result.get("success"):
        return {"success": False, "error": f"Failed to write config: {result.get('error')}"}

    logger.info(f"Read-only config written for agent {agent_name}")
    return {"success": True}


async def remove_read_only_hooks(agent_name: str) -> dict:
    """
    Disable the read-only guard for a running agent by writing
    {"enabled": false} to ~/.trinity/read-only-config.json.

    The guard script reads this file on every invocation and exits 0
    (allow) when enabled is false, so no hook un-registration is needed.

    Also cleans up any legacy settings.local.json hook entry from older
    platform versions that injected the hook dynamically.

    Args:
        agent_name: Name of the agent

    Returns:
        dict with success status
    """
    client = get_agent_client(agent_name)

    result = await client.write_file(
        _CONFIG_PATH,
        json.dumps({"enabled": False}, indent=2),
        platform=True
    )
    if not result.get("success"):
        return {"success": False, "error": f"Failed to write config: {result.get('error')}"}

    # Migration: remove legacy settings.local.json hook entry if present
    await _remove_legacy_settings_hook(client)

    logger.info(f"Read-only mode disabled for agent {agent_name}")
    return {"success": True}


async def _remove_legacy_settings_hook(client) -> None:
    """Remove the old dynamic hook entry from settings.local.json (pre-#887 agents)."""
    settings_path = ".claude/settings.local.json"
    read_result = await client.read_file(settings_path)
    if not read_result.get("success") or not read_result.get("content"):
        return

    try:
        settings = json.loads(read_result["content"])
    except json.JSONDecodeError:
        return

    pre_hook_matcher = "Write|Edit|NotebookEdit"
    changed = False
    if "hooks" in settings and "PreToolUse" in settings["hooks"]:
        before = len(settings["hooks"]["PreToolUse"])
        settings["hooks"]["PreToolUse"] = [
            h for h in settings["hooks"]["PreToolUse"]
            if h.get("matcher") != pre_hook_matcher
        ]
        changed = len(settings["hooks"]["PreToolUse"]) < before
        if not settings["hooks"]["PreToolUse"]:
            del settings["hooks"]["PreToolUse"]
        if not settings["hooks"]:
            del settings["hooks"]

    if changed:
        await client.write_file(settings_path, json.dumps(settings, indent=2))
