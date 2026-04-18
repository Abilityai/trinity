"""
Git sync endpoints for GitHub bidirectional sync.
"""
import json
import os
import subprocess
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from ..models import GitSyncRequest, GitPullRequest
from ..utils.git_conflict import classify_conflict
from .files import _read_persistent_state
from .snapshot import build_snapshot, restore_from_tar

logger = logging.getLogger(__name__)
router = APIRouter()

# S7 Layer 3 (#382): directory where we persist the last-observed remote
# SHA per branch. Written after every successful fetch and consumed by the
# push path as the "expected-sha" argument to `git push --force-with-lease`.
# Living under ~/.trinity keeps it out of the workspace tree while still
# surviving container restarts (the home bind mount is persistent).
LAST_REMOTE_SHA_DIR = Path.home() / ".trinity" / "last-remote-sha"

# File the operator queue sync service reads. We append collision entries
# here when a push is rejected by the lease.
OPERATOR_QUEUE_FILE = Path.home() / ".trinity" / "operator-queue.json"


def _compute_ahead_behind(home_dir: Path, branch: str) -> tuple:
    """Best-effort ahead/behind counts vs origin/<branch>.

    Returns ``(0, 0)`` on any failure — the classifier only uses these to pick
    AHEAD_ONLY vs BEHIND_ONLY when stderr is empty, and every real 409 path
    here has non-empty stderr, so a failure to resolve counts is harmless.
    """
    try:
        result = subprocess.run(
            ["git", "rev-list", "--left-right", "--count", f"origin/{branch}...HEAD"],
            capture_output=True, text=True, cwd=str(home_dir), timeout=10
        )
        if result.returncode == 0:
            parts = result.stdout.strip().split()
            if len(parts) == 2:
                return int(parts[1]), int(parts[0])  # (ahead, behind)
    except Exception:  # best-effort diagnostic only
        pass
    return 0, 0


def _conflict_response(
    *,
    status_code: int,
    detail: str,
    conflict_type: str,
    stderr: str,
    home_dir: Path,
    branch: Optional[str],
) -> JSONResponse:
    """Build a 409 ``JSONResponse`` that carries both the legacy ``detail`` and
    the new ``conflict_class`` classification (issue #386 / S5).

    Keeps the ``X-Conflict-Type`` header intact for backward compatibility
    with older clients; adds ``X-Conflict-Class`` alongside it.
    """
    ahead, behind = _compute_ahead_behind(home_dir, branch) if branch else (0, 0)
    conflict_class = classify_conflict(stderr or "", ahead=ahead, behind=behind).value
    return JSONResponse(
        status_code=status_code,
        content={
            "detail": detail,
            "conflict_class": conflict_class,
        },
        headers={
            "X-Conflict-Type": conflict_type,
            "X-Conflict-Class": conflict_class,
        },
    )


# ---------------------------------------------------------------------------
# Sync state file (#389 S1a) — small JSON persisted under .trinity/sync-state.json
# so counters survive container restarts. The backend's SyncHealthService
# reads these fields via GET /api/git/status every minute.
# ---------------------------------------------------------------------------

_SYNC_STATE_DEFAULT: Dict = {
    "last_sync_status": "never",
    "last_sync_at": None,
    "last_error_summary": None,
    "consecutive_failures": 0,
}


def _sync_state_path(home_dir: Path) -> Path:
    return home_dir / ".trinity" / "sync-state.json"


def _read_sync_state_file(home_dir: Path) -> Dict:
    """Read `.trinity/sync-state.json` or return defaults on missing/corrupt."""
    path = _sync_state_path(home_dir)
    if not path.exists():
        return dict(_SYNC_STATE_DEFAULT)
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("sync-state.json root is not an object")
        merged = dict(_SYNC_STATE_DEFAULT)
        merged.update(data)
        return merged
    except (ValueError, json.JSONDecodeError):
        logger.warning("sync-state.json corrupt, returning default")
        return dict(_SYNC_STATE_DEFAULT)


def _write_sync_state_file(
    home_dir: Path,
    last_sync_status: str,
    last_sync_at: Optional[str] = None,
    last_error_summary: Optional[str] = None,
) -> Dict:
    """Persist one sync outcome.

    consecutive_failures is bumped on `failed`, reset on `success`, untouched
    on `never`. last_error_summary is cleared on success, kept on never.
    """
    prior = _read_sync_state_file(home_dir)

    if last_sync_status == "failed":
        prior["consecutive_failures"] = (prior.get("consecutive_failures") or 0) + 1
        prior["last_error_summary"] = last_error_summary
    elif last_sync_status == "success":
        prior["consecutive_failures"] = 0
        prior["last_error_summary"] = None
    # else 'never' — leave counters alone

    prior["last_sync_status"] = last_sync_status
    prior["last_sync_at"] = last_sync_at or datetime.now(timezone.utc).isoformat()

    path = _sync_state_path(home_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(prior, indent=2))
    return prior


