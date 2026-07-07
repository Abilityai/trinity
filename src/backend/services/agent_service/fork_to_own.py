"""
Fork-to-own template copy (trinity-enterprise#93).

Copies a GitHub template repo into a repo the USER owns before the agent is
created from it: create (private by default) destination via the user's PAT,
bare-clone the template's default branch (full history — keeps
`git pull upstream main` clean), push it to the destination, and wait until
the git plane can see the branch so the agent's startup clone can't race an
empty repo (#1439 class).

Placement contract (review decision #12): everything here runs in the
`github:` branch of `create_agent_internal` BEFORE the docker try-block, so
the structured HTTPException details below reach the UI instead of being
flattened to a generic 500 by crud.py's catch-all.

Security:
- The user PAT is write-side only; the platform PAT (or nothing — public
  templates) is read-side only.
- Git auth travels via GIT_CONFIG_* env vars (http.extraHeader), never in the
  remote URL, so the token can't appear in `/proc/<pid>/cmdline` on the
  backend host.
- All error text is passed through `scrub_secret` before it can reach logs or
  HTTP details.
"""
import asyncio
import base64
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException

from services.github_service import GitHubService, GitHubError, OwnerType

logger = logging.getLogger(__name__)

# Bounded ops (seconds). Poll caps are generous for GitHub's eventual
# consistency; a miss surfaces a retryable 502 and the SHA-match reuse path
# makes the retry cheap.
CLONE_TIMEOUT_S = 120
PUSH_TIMEOUT_S = 120
CREATE_VISIBILITY_TIMEOUT_S = 10
PUSH_VISIBILITY_TIMEOUT_S = 15
_POLL_INTERVAL_S = 1.0

# Staging for the bare clone. The backend's /tmp is a 100 MB RAM tmpfs
# (docker-compose `tmpfs: /tmp:...size=100m`) — a full-history template clone
# there risks ENOSPC and counts against the memory limit. /data is the
# disk-backed bind mount (precedent: /data/agent-data-tmp, #1169); fall back
# to the default tmp outside the container (dev shells, tests).
_STAGING_ROOT = Path("/data/agent-fork-tmp")


def _make_staging_dir() -> str:
    try:
        _STAGING_ROOT.mkdir(parents=True, exist_ok=True)
        return tempfile.mkdtemp(prefix="fork-", dir=str(_STAGING_ROOT))
    except OSError:
        return tempfile.mkdtemp(prefix="fork-to-own-")


@dataclass
class ForkToOwnResult:
    destination_repo: str
    default_branch: str
    reused_existing: bool


def scrub_secret(text: str, secret: str) -> str:
    """Replace every occurrence of ``secret`` (and its b64 form) in ``text``."""
    if not text:
        return text or ""
    if secret:
        text = text.replace(secret, "***")
        b64 = base64.b64encode(f"x-access-token:{secret}".encode()).decode()
        text = text.replace(b64, "***")
    return text


def _http_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"error": message, "code": code})


def _git_auth_env(pat: str) -> dict:
    """Git auth as config-via-env: header never appears on the argv."""
    if not pat:
        return {}
    b64 = base64.b64encode(f"x-access-token:{pat}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: basic {b64}",
    }


async def _run_git(args: list, timeout: float, auth_pat: str = "") -> tuple:
    """Run a git command with optional env-carried auth. Returns (rc, out+err).

    Output is scrubbed of the auth token before returning, so callers can
    embed it in logs/errors safely.
    """
    env = {
        **os.environ,  # PATH etc. — exec lookup needs it
        "HOME": "/tmp",  # deterministic: never read user-level git config
        "GIT_TERMINAL_PROMPT": "0",
        **_git_auth_env(auth_pat),
    }
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return -1, f"git {args[0]} timed out after {timeout:.0f}s"
    except FileNotFoundError:
        return -1, "git is not installed on the backend host"
    output = (stdout or b"").decode(errors="replace")
    return proc.returncode, scrub_secret(output, auth_pat)


