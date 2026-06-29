"""
Agent Service Helpers - Shared utility functions.

Contains functions used across multiple agent operations.
"""
import fnmatch
import json
import logging
import asyncio
from typing import Optional, List, Callable, Any

import httpx
from fastapi import HTTPException

from models import User, AgentStatus
from database import db
from services.docker_service import (
    docker_client,
    list_all_agents,
    list_all_agents_fast,
    get_agent_container,
)
from services.docker_utils import volume_get
from services.agent_auth import derive_agent_token, merge_auth_headers
from services.settings_service import get_anthropic_api_key, get_github_pat, get_agent_full_capabilities, settings_service
from utils.helpers import sanitize_agent_name

logger = logging.getLogger(__name__)

# SEC-172: Default allowlist of permitted Docker base images (fnmatch patterns)
DEFAULT_BASE_IMAGE_ALLOWLIST = [
    "trinity-agent-base:*",
]

# #1187: runtime classification. Subscriptions are Claude-OAuth tokens, so the
# auto-assign (crud) and the CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY juggle
# (lifecycle recreate) apply ONLY to the Claude Code runtime. Gemini and Codex
# bring their own credentials via .env (CRED-002).
CLAUDE_RUNTIME_NAMES = frozenset({"claude-code", "claude"})


def is_claude_runtime(runtime: Optional[str]) -> bool:
    """True for the Claude Code runtime, INCLUDING the default (unset/empty →
    claude-code). Non-Claude runtimes (gemini-cli, gemini, codex) return False.
    """
    return (runtime or "claude-code").lower() in CLAUDE_RUNTIME_NAMES


# #1187: every runtime the platform can launch. Mirrors the agent-side
# `runtime_adapter.KNOWN_RUNTIMES` (which lives in the base image and isn't
# importable from the backend). Keep the two in sync when adding a runtime.
KNOWN_RUNTIME_NAMES = CLAUDE_RUNTIME_NAMES | frozenset({"gemini-cli", "gemini", "codex"})


def validate_runtime(runtime: Optional[str]) -> None:
    """Reject an unknown ``runtime`` at create time.

    The agent-side ``get_runtime()`` already fails loud on a bad
    ``AGENT_RUNTIME``, but only when the container boots — by then the agent
    exists and the failure is a cryptic crash-loop. Catching a typo'd
    ``runtime:`` here turns it into a clear 400 at creation. Unset/empty is
    valid (defaults to claude-code).

    Raises:
        HTTPException(400): if ``runtime`` is set to an unrecognized value.
    """
    if not runtime:
        return
    if runtime.lower() not in KNOWN_RUNTIME_NAMES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown runtime {runtime!r}. "
                f"Supported runtimes: {sorted(KNOWN_RUNTIME_NAMES)}."
            ),
        )


def validate_base_image(image: str) -> None:
    """
    Validate that a Docker base image is on the allowlist.

    Checks against the admin-configurable 'base_image_allowlist' setting,
    falling back to DEFAULT_BASE_IMAGE_ALLOWLIST.

    Raises:
        HTTPException(403): If image is not on the allowlist.
    """
    # Load allowlist from settings, fall back to default
    allowlist_json = settings_service.get_setting("base_image_allowlist")
    if allowlist_json:
        try:
            allowlist = json.loads(allowlist_json)
            if not isinstance(allowlist, list):
                logger.warning("base_image_allowlist setting is not a list, using default")
                allowlist = DEFAULT_BASE_IMAGE_ALLOWLIST
        except json.JSONDecodeError:
            logger.warning("base_image_allowlist setting is invalid JSON, using default")
            allowlist = DEFAULT_BASE_IMAGE_ALLOWLIST
    else:
        allowlist = DEFAULT_BASE_IMAGE_ALLOWLIST

    for pattern in allowlist:
        if fnmatch.fnmatch(image, pattern):
            return

    logger.warning(
        "Blocked agent creation with disallowed base image: %s (allowlist: %s)",
        image, allowlist,
    )
    raise HTTPException(
        status_code=403,
        detail=f"Base image '{image}' is not in the allowed image list. "
        f"Allowed patterns: {allowlist}"
    )