def _run_auto_sync_once(home_dir: Path) -> Dict:
    """One auto-sync cycle: stage, commit if dirty, push. Records outcome.

    Intentionally minimal — heavy conflict handling stays in the operator-
    initiated `sync_to_github` endpoint. Auto-sync is a heartbeat, not a
    rescue.
    """
    now = datetime.now(timezone.utc).isoformat()
    try:
        # Stage everything.
        subprocess.run(
            ["git", "add", "-A"],
            cwd=str(home_dir), capture_output=True, text=True, timeout=30, check=True,
        )

        # Is there anything to commit?
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(home_dir), capture_output=True, text=True, timeout=10,
        )
        if status.stdout.strip():
            commit_msg = f"Trinity auto-sync: {now}"
            subprocess.run(
                ["git", "commit", "-m", commit_msg],
                cwd=str(home_dir), capture_output=True, text=True, timeout=30, check=True,
            )

        push = subprocess.run(
            ["git", "push", "origin", "HEAD"],
            cwd=str(home_dir), capture_output=True, text=True, timeout=60,
        )
        if push.returncode != 0:
            err = _summarize_git_error(push.stderr or push.stdout or "push failed")
            _write_sync_state_file(home_dir, "failed",
                                    last_sync_at=now, last_error_summary=err)
            return {"status": "failed", "error": err}

        _write_sync_state_file(home_dir, "success", last_sync_at=now)
        return {"status": "success"}

    except subprocess.CalledProcessError as exc:
        err = _summarize_git_error(exc.stderr or exc.stdout or str(exc))
        _write_sync_state_file(home_dir, "failed",
                                last_sync_at=now, last_error_summary=err)
        return {"status": "failed", "error": err}
    except Exception as exc:  # defensive — never let the loop crash
        err = _summarize_git_error(str(exc))
        _write_sync_state_file(home_dir, "failed",
                                last_sync_at=now, last_error_summary=err)
        return {"status": "failed", "error": err}


def _summarize_git_error(raw: str) -> str:
    """Trim git stderr to a 240-char one-liner (matches operator-queue field size)."""
    if not raw:
        return "unknown error"
    first = raw.strip().splitlines()[0]
    return first[:240]


def _get_pull_branch(current_branch: str, home_dir: Path) -> str:
    """Determine the upstream branch to pull from.

    For trinity/* working branches, pull from main instead of the working
    branch (which nobody pushes to externally). Falls back to current_branch
    if origin/main doesn't exist.
    """
    if not current_branch.startswith("trinity/"):
        return current_branch
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "origin/main"],
        capture_output=True, text=True, cwd=str(home_dir), timeout=10
    )
    return "main" if result.returncode == 0 else current_branch


def _sha_file_for_branch(branch: str) -> Path:
    """Path to the last-remote-sha file for a given branch.

    Branches can contain ``/`` (``trinity/<agent>/<id>``) so we mirror the
    branch layout as nested directories. That keeps the file names readable
    rather than URL-escaping the slashes.
    """
    return LAST_REMOTE_SHA_DIR / branch


def _persist_last_remote_sha(branch: str, home_dir: Path) -> None:
    """Record the remote SHA this instance observed for ``branch`` after fetch.

    S7 Layer 3: the stored value becomes the ``expected-sha`` lease on the
    next push. If the remote moves out from under us (another instance
    pushed in the interim) the fetch will update ``origin/<branch>`` to the
    new SHA but the persisted lease is still the old one — which is exactly
    what ``--force-with-lease`` is checking, so the collision is caught.

    Failure to persist is logged, never raised: it would turn a minor I/O
    glitch into a hard sync failure, and the worst case is the next push
    has no lease and behaves like plain `--force` (one-time regression, not
    silent corruption).
    """
    rev = subprocess.run(
        ["git", "rev-parse", f"origin/{branch}"],
        capture_output=True,
        text=True,
        cwd=str(home_dir),
        timeout=10,
    )
    if rev.returncode != 0:
        logger.debug(
            "No origin/%s ref yet — skipping last-remote-sha persist", branch
        )
        return

    sha = rev.stdout.strip()
    if not sha:
        return

    try:
        target = _sha_file_for_branch(branch)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(sha + "\n")
    except OSError as exc:
        logger.warning(
            "Could not persist last-remote-sha for %s: %s", branch, exc
        )


def _read_last_remote_sha(branch: str) -> str | None:
    """Read the previously persisted remote SHA for ``branch``, if any."""
    path = _sha_file_for_branch(branch)
    try:
        return path.read_text().strip() or None
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("Could not read last-remote-sha for %s: %s", branch, exc)
        return None


