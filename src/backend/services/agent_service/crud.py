"""
Agent Service CRUD - Agent creation and deletion operations.

Contains the core logic for creating and deleting agents.
"""
import asyncio
import os
import re
import json
import secrets
import docker
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Union

import yaml
from fastapi import HTTPException, Request

from models import AgentConfig, AgentStatus, User
from db_models import ScheduleCreate
from database import db
from services.docker_service import (
    docker_client,
    get_agent_by_name,
    get_agent_container,
    get_next_available_port,
    get_agent_status_from_container,
)
from services.docker_utils import (
    volume_get, volume_create, containers_run, container_remove
)
from services.agent_runtime_state import clear_agent_breakers, clear_agent_runtime_state
from services.template_service import (
    CredentialDeclarationError,
    credential_mcp_server_names,
    fetch_template_metadata_result_for_create,
    get_github_template,
    metadata_reason_is_unreadable,
    generate_credential_files,
)
from services.template_schedules import normalize_declared_schedules
from services.template_plugins import normalize_declared_plugins
from services import git_service
from services.settings_service import get_anthropic_api_key, resolve_github_pat, get_agent_full_capabilities, get_agent_quota_for_role, get_agent_default_resources, get_agent_default_require_email, get_ephemeral_agent_quota, get_ephemeral_ttl_ceiling_seconds
from services.entitlement_service import entitlement_service
from services import rate_limiter
from . import ephemeral as ephemeral_service
from services.github_service import GitHubService, GitHubError
from services.agent_auth import derive_agent_token
from utils.helpers import parse_iso_timestamp, sanitize_agent_name, to_utc_iso, utc_now_iso
from utils.safe_yaml import HardenedYamlError, load_template_yaml
from .fork_to_own import fork_template_to_own_repo
from . import snapshot_import
from .helpers import validate_base_image, is_claude_runtime, validate_runtime
from .lifecycle import RESTRICTED_CAPABILITIES, FULL_CAPABILITIES
from .capabilities import (
    AGENT_TMPFS_MOUNT,
    AGENT_DEFAULT_TMPDIR,
    AGENT_LOG_CONFIG,
    normalize_cpu,
    normalize_memory,
)

logger = logging.getLogger(__name__)

# Allowed chars in a `local:`-prefixed template name. Strict enough to
# block path traversal (`..`, `/`, `\`, leading dots) so the templates
# directory join in `create_agent_internal` can't escape into arbitrary
# filesystem reads (CodeQL py/path-injection on #950 PR).
_LOCAL_TEMPLATE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")

# Allowed chars in a template-declared credential-file path. Unlike
# `_LOCAL_TEMPLATE_NAME_RE` this is a *relative path*, so `/` is permitted —
# but nothing that could start an absolute path or a traversal.
# (trinity-enterprise#128)
_CRED_FILE_PATH_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._/-]*$")

_CONTAINER_CURATED_TEMPLATES = Path("/agent-configs/templates")


def _repo_local_templates_dir() -> Path:
    """Repo-relative curated-template directory, for source-run backends.

    Mirrors the fallback in `template_service._local_templates_dir()` (#843).
    It is **hand-rolled, never imported**: `services.template_service` is
    MagicMocked by the #1484 characterization harness, so a gate that called
    into it would be satisfied by a truthy mock and those tests would stay
    green on the pre-#1759 behaviour — a silent, uncatchable regression.

    `parents[4]`, NOT `.parent * 4`: this module lives one directory deeper
    than `template_service.py` (`services/agent_service/` vs `services/`), so
    the copy-pasted four-`.parent` form yields
    `<repo>/src/config/agent-templates`, which does not exist. Pinned by
    `tests/unit/test_1759_template_root_parity.py`.
    """
    # The guard is NOT decorative. In the container layout
    # `/app/services/agent_service/crud.py` has only 4 parents (0-3), so a bare
    # `parents[4]` raises IndexError — and `_LOCAL_TEMPLATE_ROOTS` is computed at
    # IMPORT time, so that IndexError would stop the backend booting rather than
    # degrade a single request. The branch is believed unreachable (taken only
    # when `/agent-configs/templates` is absent, and both compose files always
    # bind it on `backend`), but "believed unreachable" is not a reason to ship a
    # crash-on-import path — especially one this file introduces.
    parents = Path(__file__).resolve().parents
    if len(parents) <= 4:
        # No repo root above us: an installed/container layout, where the
        # container catalog is the only meaningful answer.
        return _CONTAINER_CURATED_TEMPLATES
    return parents[4] / "config" / "agent-templates"


def _curated_templates_root() -> Path:
    """Curated-catalog root: the read-only bind mount inside a Trinity
    container, the in-repo catalog otherwise (#1759).

    Without the fallback neither root exists outside a container, so the
    `UNKNOWN_LOCAL_TEMPLATE` gate below (#1793) would 404 *every* `local:`
    create in dev shells, source-run CI and unit tests — the gate would be
    hostile exactly where the test suite runs, and #1793 could only paper over
    that by pointing `_LOCAL_TEMPLATE_ROOTS` at a tmp fixture inside the #1484
    harness (the #1638 accidental-green pattern).
    """
    if _CONTAINER_CURATED_TEMPLATES.exists():
        return _CONTAINER_CURATED_TEMPLATES.resolve()
    return _repo_local_templates_dir()


def _default_host_templates_base() -> str:
    """Fallback bind-source base for the `/template` mount (#1759).

    Inside a Trinity container the catalog is the read-only bind at
    `/agent-configs/templates` and compose always sets `HOST_TEMPLATES_PATH`,
    so this returns today's literal relative default verbatim — the container
    path is byte-identical. Outside a container the repo path *is* a host path
    and a valid bind source, so return it resolved rather than a relative path
    Docker would refuse.
    """
    if _CONTAINER_CURATED_TEMPLATES.exists():
        return "./config/agent-templates"
    return str(_repo_local_templates_dir())


# Roots that a resolved local-template path must stay within (#950). Read at
# THREE seams — `_resolve_local_template`, the `/template` bind decision in
# `_stage_config_files`, and (since #1900) the credential-file stager in the
# same function — which must always agree (#1759). The first two were named by
# #1759; the third existed all along and did NOT agree, because it re-derived
# the directory from the template's untrusted `name:` field. Two of the three
# now share `_resolve_local_template_dir`, which is what makes the agreement
# structural rather than a convention. Kept a module-level tuple: it is the
# single monkeypatch point the create tests use.
_LOCAL_TEMPLATE_ROOTS = (
    _curated_templates_root(),
    Path("/data/deployed-templates").resolve(),
)


def _safe_local_template_path(template_name: str, root: Path) -> Path:
    """Join `template_name` onto `root` and prove it didn't traverse out.

    Two-step defense:

    1. Regex allowlist on the name (rejects `..`, `/`, `\\`, leading
       dots etc.) — fail fast with HTTP 400 for obviously hostile input.
    2. Resolve the joined path and assert `is_relative_to(root)` — this
       is the pattern CodeQL recognises as a `py/path-injection`
       barrier, so the static analyser stops marking subsequent
       `.exists()` / `open()` calls on the returned path as tainted.

    Either failure raises `HTTPException(400)` with structured code
    `INVALID_LOCAL_TEMPLATE_NAME`.
    """
    if (
        not template_name
        or ".." in template_name
        or not _LOCAL_TEMPLATE_NAME_RE.match(template_name)
    ):
        raise HTTPException(
            status_code=400,
            detail={
                "error": (
                    f"Invalid local template name {template_name!r}: must match "
                    f"[a-zA-Z0-9][a-zA-Z0-9_.-]* with no '..' segments."
                ),
                "code": "INVALID_LOCAL_TEMPLATE_NAME",
            },
        )
    candidate = (root / template_name).resolve()
    if not candidate.is_relative_to(root):
        raise HTTPException(
            status_code=400,
            detail={
                "error": (
                    f"Resolved template path {candidate} escaped expected root {root}."
                ),
                "code": "INVALID_LOCAL_TEMPLATE_NAME",
            },
        )
    return candidate


def _safe_cred_file_path(relative_path: str, root: Path) -> Path:
    """Join a template-declared credential-file path onto `root`, proving it stays inside.

    `credentials.config_files[].path` is an author-controlled string that ends
    up as `open(root / path, "w")`, and a template is untrusted input — any
    authenticated user can upload one through `deploy_local_agent_logic`. An
    absolute path silently wins the join (`Path("/a") / "/etc/x"` is `/etc/x`)
    and `..` walks out of it, so without this the declaration is an
    arbitrary-file-write primitive. (trinity-enterprise#128)

    `template_service.credential_shape_errors` already rejects both shapes at
    the parse boundary; this is the barrier at the sink. Two steps, mirroring
    `_safe_local_template_path`:

    1. Allowlist the raw string — reject empty, absolute, `..`-bearing and
       anything outside `[A-Za-z0-9._/-]`, BEFORE it reaches the join.
    2. Resolve the joined path and assert `is_relative_to(root)`.

    Step 1 is not redundant: it is what makes the guard legible to a reader
    *and* to CodeQL, which does not treat resolve + `is_relative_to` alone as a
    `py/path-injection` barrier (this helper was flagged high-severity twice
    when it had only step 2).

    Raises `HTTPException(400)` with code `INVALID_CREDENTIAL_FILE_PATH`.
    """
    if (
        not relative_path
        or relative_path.startswith("/")
        or ".." in relative_path
        or not _CRED_FILE_PATH_RE.match(relative_path)
    ):
        raise HTTPException(
            status_code=400,
            detail={
                "error": (
                    f"Template credential file path {relative_path!r} is not a "
                    f"plain relative path inside the agent's credential "
                    f"directory."
                ),
                "code": "INVALID_CREDENTIAL_FILE_PATH",
            },
        )

    root = root.resolve()
    candidate = (root / relative_path).resolve()
    if not candidate.is_relative_to(root):
        raise HTTPException(
            status_code=400,
            detail={
                "error": (
                    f"Template credential file path {relative_path!r} escapes "
                    f"the agent's credential directory."
                ),
                "code": "INVALID_CREDENTIAL_FILE_PATH",
            },
        )
    return candidate


def _fork_destination_in_use_message(destination: str, bound_agent: str, username: str) -> str:
    """409 detail for a destination repo already bound to an agent (#93).

    Names the bound agent only when the caller can access it — a creator-role
    caller must not be able to map repo→agent bindings fleet-wide (#186
    enumeration discipline).
    """
    try:
        can_see = db.can_user_access_agent(username, bound_agent)
    except Exception:
        can_see = False
    who = f"agent '{bound_agent}'" if can_see else "another agent"
    return (
        f"Repository '{destination}' is already the workspace of {who}. "
        f"Each agent needs its own repository."
    )


def _get_default_resource(key: str) -> str:
    """Return system-default cpu or memory, falling back to hardcoded safe value."""
    defaults = get_agent_default_resources()
    return defaults.get(key, "2" if key == "cpu" else "4g")


def get_platform_version() -> str:
    """Get the current Trinity platform version from VERSION file."""
    version_paths = [
        Path("/app/VERSION"),  # In container
        Path(__file__).parent.parent.parent.parent.parent / "VERSION",  # Development
    ]
    for version_path in version_paths:
        if version_path.exists():
            return version_path.read_text().strip()
    return "unknown"


# ===========================================================================
# create_agent_internal phase-helpers (#1484)
#
# `create_agent_internal` was decomposed into the named phase-helpers below.
# The orchestrator keeps the `if docker_client: try/except/else` INLINE (so
# *what* is caught is byte-identical) and threads shared builders (`env_vars`,
# `volumes`) and the mutated `config` explicitly; each producing phase RETURNS
# its handles into the orchestrator's local variables. The except/else read the
# single `_RollbackHandles` object, populated ONLY by the orchestrator.
#
# These helpers deliberately stay in this file for now; splitting them into
# `agent_service/creation_phases.py` is the mechanical #1028 follow-up.
# ===========================================================================


@dataclass
class _TemplateResolution:
    """Set-once outputs of the template-resolution phase (github | local).

    Mirrors the upfront `x = None` init block the monolith carried, so a raise
    before a field is produced leaves a benign default (no new NameError
    surface).
    """
    template_data: dict = field(default_factory=dict)
    github_template_path: Optional[str] = None
    github_repo_for_agent: Optional[str] = None
    github_pat_for_agent: Optional[str] = None
    github_pat_tier: str = "none"  # ent#162: per_user/fork → persist per-agent PAT
    git_instance_id: Optional[str] = None
    git_working_branch: Optional[str] = None
    fork_upstream_repo: Optional[str] = None
    template_shared_folders: Optional[dict] = None
    # trinity-enterprise#89: NORMALIZED declared `schedules:`, fed by BOTH
    # resolver branches. Deliberately NOT folded into `template_data`, which
    # carries two different shapes: raw template YAML on the `local:` path and
    # `{}` on the `github:` path (which has never populated it — hence #383's
    # `persistent_state` and #1169's `data_paths` being silently `local:`-only).
    # `_stage_config_files` gates credential-file generation on
    # `if template_data:`, and the `github:` catalog dict has no `credentials`
    # key at all, so merging the two would change credential generation for
    # every GitHub agent. One normalized carrier, two producers.
    declared_schedules: list = field(default_factory=list)
    # #1704: NORMALIZED declared `plugins:` (Claude Code marketplace plugins),
    # fed by ALL THREE resolver branches — same one-carrier-two-producers shape
    # as `declared_schedules`, NOT folded into `template_data` (the `github:`
    # path never populates it). Empty dict = opt-in no-op.
    declared_plugins: dict = field(default_factory=dict)
    # trinity-enterprise#15: staged backend-materialized snapshot for the
    # "copy" import intent. When set, `github_repo_for_agent` stays None by
    # design — the container gets NO GitHub env, no git-config row, no PAT.
    copy_snapshot: Optional["snapshot_import.SnapshotStaging"] = None


@dataclass
class _RollbackHandles:
    """The exact handles the except/else roll back (AC #3). Every field is
    defaulted and the ORCHESTRATOR is the sole populator — the phase-helpers
    never touch it, so the caught behavior is byte-identical to the monolith."""
    agent_name: str = ""
    agent_mcp_key: object = None
    git_instance_id: Optional[str] = None
    github_repo_for_agent: Optional[str] = None
    ephemeral_slot_reserved: bool = False
    ephemeral_owner_id: Optional[int] = None
    # ent#313: wall-clock instant captured immediately BEFORE the docker block.
    # A container carrying a `trinity.created` label older than this existed
    # before this attempt, so this attempt did not create it and must not
    # remove it. See `_reclaim_failed_creation_container`.
    container_floor_ts: Optional[str] = None
    # trinity-enterprise#15: copy-intent snapshot cleanup — the staged clone
    # dir (always removable) and the volume THIS attempt pre-populated (set
    # only after `_prepopulate_workspace_from_template` succeeds, so the
    # rollback never removes a volume another owner claims; removing it keeps
    # the #1667 leftover-volume guard from 409ing the user's retry).
    copy_staging_dir: Optional[str] = None
    copy_volume_name: Optional[str] = None


