"""
Git synchronization routes for GitHub-native agents (Phase 7).

Provides API endpoints for:
- Getting git status
- Syncing changes to GitHub
- Viewing commit history
- Pulling from GitHub
"""
import contextlib
import hashlib
import logging
import uuid
from typing import Dict, Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from models import (
    AutoSyncToggle,
    BindAgentRepoRequest,
    BindAgentRepoResponse,
    FreezeSchedulesToggle,
    GitHubPATRequest,
    GitInitializeRequest,
    GitPullRequest,
    GitSyncRequest,
    User,
)
from database import db
from dependencies import (
    get_current_user,
    reject_agent_principal,
    AuthorizedAgentByName,
    OwnedAgentByName,
)
from services import git_service
from services.platform_audit_service import platform_audit_service, AuditEventType

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["git"])


async def _audit_git(
    *,
    action: str,
    request: Request,
    current_user: User,
    agent_name: str,
    success: bool,
    details: Dict,
) -> None:
    """Emit a single GIT_OPERATION audit row for a mutating git op (#905).

    Called exactly once per handler exit path — on success AND on the
    business-failure (409 conflict / 400 / 500) paths — so the audit trail
    covers mutating/destructive operations symmetrically. Forwards the
    request's correlation id (adopted from an incoming `X-Request-ID`, e.g.
    an MCP tool call) so the MCP `mcp_operation` row and this row are
    joinable via `GET /api/audit-log?request_id=...`. Best-effort: the audit
    service swallows its own errors and never raises.
    """
    await platform_audit_service.log(
        event_type=AuditEventType.GIT_OPERATION,
        event_action=action,
        source="api",
        actor_user=current_user,
        actor_ip=request.client.host if request.client else None,
        target_type="agent",
        target_id=agent_name,
        endpoint=str(request.url.path),
        request_id=getattr(request.state, "request_id", None),
        details={**details, "success": success},
    )


@router.get("/{agent_name}/git/status")
async def get_git_status(
    agent_name: AuthorizedAgentByName,
    request: Request
):
    """
    Get git status for an agent.

    Returns:
    - git_enabled: Whether git sync is enabled
    - branch: Current branch name
    - remote_url: GitHub repository URL
    - last_commit: Last commit info
    - changes: List of modified/untracked files
    - sync_status: "up_to_date" or "pending_sync"
    """
    # Get database config
    git_config = git_service.get_agent_git_config(agent_name)

    # Get live status from agent
    status = await git_service.get_git_status(agent_name)

    if not status:
        # Agent not running or git not enabled
        if git_config:
            return {
                "git_enabled": True,
                "agent_running": False,
                "message": "Agent must be running to get git status",
                "config": {
                    "github_repo": git_config.github_repo,
                    "working_branch": git_config.working_branch,
                    "last_sync_at": git_config.last_sync_at.isoformat() if git_config.last_sync_at else None,
                    "last_commit_sha": git_config.last_commit_sha
                }
            }
        return {
            "git_enabled": False,
            "message": "Git sync not enabled for this agent"
        }

    # Merge with database info
    if git_config:
        status["db_config"] = {
            "last_sync_at": git_config.last_sync_at.isoformat() if git_config.last_sync_at else None,
            "last_commit_sha": git_config.last_commit_sha,
            "sync_enabled": git_config.sync_enabled
        }

    return status


