"""
Agent Service Lifecycle - Agent start/stop and configuration management.

Contains functions for starting, stopping, and reconfiguring agents.
"""
import asyncio
import logging
import os
import time

import docker
import httpx

from fastapi import HTTPException

from database import db
from services.docker_service import (
    docker_client,
    get_agent_container,
    get_next_available_port,
)
from services.docker_utils import (
    container_stop, container_remove, container_start, container_reload,
    volume_get, volume_create, containers_run
)
from services.agent_service.helpers import validate_base_image
from services.agent_runtime_state import clear_agent_breakers
from services.settings_service import get_anthropic_api_key, get_github_pat, get_agent_full_capabilities, get_agent_default_resources
from services.skill_service import skill_service
from .helpers import check_shared_folder_mounts_match, check_api_key_env_matches, check_github_pat_env_matches, check_resource_limits_match, check_full_capabilities_match, check_guardrails_env_matches, check_agent_auth_token_env_matches, is_claude_runtime
from services.agent_auth import derive_agent_token
from utils.helpers import utc_now_iso
from .file_sharing import check_public_folder_mount_matches
from .read_only import inject_read_only_hooks, remove_read_only_hooks

logger = logging.getLogger(__name__)


# =============================================================================
# Readiness Probe (#406)
# =============================================================================

# Docker reporting a container as "running" precedes the in-container FastAPI
# server accepting connections by several seconds. Under multi-agent deploys,
# the downstream credential-injection retry window exhausts before the server
# is up. Gate post-start injections on HTTP readiness to close the race.

AGENT_READINESS_TIMEOUT_S = int(os.getenv("AGENT_READINESS_TIMEOUT_S", "60"))
AGENT_READINESS_POLL_INTERVAL_S = float(os.getenv("AGENT_READINESS_POLL_INTERVAL_S", "1.0"))


async def wait_for_agent_ready(
    agent_name: str,
    timeout_s: int = AGENT_READINESS_TIMEOUT_S,
    poll_interval_s: float = AGENT_READINESS_POLL_INTERVAL_S,
) -> bool:
    """Poll the agent's /health endpoint until it returns 200 or timeout.

    Returns True if ready, False on timeout. Never raises — callers treat a
    False return as "proceed anyway and let downstream retries cope."
    """
    url = f"http://agent-{agent_name}:8000/health"
    deadline = time.monotonic() + timeout_s
    attempt = 0
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            attempt += 1
            try:
                r = await client.get(url, timeout=2.0)
                if r.status_code == 200:
                    if attempt > 1:
                        logger.info(
                            f"Agent {agent_name} became ready after {attempt} poll(s)"
                        )
                    return True
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout):
                pass
            except Exception as e:  # noqa: BLE001 — readiness probe must never bubble
                logger.debug(
                    f"Readiness probe for {agent_name} hit unexpected error: {e}"
                )
            await asyncio.sleep(poll_interval_s)

    logger.warning(
        f"Agent {agent_name} did not become ready within {timeout_s}s "
        f"(polled {attempt} time(s)) — proceeding anyway"
    )
    return False


# =============================================================================
# Container Security Capability Sets — see capabilities.py for definitions
# =============================================================================
# Re-exported from .capabilities so that test code (and other callers
# that only need the constants) can import them without dragging the
# docker / fastapi / database transitive imports of this module.
from .capabilities import (  # noqa: F401
    RESTRICTED_CAPABILITIES,
    FULL_CAPABILITIES,
    PROHIBITED_CAPABILITIES,
    AGENT_TMPFS_MOUNT,
    AGENT_DEFAULT_TMPDIR,
    normalize_cpu,
    normalize_memory,
)