def _apply_ephemeral_pregates(config: AgentConfig, current_user: User) -> Optional[str]:
    """trinity-enterprise#69 ghost pre-gates — ALL before any side effect.

    Returns the stamped `ephemeral_expires_at` (None for a non-ghost) and, for a
    ghost, mutates `config.name` to the unique `{base}-{hex8}` suffix. Raises the
    gate HTTPExceptions in priority order (entitlement → fork-conflict →
    spawn-recursion → spawn-rate-limit → ttl-ceiling → name-allocation).
    """
    ephemeral_expires_at = None
    if config.ephemeral:
        # Entitlement-gated creation surface (the lifecycle mechanics below
        # are edition-agnostic; only creating WITH a budget is gated).
        if not entitlement_service.is_entitled("ephemeral_agents"):
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "Ephemeral agents are not available in this edition.",
                    "code": "ephemeral_not_entitled",
                },
            )
        # fork_to_own makes a durable user-owned repo — pointless for a ghost.
        if config.fork_to_own:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "fork_to_own cannot be combined with ephemeral.",
                    "code": "ephemeral_fork_to_own_conflict",
                },
            )
        # An ephemeral agent must not spawn ephemeral agents (chain-spawn
        # depth-1 kill; belt to the key-fence braces in dependencies.py).
        if current_user.agent_name:
            parent_info = db.get_agent_ephemeral_info(current_user.agent_name)
            if isinstance(parent_info, dict) and parent_info.get("is_ephemeral"):
                raise HTTPException(
                    status_code=403,
                    detail={
                        "error": "Ephemeral agents cannot spawn ephemeral agents.",
                        "code": "ephemeral_spawn_recursion",
                    },
                )
            # Per-parent spawn rate limit (agent-scoped callers only) —
            # resets never compound across generations because of the
            # recursion refusal above.
            rate_limiter.enforce(
                f"agent_spawn:{current_user.agent_name}",
                int(os.getenv("EPHEMERAL_SPAWN_RATE_LIMIT", "10")),
                int(os.getenv("EPHEMERAL_SPAWN_RATE_WINDOW_S", "3600")),
                detail="Ephemeral spawn rate limit exceeded for this agent.",
            )
        # TTL is ALWAYS stamped (no immortal ghost): default to the platform
        # ceiling when only max_executions was given.
        ttl_ceiling = get_ephemeral_ttl_ceiling_seconds()
        ttl = config.ephemeral.ttl_seconds or ttl_ceiling
        if ttl > ttl_ceiling:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": f"ttl_seconds exceeds the platform ceiling ({ttl_ceiling}s).",
                    "code": "ephemeral_ttl_exceeds_ceiling",
                },
            )
        ephemeral_expires_at = to_utc_iso(
            datetime.now(timezone.utc) + timedelta(seconds=ttl)
        )
        # Server-suffixed name: unique-by-construction, so a discarded ghost's
        # KEEP-policy execution rows are never inherited by a successor and
        # concurrent fan-out spawns can share a base name. 8 hex chars (review
        # M3): 4 would collide at fan-out scale (~300 spawns of one base name
        # within the 90d execution-row retention ≈ 50% birthday odds), and a
        # collision inherits the dead ghost's terminal rows → stillborn ghost.
        base_name = config.name[:48]
        for _ in range(5):
            candidate = f"{base_name}-{secrets.token_hex(4)}"
            if not (
                get_agent_by_name(candidate)
                or db.is_agent_name_reserved(candidate)
            ):
                config.name = candidate
                break
        else:
            raise HTTPException(
                status_code=409,
                detail="Could not allocate a unique ephemeral agent name",
            )
    return ephemeral_expires_at


async def _guard_leftover_workspace_volume(
    config: AgentConfig, adopt_existing_workspace: bool
) -> None:
    """#1667: refuse a leftover workspace volume that NOTHING claims unless the
    caller explicitly declares an adopt. Raised BEFORE the docker try-block so
    the 409 isn't flattened to a generic 500 (nothing is built yet to roll
    back). Ghosts are volume-less, so they never reach here."""
    if (
        not config.ephemeral
        and not adopt_existing_workspace
        and docker_client
    ):
        _workspace_vol = f"agent-{config.name}-workspace"
        try:
            await volume_get(_workspace_vol)
        except docker.errors.NotFound:
            pass  # the normal path: no leftover, create it below
        except Exception as e:  # noqa: BLE001 — a probe failure must not block creation
            logger.warning(
                "[#1667] could not probe workspace volume %s (%s); proceeding",
                _workspace_vol,
                e,
            )
        else:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Data volume '{_workspace_vol}' already exists and no agent "
                    f"claims it, so its contents would silently become this "
                    f"agent's home directory. Refusing to adopt another agent's "
                    f"leftover data. Remove it (docker volume rm "
                    f"{_workspace_vol}) or choose a different name — unclaimed "
                    f"agent volumes are also reclaimed automatically."
                ),
            )


# ent#123: owner/repo charset guard. The repo path is interpolated into
# startup.sh's `eval`-built clone command; the PAT-ful REST validation only
# blocked garbage incidentally, and the tokenless path replaces REST with a
# git-transport probe — so the barrier must be explicit, not incidental.
# GitHub's own owner/repo charset is a subset of this.
_GITHUB_REPO_PATH_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _parse_github_ref(config: AgentConfig) -> tuple[str, str, Optional[str]]:
    """GIT-002: parse `github:owner/repo[@branch]` into `(template_lookup,
    repo_path, url_branch)`. Mutates `config.source_branch` when a valid branch
    is present in the URL."""
    template_str = config.template[7:]  # Remove "github:" prefix
    url_branch = None
    if "@" in template_str:
        template_str, url_branch = template_str.rsplit("@", 1)
        # Validate branch name (alphanumeric plus - _ /)
        if url_branch and url_branch.replace("-", "").replace("_", "").replace("/", "").isalnum():
            config.source_branch = url_branch
            logger.info(f"GIT-002: Parsed branch from URL: {url_branch}")
        else:
            url_branch = None  # Invalid branch, ignore

    if "/" in template_str and not _GITHUB_REPO_PATH_RE.match(template_str):
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid GitHub repository reference. Use github:owner/repo "
                "with letters, digits, '.', '_' or '-' only."
            ),
        )

    # Reconstruct template ID without branch for lookup
    template_lookup = f"github:{template_str}" if url_branch else config.template
    return template_lookup, template_str, url_branch


def _read_source_template(
    repo: str, pat: Optional[str], ref: Optional[str]
) -> tuple:
    """The ONE creation-time read of a `github:` template's `template.yaml`.
    Returns `(metadata, reason)`.

    Deliberately NOT the catalog's `gh_template` (trinity-enterprise#89 R2):
    that dict comes from `_get_cached_metadata`, which reads with the **global
    platform** PAT off the **default branch** through a 10-minute per-process
    cache. Creation resolves its PAT completely differently (per-agent ->
    per-user -> global, ent#162), so a user creating from their own private repo
    with their own token would clone successfully and then read zero
    declarations with no signal at all — the exact silent-ignore class this
    feature exists to close, reintroduced one layer up.

    ent#89 wrote that warning about `schedules:` and then the ent#14 fork gate
    was built on the catalog dict anyway, one line below the call site that had
    already read the file correctly (trinity-enterprise#14 S2). So this returns
    the whole `(metadata, reason)` pair and BOTH consumers — the schedules
    normalizer and the `fork_to_own` gate — read it. One GitHub call, one set of
    credentials, one ref, and no second source that can disagree.

    Non-fatal by construction for schedules (the normalizer is total, and any
    failure yields `{}` plus a WARNING naming the repo, the ref and the reason).
    The `reason` is what makes the gate's fail-closed decision possible.
    """
    return fetch_template_metadata_result_for_create(repo, pat=pat, ref=ref)


def _declared_schedules_for_snapshot(snapshot) -> list:
    """Normalized `schedules:` for a copy-intent agent, read from the STAGED
    tree (trinity-enterprise#15) — the snapshot's exact content is the truth,
    so no API re-fetch (which could see a different ref). Non-fatal by
    construction, mirroring `_declared_schedules_for_github`."""
    template_yaml = Path(snapshot.staging_dir) / "template.yaml"
    if not template_yaml.is_file():
        return []
    try:
        metadata = load_template_yaml(template_yaml.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict):
            return []
        return normalize_declared_schedules(metadata.get("schedules"))
    except Exception as e:  # noqa: BLE001 — schedules are advisory, never fatal
        logger.warning(
            "snapshot-import: could not read template.yaml schedules for %s: %s",
            snapshot.source_repo, e,
        )
        return []


def _declared_plugins_for_snapshot(snapshot) -> dict:
    """Normalized `plugins:` for a copy-intent agent, read from the STAGED tree
    (#1704) — twin of `_declared_schedules_for_snapshot`. Non-fatal: plugins are
    advisory, so any read failure yields the opt-in empty declaration."""
    template_yaml = Path(snapshot.staging_dir) / "template.yaml"
    if not template_yaml.is_file():
        return {}
    try:
        metadata = load_template_yaml(template_yaml.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict):
            return {}
        return normalize_declared_plugins(metadata.get("plugins"))
    except Exception as e:  # noqa: BLE001 — plugins are advisory, never fatal
        logger.warning(
            "snapshot-import: could not read template.yaml plugins for %s: %s",
            snapshot.source_repo, e,
        )
        return {}


def _gate_tokenless_request(
    config: AgentConfig, github_pat: str
) -> Optional[str]:
    """ent#123: admit or reject a github-template create with no PAT.

    ``resolve_github_pat`` returns an EMPTY STRING (not None) when no tier
    has a token — normalize to None so every downstream consumer can rely
    on truthiness. A tokenless request is allowed only in source mode
    (pull-only): working-branch mode pushes a new branch at container boot,
    which is impossible anonymously. ``source_mode`` is Optional[bool], so
    the falsy check deliberately catches an explicit None too. Fork-to-own
    passes through — the user's own PAT becomes the write identity later.
    The public-vs-private decision is NOT made here (this helper is sync);
    it happens in ``_validate_github_access`` via the anonymous ls-remote
    probe.
    """
    if github_pat:
        return github_pat
    if not config.fork_to_own and not config.source_mode:
        raise HTTPException(
            status_code=400,
            detail=(
                "Bidirectional git sync requires write credentials — add "
                "your GitHub token in Settings (or ask an admin to configure "
                "the platform token), or create the agent in source mode "
                "(pull-only)."
            ),
        )
    return None


def _resolve_github_repo_and_pat(
    config: AgentConfig, current_user: User, template_lookup: str, repo_path: str
) -> tuple[Optional[dict], str, Optional[str], str]:
    """Resolve `(gh_template, github_repo, github_pat, github_pat_tier)` for a
    github template. Prefers the predefined catalog entry; otherwise treats the
    ref as a dynamic `owner/repo`. Mutates `config.resources`/`config.mcp_servers`
    for a predefined template."""
    gh_template = get_github_template(template_lookup)

    # ent#162: the PAT resolver prefers THIS creator's personal token
    # over the shared admin PAT. `current_user.id` is the owner user id
    # (agent-scoped keys resolve to their owner), so resolution keys on
    # ownership only — never a calling/sharing principal. `github_pat_tier`
    # records which tier supplied the token, so the persist site below
    # writes a per-agent PAT only for a deliberate identity (per-user /
    # fork), never the global fallback (see Decision 2 in the resolver
    # docstring). NOTE: agent creation requires role creator+ (ROLE-001);
    # an invited user seeded as `user` (#314) cannot reach this path until
    # promoted — a per-user PAT does not itself grant creation rights.
    creator_user_id = current_user.id

    if gh_template:
        # Pre-defined GitHub template from config.py
        github_repo = gh_template["github_repo"]

        # Resolve the GitHub PAT: per-agent → this owner's per-user
        # (live) → global (ent#162). Prefers the creator's own token so a
        # non-admin is not confined to the admin PAT's repo scope.
        # Fork-to-own (#93) doesn't need it — the user's PAT is the
        # write identity and public templates clone unauthenticated.
        github_pat, github_pat_tier = resolve_github_pat(owner_id=creator_user_id)
        github_pat = _gate_tokenless_request(config, github_pat)

        config.resources = gh_template.get("resources", config.resources)
        config.mcp_servers = gh_template.get("mcp_servers", config.mcp_servers)
        return gh_template, github_repo, github_pat, github_pat_tier

    # Dynamic GitHub template - use any github:owner/repo[@branch] format
    # Note: Branch was already parsed above; repo_path already has branch removed
    if "/" not in repo_path:
        raise HTTPException(
            status_code=400,
            detail="Invalid GitHub template format. Use: github:owner/repo or github:owner/repo@branch"
        )

    # Resolve the GitHub PAT: per-agent → this owner's per-user
    # (live) → global (ent#162). Prefers the creator's own token so a
    # non-admin can clone a private repo the admin PAT can't see.
    github_pat, github_pat_tier = resolve_github_pat(owner_id=creator_user_id)
    github_pat = _gate_tokenless_request(config, github_pat)

    logger.info(f"Using dynamic GitHub template: {repo_path} (branch: {config.source_branch})")
    return None, repo_path, github_pat, github_pat_tier


async def _apply_fork_to_own(
    config: AgentConfig,
    current_user: User,
    gh_template: Optional[dict],
    github_repo_for_agent: str,
    github_pat_for_agent: Optional[str],
    github_pat_tier: str,
    url_branch: Optional[str],
    *,
    source_metadata: dict,
    source_metadata_reason: Optional[str],
) -> tuple[str, Optional[str], str, Optional[str]]:
    """trinity-enterprise#93: enforce a `fork_to_own: required` template and, when
    the caller forks, copy the template into a user-owned repo and return the
    updated `(repo, pat, tier, fork_upstream_repo)`. Runs BEFORE the docker
    try-block so the structured FORK_* errors reach the UI. When the caller is
    NOT forking the inputs pass through unchanged (tier untouched)."""
    # trinity-enterprise#14 F2 — an unreadable template.yaml is UNKNOWN, not absent.
    #
    # The chain this closes, every link verified in source:
    #   _fetch_template_yaml_result()  -> ({}, "HTTP 403") on a rate limit
    #   _build_template()              -> "fork_to_own": None
    #   the test below                 -> `None == "required"` is False
    #   => the gate never fires and the agent is created bound to the SHARED
    #      UPSTREAM TEMPLATE REPO instead of a user-owned copy.
    #
    # `fork_to_own: required` exists precisely to stop that, and the failure is
    # silent: the user's knowledge base ends up in the wrong place with no error.
    # It is the ent#162 class ("a private KB could reach the shared public
    # upstream") reached without any attacker.
    #
    # Pre-existing, but the remote template registry converts it from
    # unreachable to EXPECTED: it re-introduces per-repo GitHub metadata fetches
    # on a DEFAULT install (#1931 had driven them to zero), ships default-on, and
    # its own arithmetic — workers x windows/hr x entries — exceeds GitHub's
    # 60/hr ANONYMOUS limit above ~5 listed repos, while the curated fleet is
    # very likely to include the fork_to_own template (Cornelius).
    #
    # WHICH READ DECIDES (trinity-enterprise#14 S2). Both inputs below come from
    # `_read_source_template` — the creation-path read — and NOT from
    # `gh_template`, whose `metadata_unavailable`/`fork_to_own` are computed from
    # `_get_cached_metadata_result`: the global platform PAT, off the default
    # branch, through a 600s cache. Reading the catalog here failed in both
    # directions at once:
    #
    #   FALSE PASS. GitHub answers 404 — not 403 — for a repo a token cannot
    #   see. So a PRIVATE `fork_to_own: required` template that only the
    #   creator's per-user PAT can read (ent#162, a supported flow) came back
    #   `("HTTP 404" -> absent, fork_to_own=None)`, the gate passed, and the
    #   agent bound to the shared upstream. The precise outcome this gate
    #   exists to prevent, with no attacker involved. Note that fixing only
    #   the availability half would NOT have closed it: the `fork_to_own`
    #   VALUE has to come from the correctly-credentialed read too, which is
    #   why `source_metadata` is threaded in beside the reason.
    #
    #   FALSE REFUSE. A creator whose own PAT reads the template fine was 503'd
    #   because the SHARED cache entry said 403 — for the full 600s TTL, on
    #   every non-forking `github:` create, including the plain
    #   `github:owner/repo` escape hatch.
    #
    # Costs zero extra GitHub calls: `_resolve_template` already made this read
    # one call earlier, for ent#89's schedules, with the PAT that will clone.
    # That last part is the structural argument — the read that DECIDES is now
    # made with the same credentials as the clone that follows, so "we could not
    # see it" and "the agent could not have cloned it" can no longer disagree.
    #
    # Scoped to the branch that is actually unsafe. A caller who IS forking ends
    # up with a user-owned repo whatever the template declares, so an outage must
    # not block them; only the non-forking path has to treat unknown as refuse.
    # A clean HTTP 404 stays "absent" (`metadata_reason_is_unreadable`), so a repo
    # that genuinely ships no template.yaml creates exactly as it always has —
    # and on this path a 404 the creator's own token cannot get past is caught
    # loudly downstream by `_validate_github_access` anyway.
    #
    # The trade is deliberate: creation now depends on GitHub API reachability
    # where it previously depended only on `git clone`. A loud, retryable refusal
    # beats a silent wrong-repo binding — the learnings.md 2026-07-15
    # direction-of-failure rule, applied to a gate instead of a retention window.
    if metadata_reason_is_unreadable(source_metadata_reason) and not config.fork_to_own:
        raise HTTPException(
            status_code=503,
            detail={
                "error": (
                    f"Could not read '{config.template}' template metadata from "
                    f"GitHub, so Trinity cannot tell whether this template must "
                    f"be copied into a repo you own. Refusing rather than "
                    f"guessing. This is usually a transient GitHub rate limit — "
                    f"retry shortly, or configure a platform GitHub token in "
                    f"Settings to raise the limit from 60 to 5000 requests/hour."
                ),
                "code": "TEMPLATE_METADATA_UNAVAILABLE",
            },
        )

    # The union, not a precedence: `required` from EITHER read wins. The
    # creation read is the better one (right credentials, pinned ref, no cache),
    # but taking it alone would mean a repo whose default branch declares
    # `required` could be created from an `@branch` that drops the line. Both
    # sources feed one boolean, so this change can only ever REMOVE a false pass
    # relative to the catalog-only gate, never add one.
    declared_fork_modes = {
        (source_metadata or {}).get("fork_to_own"),
        (gh_template or {}).get("fork_to_own"),
    }
    if "required" in declared_fork_modes and not config.fork_to_own:
        raise HTTPException(
            status_code=400,
            detail={
                "error": (
                    f"Template '{config.template}' requires fork-to-own "
                    f"creation: provide fork_to_own.destination_repo and "
                    f"fork_to_own.github_pat so the agent's repo is your "
                    f"own, not the shared template."
                ),
                "code": "FORK_TO_OWN_REQUIRED",
            },
        )
    if not config.fork_to_own:
        return github_repo_for_agent, github_pat_for_agent, github_pat_tier, None
    if url_branch:
        raise HTTPException(
            status_code=400,
            detail={
                "error": (
                    "fork_to_own copies the template's default branch; "
                    "the github:owner/repo@branch form is not supported "
                    "with it."
                ),
                "code": "FORK_BRANCH_UNSUPPORTED",
            },
        )
    destination = config.fork_to_own.destination_repo
    # Source-mode rows bypass the UNIQUE(repo, branch) index, so
    # this is the guard against two agents auto-pushing the same
    # destination main. (Re-checked after reservation below — this
    # pre-check is check-then-act with the whole copy in between.)
    bound_agents = db.get_git_config_agent_names_for_repo(destination)
    if bound_agents:
        raise HTTPException(
            status_code=409,
            detail={
                "error": _fork_destination_in_use_message(
                    destination, bound_agents[0], current_user.username
                ),
                "code": "FORK_DESTINATION_IN_USE",
            },
        )
    # Unwrap the SecretStr exactly once; plain str flows inward
    # (docker env, GitHubService header, push auth).
    user_pat = config.fork_to_own.github_pat.get_secret_value()
    fork_result = await fork_template_to_own_repo(
        template_repo=github_repo_for_agent,
        destination_repo=destination,
        user_pat=user_pat,
        read_pat=github_pat_for_agent or "",
        private=config.fork_to_own.private,
    )
    fork_upstream_repo = github_repo_for_agent
    github_repo_for_agent = fork_result.destination_repo
    github_pat_for_agent = user_pat
    github_pat_tier = "fork"  # ent#162: a deliberate per-agent identity → persist
    # Pinned semantics: the user's default branch IS the brain —
    # origin main holds captures; auto-sync pushes there.
    config.source_branch = fork_result.default_branch
    config.source_mode = True
    return github_repo_for_agent, github_pat_for_agent, github_pat_tier, fork_upstream_repo