@router.post("/{agent_name}/git/sync")
async def sync_to_github(
    agent_name: OwnedAgentByName,
    request: Request,
    body: GitSyncRequest = GitSyncRequest(),
    current_user: User = Depends(get_current_user)
):
    """
    Sync agent changes to GitHub.

    Stages all changes, creates a commit, and pushes to the working branch.

    Request body (optional):
    - message: Custom commit message
    - paths: Specific paths to sync (default: all changes)

    Returns:
    - success: Whether sync succeeded
    - commit_sha: SHA of the created commit
    - files_changed: Number of files changed
    - branch: Branch that was pushed to
    """
    # Import here to avoid circular imports
    from services.docker_service import get_agent_container

    # Check if agent exists first
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    result = await git_service.sync_to_github(
        agent_name=agent_name,
        message=body.message,
        paths=body.paths,
        strategy=body.strategy
    )

    if not result.success:
        # Return 409 for conflicts, 400 for other failures
        status_code = 409 if result.conflict_type else 400
        # S5 #386: surface conflict_class in headers so the frontend can render
        # operator-readable copy without parsing free-form detail strings.
        conflict_headers: Optional[Dict[str, str]] = None
        if result.conflict_type:
            conflict_headers = {"X-Conflict-Type": result.conflict_type}
            if result.conflict_class:
                conflict_headers["X-Conflict-Class"] = result.conflict_class
        # #905: audit the failure path too — conflicts on a mutating op must be
        # traceable, not silently dropped (previously only the success path logged).
        await _audit_git(
            action="sync",
            request=request,
            current_user=current_user,
            agent_name=agent_name,
            success=False,
            details={
                "strategy": body.strategy,
                "status_code": status_code,
                "conflict_type": result.conflict_type,
                "conflict_class": result.conflict_class,
            },
        )
        raise HTTPException(
            status_code=status_code,
            detail=result.message,
            headers=conflict_headers,
        )

    await _audit_git(
        action="sync",
        request=request,
        current_user=current_user,
        agent_name=agent_name,
        success=True,
        details={
            "commit_sha": result.commit_sha,
            "files_changed": result.files_changed,
            "branch": result.branch,
            "strategy": body.strategy,
        },
    )

    return {
        "success": result.success,
        "commit_sha": result.commit_sha,
        "files_changed": result.files_changed,
        "branch": result.branch,
        "message": result.message,
        "sync_time": result.sync_time.isoformat() if result.sync_time else None
    }


@router.get("/{agent_name}/git/log")
async def get_git_log(
    agent_name: AuthorizedAgentByName,
    request: Request,
    limit: int = 10
):
    """
    Get recent git commits for an agent.

    Returns list of commits with:
    - sha: Full commit SHA
    - short_sha: Abbreviated SHA
    - message: Commit message
    - author: Commit author
    - date: Commit date
    """
    log = await git_service.get_git_log(agent_name, limit=limit)

    if log is None:
        raise HTTPException(
            status_code=400,
            detail="Agent must be running with git enabled to view log"
        )

    return log


@router.post("/{agent_name}/git/pull")
async def pull_from_github(
    agent_name: AuthorizedAgentByName,
    request: Request,
    body: GitPullRequest = GitPullRequest(),
    current_user: User = Depends(get_current_user)
):
    """
    Pull latest changes from GitHub to the agent.

    Strategies:
    - clean: Try simple pull (fails if local changes conflict)
    - stash_reapply: Stash local changes, pull, then reapply stash
    - force_reset: Discard local changes and reset to remote
    """
    # Import here to avoid circular imports
    from services.docker_service import get_agent_container

    # Check if agent exists first
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    result = await git_service.pull_from_github(agent_name, strategy=body.strategy)

    if not result.get("success"):
        # Return 409 for conflicts, 400 for other failures
        conflict_type = result.get("conflict_type")
        status_code = 409 if conflict_type else 400
        # S5 #386: surface conflict_class alongside conflict_type.
        conflict_headers: Optional[Dict[str, str]] = None
        if conflict_type:
            conflict_headers = {"X-Conflict-Type": conflict_type}
            conflict_class = result.get("conflict_class")
            if conflict_class:
                conflict_headers["X-Conflict-Class"] = conflict_class
        # #905: audit the failure path too (see sync handler).
        await _audit_git(
            action="pull",
            request=request,
            current_user=current_user,
            agent_name=agent_name,
            success=False,
            details={
                "strategy": body.strategy,
                "status_code": status_code,
                "conflict_type": conflict_type,
                "conflict_class": result.get("conflict_class"),
            },
        )
        raise HTTPException(
            status_code=status_code,
            detail=result.get("message"),
            headers=conflict_headers,
        )

    await _audit_git(
        action="pull",
        request=request,
        current_user=current_user,
        agent_name=agent_name,
        success=True,
        details={"strategy": body.strategy},
    )

    return result


@router.get("/{agent_name}/git/config")
async def get_git_config(
    agent_name: AuthorizedAgentByName,
    request: Request
):
    """
    Get git configuration for an agent from the database.

    Returns the stored configuration including:
    - github_repo: Repository name
    - working_branch: Branch name
    - instance_id: Unique instance identifier
    - last_sync_at: Last sync timestamp
    - sync_enabled: Whether sync is enabled
    """
    config = git_service.get_agent_git_config(agent_name)

    if not config:
        return {
            "git_enabled": False,
            "message": "Git sync not configured for this agent"
        }

    return {
        "git_enabled": True,
        "github_repo": config.github_repo,
        "working_branch": config.working_branch,
        "source_branch": config.source_branch,
        "source_mode": config.source_mode,
        "instance_id": config.instance_id,
        "created_at": config.created_at.isoformat(),
        "last_sync_at": config.last_sync_at.isoformat() if config.last_sync_at else None,
        "last_commit_sha": config.last_commit_sha,
        "sync_enabled": config.sync_enabled
    }


