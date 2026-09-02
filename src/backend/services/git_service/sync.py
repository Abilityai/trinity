"""The sync verbs the routes call — status, sync, log, pull, and the S3 reset-preserve-state recovery.

Carved out of the 2,322-line `services/git_service.py` (#1028). The package
`__init__` re-exports the public surface, so `from services.git_service
import …` and `git_service.<name>` callers are unchanged.

Cross-module calls go THROUGH the sibling module object
(`gitignore._detect_git_dir(...)`, never `from .gitignore import
_detect_git_dir`) so a test that patches the owning module reaches every
caller — a from-import freezes the binding and quietly detaches such a
patch.
"""
import asyncio
import os
import re
import shlex
import uuid
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, Any, List, Tuple
from sqlalchemy.exc import IntegrityError
from database import db, AgentGitConfig, GitSyncResult
from services.agent_auth import agent_httpx_client
from services.docker_service import get_agent_container, execute_command_in_container
from utils.credential_sanitizer import scrub_secret_and_urls
from utils.safe_yaml import (  # ent#314
    AliasPolicy as _AliasPolicy,
    HardenedYamlError as _HardenedYamlError,
    load_hardened_yaml as _load_hardened_yaml,
)

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Conflict classification (S5 — operator-readable diagnosis, issue #386)
# ----------------------------------------------------------------------------


from . import conflicts, gitignore

logger = logging.getLogger(__name__)

async def get_git_status(agent_name: str) -> Optional[Dict[str, Any]]:
    """
    Get git status for an agent by calling the agent's internal API.

    Returns git status including branch, changes, and sync state.
    """
    container = get_agent_container(agent_name)
    if not container or container.status != "running":
        return None

    try:
        # Call the agent's internal git status endpoint
        async with agent_httpx_client(agent_name, timeout=30.0) as client:
            response = await client.get(
                f"http://agent-{agent_name}:8000/api/git/status"
            )
            if response.status_code == 200:
                return response.json()
            return None
    except Exception as e:
        # #1561: structured logging, not a bare print() — otherwise these
        # failures have no level/timestamp and are invisible to log-based alerting.
        logger.warning("Error getting git status for %s: %s", agent_name, e)
        return None


def _agent_has_write_credentials(agent_name: str, container) -> bool:
    """True if the agent can plausibly push (ent#123 tokenless guard).

    Predicate = the container's baked ``GITHUB_PAT`` env **or** a per-agent
    PAT row. The OR matters: ``set_agent_github_pat`` live-injects the token
    into the workspace ``.env`` and rewrites origin (``update_remote_pat``,
    #1264) BEFORE any recreate, so baked env alone would block the user who
    just fixed the problem. The global tier is deliberately excluded — a
    global PAT never reaches a tokenless container's remote.

    Fail-open: any error reading either source returns True so this guard
    can only ever produce a clearer message, never block a working push.
    """
    try:
        env_list = container.attrs.get("Config", {}).get("Env", []) or []
        for entry in env_list:
            if entry.startswith("GITHUB_PAT=") and len(entry) > len("GITHUB_PAT="):
                return True
        return bool(db.get_agent_github_pat(agent_name))
    except Exception as exc:  # noqa: BLE001 — guard must never break push
        logger.warning(
            "_agent_has_write_credentials: check failed for %s: %s — "
            "failing open", agent_name, exc,
        )
        return True


async def sync_to_github(
    agent_name: str,
    message: Optional[str] = None,
    paths: Optional[list] = None,
    strategy: Optional[str] = "normal"
) -> GitSyncResult:
    """
    Sync agent changes to GitHub.

    Calls the agent's internal sync endpoint to stage, commit, and push changes.

    Args:
        agent_name: Name of the agent
        message: Optional custom commit message
        paths: Optional specific paths to sync (default: all)
        strategy: Sync strategy - "normal", "pull_first", "force_push"

    Returns:
        GitSyncResult with sync outcome
    """
    container = get_agent_container(agent_name)
    if not container:
        return GitSyncResult(
            success=False,
            message="Agent not found"
        )

    if container.status != "running":
        return GitSyncResult(
            success=False,
            message="Agent must be running to sync"
        )

    # ent#123: a tokenless (anonymous public-template) agent has no push
    # credentials — fail with an honest, actionable message instead of
    # letting the in-container push die on a cryptic auth prompt.
    if not _agent_has_write_credentials(agent_name, container):
        return GitSyncResult(
            success=False,
            message=conflicts.NO_WRITE_CREDENTIALS_MESSAGE,
            conflict_type="no_write_credentials",
            conflict_class="AUTH_FAILURE",
        )

    # #462: bring the workspace `.gitignore` up to the current canonical list
    # and untrack any files that NOW match a rule. Runs on every Push so
    # existing agents migrate without re-init or container rebuild. Best
    # effort — failures are logged inside the helper and Push proceeds.
    await gitignore._migrate_workspace_gitignore(agent_name)

    try:
        # Call the agent's internal sync endpoint
        async with agent_httpx_client(agent_name, timeout=360.0) as client:
            payload = {"strategy": strategy}
            if message:
                payload["message"] = message
            if paths:
                payload["paths"] = paths

            response = await client.post(
                f"http://agent-{agent_name}:8000/api/git/sync",
                json=payload
            )

            if response.status_code == 200:
                data = response.json()

                # Update database with sync result
                if data.get("commit_sha"):
                    db.update_git_sync(agent_name, data["commit_sha"])

                return GitSyncResult(
                    success=data.get("success", False),
                    commit_sha=data.get("commit_sha"),
                    message=data.get("message", "Sync completed"),
                    files_changed=data.get("files_changed", 0),
                    branch=data.get("branch"),
                    sync_time=datetime.fromisoformat(data["sync_time"]) if data.get("sync_time") else datetime.utcnow()
                )
            elif response.status_code == 409:
                # Conflict - return with conflict info
                data = response.json()
                conflict_type = response.headers.get("X-Conflict-Type", "unknown")
                # S5 #386: pull operator-readable class from body (added by agent
                # server); fall back to header or UNKNOWN for older agent images.
                conflict_class = (
                    data.get("conflict_class")
                    or response.headers.get("X-Conflict-Class")
                    or "UNKNOWN"
                )
                return GitSyncResult(
                    success=False,
                    message=data.get("detail", "Sync conflict"),
                    conflict_type=conflict_type,
                    conflict_class=conflict_class,
                )
            else:
                error_detail = response.json().get("detail", "Sync failed")
                return GitSyncResult(
                    success=False,
                    message=f"Sync failed: {error_detail}"
                )
    except Exception as e:
        return GitSyncResult(
            success=False,
            message=f"Sync error: {str(e)}"
        )


async def get_git_log(agent_name: str, limit: int = 10) -> Optional[Dict[str, Any]]:
    """
    Get recent git commits for an agent.

    Returns list of commits with SHA, message, author, and date.
    """
    container = get_agent_container(agent_name)
    if not container or container.status != "running":
        return None

    try:
        async with agent_httpx_client(agent_name, timeout=30.0) as client:
            response = await client.get(
                f"http://agent-{agent_name}:8000/api/git/log",
                params={"limit": limit}
            )
            if response.status_code == 200:
                return response.json()
            return None
    except Exception as e:
        print(f"Error getting git log for {agent_name}: {e}")
        return None


async def pull_from_github(agent_name: str, strategy: Optional[str] = "clean") -> Dict[str, Any]:
    """
    Pull latest changes from GitHub to the agent.

    Args:
        agent_name: Name of the agent
        strategy: Pull strategy - "clean", "stash_reapply", "force_reset"

    Returns:
        Dict with pull result and conflict info if applicable
    """
    container = get_agent_container(agent_name)
    if not container:
        return {"success": False, "message": "Agent not found"}

    if container.status != "running":
        return {"success": False, "message": "Agent must be running to pull"}

    try:
        async with agent_httpx_client(agent_name, timeout=120.0) as client:
            response = await client.post(
                f"http://agent-{agent_name}:8000/api/git/pull",
                json={"strategy": strategy}
            )

            if response.status_code == 200:
                return response.json()
            elif response.status_code == 409:
                # Conflict detected
                data = response.json()
                conflict_type = response.headers.get("X-Conflict-Type", "unknown")
                conflict_class = (
                    data.get("conflict_class")
                    or response.headers.get("X-Conflict-Class")
                    or "UNKNOWN"
                )
                return {
                    "success": False,
                    "message": data.get("detail", "Pull conflict"),
                    "conflict_type": conflict_type,
                    "conflict_class": conflict_class,
                }
            else:
                error_detail = response.json().get("detail", "Pull failed")
                return {"success": False, "message": f"Pull failed: {error_detail}"}
    except Exception as e:
        return {"success": False, "message": f"Pull error: {str(e)}"}


def get_agent_git_config(agent_name: str) -> Optional[AgentGitConfig]:
    """Get git configuration for an agent from the database."""
    return db.get_git_config(agent_name)


def delete_agent_git_config(agent_name: str) -> bool:
    """Delete git configuration when an agent is deleted."""
    return db.delete_git_config(agent_name)


async def reset_to_main_preserve_state(agent_name: str) -> Dict[str, Any]:
    """Proxy the reset-preserve-state operation to the agent-server.

    Adds one guardrail on top of the agent-server's own checks: refuse if
    the agent is currently executing a task. The activity service is a
    backend-only view, so this check cannot live in the agent-server.

    Returns a dict shaped for the router to translate into HTTP responses:

    - Success: `{snapshot_dir, files_preserved, commit_sha, working_branch}`
    - Guard tripped: `{"error": "agent_busy" | "no_git_config" | ...,
                       "message": "..."}`
    """
    # Imported here (not at module top) so test suites that stub the
    # activity service via sys.modules can control the dependency without
    # triggering docker_service's heavy imports at git_service load time.
    from services.activity_service import activity_service

    current = await activity_service.get_current_activities(agent_name)
    if current:
        return {
            "error": "agent_busy",
            "message": (
                f"Agent {agent_name} is currently executing a task. "
                "Wait for it to finish before resetting."
            ),
        }

    # ent#123: the recovery ends in a force-with-lease PUSH — refuse up front
    # for a tokenless agent with the same honest message as sync.
    container = get_agent_container(agent_name)
    if container and not _agent_has_write_credentials(agent_name, container):
        return {
            "error": "no_write_credentials",
            "message": conflicts.NO_WRITE_CREDENTIALS_MESSAGE,
        }

    async with agent_httpx_client(agent_name, timeout=180.0) as client:
        response = await client.post(
            f"http://agent-{agent_name}:8000/api/git/reset-to-main-preserve-state"
        )
        if response.status_code == 200:
            return response.json()
        if response.status_code == 409:
            detail = ""
            try:
                detail = response.json().get("detail", "") or ""
            except Exception:  # noqa: BLE001
                detail = response.text
            return {
                "error": response.headers.get("X-Conflict-Type", "conflict"),
                "message": detail,
            }
        return {
            "error": "proxy_failed",
            "message": response.text[:500],
            "status_code": response.status_code,
        }