async def _validate_github_access(
    config: AgentConfig, github_repo_for_agent: str, github_pat_for_agent: Optional[str]
) -> None:
    """#218: validate PAT access to the repo (and branch) before container create,
    so a bad token fails loud here instead of silently in startup.sh. Transient
    network errors are logged and NOT fatal (matches the monolith).

    ent#123 tokenless path: no PAT ⇒ probe over the git transport instead of
    REST (`probe_anonymous_repo_access` — same transport as the container's
    anonymous clone, immune to the anonymous REST rate cap). Unlike the
    PAT-ful path this is FAIL-CLOSED on transient errors: if the probe can't
    reach GitHub the clone would fail too, and with monitoring default-off
    (#1121) a fail-open would produce a silently empty agent.
    """
    if not github_pat_for_agent:
        outcome = await git_service.probe_anonymous_repo_access(
            github_repo_for_agent
        )
        if outcome == "unavailable":
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Repository '{github_repo_for_agent}' was not found or is "
                    f"private. If it is private, add your GitHub token in "
                    f"Settings or ask an admin to configure the platform token."
                ),
            )
        if outcome != "ok":
            raise HTTPException(
                status_code=502,
                detail=(
                    "GitHub is unreachable — could not verify anonymous access "
                    f"to '{github_repo_for_agent}'. Retry shortly, or add a "
                    f"GitHub token."
                ),
            )
        # Repo reachable anonymously. Also verify the source branch exists —
        # source-mode clones `-b <branch>`, and a missing branch would fail
        # the clone with the same silent-empty-agent risk (the credential-less
        # ls-remote helper answers for public repos).
        if config.source_branch:
            branch_ok = await git_service.check_remote_branch_exists(
                github_repo_for_agent, config.source_branch
            )
            if not branch_ok:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Branch '{config.source_branch}' not found in public "
                        f"repository '{github_repo_for_agent}'. Pass the "
                        f"branch explicitly (github:owner/repo@branch) if the "
                        f"repository's default branch is not 'main'."
                    ),
                )
        logger.info(
            f"Validated anonymous access to public repo: {github_repo_for_agent}"
        )
        return

    try:
        gh_service = GitHubService(github_pat_for_agent)
        repo_parts = github_repo_for_agent.split("/", 1)
        if len(repo_parts) == 2:
            repo_info = await gh_service.check_repo_exists(repo_parts[0], repo_parts[1])
            if not repo_info.exists:
                raise HTTPException(
                    status_code=400,
                    detail=f"GitHub repository '{github_repo_for_agent}' not found or PAT does not have access. "
                           f"Verify the repository exists and the configured GitHub PAT has read access."
                )
            logger.info(f"Validated GitHub repo access: {github_repo_for_agent} (private={repo_info.private})")

            # If source_branch specified, validate branch exists
            if config.source_branch and config.source_branch != repo_info.default_branch:
                try:
                    branch_resp = await gh_service._request(
                        "GET", f"/repos/{github_repo_for_agent}/branches/{config.source_branch}"
                    )
                    if branch_resp.status_code == 404:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Branch '{config.source_branch}' not found in repository '{github_repo_for_agent}'. "
                                   f"Available default branch: '{repo_info.default_branch}'."
                        )
                except HTTPException:
                    raise
                except Exception as e:
                    logger.warning(f"Could not validate branch '{config.source_branch}': {e}")
    except HTTPException:
        raise
    except GitHubError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to validate GitHub repository access: {e}"
        )
    except Exception as e:
        # Log but don't block creation for transient network errors
        logger.warning(f"GitHub repo validation failed (non-blocking): {e}")


async def _reserve_git_instance(
    config: AgentConfig, current_user: User, github_repo_for_agent: str
) -> tuple[Optional[str], Optional[str]]:
    """S7 Layer 0 (#382): reserve the working branch atomically (writes the
    `agent_git_config` row that the except-block rolls back), then re-check the
    fork destination race and deterministically roll back the losing agent."""
    # Generate git sync instance ID and branch for Phase 7.
    # S7 Layer 0 (#382): reserve the working branch atomically —
    # probes the remote with `git ls-remote` and inserts the DB
    # row under the partial UNIQUE index so no two agents can end
    # up bound to the same (repo, branch). The row is written
    # here, before the container is created, so it must be rolled
    # back if anything in the rest of the flow fails (see the
    # `try: ... except: db.delete_git_config(...)` block below).
    git_instance_id, git_working_branch = (
        await git_service.reserve_and_generate_instance_id(
            agent_name=config.name,
            github_repo=github_repo_for_agent,
            source_branch=config.source_branch or "main",
            source_mode=config.source_mode,
        )
    )

    # trinity-enterprise#93: the destination-binding pre-check above is
    # check-then-act with the entire fork copy (minutes) in between,
    # and source-mode rows bypass the partial UNIQUE index — so two
    # concurrent creates to the same destination can both reach here.
    # Re-check now that our own row is inserted; losers (everyone but
    # the lexicographically-first agent name) roll back deterministically,
    # leaving exactly one winner.
    if config.fork_to_own:
        bound_now = db.get_git_config_agent_names_for_repo(github_repo_for_agent)
        if len(bound_now) > 1 and min(bound_now) != config.name:
            try:
                db.delete_git_config(config.name)
            except Exception as cleanup_exc:
                logger.warning(
                    "fork-to-own: failed to roll back git config for %s "
                    "after destination race: %s", config.name, cleanup_exc,
                )
            raise HTTPException(
                status_code=409,
                detail={
                    "error": _fork_destination_in_use_message(
                        github_repo_for_agent,
                        min(bound_now),
                        current_user.username,
                    ),
                    "code": "FORK_DESTINATION_IN_USE",
                },
            )
    return git_instance_id, git_working_branch


def _resolve_local_template_dir(raw_name: str) -> Path:
    """Curated root first, then the deploy-local store (#950) — the single
    definition of "where does `local:<raw_name>` live".

    Every candidate is `_safe_local_template_path`-validated (regex barrier +
    `is_relative_to` barrier) before any filesystem access, so the returned path
    is proven to be inside one of `_LOCAL_TEMPLATE_ROOTS`.

    Extracted in #1900 so the THREE seams that must agree cannot drift: the
    resolver, the `/template` bind decision, and the credential-file stager.
    #1759 named the first two; the third existed and re-derived the directory
    from the template's own untrusted `name:` field instead of reusing this.
    """
    candidate = _safe_local_template_path(raw_name, _LOCAL_TEMPLATE_ROOTS[0])
    if not (candidate / "template.yaml").exists():
        candidate = _safe_local_template_path(raw_name, _LOCAL_TEMPLATE_ROOTS[1])
    return candidate


def _resolve_local_template(config: AgentConfig) -> tuple[dict, Optional[dict]]:
    """Load a `local:`-prefixed template's `template.yaml` (curated catalog then
    deploy-local store, #950). Mutates `config` runtime/type/resources/tools/
    mcp_servers fields. Returns `(template_data, template_shared_folders)`.

    Raises `HTTPException(404, UNKNOWN_LOCAL_TEMPLATE)` when the name resolves
    to no `template.yaml` under either root (#1793) — this previously returned
    an empty dict and the caller provisioned a templateless container.

    Raises `HTTPException(400, LOCAL_TEMPLATE_INVALID)` when the `template.yaml`
    exists but is unreadable, unparseable, or not a YAML mapping (#1759). That
    case reached the same observable outcome as an absent template — blank
    agent, HTTP 200 — through the broad `except Exception` below, so #1793 alone
    did not close it."""
    template_data: dict = {}
    template_shared_folders = None
    # Local template - strip "local:" prefix. Look in curated catalog
    # first (/agent-configs/templates), then in deploy-local writable
    # store (/data/deployed-templates) per #950. Each candidate path
    # is validated + resolved to prove it stays under the root before
    # any filesystem access (regex barrier + is_relative_to barrier).
    raw_name = config.template[6:]
    template_path = _resolve_local_template_dir(raw_name)

    template_yaml = template_path / "template.yaml"

    # The `if/else` shape (rather than an early-return guard) is deliberate and
    # load-bearing: dedenting this block moves the `.exists()` and `open()`
    # expressions onto new lines and re-fingerprints the `py/path-injection`
    # alerts already dismissed as false positives on dev. #1793 hit exactly this
    # and reverted its own guard-clause refactor for it. Add bands INSIDE the
    # block; do not flatten it.
    if template_yaml.exists():
        try:
            with open(template_yaml) as f:
                # ent#314: hardened parse — this is the create path for a
                # template that may have come from any public repo.
                template_data = load_template_yaml(f.read())
        except (OSError, yaml.YAMLError, HardenedYamlError) as e:
            # #1759: previously swallowed by the broad `except Exception:
            # logger.warning(...)` below, which produced the *identical*
            # observable outcome as an absent template — blank agent, HTTP 200 —
            # via a different line. #1793 closed the ABSENT case; this closes the
            # present-but-unreadable one. The parser error itself is deliberately
            # NOT echoed to the caller: it quotes the resolved file path and the
            # file's bytes.
            logger.warning("Unparseable template.yaml for %s: %s", config.template, e)
            raise HTTPException(
                status_code=400,
                detail={
                    "error": (
                        f"Local template {config.template!r} has an unreadable or "
                        f"malformed template.yaml. Fix the template, or list "
                        f"working templates with GET /api/templates."
                    ),
                    "code": "LOCAL_TEMPLATE_INVALID",
                },
            ) from e

        # `yaml.safe_load("")` returns None, and a scalar/list document returns a
        # non-dict. The LISTING path already rejects both
        # (`template_service._build_local_template`), so before #1759 the create
        # path was strictly *less* strict than the surface advertising the
        # template.
        if not isinstance(template_data, dict):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": (
                        f"Local template {config.template!r} has an empty or "
                        f"malformed template.yaml (expected a YAML mapping). Fix "
                        f"the template, or list working templates with "
                        f"GET /api/templates."
                    ),
                    "code": "LOCAL_TEMPLATE_INVALID",
                },
            )

        # A malformed *field* (as opposed to a malformed file) still degrades
        # gracefully: `template_data` is a real dict here, so the agent does get
        # its template files and only some config mutations are skipped.
        # Unchanged pre-#1759 behaviour, deliberately left out of scope.
        try:
            # #2104: template.yaml `type:` stays parseable but is ignored —
            # the agent type taxonomy is retired (tags classify agents).
            config.resources = template_data.get("resources", config.resources)
            config.tools = template_data.get("tools", config.tools)
            # Read through the tolerant accessor rather than reaching straight
            # through the block. `creds.get("mcp_servers", {}).keys()` raises
            # AttributeError on a null / list / string `credentials:`, and the
            # broad `except` below then silently drops this template's `runtime:`
            # and `shared_folders:` config alongside the credential parse — one
            # malformed key costing the agent three unrelated ones.
            # (trinity-enterprise#128)
            mcp_servers = credential_mcp_server_names(template_data.get("credentials"))
            if mcp_servers:
                config.mcp_servers = mcp_servers
            # Multi-runtime support - extract runtime config from template
            runtime_config = template_data.get("runtime", {})
            if isinstance(runtime_config, dict):
                config.runtime = runtime_config.get("type", config.runtime)
                config.runtime_model = runtime_config.get("model", config.runtime_model)
            elif isinstance(runtime_config, str):
                config.runtime = runtime_config
            # Phase 9.11: Extract shared folder config from template
            shared_folders_config = template_data.get("shared_folders", {})
            if shared_folders_config:
                template_shared_folders = {
                    "expose": shared_folders_config.get("expose", False),
                    "consume": shared_folders_config.get("consume", False)
                }
        except Exception as e:
            # Still broad and still non-fatal, deliberately: the file parsed, so
            # the agent DOES get its template files and only some `config`
            # mutations are skipped. Tightening this to a 400 would reject
            # templates that deploy successfully today — beyond #1759's ACs. But
            # the mutations above run in order, so a raise part-way through
            # leaves a PARTIALLY applied template (e.g. `credentials: "a string"`
            # applies type/resources/tools, then silently skips
            # mcp_servers/runtime/shared_folders). Name the template and the
            # agent: the old message carried neither, leaving an operator nothing
            # to grep when the resulting agent is subtly wrong.
            logger.warning(
                "Template %r for agent %r: field-level config only partially "
                "applied (agent still created): %s",
                config.template,
                config.name,
                e,
            )
    else:
        # #1793: an unresolvable `local:` template must fail before any side
        # effect. Falling through with an empty `template_data` provisioned a
        # running container with no CLAUDE.md, no template.yaml and no skills —
        # an empty shell reported to the caller as a normal 200 creation. The
        # `github:` path already fails fast on an unknown repo; this matches it.
        #
        # ONE message regardless of which root missed, and no resolved path in
        # it: deploy-local templates (#950) are named after AGENT names, so a
        # root-distinguishing or path-echoing error would let a creator-role
        # caller probe whether another user's deploy-local agent exists (#186
        # enumeration discipline).
        raise HTTPException(
            status_code=404,
            detail={
                "error": (
                    f"Local template {raw_name!r} was not found. Check the id "
                    f"against GET /api/templates — note that hidden templates "
                    f"are omitted from that listing but remain creatable by id. "
                    f"To create an agent with no template at all, omit the "
                    f"'template' field."
                ),
                "code": "UNKNOWN_LOCAL_TEMPLATE",
            },
        )
    return template_data, template_shared_folders