async def agent_http_request(
    agent_name: str,
    method: str,
    path: str,
    max_retries: int = 3,
    retry_delay: float = 1.0,
    timeout: float = 30.0,
    **kwargs
) -> httpx.Response:
    """
    Make an HTTP request to an agent's internal server with retry logic.

    This handles the common case where an agent container is running but
    its internal HTTP server is not yet ready to accept connections.

    Args:
        agent_name: Name of the agent
        method: HTTP method (GET, POST, etc.)
        path: URL path (e.g., /api/files)
        max_retries: Number of retry attempts
        retry_delay: Seconds between retries (doubles each retry)
        timeout: Request timeout in seconds
        **kwargs: Additional arguments passed to httpx request

    Returns:
        httpx.Response object

    Raises:
        httpx.ConnectError: If all connection attempts fail
        httpx.TimeoutException: If request times out
    """
    agent_url = f"http://agent-{agent_name}:8000{path}"

    # #1159: stamp the per-agent auth token (overriding any caller-supplied
    # value case-insensitively) so every retry attempt carries it.
    kwargs["headers"] = merge_auth_headers(agent_name, kwargs.get("headers"))

    last_error = None
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.request(method, agent_url, **kwargs)
                return response
        except httpx.ConnectError as e:
            last_error = e
            if attempt < max_retries - 1:
                delay = retry_delay * (2 ** attempt)  # Exponential backoff
                logger.debug(
                    f"Agent {agent_name} connection failed (attempt {attempt + 1}/{max_retries}), "
                    f"retrying in {delay}s..."
                )
                await asyncio.sleep(delay)
            else:
                logger.warning(
                    f"Agent {agent_name} connection failed after {max_retries} attempts: {e}"
                )

    # All retries exhausted
    raise last_error or httpx.ConnectError(f"Failed to connect to agent {agent_name}")


def get_accessible_agents(current_user: User) -> list:
    """
    Get list of all agents accessible to the current user.

    Helper function for use by other routers that need agent access control.
    Returns list of agent dictionaries with ownership metadata.

    OPTIMIZED: Uses batch query to fetch all metadata in 2 queries total
    instead of 8-10 queries per agent (N+1 problem fix).
    """
    # Fast Docker call (no stats) - avoids slow Docker API calls
    all_agents = list_all_agents_fast()

    # Single query for user info
    user_data = db.get_user_by_username(current_user.username)
    if not user_data:
        return []

    is_admin = user_data["role"] == "admin"
    user_email = user_data.get("email")

    # SINGLE batch query for ALL agent metadata (N+1 fix)
    all_metadata = db.get_all_agent_metadata(user_email)

    accessible_agents = []
    for agent in all_agents:
        agent_dict = agent.dict() if hasattr(agent, 'dict') else dict(agent)
        agent_name = agent_dict.get("name")

        # Get metadata from batch result (not individual queries)
        metadata = all_metadata.get(agent_name)

        # Handle orphaned agents (exist in Docker but not in DB)
        if not metadata:
            # Only admin can see orphaned agents
            if not is_admin:
                continue
            # Show with minimal info
            agent_dict["owner"] = None
            agent_dict["is_owner"] = False
            agent_dict["is_shared"] = False
            agent_dict["is_system"] = False
            agent_dict["autonomy_enabled"] = False
            agent_dict["read_only_enabled"] = False
            agent_dict["github_repo"] = None
            agent_dict["memory_limit"] = None
            agent_dict["cpu_limit"] = None
            accessible_agents.append(agent_dict)
            continue

        # Access control check using batch data
        owner_username = metadata.get("owner_username")
        is_owner = owner_username == current_user.username
        is_shared = bool(metadata.get("is_shared_with_user"))

        # Skip if no access (not admin, not owner, not shared)
        if not (is_admin or is_owner or is_shared):
            continue

        # Populate agent dict from batch metadata
        agent_dict["owner"] = owner_username
        agent_dict["is_owner"] = is_owner
        agent_dict["is_shared"] = is_shared and not is_owner and not is_admin
        agent_dict["is_system"] = metadata.get("is_system", False)
        agent_dict["autonomy_enabled"] = metadata.get("autonomy_enabled", False)
        agent_dict["read_only_enabled"] = metadata.get("read_only_enabled", False)
        agent_dict["github_repo"] = metadata.get("github_repo")
        agent_dict["memory_limit"] = metadata.get("memory_limit")
        agent_dict["cpu_limit"] = metadata.get("cpu_limit")
        agent_dict["mcp_exposed"] = metadata.get("mcp_exposed", False)  # #846

        # Avatar URL (AVATAR-001)
        avatar_updated_at = metadata.get("avatar_updated_at")
        agent_dict["avatar_url"] = (
            f"/api/agents/{agent_name}/avatar?v={avatar_updated_at}"
            if avatar_updated_at else None
        )

        accessible_agents.append(agent_dict)

    return accessible_agents