async def inject_assigned_credentials(agent_name: str, max_retries: int = 3, retry_delay: float = 2.0) -> dict:
    """
    Import credentials from encrypted .credentials.enc file on agent startup.

    CRED-002: Credentials are now stored as encrypted files in the agent's
    workspace (committed to git). On startup, we try to import from
    .credentials.enc if it exists.

    Args:
        agent_name: Name of the agent
        max_retries: Number of retries for connection
        retry_delay: Seconds between retries

    Returns:
        dict with injection status
    """
    import asyncio
    from database import db
    from services.credential_encryption import (
        CredentialsFileNotFoundError,
        get_credential_encryption_service,
    )

    # #612: subscription-mode agents authenticate via CLAUDE_CODE_OAUTH_TOKEN
    # env var set at container creation (SUB-002). They do not need (and
    # typically do not have) a .credentials.enc file. Attempting the import
    # would either silently succeed-noop or surface a misleading "failed"
    # status that prompts operators to take corrective action (re-assigning
    # the subscription, recreating the container) — when nothing is wrong.
    # Short-circuit to a clear skipped status before the import path runs.
    if db.get_agent_subscription_id(agent_name):
        logger.debug(
            f"Skipping .credentials.enc import for {agent_name}: "
            f"subscription mode (auth via CLAUDE_CODE_OAUTH_TOKEN env var)"
        )
        return {
            "status": "skipped",
            "reason": "subscription_mode",
            "detail": "agent authenticates via CLAUDE_CODE_OAUTH_TOKEN; "
                      "file-based credential injection is not used",
        }

    try:
        encryption_service = get_credential_encryption_service()
    except ValueError as e:
        # No encryption key configured - this is optional
        logger.debug(f"Credential encryption not configured: {e}")
        return {"status": "skipped", "reason": "encryption_not_configured"}

    # Try to import from .credentials.enc with retries
    last_error = None
    for attempt in range(max_retries):
        try:
            files = await encryption_service.import_to_agent(agent_name)
            if files:
                logger.info(f"Imported {len(files)} credential file(s) from .credentials.enc into {agent_name}")
                return {
                    "status": "success",
                    "credential_count": len(files),
                    "files": list(files.keys())
                }
            else:
                return {"status": "skipped", "reason": "no_credentials_enc_file"}

        except CredentialsFileNotFoundError:
            # #612: ``.credentials.enc`` is absent. Common case for fresh
            # agents that haven't been through an export cycle yet — a clean
            # skip, not a failure. (Was previously caught by a fragile
            # substring match against the error message; the explicit
            # subclass makes the intent unambiguous.)
            logger.debug(f"No .credentials.enc found for agent {agent_name}")
            return {"status": "skipped", "reason": "no_credentials_enc_file"}

        except ValueError as e:
            # Other ValueError shapes (encrypted blob malformed, decrypt
            # failure, …) — keep retrying because some of them are
            # transient (e.g. agent HTTP not yet ready under multi-agent
            # cold start, #406).
            last_error = str(e)

        except Exception as e:
            last_error = str(e)
            logger.warning(f"Credential import attempt {attempt + 1} failed: {last_error}")

        if attempt < max_retries - 1:
            await asyncio.sleep(retry_delay)

    logger.error(f"Failed to import credentials into agent {agent_name} after {max_retries} attempts: {last_error}")
    return {"status": "failed", "error": last_error}


async def inject_assigned_skills(agent_name: str) -> dict:
    """
    Inject assigned skills into a running agent.

    This is called after agent startup to push any skills that were
    assigned to this agent in the Skills tab.

    Args:
        agent_name: Name of the agent

    Returns:
        dict with injection status
    """
    from database import db

    # Get assigned skills
    skill_names = db.get_agent_skill_names(agent_name)

    if not skill_names:
        logger.debug(f"No assigned skills for agent {agent_name}")
        return {"status": "skipped", "reason": "no_skills"}

    logger.info(f"Injecting {len(skill_names)} skills into agent {agent_name}: {skill_names}")

    # Inject skills
    result = await skill_service.inject_skills(agent_name, skill_names)

    if result.get("success"):
        return {
            "status": "success",
            "skills_injected": result.get("skills_injected", 0)
        }
    else:
        return {
            "status": "partial" if result.get("skills_injected", 0) > 0 else "failed",
            "skills_injected": result.get("skills_injected", 0),
            "skills_failed": result.get("skills_failed", 0),
            "results": result.get("results", {})
        }


