"""
Agent Service Lifecycle - Agent start/stop and configuration management.

Contains functions for starting, stopping, and reconfiguring agents.
"""
import asyncio
import logging
import os
import time
from typing import Literal, Optional

import docker
import httpx

from fastapi import HTTPException

from database import db
from services.docker_service import (
    docker_client,
    get_agent_container,
    get_next_available_port,
    reserve_port_for_recreate,
)
from services.docker_utils import (
    container_stop, container_remove, container_start, container_reload,
    volume_get, volume_create, containers_run, image_get, container_stop
)
from services.agent_service.helpers import validate_base_image
from services.agent_runtime_state import clear_agent_breakers
from services.settings_service import get_anthropic_api_key, get_github_pat, get_agent_full_capabilities, get_agent_default_resources
from services.skill_service import skill_service
from .helpers import check_shared_folder_mounts_match, check_api_key_env_matches, check_github_pat_env_matches, check_resource_limits_match, check_full_capabilities_match, check_guardrails_env_matches, check_agent_auth_token_env_matches, check_agent_mcp_key_matches, check_base_image_matches, is_claude_runtime, is_system_agent_name
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
    AGENT_LOG_CONFIG,
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
        # ent#236: NOT an early return any more. "Zero assigned skills" is
        # exactly the state left behind by unassigning the last skill while the
        # agent was stopped — returning here would strand that package on the
        # agent permanently, which is the precise gap this closes.
        reconcile = await skill_service.reconcile_agent_skills(agent_name, [])
        return {"status": "skipped", "reason": "no_skills", "reconcile": reconcile}

    logger.info(f"Injecting {len(skill_names)} skills into agent {agent_name}: {skill_names}")

    # Inject skills. force=False: the start path skips skills whose agent-side
    # version already matches the library tree SHA (ent#183); manual sync via
    # the REST/MCP inject endpoint stays an unconditional repair (force=True).
    from services.skill_service import SkillInjectionBusy
    try:
        result = await skill_service.inject_skills(agent_name, skill_names, force=False)
    except SkillInjectionBusy:
        return {"status": "skipped", "reason": "injection_already_running"}

    # ent#236: reconcile AFTER injection, and outside the injection lock — both
    # take the same per-agent lock, so reconciling first (or inside) would
    # deadlock against the injection that just ran. Never raises.
    reconcile = await skill_service.reconcile_agent_skills(agent_name, skill_names)

    warning_count = sum(
        len(r.get("warnings") or []) for r in result.get("results", {}).values()
    )
    if result.get("success"):
        return {
            "status": "success",
            "skills_injected": result.get("skills_injected", 0),
            "skills_unchanged": result.get("skills_unchanged", 0),
            "skills_warnings": warning_count,
            "results": result.get("results", {}),
            "reconcile": reconcile,
        }
    else:
        injected = result.get("skills_injected", 0) + result.get("skills_unchanged", 0)
        return {
            "status": "partial" if injected > 0 else "failed",
            "skills_injected": result.get("skills_injected", 0),
            "skills_unchanged": result.get("skills_unchanged", 0),
            "skills_failed": result.get("skills_failed", 0),
            "skills_warnings": warning_count,
            "results": result.get("results", {}),
            "reconcile": reconcile,
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
    # #1854: evaluated separately (not inlined into the `or` chain) because its
    # verdict is needed twice — once to decide whether to recreate, and once to
    # decide whether this recreate must also mint + bake a fresh MCP key. The
    # other predicates are satisfied by the recreate's own derived-env rules; a
    # missing/stale MCP key is not, because the plaintext is unrecoverable.
    mcp_key_match = check_agent_mcp_key_matches(container, agent_name)
    needs_recreation = (
        not shared_folder_match or
        not check_public_folder_mount_matches(container, agent_name) or
        not check_api_key_env_matches(container, agent_name) or
        not check_github_pat_env_matches(container, agent_name) or
        not check_resource_limits_match(container, agent_name) or
        not check_full_capabilities_match(container, agent_name) or
        not check_guardrails_env_matches(container, agent_name) or
        not check_agent_auth_token_env_matches(container, agent_name) or
        not mcp_key_match
    )

    # #1809: a rebuilt base image is picked up on COLD start only. Evaluated
    # lazily — the Docker round-trip runs only when no other predicate already
    # forced a recreate. Gated on `not was_already_running` because
    # start-on-running is a load-bearing idempotent no-op (MCP ensure-running
    # calls, the SUB-003 auto-switch restart, restart_system): config drift is
    # an owner-intentional per-agent change, while image drift is armed
    # fleet-wide by any `build-base-image.sh` run — it must never turn a start
    # of a running agent into a container kill. Ephemeral ghosts are excluded:
    # they are volume-less by design ("ghosts never recreate"), so an
    # image-drift recreate would silently destroy their workspace mid-budget
    # (trinity-enterprise#69).
    recreate_reason = "config_drift" if needs_recreation else None
    if not needs_recreation and not was_already_running:
        try:
            _eph_gate = db.get_agent_ephemeral_info(agent_name)
        except Exception:
            _eph_gate = None
        if not (isinstance(_eph_gate, dict) and _eph_gate.get("is_ephemeral")):
            if not await check_base_image_matches(container, agent_name):
                needs_recreation = True
                recreate_reason = "image_drift"

    # #1816 (AC2): a RUNNING trinity-system is NEVER recreated by this path.
    #
    # This is the one code path that could reach a running system agent — every
    # other caller stops the container first (subscription assign/remove, the
    # SUB-003 auto-switch, restart_system, /restart, /reinitialize), and delete,
    # rename, ephemeral discard, the cleanup orphan sweep and restart_fleet all
    # refuse or skip system agents outright.
    #
    # Deliberately gating `needs_recreation` WHOLESALE rather than the image
    # predicate alone: `recreate_container_with_updated_config` resolves the
    # image from the container's own Config.Image *tag*, so ANY predicate that
    # fires also adopts a new image. Gating only the image predicate would leave
    # AC2 open through the other eight. This makes AC2 a STRUCTURAL invariant,
    # independent of predicate count — a tenth predicate added next quarter
    # cannot reopen it with no test failing. #1816's convergence work is what
    # keeps this gate from firing routinely; the gate is what keeps AC2 true
    # when the next predicate is added.
    #
    # Honest, not silent: the caller is told the recreate was deferred and why,
    # so the remedy (stop it, then start) is named rather than inferred. This is
    # a deliberate divergence from regular-agent semantics — an admin who
    # changes a running system agent's config and clicks Start gets no recreate.
    recreate_deferred = None
    if needs_recreation and was_already_running and is_system_agent_name(agent_name):
        logger.info(
            f"Deferring {recreate_reason} recreate for {agent_name}: the system "
            f"agent is running and is never replaced mid-operation (#1816). "
            f"Stop it, then start it, to apply."
        )
        needs_recreation = False
        recreate_reason = None
        recreate_deferred = "system_agent_running"

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
        # #1854: when the MCP-key predicate is what drifted, the recreate cannot
        # heal it from derived state the way the other predicates do — only the
        # key HASH is stored, so a container whose key is absent or unrecognized
        # can never have the right plaintext reconstructed. Mint a fresh one and
        # bake it (with TRINITY_MCP_URL and TRINITY_BACKEND_URL, which the agent
        # server's injection requires together) as an env override.
        #
        # Fail-soft: heal_agent_mcp_key_env returns None on any failure, and the
        # recreate proceeds without it — a credential-bookkeeping problem must
        # never block a start. The predicate simply stays unsatisfied and the
        # next manual start retries.
        mcp_env_overrides = None
        if not mcp_key_match:
            from services.agent_mcp_key_service import heal_agent_mcp_key_env
            mcp_env_overrides = await heal_agent_mcp_key_env(agent_name)

        # Recreate container with updated config
        # Use system user for internal operations
        await recreate_container_with_updated_config(
            agent_name, container, "system", env_overrides=mcp_env_overrides
        )
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

    # #2069 (T1): heal the already-leaking existing fleet. The creation-time
    # `.gitignore` merge protects only agents created after that fix; existing
    # auto-sync agents converge HERE on their next base-image-drift recreate /
    # restart. Gated on the DB `auto_sync_enabled` flag — the persisted owner
    # intent the runtime already honors (`_apply_git_env_from_db`: GIT_SYNC_AUTO
    # = DB flag OR baked env). The R2 ephemeral gap does NOT apply on this path:
    # ghosts never recreate (they are volume-less by design), so no ephemeral
    # agent reaches start/recreate and the DB flag is correct and complete here.
    # Same readiness-gated, idempotent, non-fatal merge as creation — a warm
    # restart with an existing `.git` satisfies the readiness probe immediately
    # (one fast merge), a cold recreate waits for readiness like creation.
    # Touches neither `sync_to_github` nor the Push migration (AC#5); an
    # additive convergence path. Fire-and-forget so it adds no start latency.
    try:
        if db.get_git_auto_sync_enabled(agent_name):
            from services import git_service
            git_service.spawn_gitignore_merge_after_clone(agent_name)
    except Exception as e:
        logger.warning(
            "[#2069] failed to spawn the creation-parity .gitignore merge for "
            "%s on start: %s",
            agent_name,
            e,
        )

    return {
        "message": f"Agent {agent_name} started",
        "credentials_injection": credentials_status,
        "credentials_result": credentials_result,
        "skills_injection": skills_status,
        "skills_result": skills_result,
        "read_only_injection": read_only_result.get("status", "unknown"),
        "read_only_result": read_only_result,
        # #1809: surface whether (and why) this start replaced the container,
        # so "why did my container id change / uptime reset" is answerable from
        # the start response and the audit trail.
        "recreated": needs_recreation,
        "recreate_reason": recreate_reason if needs_recreation else None,
        # #1816: a recreate the AC2 gate suppressed. None on every normal start.
        # NB a field added here does NOT reach the API on its own —
        # routers/agents.py::start_agent_endpoint rebuilds a fresh dict from a
        # whitelist of keys (#1809's own learning), so this is surfaced there too.
        "recreate_deferred": recreate_deferred,
    }


async def restart_agent_internal(agent_name: str, *, stop_timeout: int = 30) -> dict:
    """The canonical cold restart: explicit stop, then the full start path (#1860).

    The explicit stop is load-bearing — the #1809 image-drift predicate runs
    only on a COLD start (``not was_already_running``), so a restart that
    should adopt a rebuilt base image must stop first; ``start_agent_internal``
    on a running agent is a deliberate idempotent no-op. This helper is the one
    home for the stop→start shape (routers/system_agent.py, routers/systems.py
    and subscription_auto_switch still carry inline copies — consolidating them
    is #1817's business, together with the per-agent start lock, which belongs
    in here once it exists).

    A missing container falls through to ``start_agent_internal``, which either
    rebuilds it from persisted config (#1559, live ownership row) or 404s.
    Returns ``start_agent_internal``'s result dict unchanged.
    """
    container = get_agent_container(agent_name)
    if container:
        await container_stop(container, timeout=stop_timeout)
    return await start_agent_internal(agent_name)


# =============================================================================
# GitHub sync env derivation (trinity-enterprise#109)
# =============================================================================

# Every env var `_apply_git_env_from_db` owns, named once so the set-or-clear
# sweep, the writers below, and the tests cannot drift apart.
#
# Deliberately NOT owned here:
#   GIT_WORKING_BRANCH  — only read by startup.sh's *clone* branch, which a
#                         recreate never reaches (the volume, and therefore
#                         .git, is carried forward); GIT_SOURCE_MODE=true wins
#                         over it in startup.sh:187 anyway, so a stale value is
#                         inert. Deriving it is a separate change.
#   GIT_UPSTREAM_REPO   — has no DB column (ent#93 bakes it at creation only).
#                         The `upstream` remote lives in .git/config on the
#                         pinned workspace volume and survives every recreate,
#                         so re-deriving is a repair mechanism, not a
#                         correctness requirement.
_GIT_ENV_KEYS = (
    "GITHUB_REPO",
    "GITHUB_PAT",
    "GIT_SYNC_ENABLED",
    "GIT_SOURCE_MODE",
    "GIT_SOURCE_BRANCH",
    "GIT_SYNC_AUTO",
    "TRINITY_GIT_BASE_URL",
)


def _apply_git_env_from_db(
    agent_name: str,
    env_vars: dict,
    *,
    pat_gate: Literal["effective", "per_agent_only"],
) -> None:
    """Single owner of the GitHub-sync env block across BOTH rebuild paths.

    Before ent#109 this block existed only in `_apply_persisted_auth_env`
    (`recreate_missing_container`, the rebuild-from-nothing path).
    `recreate_container_with_updated_config` — whose production callers are
    `start_agent_internal`, fired by nine config-drift predicates **and by
    base-image drift at cold start**, and (ent#109) `repo_binding`'s bind path,
    which calls it directly and therefore owes the same `clear_agent_breakers`
    that `start_agent_internal` runs immediately before its own call — seeded
    env from the OLD container and
    re-derived only `GITHUB_PAT`. So a fleet-wide base-image rebuild replayed
    whatever git env each container happened to be carrying.

    **The gate is the REPO, not the PAT** (ent#123). A tokenless agent (an
    anonymous public-template clone, e.g. Cornelius) must still receive
    `GITHUB_REPO` + `GIT_SYNC_ENABLED`, or startup.sh never attempts the clone
    and the rebuild yields a silently empty agent reporting green health — the
    #843/#1439 class.

    **Correct, never introduce — on the config-drift path only.** Because the
    repo half is repo-gated and the PAT half keeps #211's narrower per-agent
    gate, the two disagree for a real row shape (`POST /{agent}/git/initialize`
    on the global platform PAT), and introducing `GIT_SYNC_ENABLED=true` with
    no `GITHUB_PAT` makes startup.sh scrub that agent's only credential and
    blackhole its push remote. So `per_agent_only` writes the block only when
    the old container already carried a `GITHUB_REPO` (or a PAT resolves) —
    correcting a stale repo, a flipped `source_mode` or a deleted row, which is
    every case this fix is about. `effective` is exempt: with no old container,
    not introducing the block IS the #843/#1439 bug. See the inline note.

    ``pat_gate`` — **never inherited, always stated by the call site.** The two
    paths gate the PAT differently on purpose and a shared default would be a
    credential leak:

    ``"per_agent_only"`` (config-drift recreate)
        `#211`'s opt-in guard, verbatim: resolve the effective PAT only when
        the container **already carries** one, or a **per-agent** PAT row
        exists. A global-only platform PAT is therefore never injected into a
        previously-tokenless container. Without this, `configure_push_remote`
        (startup.sh) would clear the push blackhole on the next recreate and a
        tokenless agent could push a private workspace to a shared public
        upstream (`learnings.md` ent#162).

    ``"effective"`` (rebuild-from-nothing)
        The 2-tier per-agent → global resolution this path has always used.
        There is no old container to inherit a token from, so a rebuild needs
        *something*; the row's own existence is the opt-in.

    Set-or-clear, with one deliberate asymmetry. Both callers write into a dict
    that may carry values forward (`recreate_container_with_updated_config`
    seeds from the old container), so every repo-derived var is cleared when it
    no longer applies — a deleted `agent_git_config` row (reachable from the
    `routers/git.py` orphan cleanup and `_rollback_failed_creation`) pops the
    whole set, and a `source_mode` flip clears the mode/branch pair.
    ``GITHUB_PAT`` alone is **set-only while a repo is bound**: clearing it
    would revoke a live agent's push on an unrelated recreate, which is not
    this change's business.

    Named behaviour change (the only divergence from a verbatim lift): a
    container with a baked `GITHUB_PAT` and **no git binding** previously had
    that token refreshed *from the global platform PAT* on every recreate; it
    is now popped instead. The per-agent PAT is a column on `agent_git_config`,
    so "no row" means "no per-agent credential and nothing to push to" by
    construction. The population is transient (an aborted create, or an
    orphan-cleanup window), and popping runs in the same direction as ent#162.

    **Reads DB state; writes none.** `GIT_SYNC_AUTO` is derived as
    `DB flag OR baked env` and the disagreement is only logged — see the inline
    note for why a write-back cannot distinguish a creation-time discrepancy
    from an owner's explicit disable.
    """
    git_config = db.get_git_config(agent_name)

    def _gc(key: str):
        if git_config is None:
            return None
        if isinstance(git_config, dict):
            return git_config.get(key)
        return getattr(git_config, key, None)

    repo = _gc("github_repo")

    if not repo:
        # No git binding — clear every var this helper owns rather than
        # stranding stale ones in a carried-forward dict.
        for _key in _GIT_ENV_KEYS:
            env_vars.pop(_key, None)
        return

    # --- PAT gate, resolved per the call site (see docstring) ---------------
    # Computed BEFORE the repo block because the introduce-guard below consults
    # it. Safe to hoist: it reads only the carried-forward env, which the repo
    # block does not touch.
    if pat_gate == "effective":
        _pat_allowed = True
    elif pat_gate == "per_agent_only":
        # Verbatim #211 gate. The original also ANDed `bool(db.get_git_config(
        # agent_name))`, which is unconditionally true here (we have a repo).
        # Kept inlined via `db` rather than importing
        # `helpers.needs_per_agent_pat_injection`, so a test stubbing
        # `services.agent_service.helpers` cannot break this module (#1271 CI).
        _pat_allowed = bool(env_vars.get("GITHUB_PAT")) or bool(
            db.get_agent_github_pat(agent_name)
        )
    else:
        raise ValueError(f"unknown pat_gate: {pat_gate!r}")

    # --- Correct what is carried; never INTRODUCE the block -----------------
    # Config-drift only. The repo half is gated on the REPO (ent#123) while the
    # PAT half keeps #211's narrower per-agent gate, and for one real row shape
    # those two disagree: `POST /{agent}/git/initialize` writes an
    # `agent_git_config` row and pushes with the resolved — often GLOBAL —
    # platform PAT, but never recreates the container, never bakes any git env,
    # never persists a per-agent PAT row, and never writes the token into the
    # workspace `.env` (so startup.sh's #1264 fallback does not cover it). Its
    # only credential lives in the container's `.git/config` origin URL.
    #
    # Introducing `GIT_SYNC_ENABLED=true` with no `GITHUB_PAT` is exactly the
    # input startup.sh reads as "deliberately tokenless": the restart branch
    # rewrites origin to the credential-less CLONE_URL — DESTROYING that token —
    # and `configure_push_remote` blackholes the push remote. Silent, and armed
    # fleet-wide by the same base-image drift this helper exists to fix.
    #
    # So on this path the helper only ever CORRECTS a git env the container
    # already carries (a stale repo, a flipped source_mode, a deleted row —
    # every case the fix is actually about; a tokenless ent#123 agent carries
    # GITHUB_REPO from creation, so the flagship is unaffected). The
    # rebuild-from-nothing path is deliberately exempt: it has no old container
    # to carry anything, and NOT introducing the block there is the #843/#1439
    # silently-empty-agent bug.
    if pat_gate == "per_agent_only" and not (
        env_vars.get("GITHUB_REPO") or _pat_allowed
    ):
        return

    env_vars["GITHUB_REPO"] = repo
    env_vars["GIT_SYNC_ENABLED"] = "true"

    if _pat_allowed:
        from routers.git import get_github_pat_for_agent

        _pat = get_github_pat_for_agent(agent_name)
        if _pat:
            env_vars["GITHUB_PAT"] = _pat

    # --- source mode / branch: set-or-clear ---------------------------------
    # Source-mode rows re-derive the mode/branch pair so a volume-loss rebuild
    # re-clones the right branch instead of a bare default-branch clone with no
    # tracking; a row that flipped to working-branch mode clears both.
    if _gc("source_mode"):
        env_vars["GIT_SOURCE_MODE"] = "true"
        env_vars["GIT_SOURCE_BRANCH"] = _gc("source_branch") or "main"
    else:
        env_vars.pop("GIT_SOURCE_MODE", None)
        env_vars.pop("GIT_SOURCE_BRANCH", None)

    # --- GIT_SYNC_AUTO: DB flag OR baked env, then converge -----------------
    # #389's `auto_sync_enabled` column and the creation-time env genuinely
    # disagree today: `crud.py`'s DB writer carries `and not config.ephemeral`
    # inside a swallowing try/except while `_apply_github_env` does not, and the
    # column defaults to 0. So env-`true`/DB-`0` is reachable from a single
    # transient DB hiccup at creation and permanently for ghosts — and deriving
    # from the DB flag alone would silently STOP auto-push for that slice of the
    # fleet (no error, just a stale `agent_sync_state`). So: OR the two.
    #
    # Deliberately NO write-back. A `PUT /{agent}/git/auto-sync {enabled:false}`
    # writes the DB row and nothing else (routers/git.py), while the agent gates
    # on container env (`agent_server/auto_sync.py`) — and creation sets BOTH to
    # true for the ordinary non-source-mode PAT agent. So "baked true / DB 0" is
    # ALSO exactly what an owner's explicit disable looks like, and a backfill
    # cannot tell the two apart: it would silently re-enable the flag, erase the
    # only record of that intent, and make the toggle unable to ever stick.
    # Worse, `PUT .../auto-sync` is OwnedAgentByName while `POST .../start` (the
    # recreate's trigger) is AuthorizedAgentByName, so the write would let a
    # shared non-owner — or an agent-scoped key resolving to its owner WITH the
    # owner's role (trinity-ops-agent#232) — flip an owner-only flag that arms a
    # 15-minute background commit-and-push loop. Log the disagreement instead.
    # Making the #389 toggle authoritative (one writer, env re-baked on toggle)
    # is the separate follow-up that retires this OR honestly.
    _baked_auto = str(env_vars.get("GIT_SYNC_AUTO") or "").strip().lower() == "true"
    _db_auto = bool(_gc("auto_sync_enabled"))
    if _db_auto or _baked_auto:
        env_vars["GIT_SYNC_AUTO"] = "true"
    else:
        env_vars.pop("GIT_SYNC_AUTO", None)

    if _baked_auto and not _db_auto:
        logger.info(
            "Agent %s carries GIT_SYNC_AUTO=true but auto_sync_enabled=0; "
            "keeping auto-push on (the env wins until the #389 toggle is "
            "authoritative). NOT rewriting the DB flag — it may be a "
            "deliberate owner disable (ent#109)",
            agent_name,
        )

    # --- optional self-hosted git base URL: refresh from the CURRENT backend
    # env (the AGENT_TOOL_STALL_LIMIT_S idiom), so pointing the platform at or
    # away from a gitea/GHES harness takes effect on recreate.
    _git_base = (os.getenv("TRINITY_GIT_BASE_URL") or "").strip()
    if _git_base:
        env_vars["TRINITY_GIT_BASE_URL"] = _git_base
    else:
        env_vars.pop("TRINITY_GIT_BASE_URL", None)


async def recreate_container_with_updated_config(
    agent_name: str,
    old_container,
    owner_username: str,
    *,
    full_capabilities: Optional[bool] = None,
    env_overrides: Optional[dict] = None,
    require_running: bool = True,
    preserve_run_state: bool = False,
):
    """
    Recreate an agent container with updated configuration.
    Handles shared folder mounts and API key settings.
    Preserves the agent's workspace volume and other configuration.

    **This function STARTS the replacement container.** That was true before
    #2092 too, but only the call sites knew it: each in-tree caller pre-checked
    `container.status == "running"` and refused otherwise, while this function
    captured no run state, offered no way to preserve it, and said nothing about
    the precondition — which documented `env_overrides` at length.

    Recreating a deliberately-stopped agent therefore started it, silently and
    with no log line saying the run state had changed. Out-of-tree ops tooling
    doing a base-image adoption wave hit exactly that: two agents stopped eight
    days earlier came back up. Note what did NOT contain it — `autonomy_enabled
    = 0` gates cron fires and reminders (#1806) but not human-initiated inbound
    chat, so a channel binding and a public link became reachable again. No
    traffic arrived; the containment was luck.

    Args:
        require_running: refuse (ValueError) unless ``old_container`` is
            running. Default True, which is what every existing caller already
            enforces for itself — so this changes no behaviour and converts the
            silent start into a loud error for the next caller. Pass False to
            recreate a stopped agent deliberately.
        preserve_run_state: leave the replacement STOPPED when the original was
            stopped. What a base-image adoption wave actually wants, and awkward
            to do from outside: the caller would have to read the state before,
            then stop the container after it is already up and its agent server
            has begun booting — racing its own recreate. Here the stop is issued
            immediately after the handoff. The replacement is still created via
            ``containers_run`` and so boots briefly before being stopped; that is
            visible in the container's log, and is the honest cost of not
            duplicating the (heavily conditioned) creation call.

        env_overrides: #1854 — env vars to force onto the replacement,
            applied LAST (immediately before the container handoff), so they win
            over every derived rule in between. The caller owns the value; this
            function neither mints nor validates it.

            Used by MCP-key rotation and by the start-time drift self-heal, both
            of which must bake a freshly minted `TRINITY_MCP_API_KEY` that
            cannot be reconstructed from persisted state (only the hash is
            stored). NOT applied at the `Config.Env` copy point: roughly twenty
            derived mutations sit between there and the handoff (subscription
            token/API-key juggling, GitHub PAT, guardrails, stall limit,
            TRINITY_AGENT_AUTH_TOKEN, the pull-mode pop+update), and an override
            landing before them would be silently clobbered by any that happened
            to collide.
        full_capabilities: #1816 override for the fleet-wide capabilities
            setting. ``None`` (every existing caller) resolves it the same way
            the matching predicate does — the fleet default for a regular
            agent, and unconditionally ``True`` for ``trinity-system``, whose
            FULL_CAPABILITIES are contractual (package installation), not
            fleet-derived. Writer and checker route through the SAME
            ``is_system_agent_name`` helper so they cannot disagree; if they
            did, the checker would keep demanding a recreate the writer never
            satisfies (infinite recreate loop — the hazard this module already
            documents for the guardrails/PAT matchers).
    """
    # #2092: the precondition, enforced rather than assumed. Read BEFORE any
    # work, so a refusal costs nothing and cannot half-recreate.
    was_running = getattr(old_container, "status", None) == "running"
    if require_running and not was_running:
        raise ValueError(
            f"recreate_container_with_updated_config would START agent "
            f"{agent_name!r}, whose container is "
            f"{getattr(old_container, 'status', 'unknown')!r}. Pass "
            f"require_running=False to do that deliberately, and "
            f"preserve_run_state=True to leave it stopped afterwards (#2092)."
        )

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

    # ent#109: the whole GitHub-sync env block is derived from persisted DB
    # state by the single shared owner. Before this, THIS path re-derived only
    # GITHUB_PAT and replayed whatever GITHUB_REPO / GIT_SYNC_* the old
    # container happened to carry — and a base-image rebuild makes every cold
    # start a recreate, so that replay was fleet-wide.
    #
    # `per_agent_only` preserves #211's opt-in guard verbatim: a global-only
    # platform PAT is never injected into a previously-tokenless container,
    # and it stays in sync with the recreate matcher
    # (`helpers.needs_per_agent_pat_injection`) so the two converge in one pass
    # instead of looping.
    _apply_git_env_from_db(agent_name, env_vars, pat_gate="per_agent_only")
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

    # #2127: same set-or-clear treatment for the early-finalize idle ceiling —
    # this dict is seeded from the OLD container, so an unset backend env must
    # POP the stale baked value rather than leave it (the #1809/ent#109 rule).
    _idle_finalize = (os.getenv('AGENT_IDLE_FINALIZE_S') or '').strip()
    if _idle_finalize:
        env_vars['AGENT_IDLE_FINALIZE_S'] = _idle_finalize
    else:
        env_vars.pop('AGENT_IDLE_FINALIZE_S', None)

    # #1159: refresh the per-agent auth token. Deterministic from agent_name, so
    # this re-derives under the CURRENT name — the load-bearing part of the
    # rename fix (a renamed container otherwise keeps derive(old_name) and 401s
    # once enforcement is on). check_agent_auth_token_env_matches forces this
    # recreate whenever the running token is missing or stale.
    env_vars['TRINITY_AGENT_AUTH_TOKEN'] = derive_agent_token(agent_name)

    # #1081 G2 / #307 / #1083: re-ensure the agent→backend callback URL on
    # recreate. crud.py sets TRINITY_BACKEND_URL only at FRESH create (~#595);
    # recreate seeds env from the OLD container and would otherwise DROP it for a
    # legacy agent that predates it — leaving the heartbeat, the #1083 result
    # callback, AND the #1081 pull worker with no backend URL (the worker logs
    # "TRINITY_BACKEND_URL / TRINITY_MCP_API_KEY missing" and never starts).
    # setdefault preserves any value already baked on the container (matching the
    # #1098 TMPDIR idiom above).
    #
    # #1816: EXCEPT for the system agent. This env var is the agent-side
    # heartbeat loop's gate, and heartbeat_service.authorize_heartbeat accepts
    # ONLY `scope == "agent"` keys — trinity-system's key is `scope == "system"`,
    # so arming it buys nothing and costs a permanent 5-second 403 loop
    # (swallowed agent-side, ~17k backend log lines/day). Matters now because
    # #1816 makes this recreate a ROUTINE path for the system agent (boot with
    # the container stopped → base-image adoption), where before it was
    # incidental. The system agent also has no #1083 result callback and no
    # #1081 pull worker, so the other two consumers are moot too. Whether the
    # orchestrator SHOULD be visible to fleet health is a separate design
    # question (#1816 §10).
    if not is_system_agent_name(agent_name):
        env_vars.setdefault(
            'TRINITY_BACKEND_URL',
            os.getenv('TRINITY_BACKEND_URL', 'http://backend:8000'),
        )

    # #946 / #1081 Phase 2: re-apply the pull worker opt-in on recreate. Clear
    # any baked pull env FIRST (set-or-clear, mirroring the guardrails/stall-limit
    # idiom above) so DE-piloting an agent actually stops its worker on recreate —
    # pull_mode_env_vars returns {} for a non-pilot, so a bare .update() would
    # leave a stale TRINITY_PULL_MODE=true baked in (#1081 B1). Empty (no-op) for
    # every non-pilot agent, so default push behavior is unchanged.
    from services.agent_service.pull_mode import pull_mode_env_vars, PULL_MODE_ENV_KEYS
    for _pull_key in PULL_MODE_ENV_KEYS:
        env_vars.pop(_pull_key, None)
    env_vars.update(pull_mode_env_vars(agent_name))

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

    # Get full_capabilities from system-wide setting (not per-agent), unless a
    # caller overrode it or this is the system agent (#1816 — its
    # FULL_CAPABILITIES are contractual, so it must not follow the fleet
    # setting; same helper `check_full_capabilities_match` exempts on).
    if full_capabilities is None:
        full_capabilities = (
            True if is_system_agent_name(agent_name) else get_agent_full_capabilities()
        )

    # Update label to reflect current setting
    labels["trinity.full-capabilities"] = str(full_capabilities).lower()

    # #1816: carry the old container's restart policy onto the replacement.
    # `old_host_config` was extracted at the top of this function and then never
    # read — a genuinely dead variable, and precisely why `unless-stopped`
    # silently vanished from EVERY recreated agent. `trinity-system` is created
    # with it, so before this a single recreate downgraded the platform
    # orchestrator to "stays down after a crash or a host reboot".
    #
    # Null-safe by construction: `.get("RestartPolicy", {})` returns None when
    # the key exists with a null value (Docker does emit that), and `.get` on
    # None would abort the recreate with an AttributeError — the container is
    # already removed by then, so the agent would be left with none at all.
    restart_policy = old_host_config.get("RestartPolicy") or {}

    # #1809: refresh the base-image version label from the image this recreate
    # will actually run. Labels are carried forward verbatim from the old
    # container, and every AgentStatus reader prefers the container label over
    # an image lookup — so without this an image-drift recreate runs the NEW
    # image while the UI keeps reporting the OLD version. Best-effort: an
    # unreadable image keeps the carried-forward label, never blocks recreate.
    try:
        _new_image_ver = ((await image_get(image)).labels or {}).get("trinity.base-image-version")
        if _new_image_ver:
            labels["trinity.base-image-version"] = _new_image_ver
    except Exception as e:
        logger.warning(
            f"Could not refresh trinity.base-image-version label for {agent_name} "
            f"({type(e).__name__}: {e}) — keeping the carried-forward value"
        )

    # Stop and remove old container
    try:
        await container_stop(old_container)
    except Exception:
        pass
    # #2215: across the remove->create gap below, this port is invisible to the
    # allocator (its label is gone) and unreserved — and the most-recreated
    # agent tends to hold the fleet's MAX port, exactly what a concurrent
    # allocation computes as max+1. Re-assert the reservation before removal
    # (SET no-NX: it is this agent's own port; fail-open, never raises).
    reserve_port_for_recreate(ssh_port)
    try:
        await container_remove(old_container)
    except docker.errors.NotFound:
        # #1809: a concurrent start already removed it. Post-rebuild, EVERY
        # cold start is a recreate, so two racing starts (UI double-click,
        # --workers 2, restart_system loop + manual start) is routine — fall
        # through; the 409 adoption below covers the run-side of the race.
        pass

    # Build new volume configuration.
    #
    # #1665: deliberately NOT f"agent-{agent_name}-workspace" — a dead
    # assignment of exactly that name used to sit here and read as though it
    # were the mount this function uses. It isn't: the mounts are carried
    # forward from the old container's `Mounts` below, which is what keeps a
    # renamed agent on its pre-rename volume across a recreate. Anything that
    # needs to NAME this agent's volume must go through
    # `_workspace_volume_name` (ownership row), never the current name.

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

    # #1854: caller-forced env, applied LAST so it wins over every derived rule
    # above (see the `env_overrides` note in this function's docstring). Keys are
    # coerced to str because they go straight into the Docker `environment` dict.
    if env_overrides:
        env_vars.update({str(k): str(v) for k, v in env_overrides.items() if k})

    try:
        new_container = await _provision_folders_and_run_agent_container(
            agent_name,
            image=image,
            env_vars=env_vars,
            labels=labels,
            base_volumes=volumes,
            ssh_port=ssh_port,
            cpu=cpu,
            memory=memory,
            full_capabilities=full_capabilities,
            restart_policy=restart_policy,
        )
        # #2092: put the run state back. Here, NOT in the shared provisioning
        # helper — that one also serves agent creation, where "the original was
        # stopped" is meaningless and stopping the result would be wrong.
        await _restore_stopped_state(agent_name, new_container, preserve_run_state, was_running)
        return new_container
    except docker.errors.APIError as e:
        # #1809: 409 name-conflict — a concurrent start won the recreate race
        # and already ran the replacement. Adopt the winner's container instead
        # of failing this caller with a 500 ("start this agent" is idempotent).
        if getattr(e, "status_code", None) == 409:
            existing = get_agent_container(agent_name)
            if existing is not None:
                logger.info(
                    f"Recreate race for {agent_name}: adopting the concurrently "
                    f"created container"
                )
                return existing
        raise


async def _restore_stopped_state(agent_name, container, preserve_run_state, was_running):
    """Stop a freshly recreated container when the original was stopped (#2092).

    A recreate that changes whether an agent is running is exactly the event
    that used to be silent, so both outcomes are logged.

    A failed stop does NOT fail the recreate: the replacement exists and is
    healthy, and failing here leaves the caller worse off than the outcome they
    asked to avoid. But it logs at ERROR, because the agent is now running
    against an explicit request that it not be.
    """
    if not (preserve_run_state and not was_running):
        return
    try:
        await container_stop(container)
        logger.info(
            "Recreated container for agent %s and stopped it again "
            "(preserve_run_state: it was not running before)",
            agent_name,
        )
    except Exception as e:  # noqa: BLE001
        logger.error(
            "Recreated container for agent %s but could NOT stop it again "
            "(preserve_run_state): %s. The agent is running.",
            agent_name, e,
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
    restart_policy: Optional[dict] = None,
):
    """Shared tail for every container (re)build: add DB-driven shared/public
    folder mounts onto ``base_volumes`` then run the container with the full
    security posture (cap-drop ALL, AppArmor, noexec tmpfs, resource limits).

    Extracted so `recreate_container_with_updated_config` (spec from the old
    container) and `recreate_missing_container` (spec from persisted DB state
    after a soft-delete recovery, #1559) share one canonical run path — the
    security envelope can never drift between them (AC: "goes through the
    supported creation path, not a hand-rolled docker run").

    Args:
        restart_policy: #1816 — Docker restart policy to carry onto the
            replacement, e.g. ``{"Name": "unless-stopped"}``. Forwarded ONLY
            when it names a policy: docker-py rejects a ``None`` and an empty
            ``{"Name": ""}`` is Docker's "no policy", which is also what
            omitting the kwarg produces. ``None`` (the default, and every
            pre-#1816 caller) is therefore exactly today's behaviour.

            Load-bearing for ``trinity-system``, which is created with
            ``unless-stopped`` and, before this, silently LOST it on every
            recreate — the old container's HostConfig was extracted and never
            read.
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
        # #1871: bound the container's json-file log (Docker's default is
        # unbounded). This is the retrofit seam — `log_config` is creation-time,
        # so an existing agent adopts the cap when it passes through here, which
        # covers BOTH recreate_container_with_updated_config and
        # recreate_missing_container (this is their shared tail).
        log_config=AGENT_LOG_CONFIG,
        network='trinity-agent-network',
        mem_limit=memory,
        # #1126: nano_cpus (Linux CFS quota → HostConfig.NanoCpus), NOT
        # cpu_count — docker-py's cpu_count maps to the Windows-only CpuCount
        # and leaves NanoCpus=0 on Linux, so the CPU limit was never enforced.
        nano_cpus=int(cpu) * 1_000_000_000,
        # #1816: carry the restart policy forward (see the arg docstring). The
        # kwarg is omitted entirely when there is no policy to set, so this is
        # a no-op for every pre-#1816 caller.
        **({"restart_policy": restart_policy} if (restart_policy or {}).get("Name") else {}),
    )

    logger.info(f"Recreated container for agent {agent_name} with updated configuration")
    return new_container


def _workspace_volume_name(agent_name: str) -> str:
    """The name of ``agent_name``'s home volume — resolved, never assumed
    (#1664/#1665).

    THE rule for every "this agent's volume" lookup: rename keeps the agent's
    volumes under the pre-rename base, because Docker can rename neither a
    volume nor its immutable `trinity.agent-name` label. So the agent's CURRENT
    name is not its volume's name, and f-stringing it is wrong for any agent
    that was ever renamed. The ownership row is the only record of the pairing
    (`volume_base_name`, NULL ⇒ never renamed ⇒ the name itself).

    Getting this wrong is silent, not loud: `containers.run` CREATES a missing
    named volume instead of failing, so a wrong name yields an empty
    `/home/developer` and a working-looking agent.

    Fail-safe: a DB error falls back to the agent name — the pre-#1665 behavior,
    and correct for every un-renamed agent — rather than blocking the rebuild.
    """
    try:
        return f"agent-{db.get_volume_base_name(agent_name) or agent_name}-workspace"
    except Exception as e:  # noqa: BLE001 — never block a rebuild on a DB read
        logger.warning(
            "[#1665] could not resolve volume base for %s (%s); "
            "falling back to the agent name",
            agent_name,
            e,
        )
        return f"agent-{agent_name}-workspace"


def _reconstruct_template_id(agent_name: str, tmpl: dict) -> str:
    """Best-effort rebuild of an agent's template id after its container is gone (#1811).

    Returns "" when nothing identifying survives — the honest answer, and the
    same value the caller had before, so this can only improve the result.
    """
    try:
        git_cfg = db.get_git_config(agent_name)
    except Exception:  # noqa: BLE001 — never block recovery on this
        git_cfg = None

    repo = getattr(git_cfg, "github_repo", None) if git_cfg else None
    if repo:
        branch = getattr(git_cfg, "working_branch", None) or getattr(
            git_cfg, "source_branch", None
        )
        return f"github:{repo}@{branch}" if branch else f"github:{repo}"

    name = (tmpl.get("name") or "").strip()
    return f"local:{name}" if name else ""


async def _read_template_yaml_from_volume(agent_name: str) -> dict:
    """Read the agent's `template.yaml` off its persisted workspace volume
    without a running container (#1559).

    After a soft-delete the container (and its `trinity.agent-runtime`
    label) is gone, but the workspace volume — which
    carries the committed `template.yaml` — survives. A throwaway, network-less
    base-image container `cat`s the file. Tolerant: any failure (missing file,
    unparseable) returns `{}` so the caller falls back to safe defaults.

    #1665: resolves the volume through the ownership row — for a renamed agent
    the current name names no volume, so this silently read nothing and the
    caller rebuilt on the default runtime instead of the committed one.
    """
    volume_name = _workspace_volume_name(agent_name)
    try:
        out = await containers_run(
            "trinity-agent-base:latest",
            command=["cat", "/home/developer/template.yaml"],
            volumes={volume_name: {"bind": "/home/developer", "mode": "ro"}},
            remove=True,
            network_disabled=True,
        )
        text = out.decode("utf-8") if isinstance(out, (bytes, bytearray)) else str(out)
        # ent#314: template.yaml read straight out of the agent's own volume —
        # agent-writable, so it gets the same guards as every other copy. The
        # broad `except` below already degrades to {}, which is the right
        # outcome for a refused document too.
        from utils.safe_yaml import AliasPolicy, load_hardened_yaml

        data = load_hardened_yaml(
            text, kind="template", alias_policy=AliasPolicy.REJECT
        )
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

    Refuses `trinity-system` (#1816): this path reconstructs a REGULAR agent and
    cannot reproduce the orchestrator's contract — it mints a fresh
    *agent-scoped* MCP key after deactivating the existing **system-scoped** one
    (whose plaintext is unrecoverable, so the downgrade is irreversible), omits
    the `trinity.is-system` label and the read-only `/template` bind, drops
    `restart_policy: unless-stopped`, arms `TRINITY_BACKEND_URL` (the scope-403
    heartbeat loop #1816 exists to avoid), and follows the fleet capabilities
    setting instead of the contractual one. #1816 made this reachable from the
    boot path and from `POST /api/system-agent/restart` (both now delegate to
    `start_agent_internal`), where a concurrent `--workers N` recreate can null
    the container lookup mid-flight. Failing closed is safe and self-healing:
    when the system agent genuinely has no container,
    `SystemAgentService.ensure_deployed`'s create branch rebuilds it correctly
    on the next boot.
    """
    if is_system_agent_name(agent_name):
        raise HTTPException(
            status_code=409,
            detail=(
                "The system agent has no container. It is rebuilt by "
                "SystemAgentService.ensure_deployed on backend boot, not by the "
                "generic recovery path, which cannot reproduce its system-scoped "
                "MCP key, labels and mounts."
            ),
        )

    image = "trinity-agent-base:latest"
    validate_base_image(image)

    tmpl = await _read_template_yaml_from_volume(agent_name)
    # #2104: template.yaml `type:` is parsed but ignored — the taxonomy is retired.
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
    # #1811: the original template id (`local:scout`, `github:Org/repo@main`)
    # lived ONLY in the destroyed container's TEMPLATE_NAME env and
    # `trinity.template` label — the workspace `template.yaml` carries `name:`
    # and `type:`, never `_template`, so this was always empty in practice and
    # both the label and (once restored) the env var came back blank.
    # Reconstruct from what actually survives:
    #   * a GitHub-native agent → its persisted git config (repo + branch);
    #   * otherwise → `local:{name}` from the volume's template.yaml.
    # This is a reconstruction, not the original string: an agent created from
    # a github: URL with no branch suffix gets one back. Persisting the id at
    # creation is the authoritative fix and needs a schema change.
    template_name = tmpl.get("_template") or _reconstruct_template_id(agent_name, tmpl)

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
        "CREDENTIALS_FILE": "/config/credentials.json",
        "ENABLE_SSH": "true",
        "ENABLE_AGENT_UI": "true",
        "AGENT_SERVER_PORT": "8000",
        "AGENT_RUNTIME": runtime,
        "AGENT_RUNTIME_MODEL": runtime_model,
        "TMPDIR": AGENT_DEFAULT_TMPDIR,
        # #1811: creation sets this (crud.py) and two consumers read it —
        # startup.sh gates local-template init on it, and the agent-server
        # /info route reports it. Recovery read the value off template.yaml but
        # used it only for the `trinity.template` LABEL, so a recovered agent
        # reported an empty template_name and skipped the local-template
        # branch. Same empty-string-when-absent shape as creation.
        "TEMPLATE_NAME": template_name,
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
            # #1811: recovery is not idempotent — every call mints a key, and a
            # repeatedly-recovered agent accumulated active rows (50 observed on
            # one instance). Deactivate the superseded ones FIRST, so the key we
            # are about to mint stays active; the same hygiene #1745 gave delete.
            # Best-effort: never block recovery on credential bookkeeping.
            try:
                superseded = db.deactivate_agent_mcp_keys(agent_name)
                if superseded:
                    logger.info(
                        "Deactivated %d superseded MCP key(s) for %s before recovery mint (#1811)",
                        superseded, agent_name,
                    )
            except Exception as e:  # noqa: BLE001
                logger.warning("Could not deactivate prior MCP keys for %s: %s", agent_name, e)

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
        "trinity.ssh-port": str(ssh_port),
        "trinity.cpu": cpu,
        "trinity.memory": memory,
        "trinity.created": utc_now_iso(),
        "trinity.template": template_name,
        "trinity.agent-runtime": runtime,
        "trinity.full-capabilities": str(full_capabilities).lower(),
    }

    # #1664/#1665: resolve the workspace volume through the ownership row, NOT
    # f"agent-{agent_name}-workspace". Rename keeps the agent's volumes under
    # the pre-rename base (Docker can rename neither a volume nor its label), so
    # for a renamed agent the current name points at a volume that does not
    # exist — and `containers.run` CREATES a missing named volume rather than
    # failing, so recovery silently rebuilt the agent on an empty
    # `/home/developer` while its real data (incl. #1169 `data_paths`) sat
    # unreferenced under the old base. NULL pin ⇒ agent_name (never renamed).
    base_volumes = {
        _workspace_volume_name(agent_name): {
            "bind": "/home/developer",
            "mode": "rw",
        }
    }

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
    # ent#123: gate on the REPO, not the PAT (see `_apply_git_env_from_db`).
    # `effective` = the 2-tier per-agent -> global PAT resolution this path has
    # always used: there is no old container to inherit a token from, so a
    # rebuild-from-nothing needs *something*, and the git-config row's own
    # existence is the opt-in.
    _apply_git_env_from_db(agent_name, env_vars, pat_gate="effective")

    guardrails_override = db.get_guardrails_config(agent_name)
    if guardrails_override:
        import json as _json
        env_vars["AGENT_GUARDRAILS"] = _json.dumps(guardrails_override)

    _stall_limit = (os.getenv("AGENT_TOOL_STALL_LIMIT_S") or "").strip()
    if _stall_limit:
        env_vars["AGENT_TOOL_STALL_LIMIT_S"] = _stall_limit

    # #2127: early-finalize idle ceiling (rebuild-from-nothing path — no old
    # container to inherit a stale value from, so set-only mirrors the sibling).
    _idle_finalize = (os.getenv("AGENT_IDLE_FINALIZE_S") or "").strip()
    if _idle_finalize:
        env_vars["AGENT_IDLE_FINALIZE_S"] = _idle_finalize

    # #1159: per-agent in-container auth token (fail-closed: raises if secret unset).
    env_vars["TRINITY_AGENT_AUTH_TOKEN"] = derive_agent_token(agent_name)
