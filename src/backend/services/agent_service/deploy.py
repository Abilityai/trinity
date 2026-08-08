"""
Agent Service Deploy - Local agent deployment logic.

Contains the business logic for deploying local agents via MCP.
"""
import base64
import hashlib
import json
import os
import tarfile
import tempfile
import shutil
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from io import BytesIO
from typing import Any, List, NamedTuple, Optional, Tuple

import docker
from fastapi import HTTPException, Request

from models import (
    AgentConfig,
    AgentStatus,
    User,
    DeployLocalRequest,
    DeployLocalResponse,
    DeployManifestEntry,
    VersioningInfo,
    MAX_DEPLOY_CREDENTIALS,
)
from database import db
from services.template_service import (
    is_trinity_compatible,
    collect_mcp_credential_warnings,
)
from services.docker_service import get_agent_container
from services.docker_utils import container_stop, container_start
from utils.helpers import sanitize_agent_name
from services.settings_service import get_agent_quota_for_role
from services.mcp_validator import validate_mcp_config, McpValidationError
from .helpers import get_agents_by_prefix, get_next_version_name, get_latest_version

logger = logging.getLogger(__name__)

# Size limits for local deployment
MAX_ARCHIVE_SIZE = 50 * 1024 * 1024  # 50 MB (compressed; ~67 MB as base64 JSON body)
# #2060: 1000 → 10000. A Cornelius-class KB agent (vault + skills + memory)
# exceeds 1000 members easily; per-member validation is O(n) trivial and the
# byte caps are the true resource bound.
MAX_FILES = 10000
# #2060: sum of member header sizes, checked pre-extraction. Closes the
# gzip-bomb hole (a 50 MB gzip can decompress to ~51 GB onto backend disk).
MAX_EXTRACTED_SIZE = 500 * 1024 * 1024  # 500 MB

# #2060 embedded integrity manifest — computed by the caller FROM THE DISK
# TREE and shipped inside the archive as an ordinary member, so a tar-level
# `--exclude` cannot prune the manifest's content and every accidental
# truncation/exclusion class drifts loudly. (A request-field manifest would
# ride the same token-bound model turn as the archive and recreate the bug
# one level up.)
MANIFEST_FILE_NAME = ".trinity-manifest.json"
MAX_MANIFEST_BYTES = 5 * 1024 * 1024  # memory-exhaustion guard on the read
_MANIFEST_DRIFT_LIST_CAP = 50  # per-list path cap in the 400 detail
_MANIFEST_MAX_PATH_CHARS = 1024

# Per-base-name deploy serialization (#2060): two concurrent deploys of one
# base name would compute the same next-version name. SETNX + TTL, fail-open
# on Redis down (the attached-volume 409 in prepop is the backstop).
# Registered in agent_runtime_state.EXEMPT_KEYSPACES.
_DEPLOY_LOCK_TTL_S = 600

MANIFEST_GENERATION_SNIPPET = (
    "Run from the agent directory BEFORE creating the tar (same excludes):\n"
    "python3 - <<'PY'\n"
    "import hashlib, json, os\n"
    "EXCLUDE_DIRS = {'.git', 'node_modules', '__pycache__', '.venv'}\n"
    "entries = []\n"
    "for dirpath, dirnames, filenames in os.walk('.'):\n"
    "    dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]\n"
    "    for d in list(dirnames):\n"
    "        p = os.path.join(dirpath, d)\n"
    "        if os.path.islink(p):\n"
    "            entries.append({'path': os.path.relpath(p, '.'), 'link_target': os.readlink(p)})\n"
    "            dirnames.remove(d)\n"
    "    for f in filenames:\n"
    "        p = os.path.join(dirpath, f)\n"
    "        rel = os.path.relpath(p, '.')\n"
    "        if rel == '.trinity-manifest.json' or f.startswith('._'):\n"
    "            continue\n"
    "        if os.path.islink(p):\n"
    "            entries.append({'path': rel, 'link_target': os.readlink(p)})\n"
    "        elif os.path.isfile(p):\n"
    "            entries.append({'path': rel, 'sha256': hashlib.sha256(open(p, 'rb').read()).hexdigest()})\n"
    "with open('.trinity-manifest.json', 'w') as fh:\n"
    "    json.dump(entries, fh)\n"
    "PY"
)

# Container-side path for deployed-local templates (#950). Sits under
# /data which is host-bound to TRINITY_DATA_PATH (default ./trinity-data),
# writable, and owned by UID 1000 — separate from the curated catalog at
# /agent-configs/templates which is intentionally read-only.
DEPLOYED_TEMPLATES_DIR_IN_BACKEND = "/data/deployed-templates"

# Image used by the workspace pre-population transient container (#950).
# Pinned to a specific tag so a Docker Hub `latest` swap can't silently
# change deploy behavior.
_PREPOP_IMAGE = "alpine:3.20"


def _validate_archive_mcp_config(mcp_file: Path, version_name: str,
                                 actor_email: str | None) -> None:
    """Structure-validate an archive-supplied `.mcp.json` (ent#213).

    The `deploy-local` path used to copy an archive `.mcp.json` into the
    workspace WITHOUT validating it, while the post-deploy inject path
    (`routers/credentials.py`) does. Claude Code auto-loads `~/.mcp.json` via
    `--mcp-config` on the next chat/headless execution, so an unvalidated
    archive config was an ingress the inject-path guard (#598, AISEC-C2 Layer 2)
    never covered — command substitution, shell metacharacters, reserved
    env-ref overrides (LD_PRELOAD/PATH/…), oversize, unknown fields.

    Same validator (`services/mcp_validator.validate_mcp_config`) and same 400
    shape as the inject path, so the two ingresses now agree. A valid config —
    including the canonical auto-injected `trinity` entry — passes unchanged.
    """
    try:
        validate_mcp_config(mcp_file.read_text())
    except McpValidationError as e:
        logger.warning(
            "deploy-local blocked by .mcp.json validator for %s by %s: %s",
            version_name, actor_email or "?", e,
        )
        raise HTTPException(
            status_code=400,
            detail=f"Invalid .mcp.json in archive: {e}",
        )