async def start_agent_internal(agent_name: str) -> dict:
    """
    Internal function to start an agent.

    Used by both the API endpoint and system deployment.
    Triggers Trinity meta-prompt injection.

    Args:
        agent_name: Name of the agent to start

    Returns:
        dict with start status and trinity_injection result

    Raises:
        HTTPException: If agent not found or start fails
    """
    container = get_agent_container(agent_name)
    if not container:
        # #1559: no container, but a live (non-soft-deleted) agent_ownership row
        # means this is a recovered agent whose container was removed at
        # soft-delete. Rebuild it from persisted config + the surviving workspace
        # volume instead of dead-ending on 404 (the soft-delete recovery gap).
        # A genuinely nonexistent agent (no ownership row) still 404s.
        owner = db.get_agent_owner(agent_name)
        if not owner:
            raise HTTPException(status_code=404, detail="Agent not found")
        container = await recreate_missing_container(agent_name)

    # Check if container needs recreation for shared folders, API key, resource limits, or capabilities
    await container_reload(container)
    was_already_running = getattr(container, "status", None) == "running"
    shared_folder_match = await check_shared_folder_mounts_match(container, agent_name)
    needs_recreation = (
        not shared_folder_match or
        not check_public_folder_mount_matches(container, agent_name) or
        not check_api_key_env_matches(container, agent_name) or
        not check_github_pat_env_matches(container, agent_name) or
        not check_resource_limits_match(container, agent_name) or
        not check_full_capabilities_match(container, agent_name) or
        not check_guardrails_env_matches(container, agent_name) or
        not check_agent_auth_token_env_matches(container, agent_name)
    )

    # #1560: the heartbeat markers and both circuit breakers are keyed by agent
    # NAME, not by container identity, so a recreated container inherits the
    # verdict recorded against its predecessor — a fresh, healthy agent is
    # fast-failed with "agent is unhealthy" without ever being contacted. Any
    # config drift above (subscription switch, resource change, auth-token
    # rotation, guardrails edit) recreates the container, and a fleet-wide
    # rotation recreates every agent at once, so this is the load-bearing clear.
    #
    # Runs BEFORE the recreate/start below, not after: `recreate_container_with_
    # updated_config` starts the replacement via `containers_run(detach=True)`, so
    # clearing afterwards would leave a window in which a concurrent dispatch
    # reads the predecessor's verdict against a container that is already up.
    #
    # Gated on the container having actually changed or come up: a no-op start of
    # an already-running agent must NOT reset a breaker, otherwise re-issuing
    # `start` would let an operator defeat the breaker protecting a genuinely
    # wedged agent. Slots are deliberately untouched here — the container is live
    # (see services/agent_runtime_state.py).
    if needs_recreation or not was_already_running:
        clear_agent_breakers(agent_name)

    if needs_recreation:
        # Recreate container with updated config
        # Use system user for internal operations
        await recreate_container_with_updated_config(agent_name, container, "system")
        container = get_agent_container(agent_name)

    await container_start(container)

    # NOTE: Trinity platform instructions are now injected at runtime via
    # --append-system-prompt on every chat/task request (Issue #136).
    # No file-based injection needed on startup.

    # Skip credential/skill injection when the container was already running
    # and we didn't recreate it (#421). The workspace volume persists `.env`
    # and `.claude/skills/` across container starts, so re-injection on an
    # idempotent start is redundant and generates connection-error noise when
    # the agent is under load and can't accept new HTTP connections.
    skip_injection = was_already_running and not needs_recreation

    if skip_injection:
        credentials_result = {
            "status": "skipped",
            "reason": "container_already_running",
        }
        credentials_status = "skipped"
        skills_result = {
            "status": "skipped",
            "reason": "container_already_running",
        }
        skills_status = "skipped"
    else:
        # Gate post-start injections on HTTP readiness — Docker "running"
        # precedes FastAPI "listening" by several seconds, and the downstream
        # retry window is too short under multi-agent deploys (#406).
        await wait_for_agent_ready(agent_name)

        # Inject assigned credentials from the Credentials page.
        # trinity-enterprise#69: ephemeral ghosts get NO automatic credential
        # injection (no-credentials-by-default for arbitrary/untrusted
        # workspaces); a human can still inject explicitly via the
        # credentials endpoint, which is human-only under Part 2.
        # isinstance-dict guard: the accessor's contract is Optional[Dict] —
        # anything else (incl. a test double) must take the normal inject path.
        try:
            _eph_info = db.get_agent_ephemeral_info(agent_name)
        except Exception:
            _eph_info = None
        if isinstance(_eph_info, dict) and _eph_info.get("is_ephemeral"):
            credentials_result = {"status": "skipped", "reason": "ephemeral_agent"}
        else:
            credentials_result = await inject_assigned_credentials(agent_name)
        credentials_status = credentials_result.get("status", "unknown")

        # Inject assigned skills from the Skills page
        skills_result = await inject_assigned_skills(agent_name)
        skills_status = skills_result.get("status", "unknown")

    # Sync read-only config file on every start so the baked-in guard always
    # reflects the current DB state — prevents stale enabled:true config from
    # persisting on the volume after the user disables read-only mode (#887).
    read_only_result = {"status": "skipped", "reason": "unknown"}
    read_only_data = db.get_read_only_mode(agent_name)
    try:
        if read_only_data.get("enabled"):
            result = await inject_read_only_hooks(agent_name, read_only_data.get("config"))
        else:
            result = await remove_read_only_hooks(agent_name)
        read_only_result = {"status": "success" if result.get("success") else "failed", **result}
    except Exception as e:
        logger.warning(f"Failed to sync read-only config for agent {agent_name}: {e}")
        read_only_result = {"status": "failed", "error": str(e)}

    return {
        "message": f"Agent {agent_name} started",
        "credentials_injection": credentials_status,
        "credentials_result": credentials_result,
        "skills_injection": skills_status,
        "skills_result": skills_result,
        "read_only_injection": read_only_result.get("status", "unknown"),
        "read_only_result": read_only_result
    }


