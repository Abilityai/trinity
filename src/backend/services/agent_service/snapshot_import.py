"""
Backend-materialized GitHub snapshot for the "copy" import intent
(trinity-enterprise#15).

Stages a point-in-time copy of a GitHub repo on the backend — clone, capture
the head SHA, strip `.git`, drop tree-escaping symlinks — so the agent's
workspace volume can be pre-populated via the deploy-local primitive
(`deploy._prepopulate_workspace_from_template`) BEFORE the container exists.
The container therefore carries no GitHub env and no PAT, and startup.sh sees
the `.trinity-initialized` marker and skips all clone logic (marker handling
predates every image in the fleet — no base-image dependency).

Placement contract mirrors fork_to_own: the 4xx-able staging work runs in
`_resolve_template`, BEFORE crud.py's docker try-block, so structured errors
reach the UI instead of flattening to a generic 500.

Security:
- The PAT travels via `GIT_CONFIG_*` env (http.extraHeader), never argv —
  reuses fork_to_own's `_run_git`, whose output is scrub_secret'd.
- Symlinks whose target resolves OUTSIDE the staged tree are removed with a
  warning (a hostile repo's link must not smuggle backend-readable paths into
  the volume tar); in-tree symlinks are preserved (dropping all symlinks would
  break legitimate repos). Links are never followed at tar-build time —
  `tarfile.add` archives them as SYMTYPE members.
"""
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from fastapi import HTTPException

from .fork_to_own import _run_git, scrub_secret

logger = logging.getLogger(__name__)

CLONE_TIMEOUT_S = 120

# Disk-backed staging (the backend /tmp is a small RAM tmpfs — the exact trap
# the legacy in-container shallow-clone branch still carries, #1098/#1439).
_STAGING_ROOT = Path("/data/agent-import-tmp")

# Self-healing for the rare pre-try-block failure window in crud.py (a 400
# from validate_runtime etc. after staging but before the rollback handles
# exist): any sibling staging dir older than this is swept opportunistically
# on the next stage call.
_STALE_STAGING_SECONDS = 24 * 3600

# Git stderr shapes that mean "definitively unreadable" rather than transient.
# Anonymous GitHub cannot distinguish not-found from private (ent#123), so
# both map to one combined 400 — no new enumeration oracle.
_AUTH_OR_MISSING_MARKERS = (
    "authentication failed",
    "could not read username",
    "repository not found",
    "remote branch",  # "Remote branch X not found in upstream"
    "not found",
    "403",
)


@dataclass
class SnapshotStaging:
    """A staged, git-less snapshot ready for volume pre-population."""

    staging_dir: str
    source_repo: str
    source_branch: str
    head_sha: str
    file_count: int


def _http_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"error": message, "code": code})


def _sweep_stale_staging() -> None:
    """Best-effort reap of abandoned staging dirs (see _STALE_STAGING_SECONDS)."""
    try:
        cutoff = time.time() - _STALE_STAGING_SECONDS
        for entry in _STAGING_ROOT.iterdir():
            try:
                if entry.is_dir() and entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
                    logger.info("snapshot-import: swept stale staging dir %s", entry)
            except OSError:
                continue
    except OSError:
        pass


def _make_staging_dir() -> str:
    try:
        _STAGING_ROOT.mkdir(parents=True, exist_ok=True)
        _sweep_stale_staging()
        return tempfile.mkdtemp(prefix="import-", dir=str(_STAGING_ROOT))
    except OSError:
        return tempfile.mkdtemp(prefix="agent-import-")


def cleanup_staging(staging_dir: Optional[str]) -> None:
    """Idempotent, best-effort removal — called from the success path AND the
    crud.py rollback, so a double call must be harmless."""
    if staging_dir:
        shutil.rmtree(staging_dir, ignore_errors=True)


