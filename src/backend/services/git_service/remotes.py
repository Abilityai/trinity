"""Remote-URL plumbing, PAT rotation on origin, and the ent#109 rebind (push by explicit URL, then repoint origin).

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
from services import git_credential_helper
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


from . import gitignore, token_scrub


REBIND_PUSH_TIMEOUT_S = 120

# ent#615: `_git_remote_url(pat, repo)` — which built
# `<scheme>://oauth2:<pat>@<host>/<repo>.git` — is GONE. It was the chokepoint
# every persisted remote came through, and a persisted remote is read by `ps`,
# by the orphan sweep's reaped-cmdline logging and by git's own stderr. The one
# remaining URL builder is `_credentialless_remote_url` below; the credential
# arrives per-operation through the `trinity` credential helper
# (`services/git_credential_helper.py`), which git speaks to over stdin.


def _remote_seturl_subcommand(url: str) -> str:
    """Idempotent `origin` set-url/add shell subcommand (#1264 review).

    Shared by ``initialize_github_sync`` and ``update_remote_pat`` so the
    templating logic lives in one place.

    Post-ent#615 the URL is credential-less, so the ``shlex.quote`` is no longer
    protecting a secret — keep it anyway, because it is protecting the SHELL,
    and note what it is NOT: docker-py ``split_command``s a string command
    (``docker/api/exec_api.py:47``), consuming one level of quoting, so this
    quote does not survive into the container as a second layer. It is the only
    layer, and it is load-bearing for a repo name, not for a token.
    """
    q = shlex.quote(url)
    return (
        f"git remote get-url origin >/dev/null 2>&1 && "
        f"git remote set-url origin {q} || git remote add origin {q}"
    )


async def update_remote_pat(agent_name: str, github_pat: str, github_repo: str) -> bool:
    """Make a running agent authenticate with ``github_pat``, no restart (#1264).

    **Semantics flipped by ent#615, signature unchanged.** This used to rewrite
    ``remote.origin.url`` to embed the token — the "now fix" a rotation needs,
    because the live git process authenticated from that URL and not from
    `.env`. It now does the opposite: it puts the credential where the
    credential helper reads it and makes the URL credential-LESS.

    Three steps, in this order, because **no path may strip a credential it has
    not already replaced**:

    1. write the token into the container's `.env` over ``docker exec`` — the
       helper's first rung, and the rung a rotation must reach (``Config.Env``
       is immutable without a recreate, so a baked-env-first ladder would keep
       authenticating with the revoked token);
    2. run the sweep, which installs the helper, PROVES it resolves, and only
       then strips legacy userinfo — including the frozen empty-password
       ``https://x-access-token:@github.com/...`` shape #1264 exists for;
    3. ensure ``origin`` exists and is credential-less (the sweep only rewrites
       remotes that are already there; #1264's case includes one that is not).

    Returns True on success. Best-effort: returns False (never raises) if the
    container isn't running, has no git dir, or a step fails.
    """
    if not github_pat or not github_repo:
        return False
    container_name = f"agent-{agent_name}"
    try:
        git_dir = await gitignore._detect_git_dir(container_name)

        env_written = await token_scrub.write_container_github_pat(agent_name, github_pat)

        # The seed is the belt to the `.env` write: if that write failed AND the
        # container has no other source, the sweep still has a credential to
        # place before it strips anything.
        report = await token_scrub.scrub_git_remote_tokens(
            agent_name, git_dir=git_dir, seed_pat=github_pat
        )
        if not report.get("helper_ok"):
            # Nothing resolves, so step 3 would install a credential-less origin
            # over a URL that may be the agent's only credential. Leave it.
            logger.warning(
                "ent#615/#1264: no credential resolves in %s after the .env "
                "write (written=%s) — origin left as it was",
                agent_name, env_written,
            )
            return False

        cmd = _remote_seturl_subcommand(_credentialless_remote_url(github_repo))
        result = await execute_command_in_container(
            container_name=container_name,
            command=f'bash -c "cd {git_dir} && {cmd}"',
            timeout=30,
        )
        ok = result.get("exit_code", 1) == 0
        if not ok:
            logger.warning(
                "update_remote_pat: set-url failed for %s: %s",
                agent_name, result.get("output", "")[:200],
            )
        return ok
    except Exception as e:  # noqa: BLE001 — best-effort, container may be down
        logger.warning("update_remote_pat: error for %s: %s", agent_name, e)
        return False


# ============================================================================
# Post-creation repo binding (trinity-enterprise#109)
# ============================================================================



def _credentialless_remote_url(github_repo: str) -> str:
    """A remote URL with no userinfo, honouring ``TRINITY_GIT_BASE_URL``.

    Since ent#615 this is the ONLY remote-URL builder — every persisted remote
    Trinity writes comes through here, and the credential arrives separately,
    per operation, through the ``trinity`` git credential helper. It was
    already the shape ``startup.sh``'s ``UPSTREAM_URL`` used
    (``${GIT_SCHEME}://${GIT_HOST_PATH}/${repo}.git``), which is why the two
    agree with no further work.

    It replaced ``_git_remote_url(pat, repo)``, which always embedded
    ``oauth2:<pat>@``. Note why that one could not simply be called with an
    empty PAT: it yielded ``oauth2:@host``, which is not credential-less and
    defeats anonymous fetch.
    """
    base = os.getenv("TRINITY_GIT_BASE_URL", "https://github.com").rstrip("/")
    return f"{base}/{github_repo}.git"


@dataclass
class RebindResult:
    """Outcome of the in-container half of a repo rebind (ent#109)."""

    success: bool
    stage: str  # detect | branch | push | rewire — where it stopped
    branch: Optional[str] = None
    error: Optional[str] = None


def _scrub_git_output(text: str, user_pat: str) -> str:
    """Redact BOTH the request's PAT and any URL userinfo from git output.

    Two passes, because they cover different secrets. ``scrub_secret`` removes
    the token the caller just handed us; ``redact_url_userinfo`` removes
    whatever userinfo is embedded in a remote URL git happens to echo — which
    on a rebind can be a *stale baked* token that is not ``user_pat`` at all
    (learnings 2026-07-14). Dropping either one leaks a real credential into
    an HTTP error body or the audit trail.
    """
    return scrub_secret_and_urls(text, user_pat)


async def rebind_origin_and_push(
    agent_name: str,
    destination_repo: str,
    user_pat: str,
    previous_repo: Optional[str],
    branch: str,
) -> RebindResult:
    """Push the agent's current history to ``destination_repo`` and repoint ``origin``.

    The in-container half of "bind this agent to a repo you own"
    (trinity-enterprise#109 §4.4 step 5). Unlike ent#93's create-time fork,
    the content source is the agent's **workspace volume** — the accumulated
    knowledge base — not a clone of the template, which is the whole reason
    the two paths cannot share a copy step.

``branch`` is the ref the caller already resolved via
    ``inspect_container_git`` during classification, and is the SAME value it
    wrote into the CAS's ``source_branch`` — re-reading it here would open a
    window where the row and the pushed ref disagree.

    Order is chosen so a failure is never half-applied:

    1. push to the destination via an **explicit URL**, not via ``origin`` —
       so a push failure leaves ``origin`` still pointing at the old repo and
       the agent exactly as it was;
    2. only then repoint ``origin``, clear the ent#123 push blackhole, and add
       the old repo as a credential-less ``upstream`` (skipped when
       ``previous_repo`` is None — a resumption, where the row already names
       the destination and writing it would erase the real upstream);
    3. read ``origin`` back and confirm it resolves to the destination. AC #5
       forbids a *silent* origin mismatch, and a set-url that exits 0 without
       taking effect is precisely the silent case.

    **Committed history only.** This deliberately does NOT ``git add``/commit
    the working tree. Nothing is lost — the files live on the workspace volume
    and the next Push (which now works) commits them — and staging blind would
    walk straight into the ``git add .`` credential hazard that has to be
    solved before `local:` agents are supported.

    Idempotent: re-running after a partial failure re-pushes the same refs and
    re-applies the same set-urls.

    Never raises; the caller maps ``stage`` to a structured 502.
    """
    container_name = f"agent-{agent_name}"
    # ent#615: credential-LESS. The push below carries the user's PAT
    # per-operation through the exec environment instead.
    dest_url = _credentialless_remote_url(destination_repo)

    try:
        git_dir = await gitignore._detect_git_dir(container_name)
    except Exception as e:  # noqa: BLE001 — container may be down mid-op
        return RebindResult(False, "detect", error=f"Could not reach the agent container: {e}")

    async def _run(
        cmd: str,
        timeout: int = 120,
        *,
        environment: Optional[Dict[str, str]] = None,
        user: str = "developer",
    ) -> tuple:
        result = await execute_command_in_container(
            container_name=container_name,
            command=f"bash -c {shlex.quote(f'cd {shlex.quote(git_dir)} && {cmd}')}",
            timeout=timeout,
            environment=environment,
            user=user,
        )
        return (
            result.get("exit_code", 1),
            _scrub_git_output(result.get("output", ""), user_pat),
        )

    # 1. Push committed history to the destination by explicit — and now
    #    credential-less — URL.
    #
    #    ent#615/CRIT-7: the credential travels in the exec ENVIRONMENT
    #    (`http.extraHeader` via GIT_CONFIG_*), so it is in neither the argv nor
    #    `.git/config`. But env is readable through `/proc/<pid>/environ` by a
    #    SAME-UID process, and this is the USER's PAT — a credential the
    #    container is not supposed to hold at all — exposed for the whole
    #    `REBIND_PUSH_TIMEOUT_S` window to an agent that runs as `developer`
    #    and has `Bash`. So the push runs as ROOT, whose `environ` `developer`
    #    cannot read.
    #
    #    It cannot move to the backend host (the recommended shape, and the one
    #    fork_to_own uses): the history being pushed lives ONLY on the
    #    container's workspace volume, and the backend has no copy of it.
    #
    #    `safe.directory`: git refuses to operate as root on a repo owned by
    #    another user. Non-secret, and `-c` is protected configuration, so this
    #    is the one thing that legitimately belongs on the argv here. `push` is
    #    local-read-only — it writes no objects and no refs into the repo — so
    #    running it as root leaves nothing root-owned behind.
    rc, out = await _run(
        f"git -c {shlex.quote(f'safe.directory={git_dir}')} "
        f"push {shlex.quote(dest_url)} "
        f"{shlex.quote(f'refs/heads/{branch}:refs/heads/{branch}')}",
        timeout=REBIND_PUSH_TIMEOUT_S,
        environment=git_credential_helper.git_auth_env(user_pat),
        user="root",
    )
    if rc != 0:
        last = out.strip().splitlines()[-1][:300] if out.strip() else "push failed"
        return RebindResult(
            False, "push", branch=branch,
            error=(
                f"Could not push the agent's history to '{destination_repo}': "
                f"{last}"
            ),
        )

    # 2. Repoint origin, clear the ent#123 blackhole, record the old repo as
    #    upstream. Each is idempotent; `--unset-all` exits 5 when the key is
    #    absent (the normal case for an agent that always had a token), so it
    #    is explicitly tolerated rather than treated as a failure.
    #
    #    `previous_repo=None` means the caller is RESUMING a partially-applied
    #    bind, where the row already names the destination — writing `upstream`
    #    from it would point upstream at the destination itself and erase the
    #    record of the real upstream. Skip it: a first attempt that reached this
    #    point already set it, and `.git/config` is on the workspace volume every
    #    recreate reuses (#1664), so it survives.
    rewire = (
        f"{_remote_seturl_subcommand(dest_url)} && "
        f"(git config --unset-all remote.origin.pushurl 2>/dev/null || true)"
    )
    if previous_repo:
        upstream_url = _credentialless_remote_url(previous_repo)
        rewire += (
            f" && (git remote set-url upstream {shlex.quote(upstream_url)} 2>/dev/null || "
            f"git remote add upstream {shlex.quote(upstream_url)} 2>/dev/null || true)"
        )
    rc, out = await _run(rewire, timeout=60)
    if rc != 0:
        last = out.strip().splitlines()[-1][:300] if out.strip() else "rewire failed"
        return RebindResult(
            False, "rewire", branch=branch,
            error=(
                f"The history reached '{destination_repo}', but repointing the "
                f"agent's origin failed: {last}"
            ),
        )

    # 3. Read back — a set-url that "succeeded" without taking effect is the
    #    silent origin mismatch AC #5 exists to prevent.
    observed = (await inspect_container_git(agent_name)).origin_repo
    if not observed or observed.lower() != destination_repo.lower():
        return RebindResult(
            False, "rewire", branch=branch,
            error=(
                f"The history reached '{destination_repo}', but the agent's "
                f"origin reads as '{observed or 'unset'}' afterwards, not the "
                f"destination. Nothing further was changed."
            ),
        )

    logger.info(
        "repo-bind: %s pushed branch %s to %s and repointed origin (upstream=%s)",
        agent_name, branch, destination_repo, previous_repo or "unchanged",
    )
    return RebindResult(True, "done", branch=branch)


def _parse_repo_from_remote_url(raw: str) -> Optional[str]:
    """``owner/repo`` from a git remote URL, with any userinfo discarded.

    The userinfo strip is not cosmetic: a bound agent's origin is
    ``https://oauth2:<pat>@github.com/owner/repo.git``, and this value reaches
    error messages, the audit row, and the API response.
    """
    without_scheme = raw.split("://", 1)[-1]
    without_userinfo = without_scheme.split("@", 1)[-1]
    path = without_userinfo.partition("/")[2]
    if path.endswith(".git"):
        path = path[: -len(".git")]
    parts = [seg for seg in path.strip("/").split("/") if seg]
    if len(parts) < 2:
        return None
    return "/".join(parts[-2:])


@dataclass
class ContainerGitState:
    """What the LIVE container's git actually says (ent#109 classification).

    Both fields are ``None`` when unreadable — container down, no git dir, no
    ``origin``, detached HEAD. The caller must treat ``None`` as *unknown* and
    refuse, never as agreement: guessing here is how an agent silently ends up
    bound to a repo it is not actually pointing at (AC #5).
    """

    origin_repo: Optional[str] = None
    branch: Optional[str] = None


async def inspect_container_git(agent_name: str) -> ContainerGitState:
    """Read the live container's ``origin`` repo and current branch.

    Used by the rebind pre-flight (ent#109 FR-1) to refuse
    ``BIND_STATE_UNCLASSIFIED`` when the container disagrees with the DB row,
    and to learn the branch the push and the CAS's ``source_branch`` must both
    use — they have to be the same value, so it is read once here rather than
    twice with a window in between.

    Never raises; an unreachable container yields an empty state.
    """
    container_name = f"agent-{agent_name}"
    try:
        git_dir = await gitignore._detect_git_dir(container_name)
    except Exception as e:  # noqa: BLE001 — unknown, never "matches"
        logger.warning("inspect_container_git: git dir probe failed for %s: %s", agent_name, e)
        return ContainerGitState()

    async def _read(cmd: str) -> Optional[str]:
        try:
            result = await execute_command_in_container(
                container_name=container_name,
                command=f"bash -c {shlex.quote(f'cd {shlex.quote(git_dir)} && {cmd}')}",
                timeout=30,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("inspect_container_git: %r failed for %s: %s", cmd, agent_name, e)
            return None
        if result.get("exit_code", 1) != 0:
            return None
        lines = [ln.strip() for ln in (result.get("output") or "").splitlines() if ln.strip()]
        return lines[-1] if lines else None

    origin_raw = await _read("git remote get-url origin")
    # Empty on a detached HEAD — deliberately left as None so the caller
    # refuses rather than inventing a ref to push.
    branch = await _read("git branch --show-current")

    return ContainerGitState(
        origin_repo=_parse_repo_from_remote_url(origin_raw) if origin_raw else None,
        branch=branch or None,
    )