async def recreate_container_with_updated_config(agent_name: str, old_container, owner_username: str):
    """
    Recreate an agent container with updated configuration.
    Handles shared folder mounts and API key settings.
    Preserves the agent's workspace volume and other configuration.
    """
    # Extract configuration from old container
    old_config = old_container.attrs.get("Config", {})
    old_host_config = old_container.attrs.get("HostConfig", {})

    # Get key settings
    image = old_config.get("Image", "trinity-agent-base:latest")
    # SEC-172: Validate image on container recreation (defense in depth)
    validate_base_image(image)
    env_vars = {e.split("=", 1)[0]: e.split("=", 1)[1] for e in old_config.get("Env", []) if "=" in e}
    labels = old_config.get("Labels", {})

    # #1098: redirect scratch (pip/npm/build) off the 100 MB noexec /tmp tmpfs
    # onto the disk-backed home volume. setdefault so a template/user-set TMPDIR
    # carried on the existing container wins; old-image containers (no TMPDIR)
    # pick up the default on this recreate.
    env_vars.setdefault('TMPDIR', AGENT_DEFAULT_TMPDIR)

    # Update auth env vars based on current setting (SUB-002).
    # Claude Code prioritizes ANTHROPIC_API_KEY over CLAUDE_CODE_OAUTH_TOKEN,
    # so when a subscription is assigned we must remove the API key and set
    # the token env var instead.
    #
    # This whole juggle is Claude-only: subscriptions are Claude-OAuth tokens.
    # Non-Claude runtimes (Gemini, Codex) authenticate from their own .env
    # (CRED-002) and must NEVER receive a Claude subscription token on recreate,
    # even if a subscription row somehow exists for them (#1187 decision 7).
    _runtime = (
        env_vars.get('AGENT_RUNTIME')
        or labels.get('trinity.agent-runtime')
        or 'claude-code'
    )
    _is_claude_runtime = is_claude_runtime(_runtime)
    subscription_id = db.get_agent_subscription_id(agent_name)
    has_subscription = subscription_id is not None
    use_platform_key = db.get_use_platform_api_key(agent_name)

    if not _is_claude_runtime:
        # Non-Claude: leave the agent's own credentials in place; never inject a
        # Claude token.
        env_vars.pop('CLAUDE_CODE_OAUTH_TOKEN', None)
    elif has_subscription:
        # Subscription assigned — inject token, remove API key
        token = db.get_subscription_token(subscription_id)
        if token:
            env_vars['CLAUDE_CODE_OAUTH_TOKEN'] = token
        env_vars.pop('ANTHROPIC_API_KEY', None)
    elif use_platform_key:
        # No subscription, use platform API key
        env_vars['ANTHROPIC_API_KEY'] = get_anthropic_api_key()
        env_vars.pop('CLAUDE_CODE_OAUTH_TOKEN', None)
    else:
        # No subscription, no platform key — user will auth in terminal
        env_vars.pop('ANTHROPIC_API_KEY', None)
        env_vars.pop('CLAUDE_CODE_OAUTH_TOKEN', None)

    # Update GITHUB_PAT using per-agent PAT first, then platform PAT.
    _per_agent_pat = bool(db.get_agent_github_pat(agent_name)) and bool(db.get_git_config(agent_name))
    if env_vars.get('GITHUB_PAT') or _per_agent_pat:
        from routers.git import get_github_pat_for_agent
        current_pat = get_github_pat_for_agent(agent_name)
        if current_pat:
            env_vars['GITHUB_PAT'] = current_pat
    # #1574: mirror the resolved PAT onto GH_TOKEN/GITHUB_TOKEN so the `gh` CLI +
    # REST API authenticate too — always tracking the final GITHUB_PAT, and never
    # set when no token resolved (identical gating, no empty/broken credential).
    _resolved_pat = env_vars.get('GITHUB_PAT')
    if _resolved_pat:
        env_vars['GH_TOKEN'] = _resolved_pat
        env_vars['GITHUB_TOKEN'] = _resolved_pat
    else:
        env_vars.pop('GH_TOKEN', None)
        env_vars.pop('GITHUB_TOKEN', None)
    # NB: gated on db.get_agent_github_pat (NOT the global fallback) so a global-
    # only PAT is never injected into a previously-tokenless container (#211's
    # opt-in path); kept in sync with the recreate matcher so the two converge.
    # Inlined via db rather than importing the helper, so a test stubbing
    # services.agent_service.helpers can't break this module's import (#1271 CI).

    # GUARD-001: re-serialise guardrails overrides into env so startup.sh
    # can render the runtime config with the latest values.
    guardrails_override = db.get_guardrails_config(agent_name)
    if guardrails_override:
        import json as _json
        env_vars['AGENT_GUARDRAILS'] = _json.dumps(guardrails_override)
    else:
        env_vars.pop('AGENT_GUARDRAILS', None)

    # #1369: refresh the operator-configurable headless stall-watchdog ceiling
    # from the CURRENT backend env on every recreate (set or clear, mirroring the
    # guardrails idiom above), so changing/unsetting AGENT_TOOL_STALL_LIMIT_S
    # takes effect on recreate rather than persisting a stale baked value.
    _stall_limit = (os.getenv('AGENT_TOOL_STALL_LIMIT_S') or '').strip()
    if _stall_limit:
        env_vars['AGENT_TOOL_STALL_LIMIT_S'] = _stall_limit
    else:
        env_vars.pop('AGENT_TOOL_STALL_LIMIT_S', None)

    # #1159: refresh the per-agent auth token. Deterministic from agent_name, so
    # this re-derives under the CURRENT name — the load-bearing part of the
    # rename fix (a renamed container otherwise keeps derive(old_name) and 401s
    # once enforcement is on). check_agent_auth_token_env_matches forces this
    # recreate whenever the running token is missing or stale.
    env_vars['TRINITY_AGENT_AUTH_TOKEN'] = derive_agent_token(agent_name)

    # Get port from labels
    ssh_port = int(labels.get("trinity.ssh-port", 2222))

    # Get resource limits: per-agent DB override → container labels → system defaults → hardcoded
    db_limits = db.get_resource_limits(agent_name)
    system_defaults = get_agent_default_resources()
    if db_limits:
        cpu = db_limits.get("cpu") or labels.get("trinity.cpu") or system_defaults["cpu"]
        memory = db_limits.get("memory") or labels.get("trinity.memory") or system_defaults["memory"]
    else:
        cpu = labels.get("trinity.cpu") or system_defaults["cpu"]
        memory = labels.get("trinity.memory") or system_defaults["memory"]

    # #1197: validate/normalize before they reach Docker (int(cpu) NanoCpus /
    # mem_limit). A stale label or DB override carrying a non-integer cpu or a
    # Kubernetes-style memory would otherwise crash recreate with an opaque
    # ValueError; fail with a clear message instead.
    cpu = normalize_cpu(cpu, system_defaults["cpu"])
    memory = normalize_memory(memory, system_defaults["memory"])

    # Update labels with new resource limits for future reference
    labels["trinity.cpu"] = cpu
    labels["trinity.memory"] = memory

    # Get full_capabilities from system-wide setting (not per-agent)
    full_capabilities = get_agent_full_capabilities()

    # Update label to reflect current setting
    labels["trinity.full-capabilities"] = str(full_capabilities).lower()

    # Stop and remove old container
    try:
        await container_stop(old_container)
    except Exception:
        pass
    await container_remove(old_container)

    # Build new volume configuration
    agent_volume_name = f"agent-{agent_name}-workspace"

    # Start with base volumes - get existing bind mounts
    old_mounts = old_container.attrs.get("Mounts", [])
    volumes = {}

    for m in old_mounts:
        dest = m.get("Destination", "")
        # Skip shared folder mounts - we'll add the correct ones
        if dest == "/home/developer/shared-out" or dest.startswith("/home/developer/shared-in/"):
            continue
        # Skip public mount — re-added below based on current file_sharing_enabled flag.
        if dest == db.get_public_mount_path():
            continue
        # Keep other mounts
        if m.get("Type") == "bind":
            volumes[m.get("Source")] = {"bind": dest, "mode": "rw" if m.get("RW", True) else "ro"}
        elif m.get("Type") == "volume":
            vol_name = m.get("Name")
            if vol_name:
                volumes[vol_name] = {"bind": dest, "mode": "rw" if m.get("RW", True) else "ro"}

    return await _provision_folders_and_run_agent_container(
        agent_name,
        image=image,
        env_vars=env_vars,
        labels=labels,
        base_volumes=volumes,
        ssh_port=ssh_port,
        cpu=cpu,
        memory=memory,
        full_capabilities=full_capabilities,
    )