def _prune_escaping_symlinks(root: Path) -> int:
    """Remove symlinks whose target resolves outside ``root``. Returns count."""
    removed = 0
    resolved_root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            if not p.is_symlink():
                continue
            try:
                target = p.resolve()
                in_tree = target.is_relative_to(resolved_root)
            except OSError:
                in_tree = False
            if not in_tree:
                logger.warning(
                    "snapshot-import: dropping tree-escaping symlink %s",
                    p.relative_to(root),
                )
                p.unlink(missing_ok=True)
                removed += 1
    return removed


def _count_snapshot_files(root: Path) -> int:
    """Regular files in the staged tree (``.git`` already stripped)."""
    return sum(
        len(filenames) for _, _, filenames in os.walk(root, followlinks=False)
    )


async def stage_github_snapshot(
    repo_path: str,
    branch: Optional[str],
    pat: Optional[str],
) -> SnapshotStaging:
    """Clone ``repo_path`` at ``branch`` (or the repo default), capture the head
    SHA, strip ``.git``, prune escaping symlinks, and refuse an empty tree.

    Raises HTTPException with structured ``detail={"error","code"}`` on every
    failure path. The caller owns the returned staging dir — it must be
    released via ``cleanup_staging`` on both success and failure paths.
    """
    staging_dir = _make_staging_dir()
    url = f"https://github.com/{repo_path}.git"
    clone_args = ["clone", "--depth", "1", "--single-branch"]
    if branch:
        clone_args += ["--branch", branch]
    clone_args += [url, staging_dir]

    try:
        rc, out = await _run_git(clone_args, timeout=CLONE_TIMEOUT_S, auth_pat=pat or "")
        if rc != 0:
            lowered = out.lower()
            if any(marker in lowered for marker in _AUTH_OR_MISSING_MARKERS):
                # ent#123 combined form: anonymous GitHub cannot distinguish
                # not-found from private, and naming which would be an
                # enumeration oracle.
                branch_note = f" (branch '{branch}')" if branch else ""
                raise _http_error(
                    400,
                    "COPY_SOURCE_UNREADABLE",
                    f"Repository '{repo_path}'{branch_note} was not found or is "
                    f"not readable with the available credentials. Check the "
                    f"name/branch, or add a GitHub token in Settings.",
                )
            logger.error(
                "snapshot-import: clone failed for %s: %s",
                repo_path, scrub_secret(out, pat or ""),
            )
            raise _http_error(
                502,
                "COPY_CLONE_FAILED",
                f"Could not clone '{repo_path}' for the snapshot. Nothing was "
                f"created — retry in a moment.",
            )

        rc, sha_out = await _run_git(
            ["-C", staging_dir, "rev-parse", "HEAD"], timeout=15.0, auth_pat="",
        )
        head_sha = sha_out.strip() if rc == 0 else "unknown"

        rc, branch_out = await _run_git(
            ["-C", staging_dir, "rev-parse", "--abbrev-ref", "HEAD"],
            timeout=15.0, auth_pat="",
        )
        resolved_branch = branch or (branch_out.strip() if rc == 0 else "unknown")

        shutil.rmtree(Path(staging_dir) / ".git", ignore_errors=True)
        _prune_escaping_symlinks(Path(staging_dir))

        file_count = _count_snapshot_files(Path(staging_dir))
        if file_count == 0:
            # Mirrors FORK_TEMPLATE_EMPTY — never a green blank agent (a
            # depth-1 clone of an empty repo exits 0 with zero files).
            raise _http_error(
                400,
                "COPY_SOURCE_EMPTY",
                f"Repository '{repo_path}' has no files on "
                f"'{resolved_branch}' — there is nothing to snapshot.",
            )

        logger.info(
            "snapshot-import: staged %s@%s (%s, %d files) in %s",
            repo_path, resolved_branch, head_sha[:12], file_count, staging_dir,
        )
        return SnapshotStaging(
            staging_dir=staging_dir,
            source_repo=repo_path,
            source_branch=resolved_branch,
            head_sha=head_sha,
            file_count=file_count,
        )
    except Exception:
        cleanup_staging(staging_dir)
        raise