async def _resolve_template(config: AgentConfig, current_user: User) -> _TemplateResolution:
    """Dispatch template resolution (github incl. fork | local | none) and return
    the set-once `_TemplateResolution`. The whole github phase — including the
    real fork-to-own GitHub write — stays here, BEFORE the caller's docker
    try-block, so its structured 4xx errors are not flattened to a 500."""
    tr = _TemplateResolution()

    # trinity-enterprise#93: fork-to-own only makes sense for a github:
    # template (there must be a source repo to copy). Reject early and loud.
    if config.fork_to_own and not (config.template or "").startswith("github:"):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "fork_to_own requires a 'github:owner/repo' template.",
                "code": "FORK_REQUIRES_GITHUB_TEMPLATE",
            },
        )

    # trinity-enterprise#15: import-intent intake gates, all pre-side-effect.
    if config.import_intent:
        if not (config.template or "").startswith("github:"):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": (
                        "import_intent requires a 'github:owner/repo' template "
                        "— fork/copy/clone are meaningless without a source repo."
                    ),
                    "code": "INTENT_REQUIRES_GITHUB_TEMPLATE",
                },
            )
        if config.import_intent == "fork" and not config.fork_to_own:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": (
                        "import_intent 'fork' needs the fork_to_own block "
                        "(destination_repo + github_pat) — the fork is created "
                        "in YOUR account."
                    ),
                    "code": "FORK_PARAMS_REQUIRED",
                },
            )
        if config.import_intent == "copy" and config.ephemeral:
            # Ghosts are volume-less by invariant (ent#69) and the snapshot
            # lives on the workspace volume — the combination would boot a
            # green blank ghost and strand the populated volume (review F2).
            raise HTTPException(
                status_code=400,
                detail={
                    "error": (
                        "import_intent 'copy' cannot be combined with "
                        "ephemeral — a snapshot needs the durable workspace "
                        "volume that ghost agents deliberately do not mount."
                    ),
                    "code": "COPY_EPHEMERAL_UNSUPPORTED",
                },
            )
        if config.import_intent in ("copy", "clone") and config.fork_to_own:
            # A stray fork block with an explicit non-fork intent would
            # otherwise silently create a GitHub repo (fork_to_own triggers on
            # block presence alone) — refuse the contradiction by name.
            raise HTTPException(
                status_code=400,
                detail={
                    "error": (
                        f"import_intent '{config.import_intent}' contradicts "
                        f"the fork_to_own block — remove one of them."
                    ),
                    "code": "INTENT_FORK_BLOCK_CONFLICT",
                },
            )

    # Load template configuration
    if config.template:
        # #843: reject template strings that don't start with a known
        # scheme. Pre-fix, an unprefixed name (e.g. "dd-compliance"
        # instead of "local:dd-compliance") fell through every
        # branch of the dispatch and silently produced a blank agent
        # — same return code as success, no log warning, the operator
        # only noticed when the agent had no template.yaml. Reject
        # explicitly so the failure is loud.
        if not (
            config.template.startswith("github:")
            or config.template.startswith("local:")
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Template '{config.template}' must start with "
                    f"'local:' (for templates under config/agent-templates/) "
                    f"or 'github:' (for GitHub-hosted templates). "
                    f"Example: 'local:{config.template}' or 'github:owner/repo'."
                ),
            )
        if config.template.startswith("github:"):
            template_lookup, repo_path, url_branch = _parse_github_ref(config)

            if config.import_intent == "copy":
                # trinity-enterprise#15: backend-materialized snapshot. The
                # staged clone IS the reachability check (same transport class
                # as ent#123's probe); the tokenless source-mode gate is
                # deliberately NOT run — that 400 protects boot-time push, and
                # copy never pushes. `github_repo_for_agent` stays None so the
                # container gets no GitHub env, no git-config row is reserved,
                # and the PAT-persist site below never fires.
                gh_template = get_github_template(template_lookup)
                if (gh_template or {}).get("fork_to_own") == "required":
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "error": (
                                "This template requires fork-to-own creation "
                                "— use import_intent 'fork' with a "
                                "fork_to_own block."
                            ),
                            "code": "FORK_TO_OWN_REQUIRED",
                        },
                    )
                if gh_template:
                    # Catalog parity with `_resolve_github_repo_and_pat`: the
                    # catalog id maps to its declared repo, and the template's
                    # resource/MCP defaults still apply to the snapshot agent.
                    copy_repo = gh_template["github_repo"]
                    config.resources = gh_template.get("resources", config.resources)
                    config.mcp_servers = gh_template.get("mcp_servers", config.mcp_servers)
                else:
                    if "/" not in repo_path:
                        raise HTTPException(
                            status_code=400,
                            detail="Invalid GitHub template format. Use: github:owner/repo or github:owner/repo@branch",
                        )
                    copy_repo = repo_path
                copy_pat, _copy_tier = resolve_github_pat(
                    owner_id=current_user.id
                )
                # GIT-002 parity with startup.sh (`-b` only when ≠ "main"):
                # the default "main" means "the repo's default branch".
                copy_branch = (
                    config.source_branch
                    if config.source_branch and config.source_branch != "main"
                    else None
                )
                tr.copy_snapshot = await snapshot_import.stage_github_snapshot(
                    copy_repo,
                    copy_branch,
                    copy_pat or None,
                )
                tr.declared_schedules = _declared_schedules_for_snapshot(
                    tr.copy_snapshot
                )
                tr.declared_plugins = _declared_plugins_for_snapshot(
                    tr.copy_snapshot
                )
                return tr

            (
                gh_template,
                tr.github_repo_for_agent,
                tr.github_pat_for_agent,
                tr.github_pat_tier,
            ) = _resolve_github_repo_and_pat(
                config, current_user, template_lookup, repo_path
            )
            # Read the SOURCE template's declarations here, before
            # `_apply_fork_to_own` swaps in the user's fork + PAT: this pairing
            # (source repo, PAT resolved to read it) is the one that can
            # actually see a private template. ONE read feeds both consumers —
            # ent#89's schedules and ent#14's fork gate (which used to decide
            # from the catalog cache instead; see `_apply_fork_to_own`).
            source_metadata, source_metadata_reason = _read_source_template(
                tr.github_repo_for_agent, tr.github_pat_for_agent, url_branch
            )
            tr.declared_schedules = normalize_declared_schedules(
                source_metadata.get("schedules")
            )
            tr.declared_plugins = normalize_declared_plugins(
                source_metadata.get("plugins")
            )
            (
                tr.github_repo_for_agent,
                tr.github_pat_for_agent,
                tr.github_pat_tier,
                tr.fork_upstream_repo,
            ) = await _apply_fork_to_own(
                config,
                current_user,
                gh_template,
                tr.github_repo_for_agent,
                tr.github_pat_for_agent,
                tr.github_pat_tier,
                url_branch,
                source_metadata=source_metadata,
                source_metadata_reason=source_metadata_reason,
            )
            # Validate PAT has access to the repository before creating container
            # This prevents silent clone failures in startup.sh (#218)
            await _validate_github_access(
                config, tr.github_repo_for_agent, tr.github_pat_for_agent
            )
            tr.git_instance_id, tr.git_working_branch = await _reserve_git_instance(
                config, current_user, tr.github_repo_for_agent
            )
        elif config.template.startswith("local:"):
            tr.template_data, tr.template_shared_folders = _resolve_local_template(config)
            # Same normalizer as the `github:` branch above — symmetric by
            # construction, so neither source can quietly diverge.
            tr.declared_schedules = normalize_declared_schedules(
                tr.template_data.get("schedules")
            )
            tr.declared_plugins = normalize_declared_plugins(
                tr.template_data.get("plugins")
            )
    return tr