def accessible_agent_names(current_user: User) -> Optional[List[str]]:
    """Return the set of agent names the caller may access, for SQL IN filters.

    Returns None for admins (no filter — see every agent).
    Returns a list (possibly empty) for non-admins.
    Used by fleet-level endpoints that filter schedule_executions by agent_name.
    """
    if current_user.role == "admin":
        return None
    return [a["name"] for a in get_accessible_agents(current_user)]


def sanitize_and_validate_name(name: str) -> str:
    """
    Sanitize and validate an agent name.

    Args:
        name: Raw agent name

    Returns:
        Sanitized agent name

    Raises:
        ValueError: If name is invalid after sanitization
    """
    sanitized = sanitize_agent_name(name)
    if not sanitized:
        raise ValueError("Invalid agent name - must contain at least one alphanumeric character")
    return sanitized


def get_agents_by_prefix(prefix: str) -> List[AgentStatus]:
    """
    Get all agents that match a base name prefix.

    Matches:
    - Exact name (e.g., "my-agent")
    - Versioned names (e.g., "my-agent-2", "my-agent-3")

    Args:
        prefix: Base agent name to match

    Returns:
        List of matching AgentStatus objects

    Uses list_all_agents_fast() for performance.
    """
    all_agents = list_all_agents_fast()
    matching = []

    for agent in all_agents:
        name = agent.name
        if name == prefix:
            matching.append(agent)
        elif name.startswith(f"{prefix}-"):
            # Check if suffix is a version number
            suffix = name[len(prefix) + 1:]
            if suffix.isdigit():
                matching.append(agent)

    return matching


def get_next_version_name(base_name: str) -> str:
    """
    Get next available version name for an agent.

    Pattern: {base-name} -> {base-name}-2 -> {base-name}-3

    Args:
        base_name: Base agent name

    Returns:
        Next available version name
    """
    existing = get_agents_by_prefix(base_name)

    if not existing:
        return base_name

    # Find highest version number
    max_version = 1
    for agent in existing:
        if agent.name == base_name:
            max_version = max(max_version, 1)
        elif agent.name.startswith(f"{base_name}-"):
            suffix = agent.name[len(base_name) + 1:]
            try:
                v = int(suffix)
                max_version = max(max_version, v)
            except ValueError:
                pass

    return f"{base_name}-{max_version + 1}"


def get_latest_version(base_name: str) -> Optional[AgentStatus]:
    """
    Get the most recent version of an agent.

    Args:
        base_name: Base agent name

    Returns:
        Most recent AgentStatus or None if no versions exist
    """
    existing = get_agents_by_prefix(base_name)
    if not existing:
        return None

    # Sort by version number (highest first)
    def get_version(agent):
        if agent.name == base_name:
            return 1
        suffix = agent.name[len(base_name) + 1:]
        try:
            return int(suffix)
        except ValueError:
            return 0

    return max(existing, key=get_version)


async def check_shared_folder_mounts_match(container, agent_name: str) -> bool:
    """
    Check if container's shared folder mounts match the current config.
    Returns True if mounts are correct, False if recreation needed.
    """
    config = db.get_shared_folder_config(agent_name)
    if not config:
        # No config - check that no shared mounts exist
        mounts = container.attrs.get("Mounts", [])
        for m in mounts:
            dest = m.get("Destination", "")
            if dest == "/home/developer/shared-out" or dest.startswith("/home/developer/shared-in/"):
                return False  # Has mounts but no config - needs recreation to remove
        return True

    mounts = container.attrs.get("Mounts", [])
    mount_dests = {m.get("Destination") for m in mounts}

    # Check expose mount
    if config.expose_enabled:
        if "/home/developer/shared-out" not in mount_dests:
            return False  # Config says expose, but mount missing
    else:
        if "/home/developer/shared-out" in mount_dests:
            return False  # Config says no expose, but mount exists

    # Check consume mounts
    if config.consume_enabled:
        available = db.get_available_shared_folders(agent_name)
        for source_agent in available:
            mount_path = db.get_shared_mount_path(source_agent)
            source_volume = db.get_shared_volume_name(source_agent)
            # Check if volume exists (async to avoid blocking)
            try:
                await volume_get(source_volume)
                if mount_path not in mount_dests:
                    return False  # Should be mounted but isn't
            except Exception:
                pass  # Volume doesn't exist yet, OK to skip

    return True


