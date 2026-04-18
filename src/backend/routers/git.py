"""
Git synchronization routes for GitHub-native agents (Phase 7).

Provides API endpoints for:
- Getting git status
- Syncing changes to GitHub
- Viewing commit history
- Pulling from GitHub
"""
from typing import Dict, Optional, List
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from models import User
from database import db
from dependencies import get_current_user, AuthorizedAgentByName, OwnedAgentByName
from services import git_service

router = APIRouter(prefix="/api/agents", tags=["git"])


class GitSyncRequest(BaseModel):
    """Request body for git sync operation."""
    message: Optional[str] = None  # Custom commit message
    paths: Optional[List[str]] = None  # Specific paths to sync
    strategy: Optional[str] = "normal"  # "normal", "pull_first", "force_push"


class GitPullRequest(BaseModel):
    """Request body for git pull operation."""
    strategy: Optional[str] = "clean"  # "clean", "stash_reapply", "force_reset"


class GitInitializeRequest(BaseModel):
    """Request body for git initialization."""
    repo_owner: str  # GitHub username or organization
    repo_name: str  # Repository name
    create_repo: bool = True  # Whether to create the repository if it doesn't exist
    private: bool = True  # Whether the new repository should be private
    description: Optional[str] = None  # Repository description


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
    body: GitSyncRequest = GitSyncRequest()
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
        raise HTTPException(
            status_code=status_code,
            detail=result.message,
            headers=conflict_headers,
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
    body: GitPullRequest = GitPullRequest()
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
        raise HTTPException(
            status_code=status_code,
            detail=result.get("message"),
            headers=conflict_headers,
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
    request: Request
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
    - GitHub PAT configured in system settings
    - Agent must be running
    - User must be agent owner
    """
    from services.docker_service import get_agent_container
    from services.settings_service import get_github_pat
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
            db.execute_query("DELETE FROM agent_git_config WHERE agent_name = ?", (agent_name,))

    # Get GitHub PAT from settings
    github_pat = get_github_pat()
    if not github_pat:
        raise HTTPException(
            status_code=400,
            detail="GitHub Personal Access Token not configured. Please add it in Settings."
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

        # Step 2: Initialize git in container (using git_service)
        init_result = await git_service.initialize_git_in_container(
            agent_name=agent_name,
            github_repo=repo_full_name,
            github_pat=github_pat
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

        # Step 3: Store configuration in database
        instance_id = git_service.generate_instance_id()
        config = await git_service.create_git_config_for_agent(
            agent_name=agent_name,
            github_repo=repo_full_name,
            instance_id=instance_id
        )

        return {
            "success": True,
            "message": "GitHub sync initialized successfully",
            "github_repo": repo_full_name,
            "working_branch": init_result.working_branch,
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

def get_github_pat_for_agent(agent_name: str) -> str:
    """
    Get GitHub PAT for an agent, with fallback to global PAT.

    Priority:
    1. Per-agent PAT (if configured and decryption succeeds)
    2. Global PAT from system settings / env var

    Args:
        agent_name: Name of the agent

    Returns:
        GitHub PAT string (may be empty if neither configured)
    """
    from services.settings_service import get_github_pat

    # Try per-agent PAT first
    agent_pat = db.get_agent_github_pat(agent_name)
    if agent_pat:
        return agent_pat

    # Fall back to global PAT
    return get_github_pat()


class GitHubPATRequest(BaseModel):
    """Request body for setting agent GitHub PAT."""
    pat: str


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

    return {
        "message": "GitHub PAT configured successfully",
        "agent_name": agent_name,
        "github_username": username,
        "source": "agent",
        "note": "Restart agent for new PAT to be used in git operations"
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