def _stage_config_files(
    config: AgentConfig, template_data: dict,
    github_template_path: Optional[Union[Path, str]]
) -> tuple[Path, Path, Optional[dict], Optional[dict]]:
    """CRED-002: write the agent-config.yaml + empty credentials.json + template
    cred files under /tmp and compute the template/cred bind specs. Also
    normalizes + writes back the resource fields (#1197) so container labels +
    limits use canonical values. Returns
    `(config_path, credentials_path, template_volume, cred_files_volume)`."""
    # #1900: hand `generate_credential_files` the directory we ALREADY validated
    # rather than letting it re-derive one from the template's untrusted `name:`
    # field (a traversal into any readable `.mcp.json`). This is the same
    # `_safe_local_template_path`-validated ladder the `/template` bind decision
    # below uses, so all three seams agree by construction (#1759).
    local_template_base = None
    if config.template and config.template.startswith("local:"):
        # Cannot newly raise: `_resolve_local_template` already ran this exact
        # `raw_name` through the same barrier before `template_data` could be
        # non-empty, and a templateless agent has a falsy `config.template`.
        local_template_base = _resolve_local_template_dir(config.template[6:])

    generated_files = {}
    if template_data:
        # Generate empty credential files structure from template
        try:
            generated_files = generate_credential_files(
                template_data, {}, config.name,
                # `github_template_path` first so the field's eventual revival
                # wins; it has zero writers today (#1900 follow-up).
                template_base_path=github_template_path or local_template_base
            )
        except CredentialDeclarationError as e:
            # Invariant #1: the service raises an HTTP-free domain error and
            # this, its only caller, maps it 1:1. A named 400 beats both of the
            # alternatives it replaces — an uncaught TypeError (500) and a
            # silently corrupt `.env`. (trinity-enterprise#128)
            raise HTTPException(
                status_code=400,
                detail={
                    "error": str(e),
                    "code": "INVALID_CREDENTIAL_DECLARATION",
                },
            )

    cred_files_dir = Path(f"/tmp/agent-{config.name}-creds")
    cred_files_dir.mkdir(exist_ok=True)

    # Write template-generated files (.env, .mcp.json, etc.)
    for filepath, content in generated_files.items():
        file_path = _safe_cred_file_path(filepath, cred_files_dir)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, "w") as f:
            f.write(content)

    agent_config = {
        "agent": {
            "base_image": config.base_image,
            "resources": config.resources,
            "tools": config.tools,
            "mcp_servers": config.mcp_servers,
            "custom_instructions": config.custom_instructions,
            "credentials": {}  # CRED-002: Credentials injected after creation
        }
    }

    config_path = Path(f"/tmp/agent-{config.name}.yaml")
    with open(config_path, "w") as f:
        yaml.dump(agent_config, f)

    credentials_path = Path(f"/tmp/agent-{config.name}-credentials.json")
    with open(credentials_path, "w") as f:
        json.dump({}, f)  # CRED-002: Empty credentials, injected after creation

    template_volume = None
    cred_files_volume = None
    if config.template:
        if config.template.startswith("github:"):
            pass  # Agent clones at startup
        elif config.template.startswith("local:"):
            # Local template - strip "local:" prefix for path resolution.
            # Curated templates (under /agent-configs/templates) bind their
            # host path to /template; the agent's startup.sh copies it to
            # /home/developer on first boot. Deploy-local templates (under
            # /data/deployed-templates) do NOT bind here — deploy.py has
            # already pre-populated the agent's workspace volume directly
            # via put_archive (#950). The bind-mount transport relied on
            # backend's /data and the agent's host bind resolving to the
            # same host path, which was true in prod compose (host bind)
            # but not in dev compose (named volume).
            raw_name = config.template[6:]
            curated_path = _safe_local_template_path(
                raw_name, _LOCAL_TEMPLATE_ROOTS[0]
            )
            if curated_path.exists():
                # `or`, NOT `os.getenv(key, default)` (#1759): an EMPTY
                # HOST_TEMPLATES_PATH made `Path("") / name` collapse to the
                # bare name, which Docker reads as a NAMED VOLUME — an empty
                # one mounted at /template, i.e. a silently blank template.
                # That is this issue's bug class, one seam over.
                host_templates_base = (
                    os.getenv("HOST_TEMPLATES_PATH") or _default_host_templates_base()
                )
                # raw_name already validated by _safe_local_template_path; the
                # join here is on a value that survived the regex + resolve
                # barriers above, so the bind source can't traverse out.
                host_template_path = Path(host_templates_base) / curated_path.name
                template_volume = {str(host_template_path): {'bind': '/template', 'mode': 'ro'}}

        if generated_files:
            cred_files_volume = {str(cred_files_dir): {'bind': '/generated-creds', 'mode': 'ro'}}

    # #1197: validate/normalize template resource fields against the allowed
    # set BEFORE any side effects (MCP key, subscription, container). A
    # Kubernetes-style `cpu: "0.5"` / `memory: "512Mi"` from a source repo's
    # template.yaml used to reach the raw `int(cpu)` at container-create and
    # abort with an opaque ValueError — leaving an orphaned mcp_api_keys row.
    # Fail fast here with an actionable 400 instead, and write the canonical
    # values back so the container labels + limits use them.
    if config.resources is None:
        config.resources = {}
    try:
        config.resources['cpu'] = normalize_cpu(
            config.resources.get('cpu'), _get_default_resource('cpu')
        )
        config.resources['memory'] = normalize_memory(
            config.resources.get('memory'), _get_default_resource('memory')
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return config_path, credentials_path, template_volume, cred_files_volume


def _build_base_env(config: AgentConfig) -> dict:
    """Base container env (name/type/creds/runtime/#1098 TMPDIR) plus the #1369
    stall-watchdog ceiling and GUARD-001 guardrails overrides."""
    env_vars = {
        'AGENT_NAME': config.name,
        'CREDENTIALS_FILE': '/config/credentials.json',
        'ANTHROPIC_API_KEY': get_anthropic_api_key(),
        'ENABLE_SSH': 'true',
        'ENABLE_AGENT_UI': 'true',
        'AGENT_SERVER_PORT': '8000',
        'TEMPLATE_NAME': config.template if config.template else '',
        # Multi-runtime support
        'AGENT_RUNTIME': config.runtime or 'claude-code',
        'AGENT_RUNTIME_MODEL': config.runtime_model or '',
        # #1098: redirect scratch (pip/npm/build, ML wheels) off the 100 MB
        # noexec /tmp tmpfs onto the disk-backed, exec-capable home volume.
        # The dir is created at container start by startup.sh.
        'TMPDIR': AGENT_DEFAULT_TMPDIR,
    }

    # #1369: operator-configurable headless per-tool stall-watchdog ceiling.
    # Only propagate when the backend env sets it — an unset value leaves the
    # agent-side default (1800s). Baked at create like AGENT_TMP_SIZE; existing
    # agents pick up a change on their next recreate (not a plain restart).
    _stall_limit = (os.getenv('AGENT_TOOL_STALL_LIMIT_S') or '').strip()
    if _stall_limit:
        env_vars['AGENT_TOOL_STALL_LIMIT_S'] = _stall_limit

    # #2127: operator-configurable headless early-finalize idle ceiling. Same
    # propagation idiom as the stall limit above — unset leaves the agent-side
    # default (300s). This is the documented escape hatch for the known bound
    # that agents with an execution timeout under the ceiling lose #970's early
    # finalize, so it has to actually be settable end to end.
    _idle_finalize = (os.getenv('AGENT_IDLE_FINALIZE_S') or '').strip()
    if _idle_finalize:
        env_vars['AGENT_IDLE_FINALIZE_S'] = _idle_finalize

    # GUARD-001: per-agent guardrails overrides (empty by default; baseline
    # is always applied inside the container).
    _guardrails = db.get_guardrails_config(config.name)
    if _guardrails:
        import json as _json
        env_vars['AGENT_GUARDRAILS'] = _json.dumps(_guardrails)

    return env_vars


def _apply_subscription_env(config: AgentConfig, env_vars: dict) -> Optional[str]:
    """#74: auto-assign a Claude subscription (Claude runtimes only) — since
    #2409 the one with the most headroom, load-balance order among unranked.
    Sets `CLAUDE_CODE_OAUTH_TOKEN` and pops `ANTHROPIC_API_KEY` on success.
    Returns the assigned subscription id (None when skipped)."""
    # Auto-assign subscription — #74 (headroom-ranked since #2409).
    # Subscriptions are Claude-OAuth tokens (CLAUDE_CODE_OAUTH_TOKEN) and apply
    # ONLY to the Claude Code runtime. Non-Claude runtimes (Gemini, Codex) bring
    # their own credentials via .env (CRED-002), so skip the assign entirely —
    # otherwise a Codex agent would get a persisted subscription_id
    # (has_subscription=True) and a spurious Claude token injected on every
    # create/recreate (#1187 decision 7).
    auto_assigned_subscription_id = None
    if is_claude_runtime(config.runtime):
        try:
            # #2409: filter (db) → rank by cached headroom → first decryptable
            # token, in the subscription service. Lazy import — this module's
            # creation harnesses stub `services.*` selectively at load.
            from services.subscription_service import select_subscription_for_new_agent
            least_used = select_subscription_for_new_agent()
            if least_used:
                token = db.get_subscription_token(least_used.id)
                if token:
                    env_vars['CLAUDE_CODE_OAUTH_TOKEN'] = token
                    env_vars.pop('ANTHROPIC_API_KEY', None)
                    auto_assigned_subscription_id = least_used.id
                    logger.info(f"Auto-assigned subscription '{least_used.name}' to agent {config.name}")
                else:
                    logger.warning(f"Failed to decrypt subscription '{least_used.name}' token, using platform API key")
        except Exception as e:
            logger.warning(f"Subscription auto-assign failed for {config.name}: {e}")
    else:
        logger.info(
            f"Skipping subscription auto-assign for agent {config.name} "
            f"(runtime={(config.runtime or 'claude-code')!r} is non-Claude — uses its own .env credentials)"
        )
    return auto_assigned_subscription_id


def _apply_gemini_and_otel_env(config: AgentConfig, env_vars: dict) -> None:
    """Inject GEMINI_API_KEY for Gemini runtimes and the (default-on) Claude Code
    OpenTelemetry export vars."""
    # Add Google API key if using Gemini runtime
    # Gemini CLI expects GEMINI_API_KEY environment variable
    if config.runtime == 'gemini-cli' or config.runtime == 'gemini':
        google_api_key = os.getenv('GOOGLE_API_KEY', '')
        if google_api_key:
            env_vars['GEMINI_API_KEY'] = google_api_key  # Gemini CLI expects this name
        else:
            logger.warning("Gemini runtime selected but GOOGLE_API_KEY not configured")

    # OpenTelemetry Configuration (enabled by default)
    # Claude Code has built-in OTel support - these vars enable metrics export
    if os.getenv('OTEL_ENABLED', '1') == '1':
        env_vars['CLAUDE_CODE_ENABLE_TELEMETRY'] = '1'
        env_vars['OTEL_METRICS_EXPORTER'] = os.getenv('OTEL_METRICS_EXPORTER', 'otlp')
        env_vars['OTEL_LOGS_EXPORTER'] = os.getenv('OTEL_LOGS_EXPORTER', 'otlp')
        env_vars['OTEL_EXPORTER_OTLP_PROTOCOL'] = os.getenv('OTEL_EXPORTER_OTLP_PROTOCOL', 'grpc')
        env_vars['OTEL_EXPORTER_OTLP_ENDPOINT'] = os.getenv('OTEL_COLLECTOR_ENDPOINT', 'http://trinity-otel-collector:4317')
        env_vars['OTEL_METRIC_EXPORT_INTERVAL'] = os.getenv('OTEL_METRIC_EXPORT_INTERVAL', '60000')


def _apply_mcp_and_auth_env(
    config: AgentConfig, env_vars: dict, agent_mcp_key, trinity_mcp_url: str
) -> None:
    """Inject the Trinity MCP creds + heartbeat backend URL (#307, gated on the
    MCP key) and the unconditional per-agent in-container auth token (#1159)."""
    # Phase: Agent-to-Agent Collaboration - Inject Trinity MCP credentials
    if agent_mcp_key:
        env_vars['TRINITY_MCP_URL'] = trinity_mcp_url
        env_vars['TRINITY_MCP_API_KEY'] = agent_mcp_key.api_key
        # RELIABILITY-004 / #307: backend base URL for the liveness heartbeat
        # loop. The agent authenticates the beat with the MCP key injected
        # above (Option B — no master internal secret in agents); the agent
        # heartbeat is gated on both this URL and the MCP key being present.
        env_vars['TRINITY_BACKEND_URL'] = os.getenv('TRINITY_BACKEND_URL', 'http://backend:8000')

    # #1159: per-agent in-container auth token. Derived from the stable master
    # (AGENT_AUTH_SECRET); the agent middleware verifies it on every inbound
    # call. Unconditional (NOT gated on the MCP key) so even MCP-less agents are
    # protected; recomputed identically on recreate (lifecycle.py) and checked
    # by check_agent_auth_token_env_matches so a rename re-derives under the new
    # name. Raises if AGENT_AUTH_SECRET is unset — fail-closed, never tokenless.
    env_vars['TRINITY_AGENT_AUTH_TOKEN'] = derive_agent_token(config.name)


def _apply_github_env(
    config: AgentConfig,
    env_vars: dict,
    github_repo_for_agent: Optional[str],
    github_pat_for_agent: Optional[str],
    fork_upstream_repo: Optional[str],
    git_working_branch: Optional[str],
) -> None:
    """Bake the GitHub sync env (#1574/#93/#389) for a GitHub-native agent —
    repo/PAT/gh-CLI tokens, upstream remote, auto-sync heartbeat flag, and
    source-vs-working-branch mode. ent#123: a tokenless agent (anonymous
    public-template clone) gets repo + sync flags but NO token vars."""
    if github_repo_for_agent:
        env_vars['GITHUB_REPO'] = github_repo_for_agent
        if github_pat_for_agent:
            env_vars['GITHUB_PAT'] = github_pat_for_agent
            # #1574: the SAME managed token also authenticates the `gh` CLI
            # and the REST API (which read GH_TOKEN/GITHUB_TOKEN), not just
            # git. Gated identically to GITHUB_PAT — never set for a
            # tokenless agent.
            env_vars['GH_TOKEN'] = github_pat_for_agent
            env_vars['GITHUB_TOKEN'] = github_pat_for_agent
        # Phase 7: Enable git sync for GitHub-native agents (tokenless
        # included — the .git dir is what makes pull-only updates work)
        env_vars['GIT_SYNC_ENABLED'] = 'true'
        # Dev/self-host: propagate optional git base-URL override to agent container
        _git_base = os.getenv('TRINITY_GIT_BASE_URL')
        if _git_base:
            env_vars['TRINITY_GIT_BASE_URL'] = _git_base

        # trinity-enterprise#93: startup.sh adds a credential-less `upstream`
        # remote so `git pull upstream <branch>` adopts template updates.
        if fork_upstream_repo:
            env_vars['GIT_UPSTREAM_REPO'] = fork_upstream_repo

        # #389 S1a: 15-min auto-sync heartbeat. Only legacy (working-branch)
        # agents get it — source-mode agents track main read-only, and
        # auto-pushing to main would clobber protected branches. Operators
        # can toggle per-agent via PUT /api/agents/{name}/git/auto-sync.
        # Exception (#93): fork-to-own agents own their repo — auto-pushing
        # captures to their own main is the point.
        # ent#123: `and github_pat_for_agent` is a belt — tokenless is
        # provably source-mode+non-fork today, but auto-push must never
        # engage without credentials if that restriction is ever relaxed.
        # #2069: this exact condition is the single owner
        # `git_service._git_auto_sync_baked`, shared with the creation-time
        # `.gitignore` merge spawn so the merge covers EXACTLY the population
        # whose in-container auto-sync loop commits (ephemeral ghosts included).
        if git_service._git_auto_sync_baked(
            config, github_repo_for_agent, github_pat_for_agent, fork_upstream_repo
        ):
            env_vars['GIT_SYNC_AUTO'] = 'true'

        # Source mode (default): Track source branch directly for pull-only sync
        # Legacy mode: Create a unique working branch for bidirectional sync
        if config.source_mode:
            env_vars['GIT_SOURCE_MODE'] = 'true'
            env_vars['GIT_SOURCE_BRANCH'] = config.source_branch or 'main'
            logger.info(
                f"GitHub template env vars set for {config.name}: "
                f"repo={github_repo_for_agent}, branch={config.source_branch or 'main'}, "
                f"source_mode=true, sync=true"
            )
        else:
            env_vars['GIT_WORKING_BRANCH'] = git_working_branch
            logger.info(
                f"GitHub template env vars set for {config.name}: "
                f"repo={github_repo_for_agent}, working_branch={git_working_branch}, "
                f"source_mode=false, sync=true"
            )


def _build_env_vars(
    config: AgentConfig,
    agent_mcp_key,
    trinity_mcp_url: str,
    tr: _TemplateResolution,
) -> tuple[dict, Optional[str]]:
    """Assemble the full container env in the monolith's exact sequence and
    return `(env_vars, auto_assigned_subscription_id)`."""
    env_vars = _build_base_env(config)
    auto_assigned_subscription_id = _apply_subscription_env(config, env_vars)
    _apply_gemini_and_otel_env(config, env_vars)
    _apply_mcp_and_auth_env(config, env_vars, agent_mcp_key, trinity_mcp_url)
    _apply_github_env(
        config,
        env_vars,
        tr.github_repo_for_agent,
        tr.github_pat_for_agent,
        tr.fork_upstream_repo,
        tr.git_working_branch,
    )

    # #946 / #1081 Phase 2: opt an allowlisted pilot agent into the pull worker
    # pool. Returns {} (a no-op) for every non-pilot agent, so the default push
    # behavior is unchanged. See services/agent_service/pull_mode.py.
    from services.agent_service.pull_mode import pull_mode_env_vars
    env_vars.update(pull_mode_env_vars(config.name))
    return env_vars, auto_assigned_subscription_id


async def _workspace_volume_mount(config: AgentConfig, volumes: dict) -> None:
    """Get-or-create the durable per-agent workspace volume and mount it at
    /home/developer. A pre-existing volume is a DECLARED adopt (#1667 — the
    refusal gate already ran before the try-block)."""
    agent_volume_name = f"agent-{config.name}-workspace"
    # #1667: adopting a pre-existing volume is a DECISION, not a
    # fallthrough. This used to be get-then-create with no branch —
    # an existing volume was silently mounted as the new agent's
    # `/home/developer`, so whatever the previous holder of this
    # name left behind (its `.env`, its `.credentials.enc`, its
    # workspace) resurfaced inside a different agent, possibly a
    # different owner's. #1664's gate covers the case where a row
    # still claims the base (rename); this covers the case where
    # NOTHING claims it — a purge whose removal hit an in-use 409,
    # a crash between `volume_create` and the ownership INSERT
    # (creation writes the volume first — the reason the orphan
    # sweep carries a 1h creation grace), or a restored backup.
    #
    # Emptiness cannot be the discriminator: the one legitimate
    # adopter — deploy-local (#950) — PRE-POPULATES this volume with
    # the template before calling create, so a valid adopt is
    # non-empty. (And Docker auto-populates a named volume from the
    # image on first mount, so "empty" wouldn't even identify a
    # crashed create.) So the adopter declares itself, and everyone
    # else is refused.
    try:
        await volume_get(agent_volume_name)
        # Reaching here means the volume pre-exists. The refusal
        # gate above already ran, so this is a declared adopt —
        # deploy-local's pre-populated workspace (#950). Logged:
        # an adopt is never silent again (#1667).
        logger.info(
            "[#1667] adopting pre-existing workspace volume %s for %s",
            agent_volume_name,
            config.name,
        )
    except docker.errors.NotFound:
        await volume_create(
            name=agent_volume_name,
            labels={
                'trinity.platform': 'agent-workspace',
                'trinity.agent-name': config.name
            }
        )
    volumes[agent_volume_name] = {'bind': '/home/developer', 'mode': 'rw'}  # Persistent workspace


async def _shared_folder_mounts(
    config: AgentConfig, volumes: dict, template_shared_folders: Optional[dict]
) -> None:
    """Phase 9.11: apply the template-defined shared-folder config, then create/
    mount the expose volume and mount any consumable peer shared volumes."""
    # First, write template-defined shared folder config to DB (if defined)
    if template_shared_folders:
        try:
            db.upsert_shared_folder_config(
                agent_name=config.name,
                expose_enabled=template_shared_folders.get("expose", False),
                consume_enabled=template_shared_folders.get("consume", False)
            )
            logger.info(f"Applied template shared folder config for {config.name}: expose={template_shared_folders.get('expose')}, consume={template_shared_folders.get('consume')}")
        except Exception as e:
            logger.warning(f"Failed to apply template shared folder config for {config.name}: {e}")

    shared_folder_config = db.get_shared_folder_config(config.name)
    if shared_folder_config:
        # If agent exposes a shared folder, create and mount the shared volume
        if shared_folder_config.expose_enabled:
            shared_volume_name = db.get_shared_volume_name(config.name)
            volume_created = False
            try:
                await volume_get(shared_volume_name)
            except docker.errors.NotFound:
                await volume_create(
                    name=shared_volume_name,
                    labels={
                        'trinity.platform': 'agent-shared',
                        'trinity.agent-name': config.name
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

        # If agent consumes shared folders, mount available shared volumes
        if shared_folder_config.consume_enabled:
            available_folders = db.get_available_shared_folders(config.name)
            for source_agent in available_folders:
                source_volume = db.get_shared_volume_name(source_agent)
                mount_path = db.get_shared_mount_path(source_agent)
                # Only mount if the source volume exists
                try:
                    await volume_get(source_volume)
                    volumes[source_volume] = {'bind': mount_path, 'mode': 'rw'}
                except docker.errors.NotFound:
                    # Source agent hasn't started yet or doesn't have shared volume
                    pass


async def _public_volume_mount(config: AgentConfig, volumes: dict) -> None:
    """FILES-001 Step 2: create + mount the per-agent public volume when file
    sharing is enabled (symmetric to the shared-folders expose flow)."""
    if db.get_file_sharing_enabled(config.name):
        public_volume_name = db.get_public_volume_name(config.name)
        public_volume_created = False
        try:
            await volume_get(public_volume_name)
        except docker.errors.NotFound:
            await volume_create(
                name=public_volume_name,
                labels={
                    'trinity.platform': 'agent-public',
                    'trinity.agent-name': config.name,
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


async def _build_volume_mounts(
    config: AgentConfig,
    config_path: Path,
    credentials_path: Path,
    template_volume: Optional[dict],
    cred_files_volume: Optional[dict],
    template_shared_folders: Optional[dict],
) -> dict:
    """Assemble the container volume-mount spec: config/creds/encrypted-data
    binds, the durable workspace (skipped for volume-less ghosts, ent#69), the
    template/cred bind mounts, and the shared-folder + FILES-001 public volumes."""
    # Create per-agent persistent volume for /home/developer (Pillar III: Persistent Memory)
    # This ensures files created by the agent survive container restarts.
    # trinity-enterprise#69: ephemeral ghosts are VOLUME-LESS — their
    # /home/developer lives on the container writable layer (overlayfs),
    # auto-reclaimed by container removal. Volumes exist to survive
    # recreate, and ghosts never recreate.
    # #1811: the `encrypted-data:/data` mount was removed rather than copied
    # into the recovery path. It was dead AND unsafe:
    #   * nothing in the agent image ever touched /data — the Dockerfile only
    #     `mkdir`s it, and no code in docker/base-image references it;
    #   * the volume name was a LITERAL, so a single volume was mounted rw into
    #     every agent at once — a cross-agent read/write surface in a product
    #     whose premise is per-agent isolation. Unused today is not a guarantee.
    # Removing it here (instead of adding it to recreate_missing_container)
    # makes both paths agree and closes the surface. The volume itself is not
    # deleted, so anything historically written to it remains on the host.
    volumes = {
        str(config_path): {'bind': '/config/agent-config.yaml', 'mode': 'ro'},
        str(credentials_path): {'bind': '/config/credentials.json', 'mode': 'ro'},
    }
    if not config.ephemeral:
        await _workspace_volume_mount(config, volumes)

    if template_volume:
        volumes.update(template_volume)
    if cred_files_volume:
        volumes.update(cred_files_volume)

    await _shared_folder_mounts(config, volumes, template_shared_folders)
    await _public_volume_mount(config, volumes)
    return volumes


async def _create_agent_container(
    config: AgentConfig,
    volumes: dict,
    env_vars: dict,
    current_user: User,
    ephemeral_expires_at: Optional[str],
):
    """`docker run` the agent container with the baseline security posture
    (cap_drop ALL + mode caps, AppArmor, tmpfs #1098, mem/cpu limits). AC #5:
    the agent network is HARD-CODED here — agents never join the platform net."""
    # Get system-wide full_capabilities setting (not per-agent)
    full_capabilities = get_agent_full_capabilities()

    # Create container with security settings
    # Security principle: ALWAYS apply baseline security, even in full_capabilities mode
    # - Always drop ALL caps, then add back only what's needed
    # - Always apply AppArmor profile
    # - Always apply noexec,nosuid to /tmp
    container_labels = {
        'trinity.platform': 'agent',
        'trinity.agent-name': config.name,
        'trinity.ssh-port': str(config.port),
        'trinity.cpu': config.resources['cpu'],
        'trinity.memory': config.resources['memory'],
        'trinity.created': utc_now_iso(),
        'trinity.template': config.template or '',
        'trinity.agent-runtime': config.runtime or 'claude-code',
        'trinity.full-capabilities': str(full_capabilities).lower(),
        'trinity.base-image-version': get_platform_version()
    }
    if config.import_intent:
        # trinity-enterprise#15: ops-visible import provenance (a copy agent
        # is otherwise indistinguishable from a local agent post-hoc).
        container_labels['trinity.import-intent'] = config.import_intent
    if config.ephemeral:
        # trinity-enterprise#69: Docker-as-truth ghost markers — the GC
        # orphan pass reclaims labeled containers whose ownership row
        # is gone (backend restarted mid-create/mid-discard).
        container_labels['trinity.ephemeral'] = 'true'
        container_labels['trinity.ephemeral-expires-at'] = ephemeral_expires_at or ''
    if current_user.agent_name:
        # Part 2 spawn provenance rides on ANY agent-spawned creation
        # (durable or ephemeral), pairing with the DB columns.
        container_labels['trinity.spawned-by'] = current_user.agent_name

    return await containers_run(
        config.base_image,
        detach=True,
        name=f"agent-{config.name}",
        ports={'22/tcp': config.port},
        volumes=volumes,
        environment=env_vars,
        labels=container_labels,
        # Always apply AppArmor for additional sandboxing
        security_opt=['apparmor:docker-default'],
        # Always drop ALL capabilities first (defense in depth)
        cap_drop=['ALL'],
        # Add back only the capabilities needed for the mode
        cap_add=FULL_CAPABILITIES if full_capabilities else RESTRICTED_CAPABILITIES,
        read_only=False,
        # Always apply noexec,nosuid to /tmp for security (#1098: scratch
        # is redirected off this tiny tmpfs via the TMPDIR env var).
        tmpfs=AGENT_TMPFS_MOUNT,
        # #1871: bound the container's json-file log. Docker's default is
        # unbounded, so without this the log grows until the Docker data root
        # fills and dockerd wedges. Creation-time — see AGENT_LOG_CONFIG.
        log_config=AGENT_LOG_CONFIG,
        network='trinity-agent-network',
        # #1197: cpu/memory normalized + validated above (raises 400 on
        # a bad template value), so these are guaranteed Docker-valid.
        mem_limit=config.resources['memory'],
        # #1126: nano_cpus (Linux CFS quota), NOT cpu_count — the latter
        # is Windows-only in docker-py and left NanoCpus=0, so newly
        # created agents never got a CPU limit on Linux.
        nano_cpus=int(config.resources['cpu']) * 1_000_000_000,
    )


async def _broadcast_agent_created(agent_status: AgentStatus, ws_manager) -> None:
    """Broadcast the `agent_created` WS event (best-effort, no-op without a
    ws_manager)."""
    if ws_manager:
        await ws_manager.broadcast(json.dumps({
            "event": "agent_created",
            "data": {
                "name": agent_status.name,
                "status": agent_status.status,
                "port": agent_status.port,
                "created": agent_status.created.isoformat(),
                "resources": agent_status.resources,
                "container_id": agent_status.container_id
            }
        }))


def _register_agent(
    config: AgentConfig,
    current_user: User,
    template_data: dict,
    ephemeral_expires_at: Optional[str],
    auto_assigned_subscription_id: Optional[str],
) -> None:
    """DB registration: ownership row (require_email #1129 + ephemeral fields +
    provenance), the ent#69 parent→child spawn edge, the auto-assigned
    subscription (#74), the AVATAR-003 avatar seed, and default permissions.
    Each post-registration grant is log-and-continue (non-fatal)."""
    # #1129: seed require_email from the fleet-wide default
    # (secure-by-default ON) at creation; owners can override per agent.
    # trinity-enterprise#69 Part 2: spawn provenance is written for ANY
    # agent-spawned creation; the parent's key id (not just its name)
    # backs the control gate — a recycled name alone must never inherit
    # control of surviving children.
    spawned_by_key_id = None
    if current_user.agent_name:
        try:
            parent_key = db.get_agent_mcp_api_key(current_user.agent_name)
            spawned_by_key_id = parent_key.id if parent_key else None
        except Exception as e:
            logger.warning(f"Could not resolve parent key id for {current_user.agent_name}: {e}")
    db.register_agent_owner(
        config.name,
        current_user.username,
        require_email=get_agent_default_require_email(),
        is_ephemeral=bool(config.ephemeral),
        ephemeral_max_executions=(config.ephemeral.max_executions if config.ephemeral else None),
        ephemeral_expires_at=ephemeral_expires_at,
        spawned_by_agent=current_user.agent_name,
        spawned_by_key_id=spawned_by_key_id,
        # Ghosts default to 1 concurrent turn: bounds check-then-act
        # budget overshoot to a single in-flight execution and shrinks
        # the blast radius of an untrusted workspace.
        max_parallel_tasks=(1 if config.ephemeral else None),
    )

    # ent#1640: persist the optional display label set at creation. Reuses the
    # same setter as PUT /label (trim + blank→NULL), on the row just created.
    # Best-effort: a label write must never fail a successful agent creation —
    # the agent is fully functional under its slug without it.
    if config.display_label:
        try:
            db.set_display_label(config.name, config.display_label)
        except Exception as e:
            logger.warning(f"Could not set display label for {config.name}: {e}")

    # trinity-enterprise#69 Part 2: auto-grant the parent→child
    # permission edge so the spawning agent can immediately
    # chat/list/info its child (the MCP layer gates on
    # agent_permissions; grant_default_permissions is deliberately
    # empty). created_by carries the spawn sentinel so a human grant
    # and an auto-grant stay distinguishable.
    if current_user.agent_name:
        try:
            db.add_agent_permission(
                current_user.agent_name,
                config.name,
                created_by=f"spawn:{current_user.agent_name}",
            )
            logger.info(
                f"Auto-granted spawn permission edge {current_user.agent_name} -> {config.name}"
            )
        except Exception as e:
            logger.warning(
                f"Failed to auto-grant spawn permission edge for {config.name}: {e}"
            )

    # Persist auto-assigned subscription (#74)
    if auto_assigned_subscription_id:
        try:
            db.assign_subscription_to_agent(config.name, auto_assigned_subscription_id)
        except Exception as e:
            logger.warning(f"Failed to persist subscription assignment for {config.name}: {e}")

    # AVATAR-003: Seed avatar prompt from template
    # (skipped for ephemeral ghosts — avatar generation is a paid,
    # durable-identity nicety a disposable agent never benefits from)
    _avatar_prompt = (template_data.get("avatar_prompt") if template_data else None) if not config.ephemeral else None
    if _avatar_prompt:
        try:
            db.set_default_avatar(config.name, _avatar_prompt, datetime.now(timezone.utc).isoformat())
            logger.info(f"[AVATAR-003] Seeded avatar prompt from template for {config.name}")
        except Exception as e:
            logger.warning(f"[AVATAR-003] Failed to seed avatar prompt for {config.name}: {e}")

    # Phase 9.10: Grant default permissions (Option B - same-owner agents)
    try:
        permissions_count = db.grant_default_permissions(config.name, current_user.username)
        if permissions_count > 0:
            logger.info(f"Granted {permissions_count} default permissions for agent {config.name}")
    except Exception as e:
        logger.warning(f"Failed to grant default permissions for {config.name}: {e}")

    # Phase 7: git config was already reserved and persisted via
    # `reserve_and_generate_instance_id` earlier in this function
    # (S7 Layer 0). No second db.create_git_config call here — that
    # would either be a no-op (agent_name UNIQUE) or, worse, mask
    # a Layer 2 conflict.


def reconcile_declared_schedules(
    agent_name: str, declared: list, owner_username: str
) -> None:
    """Create a template's declared schedules on `agent_name`, skipping by name.

    Shaped as a RECONCILE primitive — it takes `agent_name`, not `AgentConfig`,
    so an operator-triggered "re-apply template" can call it unchanged. It is
    NOT hooked into container recreate today, deliberately: an eager
    re-materialize would resurrect schedules an operator deliberately deleted,
    which is strictly worse than the gap it would close. (ent#89 D2/D2b)

    Non-fatal by contract — the caller wraps the whole call, so a failure here
    (including the `list_agent_schedules` read) costs the schedules, never the
    agent.

    Counts come from ACTUAL outcomes, never from `len(declared)`:
    `db.create_schedule` returns **None** — it does not raise — on three silent
    paths (unknown user, no agent access, and the #1445 `is_agent_live`
    no-orphan gate), so a length-derived counter would report schedules that
    were never written.
    """
    if not declared:
        return

    # Name-match idempotency (AC #4). Known blind spot: `list_agent_schedules`
    # excludes soft-deleted rows (#834), so a soft-deleted schedule of the same
    # name does not suppress a re-create. Accepted and documented.
    seen = {s.name for s in db.list_agent_schedules(agent_name)}
    created = skipped = failed = 0

    for entry in declared:
        name = entry["name"]
        if name in seen:
            skipped += 1
            continue
        try:
            # `enabled` is passed EXPLICITLY: `ScheduleCreate.enabled` defaults
            # to True, so omitting it would arm every undeclared schedule and
            # invert AC #3.
            schedule = db.create_schedule(
                agent_name=agent_name,
                username=owner_username,
                schedule_data=ScheduleCreate(
                    name=name,
                    cron_expression=entry["cron"],
                    message=entry["message"],
                    enabled=entry["enabled"],
                    timezone=entry["timezone"],
                    description=entry["description"],
                ),
            )
        except Exception as e:  # noqa: BLE001 — one bad entry never costs the rest
            failed += 1
            logger.warning(
                "[ent#89] Failed to create declared schedule %r for %s: %s",
                name, agent_name, e,
            )
            continue
        if not schedule:
            failed += 1
            logger.warning(
                "[ent#89] Declared schedule %r for %s was not created "
                "(no user, no access, or the agent is not live)",
                name, agent_name,
            )
            continue
        seen.add(name)
        created += 1

    logger.info(
        "[ent#89] Declared schedules for %s: %d created, %d already existed, "
        "%d failed (%d declared)",
        agent_name, created, skipped, failed, len(declared),
    )


async def _materialize_agent_files(
    config: AgentConfig,
    template_data: dict,
    github_repo_for_agent: Optional[str],
    fork_upstream_repo: Optional[str],
    github_pat_for_agent: Optional[str] = None,
    declared_schedules: Optional[list] = None,
    owner_username: str = "",
    declared_plugins: Optional[dict] = None,
) -> None:
    """Materialize the S4 persistent-state allowlist (#383), the declared
    data_paths (#1169), the declared `plugins:` (#1704) and the declared
    `schedules:` (trinity-enterprise#89) into the agent, then opt non-source-mode
    GitHub agents into the auto-sync heartbeat (#389). All are non-fatal."""
    # S4 (#383): Materialize persistent-state allowlist into the agent.
    # Runtime sync/reset paths read `.trinity/persistent-state.yaml`;
    # template.yaml is only read at creation (10-min cache), so this
    # is the source of truth going forward. Non-fatal on failure —
    # reset operations fall back to the default list at read time.
    persistent_state = (
        (template_data or {}).get(
            "persistent_state", git_service.DEFAULT_PERSISTENT_STATE
        )
    )
    try:
        await git_service.materialize_persistent_state(
            config.name, persistent_state
        )
    except Exception as e:
        logger.warning(
            f"[S4] Failed to materialize persistent-state.yaml for "
            f"{config.name}: {e}"
        )

    # #1169: Materialize the declared `data_paths` into the agent.
    # Opt-in (empty list = no-op), so undeclared agents are
    # untouched. Writes `.trinity/data-paths.yaml` and gitignores the
    # `data/` root in the agent's own .gitignore. Non-fatal — the
    # home volume is already durable; the declaration just enables
    # selective snapshot/export and keeps runtime data out of git.
    data_paths = (template_data or {}).get(
        "data_paths", git_service.DEFAULT_DATA_PATHS
    )
    try:
        await git_service.materialize_data_paths(
            config.name, data_paths
        )
    except Exception as e:
        logger.warning(
            f"[#1169] Failed to materialize data-paths.yaml for "
            f"{config.name}: {e}"
        )

    # #1704: materialize the template's declared Claude Code `plugins:` into a
    # COMMITTED `.trinity/plugins.yaml`, so the plugin selection survives a
    # git-based reconstitution (the boot hook re-installs anything missing).
    # Opt-in (empty = no-op) and ghost-skipped: an ephemeral agent never
    # recreates and never persists. Non-fatal — sits inside the destructive
    # rollback fence, so a raise here must never cost a successful creation
    # (`declared_plugins` comes from the resolver, NOT `template_data`, since the
    # `github:` path never populates the latter).
    if declared_plugins and not config.ephemeral:
        try:
            await git_service.materialize_plugins(
                config.name, declared_plugins
            )
        except Exception as e:
            logger.warning(
                f"[#1704] Failed to materialize plugins.yaml for "
                f"{config.name}: {e}"
            )

    # trinity-enterprise#89: materialize the template's declared `schedules:`.
    # The ghost skip lives HERE rather than in the helper: schedules on an
    # ephemeral agent are a 400 by ent#69 fleet hygiene, and a new caller must
    # exclude ghosts itself. The try/except wraps the ENTIRE call, including
    # the helper's `list_agent_schedules` read — this whole function sits inside
    # the destructive rollback fence, so a raise that escapes here would roll
    # back a successful creation over a schedule.
    if declared_schedules and not config.ephemeral:
        try:
            reconcile_declared_schedules(
                config.name, declared_schedules, owner_username
            )
        except Exception as e:
            logger.warning(
                f"[ent#89] Failed to materialize declared schedules for "
                f"{config.name}: {e}"
            )

    # #389 S1a: opt non-source-mode GitHub-template agents into the
    # auto-sync heartbeat by default. Source-mode agents stay opt-in
    # (auto-pushing to main would clobber protected branches) —
    # except fork-to-own agents (#93), which own their repo.
    # trinity-enterprise#69: ghosts never auto-push — their workspace
    # is throwaway by definition, so the 15-min sync heartbeat stays off.
    # ent#123: tokenless agents never auto-push (belt — see _apply_github_env).
    if github_repo_for_agent and github_pat_for_agent and not config.ephemeral and (not config.source_mode or fork_upstream_repo):
        try:
            db.set_git_auto_sync_enabled(config.name, True)
        except Exception as e:
            logger.warning(
                f"Failed to enable auto-sync for {config.name}: {e}"
            )

    # #2069: the fleet-wide `.gitignore` merge never ran at creation, so the
    # 15-min in-container auto-sync loop (on from birth for the GIT_SYNC_AUTO
    # set — non-source/fork `github:` agents, ephemeral ghosts INCLUDED) staged
    # `.trinity/` runtime state + the root-level `.env`/`.mcp.json` into a
    # user-owned repo before any Push could migrate the list. Land the canonical
    # list after startup.sh's FULL git setup (gated inside the merge on
    # agent-server /health readiness, which follows the clone+checkout at
    # startup.sh:517) and before the first auto-sync cycle. Fire-and-forget so it
    # adds no creation latency; non-fatal. Gated on the SAME ENV predicate that
    # bakes GIT_SYNC_AUTO (NOT the DB-flag block above, which excludes ghosts),
    # so the merge covers exactly the auto-committing population.
    if git_service._git_auto_sync_baked(
        config, github_repo_for_agent, github_pat_for_agent, fork_upstream_repo
    ):
        git_service.spawn_gitignore_merge_after_clone(config.name)


def _rollback_failed_creation(handles: _RollbackHandles) -> None:
    """The except-path DB/quota rollback: the agent_git_config reservation, the
    ephemeral quota slot, and the agent MCP key — each guarded, each
    best-effort.

    Scope note (ent#313): this function stays DB/quota-only, but its old
    docstring justified the omission by saying the container and volumes were
    "left for the cleanup watchdog". No such watchdog exists for a
    non-ephemeral agent, so that deferral leaked a running, ownerless
    container. The container is now reclaimed by
    ``_reclaim_failed_creation_container``, called right after this one.
    """
    # S7 Layer 0 (#382): if anything after the reservation fails,
    # roll back the agent_git_config row so the working branch is
    # released and a retry can claim it fresh.
    if handles.github_repo_for_agent and handles.git_instance_id:
        try:
            db.delete_git_config(handles.agent_name)
        except Exception as cleanup_exc:
            logger.warning(
                "Failed to roll back agent_git_config for %s after "
                "creation failure: %s",
                handles.agent_name,
                cleanup_exc,
            )
    # trinity-enterprise#69: release the reserved ephemeral quota slot
    # so a failed creation doesn't permanently consume owner capacity.
    if handles.ephemeral_slot_reserved and handles.ephemeral_owner_id is not None:
        try:
            ephemeral_service.release_ephemeral_slot(handles.ephemeral_owner_id)
        except Exception as cleanup_exc:
            logger.warning(
                "Failed to release ephemeral quota slot for %s after "
                "creation failure: %s",
                handles.agent_name,
                cleanup_exc,
            )
    # #1197: the agent-scoped MCP key is minted before container
    # creation, so a failure here would otherwise leave an orphaned
    # mcp_api_keys row (one per failed attempt). Roll it back too.
    if handles.agent_mcp_key:
        try:
            db.delete_agent_mcp_api_key(handles.agent_name)
        except Exception as cleanup_exc:
            logger.warning(
                "Failed to roll back MCP key for %s after creation "
                "failure: %s",
                handles.agent_name,
                cleanup_exc,
            )


def _cleanup_copy_artifacts(handles: _RollbackHandles) -> None:
    """trinity-enterprise#15: copy-intent except-path cleanup — the staged
    snapshot dir (best-effort, idempotent) and the volume THIS attempt
    pre-populated. Called AFTER ``_reclaim_failed_creation_container`` so a
    created-then-reclaimed container no longer holds the volume mount.
    Removing the volume keeps the #1667 leftover-volume guard from 409ing the
    user's retry; safe because ``copy_volume_name`` is set only after this
    attempt created+populated it."""
    snapshot_import.cleanup_staging(handles.copy_staging_dir)
    if handles.copy_volume_name and docker_client:
        try:
            docker_client.volumes.get(handles.copy_volume_name).remove(force=True)
        except Exception as cleanup_exc:  # noqa: BLE001 — NotFound/in-use both fine
            logger.warning(
                "Failed to remove pre-populated copy volume %s after creation "
                "failure (the #1581 orphan sweep will reclaim it): %s",
                handles.copy_volume_name,
                cleanup_exc,
            )


def _is_container_name_conflict(exc: BaseException) -> bool:
    """True when the failure is Docker refusing a duplicate container name.

    On a 409 the daemon created NOTHING, so any `agent-{name}` container that
    exists belongs to somebody else — a live agent of this install, or (on a
    shared Docker daemon: git worktrees, a second stack) another install's
    agent entirely. Removing it would be catastrophic, so this is the first
    thing the reclaim checks.
    """
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        # Duck-typed on purpose: `isinstance(exc, docker.errors.APIError)` raises
        # `TypeError: isinstance() arg 2 must be a type` wherever the docker
        # module is a test double, which would propagate out of a function whose
        # whole contract is "never raises" and REPLACE the creation error the
        # caller is trying to report. Any exception carrying a 409 response is
        # the signal we want, whatever its class.
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 409:
            return True
        text = str(exc).lower()
        if "already in use" in text or "conflict" in text:
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def _created_by_this_attempt(container, floor_ts: Optional[str]) -> bool:
    """True only if the container's own `trinity.created` label is at or after
    the instant this creation attempt entered the docker block.

    Fail-closed: a missing floor, a missing label, or an unparseable value all
    return False. The cost of a false negative is one orphan an operator removes
    by hand; the cost of a false positive is deleting a running agent.
    """
    if not floor_ts:
        return False
    try:
        labels = (container.attrs or {}).get("Config", {}).get("Labels") or {}
        created = labels.get("trinity.created")
        if not created:
            return False
        return parse_iso_timestamp(created) >= parse_iso_timestamp(floor_ts)
    except Exception as exc:  # noqa: BLE001 — unparseable ⇒ not ours
        logger.warning(
            "ent#313: could not establish creation provenance for a container: %s",
            exc,
        )
        return False


# #2215 D2: bounded port-bind-conflict retry around `_create_agent_container`.
# The per-port Redis reservation (docker_service, D1) is fail-open, so a Redis
# outage/restart, an expired reservation, or a foreign host process can still
# surface as a bind failure at `containers.run` — this belt converges those
# within the same create call, before the caller (e.g. a first-run seed deploy)
# computes a permanently-latched `partial`.
_PORT_BIND_RETRY_MAX_ATTEMPTS = 3


def _is_port_bind_conflict(exc: BaseException) -> bool:
    """True when the failure is Docker failing to BIND the published SSH port.

    Deliberately NARROW — exactly two daemon phrasings:
      * "port is already allocated"  (docker-proxy, native Linux)
      * "address already in use"     (userland-proxy phrasing, Docker Desktop)
    and deliberately NOT the generic "conflict": daemon-unreachable / timeout /
    name-conflict errors must bubble immediately without burning retries.

    NOTE the overlap with `_is_container_name_conflict`, whose text fallback
    matches the bare substring "already in use" — which Docker Desktop's bind
    failure CONTAINS. A bind conflict proves `containers.create` SUCCEEDED
    (only the start/bind failed) — the exact opposite of a name conflict (the
    daemon created NOTHING) — so wherever both could match, bind must be
    classified FIRST (see `_reclaim_failed_creation_container`). Duck-typed,
    cycle-guarded cause/context walk, mirroring `_is_container_name_conflict`
    (never `isinstance` against the docker module — it may be a test double).
    """
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        # Defensive str(): docker.errors.APIError.__str__ dereferences response
        # attributes and can itself raise on a partial/stubbed response object —
        # and this classifier runs inside the never-raises reclaim (D2b), where
        # a raise would REPLACE the creation error being reported.
        try:
            text = str(exc).lower()
        except Exception:
            text = ""
        if "port is already allocated" in text or "address already in use" in text:
            return True
        exc = exc.__cause__ or exc.__context__
    return False


async def _cleanup_bind_failed_container(agent_name: str, floor_ts: Optional[str]) -> bool:
    """Remove the Created container a failed port bind left behind (#2215 D2).

    A bind failure means `containers.create` succeeded and only the start
    failed — the daemon holds a Created `agent-{name}` husk that would 409 the
    next attempt's name. Runs on EVERY bind-classified attempt, the final one
    included, so a bind husk is never handed to the generic ent#313 reclaim
    still on the name.

    Returns True only when the name is PROVABLY clear for another attempt
    (husk removed, or no container exists). Returns False on ANY doubt —
    ownership row present, lookup failure, unprovable provenance, removal
    failure — so the caller aborts retries and re-raises the ORIGINAL bind
    error (the outer ent#313 reclaim then takes its own gated removal shot via
    the bind-before-name precedence guard there). Proceeding after a failed
    removal would make the next attempt 409 against our OWN husk, which the
    reclaim reads as "not ours" and strands.

    The lookup is a direct `containers.get`, NOT `get_agent_container`: that
    helper flattens a lookup FAILURE into "absent", and here absent means
    "safe to retry" while failure must abort. NotFound is duck-typed off the
    response status (the `_is_container_name_conflict` rationale — the docker
    module may be a test double).
    """
    try:
        if db.is_agent_name_reserved(agent_name):
            # Ownership-gate parity with the ent#313 reclaim: on a shared
            # daemon the name may be claimed by an agent that is not ours.
            logger.warning(
                "#2215: %s has an ownership row — leaving its container alone; "
                "aborting port retries",
                agent_name,
            )
            return False
    except Exception as lookup_exc:  # noqa: BLE001 — fail closed
        logger.warning(
            "#2215: ownership lookup failed for %s (%s) — aborting port retries",
            agent_name,
            lookup_exc,
        )
        return False
    try:
        target = docker_client.containers.get(f"agent-{agent_name}")
    except Exception as lookup_exc:  # noqa: BLE001
        status = getattr(getattr(lookup_exc, "response", None), "status_code", None)
        if status == 404:
            # No husk — the daemon kept nothing; the name is clear.
            return True
        logger.warning(
            "#2215: container lookup failed for %s (%s) — aborting port retries",
            agent_name,
            lookup_exc,
        )
        return False
    if not _created_by_this_attempt(target, floor_ts):
        logger.warning(
            "#2215: a container named agent-%s exists but is not provably this "
            "attempt's — aborting port retries",
            agent_name,
        )
        return False
    try:
        await container_remove(target, force=True)
    except Exception as remove_exc:  # noqa: BLE001
        logger.warning(
            "#2215: could not remove the bind-failed container for %s (%s) — "
            "aborting port retries",
            agent_name,
            remove_exc,
        )
        return False
    logger.info(
        "#2215: removed the bind-failed container for %s before the port retry",
        agent_name,
    )
    return True


async def _run_agent_container_with_port_retry(
    config: AgentConfig,
    volumes: dict,
    env_vars: dict,
    current_user: User,
    ephemeral_expires_at: Optional[str],
    handles: "_RollbackHandles",
    auto_allocated_port: bool,
):
    """`_create_agent_container` with a bounded port-bind-conflict retry (#2215).

    The D1 reservation is fail-open, so a collision can still surface at
    `containers.run` (Redis down/restarted, reservation expired, a foreign
    process on the host). On a bind-classified failure, in strict order:
    (1) record the failed port; (2) clean up the leaked husk — on EVERY
    bind-classified attempt, the final one included; (3) if attempts remain,
    re-allocate with the failed ports excluded and retry. Bounded at
    `_PORT_BIND_RETRY_MAX_ATTEMPTS` total attempts, and gated on the port
    having been AUTO-allocated — a caller-pinned port never silently moves.
    Any cleanup doubt aborts and re-raises the ORIGINAL bind error, never the
    cleanup error. Mutating `config.port` between attempts is safe: it is
    consumed only inside `_create_agent_container` (label + ports map, rebuilt
    per call) and no DB copy of the port exists anywhere.
    """
    attempted_ports: set = set()
    attempt = 1
    while True:
        try:
            return await _create_agent_container(
                config, volumes, env_vars, current_user, ephemeral_expires_at
            )
        except Exception as exc:  # noqa: BLE001 — non-bind re-raised immediately
            if not auto_allocated_port or not _is_port_bind_conflict(exc):
                raise
            attempted_ports.add(config.port)
            if not await _cleanup_bind_failed_container(
                config.name, handles.container_floor_ts
            ):
                # Cleanup doubt: abort — the bare raise re-raises the ORIGINAL
                # bind error, so the outer ent#313 reclaim gets its own shot.
                raise
            if attempt >= _PORT_BIND_RETRY_MAX_ATTEMPTS:
                raise
            new_port = get_next_available_port(exclude=attempted_ports)
            logger.warning(
                "#2215: port %s was already bound — retrying %s on port %s "
                "(attempt %d/%d)",
                config.port,
                config.name,
                new_port,
                attempt + 1,
                _PORT_BIND_RETRY_MAX_ATTEMPTS,
            )
            config.port = new_port
            attempt += 1


async def _reclaim_failed_creation_container(
    handles: _RollbackHandles, container, exc: BaseException
) -> None:
    """ent#313 — remove the container a failed creation left behind, and clear
    the per-agent Redis keyspace.

    Before this, `_rollback_failed_creation` rolled back only DB/quota handles
    and deferred the container to "the cleanup watchdog" — which does not exist
    for a non-ephemeral agent (`_sweep_ephemeral_agents` is gated on the
    `trinity.ephemeral` label). The two guards deadlocked: nothing removed the
    container, and because the container kept its workspace volume mounted, the
    #1581 orphan-volume sweep could never advance its unattached-strike counter
    either. The agent still appeared in the fleet listing, which is
    Docker-as-truth (Invariant #11) — a phantom with no `agent_ownership` row.

    Two arrival shapes, because the reported failure has no handle:

    * `container` is not None — the create returned and a later step raised.
      Ownership is unambiguous; remove by handle.
    Both shapes first require that the name has NO `agent_ownership` row: the
    create registers the row before the last step, so a late failure must not
    strip the container off an agent the DB already considers created.

    * `container` is None — the failure happened INSIDE `containers.run`
      (the observed case: a 60s Docker read timeout). The daemon may still have
      created it, so re-derive by name — under three fail-closed gates, since a
      lookup by name can resolve to a container this attempt did not create:
      not a name conflict (above), no `agent_ownership` row (a concurrent
      creation that won the name has already registered one), and the
      `trinity.created` provenance check.

    Never raises: this runs inside the creation except-path, which must keep
    reporting the original failure.
    """
    agent_name = handles.agent_name
    target = container

    # Gate BOTH arrival shapes on ownership. Registration happens inside the
    # same try, so a failure in a LATER step (file materialization) arrives here
    # with the row already written — and this is the one case where removing the
    # container makes things worse: the agent would become a row with no
    # container, still holding its name, where before it was at least present
    # and deletable through the normal path. It also covers the no-handle race
    # where a concurrent creation won the name and registered it.
    try:
        if db.is_agent_name_reserved(agent_name):
            logger.warning(
                "ent#313: %s already has an ownership row — leaving its "
                "container in place (delete the agent to remove both)",
                agent_name,
            )
            return
    except Exception as lookup_exc:  # noqa: BLE001 — fail closed
        logger.warning(
            "ent#313: ownership lookup failed for %s; leaving any container in "
            "place: %s",
            agent_name,
            lookup_exc,
        )
        return

    if target is None:
        # #2215 D2b: classify bind-conflict BEFORE the name-conflict decline —
        # Docker Desktop's bind failure ("… bind: address already in use")
        # contains the name-conflict text fallback's substring, but a bind
        # conflict proves the create SUCCEEDED, so fall through to the
        # fail-closed lookup + ownership + provenance gates instead.
        if _is_container_name_conflict(exc) and not _is_port_bind_conflict(exc):
            logger.info(
                "ent#313: creation of %s hit a container-name conflict — the "
                "existing container is not ours, leaving it untouched",
                agent_name,
            )
            return
        try:
            target = get_agent_container(agent_name)
        except Exception as lookup_exc:  # noqa: BLE001
            logger.warning(
                "ent#313: container lookup failed for %s: %s", agent_name, lookup_exc
            )
            return
        if target is not None and not _created_by_this_attempt(
            target, handles.container_floor_ts
        ):
            logger.warning(
                "ent#313: a container named agent-%s exists but predates this "
                "attempt — leaving it untouched",
                agent_name,
            )
            return

    if target is not None:
        try:
            await container_remove(target, force=True)
            logger.info(
                "ent#313: removed the orphaned container for %s after a failed "
                "creation",
                agent_name,
            )
        except Exception as remove_exc:  # noqa: BLE001
            # Leave the Redis keyspace alone: the container is still up, so the
            # slot ZSET is not provably idle.
            logger.warning(
                "ent#313: could not remove the orphaned container for %s — it "
                "needs manual `docker rm -f agent-%s`: %s",
                agent_name,
                agent_name,
                remove_exc,
            )
            return

    # Reached only when the container is provably gone (removed just now, or
    # never created). #1560: the keyspace is name-keyed and TTL-less, so a
    # later agent reusing this name would otherwise inherit stale breaker
    # verdicts and be fast-failed as unhealthy without ever being contacted.
    try:
        await clear_agent_runtime_state(agent_name)
    except Exception as clear_exc:  # noqa: BLE001
        logger.warning(
            "ent#313: Redis state clear failed for %s: %s", agent_name, clear_exc
        )


def _release_ephemeral_on_no_docker(handles: _RollbackHandles) -> None:
    """The docker-unavailable else-branch cleanup (PRESERVED exactly): release
    ONLY the ephemeral quota slot. The MCP key and git-config reservation are
    deliberately NOT rolled back here (a pre-existing, preserved leak)."""
    # trinity-enterprise#69 (review M2): release the quota reservation on
    # the Docker-unavailable path too — otherwise repeated attempts during
    # an outage consume the owner's ghost quota for the counter TTL.
    if handles.ephemeral_slot_reserved and handles.ephemeral_owner_id is not None:
        try:
            ephemeral_service.release_ephemeral_slot(handles.ephemeral_owner_id)
        except Exception as cleanup_exc:
            logger.warning(
                "Failed to release ephemeral quota slot for %s (no docker): %s",
                handles.agent_name,
                cleanup_exc,
            )


def agent_name_is_taken(name: str) -> bool:
    """True when `name` is claimed by a live, soft-deleted, or container-only
    agent — the exact predicate the create path refuses on with
    409 "Agent already exists".

    Exported so a caller that catches that 409 can tell "the agent really is
    there" from the create path's OTHER 409s (#1664 volume-base still owned,
    #1667 unclaimed leftover volume, fork-destination in use), which look
    identical by status code but mean the agent was NOT created (#1790).
    Sharing one predicate is the point: a copy would drift the moment a new
    claim source is added here.
    """
    return bool(
        get_agent_by_name(name)
        or db.get_agent_owner(name)
        or db.is_agent_name_reserved(name)
    )


def _check_name_availability(config: AgentConfig) -> None:
    """Refuse a name already taken (#834 existence guard, incl. soft-deleted) or
    whose data volumes another agent still owns after a rename (#1664). Both
    raise 409 before any side effect."""
    # #834: the name-reservation check must also catch soft-deleted agents.
    # `get_agent_owner` filters them out (user-facing 404 transparency), so
    # we use the unfiltered `is_agent_name_reserved` here. Without this the
    # create flow walks past the existence guard, the container ends up
    # created, and the agent_ownership INSERT hits a UNIQUE constraint
    # IntegrityError leaving the system half-built.
    if agent_name_is_taken(config.name):
        raise HTTPException(status_code=409, detail="Agent already exists")

    # #1664: the name being free does NOT mean its volumes are. Rename frees the
    # NAME while the agent keeps its volumes under the old base (Docker can
    # rename neither a volume nor its label), so `agent-{name}-workspace` can
    # still be a live agent's `/home/developer`. The volume block below is
    # get-then-create — an existing volume is REUSED, not rejected — so without
    # this gate a new agent created under a freed name silently boots on the
    # renamed agent's home volume: its `.env`, its `.credentials.enc`, its
    # workspace, with both containers writing the same disk. The owners need not
    # be the same person, which makes it a cross-tenant credential disclosure,
    # not just corruption. Refuse instead: the volumes are somebody's live data
    # until their owning row is purged.
    # Ghosts are exempt: they are volume-less by construction (the volume block
    # below is `if not config.ephemeral`), so there is nothing to collide with —
    # and this would put a DB read on the burst-spawn path for no reason.
    if not config.ephemeral and db.is_volume_base_reserved(config.name):
        raise HTTPException(
            status_code=409,
            detail=(
                "Agent name unavailable: its data volumes still belong to "
                "another agent (it was renamed). Pick a different name."
            ),
        )


def _enforce_role_quota(config: AgentConfig, current_user: User) -> None:
    """QUOTA-001 per-role agent quota (429). Ephemeral agents have their OWN
    quota (reserved atomically just before the docker block), so they bypass
    this durable-agent limit."""
    # Agent quota enforcement: per-role limits (QUOTA-001).
    # Ephemeral agents have their OWN quota (atomic reservation just before
    # the docker block) — counting ghosts against the durable quota would
    # starve the burst-parallelism use case (trinity-enterprise#69).
    max_agents = get_agent_quota_for_role(current_user.role) if not config.ephemeral else 0
    if max_agents > 0:
        owned = db.get_agents_by_owner(current_user.username)
        # System agents don't count toward user quota
        non_system = [a for a in owned if not (db.get_agent_owner(a) or {}).get("is_system")]
        if len(non_system) >= max_agents:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": f"Agent quota exceeded. You have {len(non_system)}/{max_agents} agents. "
                             f"Delete an agent to create a new one.",
                    "code": "QUOTA_EXCEEDED",
                    "current": len(non_system),
                    "limit": max_agents
                }
            )


def _mint_agent_mcp_key(config: AgentConfig, current_user: User) -> tuple[object, str]:
    """Mint the agent-scoped Trinity MCP API key (best-effort; None on failure).
    Returns `(agent_mcp_key, trinity_mcp_url)`. The key is a rollback handle —
    minted here (before the container) and rolled back in the except."""
    # Phase: Agent-to-Agent Collaboration
    # Generate agent-scoped MCP API key for Trinity MCP access
    agent_mcp_key = None
    trinity_mcp_url = os.getenv('TRINITY_MCP_URL', 'http://mcp-server:8080/mcp')
    try:
        agent_mcp_key = db.create_agent_mcp_api_key(
            agent_name=config.name,
            owner_username=current_user.username,
            description=f"Auto-generated Trinity MCP key for agent {config.name}"
        )
        if agent_mcp_key:
            logger.info(f"Created MCP API key for agent {config.name}: {agent_mcp_key.key_prefix}...")
    except Exception as e:
        logger.warning(f"Failed to create MCP API key for agent {config.name}: {e}")
    return agent_mcp_key, trinity_mcp_url


def _reserve_ephemeral_slot(
    config: AgentConfig, current_user: User
) -> tuple[bool, Optional[int]]:
    """trinity-enterprise#69: atomic ephemeral quota reservation (Redis
    INCR-with-cap; DB-count fallback when Redis is down). Returns
    `(ephemeral_slot_reserved, ephemeral_owner_id)` — the two rollback handles
    the except/else paths release."""
    ephemeral_slot_reserved = False
    ephemeral_owner_id = None
    if config.ephemeral:
        owner_row = db.get_user_by_username(current_user.username)
        ephemeral_owner_id = (owner_row or {}).get("id") if isinstance(owner_row, dict) else getattr(owner_row, "id", None)
        if ephemeral_owner_id is None:
            raise HTTPException(status_code=500, detail="Could not resolve owner for ephemeral quota")
        eph_cap = get_ephemeral_agent_quota()
        if not ephemeral_service.try_reserve_ephemeral_slot(ephemeral_owner_id, eph_cap):
            raise HTTPException(
                status_code=429,
                detail={
                    "error": f"Ephemeral agent quota exceeded ({eph_cap} live ghosts per owner).",
                    "code": "ephemeral_quota_exceeded",
                    "limit": eph_cap,
                },
            )
        ephemeral_slot_reserved = True
    return ephemeral_slot_reserved, ephemeral_owner_id


async def create_agent_internal(
    config: AgentConfig,
    current_user: User,
    request: Optional[Request] = None,
    skip_name_sanitization: bool = False,
    ws_manager=None,
    adopt_existing_workspace: bool = False,
) -> AgentStatus:
    """
    Internal function to create an agent.

    Used by both the API endpoint and system deployment.

    `request` is optional: the HTTP request object is not dereferenced anywhere
    in this function, so boot-time / background callers with no live request
    (e.g. the Cornelius first-run seeder, ent#107) pass `request=None`.

    CRED-002: Credentials are no longer auto-injected during creation.
    They are added after creation via inject_credentials endpoint or
    imported from .credentials.enc on startup.

    Args:
        config: Agent configuration
        current_user: Authenticated user
        request: FastAPI request object
        skip_name_sanitization: If True, don't sanitize the name (used when name is pre-validated)
        ws_manager: Optional WebSocket manager for broadcasts
        adopt_existing_workspace: #1667 — allow mounting a workspace volume that
            ALREADY exists instead of refusing it (409). Only deploy-local
            (#950) may pass True: it pre-populates the volume with the template
            before calling here, so for it a pre-existing volume is expected
            rather than a stranger's leftover. Deliberately a function kwarg and
            NOT a field on `AgentConfig` — as an API field, a caller could set
            it and re-open the silent-adopt disclosure this closes.

    Returns:
        AgentStatus of the created agent

    Raises:
        HTTPException: On validation or creation errors
    """
    original_name = config.name
    if not skip_name_sanitization:
        config.name = sanitize_agent_name(config.name)

    if not config.name:
        raise HTTPException(status_code=400, detail="Invalid agent name - must contain at least one alphanumeric character")

    # trinity-enterprise#69: ephemeral "ghost" pre-gates. All BEFORE any side
    # effect (no partial state on refusal). Mutates config.name to the suffixed
    # ghost name; returns the stamped expiry (None for a durable agent).
    ephemeral_expires_at = _apply_ephemeral_pregates(config, current_user)

    # #834 existence guard + #1664 volume-base guard (both 409, pre-side-effect).
    _check_name_availability(config)

    # #1667: the gate above covers a volume some ROW still claims (the rename
    # case). This covers the volume NOTHING claims — refuse a leftover workspace
    # volume unless the caller (deploy-local #950) explicitly declares an adopt.
    # Raised HERE, before the docker try-block, so the 409 isn't flattened to a
    # generic 500 (nothing is built yet, so nothing to roll back).
    await _guard_leftover_workspace_volume(config, adopt_existing_workspace)

    # #1560: reaching here means the name is free — but `is_agent_name_reserved`
    # only stops matching once the retention purge hard-deletes the row, and the
    # breakers are keyed by name with no TTL. Clear any predecessor's verdict
    # BEFORE the container exists, so nothing races the agent's first heartbeat.
    # Breakers only: no slots exist for a name nothing is running under yet, and
    # the full sweep is reserved for teardown paths.
    clear_agent_breakers(config.name)

    # QUOTA-001: per-role durable-agent quota (429; ephemeral agents bypass it).
    _enforce_role_quota(config, current_user)

    # SEC-172: Validate base image against allowlist before any Docker operations
    validate_base_image(config.base_image)

    # Resolve template (github incl. fork-to-own, or local). The whole github
    # phase — including the real fork-to-own GitHub write and its structured
    # FORK_* 4xx errors — stays OUTSIDE the docker try-block below, so those
    # errors are not flattened to a generic 500.
    tr = await _resolve_template(config, current_user)

    # #1187: runtime is final here (request value, possibly overridden by the
    # template). Reject an unknown one now (clear 400) instead of letting the
    # agent container crash-loop on boot when get_runtime() can't resolve it.
    validate_runtime(config.runtime)

    # #1187: normalize the stored runtime to lowercase so the AGENT_RUNTIME env
    # var and the `trinity.agent-runtime` label agree with the exact-case checks
    # downstream — startup.sh's `[ "${AGENT_RUNTIME}" = "codex" ]` Codex setup
    # block and the Gemini key-injection branch below (`config.runtime ==
    # 'gemini-cli'`). validate_runtime() accepts mixed case (it lowercases only
    # for the membership test) but does not normalize the stored value, so a
    # template `runtime: Codex` would pass validation yet silently skip Codex's
    # startup setup (AGENTS.md mirror / CODEX_HOME) or Gemini's credential inject.
    if config.runtime:
        config.runtime = config.runtime.lower()

    # #2215: the SSH-port allocation moved INTO the docker try-block below (just
    # before the container run) — see the comment there. `config.port` is
    # consumed only by `_create_agent_container`, so nothing between here and
    # there needs it.

    # CRED-002: Credentials are now injected directly into agents after creation
    # via the inject_credentials endpoint, not auto-injected during creation.
    # The agent starts without credentials and they are added via Quick Inject
    # or imported from .credentials.enc files.

    (
        config_path,
        credentials_path,
        template_volume,
        cred_files_volume,
    ) = _stage_config_files(config, tr.template_data, tr.github_template_path)

    # Phase: Agent-to-Agent Collaboration — mint the agent-scoped MCP key
    # (a rollback handle, rolled back in the except below).
    agent_mcp_key, trinity_mcp_url = _mint_agent_mcp_key(config, current_user)

    env_vars, auto_assigned_subscription_id = _build_env_vars(
        config, agent_mcp_key, trinity_mcp_url, tr
    )

    # trinity-enterprise#69: atomic ephemeral quota reservation, placed
    # immediately before the docker block so every later failure path releases
    # it via the except/else rollback below.
    ephemeral_slot_reserved, ephemeral_owner_id = _reserve_ephemeral_slot(
        config, current_user
    )

    # AC #3: assemble the rollback handles the except/else read. Only the
    # orchestrator populates them; each field mirrors a value fixed BEFORE the
    # docker block, so this is byte-identical to reading the locals in place.
    handles = _RollbackHandles(
        agent_name=config.name,
        agent_mcp_key=agent_mcp_key,
        git_instance_id=tr.git_instance_id,
        github_repo_for_agent=tr.github_repo_for_agent,
        ephemeral_slot_reserved=ephemeral_slot_reserved,
        ephemeral_owner_id=ephemeral_owner_id,
        # ent#313: stamped before anything can create a container, so the
        # rollback can tell a container THIS attempt created from one that was
        # already there.
        container_floor_ts=utc_now_iso(),
        # trinity-enterprise#15: staged snapshot dir travels into the rollback
        # so a mid-try failure never strands it.
        copy_staging_dir=(
            tr.copy_snapshot.staging_dir if tr.copy_snapshot else None
        ),
    )

    if docker_client:
        # ent#313: the except-path needs whatever handle we got. It stays None
        # when the failure happens inside container creation itself — the
        # reclaim re-derives it by name under provenance gates.
        created_container = None
        try:
            # Persist the resolved PAT as the per-agent PAT (#347) onto the
            # agent_git_config row the reservation above just created. Inside
            # this try so a failure hits the except below and rolls back the
            # reserved row + MCP key. ent#162 — persist ONLY for a deliberate
            # identity (fork-to-own #93 or the creator's per-user PAT), NEVER
            # the `global` tier (Decision 2: keep github_pat_encrypted NULL so
            # propagation keeps reaching it on admin rotation).
            if tr.github_pat_tier in ("fork", "per_user") and tr.github_repo_for_agent:
                if not db.set_agent_github_pat(config.name, tr.github_pat_for_agent):
                    raise RuntimeError(
                        f"failed to persist per-agent GitHub PAT for {config.name}"
                    )

            # trinity-enterprise#15: copy intent — stream the staged snapshot
            # (+ .trinity-initialized marker) into the workspace volume BEFORE
            # the container exists; `_workspace_volume_mount` below then adopts
            # the volume this attempt just created (the #1667 refusal gate
            # already passed pre-try, so no other owner can claim it).
            if tr.copy_snapshot:
                from .deploy import _prepopulate_workspace_from_template
                # Armed BEFORE the call (review F4): `_prepopulate` creates the
                # volume first and can fail mid-stream — an unarmed handle
                # would strand a half-populated volume that 409s the user's
                # retry via the #1667 guard until the orphan sweep. Safe
                # single-flight: the #1667 gate already proved no volume
                # existed pre-try, so anything under this name is ours.
                handles.copy_volume_name = f"agent-{config.name}-workspace"
                await asyncio.to_thread(
                    _prepopulate_workspace_from_template,
                    config.name,
                    Path(tr.copy_snapshot.staging_dir),
                )
                snapshot_import.cleanup_staging(tr.copy_snapshot.staging_dir)
                handles.copy_staging_dir = None

            volumes = await _build_volume_mounts(
                config,
                config_path,
                credentials_path,
                template_volume,
                cred_files_volume,
                tr.template_shared_folders,
            )
            # #2215: allocate the SSH port HERE — inside the rollback fence and
            # as close to `containers.run` as possible. Two reasons it is not
            # up with the other pre-try resolution steps: (1) the allocator now
            # fails LOUD on a Docker listing fault (a swallowed fault would
            # allocate 2222 over the existing fleet), and a raise before this
            # try would strand the `agent_git_config` reservation
            # `_resolve_template` already wrote — after which every create of
            # that name fails `agent_git_config already exists`, Cornelius's
            # next-boot retry included; (2) the per-port Redis reservation is
            # TTL-bounded (600s), so the ent#15 snapshot prepopulate and the
            # volume builds above must not eat into it. `auto_allocated_port`
            # is captured BEFORE the mutation — captured after it, the gate is
            # always-False and the bind-conflict retry ships dead. Only an
            # auto-allocated port may move between attempts; a caller-pinned
            # port never silently changes (a published port differing from the
            # requested one is a surprise with security texture).
            auto_allocated_port = config.port is None
            if config.port is None:
                config.port = get_next_available_port()
            container = await _run_agent_container_with_port_retry(
                config,
                volumes,
                env_vars,
                current_user,
                ephemeral_expires_at,
                handles,
                auto_allocated_port,
            )
            created_container = container
            agent_status = get_agent_status_from_container(container)
            if tr.copy_snapshot:
                # trinity-enterprise#15: surface snapshot provenance on the
                # create response (the endpoint folds it into the audit entry).
                agent_status.import_snapshot = {
                    "source_repo": tr.copy_snapshot.source_repo,
                    "source_branch": tr.copy_snapshot.source_branch,
                    "head_sha": tr.copy_snapshot.head_sha,
                    "file_count": tr.copy_snapshot.file_count,
                }
            await _broadcast_agent_created(agent_status, ws_manager)
            _register_agent(
                config,
                current_user,
                tr.template_data,
                ephemeral_expires_at,
                auto_assigned_subscription_id,
            )
            await _materialize_agent_files(
                config,
                tr.template_data,
                tr.github_repo_for_agent,
                tr.fork_upstream_repo,
                tr.github_pat_for_agent,
                tr.declared_schedules,
                current_user.username,
                tr.declared_plugins,
            )
            return agent_status
        except Exception as e:
            _rollback_failed_creation(handles)
            # ent#313: and the container + its name-keyed Redis state, which the
            # DB/quota rollback above deliberately left to a watchdog that only
            # covers ephemeral agents.
            await _reclaim_failed_creation_container(handles, created_container, e)
            # trinity-enterprise#15: AFTER the container reclaim, so the volume
            # this attempt pre-populated is no longer mounted and its removal
            # keeps the #1667 guard from 409ing the retry.
            _cleanup_copy_artifacts(handles)
            logger.error(f"Failed to create agent {config.name}: {e}")
            raise HTTPException(status_code=500, detail="Failed to create agent. Please try again.")
    else:
        _release_ephemeral_on_no_docker(handles)
        raise HTTPException(
            status_code=503,
            detail="Docker not available - cannot create agents in demo mode"
        )
