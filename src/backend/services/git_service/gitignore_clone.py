"""The creation-time gitignore merge (#2069) and the readiness it waits on.

Split out of `gitignore.py` (#1028), for the same reason as
`gitignore_sweep.py`: #2529 grew that module past the 800-line threshold. This
half is the one that runs ONCE, just after an agent is cloned — fire-and-forget,
gated on the agent server answering `/health`, merging the fleet-wide list into
a workspace nobody has pushed from yet — as against the sweep that runs on every
Push.

Sibling calls go through the module object (`gitignore.<name>`), never a
from-import: a from-import freezes the binding, and test_2069's readiness probes
went dark exactly that way mid-split.
"""
import asyncio
import os
import logging
from typing import Optional, Dict, Any

from database import db
from services.agent_auth import agent_httpx_client
from services.docker_service import get_agent_container, execute_command_in_container

from . import gitignore

logger = logging.getLogger(__name__)

_MERGE_READY_TIMEOUT_SECONDS = int(
    os.getenv("TRINITY_GITIGNORE_MERGE_TIMEOUT_SECONDS", "1800")
)

_MERGE_READY_INTERVAL_SECONDS = int(
    os.getenv("TRINITY_GITIGNORE_MERGE_INTERVAL_SECONDS", "5")
)

_MERGE_EXEC_TIMEOUT_SECONDS = 30

_MERGE_POLLER_CONCURRENCY = int(os.getenv("TRINITY_GITIGNORE_MERGE_CONCURRENCY", "6"))

_gitignore_merge_semaphore = asyncio.Semaphore(_MERGE_POLLER_CONCURRENCY)

_inflight_gitignore_merge_tasks: "set[asyncio.Task]" = set()


def _git_auto_sync_baked(
    config,
    github_repo: Optional[str],
    github_pat: Optional[str],
    fork_upstream: Optional[str],
) -> bool:
    """Does this agent bake ``GIT_SYNC_AUTO='true'`` at creation? — the single
    owner of that predicate (the ent#109 `_apply_git_env_from_db` "single owner
    of the env gate" discipline; used at both `crud.py::_apply_github_env` and
    the #2069 merge spawn).

    Mirrors `_apply_github_env` verbatim: the flag is set inside `if
    github_repo:` when `(not source_mode or fork_upstream) and github_pat`. The
    in-container auto-sync loop gates purely on this env var, so this predicate —
    NOT the `_materialize_agent_files` DB-flag block, which additionally excludes
    ephemeral ghosts (`and not config.ephemeral`) — is exactly the population
    whose loop auto-commits, and therefore exactly what the #2069 merge must
    cover: an ephemeral non-source `github:`+PAT ghost bakes the env, auto-commits
    from birth, and is never operator-Pushed, so the DB-flag-gated path would
    leave it leaking unremediated.
    """
    return (
        bool(github_repo)
        and bool(github_pat)
        and (not config.source_mode or bool(fork_upstream))
    )


async def _probe_agent_server_ready(agent_name: str) -> bool:
    """One DIRECT agent-server `/health` probe (#2069 / #1159).

    Direct (`agent_httpx_client` → `http://agent-{name}:8000/health`), NEVER the
    backend proxy route, which masks a mid-startup `httpx.ConnectError` as an
    HTTP 200 fallback body carrying a `message` key (ent#15 / learnings
    2026-08-04). Until the server is up the connect raises and we return False;
    a real 200 returns True. `/health` is the ONE path the agent-server auth
    middleware exempts, so the probe needs nothing beyond what the client stamps.
    """
    try:
        async with agent_httpx_client(
            agent_name, timeout=_MERGE_READY_INTERVAL_SECONDS
        ) as client:
            resp = await client.get(f"http://agent-{agent_name}:8000/health")
            return resp.status_code == 200
    except Exception:
        return False


async def _container_has_git_dir(container_name: str) -> bool:
    """True iff `/home/developer/.git` exists (one exec)."""
    result = await execute_command_in_container(
        container_name=container_name,
        command='bash -c "[ -d /home/developer/.git ]"',
        timeout=5,
    )
    return result.get("exit_code") == 0