def _record_push_collision(branch: str, lease_sha: str | None, stderr: str) -> None:
    """Append a structured alert to ~/.trinity/operator-queue.json.

    S7 Layer 3 surfacing: when ``--force-with-lease`` rejects the push the
    losing instance now knows it lost, so we write an operator-queue entry
    that the backend's ``operator_queue_service`` picks up on its next
    poll. The entry is an ``alert`` (no decision required) — the operator
    just needs to know another instance is writing to the same branch so
    they can rebind one of them.
    """
    try:
        OPERATOR_QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
        if OPERATOR_QUEUE_FILE.exists():
            try:
                payload = json.loads(OPERATOR_QUEUE_FILE.read_text() or "{}")
            except json.JSONDecodeError:
                logger.warning(
                    "operator-queue.json is malformed; recreating before appending"
                )
                payload = {}
        else:
            payload = {}

        payload.setdefault("$schema", "operator-queue-v1")
        requests = payload.setdefault("requests", [])

        now_iso = datetime.now(timezone.utc).isoformat()
        requests.append(
            {
                "id": f"git-collision-{uuid.uuid4().hex[:12]}",
                "type": "alert",
                "status": "pending",
                "priority": "high",
                "title": f"Git push rejected on {branch} — branch binding collision",
                "question": (
                    "Another Trinity instance wrote to this working branch since "
                    "this agent last fetched. The --force-with-lease push was "
                    "rejected to prevent silent data loss. Rebind one of the "
                    "agents to a fresh working branch before retrying."
                ),
                "options": None,
                "context": {
                    "branch": branch,
                    "expected_sha": lease_sha,
                    "git_stderr": (stderr or "").strip()[:2000],
                    "remediation": (
                        "Assign a fresh working branch to one of the colliding "
                        "agents (Fleet → Branch Bindings → Assign fresh branch)."
                    ),
                },
                "created_at": now_iso,
            }
        )

        OPERATOR_QUEUE_FILE.write_text(json.dumps(payload, indent=2))
    except Exception as exc:  # pragma: no cover — best-effort surfacing
        logger.warning("Failed to record push-collision alert: %s", exc)


def _is_stale_lease_rejection(stderr: str) -> bool:
    """Return True if git's stderr indicates a --force-with-lease mismatch."""
    s = (stderr or "").lower()
    return "stale info" in s or "stale" in s and "rejected" in s


def _dual_ahead_behind_payload(current_branch: str, home_dir: Path) -> dict:
    """Return ahead/behind tuples for both `origin/main` and the working branch.

    Fixes P6 (#389): the old implementation redirected `trinity/*` branches to
    `origin/main` for ahead/behind, hiding external writes to the working
    branch. We now compute BOTH tuples:

    - `ahead_main`/`behind_main`     — against `origin/main` (template sync)
    - `ahead_working`/`behind_working` — against `origin/<current_branch>`
      (peer divergence / P5-style silent clobber)

    Legacy aliases `ahead` and `behind` track the main tuple to preserve
    backward compatibility with clients written against the old response.
    """
    # Uses upstream's `_compute_ahead_behind(home_dir, branch) -> (ahead, behind)`
    # defined near the top of this module.
    main_ahead, main_behind = _compute_ahead_behind(home_dir, "main")
    # Non-trinity branches use the same ref twice; avoid a second subprocess
    # for the common case.
    if current_branch.startswith("trinity/") and current_branch != "main":
        working_ahead, working_behind = _compute_ahead_behind(home_dir, current_branch)
    else:
        working_ahead, working_behind = main_ahead, main_behind

    return {
        "ahead": main_ahead,  # legacy alias
        "behind": main_behind,  # legacy alias
        "ahead_main": main_ahead,
        "behind_main": main_behind,
        "ahead_working": working_ahead,
        "behind_working": working_behind,
    }