async def _provision_folders_and_run_agent_container(
    agent_name: str,
    *,
    image: str,
    env_vars: dict,
    labels: dict,
    base_volumes: dict,
    ssh_port: int,
    cpu,
    memory,
    full_capabilities: bool,
):
    """Shared tail for every container (re)build: add DB-driven shared/public
    folder mounts onto ``base_volumes`` then run the container with the full
    security posture (cap-drop ALL, AppArmor, noexec tmpfs, resource limits).

    Extracted so `recreate_container_with_updated_config` (spec from the old
    container) and `recreate_missing_container` (spec from persisted DB state
    after a soft-delete recovery, #1559) share one canonical run path — the
    security envelope can never drift between them (AC: "goes through the
    supported creation path, not a hand-rolled docker run").
    """
    volumes = dict(base_volumes)

    # Add shared folder mounts based on current config
    shared_config = db.get_shared_folder_config(agent_name)
    if shared_config:
        if shared_config.expose_enabled:
            shared_volume_name = db.get_shared_volume_name(agent_name)
            volume_created = False
            try:
                await volume_get(shared_volume_name)
            except docker.errors.NotFound:
                await volume_create(
                    name=shared_volume_name,
                    labels={
                        'trinity.platform': 'agent-shared',
                        'trinity.agent-name': agent_name
                    }
                )
                volume_created = True

            # Fix ownership of new volumes (Docker creates them as root)
            if volume_created:
                try:
                    await containers_run(
                        'alpine',
                        command='chown 1000:1000 /shared',
                        volumes={shared_volume_name: {'bind': '/shared', 'mode': 'rw'}},
                        remove=True
                    )
                except Exception as e:
                    logger.warning(f"Could not fix shared volume ownership: {e}")

            volumes[shared_volume_name] = {'bind': '/home/developer/shared-out', 'mode': 'rw'}

        if shared_config.consume_enabled:
            available_folders = db.get_available_shared_folders(agent_name)
            for source_agent in available_folders:
                source_volume = db.get_shared_volume_name(source_agent)
                mount_path = db.get_shared_mount_path(source_agent)
                try:
                    await volume_get(source_volume)
                    volumes[source_volume] = {'bind': mount_path, 'mode': 'rw'}
                except docker.errors.NotFound:
                    pass

    # Add public folder mount based on current file_sharing_enabled flag
    # (FILES-001 Step 2). Mirrors the shared-folders expose pattern.
    if db.get_file_sharing_enabled(agent_name):
        public_volume_name = db.get_public_volume_name(agent_name)
        public_volume_created = False
        try:
            await volume_get(public_volume_name)
        except docker.errors.NotFound:
            await volume_create(
                name=public_volume_name,
                labels={
                    'trinity.platform': 'agent-public',
                    'trinity.agent-name': agent_name,
                },
            )
            public_volume_created = True

        if public_volume_created:
            try:
                await containers_run(
                    'alpine',
                    command='chown 1000:1000 /public',
                    volumes={public_volume_name: {'bind': '/public', 'mode': 'rw'}},
                    remove=True,
                )
            except Exception as e:
                logger.warning(f"Could not fix public volume ownership: {e}")

        volumes[public_volume_name] = {'bind': db.get_public_mount_path(), 'mode': 'rw'}

    # Create new container with security settings
    # Security principle: ALWAYS apply baseline security, even in full_capabilities mode
    # - Always drop ALL caps, then add back only what's needed
    # - Always apply AppArmor profile
    # - Always apply noexec,nosuid to /tmp
    new_container = await containers_run(
        image,
        detach=True,
        name=f"agent-{agent_name}",
        ports={'22/tcp': ssh_port},
        volumes=volumes,
        environment=env_vars,
        labels=labels,
        # Always apply AppArmor for additional sandboxing
        security_opt=['apparmor:docker-default'],
        # Always drop ALL capabilities first (defense in depth)
        cap_drop=['ALL'],
        # Add back only the capabilities needed for the mode
        cap_add=FULL_CAPABILITIES if full_capabilities else RESTRICTED_CAPABILITIES,
        read_only=False,
        # Always apply noexec,nosuid to /tmp for security (#1098: scratch is
        # redirected off this tiny tmpfs via the TMPDIR env var above).
        tmpfs=AGENT_TMPFS_MOUNT,
        network='trinity-agent-network',
        mem_limit=memory,
        # #1126: nano_cpus (Linux CFS quota → HostConfig.NanoCpus), NOT
        # cpu_count — docker-py's cpu_count maps to the Windows-only CpuCount
        # and leaves NanoCpus=0 on Linux, so the CPU limit was never enforced.
        nano_cpus=int(cpu) * 1_000_000_000,
    )

    logger.info(f"Recreated container for agent {agent_name} with updated configuration")
    return new_container