def _remove_partial_deploy(dest_created: Path | None) -> None:
    """Remove a deployed-templates directory a failed deploy left behind (#2006).

    Second layer under the moved `.mcp.json` gate: that gate now runs before
    the copy, but every OTHER failure between `copytree` and `create_agent_fn`
    (`_prepopulate_workspace_from_template`'s 500, a docker outage) leaves the
    same addressable residue, because `/data/deployed-templates` is a member of
    `_LOCAL_TEMPLATE_ROOTS` and the directory name is exactly the
    `local:<version_name>` id the caller can pass to `POST /api/agents`.

    Deliberately NOT a blanket cleanup in `finally`: the caller clears its
    handle before creation starts, so a directory a container may already
    reference is never removed. Never raises — this runs on an error path and
    must not replace the real failure with a cleanup failure.

    The containment check is repeated HERE rather than inherited from the
    caller's #950 guard. `rmtree` is a destructive sink whose path descends
    from a caller-supplied name, and "the caller already validated it" is a
    property that survives exactly until someone adds a second call site. It is
    also the barrier CodeQL recognizes (normalize → prefix-check → use the
    normalized value), which is why the flagged alert was fixed rather than
    dismissed: on a `rmtree`, "probably confined" is not the standard.
    """
    if dest_created is None:
        return

    base = os.path.normpath(DEPLOYED_TEMPLATES_DIR_IN_BACKEND)
    target = os.path.normpath(str(dest_created))
    if not target.startswith(base + os.sep) or target == base:
        logger.error(
            "refusing to remove a partial deploy outside the deployed-templates "
            "directory: %s", target,
        )
        return

    try:
        shutil.rmtree(target)
        logger.info("Removed partial deploy directory: %s", target)
    except Exception as e:  # noqa: BLE001 — the original error is what matters
        logger.warning(
            "could not remove partial deploy directory %s: %s", dest_created, e
        )


# =============================================================================
# Deploy lock (#2060) — per-base-name SETNX serialization
# =============================================================================

class _DeployLock(NamedTuple):
    client: Any  # None = fail-open (nothing to release)
    key: str
    token: str


def _deploy_lock_client():
    """The Redis client for the deploy lock; None = fail-open."""
    try:
        from routers.auth import get_redis_client
        return get_redis_client()
    except Exception:  # pragma: no cover — defensive
        return None


def _acquire_deploy_lock(base_name: str) -> _DeployLock:
    """Serialize deploys per base name across uvicorn workers (#2060).

    Two concurrent deploys of one base name both compute the same
    `get_next_version_name` and collide on the template dir + workspace
    volume. SETNX with a TTL backstop; **fail-open** when Redis is down (a
    flaky lock layer must never block a real deploy — the attached-volume 409
    in prepop is the destructive-collision backstop). 409 on contention.
    """
    key = f"agent:deploy_op:{base_name}"
    token = uuid.uuid4().hex
    client = _deploy_lock_client()
    if client is None:
        return _DeployLock(None, key, token)
    try:
        held = bool(client.set(key, token, nx=True, ex=_DEPLOY_LOCK_TTL_S))
    except Exception:
        return _DeployLock(None, key, token)
    if not held:
        raise HTTPException(
            status_code=409,
            detail={
                "error": (
                    f"A deploy of '{base_name}' is already in progress. "
                    f"Wait for it to finish (or up to {_DEPLOY_LOCK_TTL_S}s) "
                    f"and retry."
                ),
                "code": "DEPLOY_IN_PROGRESS",
            },
        )
    return _DeployLock(client, key, token)


def _release_deploy_lock(lock: Optional[_DeployLock]) -> None:
    """Best-effort ownership-checked release; the TTL backstops."""
    if lock is None or lock.client is None:
        return
    try:
        if lock.client.get(lock.key) == lock.token:
            lock.client.delete(lock.key)
    except Exception:  # pragma: no cover — defensive
        logger.debug("deploy lock release failed for %s", lock.key, exc_info=True)


# =============================================================================
# Integrity manifest (#2060)
# =============================================================================

@dataclass
class ManifestDrift:
    """Result of verifying a tree against the embedded manifest."""
    missing: List[str] = field(default_factory=list)
    altered: List[str] = field(default_factory=list)
    extra: List[str] = field(default_factory=list)
    link_mismatch: List[str] = field(default_factory=list)

    def any(self) -> bool:
        return bool(self.missing or self.altered or self.extra or self.link_mismatch)


def _manifest_invalid(reason: str) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={
            "error": f"Invalid {MANIFEST_FILE_NAME}: {reason}",
            "code": "MANIFEST_INVALID",
        },
    )