async def _wait_for_repo_visible(gh: GitHubService, repo: str) -> None:
    """Poll the REST plane until the freshly created repo resolves."""
    deadline = asyncio.get_event_loop().time() + CREATE_VISIBILITY_TIMEOUT_S
    owner, name = repo.split("/", 1)
    while True:
        try:
            info = await gh.check_repo_exists(owner, name)
            if info.exists:
                return
        except GitHubError:
            pass
        if asyncio.get_event_loop().time() >= deadline:
            raise _http_error(
                502, "FORK_REPO_NOT_VISIBLE",
                f"GitHub is still propagating '{repo}'. The repository was "
                f"created — retry in a moment and it will be reused.",
            )
        await asyncio.sleep(_POLL_INTERVAL_S)


async def _wait_for_branch_on_git_plane(repo: str, branch: str, pat: str) -> None:
    """Poll `git ls-remote` (the plane the agent's clone reads) for the ref."""
    url = f"https://github.com/{repo}.git"
    deadline = asyncio.get_event_loop().time() + PUSH_VISIBILITY_TIMEOUT_S
    while True:
        rc, out = await _run_git(
            ["ls-remote", "--heads", url, f"refs/heads/{branch}"],
            timeout=15.0, auth_pat=pat,
        )
        if rc == 0 and out.strip():
            return
        if asyncio.get_event_loop().time() >= deadline:
            raise _http_error(
                502, "FORK_PUSH_NOT_VISIBLE",
                f"'{repo}' was created and populated but GitHub has not "
                f"finished propagating branch '{branch}'. Retry in a moment "
                f"— the existing copy will be reused.",
            )
        await asyncio.sleep(_POLL_INTERVAL_S)


async def _resolve_template_tip(template_repo: str, read_pat: str) -> tuple:
    """(default_branch, head_sha) of the template repo, via the read-side PAT."""
    gh = GitHubService(read_pat)
    owner, name = template_repo.split("/", 1)
    try:
        info = await gh.check_repo_exists(owner, name)
    except GitHubError as e:
        raise _http_error(502, "FORK_TEMPLATE_UNREACHABLE",
                          f"Could not read template '{template_repo}': {e}")
    if not info.exists:
        raise _http_error(
            400, "FORK_TEMPLATE_NOT_FOUND",
            f"Template repository '{template_repo}' not found or not readable.",
        )
    default_branch = info.default_branch or "main"
    try:
        head_sha = await gh.get_branch_sha(template_repo, default_branch)
    except GitHubError as e:
        raise _http_error(502, "FORK_TEMPLATE_UNREACHABLE",
                          f"Could not read template branch: {e}")
    if not head_sha:
        raise _http_error(
            400, "FORK_TEMPLATE_EMPTY",
            f"Template '{template_repo}' has no '{default_branch}' branch to copy.",
        )
    return default_branch, head_sha