@router.post("/{agent_name}/git/initialize")
async def initialize_github_sync(
    agent_name: OwnedAgentByName,
    body: GitInitializeRequest,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Initialize GitHub synchronization for an agent.

    This endpoint:
    1. Creates a GitHub repository (if requested)
    2. Initializes git in the agent workspace
    3. Commits the current state
    4. Pushes to GitHub
    5. Creates a working branch
    6. Stores configuration in the database

    Requires:
    - GitHub PAT configured (per-agent or platform-level) in system settings
    - Agent must be running
    - User must be agent owner
    """
    from services.docker_service import get_agent_container
    from services.github_service import GitHubService, GitHubError

    # Check if agent exists and is running
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")
    if container.status != "running":
        raise HTTPException(status_code=400, detail="Agent must be running to initialize Git sync")

    # Check if already configured
    existing_config = git_service.get_agent_git_config(agent_name)
    if existing_config:
        # Verify git is actually initialized in the container
        git_dir = await git_service.check_git_initialized(agent_name)
        if git_dir:
            # Git is properly initialized, prevent re-initialization
            raise HTTPException(
                status_code=409,
                detail=f"Git sync already configured for this agent. Repository: {existing_config.github_repo}"
            )
        else:
            # Database record exists but git not initialized - clean up orphaned record
            print(f"Warning: Found orphaned git config for {agent_name}. Cleaning up and allowing re-initialization.")
            db.delete_git_config(agent_name)

    # Get GitHub PAT: per-agent PAT first, then platform PAT (DB then env var)
    github_pat = get_github_pat_for_agent(agent_name)
    if not github_pat:
        raise HTTPException(
            status_code=400,
            detail="GitHub Personal Access Token not configured. Set a per-agent PAT or configure the platform PAT in Settings."
        )

    repo_full_name = f"{body.repo_owner}/{body.repo_name}"

    try:
        gh = GitHubService(github_pat)

        # Step 1: Check repository existence and handle create_repo flag
        repo_info = await gh.check_repo_exists(body.repo_owner, body.repo_name)

        if body.create_repo:
            # Create repository if it doesn't exist
            if not repo_info.exists:
                create_result = await gh.create_repository(
                    owner=body.repo_owner,
                    name=body.repo_name,
                    private=body.private,
                    description=body.description
                )
                if not create_result.success:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Failed to create repository: {create_result.error}"
                    )
        else:
            # create_repo=False: Repository MUST exist
            if not repo_info.exists:
                raise HTTPException(
                    status_code=400,
                    detail=f"Repository '{repo_full_name}' does not exist. Set create_repo=true to create it, or use an existing repository."
                )

        # Step 2: Reserve the working branch BEFORE touching the container.
        # S7 Layer 0 (#382): goes through the single-entry helper so the
        # remote probe + DB insert under the partial UNIQUE index happen
        # atomically. If anything in the rest of this handler fails we
        # roll the DB row back below so retries can claim a fresh branch.
        instance_id, reserved_branch = await git_service.reserve_and_generate_instance_id(
            agent_name=agent_name,
            github_repo=repo_full_name,
        )

        try:
            # Step 3: Initialize git in container using the reserved branch.
            # `create_working_branch=False` tells the helper not to generate
            # its own ID — the caller owns the reservation now (S7 Layer 0).
            init_result = await git_service.initialize_git_in_container(
                agent_name=agent_name,
                github_repo=repo_full_name,
                github_pat=github_pat,
                create_working_branch=False,
                working_branch=reserved_branch,
            )

            if not init_result.success:
                # Determine if this is a user error (400) or server error (500)
                error_msg = init_result.error or "Unknown error"
                # Repository not found during push = user configuration error
                if "Repository not found" in error_msg or "not found" in error_msg.lower():
                    raise HTTPException(
                        status_code=400,
                        detail=f"Git initialization failed: {error_msg}. Verify the repository exists and you have push access."
                    )
                # Permission issues = user error
                if "permission" in error_msg.lower() or "403" in error_msg:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Git initialization failed: {error_msg}. Check that your GitHub PAT has push access to this repository."
                    )
                # Other errors could still be server issues
                raise HTTPException(
                    status_code=400,
                    detail=f"Git initialization failed: {error_msg}"
                )
        except Exception:
            # Release the reservation so a retry can grab a fresh branch.
            try:
                db.delete_git_config(agent_name)
            except Exception as cleanup_exc:
                logger.warning(
                    "Failed to roll back agent_git_config for %s after init "
                    "failure: %s",
                    agent_name,
                    cleanup_exc,
                )
            raise

        await platform_audit_service.log(
            event_type=AuditEventType.GIT_OPERATION,
            event_action="init",
            source="api",
            actor_user=current_user,
            actor_ip=request.client.host if request.client else None,
            target_type="agent",
            target_id=agent_name,
            endpoint=str(request.url.path),
            request_id=getattr(request.state, "request_id", None),
            details={
                "github_repo": repo_full_name,
                "working_branch": init_result.working_branch,
                "instance_id": instance_id,
                "created_repo": bool(body.create_repo),
                "private": bool(body.private),
            },
        )

        return {
            "success": True,
            "message": "GitHub sync initialized successfully",
            "github_repo": repo_full_name,
            "working_branch": reserved_branch,
            "instance_id": instance_id,
            "repo_url": f"https://github.com/{repo_full_name}"
        }

    except HTTPException:
        raise
    except GitHubError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to initialize GitHub sync: {str(e)}")


# =============================================================================
# Per-Agent GitHub PAT Configuration (#347)
# =============================================================================

# get_github_pat_for_agent moved to services/settings_service.py (ent#162) so
# services (crud/lifecycle/helpers) stop importing a router (Invariant #1:
# Router → Service → DB, not the reverse). Re-exported here so existing callers
# `from routers.git import get_github_pat_for_agent` keep working unchanged.
# It stays the 2-tier per-agent → global ladder; the 3-tier create-path resolver
# is settings_service.resolve_github_pat (see the safety pin in that module).
from services.settings_service import get_github_pat_for_agent  # noqa: E402,F401


@router.get("/{agent_name}/github-pat")
async def get_agent_github_pat_status(
    agent_name: AuthorizedAgentByName,
    request: Request
):
    """
    Get GitHub PAT configuration status for an agent.

    Returns:
    - configured: Whether agent has a custom PAT
    - source: "agent" if custom PAT, "global" if using system PAT
    - has_global: Whether a global PAT is configured
    """
    from services.settings_service import get_github_pat

    has_agent_pat = db.has_agent_github_pat(agent_name)
    global_pat = get_github_pat()
    has_global_pat = bool(global_pat)

    return {
        "agent_name": agent_name,
        "configured": has_agent_pat,
        "source": "agent" if has_agent_pat else "global",
        "has_global": has_global_pat
    }


@router.put("/{agent_name}/github-pat")
async def set_agent_github_pat(
    agent_name: OwnedAgentByName,
    body: GitHubPATRequest,
    request: Request
):
    """
    Set a per-agent GitHub PAT.

    The PAT is validated against GitHub API before saving.
    PAT is encrypted at rest using AES-256-GCM.

    Note: Agent must be restarted for the new PAT to be used in
    container git operations (PAT is embedded in remote URL on restart).

    Body:
    - pat: GitHub Personal Access Token
    """
    from services.github_service import GitHubService, GitHubError

    pat = body.pat.strip()
    if not pat:
        raise HTTPException(status_code=400, detail="PAT cannot be empty")

    # Validate PAT against GitHub API
    try:
        gh = GitHubService(pat)
        is_valid, username = await gh.validate_token()
        if not is_valid:
            raise HTTPException(
                status_code=400,
                detail="Invalid GitHub PAT. Token was rejected by GitHub API."
            )
    except GitHubError as e:
        raise HTTPException(status_code=400, detail=f"GitHub API error: {str(e)}")
    except HTTPException:
        raise  # Re-raise HTTP exceptions as-is
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to validate PAT: {str(e)}")

    # Check if agent has git config (required for storing PAT)
    git_config = git_service.get_agent_git_config(agent_name)
    if not git_config:
        raise HTTPException(
            status_code=400,
            detail="Agent does not have Git sync configured. Initialize Git sync first."
        )

    # Store encrypted PAT
    success = db.set_agent_github_pat(agent_name, pat)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to save PAT")

    # #1264: propagate to the running container so the PAT takes effect WITHOUT a
    # restart — inject GITHUB_PAT into .env (adding the line if the container was
    # created tokenless) and re-template the live git remote. Best-effort: a
    # stopped agent picks it up on next start (relaxed lifecycle injection +
    # startup self-heal).
    from services.github_pat_propagation_service import propagate_pat_to_single_agent
    propagation = await propagate_pat_to_single_agent(agent_name, pat)

    # #1264 review: the live git process authenticates from the remote URL, so
    # only `remote_updated` means git ops work *immediately*. An env-only update
    # (or a stopped agent) takes effect on the next restart.
    if propagation.get("remote_updated"):
        note = "PAT applied to the running agent — git operations will use it immediately."
    elif propagation.get("env_updated"):
        note = "PAT saved to the agent; it will take effect on the next restart."
    elif propagation.get("reason") == "agent_not_running":
        note = "PAT saved. Start the agent for it to take effect in git operations."
    else:
        note = "PAT saved, but live propagation did not complete; restart the agent to apply it."

    return {
        "message": "GitHub PAT configured successfully",
        "agent_name": agent_name,
        "github_username": username,
        "source": "agent",
        "propagation": propagation,
        "note": note,
    }


@router.delete("/{agent_name}/github-pat")
async def clear_agent_github_pat(
    agent_name: OwnedAgentByName,
    request: Request
):
    """
    Clear per-agent GitHub PAT (revert to global PAT).

    Note: Agent must be restarted for the change to take effect
    in container git operations.
    """
    # Clear the PAT
    db.clear_agent_github_pat(agent_name)

    return {
        "message": "GitHub PAT cleared, now using global PAT",
        "agent_name": agent_name,
        "source": "global"
    }


# ============================================================================
# S3 — Reset-to-main-preserve-state (abilityai/trinity#384)
# ============================================================================


@router.post("/{agent_name}/git/reset-to-main-preserve-state")
async def reset_to_main_preserve_state(
    agent_name: OwnedAgentByName,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Adopt `origin/main` as the new baseline, preserving instance state.

    The safe recovery for the parallel-history deadlock (§S3 in the
    git-improvements proposal). Snapshots the files matching the
    persistent-state allowlist (§S4) to `.trinity/backup/<ts>/`, hard-
    resets to `origin/main`, overlays the snapshot back, commits `Adopt
    main baseline, preserve state`, and pushes with `--force-with-lease`.

    Guardrails (409):
    - `agent_busy` — the agent is currently executing a task.
    - `no_git_config` — the agent has no `.git` directory or no origin.
    - `no_remote_main` — origin has no `main` branch to adopt.
    """
    result = await git_service.reset_to_main_preserve_state(agent_name)

    err = result.get("error")
    if err:
        # #905: this is a destructive, force-with-lease recovery op — every
        # exit path (success and each guardrail/failure) must be auditable.
        status_code = 409 if err in ("agent_busy", "no_git_config", "no_remote_main", "no_write_credentials") else 500
        await _audit_git(
            action="reset_to_main_preserve_state",
            request=request,
            current_user=current_user,
            agent_name=agent_name,
            success=False,
            details={"error": err, "status_code": status_code},
        )
        headers = {"X-Conflict-Type": err} if status_code == 409 else None
        raise HTTPException(
            status_code=status_code,
            detail=result.get("message", err),
            headers=headers,
        )

    await _audit_git(
        action="reset_to_main_preserve_state",
        request=request,
        current_user=current_user,
        agent_name=agent_name,
        success=True,
        details={
            "snapshot_dir": result.get("snapshot_dir"),
            "commit_sha": result.get("commit_sha"),
            "files_preserved": result.get("files_preserved"),
            "working_branch": result.get("working_branch"),
        },
    )

    return {"success": True, **result}


# ============================================================================
# Post-creation repo binding (trinity-enterprise#109)
# ============================================================================

# Both locks carry a TTL comfortably above the worst-case end-to-end budget
# (repo create + visibility poll + a full-history push + a container
# replacement). A binding that genuinely outruns this has already failed in a
# way the operator must see; the TTL is a backstop against a crashed worker
# wedging a destination forever, not a deadline.
_BIND_LOCK_TTL_S = 600


@contextlib.asynccontextmanager
async def _bind_locks(agent_name: str, destination_repo: str):
    """Serialize a binding on BOTH axes, and FAIL CLOSED if Redis is unavailable.

    Two locks, because they guard two different collisions:

    ``agent:bind_dest:{sha256(lower(destination))}``
        The one that actually matters. The race this feature can produce is
        two DIFFERENT agents binding one destination repo, which no per-agent
        lock ever serializes. Keyed on the destination, hashed so an arbitrary
        `owner/name` can't shape the key, and lower-cased first because GitHub
        slugs are case-insensitive — otherwise `Alice/Brain` and `alice/brain`
        would take two different locks on one repo.

    ``agent:bind_op:{agent}``
        Guards double-submit on a single agent (an impatient second click
        while the first request is mid-recreate).

    **Fail closed (503), unlike ``_agent_data_op_lock``.** That lock's
    fail-open is calibrated for a tar round-trip, where a lost lock costs a
    duplicated read. Here a lost lock means two repo creations, two CAS
    writes, and two concurrent recreates of one container — so a Redis outage
    must stop the operation, not wave it through.
    """
    from routers.auth import get_redis_client

    dest_key = (
        "agent:bind_dest:"
        + hashlib.sha256(destination_repo.strip().lower().encode()).hexdigest()
    )
    agent_key = f"agent:bind_op:{agent_name}"

    try:
        client = get_redis_client()
        if client is None:
            raise RuntimeError("no redis client")
        # Cheap liveness probe: `set(nx=True)` returning False is ambiguous
        # between "held" and "Redis is broken in a way that lies", so make the
        # connection prove itself before any verdict is derived from it.
        client.ping()
    except Exception as e:  # noqa: BLE001 — fail CLOSED, see docstring
        logger.warning("repo-bind: lock layer unavailable for %s: %s", agent_name, e)
        raise HTTPException(
            status_code=503,
            detail={
                "error": (
                    "Repository binding is temporarily unavailable — the "
                    "coordination service could not be reached. No changes were "
                    "made. Please retry shortly."
                ),
                "code": "BIND_OP_IN_PROGRESS",
            },
            headers={"Retry-After": "30"},
        )

    held: list = []
    try:
        for key in (dest_key, agent_key):
            token = uuid.uuid4().hex
            try:
                acquired = bool(client.set(key, token, nx=True, ex=_BIND_LOCK_TTL_S))
            except Exception as e:  # noqa: BLE001 — fail CLOSED mid-acquire too
                logger.warning("repo-bind: lock acquire failed for %s: %s", key, e)
                raise HTTPException(
                    status_code=503,
                    detail={
                        "error": (
                            "Repository binding is temporarily unavailable. No "
                            "changes were made. Please retry shortly."
                        ),
                        "code": "BIND_OP_IN_PROGRESS",
                    },
                    headers={"Retry-After": "30"},
                )
            if not acquired:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "error": (
                            "Another repository binding is already in progress "
                            "for this agent or this destination repository. No "
                            "changes were made. Please retry shortly."
                        ),
                        "code": "BIND_OP_IN_PROGRESS",
                    },
                    headers={"Retry-After": "30"},
                )
            held.append((key, token))
        yield
    finally:
        for key, token in held:
            try:
                # Ownership-checked release: never delete a lock the TTL already
                # expired and a second caller re-took.
                if client.get(key) == token:
                    client.delete(key)
            except Exception:  # noqa: BLE001 — the TTL is the backstop
                pass