def _load_manifest(root: Path) -> Optional[List[DeployManifestEntry]]:
    """Parse + bound-check the embedded manifest; None when absent.

    Bounds (each a named 400 MANIFEST_INVALID): read cap MAX_MANIFEST_BYTES,
    ≤ MAX_FILES entries, path ≤ 1024 chars, no duplicates, no absolute/`..`
    paths, exactly one of sha256/link_target per entry, never lists itself.
    """
    manifest_file = root / MANIFEST_FILE_NAME
    if not manifest_file.is_file():
        return None
    try:
        size = manifest_file.stat().st_size
    except OSError as e:
        raise _manifest_invalid(f"unreadable: {e}")
    if size > MAX_MANIFEST_BYTES:
        raise _manifest_invalid(
            f"file is {size} bytes, exceeding the {MAX_MANIFEST_BYTES} byte cap"
        )
    try:
        data = json.loads(manifest_file.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise _manifest_invalid(f"not valid JSON ({e})")
    if not isinstance(data, list):
        raise _manifest_invalid("top level must be a JSON array of entries")
    if len(data) > MAX_FILES:
        raise _manifest_invalid(
            f"{len(data)} entries exceed the {MAX_FILES} entry cap"
        )

    entries: List[DeployManifestEntry] = []
    seen: set = set()
    for raw in data:
        if not isinstance(raw, dict):
            raise _manifest_invalid("every entry must be an object")
        try:
            entry = DeployManifestEntry(**raw)
        except Exception:
            raise _manifest_invalid(f"malformed entry: {raw!r:.200}")
        p = entry.path
        if not p or len(p) > _MANIFEST_MAX_PATH_CHARS:
            raise _manifest_invalid("empty or over-long path")
        if p.startswith("/") or ".." in p.split("/"):
            raise _manifest_invalid(f"absolute or traversal path not allowed: {p}")
        if p == MANIFEST_FILE_NAME:
            raise _manifest_invalid(
                "the manifest must not list itself (it cannot self-hash)"
            )
        if p in seen:
            raise _manifest_invalid(f"duplicate path: {p}")
        if bool(entry.sha256) == bool(entry.link_target):
            raise _manifest_invalid(
                f"entry for {p} must carry exactly one of sha256 (file) or "
                f"link_target (symlink)"
            )
        seen.add(p)
        entries.append(entry)
    return entries


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _walk_tree_entries(root: Path):
    """Yield (relpath, full_path) for every non-directory entry: regular
    files, symlinks to anything (walk does not follow links)."""
    for dirpath, dirnames, filenames in os.walk(root):
        # Symlinks-to-dirs sit in dirnames (not descended, followlinks=False);
        # surface them as entries and prune the descent explicitly.
        for d in list(dirnames):
            full = Path(dirpath) / d
            if full.is_symlink():
                yield full.relative_to(root).as_posix(), full
                dirnames.remove(d)
        for f in filenames:
            full = Path(dirpath) / f
            yield full.relative_to(root).as_posix(), full


def _verify_manifest(
    root: Path, entries: List[DeployManifestEntry], *, check_extras: bool
) -> ManifestDrift:
    """Pure drift check of a tree against the manifest (#2060).

    Runs post-extract (check_extras=True — a member the manifest does not
    list is drift too) and post-copy (check_extras=False — copytree cannot
    invent files). NEVER a security layer: containment/link validation runs
    pre-extraction in `_validate_tar_member` (layering rule).
    """
    drift = ManifestDrift()
    manifest_paths = set()
    for entry in entries:
        manifest_paths.add(entry.path)
        target = root / entry.path
        if entry.link_target is not None:
            if not os.path.lexists(target):
                drift.missing.append(entry.path)
            elif not target.is_symlink():
                drift.link_mismatch.append(entry.path)
            elif os.readlink(target) != entry.link_target:
                drift.link_mismatch.append(entry.path)
        else:
            if not os.path.lexists(target):
                drift.missing.append(entry.path)
            elif target.is_symlink() or not target.is_file():
                drift.altered.append(entry.path)
            elif _sha256_file(target) != entry.sha256:
                drift.altered.append(entry.path)
    if check_extras:
        for rel, _full in _walk_tree_entries(root):
            if rel == MANIFEST_FILE_NAME:
                continue
            if rel not in manifest_paths:
                drift.extra.append(rel)
    return drift


def _raise_manifest_drift(drift: ManifestDrift, stage: str) -> None:
    def cap(lst: List[str]) -> List[str]:
        return sorted(lst)[:_MANIFEST_DRIFT_LIST_CAP]

    named = ", ".join(
        (cap(drift.missing) + cap(drift.altered) + cap(drift.extra)
         + cap(drift.link_mismatch))[:10]
    )
    # Recovery text deliberately directs at rebuild-without-excludes / the
    # CLI — NEVER at excluding entries from the manifest, which would teach
    # the consistent two-command prune this contract exists to catch.
    raise HTTPException(
        status_code=400,
        detail={
            "error": (
                f"Archive content does not match its embedded manifest "
                f"({stage}): {len(drift.missing)} missing, "
                f"{len(drift.altered)} altered, {len(drift.extra)} unexpected, "
                f"{len(drift.link_mismatch)} link mismatch(es) — e.g. {named}. "
                f"Rebuild the archive WITHOUT extra excludes and regenerate "
                f"{MANIFEST_FILE_NAME} from the full agent directory, or "
                f"deploy with the `trinity` CLI for large agents."
            ),
            "code": "MANIFEST_DRIFT",
            "stage": stage,
            "missing": cap(drift.missing),
            "missing_count": len(drift.missing),
            "altered": cap(drift.altered),
            "altered_count": len(drift.altered),
            "extra": cap(drift.extra),
            "extra_count": len(drift.extra),
            "link_mismatch": cap(drift.link_mismatch),
            "link_mismatch_count": len(drift.link_mismatch),
        },
    )


def _collect_dangling_symlinks(root: Path) -> List[Tuple[str, str]]:
    """In-root symlinks whose target does not (yet) exist — legitimate for
    runtime-created dirs (`content/`, `data/`), preserved with a warning."""
    out = []
    for rel, full in _walk_tree_entries(root):
        if full.is_symlink() and not full.exists():
            try:
                out.append((rel, os.readlink(full)))
            except OSError:  # pragma: no cover — racy unlink
                out.append((rel, "?"))
    return sorted(out)


def _count_deployed(root: Path) -> Tuple[int, int]:
    """(regular files excluding the manifest member, symlinks) under root."""
    files = 0
    symlinks = 0
    for rel, full in _walk_tree_entries(root):
        if full.is_symlink():
            symlinks += 1
        elif rel != MANIFEST_FILE_NAME and full.is_file():
            files += 1
    return files, symlinks


async def _compatibility_hard_count(version_name: str) -> Optional[int]:
    """Post-deploy #668 STATIC-only evidence (#2060). None = unavailable.

    Lazy import: `services.compatibility` pulls the collector/AI stack, which
    deploy must not need at import time. `include_ai=False` — deterministic,
    free, no token spend. An `unavailable` report yields None, never 0:
    absent evidence is not zero findings.
    """
    from services.compatibility import build_report
    report = await build_report(version_name, include_ai=False)
    if report.get("overall_status") == "unavailable":
        return None
    return report.get("hard_count")


# =============================================================================
# Workspace-volume hygiene (#2060 S6)
# =============================================================================

def _attached_volume_names_sync(client) -> Optional[set]:
    """Names of every named volume mounted by ANY container; None = unknown.

    Sync sibling of docker_utils.list_attached_volume_names (this module's
    prepop path is synchronous). Fail-closed: callers must treat None as
    "cannot establish", never as "unattached" (#1664 lesson).
    """
    try:
        names = set()
        for container in client.containers.list(all=True):
            for mount in (container.attrs.get("Mounts") or []):
                if mount.get("Type") == "volume" and mount.get("Name"):
                    names.add(mount["Name"])
        return names
    except Exception as e:
        logger.warning(f"[#2060] listing attached volumes failed: {e}")
        return None


def _ensure_fresh_workspace_volume(client, workspace_vol: str, version_name: str) -> None:
    """A pre-existing volume under the NEW version name is a failed/concurrent
    deploy's leftover (by construction no ownership row exists yet).

    Unattached → remove-and-recreate (put_archive overlays, never prunes —
    reusing it would let stale files from the failed attempt survive into
    this deploy's workspace). Attached (or unknowable) → 409, never overlay
    into a mounted volume: it means a concurrent/zombie deploy.
    """
    try:
        existing = client.volumes.get(workspace_vol)
    except docker.errors.NotFound:
        existing = None

    if existing is not None:
        attached = _attached_volume_names_sync(client)
        if attached is None or workspace_vol in attached:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": (
                        f"Workspace volume {workspace_vol} already exists and "
                        f"is attached to a container (or attachment could not "
                        f"be established) — a concurrent or interrupted deploy "
                        f"of {version_name} may be in progress. Wait or remove "
                        f"the container before retrying."
                    ),
                    "code": "WORKSPACE_VOLUME_IN_USE",
                    "volume": workspace_vol,
                },
            )
        try:
            existing.remove(force=True)
            logger.info(
                f"[#2060] removed stale workspace volume {workspace_vol} "
                f"before re-prepopulation"
            )
        except docker.errors.APIError as e:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": (
                        f"Stale workspace volume {workspace_vol} could not be "
                        f"removed: {e}"
                    ),
                    "code": "WORKSPACE_VOLUME_IN_USE",
                    "volume": workspace_vol,
                },
            )

    client.volumes.create(
        name=workspace_vol,
        labels={
            "trinity.platform": "agent-workspace",
            "trinity.agent-name": version_name,
        },
    )