def check_api_key_env_matches(container, agent_name: str) -> bool:
    """
    Check if container's auth env vars match the current setting.
    Returns True if env matches config, False if recreation needed.

    SUB-002: When a subscription is assigned, CLAUDE_CODE_OAUTH_TOKEN must be
    present with the correct value, and ANTHROPIC_API_KEY must be absent.
    """
    # Get current env vars from container
    env_list = container.attrs.get("Config", {}).get("Env", [])
    env_dict = {e.split("=", 1)[0]: e.split("=", 1)[1] for e in env_list if "=" in e}

    has_api_key = "ANTHROPIC_API_KEY" in env_dict and env_dict["ANTHROPIC_API_KEY"]
    has_oauth_token = "CLAUDE_CODE_OAUTH_TOKEN" in env_dict and env_dict["CLAUDE_CODE_OAUTH_TOKEN"]

    # Subscription takes priority — if assigned, must have token and NOT have API key
    subscription_id = db.get_agent_subscription_id(agent_name)
    if subscription_id is not None:
        if has_api_key:
            return False
        if not has_oauth_token:
            return False
        # Verify token value matches DB
        expected_token = db.get_subscription_token(subscription_id)
        if expected_token and env_dict.get("CLAUDE_CODE_OAUTH_TOKEN") != expected_token:
            return False
        return True

    use_platform_key = db.get_use_platform_api_key(agent_name)
    if use_platform_key:
        # Should have the platform key and NOT have oauth token
        if has_oauth_token:
            return False
        expected_key = get_anthropic_api_key()
        current_key = env_dict.get("ANTHROPIC_API_KEY", "")
        return current_key == expected_key
    else:
        # Should NOT have the key or oauth token
        return not has_api_key and not has_oauth_token


def check_guardrails_env_matches(container, agent_name: str) -> bool:
    """
    GUARD-001: Verify the container's AGENT_GUARDRAILS env var matches the
    per-agent override stored in the database. Returns False when the user
    has updated guardrails via PUT /api/agents/{name}/guardrails but the
    container is still running with stale config — signals that start_agent
    should recreate the container so startup.sh re-renders the runtime JSON.
    """
    import json as _json

    env_list = container.attrs.get("Config", {}).get("Env", [])
    env_dict = {e.split("=", 1)[0]: e.split("=", 1)[1] for e in env_list if "=" in e}
    current = env_dict.get("AGENT_GUARDRAILS", "")

    desired = db.get_guardrails_config(agent_name) or {}
    expected = _json.dumps(desired) if desired else ""

    if not desired and not current:
        return True
    try:
        return _json.loads(current or "{}") == desired
    except (_json.JSONDecodeError, ValueError):
        return False


def check_agent_auth_token_env_matches(container, agent_name: str) -> bool:
    """#1159: verify the container's ``TRINITY_AGENT_AUTH_TOKEN`` matches the
    token derived for the agent's CURRENT name. Returns ``True`` if it matches,
    ``False`` (→ recreate) when the env is missing or stale.

    Catches two cases:
      * old-image / pre-#1159 container with no token → recreate injects it;
      * a renamed container still carrying ``derive(old_name)`` → recreate
        re-derives under the new name so it stops 401-ing once enforcement is on.

    Deterministic derive means the value this checks is exactly what the
    create/recreate inject writes, so the recreate converges in a single pass
    (no infinite recreate loop — same contract as the guardrails/PAT matchers).
    """
    env_list = container.attrs.get("Config", {}).get("Env", [])
    env_dict = {e.split("=", 1)[0]: e.split("=", 1)[1] for e in env_list if "=" in e}
    return env_dict.get("TRINITY_AGENT_AUTH_TOKEN") == derive_agent_token(agent_name)