@router.get("/api/git/status")
async def get_git_status():
    """
    Get git repository status including current branch, changes, and sync state.
    Only available for agents with git sync enabled.
    """
    home_dir = Path("/home/developer")
    git_dir = home_dir / ".git"

    if not git_dir.exists():
        return {
            "git_enabled": False,
            "message": "Git sync not enabled for this agent"
        }

    try:
        # Get current branch
        branch_result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(home_dir),
            timeout=10
        )
        current_branch = branch_result.stdout.strip() if branch_result.returncode == 0 else "unknown"

        # Get status (modified, untracked files)
        status_result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            cwd=str(home_dir),
            timeout=10
        )
        changes = []
        if status_result.returncode == 0 and status_result.stdout.strip():
            for line in status_result.stdout.strip().split('\n'):
                if line:
                    status_code = line[:2]
                    filepath = line[3:]
                    changes.append({
                        "status": status_code.strip(),
                        "path": filepath
                    })

        # Get last commit
        log_result = subprocess.run(
            ["git", "log", "-1", "--format=%H|%h|%s|%an|%ai"],
            capture_output=True,
            text=True,
            cwd=str(home_dir),
            timeout=10
        )
        last_commit = None
        if log_result.returncode == 0 and log_result.stdout.strip():
            parts = log_result.stdout.strip().split('|')
            if len(parts) >= 5:
                last_commit = {
                    "sha": parts[0],
                    "short_sha": parts[1],
                    "message": parts[2],
                    "author": parts[3],
                    "date": parts[4]
                }

        # Fetch to update remote refs (required for accurate ahead/behind)
        fetch_result = subprocess.run(
            ["git", "fetch", "origin"],
            capture_output=True,
            text=True,
            cwd=str(home_dir),
            timeout=30
        )
        # S7 Layer 3 (#382): snapshot the remote SHA we just observed so
        # the next push can use it as the --force-with-lease expected-sha.
        if fetch_result.returncode == 0:
            _persist_last_remote_sha(current_branch, home_dir)

        # #389 P6: compute ahead/behind against BOTH origin/main and the
        # working branch's own remote, so external writes to trinity/* are
        # visible in the UI. Legacy `ahead`/`behind` keys still alias the
        # main tuple.
        ahead_behind = _dual_ahead_behind_payload(current_branch, home_dir)
        ahead = ahead_behind["ahead"]
        behind = ahead_behind["behind"]

        # Parallel-history detection (S2, issue #385): surface the common
        # ancestor between HEAD and origin/<pull_branch> so the frontend can
        # distinguish "simple behind" from "parallel history" (where both
        # Pull First and Force Push are wrong answers).
        common_ancestor_sha = ""
        common_ancestor_age_days = None
        merge_base_result = subprocess.run(
            ["git", "merge-base", "HEAD", f"origin/{pull_branch}"],
            capture_output=True,
            text=True,
            cwd=str(home_dir),
            timeout=10
        )
        if merge_base_result.returncode == 0:
            common_ancestor_sha = merge_base_result.stdout.strip()
            if common_ancestor_sha:
                ancestor_date_result = subprocess.run(
                    ["git", "log", "-1", "--format=%cI", common_ancestor_sha],
                    capture_output=True,
                    text=True,
                    cwd=str(home_dir),
                    timeout=10
                )
                if ancestor_date_result.returncode == 0:
                    date_str = ancestor_date_result.stdout.strip()
                    if date_str:
                        try:
                            ancestor_dt = datetime.fromisoformat(date_str)
                            if ancestor_dt.tzinfo is None:
                                ancestor_dt = ancestor_dt.replace(tzinfo=timezone.utc)
                            delta = datetime.now(timezone.utc) - ancestor_dt
                            common_ancestor_age_days = delta.days
                        except ValueError:
                            logger.warning(
                                f"Could not parse common-ancestor date: {date_str!r}"
                            )

        # Get remote URL (without credentials)
        remote_result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            cwd=str(home_dir),
            timeout=10
        )
        remote_url = ""
        if remote_result.returncode == 0:
            url = remote_result.stdout.strip()
            # Remove credentials from URL for display
            if '@github.com' in url:
                remote_url = "https://github.com/" + url.split('@github.com/')[1]
            else:
                remote_url = url

        response = {
            "git_enabled": True,
            "branch": current_branch,
            "pull_branch": pull_branch,
            "remote_url": remote_url,
            "last_commit": last_commit,
            "changes": changes,
            "changes_count": len(changes),
            "ahead": ahead,
            "behind": behind,
            "common_ancestor_sha": common_ancestor_sha,
            "common_ancestor_age_days": common_ancestor_age_days,
            "sync_status": "up_to_date" if ahead == 0 and len(changes) == 0 else "pending_sync",
        }
        # #389: dual ahead/behind tuples plus legacy ahead/behind aliases.
        response.update(ahead_behind)
        # #389: merge auto-sync heartbeat state (may be defaults if never run).
        response["sync_state"] = _read_sync_state_file(home_dir)
        return response

    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Git operation timed out")
    except Exception as e:
        logger.error(f"Git status error: {e}")
        raise HTTPException(status_code=500, detail=f"Git status error: {str(e)}")


