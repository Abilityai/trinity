"""
Git synchronization service for GitHub-native agents (Phase 7).

Handles:
- Creating working branches for new agents
- Syncing agent changes to GitHub
- Managing git configuration in the database
- Initializing git in agent containers
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

class ConflictClass(str, Enum):
    """Symbolic class of a git sync/push/pull failure, used by the UI to pick
    operator-readable copy.

    Members map one-to-one to the decision tree defined in the git-improvements
    proposal (§P4/§S5). The string value equals the member name so JSON
    serialization in ``conflict_class`` fields stays stable.
    """

    AHEAD_ONLY = "AHEAD_ONLY"
    BEHIND_ONLY = "BEHIND_ONLY"
    PARALLEL_HISTORY = "PARALLEL_HISTORY"
    UNCOMMITTED_LOCAL = "UNCOMMITTED_LOCAL"
    AUTH_FAILURE = "AUTH_FAILURE"
    WORKING_BRANCH_EXTERNAL_WRITE = "WORKING_BRANCH_EXTERNAL_WRITE"
    UNKNOWN = "UNKNOWN"


# Regexes matched against the stderr. Patterns are drawn from real stderr
# samples captured in /tmp/trinity-repro/ (see tests/git_sync/fixtures/).
_AUTH_PATTERNS = (
    re.compile(r"authentication failed", re.IGNORECASE),
    re.compile(r"could not read username", re.IGNORECASE),
    re.compile(r"could not read password", re.IGNORECASE),
    re.compile(r"invalid username or password", re.IGNORECASE),
    re.compile(r"permission denied \(publickey\)", re.IGNORECASE),
)

_UNCOMMITTED_PATTERNS = (
    re.compile(r"your local changes to the following files would be overwritten", re.IGNORECASE),
    re.compile(r"please commit your changes or stash them", re.IGNORECASE),
)

# "cannot lock ref" means the ref moved between when git computed the expected
# old sha and when the server tried to apply the update. In Trinity this shows
# up when two agent instances race into the same working branch (P5 clobber).
_EXTERNAL_WRITE_PATTERNS = (
    re.compile(r"cannot lock ref", re.IGNORECASE),
    re.compile(r"failed to update ref", re.IGNORECASE),
)

# Rebase-apply failure with explicit sha: the parallel-history trap.
# Shape: `error: could not apply <sha>...` or `Could not apply <sha>...`.
_PARALLEL_HISTORY_PATTERNS = (
    re.compile(r"could not apply [0-9a-f]{7,40}", re.IGNORECASE),
    re.compile(r"conflict \(add/add\):", re.IGNORECASE),
)


def classify_conflict(
    stderr: str,
    ahead: int,
    behind: int,
    common_ancestor_sha: Optional[str] = None,
) -> ConflictClass:
    """Classify a git sync/push/pull failure into an operator-readable class.

    Pure function: takes the raw stderr string plus the current ahead/behind
    counts (as reported by ``git rev-list --left-right --count``) and returns
    a :class:`ConflictClass` enum member. No IO, no DB access.

    The decision order is deliberate:

    1. Auth failures first — they mask everything downstream.
    2. Uncommitted-local before any ref-update checks, because git refuses to
       even try the update when the working tree is dirty.
    3. External-write on the working branch (``cannot lock ref`` /
       ``failed to update ref``) — this is the P5 silent-clobber signature.
    4. Parallel-history (rebase apply failed on a specific sha) — this is P2.
    5. Fall back to numeric state (``AHEAD_ONLY`` / ``BEHIND_ONLY``) when
       stderr is empty or unhelpful.
    6. ``UNKNOWN`` when we genuinely cannot tell.
    """
    # ``common_ancestor_sha`` is accepted for forward compatibility with the
    # parallel-history discriminator in #385; classification today does not
    # need it because the stderr patterns alone are specific enough.
    del common_ancestor_sha

    text = stderr or ""

    for pat in _AUTH_PATTERNS:
        if pat.search(text):
            return ConflictClass.AUTH_FAILURE

    for pat in _UNCOMMITTED_PATTERNS:
        if pat.search(text):
            return ConflictClass.UNCOMMITTED_LOCAL

    for pat in _EXTERNAL_WRITE_PATTERNS:
        if pat.search(text):
            return ConflictClass.WORKING_BRANCH_EXTERNAL_WRITE

    for pat in _PARALLEL_HISTORY_PATTERNS:
        if pat.search(text):
            return ConflictClass.PARALLEL_HISTORY

    if not text.strip():
        if ahead > 0 and behind == 0:
            return ConflictClass.AHEAD_ONLY
        if behind > 0 and ahead == 0:
            return ConflictClass.BEHIND_ONLY

    return ConflictClass.UNKNOWN


# S7 Layer 0: how many times reserve_and_generate_instance_id retries on
# a remote/DB collision before giving up. 5 is generous — with a 32-bit
# UUID prefix the probability of a single collision is ~0 and the probability
# of five in a row is astronomically small, so this catches only real bugs
# (e.g. the caller feeding us a non-unique repo).
MAX_INSTANCE_ID_RETRIES = 5


def generate_instance_id() -> str:
    """Generate a unique instance ID for an agent.

    NOTE (S7 Layer 0): this returns a raw UUID prefix with no remote/DB
    collision check. New call sites should use
    ``reserve_and_generate_instance_id`` instead; this is kept only for
    helpers that need the raw generator (e.g. inside the reserve helper).
    """
    return uuid.uuid4().hex[:8]


def _git_remote_url(github_pat: str, github_repo: str) -> str:
    """Build an authenticated git remote URL.

    Defaults to GitHub. Dev/self-host deployments can override the base via
    TRINITY_GIT_BASE_URL (e.g., "http://trinity-gitea-dev:3000" for a local
    gitea in the test harness). The base URL must include the scheme.
    """
    base = os.getenv("TRINITY_GIT_BASE_URL", "https://github.com").rstrip("/")
    scheme, _, host_path = base.partition("://")
    return f"{scheme}://oauth2:{github_pat}@{host_path}/{github_repo}.git"


def _remote_seturl_subcommand(url: str) -> str:
    """Idempotent `origin` set-url/add shell subcommand, with the (token-bearing)
    URL shell-quoted (#1264 review). Shared by ``initialize_github_sync`` and
    ``update_remote_pat`` so the templating logic lives in one place; the
    ``shlex.quote`` is defense-in-depth (canonical PATs are ``[A-Za-z0-9_]``,
    but ``set_agent_github_pat`` accepts arbitrary input)."""
    q = shlex.quote(url)
    return (
        f"git remote get-url origin >/dev/null 2>&1 && "
        f"git remote set-url origin {q} || git remote add origin {q}"
    )


def generate_working_branch(agent_name: str, instance_id: str) -> str:
    """Generate a working branch name for an agent instance."""
    return f"trinity/{agent_name}/{instance_id}"


async def update_remote_pat(agent_name: str, github_pat: str, github_repo: str) -> bool:
    """Re-template a running agent's ``origin`` remote to embed ``github_pat`` (#1264).

    A per-agent PAT configured *after* the container/git was set up never reaches
    the live remote — it stays frozen with an empty password (e.g.
    ``https://x-access-token:@github.com/...``) in the persisted workspace
    volume, so every fetch/push fails. This rewrites ``remote.origin.url`` to the
    authenticated ``oauth2:<pat>@`` URL (``_git_remote_url``, same scheme
    startup.sh uses), idempotently (set-url if origin exists, else add). The
    startup.sh self-heal does the same on restart; this is the no-restart path
    used by ``set_agent_github_pat``.

    Returns True on success. Best-effort: returns False (never raises) if the
    container isn't running, has no git dir, or the command fails.
    """
    if not github_pat or not github_repo:
        return False
    container_name = f"agent-{agent_name}"
    try:
        git_dir = await _detect_git_dir(container_name)
        cmd = _remote_seturl_subcommand(_git_remote_url(github_pat, github_repo))
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

# A full-history push of an agent's accumulated workspace, over the container's
# network. Matched to fork_to_own.PUSH_TIMEOUT_S so the two halves of the same
# feature cannot disagree about how long a push may legitimately take.
REBIND_PUSH_TIMEOUT_S = 120


def _credentialless_remote_url(github_repo: str) -> str:
    """A remote URL with no userinfo, honouring ``TRINITY_GIT_BASE_URL``.

    Mirrors ``startup.sh``'s ``UPSTREAM_URL`` construction
    (``${GIT_SCHEME}://${GIT_HOST_PATH}/${repo}.git``) so the ``upstream``
    remote this module writes and the one startup.sh self-heals are the same
    string. Distinct from ``_git_remote_url``, which always embeds
    ``oauth2:<pat>@`` — passing an empty PAT there yields ``oauth2:@host``,
    which is NOT credential-less and defeats anonymous fetch.
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
    dest_url = _git_remote_url(user_pat, destination_repo)

    try:
        git_dir = await _detect_git_dir(container_name)
    except Exception as e:  # noqa: BLE001 — container may be down mid-op
        return RebindResult(False, "detect", error=f"Could not reach the agent container: {e}")

    async def _run(cmd: str, timeout: int = 120) -> tuple:
        result = await execute_command_in_container(
            container_name=container_name,
            command=f"bash -c {shlex.quote(f'cd {shlex.quote(git_dir)} && {cmd}')}",
            timeout=timeout,
        )
        return (
            result.get("exit_code", 1),
            _scrub_git_output(result.get("output", ""), user_pat),
        )

    # 1. Push committed history to the destination by explicit URL.
    rc, out = await _run(
        f"git push {shlex.quote(dest_url)} "
        f"{shlex.quote(f'refs/heads/{branch}:refs/heads/{branch}')}",
        timeout=REBIND_PUSH_TIMEOUT_S,
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
        git_dir = await _detect_git_dir(container_name)
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


# ============================================================================
# S4 — Persistent State Allowlist (abilityai/trinity#383)
# ============================================================================
#
# The list of workspace paths that must survive a template-level reset lives
# on disk at `.trinity/persistent-state.yaml` inside each agent. It is seeded
# at creation time from the template (or the defaults below) and may be
# edited per-agent thereafter. Template.yaml is only read at creation
# (template_service.py caches it for 10 minutes); runtime sync/reset paths
# must read from the on-disk file, never re-read the template.

DEFAULT_PERSISTENT_STATE: list[str] = [
    "workspace/**",
    ".trinity/**",
    ".mcp.json",
    ".claude.json",
    ".claude/.credentials.json",
]

_PERSISTENT_STATE_PATH = "/home/developer/.trinity/persistent-state.yaml"

# #1169: declared runtime-data paths over the existing durable home volume.
# Opt-in (empty default) — undeclared agents get no file and no side effects.
DEFAULT_DATA_PATHS: list[str] = []

_DATA_PATHS_PATH = "/home/developer/.trinity/data-paths.yaml"

# #1169: the agent's runtime-data root, relative to the git repo root
# (`/home/developer`). Always gitignored alongside any declared paths so
# runtime data never lands in a commit.
_DATA_ROOT_GITIGNORE = "data/"

# #1704: declared Claude Code plugin selection (marketplaces + installed
# plugins). Unlike persistent-state.yaml / data-paths.yaml this file is
# COMMITTED — it is in `_TRINITY_AUTHORED_PATHS`, so it rides the #2070
# contents-only re-include and survives a git-based reconstitution onto a
# fresh volume or a new host (the real #1704 gap; a plain recreate is already
# volume-safe). The boot hook (`agent_server.plugins_reinstall`) reads it and
# re-installs anything declared-but-missing. Opt-in — empty declaration writes
# no file.
_PLUGINS_PATH = "/home/developer/.trinity/plugins.yaml"


# ---------------------------------------------------------------------------
# Shared `.trinity/<file>.yaml` list primitives (#1169)
#
# Extracted from the S4 persistent-state functions (#383) so persistent_state
# and data_paths share one injection-safe write/read implementation. The
# heredoc delimiter is a parameter so each caller keeps its own marker (the
# S4 tests pin `PSTATE_EOF`).
# ---------------------------------------------------------------------------


async def _write_trinity_yaml_file(
    agent_name: str,
    *,
    path: str,
    body: str,
    heredoc: str,
    timeout: int = 10,
) -> None:
    """Write a pre-serialized YAML `body` to `path` inside the agent container.

    THE single container-write mechanism for every `.trinity/*.yaml` the backend
    materializes (#953: never hand-roll a raw container write). The single-quoted
    heredoc preserves the body verbatim and is injection-safe (no shell expansion
    inside `<<'HEREDOC'`); callers keep the body's values free of the outer
    `bash -c "..."` metacharacters (`$`, backtick, `"`, `\\`) — which the reused
    `_SAFE_DATA_PATH_RE` / `template_plugins` charset validators guarantee.
    """
    cmd = (
        f"mkdir -p /home/developer/.trinity && "
        f"cat > {path} <<'{heredoc}'\n{body}{heredoc}"
    )
    await execute_command_in_container(
        container_name=f"agent-{agent_name}",
        command=f'bash -c "{cmd}"',
        timeout=timeout,
    )


async def materialize_trinity_yaml_list(
    agent_name: str,
    *,
    path: str,
    key: str,
    patterns: list[str],
    heredoc: str,
    timeout: int = 10,
) -> None:
    """Write `{key: [patterns]}` as YAML to `path` inside the agent container.

    The single-quoted heredoc preserves glob characters verbatim and is
    injection-safe (no shell expansion inside the body).
    """
    import yaml as _yaml

    body = _yaml.safe_dump({key: list(patterns)}, sort_keys=False)
    await _write_trinity_yaml_file(
        agent_name, path=path, body=body, heredoc=heredoc, timeout=timeout
    )


async def _read_trinity_yaml_list(
    agent_name: str,
    *,
    path: str,
    key: str,
    default: list[str],
    timeout: int = 5,
) -> list[str]:
    """Read a `{key: [...]}` list from `path`, falling back to `default`.

    Returns the on-disk list when present and valid; otherwise returns a
    fresh copy of `default` (never a reference — callers may mutate the
    result, and must not mutate a shared default constant).
    """
    import yaml as _yaml
    result = await execute_command_in_container(
        container_name=f"agent-{agent_name}",
        command=f'bash -c "cat {path} 2>/dev/null || true"',
        timeout=timeout,
    )
    if result.get("exit_code", 0) != 0:
        return list(default)
    raw = result.get("output", "").strip()
    if not raw:
        return list(default)
    try:
        # ent#314: agent-written file read out of the container.
        data = _load_hardened_yaml(
            raw, kind="agent_yaml", alias_policy=_AliasPolicy.REJECT
        ) or {}
    except (_yaml.YAMLError, _HardenedYamlError):
        return list(default)
    patterns = data.get(key)
    if not isinstance(patterns, list) or not patterns:
        return list(default)
    return [str(p) for p in patterns]


async def materialize_persistent_state(
    agent_name: str, patterns: list[str]
) -> None:
    """Write `.trinity/persistent-state.yaml` inside the agent container.

    Called once from `agent_service.crud` after the container is running.
    Operators may edit the file thereafter; runtime readers treat the
    on-disk copy as authoritative.
    """
    await materialize_trinity_yaml_list(
        agent_name,
        path=_PERSISTENT_STATE_PATH,
        key="persistent_state",
        patterns=patterns,
        heredoc="PSTATE_EOF",
    )


async def _persistent_state_for(agent_name: str) -> list[str]:
    """Read the persistent-state allowlist for an agent.

    Returns the on-disk list when `.trinity/persistent-state.yaml` is
    present and valid; otherwise returns a fresh copy of
    `DEFAULT_PERSISTENT_STATE`. Consumers of this helper (e.g. the future
    reset-preserve-state operation from #384) must not mutate the default
    constant, hence the defensive `list(...)` copies on every fallback.
    """
    return await _read_trinity_yaml_list(
        agent_name,
        path=_PERSISTENT_STATE_PATH,
        key="persistent_state",
        default=DEFAULT_PERSISTENT_STATE,
    )


# #1169 L1: a declared data_path must be a plain glob over the data root —
# alphanumerics, path separators, and glob metacharacters only. The heredoc
# write below runs in the agent's OWN container (no privilege boundary), but a
# shell metacharacter (quote, `$`, backtick, `;`, `|`, …) in a template-supplied
# entry would still corrupt the `bash -c` tokenization and fail the whole
# materialization, so unsafe entries are dropped loudly rather than silently
# breaking the declaration. `re.fullmatch` rejects any embedded/trailing
# newline that `$` would otherwise tolerate.
_SAFE_DATA_PATH_RE = re.compile(r"[A-Za-z0-9_./*?{},\[\] -]+")


def _is_safe_data_path(path: str) -> bool:
    """True if `path` is a shell-safe glob over the data root (#1169 L1)."""
    return bool(path) and _SAFE_DATA_PATH_RE.fullmatch(path) is not None


async def materialize_data_paths(agent_name: str, paths: list[str]) -> None:
    """Materialize an agent's declared `data_paths` (#1169).

    Writes `.trinity/data-paths.yaml` (key `data_paths`) AND appends the
    runtime-data root + each declared path to the agent's own `.gitignore`,
    so the declaration and its ignore rule are materialized together.

    Opt-in: a falsy/whitespace-only declaration is a complete no-op — no
    file is written and no `.gitignore` is touched — so undeclared agents
    are entirely unaffected. Entries carrying shell metacharacters are
    dropped (logged) before any container write (#1169 L1).
    """
    cleaned = [str(p).strip() for p in (paths or []) if str(p).strip()]
    safe: list[str] = []
    dropped: list[str] = []
    for p in cleaned:
        (safe if _is_safe_data_path(p) else dropped).append(p)
    if dropped:
        logger.warning(
            "[#1169] Dropping %d unsafe data_paths entr%s for %s "
            "(shell metacharacters): %r",
            len(dropped),
            "y" if len(dropped) == 1 else "ies",
            agent_name,
            dropped,
        )
    if not safe:
        return
    await materialize_trinity_yaml_list(
        agent_name,
        path=_DATA_PATHS_PATH,
        key="data_paths",
        patterns=safe,
        heredoc="DATAPATHS_EOF",
    )
    await _append_agent_gitignore(
        agent_name, _data_paths_gitignore_entries(safe)
    )


async def _data_paths_for(agent_name: str) -> list[str]:
    """Read an agent's declared `data_paths` (#1169).

    Returns the on-disk list when `.trinity/data-paths.yaml` is present and
    valid; otherwise a fresh empty list (data_paths is opt-in).
    """
    return await _read_trinity_yaml_list(
        agent_name,
        path=_DATA_PATHS_PATH,
        key="data_paths",
        default=DEFAULT_DATA_PATHS,
    )


async def materialize_plugins(agent_name: str, plugins: dict) -> None:
    """Materialize an agent's declared Claude Code plugins (#1704).

    Writes `.trinity/plugins.yaml` as nested
    `{plugins: {marketplaces: [{name, source}], installed: ["plugin@mkt"]}}` via
    the shared injection-safe heredoc writer. The file is COMMITTED (it is in
    `_TRINITY_AUTHORED_PATHS`), so the declaration survives a git-based
    reconstitution onto a fresh volume or a new host, where the gitignored
    `~/.claude.json` + `~/.claude/plugins/` cache are dropped.

    `plugins` is the ALREADY-normalized dict from
    `template_plugins.normalize_declared_plugins` — every value is
    charset-validated, so the nested body is safe inside the outer `bash -c`
    double quotes (see `_write_trinity_yaml_file`).

    Opt-in: a falsy declaration (`{}` / None / no marketplaces + no installed)
    is a complete no-op — no file is written.

    Determinism (a correctness property): `sort_keys=True` + the normalizer's
    sorted, de-duplicated lists mean a stable plugin set produces a byte-
    identical file, so the 15-min auto-sync loop never re-commits a churning
    manifest — unlike the flat `materialize_trinity_yaml_list` (`sort_keys=False`).
    """
    import yaml as _yaml

    if not isinstance(plugins, dict):
        return
    marketplaces = plugins.get("marketplaces") or []
    installed = plugins.get("installed") or []
    if not marketplaces and not installed:
        return

    body = _yaml.safe_dump(
        {"plugins": {"marketplaces": marketplaces, "installed": installed}},
        sort_keys=True,
        default_flow_style=False,
    )
    await _write_trinity_yaml_file(
        agent_name, path=_PLUGINS_PATH, body=body, heredoc="PLUGINS_EOF"
    )


def _data_paths_gitignore_entries(paths: list[str]) -> list[str]:
    """Gitignore lines for declared data_paths: the `data/` root plus each
    declared path, deduped and order-preserving.
    """
    entries = [_DATA_ROOT_GITIGNORE]
    for p in paths:
        if p not in entries:
            entries.append(p)
    return entries


async def _append_agent_gitignore(agent_name: str, patterns: list[str]) -> None:
    """Append `patterns` to the agent's own `/home/developer/.gitignore` (#1169).

    Idempotent (exact-line `grep -qxF` gate). Best-effort and scoped to the
    agent's repo root — never the fleet-wide `_GITIGNORE_PATTERNS`.
    """
    if not patterns:
        return
    await execute_command_in_container(
        container_name=f"agent-{agent_name}",
        command=_build_gitignore_append_command("/home/developer", patterns),
        timeout=10,
    )


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


# Stderr fragments that mean the REMOTE answered and refused: the repo needs
# credentials (private) or does not exist. Anonymous GitHub deliberately
# answers both the same way, so callers must present them as one outcome.
_ANON_PROBE_DEFINITIVE_PATTERNS = (
    "could not read username",       # auth challenge with GIT_TERMINAL_PROMPT=0
    "authentication failed",
    "repository not found",
    "could not read password",
)


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


# ent#109 retired the workaround this used to teach ("create a new agent with
# fork-to-own and import your data"), which discarded the agent's identity,
# name reservation and history. The retrofit now exists in place, so point at
# it. Kept in sync with the MCP 409 hint in `src/mcp-server/src/tools/git.ts`
# (Invariant #13) by `tests/unit/test_ent109_no_write_credentials_message.py`.
NO_WRITE_CREDENTIALS_MESSAGE = (
    "This agent has no write credentials — it tracks a public template "
    "read-only. Use 'Bind to your own repo' in this agent's Git tab to point "
    "it at a GitHub repository you own (keeping everything it has learned), "
    "or add a GitHub token."
)


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
            message=NO_WRITE_CREDENTIALS_MESSAGE,
            conflict_type="no_write_credentials",
            conflict_class="AUTH_FAILURE",
        )

    # #462: bring the workspace `.gitignore` up to the current canonical list
    # and untrack any files that NOW match a rule. Runs on every Push so
    # existing agents migrate without re-init or container rebuild. Best
    # effort — failures are logged inside the helper and Push proceeds.
    await _migrate_workspace_gitignore(agent_name)

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


# ============================================================================
# Git Initialization in Container
# ============================================================================

# Canonical exclusion list merged (append-if-missing) into every agent's
# `.gitignore`. This is the single source of truth — the matching block in
# `docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md` mirrors it, and a unit test
# (`test_doc_and_constant_in_sync`) keeps them aligned.
#
# Ordering is preserved in the file so operators reading it see entries
# grouped by category. The list covers the runtime/instance noise the #462
# bug report named (1,599 files leaked on a single Push) plus the credential
# files `inject_credentials` writes (the #458 trio).
# Authored content under `.trinity/` — files a TEMPLATE commits and the
# platform reads back (#2070). Everything else in that directory is runtime
# state the platform writes.
#
# The rule used to be "ignore `.trinity/` wholesale", with a hardcoded pathspec
# exemption list in the `git rm --cached` sweep. That is backwards: the default
# was "untrack it", so every authored file needed remembering, and three
# separate incidents were three forgotten strings — brain-orb hooks
# (trinity-enterprise#76), `setup.sh` (swept live before the second exemption),
# and `pre-check` (#2070, the SCHED-COND-001 hook the platform's own docs tell
# template authors to commit).
#
# Inverted: `.trinity/*` ignores the directory's contents, and each authored
# path is re-included. A NEW runtime file is ignored by default (no action
# needed), and authored content is tracked by construction rather than by an
# exemption someone has to remember. The star form is required — git does not
# descend into a directory excluded by the `.trinity/` dir-form, so negations
# under it never apply (which is also why compat check S-005 accepts it).
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


# Exact lines the fleet-wide merge REMOVES before appending (#2070).
#
# Append-only is not enough here. Git does not descend into a directory
# excluded by the dir-form ``.trinity/``, so the ``!.trinity/pre-check``
# negations that follow it are inert. Every agent synced before #2070 carries
# that line — precisely the fleet this fixes — so leaving it in place would
# keep sweeping authored hooks forever.
#
# Removal is by EXACT LINE and confined to the two spellings this platform
# itself wrote (the same discipline as the G-001 auto-fix, which removes a
# blanket ``.claude/`` line by exact-line match). A user rule that merely
# mentions the directory — ``.trinity/scratch/``, ``!.trinity/mine`` — does not
# match and is left alone.
_GITIGNORE_SUPERSEDED_LINES: Tuple[str, ...] = (
    ".trinity/",
    ".trinity",
)


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


AGENT_HOME_DIR = "/home/developer"
LEGACY_WORKSPACE_DIR = "/home/developer/workspace"


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


# ---------------------------------------------------------------------------
# #2069: creation-time canonical `.gitignore` seed
# ---------------------------------------------------------------------------
#
# `_GITIGNORE_PATTERNS` was applied at exactly two operator-initiated moments —
# the Push migration (`_migrate_workspace_gitignore`) and the init-path merge
# (`initialize_git_in_container` step 2) — and NEVER at agent creation. But the
# in-container 15-min auto-sync loop (`agent_server/auto_sync.py`, a bare
# `git add -A`) is on FROM BIRTH for the `GIT_SYNC_AUTO` set (non-source-mode /
# fork-to-own `github:` agents, ephemeral ghosts included), so such an agent
# auto-committed `.trinity/` runtime state, `.claude/projects/`, `content/` and
# the root-level `.env` / `.mcp.json` (which sit at the repo root because $HOME
# IS the repo root, #1703) into its user-owned repo before any Push could
# migrate the list — a credential-leak-into-a-private-repo hygiene hazard plus
# the #1595/#1596 unbounded-`.git`-bloat class. This lands the canonical list on
# disk after startup.sh's FULL git setup and before the first auto-sync cycle.
# #1703 (repo root ≡ $HOME) is the structural fix that retires this whole layer.

# Deadline sized to a slow full-history clone + startup, NOT to the 900s
# pre-first-cycle sleep (which is NOT relied on for correctness). Env-tunable.
_MERGE_READY_TIMEOUT_SECONDS = int(
    os.getenv("TRINITY_GITIGNORE_MERGE_TIMEOUT_SECONDS", "1800")
)
_MERGE_READY_INTERVAL_SECONDS = int(
    os.getenv("TRINITY_GITIGNORE_MERGE_INTERVAL_SECONDS", "5")
)
# Per-operation `asyncio.wait_for` belt around EVERY exec/HTTP (the /health
# probe, the `.git` check, `_git_toplevel`, the merge exec): `execute_command_in_
# container`'s `timeout=` arg is never forwarded to `container_exec_run`
# (docker_service.py), so a hung exec would otherwise pin the fire-and-forget
# task. HONEST: `wait_for` frees the TASK, not the pinned Docker pool thread (the
# shared 4-worker executor, docker_utils.py) — hence the Semaphore cap below.
_MERGE_EXEC_TIMEOUT_SECONDS = 30
# Fleet-wide cap so N concurrent creations can't fire unbounded concurrent Docker
# execs and starve unrelated Docker ops on the shared 4-thread pool. Scoped to the
# EXEC section only (`.git` check / `_git_toplevel` / merge) — NOT the readiness
# poll, which is pure agent-`/health` HTTP and touches no pool thread. Holding the
# cap across a (<= _MERGE_READY_TIMEOUT_SECONDS) readiness wait would let a handful
# of slow-/never-booting agents head-of-line-block a healthy, fast-booting agent's
# merge past its OWN first auto-sync cycle (server-boot + ~900s) — re-opening the
# exact leak this fix closes.
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
    git_dir = await _detect_git_dir(container_name)
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
        command=_build_gitignore_merge_command(git_dir),
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
        (_remote_seturl_subcommand(_git_remote_url(github_pat, github_repo)), True),
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


# ============================================================================
# S3 — Reset-to-main-preserve-state proxy (abilityai/trinity#384)
# ============================================================================


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
            "message": NO_WRITE_CREDENTIALS_MESSAGE,
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