def needs_per_agent_pat_injection(agent_name: str) -> bool:
    """#1264: True when the agent has a **per-agent** GitHub PAT configured AND
    git sync — i.e. the container SHOULD carry that token.

    Gated on ``db.get_agent_github_pat`` (the per-agent PAT only), NOT
    ``get_github_pat_for_agent`` (which falls back to the global platform PAT).
    Using the global fallback here would force-recreate / inject into every
    tokenless git agent the moment an operator sets a global PAT — that's #211's
    deliberate opt-in `.env` path, not this one's job, and is out of #1264 scope.

    Shared by the recreate matcher (:func:`check_github_pat_env_matches`) and the
    lifecycle recreate inject gate so the two predicates cannot drift — if they
    did, the matcher could keep requesting a recreate the inject never satisfies
    (infinite recreate loop).
    """
    return bool(db.get_agent_github_pat(agent_name)) and bool(db.get_git_config(agent_name))


def check_github_pat_env_matches(container, agent_name: str) -> bool:
    """
    Check if container's GITHUB_PAT env var matches the effective PAT for this agent.
    Returns True if env matches (or agent has no PAT), False if recreation needed.
    Checks per-agent PAT first, then falls back to platform PAT.
    """
    from routers.git import get_github_pat_for_agent

    env_list = container.attrs.get("Config", {}).get("Env", [])
    env_dict = {e.split("=", 1)[0]: e.split("=", 1)[1] for e in env_list if "=" in e}

    container_pat = env_dict.get("GITHUB_PAT")
    if not container_pat:
        # #1264: a tokenless container needs a recreate ONLY when a per-agent PAT
        # is configured (so creation/recreate will inject it). A global-only PAT
        # must NOT force tokenless agents to recreate — see
        # needs_per_agent_pat_injection. Recreate converges in one pass because
        # the lifecycle inject gate uses the SAME predicate.
        return not needs_per_agent_pat_injection(agent_name)

    # Container already has a token — keep it in sync with the effective PAT
    # (per-agent first, then the global fallback for already-tokened containers).
    current_pat = get_github_pat_for_agent(agent_name)
    if not current_pat:
        # No PAT configured anywhere — can't update, leave as-is
        return True

    return container_pat == current_pat


def check_resource_limits_match(container, agent_name: str) -> bool:
    """
    Check if container's resource limits match the current database settings.
    Returns True if resources match, False if recreation needed.
    """
    # Get DB settings (may be None if using template defaults)
    db_limits = db.get_resource_limits(agent_name)

    # Get current container limits from labels (stored during creation)
    labels = container.attrs.get("Config", {}).get("Labels", {})
    current_memory = labels.get("trinity.memory", "4g")
    current_cpu = labels.get("trinity.cpu", "2")

    if db_limits is None:
        # No custom limits set in DB - container should use template defaults
        # Don't trigger recreation just because DB is empty
        return True

    # Compare DB limits with current container limits
    expected_memory = db_limits.get("memory") or current_memory
    expected_cpu = db_limits.get("cpu") or current_cpu

    if expected_memory != current_memory:
        logger.info(f"Resource mismatch for {agent_name}: memory {current_memory} -> {expected_memory}")
        return False

    if expected_cpu != current_cpu:
        logger.info(f"Resource mismatch for {agent_name}: cpu {current_cpu} -> {expected_cpu}")
        return False

    return True


def check_full_capabilities_match(container, agent_name: str) -> bool:
    """
    Check if container's full_capabilities setting matches the current system-wide setting.
    Returns True if capabilities match, False if recreation needed.

    When full_capabilities=True:
    - Container runs with Docker default capabilities
    - Can install packages with apt-get, use sudo

    When full_capabilities=False:
    - Container runs with restricted capabilities
    - More secure but cannot install packages

    Note: This is now a system-wide setting, not per-agent.
    """
    # Get system-wide setting
    system_full_caps = get_agent_full_capabilities()

    # Get current container setting from labels
    labels = container.attrs.get("Config", {}).get("Labels", {})
    current_full_caps = labels.get("trinity.full-capabilities", "false").lower() == "true"

    if system_full_caps != current_full_caps:
        logger.info(f"Capabilities mismatch for {agent_name}: container={current_full_caps} -> system={system_full_caps}")
        return False

    return True