async def _read_template_yaml_from_volume(agent_name: str) -> dict:
    """Read the agent's `template.yaml` off its persisted workspace volume
    without a running container (#1559).

    After a soft-delete the container (and its `trinity.agent-type` /
    `trinity.agent-runtime` labels) is gone, but the workspace volume — which
    carries the committed `template.yaml` — survives. A throwaway, network-less
    base-image container `cat`s the file. Tolerant: any failure (missing file,
    unparseable) returns `{}` so the caller falls back to safe defaults.
    """
    volume_name = f"agent-{agent_name}-workspace"
    try:
        out = await containers_run(
            "trinity-agent-base:latest",
            command=["cat", "/home/developer/template.yaml"],
            volumes={volume_name: {"bind": "/home/developer", "mode": "ro"}},
            remove=True,
            network_disabled=True,
        )
        text = out.decode("utf-8") if isinstance(out, (bytes, bytearray)) else str(out)
        import yaml as _yaml
        data = _yaml.safe_load(text)
        return data if isinstance(data, dict) else {}
    except Exception as e:  # noqa: BLE001 — best-effort; defaults cover the gap
        logger.warning(
            "Could not read template.yaml from volume for %s: %s", agent_name, e
        )
        return {}


async def recreate_missing_container(agent_name: str):
    """Rebuild a container for an existing agent that has **no** container —
    the soft-delete recovery gap (#1559).

    Soft delete removes the container but keeps the `agent-<name>-workspace`
    volume and every relational row. Recovery clears `deleted_at` but nothing
    could bring the agent back online: `start` 404'd (no container) and
    `recreate_container_with_updated_config` needs an `old_container` to copy
    config from. This reconstructs the container spec from persisted state
    (`agent_ownership` + `agent_git_config` + the volume's `template.yaml`),
    reuses the existing volume (never recreated — no data loss), and runs it
    through the same `_provision_folders_and_run_agent_container` tail as a
    normal recreate, so the full security posture (cap-drop, no-new-privileges
    via AppArmor+cap model, noexec tmpfs, derived TRINITY_AGENT_AUTH_TOKEN) is
    identical. startup.sh sees `.git` already on the volume and skips the clone.

    Caller must confirm a live `agent_ownership` row exists first — this does
    NOT create ownership/child rows, only the container.
    """
    image = "trinity-agent-base:latest"
    validate_base_image(image)

    tmpl = await _read_template_yaml_from_volume(agent_name)
    agent_type = tmpl.get("type") or "business-assistant"
    runtime_cfg = tmpl.get("runtime", {})
    if isinstance(runtime_cfg, dict):
        runtime = (runtime_cfg.get("type") or "claude-code").lower()
        runtime_model = runtime_cfg.get("model") or ""
    elif isinstance(runtime_cfg, str):
        runtime = runtime_cfg.lower()
        runtime_model = ""
    else:
        runtime = "claude-code"
        runtime_model = ""
    template_name = tmpl.get("_template") or ""  # display-only; label field

    # --- Resource limits: per-agent DB override → system defaults ---
    system_defaults = get_agent_default_resources()
    db_limits = db.get_resource_limits(agent_name) or {}
    cpu = normalize_cpu(db_limits.get("cpu") or system_defaults["cpu"], system_defaults["cpu"])
    memory = normalize_memory(db_limits.get("memory") or system_defaults["memory"], system_defaults["memory"])
    full_capabilities = get_agent_full_capabilities()
    ssh_port = get_next_available_port()

    # --- Base env (mirrors crud.create_agent_internal's baked set) ---
    env_vars = {
        "AGENT_NAME": agent_name,
        "AGENT_TYPE": agent_type,
        "CREDENTIALS_FILE": "/config/credentials.json",
        "ENABLE_SSH": "true",
        "ENABLE_AGENT_UI": "true",
        "AGENT_SERVER_PORT": "8000",
        "AGENT_RUNTIME": runtime,
        "AGENT_RUNTIME_MODEL": runtime_model,
        "TMPDIR": AGENT_DEFAULT_TMPDIR,
    }

    # OpenTelemetry (default on) — same wiring as create.
    if os.getenv("OTEL_ENABLED", "1") == "1":
        env_vars["CLAUDE_CODE_ENABLE_TELEMETRY"] = "1"
        env_vars["OTEL_METRICS_EXPORTER"] = os.getenv("OTEL_METRICS_EXPORTER", "otlp")
        env_vars["OTEL_LOGS_EXPORTER"] = os.getenv("OTEL_LOGS_EXPORTER", "otlp")
        env_vars["OTEL_EXPORTER_OTLP_PROTOCOL"] = os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
        env_vars["OTEL_EXPORTER_OTLP_ENDPOINT"] = os.getenv("OTEL_COLLECTOR_ENDPOINT", "http://trinity-otel-collector:4317")
        env_vars["OTEL_METRIC_EXPORT_INTERVAL"] = os.getenv("OTEL_METRIC_EXPORT_INTERVAL", "60000")

    # Mint a fresh agent-scoped MCP key (the old key's plaintext is unrecoverable
    # — only the hash is stored). Same wiring as create: enables collab +
    # heartbeat. Owner resolved from the ownership row.
    owner = db.get_agent_owner(agent_name) or {}
    owner_username = owner.get("owner_username") or owner.get("username")
    try:
        if owner_username:
            agent_mcp_key = db.create_agent_mcp_api_key(
                agent_name, owner_username, description="recovery-recreate"
            )
            if agent_mcp_key:
                env_vars["TRINITY_MCP_URL"] = os.getenv("TRINITY_MCP_URL", "http://mcp-server:8080/mcp")
                env_vars["TRINITY_MCP_API_KEY"] = agent_mcp_key.api_key
                env_vars["TRINITY_BACKEND_URL"] = os.getenv("TRINITY_BACKEND_URL", "http://backend:8000")
    except Exception as e:  # noqa: BLE001 — non-fatal; agent still boots
        logger.warning("Could not mint MCP key on recovery recreate for %s: %s", agent_name, e)

    # Auth env (subscription token / platform key), GitHub PAT, guardrails,
    # stall-limit, per-agent auth token — reuse the exact create/recreate rules.
    _apply_persisted_auth_env(agent_name, env_vars, runtime)

    labels = {
        "trinity.platform": "agent",
        "trinity.agent-name": agent_name,
        "trinity.agent-type": agent_type,
        "trinity.ssh-port": str(ssh_port),
        "trinity.cpu": cpu,
        "trinity.memory": memory,
        "trinity.created": utc_now_iso(),
        "trinity.template": template_name,
        "trinity.agent-runtime": runtime,
        "trinity.full-capabilities": str(full_capabilities).lower(),
    }

    base_volumes = {f"agent-{agent_name}-workspace": {"bind": "/home/developer", "mode": "rw"}}

    logger.info("Rebuilding missing container for recovered agent %s (#1559)", agent_name)
    return await _provision_folders_and_run_agent_container(
        agent_name,
        image=image,
        env_vars=env_vars,
        labels=labels,
        base_volumes=base_volumes,
        ssh_port=ssh_port,
        cpu=cpu,
        memory=memory,
        full_capabilities=full_capabilities,
    )