@router.post("/api/git/sync")
async def sync_to_github(request: GitSyncRequest):
    """
    Sync local changes to GitHub by staging, committing, and pushing.

    Strategies:
    - normal: Stage, commit, push (fails if remote has changes)
    - pull_first: Pull latest, then stage, commit, push
    - force_push: Stage, commit, force push (overwrites remote)

    Steps:
    1. Stage all changes (or specific paths if provided)
    2. Create a commit with the provided message (or auto-generated)
    3. Push to the working branch (based on strategy)

    Returns the commit SHA on success.
    """
    home_dir = Path("/home/developer")
    git_dir = home_dir / ".git"
    strategy = request.strategy or "normal"

    if not git_dir.exists():
        raise HTTPException(status_code=400, detail="Git sync not enabled for this agent")

    try:
        # Get current branch
        branch_result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(home_dir),
            timeout=10
        )
        current_branch = branch_result.stdout.strip() if branch_result.returncode == 0 else "main"

        # For pull_first strategy, pull before staging
        if strategy == "pull_first":
            # Fetch first
            fetch_result = subprocess.run(
                ["git", "fetch", "origin"],
                capture_output=True,
                text=True,
                cwd=str(home_dir),
                timeout=60
            )
            # S7 Layer 3 (#382): snapshot the remote SHA for lease checks
            # on the upcoming push.
            if fetch_result.returncode == 0:
                _persist_last_remote_sha(current_branch, home_dir)

            # For trinity/* working branches, pull from main
            pull_branch = _get_pull_branch(current_branch, home_dir)

            # Check if we're behind
            behind_result = subprocess.run(
                ["git", "rev-list", "--count", f"HEAD..origin/{pull_branch}"],
                capture_output=True,
                text=True,
                cwd=str(home_dir),
                timeout=10
            )
            commits_behind = int(behind_result.stdout.strip()) if behind_result.returncode == 0 else 0

            if commits_behind > 0:
                # Stash local changes before pull
                status_check = subprocess.run(
                    ["git", "status", "--porcelain"],
                    capture_output=True,
                    text=True,
                    cwd=str(home_dir),
                    timeout=10
                )
                has_changes = bool(status_check.stdout.strip())

                if has_changes:
                    stash_result = subprocess.run(
                        ["git", "stash", "push", "-m", "Trinity auto-stash before sync"],
                        capture_output=True,
                        text=True,
                        cwd=str(home_dir),
                        timeout=30
                    )
                    stash_created = stash_result.returncode == 0 and "No local changes" not in stash_result.stdout
                else:
                    stash_created = False

                # Pull with rebase (from upstream branch, not working branch)
                pull_result = subprocess.run(
                    ["git", "pull", "--rebase", "origin", pull_branch],
                    capture_output=True,
                    text=True,
                    cwd=str(home_dir),
                    timeout=60
                )

                if pull_result.returncode != 0:
                    subprocess.run(["git", "rebase", "--abort"], cwd=str(home_dir), timeout=10, capture_output=True)
                    if stash_created:
                        subprocess.run(["git", "stash", "pop"], cwd=str(home_dir), timeout=30, capture_output=True)
                    return _conflict_response(
                        status_code=409,
                        detail=f"Pull failed during sync: {pull_result.stderr}",
                        conflict_type="merge_conflict",
                        stderr=pull_result.stderr or "",
                        home_dir=home_dir,
                        branch=pull_branch,
                    )

                # Reapply stash
                if stash_created:
                    pop_result = subprocess.run(
                        ["git", "stash", "pop"],
                        capture_output=True,
                        text=True,
                        cwd=str(home_dir),
                        timeout=30
                    )
                    if pop_result.returncode != 0:
                        logger.warning(f"Failed to reapply stash: {pop_result.stderr}")

        # 1. Stage changes
        if request.paths:
            # Stage specific paths (single git add call for all paths)
            add_result = subprocess.run(
                ["git", "add"] + list(request.paths),
                capture_output=True,
                text=True,
                cwd=str(home_dir),
                timeout=30
            )
            if add_result.returncode != 0:
                logger.warning(f"Failed to add paths: {add_result.stderr}")
        else:
            # Stage all changes
            add_result = subprocess.run(
                ["git", "add", "-A"],
                capture_output=True,
                text=True,
                cwd=str(home_dir),
                timeout=30
            )
            if add_result.returncode != 0:
                raise HTTPException(status_code=500, detail=f"Git add failed: {add_result.stderr}")

        # Check if there's anything to commit
        status_result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            cwd=str(home_dir),
            timeout=10
        )

        staged_changes = [line for line in status_result.stdout.split('\n') if line and line[0] != ' ' and line[0] != '?']
        if not staged_changes:
            return {
                "success": True,
                "message": "No changes to sync",
                "commit_sha": None,
                "files_changed": 0,
                "strategy": strategy
            }

        # 2. Create commit
        commit_message = request.message or f"Trinity sync: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        commit_result = subprocess.run(
            ["git", "commit", "-m", commit_message],
            capture_output=True,
            text=True,
            cwd=str(home_dir),
            timeout=30
        )
        if commit_result.returncode != 0:
            raise HTTPException(status_code=500, detail=f"Git commit failed: {commit_result.stderr}")

        # Get the commit SHA
        sha_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(home_dir),
            timeout=10
        )
        commit_sha = sha_result.stdout.strip() if sha_result.returncode == 0 else None

        # 3. Push to remote based on strategy
        if strategy == "force_push":
            # S7 Layer 3 (#382): replace plain `git push --force` with
            # `--force-with-lease=<ref>:<expected-sha>`. If another instance
            # wrote to the branch since we last fetched, the lease is stale
            # and the push is rejected cleanly with "stale info" — rather
            # than silently clobbering the peer's state (2026-04-17
            # alpaca incident).
            lease_sha = _read_last_remote_sha(current_branch)
            push_cmd: list[str] = ["git", "push"]
            if lease_sha:
                push_cmd.append(f"--force-with-lease={current_branch}:{lease_sha}")
            else:
                # No lease on file (first push, or we couldn't persist one).
                # Use the unparameterized `--force-with-lease`, which falls
                # back to remote-tracking-ref as the expected-sha — still
                # safer than `--force`.
                push_cmd.append("--force-with-lease")
            push_cmd += ["origin", current_branch]

            push_result = subprocess.run(
                push_cmd,
                capture_output=True,
                text=True,
                cwd=str(home_dir),
                timeout=60,
            )
            if push_result.returncode != 0:
                stderr = push_result.stderr or ""
                if _is_stale_lease_rejection(stderr):
                    # Surface the collision to the operator queue. The
                    # losing instance now knows it lost — that's the whole
                    # point of the lease.
                    _record_push_collision(current_branch, lease_sha, stderr)
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Force-push rejected: another instance has "
                            "written to this branch since the last fetch. "
                            "A collision alert was recorded in the operator "
                            "queue. Rebind one of the agents to a fresh "
                            "working branch before retrying."
                        ),
                        headers={"X-Conflict-Type": "branch_ownership_collision"},
                    )
                raise HTTPException(
                    status_code=500,
                    detail=f"Force push failed: {stderr}",
                )
        else:
            # Normal push or pull_first (after pull, should be safe to push)
            push_result = subprocess.run(
                ["git", "push", "-u", "origin", current_branch],
                capture_output=True,
                text=True,
                cwd=str(home_dir),
                timeout=60
            )

            if push_result.returncode != 0:
                stderr = push_result.stderr or ""
                stderr_lower = stderr.lower()
                if "has no upstream branch" in stderr_lower:
                    upstream_result = subprocess.run(
                        ["git", "push", "--set-upstream", "origin", current_branch],
                        capture_output=True,
                        text=True,
                        cwd=str(home_dir),
                        timeout=60
                    )
                    if upstream_result.returncode != 0:
                        raise HTTPException(
                            status_code=500,
                            detail=f"Git push failed: {upstream_result.stderr}"
                        )
                    return {
                        "success": True,
                        "message": f"Synced to {current_branch}",
                        "commit_sha": commit_sha,
                        "files_changed": len(staged_changes),
                        "branch": current_branch,
                        "strategy": strategy,
                        "sync_time": datetime.now().isoformat()
                    }
                # Check if it's a rejection due to remote changes
                if "rejected" in stderr_lower or "fetch first" in stderr_lower or "non-fast-forward" in stderr_lower:
                    return _conflict_response(
                        status_code=409,
                        detail="Push rejected: Remote has changes. Use 'Pull First' or 'Force Push' strategy.",
                        conflict_type="push_rejected",
                        stderr=stderr,
                        home_dir=home_dir,
                        branch=current_branch,
                    )
                else:
                    raise HTTPException(status_code=500, detail=f"Git push failed: {stderr}")

        return {
            "success": True,
            "message": f"Synced to {current_branch}",
            "commit_sha": commit_sha,
            "files_changed": len(staged_changes),
            "branch": current_branch,
            "strategy": strategy,
            "sync_time": datetime.now().isoformat()
        }

    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Git operation timed out")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Git sync error: {e}")
        raise HTTPException(status_code=500, detail=f"Git sync error: {str(e)}")