async def merge_gitignore_after_clone(agent_name: str) -> None:
    """Readiness-gated fire-and-forget merge of `gitignore._GITIGNORE_PATTERNS` into a
    fresh `github:` agent's `.gitignore`, so the first in-container auto-sync
    cycle stages none of the ignored runtime/credential paths (#2069).

    Two-tier safety property:
      * **Creation-time = PREVENT** — merge-only (NO `_build_rm_cached_ignored_
        command`). The generated `.env`/`.mcp.json` are written post-clone as
        UNTRACKED files, so a merge-installed `.gitignore` stops `git add -A`
        from ever staging them (the common case). Untracking the template's own
        committed content just because it matches a broad pattern would be
        surprising; a template that COMMITTED a credential file (unusual
        subclass) is remediated on the first Push (`gitignore._migrate_workspace_gitignore`)
        + retired by #1703.
      * **Push = REMEDIATE** — `gitignore._migrate_workspace_gitignore` still does
        merge + untrack, unchanged (AC#5: no behaviour change on `sync_to_github`).

    Merge point — the central correctness question. The merge must run AFTER
    startup.sh finishes ALL of its git setup and BEFORE the first auto-sync
    cycle, WITHOUT relying on the 900s pre-first-cycle sleep for correctness. The
    gate is **agent-server /health readiness ∧ /home/developer/.git present**:
      * The agent server is launched ONCE, at startup.sh:517 — strictly after the
        entire git block (clone → tar-merge → `git checkout` of the source/working
        branch → remote-config). A filesystem gate like `.git ∧ ¬.trinity-clone-
        tmp` fires mid-git-setup, where a later `git checkout` can REVERT the
        merged `.gitignore` (target branch ships a different one) or FAIL on the
        uncommitted change — and because the poll fires the merge once and exits,
        a reverted merge is not retried (Codex #1). `/health` responding proves
        startup.sh is past ALL working-tree mutation, and is still ~900s before
        the auto-sync loop (which lives inside that same server) runs its first
        cycle. The readiness gate is therefore STRONGER than the filesystem check.
      * The probe is DIRECT — the backend proxy masks a mid-startup ConnectError
        as a 200 fallback body (ent#15).
      * `.git` present handles the failed-clone case: the server still launches
        (startup.sh has no `set -e`), so `/health` comes up, but `.git` is absent
        → skip (nothing to pollute; the Push migration is the backstop).

    Bounded & non-fatal: a monotonic deadline (`_MERGE_READY_TIMEOUT_SECONDS`,
    sized to clone + startup, NOT the 900s cycle), a module-level Semaphore cap on
    the DOCKER-EXEC section only (batch creation must not starve the shared 4-thread
    Docker pool), and `asyncio.wait_for` around every exec/HTTP. wait_for frees the
    TASK, not the pinned pool thread. The readiness poll runs OUTSIDE the Semaphore
    — it is pure agent-`/health` HTTP and touches no pool thread, so capping it
    would let slow-booting agents head-of-line-block a healthy agent's merge past
    its own first cycle. Any failure logs and returns; on deadline the Push
    migration remains the backstop.

    Known limitation: a backend restart within the readiness-wait window loses
    this in-memory task. Acceptable for a P2 — the Push migration remediates and
    #1703 is the structural fix.
    """
    container_name = f"agent-{agent_name}"
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _MERGE_READY_TIMEOUT_SECONDS
    ready = False
    try:
        # Readiness poll — pure agent-`/health` HTTP, NOT a Docker exec, so it runs
        # OUTSIDE `_gitignore_merge_semaphore` (which bounds only the Docker-pool
        # exec section below). Holding the pool cap across a <=1800s readiness wait
        # would let slow-booting agents head-of-line-block a healthy agent's merge
        # past its own first auto-sync cycle — re-opening the leak this fix closes.
        while loop.time() < deadline:
            try:
                ready = await asyncio.wait_for(
                    _probe_agent_server_ready(agent_name),
                    timeout=_MERGE_EXEC_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                ready = False
            if ready:
                break
            await asyncio.sleep(_MERGE_READY_INTERVAL_SECONDS)

        if not ready:
            logger.warning(
                "[#2069] agent-server for %s never became ready within %ss; "
                "skipping the creation .gitignore merge — the Push migration "
                "remains the backstop.",
                agent_name,
                _MERGE_READY_TIMEOUT_SECONDS,
            )
            return

        # Docker-exec section (`.git` check / `gitignore._git_toplevel` / merge) — bound the
        # shared 4-thread pool HERE. Each exec is short, so the cap drains fast even
        # under batch creation; a queued agent's exec still lands well inside its
        # ~900s pre-first-cycle window because it is no longer stuck behind other
        # agents' readiness waits.
        async with _gitignore_merge_semaphore:
            # Server is up ⟹ startup.sh is past ALL git mutation (single launch
            # point, sequential) — no more `git checkout` can revert the merge.
            try:
                has_git = await asyncio.wait_for(
                    _container_has_git_dir(container_name),
                    timeout=_MERGE_EXEC_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                has_git = False
            if not has_git:
                logger.info(
                    "[#2069] %s is ready but has no .git (failed clone / not "
                    "git-bound); skipping the creation .gitignore merge.",
                    agent_name,
                )
                return

            # The gate already proved a repo exists, so resolve the toplevel with
            # `gitignore._git_toplevel` (None → skip) rather than `gitignore._detect_git_dir`'s
            # heuristic fallback — safer to skip on unresolved than merge against
            # a guessed path.
            try:
                git_dir = await asyncio.wait_for(
                    gitignore._git_toplevel(container_name),
                    timeout=_MERGE_EXEC_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                git_dir = None
            if git_dir is None:
                logger.info(
                    "[#2069] could not resolve the git toplevel for %s; "
                    "skipping the creation .gitignore merge.",
                    agent_name,
                )
                return

            await asyncio.wait_for(
                execute_command_in_container(
                    container_name=container_name,
                    command=gitignore._build_gitignore_merge_command(git_dir),
                    timeout=_MERGE_EXEC_TIMEOUT_SECONDS,
                ),
                timeout=_MERGE_EXEC_TIMEOUT_SECONDS,
            )
            logger.info(
                "[#2069] seeded the canonical .gitignore for %s at %s before "
                "its first auto-sync cycle.",
                agent_name,
                git_dir,
            )
    except Exception as exc:
        logger.warning(
            "[#2069] creation .gitignore merge failed for %s: %s. "
            "The Push migration remains the backstop.",
            agent_name,
            exc,
        )


def spawn_gitignore_merge_after_clone(agent_name: str) -> None:
    """Fire ``merge_gitignore_after_clone`` fire-and-forget (mirrors
    `activity_service.spawn_close_execution_activity`): zero creation latency;
    the merge lands within one poll interval of agent-server readiness.

    The Docker-exec section is bounded INSIDE the coro by
    `_gitignore_merge_semaphore`, so an excess spawn's merge exec queues rather
    than piling another concurrent exec onto the shared Docker pool; the readiness
    poll runs OUTSIDE the cap (pure agent-`/health` HTTP, no pool thread). A strong
    ref in `_inflight_gitignore_merge_tasks` defeats the
    asyncio `create_task` GC footgun. With no running loop the coro is closed and
    the spawn is skipped (logged), never raised — the Push migration is the
    backstop.
    """
    coro = merge_gitignore_after_clone(agent_name)
    try:
        task = asyncio.create_task(coro)
        _inflight_gitignore_merge_tasks.add(task)
        task.add_done_callback(_inflight_gitignore_merge_tasks.discard)
    except RuntimeError as e:
        coro.close()
        logger.debug(
            "[#2069] spawn_gitignore_merge_after_clone skipped (no loop): %s", e
        )