async def fork_template_to_own_repo(
    template_repo: str,
    destination_repo: str,
    user_pat: str,
    read_pat: str,
    private: bool,
) -> ForkToOwnResult:
    """Copy ``template_repo`` into the user-owned ``destination_repo``.

    Returns the destination + its default branch (same name as the
    template's). Raises HTTPException with structured detail codes on every
    failure path — see the Error & Rescue registry in the plan.
    """
    user_gh = GitHubService(user_pat)

    valid, login = await user_gh.validate_token()
    if not valid:
        raise _http_error(
            400, "FORK_PAT_INVALID",
            "GitHub token is invalid or expired. It must be able to create "
            "repositories (classic PAT with 'repo' scope, or a fine-grained "
            "token with Administration + Contents write).",
        )

    dest_owner, dest_name = destination_repo.split("/", 1)
    template_default, template_sha = await _resolve_template_tip(template_repo, read_pat)

    # Destination state → create / reuse / refuse.
    try:
        dest_info = await user_gh.check_repo_exists(dest_owner, dest_name)
    except GitHubError as e:
        raise _http_error(502, "FORK_DESTINATION_UNREACHABLE",
                          f"Could not check '{destination_repo}': {e}")

    reused = False
    if dest_info.exists:
        try:
            dest_branches = await user_gh.list_branches(destination_repo, limit=10)
        except GitHubError as e:
            raise _http_error(502, "FORK_DESTINATION_UNREACHABLE",
                              f"Could not inspect '{destination_repo}': {e}")
        if not dest_branches:
            # Empty repo (prior failed attempt, or pre-created without a
            # README) — push into it.
            reused = True
        elif (
            len(dest_branches) == 1
            and dest_branches[0]["name"] == template_default
            and dest_branches[0]["sha"] == template_sha
        ):
            # Exactly the template tip and nothing else: a prior attempt got
            # through the copy but failed later. Idempotent — skip the push.
            logger.info(
                "fork-to-own: destination %s already holds the template tip; reusing",
                destination_repo,
            )
            return ForkToOwnResult(destination_repo, template_default, True)
        else:
            raise _http_error(
                409, "FORK_DESTINATION_EXISTS",
                f"Repository '{destination_repo}' already exists and contains "
                f"data. Choose another name, or delete the repository if it "
                f"was created by a previous failed attempt. (A repo "
                f"pre-created WITH a README also lands here — create it "
                f"empty, or let Trinity create it.)",
            )
    else:
        # PAT can only create under its own user account (or an org it has
        # rights in — the org endpoint enforces that itself).
        owner_type = await user_gh.get_owner_type(dest_owner)
        if owner_type == OwnerType.USER and login and dest_owner.lower() != login.lower():
            raise _http_error(
                400, "FORK_DESTINATION_FORBIDDEN",
                f"The token belongs to '{login}' and cannot create a "
                f"repository under user '{dest_owner}'. Use your own account "
                f"or an organization you can create repositories in.",
            )
        result = await user_gh.create_repository(
            dest_owner, dest_name, private=private,
            description=f"Trinity agent workspace (from {template_repo})",
        )
        if not result.success:
            raise _http_error(
                400, "FORK_REPO_CREATE_FAILED",
                f"Could not create '{destination_repo}': "
                f"{scrub_secret(result.error or 'unknown error', user_pat)}",
            )
        await _wait_for_repo_visible(user_gh, destination_repo)

    # Copy: bare single-branch clone of the template (read auth), push the
    # branch to the destination (user auth). Full history — upstream pulls
    # stay merge-clean.
    tmp_dir = _make_staging_dir()
    try:
        template_url = f"https://github.com/{template_repo}.git"
        rc, out = await _run_git(
            ["clone", "--bare", "--single-branch", "--branch", template_default,
             template_url, tmp_dir],
            timeout=CLONE_TIMEOUT_S, auth_pat=read_pat,
        )
        if rc != 0:
            logger.error("fork-to-own: template clone failed for %s: %s",
                         template_repo, scrub_secret(out, read_pat))
            raise _http_error(
                502, "FORK_TEMPLATE_CLONE_FAILED",
                f"Could not clone template '{template_repo}'. The empty "
                f"repository '{destination_repo}' remains and will be reused "
                f"on retry.",
            )

        dest_url = f"https://github.com/{destination_repo}.git"
        rc, out = await _run_git(
            ["-C", tmp_dir, "push", dest_url,
             f"refs/heads/{template_default}:refs/heads/{template_default}"],
            timeout=PUSH_TIMEOUT_S, auth_pat=user_pat,
        )
        if rc != 0:
            logger.error("fork-to-own: push to %s failed: %s", destination_repo, out)
            raise _http_error(
                502, "FORK_PUSH_FAILED",
                f"Could not push the template copy to '{destination_repo}': "
                f"{out.strip().splitlines()[-1][:200] if out.strip() else 'push failed'}. "
                f"The repository will be reused on retry.",
            )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    await _wait_for_branch_on_git_plane(destination_repo, template_default, user_pat)

    logger.info(
        "fork-to-own: copied %s@%s (%s) into %s (private=%s, reused_empty=%s)",
        template_repo, template_default, template_sha[:12], destination_repo,
        private, reused,
    )
    return ForkToOwnResult(destination_repo, template_default, reused)
