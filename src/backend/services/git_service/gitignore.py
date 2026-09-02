"""The fleet gitignore machinery — pattern list, merge/append/rm-cached builders, the #2069 creation-time merge, and git-dir detection.

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



logger = logging.getLogger(__name__)

_TRINITY_AUTHORED_PATHS: Tuple[str, ...] = (
    ".trinity/pre-check",       # SCHED-COND-001 conditional-schedule hook (#454)
    # Template-authored output-contract validator. No platform executor runs it
    # today (compat check I-005 was retired in #2137 as gating on a fiction), but
    # the path stays: #2070 derives the `!` re-includes from this tuple, and 14
    # bundled templates already ship `!.trinity/post-check`, so removing it would
    # untrack an authored hook on the next push — the exact #2070 regression.
    ".trinity/post-check",
    ".trinity/pre-snapshot",    # data-snapshot quiesce hook (#1169)
    ".trinity/setup.sh",        # startup setup convention (trinity-enterprise#76)
    ".trinity/persistent-processes.allow",  # orphan-sweep allowlist patterns (#1501)
    ".trinity/brain-orb/",      # brain-orb convention hooks (#58/#60)
    ".trinity/pipelines/",      # agent-defined pipeline DEFINITIONS (#919);
                                # instance STATE lives in pipeline-state/
    # #1704: declared Claude Code plugin selection (marketplaces + installed).
    # COMMITTED — unlike persistent-state.yaml / data-paths.yaml (volume-local,
    # re-materialized at creation), this must survive a git-based reconstitution
    # onto a fresh volume or a new host, the gap #1704 closes. This entry alone
    # yields both the `!` re-include and the `git rm --cached` exemption, so the
    # manifest is committable while `.claude.json` and `.claude/plugins/` (#1705)
    # stay gitignored.
    ".trinity/plugins.yaml",
)

_GITIGNORE_PATTERNS: Tuple[str, ...] = (
    # Shell init / history (instance-specific)
    ".bash_logout",
    ".bashrc",
    ".profile",
    ".bash_history",
    ".sudo_as_admin_successful",
    # Credentials — NEVER COMMIT
    ".env",
    ".env.*",
    ".mcp.json",
    "credentials.json",
    "*.pem",
    "*.key",
    # Instance-specific directories
    ".cache/",
    ".local/",
    ".npm/",
    ".ssh/",
    # #2070: contents-only, so the authored paths below can be re-included.
    ".trinity/*",
    *(f"!{path}" for path in _TRINITY_AUTHORED_PATHS),
    ".tmp/",  # #1098 disk-backed scratch (TMPDIR); #1187 relocated CODEX_HOME
    ".trinity-clone-tmp/",  # #1439 transient full-history clone staging dir (removed post-merge; ignored so a crash-orphaned copy — incl. its PAT-bearing .git/config — is never committed)
    # Large generated content
    "content/",
    # #1596: bulk data / dependency / cache / index dirs that churn on every
    # run and bloat `.git` unboundedly under auto-sync. Git sync is for code +
    # state, not datasets/indexes/deps — those belong in `data_paths` (#1169) or
    # stay local. Merged into existing agents on sync, which also untracks any
    # already-committed matches (stops future churn; doesn't shrink history).
    # An agent that genuinely needs one committed can negate it in its own
    # `.gitignore` (e.g. `!keep.db`).
    "node_modules/",
    ".venv/",
    "venv/",
    "__pycache__/",
    "*.pyc",
    "*.pyo",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".ipynb_checkpoints/",
    "*.sqlite",
    "*.sqlite3",
    "*.db",
    # Claude Code runtime — commit commands/skills/agents, exclude runtime data
    ".claude.json",
    ".claude.json.backup",
    ".claude/projects/",
    ".claude/statsig/",
    ".claude/todos/",
    ".claude/debug/",
    ".claude/sessions/",
    ".claude/shell-snapshots/",
    # Marketplace plugin caches (#1702): Claude Code copies each installed
    # plugin (skills/agents/hooks) into ~/.claude/plugins/cache/<plugin>@<ver>/.
    # Since HOME == the agent's repo root, that lands in the working tree and
    # the 15-min sync loop commits it (and every plugin update commits another
    # copy) — repo bloat, same class as #1596. Re-installable, so never in git.
    ".claude/plugins/",
    # #2036: container-only Claude Code config. The base image bakes
    # `~/.claude/settings.json` (docker/base-image/hooks/claude-settings.json)
    # registering the platform guardrail hooks by ABSOLUTE container path
    # (`/opt/trinity/hooks/*.py`). HOME == the repo root, so `git add -A` swept
    # it into the agent's GitHub repo — and any clone made outside the container
    # is then hard-bricked: a PreToolUse hook whose script is missing exits 2,
    # which is precisely Claude Code's "block this tool call" signal, so every
    # Bash/Edit/Write fails on a machine that has no `/opt/trinity`. Worse blast
    # radius than #462/#1596/#1702 — the leak breaks foreign clones rather than
    # merely bloating them. The rest are runtime state observed leaking in the
    # same commit (`backups/` alone was ~3,000 lines).
    #
    # ent#345 UPDATE: the platform no longer bakes this file — the guardrail
    # registration moved to root-owned `/etc/claude-code/managed-settings.json`,
    # out of the agent's write reach and out of the synced tree. The rule STAYS
    # load-bearing, for the two copies that can still exist: a legacy one on a
    # volume that predates ent#345 (removed by `startup.sh` only on an exact
    # content match, so an agent that never restarts still has it) and an
    # agent-authored one. Either still registers absolute `/opt/trinity` paths, so
    # committing either still bricks a foreign clone — the damage above, unchanged.
    #
    # Trade-off, stated: `.claude/settings.json` doubles as Claude Code's
    # PROJECT-level settings file, so a template can no longer commit one. The
    # original justification ("the baked file always exists and would collide") no
    # longer holds — nothing bakes it — but the rule survives on the leak argument
    # alone, and an agent that genuinely needs it keeps the #1596 escape hatch:
    # negate in its own `.gitignore` (`!.claude/settings.json`).
    # `settings.local.json` is already covered by the `*.local.json` rule below.
    ".claude/settings.json",
    ".claude/remote-settings.json",
    ".claude/policy-limits.json",
    ".claude/backups/",
    ".claude/.last-cleanup",
    # Temporary files
    "*.log",
    "*.tmp",
    ".DS_Store",
    # Local overrides
    "*.local.md",
    "*.local.json",
)

_GITIGNORE_SUPERSEDED_LINES: Tuple[str, ...] = (
    ".trinity/",
    ".trinity",
)

AGENT_HOME_DIR = "/home/developer"

LEGACY_WORKSPACE_DIR = "/home/developer/workspace"

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

def _build_gitignore_append_command(git_dir: str, patterns) -> str:
    """Build a bash command that appends any missing ``patterns`` to
    ``{git_dir}/.gitignore`` without clobbering user-supplied rules.
    Idempotent — each pattern is gated by an exact-line ``grep -qxF`` check,
    so a second run is a no-op. Generic over the pattern list so both the
    fleet-wide ignore merge and the per-agent data_paths append (#1169) share
    one implementation.
    """
    parts = [f"cd {shlex.quote(git_dir)}", "touch .gitignore"]
    for p in patterns:
        q = shlex.quote(p)
        parts.append(f"(grep -qxF -- {q} .gitignore || echo {q} >> .gitignore)")
    script = " && ".join(parts)
    return f"bash -c {shlex.quote(script)}"


def _build_gitignore_merge_command(git_dir: str) -> str:
    """Build a bash command that reconciles ``{git_dir}/.gitignore`` with
    ``_GITIGNORE_PATTERNS``: drop superseded exact lines, then append whatever
    is missing — without clobbering user-supplied rules. Idempotent: the
    removal is a no-op once the line is gone, and each append is gated by an
    exact-line ``grep -qxF`` check.
    """
    parts = [f"cd {shlex.quote(git_dir)}", "touch .gitignore"]
    for stale in _GITIGNORE_SUPERSEDED_LINES:
        # `grep -vxF` into a temp file, not `sed -i`: `-x -F` is a whole-line
        # literal match, so no pattern metacharacter in a user's rule can be
        # caught by accident. Guarded on the line existing, so the common path
        # leaves the file (and its mtime) untouched.
        q = shlex.quote(stale)
        # `|| true` on the filter: `grep -v` exits 1 when it selects NO lines,
        # which is the legitimate case of a `.gitignore` that contained only
        # the superseded line. Without it the whole `&&` chain aborts and the
        # merge never runs — and the guard above has already proved the file is
        # readable, so the only status being swallowed is "empty result".
        parts.append(
            f"(! grep -qxF -- {q} .gitignore || "
            f"{{ grep -vxF -- {q} .gitignore > .gitignore.tmp || true; "
            f"mv .gitignore.tmp .gitignore; }})"
        )
    for pattern in _GITIGNORE_PATTERNS:
        q = shlex.quote(pattern)
        parts.append(f"(grep -qxF -- {q} .gitignore || echo {q} >> .gitignore)")
    script = " && ".join(parts)
    return f"bash -c {shlex.quote(script)}"


def _build_rm_cached_ignored_command(git_dir: str) -> str:
    """Build a bash command that ``git rm --cached``s any tracked files that
    NOW match an ignore rule. Idempotent — `git ls-files -ci` returns the
    empty set after the first successful run.

    Two-pass: a non-NUL `git ls-files` to check emptiness via shell variable
    (bash can't hold NUL bytes), then a NUL-delimited pipe to xargs so paths
    with spaces or unicode survive the round-trip. Working-tree files are
    left alone; only the index is touched.

    Authored ``.trinity/`` content is exempt, and the exemption list is DERIVED
    from ``_TRINITY_AUTHORED_PATHS`` rather than written out here (#2070). It
    used to be two hardcoded strings, and each of the three incidents in this
    area was one more forgotten string: brain-orb hooks
    (trinity-enterprise#76), ``setup.sh`` (swept live before the second
    exemption was added), ``pre-check`` (#2070 — the SCHED-COND-001 hook the
    platform's own docs tell template authors to commit). Deriving it makes
    adding an authored path one edit instead of two, and the two cannot drift.

    Belt-and-braces: since #2070 the ignore rules themselves no longer match
    these paths, so ``git ls-files -ci`` should not list them at all. The
    pathspec stays for the agent whose ``.gitignore`` still carries the
    superseded wholesale ``.trinity/`` line — otherwise its hooks would be
    swept by the very push that repairs the file.
    """
    exempt = " ".join(
        shlex.quote(f":!{path.rstrip('/')}") for path in _TRINITY_AUTHORED_PATHS
    )
    script = (
        f"cd {shlex.quote(git_dir)} && "
        f"ignored=$(git ls-files -ci --exclude-standard -- . {exempt}) && "
        'if [ -n "$ignored" ]; then '
        f"git ls-files -ci -z --exclude-standard -- . {exempt} | "
        "xargs -0 git rm --cached --quiet -r --; "
        "fi"
    )
    return f"bash -c {shlex.quote(script)}"


async def _git_toplevel(container_name: str) -> Optional[str]:
    """Ask git where this agent's repository is rooted (#2075).

    The probe starts at ``workspace/`` when that directory exists and at the
    home directory otherwise. ``rev-parse --show-toplevel`` walks **up**, so
    the nearest enclosing repository wins: a genuinely workspace-rooted legacy
    repo still answers ``/home/developer/workspace``, while a standard agent
    that merely keeps a populated non-git ``workspace/`` data directory answers
    ``/home/developer`` — the case the old content heuristic got wrong.

    ``safe.directory`` is relaxed for this read-only query only: the exec runs
    as ``developer``, but a volume restored with foreign ownership would
    otherwise make git refuse to answer and silently drop the caller onto the
    fallback heuristic that this function exists to replace.

    ``GIT_DISCOVERY_ACROSS_FILESYSTEM=1`` because the walk up has a second way to
    stop that has nothing to do with repositories (#2245): git halts discovery at
    a filesystem boundary by default, so on an agent whose ``workspace/`` is its
    own mount — a bind mount, a distinct volume, an overlay — a probe started
    inside it never reaches a repository rooted at the home directory. Git says so
    in as many words: *"Stopping at filesystem boundary
    (GIT_DISCOVERY_ACROSS_FILESYSTEM not set)"*, exit 128. This function would then
    return None and the caller would fall through to the content heuristic, which
    answers ``/home/developer/workspace`` for exactly that topology — the
    misclassification #2075 exists to eliminate, reintroduced by a mount.

    Crossing the boundary is safe here specifically because the containment check
    below is unchanged: discovery may walk out of the mount, but an answer outside
    the agent home is still refused before it is trusted. (Carried over from #2076,
    a competing #2075 fix closed as superseded — this flag was the one thing it had
    that #2077 did not.)

    Returns None when there is no repository at or above the probe point, or
    when git answers with a path outside the agent home (never trusted).
    """
    script = (
        f"start={shlex.quote(LEGACY_WORKSPACE_DIR)}; "
        f'[ -d "$start" ] || start={shlex.quote(AGENT_HOME_DIR)}; '
        "GIT_DISCOVERY_ACROSS_FILESYSTEM=1 "
        "git -c safe.directory='*' -C \"$start\" rev-parse --show-toplevel "
        "2>/dev/null"
    )
    result = await execute_command_in_container(
        container_name=container_name,
        command=f"bash -c {shlex.quote(script)}",
        timeout=5,
    )
    if result.get("exit_code") != 0:
        return None
    top = (result.get("output") or "").strip().splitlines()
    top = top[-1].strip() if top else ""
    if top == AGENT_HOME_DIR or top.startswith(f"{AGENT_HOME_DIR}/"):
        return top
    return None


async def _detect_git_dir_fallback(container_name: str) -> str:
    """Where to *create* a repo when the container has none yet.

    Verbatim the pre-#2075 content heuristic: any non-empty ``workspace/``
    means the repo goes there. ``initialize_git_in_container`` uses this to
    place a brand-new repo, so fresh-agent placement stays byte-compatible.
    """
    check_workspace = await execute_command_in_container(
        container_name=container_name,
        command=(
            'bash -c "[ -d /home/developer/workspace ] && '
            'find /home/developer/workspace -mindepth 1 -maxdepth 1 | '
            'head -1 | wc -l"'
        ),
        timeout=5,
    )
    workspace_has_content = (
        check_workspace.get("exit_code") == 0
        and "1" in check_workspace.get("output", "")
    )
    return "/home/developer/workspace" if workspace_has_content else "/home/developer"


async def _detect_git_dir(container_name: str) -> str:
    """Pick the directory git operations should run in for an agent container.

    Git's own answer wins (``_git_toplevel``). Only when the container has no
    repository at all does the legacy content heuristic decide — that path is
    reached by ``initialize_git_in_container``, which needs a placement for a
    repo that does not exist yet.
    """
    top = await _git_toplevel(container_name)
    if top:
        return top
    return await _detect_git_dir_fallback(container_name)


async def _migrate_workspace_gitignore(agent_name: str) -> None:
    """Idempotently bring an existing agent's `.gitignore` up to the current
    `_GITIGNORE_PATTERNS` and untrack any files that NOW match a rule.

    Runs on every Push (#462) so existing agents adopt new patterns without
    requiring a re-init or container rebuild. Errors are logged and swallowed
    — a transient migration failure must not break an operator's Push.

    No-op if the container has no `.git` directory (agent not initialized for
    git sync).
    """
    container_name = f"agent-{agent_name}"
    try:
        git_dir = await _detect_git_dir(container_name)
        # Bail if not git-initialized — the agent's /api/git/sync will
        # return its own 400 in that case.
        check_git = await execute_command_in_container(
            container_name=container_name,
            command=f'bash -c "[ -d {shlex.quote(git_dir)}/.git ]"',
            timeout=5,
        )
        if check_git.get("exit_code") != 0:
            return
        # 1. Append missing patterns (idempotent).
        await execute_command_in_container(
            container_name=container_name,
            command=_build_gitignore_merge_command(git_dir),
            timeout=10,
        )
        # 2. Untrack any indexed files that now match an ignore rule.
        await execute_command_in_container(
            container_name=container_name,
            command=_build_rm_cached_ignored_command(git_dir),
            timeout=30,
        )
    except Exception as exc:
        logger.warning(
            f"_migrate_workspace_gitignore failed for {agent_name}: {exc}. "
            "Push will proceed against the existing .gitignore."
        )


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
    """Readiness-gated fire-and-forget merge of `_GITIGNORE_PATTERNS` into a
    fresh `github:` agent's `.gitignore`, so the first in-container auto-sync
    cycle stages none of the ignored runtime/credential paths (#2069).

    Two-tier safety property:
      * **Creation-time = PREVENT** — merge-only (NO `_build_rm_cached_ignored_
        command`). The generated `.env`/`.mcp.json` are written post-clone as
        UNTRACKED files, so a merge-installed `.gitignore` stops `git add -A`
        from ever staging them (the common case). Untracking the template's own
        committed content just because it matches a broad pattern would be
        surprising; a template that COMMITTED a credential file (unusual
        subclass) is remediated on the first Push (`_migrate_workspace_gitignore`)
        + retired by #1703.
      * **Push = REMEDIATE** — `_migrate_workspace_gitignore` still does
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

        # Docker-exec section (`.git` check / `_git_toplevel` / merge) — bound the
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
            # `_git_toplevel` (None → skip) rather than `_detect_git_dir`'s
            # heuristic fallback — safer to skip on unresolved than merge against
            # a guessed path.
            try:
                git_dir = await asyncio.wait_for(
                    _git_toplevel(container_name),
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
                    command=_build_gitignore_merge_command(git_dir),
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


