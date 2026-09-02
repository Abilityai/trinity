"""Repo provisioning — instance ids, working branches, remote probes, and `initialize_git_in_container`.

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


from . import gitignore, remotes

logger = logging.getLogger(__name__)

MAX_INSTANCE_ID_RETRIES = 5

_ANON_PROBE_DEFINITIVE_PATTERNS = (
    "could not read username",       # auth challenge with GIT_TERMINAL_PROMPT=0
    "authentication failed",
    "repository not found",
    "could not read password",
)

def generate_instance_id() -> str:
    """Generate a unique instance ID for an agent.

    NOTE (S7 Layer 0): this returns a raw UUID prefix with no remote/DB
    collision check. New call sites should use
    ``reserve_and_generate_instance_id`` instead; this is kept only for
    helpers that need the raw generator (e.g. inside the reserve helper).
    """
    return uuid.uuid4().hex[:8]


def generate_working_branch(agent_name: str, instance_id: str) -> str:
    """Generate a working branch name for an agent instance."""
    return f"trinity/{agent_name}/{instance_id}"


async def check_remote_branch_exists(github_repo: str, branch: str) -> bool:
    """Return True if ``refs/heads/<branch>`` exists on the remote.

    Uses ``git ls-remote`` so the check does not require the GitHub REST API
    or a specific auth mode — anything that can `git fetch` can also
    `git ls-remote`. Returns False on network/command errors: the caller
    treats that as "proceed with caution", since a stale "false" only costs
    us an extra DB-insert collision which Layer 2 catches.

    S7 Layer 0 — part of the pre-flight for ``reserve_and_generate_instance_id``.
    """
    # Prefer https://github.com/<repo>.git so the command works whether or
    # not the backend has a PAT configured. Public repos answer ls-remote
    # unauthenticated; private repos fall through to False and Layer 2
    # catches any duplicate insert.
    remote_url = f"https://github.com/{github_repo}.git"
    ref = f"refs/heads/{branch}"

    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "ls-remote",
            "--heads",
            "--exit-code",
            remote_url,
            ref,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=15.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning(
                "git ls-remote timed out for %s %s — treating as 'not present'",
                github_repo,
                branch,
            )
            return False
    except FileNotFoundError:
        logger.warning("git not installed on backend host; skipping remote branch check")
        return False
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "git ls-remote failed for %s %s: %s — treating as 'not present'",
            github_repo,
            branch,
            exc,
        )
        return False

    # --exit-code: 0 = ref found, 2 = not found. Anything else is an error
    # we log and treat as "not present" (Layer 2 catches real duplicates).
    if proc.returncode == 0:
        return bool(stdout.strip())
    if proc.returncode == 2:
        return False
    logger.warning(
        "git ls-remote %s %s exited %s — treating as 'not present'",
        github_repo,
        branch,
        proc.returncode,
    )
    return False


async def probe_anonymous_repo_access(github_repo: str) -> str:
    """Probe whether ``github_repo`` is clonable WITHOUT credentials (ent#123).

    Runs a credential-less ``git ls-remote <url> HEAD`` — the same transport
    the container's anonymous clone uses (so success here ≈ the clone will
    succeed) and immune to the anonymous REST 60/hr cap that makes
    ``GitHubService.check_repo_exists`` raise on 403.

    Returns one of:
      - ``"ok"``          — remote answered; public and reachable
      - ``"unavailable"`` — remote answered with an auth challenge / not-found;
                            anonymous GitHub cannot distinguish private from
                            nonexistent, so this is one combined outcome
      - ``"transient"``   — GitHub itself unreachable (timeout/DNS/no git);
                            says nothing about the repo
    """
    base = os.getenv("TRINITY_GIT_BASE_URL", "https://github.com").rstrip("/")
    remote_url = f"{base}/{github_repo}.git"

    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "ls-remote",
            remote_url,
            "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Force the deterministic fail-fast: an auth challenge becomes
            # "could not read Username" instead of a hang on a prompt.
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        try:
            _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning(
                "probe_anonymous_repo_access: ls-remote timed out for %s",
                github_repo,
            )
            return "transient"
    except FileNotFoundError:
        logger.warning(
            "probe_anonymous_repo_access: git not installed on backend host"
        )
        return "transient"
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning(
            "probe_anonymous_repo_access: ls-remote failed for %s: %s",
            github_repo,
            exc,
        )
        return "transient"

    if proc.returncode == 0:
        return "ok"

    stderr_text = (stderr or b"").decode("utf-8", errors="replace").lower()
    if any(p in stderr_text for p in _ANON_PROBE_DEFINITIVE_PATTERNS):
        return "unavailable"
    logger.warning(
        "probe_anonymous_repo_access: ls-remote for %s exited %s "
        "with unrecognized error — treating as transient",
        github_repo,
        proc.returncode,
    )
    return "transient"


async def reserve_and_generate_instance_id(
    agent_name: str,
    github_repo: str,
    source_branch: str = "main",
    source_mode: bool = False,
    sync_paths: Optional[List[str]] = None,
) -> Tuple[str, str]:
    """Atomically reserve a fresh working branch for an agent.

    S7 Layer 0 — single entry point for generating an instance ID. Combines:
      1. UUID generation
      2. ``git ls-remote`` probe against the remote (Layer 1)
      3. DB insert into ``agent_git_config`` under the partial UNIQUE index
         ``UNIQUE(github_repo, working_branch) WHERE source_mode = 0`` (Layer 2)

    Retries on either a remote hit or a DB IntegrityError up to
    ``MAX_INSTANCE_ID_RETRIES`` times, then raises ``RuntimeError``.

    For ``source_mode=True`` the branch is the source branch (e.g. ``main``),
    the remote probe is skipped (intentional shared-branch mode), and the DB
    insert bypasses the partial UNIQUE index by design.

    Returns:
        A ``(instance_id, working_branch)`` tuple. The DB row is already
        persisted when this function returns.

    Raises:
        RuntimeError: if ``MAX_INSTANCE_ID_RETRIES`` consecutive reservations
            collide on either the remote or the DB.
    """
    last_error: Optional[BaseException] = None

    for attempt in range(1, MAX_INSTANCE_ID_RETRIES + 1):
        if source_mode:
            # Source-mode agents share the source branch intentionally.
            instance_id = generate_instance_id()
            working_branch = source_branch
        else:
            instance_id = generate_instance_id()
            working_branch = generate_working_branch(agent_name, instance_id)

            if await check_remote_branch_exists(github_repo, working_branch):
                logger.warning(
                    "reserve_and_generate_instance_id: remote collision for %s "
                    "(attempt %d/%d)",
                    working_branch,
                    attempt,
                    MAX_INSTANCE_ID_RETRIES,
                )
                continue

        try:
            config = db.create_git_config(
                agent_name=agent_name,
                github_repo=github_repo,
                working_branch=working_branch,
                instance_id=instance_id,
                sync_paths=sync_paths,
                source_branch=source_branch,
                source_mode=source_mode,
            )
        except IntegrityError as exc:
            last_error = exc
            # The partial UNIQUE index on (github_repo, working_branch) WHERE
            # source_mode = 0 fired — another agent already owns this branch.
            # Retry with a fresh UUID. (#300: db.create_git_config now raises
            # sqlalchemy.exc.IntegrityError on both backends.)
            logger.warning(
                "reserve_and_generate_instance_id: DB collision for %s "
                "(attempt %d/%d): %s",
                working_branch,
                attempt,
                MAX_INSTANCE_ID_RETRIES,
                exc,
            )
            continue

        if config is None:
            # create_git_config returns None on a plain agent_name UNIQUE
            # violation — this is a different bug (agent already has config)
            # and should not be silently retried. Surface immediately.
            raise RuntimeError(
                f"reserve_and_generate_instance_id: agent_git_config already "
                f"exists for agent {agent_name!r}"
            )

        return instance_id, working_branch

    raise RuntimeError(
        f"reserve_and_generate_instance_id: could not reserve a fresh working "
        f"branch for {agent_name!r} in {github_repo!r} after "
        f"{MAX_INSTANCE_ID_RETRIES} retries (last error: {last_error!r})"
    )


async def create_git_config_for_agent(
    agent_name: str,
    github_repo: str,
    instance_id: Optional[str] = None
) -> AgentGitConfig:
    """
    Create git configuration for a new agent.

    Args:
        agent_name: Name of the agent
        github_repo: GitHub repository (e.g., "Abilityai/agent-ruby")
        instance_id: Optional instance ID (generated if not provided)

    Returns:
        AgentGitConfig with the configuration
    """
    if not instance_id:
        instance_id = generate_instance_id()

    working_branch = generate_working_branch(agent_name, instance_id)

    # Create the database record
    config = db.create_git_config(
        agent_name=agent_name,
        github_repo=github_repo,
        working_branch=working_branch,
        instance_id=instance_id
    )

    return config


@dataclass
class GitInitResult:
    """Result of git initialization in container."""
    success: bool
    git_dir: str
    working_branch: Optional[str] = None
    error: Optional[str] = None


async def initialize_git_in_container(
    agent_name: str,
    github_repo: str,
    github_pat: str,
    create_working_branch: bool = True,
    working_branch: Optional[str] = None,
) -> GitInitResult:
    """
    Initialize git in an agent container.

    Performs:
    1. Detect git directory (workspace or home)
    2. Create .gitignore
    3. Initialize git repo
    4. Configure remote
    5. Create initial commit
    6. Push to GitHub
    7. Create working branch (optional; prefer the pre-reserved path)

    Args:
        agent_name: Name of the agent container
        github_repo: Full repo name (e.g., "owner/repo")
        github_pat: GitHub PAT for authentication
        create_working_branch: DEPRECATED (S7 Layer 0 / #382). When True the
            helper generates an instance ID internally, bypassing the
            `reserve_and_generate_instance_id` collision check. New callers
            MUST pre-reserve via `reserve_and_generate_instance_id` and pass
            `create_working_branch=False, working_branch=<reserved>` instead.
        working_branch: Pre-reserved working branch name (e.g.
            ``trinity/<agent>/<id>``). Required when
            ``create_working_branch=False``. Mutually exclusive with
            internal generation — when set, this function just checks out /
            pushes that branch.

    Returns:
        GitInitResult with status and branch info
    """
    container_name = f"agent-{agent_name}"

    # Step 1: Determine git directory (workspace for legacy agents, else home).
    # Detection logic is shared with `_migrate_workspace_gitignore` so the
    # post-init Push migration targets the same path.
    git_dir = await gitignore._detect_git_dir(container_name)
    if git_dir == "/home/developer/workspace":
        logger.info(f"[LEGACY] Using workspace directory with existing content: {git_dir}")
    else:
        logger.info(f"Using home directory: {git_dir}")

    # Step 2: Append any missing `_GITIGNORE_PATTERNS` entries to the
    # agent's `.gitignore`. Runs for BOTH `/home/developer` and the legacy
    # `/home/developer/workspace` path — previously the legacy branch was
    # skipped entirely, and the home path used `cat > .gitignore` which
    # clobbered any workspace-supplied rules (including `.env` / `.mcp.json`
    # added by `/trinity:onboard`). The merge is idempotent.
    await execute_command_in_container(
        container_name=container_name,
        command=gitignore._build_gitignore_merge_command(git_dir),
        timeout=5,
    )

    # Step 3: Initialize git and try to preserve remote history
    # Commands marked required=True will abort on failure;
    # optional commands (like fetch) may fail for empty repos.
    setup_commands: list[tuple[str, bool]] = [
        ('git config --global user.email "trinity@agent.local"', True),
        ('git config --global user.name "Trinity Agent"', True),
        ('git config --global init.defaultBranch main', True),
        # #1595: auto-gc always detaches to PID 1 and is SIGKILLed by the
        # orphan sweep — disable it; the agent-server's registered maintenance
        # pass owns repo upkeep. Global (volume-persisted ~/.gitconfig) so
        # agents on older base images pick it up on the next sync init.
        ('git config --global gc.auto 0', True),
        ('git config --global gc.autoDetach false', True),
        ('git config --global maintenance.auto false', True),
        ('git config --global maintenance.autoDetach false', True),
        ('git init', True),
        (remotes._remote_seturl_subcommand(remotes._git_remote_url(github_pat, github_repo)), True),
        ('git fetch origin', False),  # Optional — remote may be empty
    ]

    for cmd, required in setup_commands:
        result = await execute_command_in_container(
            container_name=container_name,
            command=f'bash -c "cd {git_dir} && {cmd}"',
            timeout=60
        )
        if result.get("exit_code", 0) != 0 and required:
            output = result.get("output", "")
            return GitInitResult(
                success=False,
                git_dir=git_dir,
                error=f"Git command failed: {cmd}\nOutput: {output}"
            )

    # Check if remote has commits on main (to preserve history)
    check_main = await execute_command_in_container(
        container_name=container_name,
        command=f'bash -c "cd {git_dir} && git rev-parse --verify origin/main"',
        timeout=10
    )
    remote_has_main = check_main.get("exit_code", 1) == 0

    if remote_has_main:
        # Preserve remote history: reset index to origin/main, then stage
        # the current workspace on top of it and fast-forward push.
        commit_commands = [
            'git reset origin/main',
            'git add .',
            'git commit -m "Initial commit from Trinity Agent" || echo "Nothing to commit"',
            # Always set upstream; no-op when there is nothing new to push.
            'git push -u origin main',
        ]
    else:
        # Empty repo: force push creates the initial history.
        commit_commands = [
            'git add .',
            'git commit -m "Initial commit from Trinity Agent" || echo "Nothing to commit"',
            'git push -u origin main --force',
        ]

    for cmd in commit_commands:
        result = await execute_command_in_container(
            container_name=container_name,
            command=f'bash -c "cd {git_dir} && {cmd}"',
            timeout=60
        )
        if result.get("exit_code", 0) != 0:
            output = result.get("output", "")
            if "Nothing to commit" not in output:
                return GitInitResult(
                    success=False,
                    git_dir=git_dir,
                    error=f"Git command failed: {cmd}\nOutput: {output}"
                )

    # Step 4: Create (or check out) the working branch.
    # S7 Layer 0 (#382): prefer the pre-reserved path — callers pass
    # `working_branch=<reserved>` and `create_working_branch=False`. The
    # legacy `create_working_branch=True` path falls back to an internal
    # `generate_instance_id()` call and is deprecated; it's kept so older
    # callers don't break, but emits a warning on every use.
    if working_branch is not None:
        branch_commands = [
            f"git checkout -b {working_branch}",
            f"git push -u origin {working_branch}",
        ]
        for cmd in branch_commands:
            result = await execute_command_in_container(
                container_name=container_name,
                command=f'bash -c "cd {git_dir} && {cmd}"',
                timeout=60,
            )
            if result.get("exit_code", 0) != 0:
                logger.warning(
                    "Failed to create pre-reserved working branch %s: %s",
                    working_branch,
                    result.get("output", ""),
                )
    elif create_working_branch:
        # Deprecated path — no caller should hit this after S7 rolls out.
        logger.warning(
            "initialize_git_in_container(create_working_branch=True) is "
            "deprecated (S7 / #382). Pre-reserve via "
            "reserve_and_generate_instance_id and pass working_branch "
            "explicitly."
        )
        instance_id = generate_instance_id()
        working_branch = generate_working_branch(agent_name, instance_id)

        branch_commands = [
            f'git checkout -b {working_branch}',
            f'git push -u origin {working_branch}'
        ]

        for cmd in branch_commands:
            result = await execute_command_in_container(
                container_name=container_name,
                command=f'bash -c "cd {git_dir} && {cmd}"',
                timeout=60
            )
            if result.get("exit_code", 0) != 0:
                # Working branch creation is optional - log but don't fail
                logger.warning(f"Failed to create working branch: {result.get('output', '')}")

    # Step 5: Verify
    verify_result = await execute_command_in_container(
        container_name=container_name,
        command=f'bash -c "cd {git_dir} && git rev-parse --git-dir"',
        timeout=5
    )

    if verify_result.get("exit_code", 0) != 0:
        return GitInitResult(
            success=False,
            git_dir=git_dir,
            error="Git initialization verification failed"
        )

    logger.info(f"Git initialization verified successfully in {git_dir}")

    return GitInitResult(
        success=True,
        git_dir=git_dir,
        working_branch=working_branch
    )


async def check_git_initialized(agent_name: str) -> Optional[str]:
    """
    Check if git is initialized in an agent container.

    Args:
        agent_name: Name of the agent

    Returns:
        The git directory path if initialized, None otherwise
    """
    container_name = f"agent-{agent_name}"

    # NOTE: The workspace check is LEGACY support for agents created before 2026-02.
    # New agents use /home/developer directly.
    result = await execute_command_in_container(
        container_name=container_name,
        command='bash -c "[ -d /home/developer/workspace/.git ] && echo workspace || ([ -d /home/developer/.git ] && echo home || echo notexists)"',
        timeout=5
    )

    output = result.get("output", "").strip()

    if "workspace" in output:
        # Legacy agent with workspace subdirectory
        return "/home/developer/workspace"
    elif "home" in output:
        # Standard path for all current agents
        return "/home/developer"

    return None