@router.get("/api/git/log")
async def get_git_log(limit: int = 10):
    """
    Get recent git commits for this agent's branch.
    """
    home_dir = Path("/home/developer")
    git_dir = home_dir / ".git"

    if not git_dir.exists():
        raise HTTPException(status_code=400, detail="Git sync not enabled for this agent")

    try:
        log_result = subprocess.run(
            ["git", "log", f"-{limit}", "--format=%H|%h|%s|%an|%ai"],
            capture_output=True,
            text=True,
            cwd=str(home_dir),
            timeout=30
        )

        if log_result.returncode != 0:
            raise HTTPException(status_code=500, detail=f"Git log failed: {log_result.stderr}")

        commits = []
        for line in log_result.stdout.strip().split('\n'):
            if line:
                parts = line.split('|')
                if len(parts) >= 5:
                    commits.append({
                        "sha": parts[0],
                        "short_sha": parts[1],
                        "message": parts[2],
                        "author": parts[3],
                        "date": parts[4]
                    })

        return {
            "commits": commits,
            "count": len(commits)
        }

    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Git operation timed out")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Git log error: {e}")
        raise HTTPException(status_code=500, detail=f"Git log error: {str(e)}")


@router.post("/api/git/pull")
async def pull_from_github(request: GitPullRequest = GitPullRequest()):
    """
    Pull latest changes from the remote branch with conflict resolution strategies.

    Strategies:
    - clean: Try simple pull --rebase (fails if local changes conflict)
    - stash_reapply: Stash local changes, pull, then reapply stash
    - force_reset: Discard local changes and reset to remote (destructive!)
    """
    home_dir = Path("/home/developer")
    git_dir = home_dir / ".git"
    strategy = request.strategy or "clean"

    if not git_dir.exists():
        raise HTTPException(status_code=400, detail="Git sync not enabled for this agent")

    try:
        # Always fetch first to update remote refs
        fetch_result = subprocess.run(
            ["git", "fetch", "origin"],
            capture_output=True,
            text=True,
            cwd=str(home_dir),
            timeout=60
        )
        if fetch_result.returncode != 0:
            raise HTTPException(status_code=500, detail=f"Git fetch failed: {fetch_result.stderr}")

        # Get current branch
        branch_result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(home_dir),
            timeout=10
        )
        current_branch = branch_result.stdout.strip() if branch_result.returncode == 0 else "main"

        # For trinity/* working branches, pull from main instead
        pull_branch = _get_pull_branch(current_branch, home_dir)

        # Check for local uncommitted changes
        status_result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            cwd=str(home_dir),
            timeout=10
        )
        has_local_changes = bool(status_result.stdout.strip())

        # Execute strategy
        if strategy == "force_reset":
            # Discard all local changes and reset to remote
            reset_result = subprocess.run(
                ["git", "reset", "--hard", f"origin/{pull_branch}"],
                capture_output=True,
                text=True,
                cwd=str(home_dir),
                timeout=60
            )
            if reset_result.returncode != 0:
                raise HTTPException(status_code=500, detail=f"Git reset failed: {reset_result.stderr}")

            # Clean untracked files too
            subprocess.run(
                ["git", "clean", "-fd"],
                capture_output=True,
                text=True,
                cwd=str(home_dir),
                timeout=30
            )

            return {
                "success": True,
                "message": f"Force reset to origin/{pull_branch}",
                "strategy": "force_reset",
                "local_changes_discarded": has_local_changes
            }

        elif strategy == "stash_reapply":
            stash_created = False
            stash_message = ""

            # Stash local changes if any
            if has_local_changes:
                stash_result = subprocess.run(
                    ["git", "stash", "push", "-m", "Trinity auto-stash before pull"],
                    capture_output=True,
                    text=True,
                    cwd=str(home_dir),
                    timeout=30
                )
                if stash_result.returncode != 0:
                    return _conflict_response(
                        status_code=409,
                        detail=f"Failed to stash local changes: {stash_result.stderr}",
                        conflict_type="stash_failed",
                        stderr=stash_result.stderr or "",
                        home_dir=home_dir,
                        branch=pull_branch,
                    )
                stash_created = "No local changes" not in stash_result.stdout

            # Pull with rebase (from upstream branch, not working branch)
            pull_result = subprocess.run(
                ["git", "pull", "--rebase", "origin", pull_branch],
                capture_output=True,
                text=True,
                cwd=str(home_dir),
                timeout=60
            )

            if pull_result.returncode != 0:
                # Abort rebase if it failed
                subprocess.run(["git", "rebase", "--abort"], cwd=str(home_dir), timeout=10, capture_output=True)

                # Try to restore stash if we created one
                if stash_created:
                    subprocess.run(["git", "stash", "pop"], cwd=str(home_dir), timeout=30, capture_output=True)

                return _conflict_response(
                    status_code=409,
                    detail=f"Pull failed with conflicts: {pull_result.stderr}",
                    conflict_type="merge_conflict",
                    stderr=pull_result.stderr or "",
                    home_dir=home_dir,
                    branch=pull_branch,
                )

            # Reapply stash if we created one
            if stash_created:
                pop_result = subprocess.run(
                    ["git", "stash", "pop"],
                    capture_output=True,
                    text=True,
                    cwd=str(home_dir),
                    timeout=30
                )
                if pop_result.returncode != 0:
                    # Stash pop failed - likely conflicts with newly pulled changes
                    stash_message = f" (Warning: Could not reapply local changes: {pop_result.stderr.strip()})"

            return {
                "success": True,
                "message": f"Pulled latest changes from origin/{pull_branch}{stash_message}",
                "strategy": "stash_reapply",
                "stash_created": stash_created,
                "output": pull_result.stdout
            }

        else:  # "clean" strategy (default)
            # Check if we're behind remote (using upstream branch)
            behind_result = subprocess.run(
                ["git", "rev-list", "--count", f"HEAD..origin/{pull_branch}"],
                capture_output=True,
                text=True,
                cwd=str(home_dir),
                timeout=10
            )
            commits_behind = int(behind_result.stdout.strip()) if behind_result.returncode == 0 else 0

            if commits_behind == 0:
                return {
                    "success": True,
                    "message": "Already up to date",
                    "strategy": "clean",
                    "commits_behind": 0
                }

            # Try simple pull with rebase (from upstream branch)
            pull_result = subprocess.run(
                ["git", "pull", "--rebase", "origin", pull_branch],
                capture_output=True,
                text=True,
                cwd=str(home_dir),
                timeout=60
            )

            if pull_result.returncode != 0:
                # Abort rebase
                subprocess.run(["git", "rebase", "--abort"], cwd=str(home_dir), timeout=10, capture_output=True)

                # Determine conflict type
                conflict_type = "local_uncommitted" if has_local_changes else "merge_conflict"
                error_detail = pull_result.stderr.strip()

                return _conflict_response(
                    status_code=409,
                    detail=f"Pull failed: {error_detail}",
                    conflict_type=conflict_type,
                    stderr=pull_result.stderr or "",
                    home_dir=home_dir,
                    branch=pull_branch,
                )

            return {
                "success": True,
                "message": f"Pulled {commits_behind} commit(s) from origin/{pull_branch}",
                "strategy": "clean",
                "commits_behind": commits_behind,
                "output": pull_result.stdout
            }

    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Git operation timed out")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Git pull error: {e}")
        raise HTTPException(status_code=500, detail=f"Git pull error: {str(e)}")