def _apply_persisted_auth_env(agent_name: str, env_vars: dict, runtime: str) -> None:
    """Set auth-related env from persisted DB state, mirroring the refresh block
    in `recreate_container_with_updated_config` (subscription token vs platform
    key, per-agent GitHub PAT, guardrails, stall-limit, derived agent token)."""
    if not is_claude_runtime(runtime):
        env_vars.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        env_vars.pop("ANTHROPIC_API_KEY", None)
    else:
        subscription_id = db.get_agent_subscription_id(agent_name)
        if subscription_id:
            token = db.get_subscription_token(subscription_id)
            if token:
                env_vars["CLAUDE_CODE_OAUTH_TOKEN"] = token
            env_vars.pop("ANTHROPIC_API_KEY", None)
        elif db.get_use_platform_api_key(agent_name):
            env_vars["ANTHROPIC_API_KEY"] = get_anthropic_api_key()
            env_vars.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        else:
            env_vars.pop("ANTHROPIC_API_KEY", None)
            env_vars.pop("CLAUDE_CODE_OAUTH_TOKEN", None)

    # Per-agent GitHub PAT (opt-in), plus GITHUB_REPO / GIT_SYNC from git config.
    git_config = db.get_git_config(agent_name)
    if git_config:
        from routers.git import get_github_pat_for_agent
        pat = get_github_pat_for_agent(agent_name)
        repo = git_config.get("github_repo") if isinstance(git_config, dict) else getattr(git_config, "github_repo", None)
        if pat and repo:
            env_vars["GITHUB_REPO"] = repo
            env_vars["GITHUB_PAT"] = pat
            env_vars["GIT_SYNC_ENABLED"] = "true"
            _git_base = os.getenv("TRINITY_GIT_BASE_URL")
            if _git_base:
                env_vars["TRINITY_GIT_BASE_URL"] = _git_base

    guardrails_override = db.get_guardrails_config(agent_name)
    if guardrails_override:
        import json as _json
        env_vars["AGENT_GUARDRAILS"] = _json.dumps(guardrails_override)

    _stall_limit = (os.getenv("AGENT_TOOL_STALL_LIMIT_S") or "").strip()
    if _stall_limit:
        env_vars["AGENT_TOOL_STALL_LIMIT_S"] = _stall_limit

    # #1159: per-agent in-container auth token (fail-closed: raises if secret unset).
    env_vars["TRINITY_AGENT_AUTH_TOKEN"] = derive_agent_token(agent_name)