def _cleanup_deploy_volume(volume_name: Optional[str]) -> None:
    """Best-effort removal of the workspace volume a FAILED deploy
    prepopulated (#2060 S6; the #1581 double-guard shape). Never raises.

    Only removes when the platform label matches AND the volume is provably
    unattached — after a create_agent_fn failure, crud rollback + ent#313
    reclaim remove the failed container first, so an attached volume here
    means someone else's live data (skip; the #1581 orphan sweep backstops).
    """
    if not volume_name:
        return
    try:
        client = docker.from_env()
        try:
            vol = client.volumes.get(volume_name)
        except docker.errors.NotFound:
            return
        labels = (vol.attrs or {}).get("Labels") or {}
        if labels.get("trinity.platform") != "agent-workspace":
            logger.error(
                f"[#2060] refusing to clean up volume {volume_name}: not an "
                f"agent-workspace volume (label mismatch)"
            )
            return
        attached = _attached_volume_names_sync(client)
        if attached is None or volume_name in attached:
            logger.warning(
                f"[#2060] leaving workspace volume {volume_name} for the "
                f"orphan sweep: attached or attachment unknown"
            )
            return
        vol.remove(force=True)
        logger.info(f"[#2060] removed workspace volume {volume_name} after failed deploy")
    except Exception as e:  # noqa: BLE001 — error path, must not mask the failure
        logger.warning(f"[#2060] could not clean up workspace volume {volume_name}: {e}")


async def _restart_stopped_previous(previous_name: Optional[str]) -> None:
    """Compensation (#2060 S6): a deploy that stopped the previous version
    and then failed restarts it, best-effort — including when create_agent_fn
    raised (crud rollback + ent#313 reclaim remove the failed new container,
    so the restart cannot conflict). Log-only on failure, never masks the
    original error."""
    if not previous_name:
        return
    try:
        container = get_agent_container(previous_name)
        if container:
            await container_start(container)
            logger.info(
                f"[#2060] restarted previous version {previous_name} after failed deploy"
            )
    except Exception as e:  # noqa: BLE001 — error path, must not mask the failure
        logger.warning(
            f"[#2060] could not restart previous version {previous_name} "
            f"after failed deploy: {e}"
        )


def _prepopulate_workspace_from_template(version_name: str, template_dir: Path) -> None:
    """Pre-populate `agent-{version_name}-workspace` with the template files (#950).

    Creates (or reuses) the docker named volume that the agent container
    will mount at `/home/developer`, then copies the extracted template
    contents into it via an ephemeral alpine container (`put_archive`).
    A `.trinity-initialized` marker is included in the same tar so the
    agent's `startup.sh` skips its `/template`→`/home/developer` copy on
    boot. This bypasses the host-path bind-mount transport that was
    inconsistent between dev (named volume `/data`) and prod (host bind
    `/data`) compose files.

    Failures raise HTTPException(500) — partial pre-population would
    leave the deploy in an inconsistent state.
    """
    # #1665 audit: naming off `version_name` is correct HERE by construction —
    # a deploy version is a brand-new name whose ownership row doesn't exist
    # yet (this runs before creation), so there is no pin to resolve and no
    # rename in its past. Every OTHER "this agent's volume" lookup must go
    # through `db.get_volume_base_name` (see lifecycle._workspace_volume_name).
    workspace_vol = f"agent-{version_name}-workspace"
    client = docker.from_env()

    # #2060: a pre-existing volume under the new version name is a failed or
    # concurrent deploy's leftover — remove-and-recreate when unattached,
    # 409 when attached (never put_archive into a mounted volume).
    _ensure_fresh_workspace_volume(client, workspace_vol, version_name)

    # Stream template + .trinity-initialized marker into the volume.
    # Both files captured under uid=1000/gid=1000 so the agent container
    # (running as `developer`, UID 1000 per #874) can read & write them.
    #
    # Disk-spooled, not BytesIO (review #2040 F2): this primitive was written
    # for operator-bounded deploy-local templates, but the copy-intent import
    # points it at user-specified GitHub content — materializing the whole
    # tree as one in-memory tar would let a large repo OOM the backend
    # process, which serves every other agent. The spool file lives beside
    # the template tree (disk-backed /data in both callers — NEVER inside
    # `template_dir`, which would tar the growing tar into itself; and never
    # the default tmp, which is a small RAM tmpfs, #1098). requests streams a
    # file object, so put_archive never holds the full tar in memory either.
    def _set_owner(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = 1000
        info.gid = 1000
        info.uname = "developer"
        info.gname = "developer"
        return info

    tar_spool = tempfile.TemporaryFile(dir=str(Path(template_dir).parent))
    try:
        with tarfile.open(fileobj=tar_spool, mode="w") as tar:
            tar.add(str(template_dir), arcname=".", filter=_set_owner)

            marker = tarfile.TarInfo(name=".trinity-initialized")
            marker.size = 0
            marker.uid = 1000
            marker.gid = 1000
            marker.uname = "developer"
            marker.gname = "developer"
            tar.addfile(marker, BytesIO(b""))
        tar_spool.seek(0)
    except Exception:
        tar_spool.close()
        raise

    transient = None
    try:
        # Auto-pull the image if it isn't already present on the daemon
        # (docker SDK's `containers.create` doesn't pull, unlike `run`).
        try:
            client.images.get(_PREPOP_IMAGE)
        except docker.errors.ImageNotFound:
            logger.info(f"Pulling {_PREPOP_IMAGE} for workspace pre-pop")
            client.images.pull(_PREPOP_IMAGE)
        transient = client.containers.create(
            _PREPOP_IMAGE,
            # Chown the volume root after put_archive — Docker creates new
            # volumes root-owned, and put_archive only sets ownership on
            # the entries inside, not on the mount point itself. Without
            # this, the agent (UID 1000) can't write to /home/developer.
            command=["sh", "-c", "chown 1000:1000 /dest && chmod 755 /dest"],
            volumes={workspace_vol: {"bind": "/dest", "mode": "rw"}},
        )
        ok = transient.put_archive("/dest", tar_spool)
        if not ok:
            raise RuntimeError("put_archive returned False")
        transient.start()
        result = transient.wait(timeout=30)
        if result.get("StatusCode", 1) != 0:
            log_tail = transient.logs(tail=20).decode(errors="replace")
            raise RuntimeError(
                f"chown step failed (exit {result.get('StatusCode')}): {log_tail}"
            )
        logger.info(
            f"Pre-populated workspace volume {workspace_vol} from {template_dir}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={
                "error": f"Failed to pre-populate workspace for {version_name}: {e}",
                "code": "WORKSPACE_PREPOP_FAILED",
            },
        )
    finally:
        tar_spool.close()
        if transient is not None:
            try:
                transient.remove(force=True)
            except Exception:
                pass