@router.post("/{agent_name}/git/bind-to-own-repo", response_model=BindAgentRepoResponse)
async def bind_agent_to_own_repo(
    agent_name: OwnedAgentByName,
    body: BindAgentRepoRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    """Bind this agent to a GitHub repository the caller owns (ent#109).

    Creates the destination if needed, pushes the agent's CURRENT workspace
    history into it, repoints `origin`, persists the per-agent PAT, and
    re-bakes the container environment so the rebind survives a restart.

    Owner-only (`OwnedAgentByName`, uniform 404) **and human-only**
    (`reject_agent_principal`). The role gate alone is insufficient: an
    agent-scoped key resolves to its owner CARRYING the owner's role, so on a
    default admin-owned install any agent's injected `TRINITY_MCP_API_KEY`
    would satisfy it (trinity-ops-agent#232; #1644/#1816 precedent). Blast
    radius here is operator-scale — external GitHub state, a persisted
    credential, and a container replacement.

    A thin HTTP mapper by design: orchestration lives in
    `services/agent_service/repo_binding.py` (Invariant #1). This function owns
    only the locks, the idempotency claim, and the audit row.
    """
    from services import idempotency_service
    from services.agent_service.repo_binding import (
        BindError,
        bind_agent_to_own_repo as _bind,
    )
    from utils.credential_sanitizer import scrub_secret_and_urls

    reject_agent_principal(current_user)

    destination = body.destination_repo
    scope = idempotency_service.make_agent_scope(agent_name)
    # Verb-folded key: a client reusing ONE Idempotency-Key across different
    # actions on the same agent must not replay the wrong snapshot
    # (learnings 2026-07-01). No key is derived when the caller omits the
    # header — unlike a webhook retry, this is a deliberate human action, and
    # a derived key would silently swallow an intentional re-bind to the same
    # destination (the documented recovery from a partial failure).
    folded_key = f"bind_to_own_repo:{idempotency_key}" if idempotency_key else None
    idem = idempotency_service.begin(scope, folded_key)

    async def _audit(success: bool, details: Dict) -> None:
        await _audit_git(
            action="bind_to_own_repo",
            request=request,
            current_user=current_user,
            agent_name=agent_name,
            success=success,
            details=details,
        )

    if idem.replay:
        # Audited like every other exit (#905). A replay is not a no-op from an
        # operator's point of view — it is the visible trace of a client that
        # retried a partially-irreversible external write, and reading the audit
        # log without it makes one bind look like one attempt.
        if idem.in_flight:
            await _audit(False, {
                "destination_repo": destination,
                "private": body.private,
                "code": "BIND_OP_IN_PROGRESS",
                "status_code": 409,
                "idempotent_replay": "in_flight",
            })
            raise HTTPException(
                status_code=409,
                detail={
                    "error": (
                        "A repository binding with this Idempotency-Key is "
                        "already in flight for this agent."
                    ),
                    "code": "BIND_OP_IN_PROGRESS",
                },
            )
        if idem.snapshot:
            await _audit(True, {
                "destination_repo": destination,
                "private": body.private,
                "idempotent_replay": "completed",
            })
            return JSONResponse(
                content=idem.snapshot,
                headers={"X-Idempotent-Replay": "true"},
            )

    try:
        async with _bind_locks(agent_name, destination):
            outcome = await _bind(
                agent_name=agent_name,
                destination_repo=destination,
                user_pat=body.github_pat.get_secret_value(),
                private=body.private,
                owner_username=current_user.username,
            )
    except BindError as e:
        # Audit BEFORE raising: #905 wants every exit path on a mutating,
        # partially-irreversible operation traceable, not just the happy one.
        await _audit(False, {
            "destination_repo": destination,
            "private": body.private,
            "code": e.code,
            "status_code": e.status_code,
            "partial": e.partial,
            **e.context,
        })
        with contextlib.suppress(Exception):
            idempotency_service.fail(idem)
        raise HTTPException(
            status_code=e.status_code,
            detail={"error": e.message, "code": e.code, "partial": e.partial},
        )
    except HTTPException as e:
        # Lock contention / 503 — no agent state was touched.
        await _audit(False, {
            "destination_repo": destination,
            "private": body.private,
            "code": (e.detail or {}).get("code") if isinstance(e.detail, dict) else None,
            "status_code": e.status_code,
        })
        with contextlib.suppress(Exception):
            idempotency_service.fail(idem)
        raise
    except Exception as e:
        # The ONE path that surfaces a raw exception string, so it is the one
        # that must scrub. An httpx/h11 header-validation error echoes the
        # offending header value verbatim — i.e. `Bearer <the user's PAT>` —
        # and `logger.exception` would put it in the Vector-captured platform
        # log while the detail put it in the response body. The model-level
        # charset guard (`models._validate_pat_secret`) is the primary fix;
        # this is the belt, because the next foreign exception type is not
        # knowable in advance.
        safe = scrub_secret_and_urls(str(e), body.github_pat.get_secret_value())
        logger.error(
            "repo-bind: unexpected failure for %s (%s): %s",
            agent_name, type(e).__name__, safe,
        )
        await _audit(False, {
            "destination_repo": destination,
            "private": body.private,
            "code": "BIND_UNEXPECTED_ERROR",
            "status_code": 500,
        })
        with contextlib.suppress(Exception):
            idempotency_service.fail(idem)
        raise HTTPException(
            status_code=500,
            detail={
                "error": f"Repository binding failed unexpectedly: {safe}",
                "code": "BIND_UNEXPECTED_ERROR",
            },
        )

    await _audit(True, outcome.audit)

    response = BindAgentRepoResponse(
        agent_name=agent_name,
        github_repo=outcome.github_repo,
        previous_repo=outcome.previous_repo,
        default_branch=outcome.default_branch,
        private=outcome.private,
        created_repo=outcome.created_repo,
        reused_existing=outcome.reused_existing,
        recreated=outcome.recreated,
        repo_url=f"https://github.com/{outcome.github_repo}",
        message=(
            f"This agent is now bound to {outcome.github_repo}. Its workspace "
            f"history was pushed to the '{outcome.default_branch}' branch, and "
            f"the previous repository is kept as the 'upstream' remote."
        ),
    )
    with contextlib.suppress(Exception):
        idempotency_service.complete(idem, None, response.model_dump())
    return response


@router.get("/{agent_name}/git/bind-to-own-repo/status")
async def get_bind_to_own_repo_status(agent_name: AuthorizedAgentByName):
    """Resolve the outcome of a binding whose HTTP response was lost (ent#109 §4.1).

    The binding's worst case — repo create + visibility poll + a full-history
    push + a container replacement — can outrun a proxy's idle timeout, and a
    client that eats a 504 otherwise cannot tell a completed bind from a failed
    one. This reports what the DB and the live container actually say, so the
    answer comes from state rather than from a remembered request.

    Read-only, and read-scoped (`AuthorizedAgentByName`) rather than
    owner-only: it discloses nothing the Git tab does not already show.
    """
    config = db.get_git_config(agent_name)
    if not config:
        return {
            "agent_name": agent_name,
            "bound": False,
            "message": "This agent has no GitHub sync configured.",
        }

    state = await git_service.inspect_container_git(agent_name)
    configured = config.github_repo
    observed = state.origin_repo
    in_sync = bool(observed) and observed.lower() == (configured or "").lower()

    return {
        "agent_name": agent_name,
        "bound": True,
        "github_repo": configured,
        "container_origin": observed,
        "branch": state.branch,
        "has_agent_pat": db.has_agent_github_pat(agent_name),
        # False means the DB commit landed but the in-container rewire or the
        # container rebuild did not — i.e. a partially applied bind. Retrying
        # the bind is the documented fix; it is idempotent.
        "origin_in_sync": in_sync,
    }


# ============================================================================
# Sync health observability (#389)
# ============================================================================


@router.get("/{agent_name}/git/auto-sync")
async def get_auto_sync_config(agent_name: AuthorizedAgentByName):
    """Return current auto-sync flag and interval for this agent (#389)."""
    config = db.get_git_config(agent_name)
    if not config:
        raise HTTPException(status_code=404, detail="Git not configured")
    value = getattr(config, "auto_sync_enabled", False)
    return {
        "agent_name": agent_name,
        "auto_sync_enabled": bool(value),
    }


@router.put("/{agent_name}/git/auto-sync")
async def set_auto_sync_config(
    agent_name: OwnedAgentByName,
    body: AutoSyncToggle,
):
    """Toggle the 15-min auto-sync heartbeat for this agent (#389)."""
    config = db.get_git_config(agent_name)
    if not config:
        raise HTTPException(status_code=404, detail="Git not configured")
    db.set_git_auto_sync_enabled(agent_name, body.enabled)
    return {"agent_name": agent_name, "auto_sync_enabled": body.enabled}


@router.get("/{agent_name}/git/freeze-schedules-if-failing")
async def get_freeze_schedules_config(agent_name: AuthorizedAgentByName):
    """Return whether scheduled executions should pause when sync is failing."""
    config = db.get_git_config(agent_name)
    if not config:
        raise HTTPException(status_code=404, detail="Git not configured")
    value = getattr(config, "freeze_schedules_if_sync_failing", False)
    return {
        "agent_name": agent_name,
        "freeze_schedules_if_sync_failing": bool(value),
    }


@router.put("/{agent_name}/git/freeze-schedules-if-failing")
async def set_freeze_schedules_config(
    agent_name: OwnedAgentByName,
    body: FreezeSchedulesToggle,
):
    """Toggle schedule-freeze-when-sync-failing for this agent (#389)."""
    config = db.get_git_config(agent_name)
    if not config:
        raise HTTPException(status_code=404, detail="Git not configured")
    db.set_freeze_schedules_if_sync_failing(agent_name, body.enabled)
    return {
        "agent_name": agent_name,
        "freeze_schedules_if_sync_failing": body.enabled,
    }


@router.get("/{agent_name}/git/sync-state")
async def get_agent_sync_state(agent_name: AuthorizedAgentByName):
    """Return the persisted sync-state row for this agent (#389)."""
    row = db.get_sync_state(agent_name)
    if row is None:
        return {
            "agent_name": agent_name,
            "last_sync_status": "never",
            "consecutive_failures": 0,
        }
    return row