# ---------------------------------------------------------------------------
# Reset-preserve-state (S3, #384)
# ---------------------------------------------------------------------------


def _git(
    args: list[str], cwd: Path, timeout: int = 60
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def reset_to_main_preserve_state_impl(
    home_dir: Path,
    read_allowlist: Callable[[], list[str]] = _read_persistent_state,
    skip_push: bool = False,
) -> dict[str, object]:
    """Adopt origin/main as the new baseline, preserving allowlisted files.

    The safe-recovery primitive for the parallel-history deadlock (P2/P3
    in the git-improvements proposal). Composes three steps:

    1. Read the persistent-state allowlist (#383 / S4).
    2. Snapshot matching files to `.trinity/backup/<iso-ts>/` so the
       destructive reset is always recoverable from inside the container.
    3. `git reset --hard origin/main`, overlay the snapshot, commit with
       the spec's exact message, and `git push --force-with-lease`.

    `skip_push=True` is used by tests so the full sequence can be
    verified without needing a writable remote.
    """
    if not (home_dir / ".git").exists():
        return {"error": "no_git_config"}
    if _git(["remote", "get-url", "origin"], home_dir).returncode != 0:
        return {"error": "no_git_config"}

    _git(["fetch", "origin", "main"], home_dir, timeout=120)
    if _git(["rev-parse", "--verify", "origin/main"], home_dir).returncode != 0:
        return {"error": "no_remote_main"}

    current = _git(["rev-parse", "--abbrev-ref", "HEAD"], home_dir)
    if current.returncode != 0:
        return {"error": "no_git_config"}
    working_branch = current.stdout.strip()

    patterns = read_allowlist()
    iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    backup_rel = Path(".trinity/backup") / iso
    backup_dir = home_dir / backup_rel
    backup_dir.mkdir(parents=True, exist_ok=True)

    tar_bytes, files_preserved = build_snapshot(home_dir, patterns)
    (backup_dir / "snapshot.tar").write_bytes(tar_bytes)
    (backup_dir / "files.txt").write_text("\n".join(files_preserved) + "\n")

    reset_res = _git(["reset", "--hard", "origin/main"], home_dir)
    if reset_res.returncode != 0:
        return {"error": "reset_failed", "stderr": reset_res.stderr}

    restored, _skipped = restore_from_tar(home_dir, tar_bytes, patterns)

    _git(["add", "-A"], home_dir)
    commit_res = _git(
        ["commit", "-m", "Adopt main baseline, preserve state", "--allow-empty"],
        home_dir,
    )
    if commit_res.returncode != 0:
        return {"error": "commit_failed", "stderr": commit_res.stderr}

    commit_sha = _git(["rev-parse", "HEAD"], home_dir).stdout.strip()

    if not skip_push:
        push_res = _git(
            [
                "push",
                "--force-with-lease",
                "origin",
                f"HEAD:{working_branch}",
            ],
            home_dir,
            timeout=120,
        )
        if push_res.returncode != 0:
            return {
                "error": "push_failed",
                "stderr": push_res.stderr,
                "commit_sha": commit_sha,
            }

    return {
        "snapshot_dir": str(backup_rel) + "/",
        "files_preserved": restored,
        "commit_sha": commit_sha,
        "working_branch": working_branch,
    }


@router.post("/api/git/reset-to-main-preserve-state")
async def reset_to_main_preserve_state():
    """Adopt origin/main as the baseline, preserving allowlisted files (S3, #384).

    The sync-time counterpart to the persistent-state allowlist (#383).
    Snapshots every file matching the allowlist to `.trinity/backup/<ts>/`
    before running `git reset --hard origin/main`, then overlays the
    snapshot back, commits `Adopt main baseline, preserve state`, and
    pushes with `--force-with-lease`.

    Backend must verify the agent is not running a task before calling this
    endpoint; the check lives there because this server has no view of the
    activity service.
    """
    home_dir = Path("/home/developer")
    try:
        result = reset_to_main_preserve_state_impl(home_dir)
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Git operation timed out")

    err = result.get("error")
    if err == "no_git_config":
        raise HTTPException(
            status_code=409,
            detail="Agent has no git configuration",
            headers={"X-Conflict-Type": "no_git_config"},
        )
    if err == "no_remote_main":
        raise HTTPException(
            status_code=409,
            detail="Remote origin has no main branch",
            headers={"X-Conflict-Type": "no_remote_main"},
        )
    if err:
        stderr = result.get("stderr", "")
        detail = f"{err}: {stderr[:500]}" if stderr else err
        raise HTTPException(status_code=500, detail=detail)
    return result