# =============================================================================
# Safe Tar Extraction Utilities
# =============================================================================

def _is_path_within(base: Path, target: Path) -> bool:
    """
    Check if target path is within base directory.
    
    Uses Path.resolve() to handle symlinks and normalize paths.
    Returns True if target is inside base (or is base itself).
    """
    try:
        # resolve() normalizes the path and resolves symlinks
        # We use strict=False because target may not exist yet during extraction
        base_resolved = base.resolve()
        target_resolved = target.resolve()
        
        # Check if target starts with base path
        return str(target_resolved).startswith(str(base_resolved) + "/") or \
               target_resolved == base_resolved
    except (OSError, ValueError):
        return False


def _validate_tar_member(member: tarfile.TarInfo, base_dir: Path) -> tuple[bool, str]:
    """
    Validate a tar archive member for safe extraction.
    
    Checks:
    - Destination path stays within base_dir
    - No absolute paths
    - Symlinks/hardlinks only point within base_dir
    - No special file types (devices, FIFOs)
    
    Args:
        member: The tar archive member to validate
        base_dir: The base directory for extraction
        
    Returns:
        Tuple of (is_valid, error_message). If valid, error_message is empty.
    """
    member_name = member.name
    
    # Reject absolute paths
    if member_name.startswith('/'):
        return False, f"Absolute path not allowed: {member_name}"
    
    # Reject path traversal in member name
    if '..' in member_name.split('/'):
        return False, f"Path traversal not allowed: {member_name}"
    
    # Calculate the destination path
    dest_path = base_dir / member_name
    
    # Verify destination stays within base_dir
    if not _is_path_within(base_dir, dest_path):
        return False, f"Path escapes extraction directory: {member_name}"
    
    # Reject special file types (devices, FIFOs)
    if member.ischr() or member.isblk():
        return False, f"Device files not allowed: {member_name}"
    if member.isfifo():
        return False, f"FIFO files not allowed: {member_name}"
    
    # Validate symlinks
    if member.issym():
        linkname = member.linkname
        
        # Reject absolute symlink targets
        if linkname.startswith('/'):
            return False, f"Absolute symlink target not allowed: {member_name} -> {linkname}"
        
        # Calculate where the symlink would point
        # Symlink is relative to the directory containing it
        link_dir = dest_path.parent
        link_target = (link_dir / linkname).resolve()
        
        # Verify symlink target stays within base_dir
        if not _is_path_within(base_dir, link_target):
            return False, f"Symlink escapes extraction directory: {member_name} -> {linkname}"
    
    # Validate hardlinks
    if member.islnk():
        linkname = member.linkname
        
        # Reject absolute hardlink targets
        if linkname.startswith('/'):
            return False, f"Absolute hardlink target not allowed: {member_name} -> {linkname}"
        
        # Hardlink target is relative to archive root (base_dir)
        link_target = base_dir / linkname
        
        # Verify hardlink target stays within base_dir
        if not _is_path_within(base_dir, link_target):
            return False, f"Hardlink escapes extraction directory: {member_name} -> {linkname}"
    
    return True, ""


def _safe_extract_tar(tar: tarfile.TarFile, dest_dir: Path, max_files: int) -> int:
    """
    Safely extract a tar archive with full validation.

    Validates all members before extraction and raises HTTPException
    if any member fails validation.

    Args:
        tar: Open tarfile object
        dest_dir: Destination directory for extraction
        max_files: Maximum number of files allowed

    Returns:
        Number of skipped macOS AppleDouble (``._*``) members (#2060) — the
        caller surfaces them as a warning. Skipping (vs extracting) keeps them
        out of the workspace AND out of the manifest-extras check.

    Raises:
        HTTPException: If archive validation fails
    """
    members = tar.getmembers()

    # #2060: macOS `tar` without COPYFILE_DISABLE=1 emits an AppleDouble
    # `._name` sidecar per file — metadata pollution, never agent content.
    kept = []
    appledouble_skipped = 0
    for member in members:
        if os.path.basename(member.name.rstrip("/")).startswith("._"):
            appledouble_skipped += 1
            continue
        kept.append(member)

    # Check file count (#2060: observed + limit in the detail)
    if len(kept) > max_files:
        raise HTTPException(
            status_code=400,
            detail={
                "error": (
                    f"Archive exceeds maximum file count of {max_files} "
                    f"({len(kept)} members)"
                ),
                "code": "TOO_MANY_FILES",
                "observed": len(kept),
                "limit": max_files,
            }
        )

    # #2060: decompressed-size cap from member headers, pre-extraction —
    # closes the gzip-bomb hole (the 50 MB cap bounds the COMPRESSED size).
    total_size = sum(max(m.size, 0) for m in kept)
    if total_size > MAX_EXTRACTED_SIZE:
        raise HTTPException(
            status_code=400,
            detail={
                "error": (
                    f"Archive would extract to {total_size} bytes, exceeding "
                    f"the {MAX_EXTRACTED_SIZE} byte limit"
                ),
                "code": "ARCHIVE_EXTRACTED_TOO_LARGE",
                "observed": total_size,
                "limit": MAX_EXTRACTED_SIZE,
            }
        )

    # SECURITY LAYER — validate all members BEFORE any extraction (#2060
    # layering rule: containment/link/member-type checks are pre-extraction by
    # contract; only manifest drift verification runs post-extract. Moving
    # this after extractall would reopen the tar-slip class.)
    safe_members = []
    for member in kept:
        is_valid, error_msg = _validate_tar_member(member, dest_dir)
        if not is_valid:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": f"Invalid archive: {error_msg}",
                    "code": "INVALID_ARCHIVE"
                }
            )
        safe_members.append(member)

    # Extract validated members. filter='tar' pinned deliberately (#2060):
    # Py3.14 flips the unpinned default to 'data', which re-adjudicates link
    # targets with different edge behavior — `_validate_tar_member` stays the
    # single authoritative link barrier and the filter is defense-in-depth
    # (it also strips setuid/setgid/sticky bits, a hardening win).
    tar.extractall(dest_dir, members=safe_members, filter='tar')
    return appledouble_skipped


async def deploy_local_agent_logic(
    body: DeployLocalRequest,
    current_user: User,
    request: Request,
    create_agent_fn
) -> DeployLocalResponse:
    """
    Deploy a Trinity-compatible local agent.

    This receives a base64-encoded tar.gz archive of a local agent
    directory, validates it's Trinity-compatible (has template.yaml), handles
    versioning if an agent with the same name exists, and creates the agent.

    Credentials should be included in the archive (.env file) — no
    separate credential injection step.

    Args:
        body: Deploy request with archive
        current_user: Authenticated user
        request: FastAPI request object
        create_agent_fn: Function to create agent (create_agent_internal)

    Returns:
        DeployLocalResponse with deployment details
    """
    temp_dir = None
    # #2006: the deployed-templates dir this call created, while it is still
    # this call's to remove. Cleared just before `create_agent_fn` — once
    # creation starts, the directory may be referenced by a container mount
    # spec, so removing it on a late failure would break a half-created agent
    # rather than clean up after one.
    dest_created = None
    # #2060 S6 compensation state. `volume_created` deliberately stays set
    # ACROSS create_agent_fn (unlike dest_created): the cleanup's
    # label + unattached double-guard supplies the safety — after a create
    # failure, crud rollback + ent#313 reclaim remove the failed container,
    # making the volume provably unattached and removable; while a container
    # mounts it, the guard refuses.
    volume_created = None
    previous_stopped_name = None
    deploy_lock = None

    try:
        # 1. Validate archive size
        try:
            archive_bytes = base64.b64decode(body.archive)
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": f"Invalid base64 encoding: {e}",
                    "code": "INVALID_ARCHIVE"
                }
            )

        if len(archive_bytes) > MAX_ARCHIVE_SIZE:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": (
                        f"Archive exceeds maximum size of "
                        f"{MAX_ARCHIVE_SIZE // (1024*1024)}MB "
                        f"({len(archive_bytes)} bytes received)"
                    ),
                    "code": "ARCHIVE_TOO_LARGE",
                    "observed": len(archive_bytes),
                    "limit": MAX_ARCHIVE_SIZE,
                }
            )

        # 1b. Validate credential count limit
        if body.credentials and len(body.credentials) > MAX_DEPLOY_CREDENTIALS:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": f"Too many credentials: {len(body.credentials)} exceeds limit of {MAX_DEPLOY_CREDENTIALS}",
                    "code": "TOO_MANY_CREDENTIALS"
                }
            )

        # 2. Extract archive to temp directory
        temp_dir = Path(tempfile.mkdtemp(prefix="trinity-deploy-"))
        try:
            with tarfile.open(fileobj=BytesIO(archive_bytes), mode='r:gz') as tar:
                # Security: Safe extraction with full validation
                # - Validates paths stay within temp_dir
                # - Blocks symlinks/hardlinks pointing outside
                # - Rejects device files and FIFOs
                appledouble_skipped = _safe_extract_tar(tar, temp_dir, MAX_FILES)
        except tarfile.TarError as e:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": f"Invalid tar.gz archive: {e}",
                    "code": "INVALID_ARCHIVE"
                }
            )

        # 4. Find the root directory (handle nested extraction)
        contents = list(temp_dir.iterdir())
        if len(contents) == 1 and contents[0].is_dir():
            extract_root = contents[0]
        else:
            extract_root = temp_dir

        # 4a. Integrity manifest (#2060) — parse + POST-EXTRACT verification,
        # strictly before any side effect (no version computed, no previous
        # agent stopped, nothing persisted — the #2006 gate-ordering rule).
        # This is the drift layer; the SECURITY layer already ran inside
        # `_safe_extract_tar`, pre-extraction (layering rule).
        deploy_warnings: list = []
        if appledouble_skipped:
            deploy_warnings.append(
                f"Skipped {appledouble_skipped} macOS AppleDouble member(s) "
                f"('._*') from the archive — package with COPYFILE_DISABLE=1 "
                f"to avoid them."
            )

        manifest_entries = _load_manifest(extract_root)
        if bool(getattr(body, "require_manifest", False)) and manifest_entries is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": (
                        f"require_manifest is set but the archive carries no "
                        f"{MANIFEST_FILE_NAME}. Generate it from the FULL agent "
                        f"directory and rebuild the archive."
                    ),
                    "code": "MANIFEST_REQUIRED",
                    "generate_with": MANIFEST_GENERATION_SNIPPET,
                },
            )
        if manifest_entries is not None:
            drift = _verify_manifest(extract_root, manifest_entries, check_extras=True)
            if drift.any():
                _raise_manifest_drift(drift, "post-extract")
        else:
            deploy_warnings.append(
                f"No {MANIFEST_FILE_NAME} in the archive — deployed content "
                f"was NOT integrity-verified (verified: false). Embed a "
                f"manifest to enable verification."
            )
        for rel, link_target in _collect_dangling_symlinks(extract_root):
            deploy_warnings.append(
                f"dangling symlink preserved: {rel} -> {link_target}"
            )

        # 5. Validate Trinity-compatible
        is_compatible, error_msg, template_data = is_trinity_compatible(extract_root)
        if not is_compatible:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": f"Agent is not Trinity-compatible: {error_msg}",
                    "code": "NOT_TRINITY_COMPATIBLE"
                }
            )

        # 6. Determine agent name
        base_name = body.name or template_data.get("name")
        if not base_name:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "No agent name specified and template.yaml has no name field",
                    "code": "MISSING_NAME"
                }
            )

        base_name = sanitize_agent_name(base_name)

        # 6a. Structure-validate an archive-supplied `.mcp.json` (ent#213) —
        # on the EXTRACTED copy, BEFORE anything is persisted or stopped
        # (#2006). The guard used to run 32 lines after `copytree`, so a 400
        # aborted the deploy while leaving the rejected config on disk under
        # `/data/deployed-templates/<version_name>/` — a live member of
        # `_LOCAL_TEMPLATE_ROOTS`, therefore reachable by a subsequent
        # `POST /api/agents {"template": "local:<version_name>"}`. A guard that
        # runs after the persist is not a gate.
        #
        # `temp_dir` is the only thing written before this point and the
        # `finally` already removes it, so a rejection now leaves nothing
        # behind. Running here also precedes the quota check and the
        # stop-previous-version step, so a deploy that will be refused no
        # longer stops a running agent on its way to the refusal.
        archive_mcp_file = extract_root / ".mcp.json"
        if archive_mcp_file.exists():
            _validate_archive_mcp_config(
                archive_mcp_file, base_name, getattr(current_user, "email", None)
            )

        # 6b. Agent quota enforcement: per-role limits (QUOTA-001)
        # Skip for redeploys of existing agents owned by this user
        existing_versions = get_agents_by_prefix(base_name)
        owned = db.get_agents_by_owner(current_user.username)
        is_redeploy = any(v.name in owned for v in existing_versions)
        if not is_redeploy:
            max_agents = get_agent_quota_for_role(current_user.role)
            if max_agents > 0:
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

        # 6c. Per-base-name deploy lock (#2060) — held from here (before the
        # first side effect: the version computation + stop-previous below)
        # through the whole deploy; released in `finally`.
        deploy_lock = _acquire_deploy_lock(base_name)

        # 7. Version handling
        version_name = get_next_version_name(base_name)
        previous_version = get_latest_version(base_name)
        previous_stopped = False

        if previous_version and previous_version.status == "running":
            # Stop the previous version
            try:
                container = get_agent_container(previous_version.name)
                if container:
                    await container_stop(container)
                    previous_stopped = True
                    # #2060 S6: remembered for compensation — a failed deploy
                    # restarts what it stopped.
                    previous_stopped_name = previous_version.name
                    logger.info(f"Stopped previous version: {previous_version.name}")
            except Exception as e:
                logger.warning(f"Failed to stop previous version {previous_version.name}: {e}")

        # 8. Copy to deployed-templates directory (#950).
        # The historical /agent-configs/templates mount is intentionally read-only
        # in compose to protect the curated catalog; the prior writability probe
        # always failed and silently fell back to ./config/agent-templates which
        # resolved INSIDE the backend container, leaving the new agent's bind
        # mount pointing at a host path that didn't exist → empty agents.
        # /data is host-mapped (TRINITY_DATA_PATH), writable, owned by UID 1000.
        templates_dir = Path(DEPLOYED_TEMPLATES_DIR_IN_BACKEND)
        try:
            templates_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise HTTPException(
                status_code=500,
                detail={
                    "error": (
                        f"Deployed-templates directory {templates_dir} is not writable: {e}. "
                        f"Check that {DEPLOYED_TEMPLATES_DIR_IN_BACKEND}'s host bind "
                        f"(TRINITY_DATA_PATH default './trinity-data') exists and is owned "
                        f"by UID 1000 (see docs/migrations/NON_ROOT_CONTAINERS_2026-05.md)."
                    ),
                    "code": "DEPLOYED_TEMPLATES_DIR_UNWRITABLE",
                }
            )

        dest_path = templates_dir / version_name

        # Path-containment guard (#950). version_name is already a single
        # sanitized slug (sanitize_agent_name strips path separators), but
        # normalize + verify containment so the value reaching every downstream
        # file access provably stays under templates_dir. This is
        # defense-in-depth AND the CodeQL-recognized path-injection barrier:
        # normalize, inline startswith prefix-check, and use the normalized
        # value downstream.
        _templates_base = os.path.normpath(str(templates_dir))
        _normalized_dest = os.path.normpath(str(dest_path))
        if _normalized_dest != _templates_base and not _normalized_dest.startswith(
            _templates_base + os.sep
        ):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": (
                        f"Resolved template path escapes deployed-templates "
                        f"directory: {version_name}"
                    ),
                    "code": "TEMPLATE_PATH_ESCAPE",
                },
            )
        dest_path = Path(_normalized_dest)

        # #2060 (Crux #4, the #2006 class): claim the handle BEFORE the copy —
        # a mid-copy failure must leave a cleanable directory, and assigning
        # only after `copytree` returns is exactly how the partial dir became
        # an addressable `local:<version_name>` template.
        dest_created = dest_path
        if dest_path.exists():
            shutil.rmtree(dest_path)

        try:
            # symlinks=True is the #2060 symlink contract: in-root links are
            # preserved end to end (the default dereferenced them), and
            # dangling in-root links are copied as links instead of crashing
            # copytree into an opaque 500.
            shutil.copytree(extract_root, dest_path, symlinks=True)
        except (shutil.Error, OSError) as e:
            summary = str(e)
            if isinstance(e, shutil.Error) and e.args and isinstance(e.args[0], list):
                parts = [
                    f"{src} -> {dst}: {why}" for src, dst, why in e.args[0][:5]
                ]
                summary = "; ".join(parts)
            raise HTTPException(
                status_code=500,
                detail={
                    "error": f"Failed to copy template into the deploy store: {summary}",
                    "code": "TEMPLATE_COPY_FAILED",
                },
            )
        logger.info(f"Copied agent template to: {dest_path}")

        # 8a. POST-COPY manifest re-verification (#2060) — the deployed
        # template is what agents are actually created from. Runs BEFORE the
        # step-9b credentials merge mutates `.env` (ordering load-bearing:
        # the merge would otherwise false-drift a manifest-listed .env).
        # No extras check: copytree cannot invent files.
        if manifest_entries is not None:
            drift = _verify_manifest(dest_path, manifest_entries, check_extras=False)
            if drift.any():
                _raise_manifest_drift(drift, "post-copy")

        # Evidence counts, captured at verification time (before the merge
        # can add a request-credentials .env).
        files_deployed, symlinks_deployed = _count_deployed(dest_path)
        files_expected = (
            sum(1 for e in manifest_entries if e.sha256)
            if manifest_entries is not None else None
        )
        verified = manifest_entries is not None

        # 10. Create agent
        # Extract runtime config from template
        runtime_config = template_data.get("runtime", {})
        runtime_type = None
        runtime_model = None
        if isinstance(runtime_config, dict):
            runtime_type = runtime_config.get("type")
            runtime_model = runtime_config.get("model")
        elif isinstance(runtime_config, str):
            runtime_type = runtime_config

        agent_config = AgentConfig(
            name=version_name,
            template=f"local:{version_name}",
            resources=template_data.get("resources", {"cpu": "2", "memory": "4g"}),
            runtime=runtime_type,
            runtime_model=runtime_model
        )

        # 9b. Process credentials before agent creation
        credentials_imported = {}
        credentials_injected = 0

        # Check for credential files in archive
        env_file = dest_path / ".env"
        if env_file.exists():
            credentials_imported[".env"] = "from_archive"

        mcp_file = dest_path / ".mcp.json"
        if mcp_file.exists():
            # ent#213's validation moved to step 6a (#2006) — it now runs on
            # `extract_root` before this copy exists. This is a copy of the
            # bytes that already passed, so re-validating here would only be
            # able to fail on something written between the two points, and
            # nothing writes `.mcp.json` in that window. Bookkeeping only.
            credentials_imported[".mcp.json"] = "from_archive"

        # Write credentials from request to template directory
        # These will be copied to the agent workspace during creation
        if body.credentials:
            env_content = "\n".join(f"{k}={v}" for k, v in body.credentials.items())
            # Append to existing .env or create new one
            if env_file.exists():
                existing = env_file.read_text()
                # Append with newline separator
                env_file.write_text(existing.rstrip() + "\n" + env_content + "\n")
                credentials_imported[".env"] = "merged"
            else:
                env_file.write_text(env_content + "\n")
                credentials_imported[".env"] = "created"
            credentials_injected = len(body.credentials)
            logger.info(f"Wrote {credentials_injected} credentials to template for agent {version_name}")

        # 9b-advisory. Warn about MCP servers whose ${VAR} references have no
        # matching credential in the post-merge .env (#950 deferred hardening).
        # Read dest_path/.env — that's where body.credentials were merged just
        # above; extract_root still holds the un-merged archive copy.
        warnings = collect_mcp_credential_warnings(dest_path)
        if warnings:
            logger.info(
                f"Deploy {version_name}: {len(warnings)} MCP credential warning(s)"
            )
        warnings = warnings + deploy_warnings

        # 9c. Pre-populate the agent's workspace volume from the extracted
        # template (#950). Sidesteps the bind-mount transport entirely:
        # dev compose uses a docker-managed named volume for /data while
        # prod uses a host bind, so any host-path math in crud.py would be
        # right on prod and wrong on dev. By writing into the workspace
        # volume directly here, both environments behave identically.
        # The `.trinity-initialized` marker tells the agent's startup.sh
        # to skip its `/template` -> `/home/developer` copy (which won't
        # run anyway since no /template bind is set up — see crud.py).
        volume_created = f"agent-{version_name}-workspace"
        _prepopulate_workspace_from_template(version_name, dest_path)

        # Hand the directory over: from here it can be referenced by the
        # container's mount spec, so it is no longer ours to delete (#2006).
        # `volume_created` deliberately stays set (see its init comment): the
        # cleanup guard, not this handover, decides whether it is removable.
        dest_created = None

        agent_status = await create_agent_fn(
            agent_config,
            current_user,
            request,
            skip_name_sanitization=True,
            # #1667: THE one legitimate adopt — the workspace volume was just
            # pre-populated with the template above, so create must mount it
            # rather than refuse it. Every other caller is refused a
            # pre-existing volume (it would be another agent's leftover data).
            adopt_existing_workspace=True,
        )

        # 10a. Post-deploy compatibility evidence (#2060 / #668) — STATIC
        # only, fail-open: absent evidence yields None + a warning, never a
        # failed deploy (the agent is already created and running).
        compatibility_hard_count = None
        try:
            compatibility_hard_count = await _compatibility_hard_count(version_name)
            if compatibility_hard_count is None:
                warnings.append(
                    "Post-deploy compatibility check unavailable — run "
                    "get_agent_compatibility_report once the agent is up."
                )
        except Exception as e:  # noqa: BLE001 — fail-open by contract
            logger.warning(
                f"[#2060] post-deploy compatibility check failed for "
                f"{version_name}: {e}"
            )
            warnings.append(
                f"Post-deploy compatibility check failed "
                f"({type(e).__name__}) — run get_agent_compatibility_report "
                f"once the agent is up."
            )

        # 11. Return response
        return DeployLocalResponse(
            status="success",
            agent=agent_status,
            versioning=VersioningInfo(
                base_name=base_name,
                previous_version=previous_version.name if previous_version else None,
                previous_version_stopped=previous_stopped,
                new_version=version_name
            ),
            credentials_imported=credentials_imported,
            credentials_injected=credentials_injected,
            warnings=warnings,
            verified=verified,
            files_expected=files_expected,
            files_deployed=files_deployed,
            symlinks_deployed=symlinks_deployed,
            compatibility_hard_count=compatibility_hard_count,
        )

    except HTTPException:
        _remove_partial_deploy(dest_created)
        _cleanup_deploy_volume(volume_created)
        await _restart_stopped_previous(previous_stopped_name)
        raise
    except Exception as e:
        _remove_partial_deploy(dest_created)
        _cleanup_deploy_volume(volume_created)
        await _restart_stopped_previous(previous_stopped_name)
        raise HTTPException(
            status_code=500,
            detail={
                "error": f"Failed to deploy local agent: {str(e)}",
                "code": "DEPLOY_FAILED",
            }
        )
    finally:
        _release_deploy_lock(deploy_lock)
        # Cleanup temp directory
        if temp_dir and temp_dir.exists():
            try:
                shutil.rmtree(temp_dir)
            except Exception:
                pass
