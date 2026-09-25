"""
Git sync endpoints for GitHub bidirectional sync.
"""
import asyncio
import functools
import json
import os
import re
import shutil
import stat
import subprocess
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from ..models import GitSyncRequest, GitPullRequest
from ..utils.git_conflict import classify_conflict
from ..utils.registered_run import run_registered
from .files import _read_persistent_state
from .snapshot import build_snapshot, restore_from_tar

from ..utils.credential_sanitizer import redact_url_userinfo

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
        # #2742: sweep-registered. Reached from the status path AND from
        # `_conflict_response`, i.e. the 409 path of every locked endpoint —
        # `run_registered` takes neither `capture_output` nor `text` (it hardcodes
        # PIPE + text in Popen), so both kwargs are deleted, not renamed.
        result = run_registered(
            ["git", "rev-list", "--left-right", "--count", f"origin/{branch}...HEAD"],
            cwd=str(home_dir), timeout=10,
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
    "git_dir_bytes": None,  # #1596: last-measured .git on-disk size
    "pack_count": None,  # #1595: packs from `git count-objects -v`
    "loose_objects": None,  # #1595: loose objects from `git count-objects -v`
    "maintenance_status": None,  # #1595: last maintenance outcome string
    "maintenance_failures": 0,  # #1595: consecutive failed maintenance attempts
    "maintenance_next_attempt_at": None,  # #1595: backoff gate (ISO timestamp)
    "last_lock_recovery": None,  # #2742: the boot reap's own record of a wedge
    "last_successful_push_at": None,  # #3011: last cycle whose push landed
    "behind_after_fetch": None,  # #3011: commits origin/<branch> had that we lacked
    "last_pull_at": None,  # trinity-enterprise#703: the container's pull cycle
    "last_pull_status": "never",
    "last_pull_error": None,
    "behind_after_pull": None,
}


# #2742: hard ceiling on the agent-authored sync-state document (see
# `_read_sync_state_file`). Not a tunable — it is a boundary guard.
_SYNC_STATE_MAX_BYTES = 64 * 1024


def _sync_state_path(home_dir: Path) -> Path:
    return home_dir / ".trinity" / "sync-state.json"


def _read_sync_state_file(home_dir: Path) -> Dict:
    """Read `.trinity/sync-state.json` or return defaults on missing/corrupt."""
    path = _sync_state_path(home_dir)
    if not path.exists():
        return dict(_SYNC_STATE_DEFAULT)
    try:
        # #2742: bound the read before taking it. This file is FULLY
        # agent-authored (`merged.update(data)` below merges it wholesale) and
        # the backend's poller reads every agent concurrently via
        # asyncio.gather — so an unbounded read_text() on a runaway or hostile
        # sync-state.json OOMs this container's uvicorn AND the backend, once a
        # minute. 64 KiB is ~30x the largest legitimate document.
        size = path.stat().st_size
        if size > _SYNC_STATE_MAX_BYTES:
            logger.warning(
                "sync-state.json too large (%s bytes > %s), using defaults",
                size, _SYNC_STATE_MAX_BYTES,
            )
            return dict(_SYNC_STATE_DEFAULT)
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("sync-state.json root is not an object")
        merged = dict(_SYNC_STATE_DEFAULT)
        merged.update(data)
        return merged
    except (ValueError, json.JSONDecodeError):
        logger.warning("sync-state.json corrupt, returning default")
        return dict(_SYNC_STATE_DEFAULT)


# #1595: exponential backoff for failed maintenance — a repack that cannot
# converge (too big for the timeout, low disk) must not become a ~2/3-duty-cycle
# IO treadmill that also litters a multi-GB tmp_pack every 15 minutes.
_MAINTENANCE_BACKOFF_BASE_SECONDS = 3600
_MAINTENANCE_BACKOFF_CAP_SECONDS = 86400


def _write_sync_state_file(
    home_dir: Path,
    last_sync_status: str,
    last_sync_at: Optional[str] = None,
    last_error_summary: Optional[str] = None,
    git_dir_bytes: Optional[int] = None,
    pack_count: Optional[int] = None,
    loose_objects: Optional[int] = None,
    maintenance_status: Optional[str] = None,
    behind_after_fetch: Optional[int] = None,
) -> Dict:
    """Persist one sync outcome.

    consecutive_failures is bumped on `failed`, reset on `success`, untouched
    on `never`. last_error_summary is cleared on success, kept on never.
    git_dir_bytes (#1596) / pack_count / loose_objects (#1595) /
    behind_after_fetch (#3011) are updated when measured; a None here preserves
    the last known value. last_successful_push_at (#3011) is stamped on
    `success` only — a sync outcome here always means the push landed.

    maintenance_status (#1595) drives the maintenance backoff bookkeeping:
    "failed" increments maintenance_failures and pushes
    maintenance_next_attempt_at out exponentially (1h → 2h → … capped 24h);
    a "repacked …" success resets both; skip statuses ("skipped_low_disk",
    "backoff") are recorded without touching the counter; None (no maintenance
    considered this cycle) preserves all three fields.

    The write is atomic (temp file + os.replace): the writer lives in the
    auto-sync worker thread while `/api/git/status` reads on the event loop —
    a read landing mid-truncate would see defaults and mask a real failure
    streak in the backend's `agent_sync_state` upsert.
    """
    prior = _read_sync_state_file(home_dir)

    if last_sync_status == "failed":
        prior["consecutive_failures"] = (prior.get("consecutive_failures") or 0) + 1
        prior["last_error_summary"] = last_error_summary
    elif last_sync_status == "success":
        prior["consecutive_failures"] = 0
        prior["last_error_summary"] = None
    # else 'never' — leave counters alone

    if git_dir_bytes is not None:
        prior["git_dir_bytes"] = git_dir_bytes
    if pack_count is not None:
        prior["pack_count"] = pack_count
    if loose_objects is not None:
        prior["loose_objects"] = loose_objects
    if behind_after_fetch is not None:
        prior["behind_after_fetch"] = behind_after_fetch

    if maintenance_status is not None:
        prior["maintenance_status"] = maintenance_status
        if maintenance_status == "failed":
            prior_failures = prior.get("maintenance_failures")
            if not isinstance(prior_failures, int) or isinstance(prior_failures, bool):
                prior_failures = 0  # sync-state is agent-writable JSON — never trust it
            failures = prior_failures + 1
            prior["maintenance_failures"] = failures
            # Exponent clamped: the cap is reached at 2^5 anyway, and an
            # unclamped 2**N on a tampered counter is a bignum memory bomb.
            backoff = min(
                _MAINTENANCE_BACKOFF_BASE_SECONDS * (2 ** min(failures - 1, 10)),
                _MAINTENANCE_BACKOFF_CAP_SECONDS,
            )
            prior["maintenance_next_attempt_at"] = (
                datetime.now(timezone.utc) + timedelta(seconds=backoff)
            ).isoformat()
        elif maintenance_status.startswith("repacked"):
            prior["maintenance_failures"] = 0
            prior["maintenance_next_attempt_at"] = None
        # skip statuses ("skipped_low_disk", "backoff"): record only

    prior["last_sync_status"] = last_sync_status
    prior["last_sync_at"] = last_sync_at or datetime.now(timezone.utc).isoformat()
    if last_sync_status == "success":
        prior["last_successful_push_at"] = prior["last_sync_at"]

    path = _sync_state_path(home_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(prior, indent=2))
    os.replace(tmp_path, path)
    return prior


def _patch_sync_state(home_dir: Path, updates: Dict) -> Dict:
    """Atomically set ONLY the given keys in `sync-state.json` (#2742).

    A dedicated writer is mandatory, not tidiness: `_write_sync_state_file`
    unconditionally stamps `last_sync_at = now`, so reusing it to record an
    observability field would mark a never-synced agent as freshly synced and
    turn its dashboard dot green. This one never touches `last_sync_status`,
    `last_sync_at` or `consecutive_failures`.

    Same atomic tmp + `os.replace` posture as the sync writer, and the same
    read-modify-write race against it — which is why the caller set is kept tiny
    (one write per container boot), and why the stuck-lock detector's per-tick
    ledger deliberately does NOT live in this file.
    """
    state = _read_sync_state_file(home_dir)
    state.update(updates)
    path = _sync_state_path(home_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(state, indent=2))
    os.replace(tmp_path, path)
    return state


# #2742: written by `startup.sh` when the boot reap actually removed something.
_LOCK_RECOVERY_MARKER = ".trinity/lock-recovery.json"
_LOCK_RECOVERY_MARKER_MAX_BYTES = 4096


def _record_lock_recovery(home_dir: Path) -> Optional[Dict]:
    """Fold `startup.sh`'s boot-reap marker into sync-state, once (#2742).

    The reap itself is the safe half of AC3 and already existed — it runs where
    "no git process is running" is definitional. The missing half was that it
    was silent. This turns the one-shot marker into a durable
    `sync_state.last_lock_recovery` the backend poller can see, then deletes the
    marker so the event is reported once rather than every minute forever.

    Server-composed on read: the marker is written by our own startup script,
    but it lives in an agent-writable directory, so only the parsed timestamp
    and a bounded label survive. Best-effort throughout — a failure here must
    never fail a status read.
    """
    marker = home_dir / _LOCK_RECOVERY_MARKER
    try:
        if not marker.is_file():
            return None
        if marker.stat().st_size > _LOCK_RECOVERY_MARKER_MAX_BYTES:
            marker.unlink(missing_ok=True)
            return None
        data = json.loads(marker.read_text())
        if not isinstance(data, dict):
            marker.unlink(missing_ok=True)
            return None
        record = {
            "at": str(data.get("at") or "")[:64],
            "locks": str(data.get("locks") or "")[:200],
            "source": "startup_reap",
        }
        _patch_sync_state(home_dir, {"last_lock_recovery": record})
        marker.unlink(missing_ok=True)
        logger.warning(
            "git lock recovery recorded from the boot reap (locks=%s)",
            record["locks"],
        )
        return record
    except (OSError, ValueError, json.JSONDecodeError):
        logger.debug("could not fold the lock-recovery marker", exc_info=True)
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            pass
        return None


# ---------------------------------------------------------------------------
# Stuck-lock OBSERVER (#2742) — reports, never unlinks. Read the reasoning
# before shortening any of it; every clause below was a measurement.
# ---------------------------------------------------------------------------

# Number of consecutive unchanged sightings of the SAME inode before a lock is
# called a wedge, and the minimum wall span those sightings must cover. The
# tunable is the SIGHTING COUNT, not a wall-clock age, and that is the whole
# point: `st_mtime` on an index.lock is stamped at create and never advances
# (git does not write into the fd until the final flush), so age measures the
# IN-FLIGHT OPERATION, not abandonment. Measured: a healthy `git add -A` holds a
# 0-byte lock for 100% of its life — 3.9 s on 450 MB, 29 s at 60 000 files,
# 155 s with a `clean` filter configured. A one-tick age gate cannot tell that
# apart from a wedge; two observations of an unchanged inode can. It is also
# immune to a forward NTP step or a live migration, which instantly age every
# existing lock at once — the one clock direction that makes a false positive
# MORE likely.
_STUCK_LOCK_MIN_SIGHTINGS = 3
_STUCK_LOCK_MIN_AGE_SECONDS = 900  # 15 min

# The sighting ledger, in memory and deliberately NOT in sync-state.
#
# Per-tick writes into the agent-authored sync-state document would race the
# auto-sync writer's own read-modify-write and could drop `consecutive_failures`
# or `last_sync_status` — an observability path corrupting the very feed it
# exists to brighten. And it does not need to be durable: the only thing that
# clears a genuinely wedged lock is a container restart, which clears this too.
# Keyed by absolute candidate path; monotonic clock so a clock step cannot forge
# stability either.
_LOCK_SIGHTINGS: Dict[str, Dict] = {}

_GITFILE_MAX_BYTES = 4096


def _resolve_git_dir(home_dir: Path) -> Optional[Path]:
    """The REAL gitdir for `home_dir`, or None when there isn't one to reason about.

    `home_dir/".git"` is a plain directory for an ordinary clone, but a FILE
    holding `gitdir: <path>` for a linked worktree (`git worktree add`) and for a
    submodule — both of which an agent can create in its own workspace. Assuming
    a directory makes the observer look in a place where the lock provably is
    not, while the real one sits at `<gitdir>/worktrees/<wt>/index.lock`.

    A symlinked `.git` returns None: a follow-stat reasons about one file while
    any action would touch another, and this code refuses to be in that position
    even though it only ever reads.
    """
    git_path = home_dir / ".git"
    st = os.lstat(git_path)  # OSError if absent — caller owns it
    if stat.S_ISLNK(st.st_mode):
        return None
    if stat.S_ISDIR(st.st_mode):
        return git_path
    if not stat.S_ISREG(st.st_mode):
        return None
    if st.st_size > _GITFILE_MAX_BYTES:
        return None
    text = git_path.read_text(errors="replace")
    for line in text.splitlines():
        if line.startswith("gitdir:"):
            target = line.split("gitdir:", 1)[1].strip()
            if not target:
                return None
            resolved = Path(target)
            if not resolved.is_absolute():
                resolved = (home_dir / resolved).resolve()
            return resolved if resolved.is_dir() else None
    return None


def _index_lock_candidates(git_dir: Path):
    """`<gitdir>/index.lock` plus the submodule and linked-worktree indexes.

    A lock under `modules/` or `worktrees/` is covered by neither the per-cycle
    reaper nor (before #2742) the boot reap, so those wedges used to be
    permanent and survive every restart.
    """
    yield git_dir / "index.lock"
    for sub in ("modules", "worktrees"):
        base = git_dir / sub
        try:
            if not base.is_dir():
                continue
            yield from base.glob("*/index.lock")
        except OSError:
            continue


def _index_lock_stuck(home_dir: Path) -> Optional[Dict]:
    """Report a lock that looks abandoned. **Never unlinks anything.**

    Why there is no delete here, in one place so nobody re-derives it:

    1. `st_size == 0` is the signature of a LIVE writer, not an abandoned one.
       git creates the lock with O_EXCL *before* walking the worktree and writes
       the new index into it only at the very end.
    2. `st_mtime` never advances, so age measures the in-flight operation.
    3. A wrong unlink is permanent and strictly worse than the wedge it repairs:
       git renames by PATH, so after an unlink a second git owns the path and the
       first git's closing `rename(index.lock, index)` promotes the second's
       in-flight file onto `.git/index`. The corrupting process exits rc=0 with
       empty stderr, and the resulting 0-byte index is cleared by nothing —
       not the boot reap, not `_reap_stale_git_litter`, not `git reset`.
    4. "No process holds this lock" is provable for free only when no processes
       exist, i.e. at container boot — which is exactly where the repair lives.
    5. `_REPO_LOCK` would not help: it excludes this server's own auto-sync
       cycle and nothing else — not the agent's own `git add`, which is the
       entire premise of #2742.

    It also takes NO lock: an `lstat` needs no mutual exclusion, and holding
    `_REPO_LOCK` across the observation would make a status poll a brand-new
    source of 409 `agent_busy` on an operator's `POST /api/git/sync`.

    Wrapped end to end in its own `except OSError` — it runs inside
    `_compute_git_status`'s `try`, whose tail is `HTTPException(500)`, and a 500
    makes the backend poller write NOTHING, so an EACCES or a gitlink `.git`
    would silently darken the very feed this exists to brighten.
    """
    try:
        git_dir = _resolve_git_dir(home_dir)
        if git_dir is None:
            return None

        now = time.monotonic()
        seen_paths = set()
        report: Optional[Dict] = None

        for candidate in _index_lock_candidates(git_dir):
            key = str(candidate)
            seen_paths.add(key)
            try:
                st = os.lstat(candidate)
            except OSError:
                _LOCK_SIGHTINGS.pop(key, None)
                continue
            if not stat.S_ISREG(st.st_mode):
                _LOCK_SIGHTINGS.pop(key, None)
                continue

            fingerprint = (st.st_ino, st.st_mtime_ns, st.st_size)
            prior = _LOCK_SIGHTINGS.get(key)
            if prior is None or prior["fingerprint"] != fingerprint:
                # A changed (or brand-new) inode is a DIFFERENT operation — a
                # finishing git replacing the lock looks exactly like this, and
                # calling that a wedge is how a healthy 155-second `git add`
                # gets reaped. Start counting again from one.
                _LOCK_SIGHTINGS[key] = {
                    "fingerprint": fingerprint,
                    "first_seen": now,
                    "count": 1,
                }
                continue

            prior["count"] += 1
            stable_for = now - prior["first_seen"]
            if (
                prior["count"] >= _STUCK_LOCK_MIN_SIGHTINGS
                and stable_for >= _STUCK_LOCK_MIN_AGE_SECONDS
                and report is None
            ):
                report = {
                    "path": candidate.relative_to(git_dir).as_posix(),
                    "age_seconds": max(0, int(time.time() - st.st_mtime)),
                    "stable_for_seconds": int(stable_for),
                    "sightings": prior["count"],
                    "size_bytes": st.st_size,
                }

        # Forget candidates that no longer exist so the ledger cannot grow.
        for gone in [k for k in _LOCK_SIGHTINGS if k not in seen_paths]:
            _LOCK_SIGHTINGS.pop(gone, None)

        if report is not None:
            logger.warning(
                "git index lock has been unchanged for %ss across %s status "
                "reads (%s) — REPORTING ONLY, not removing it: a running git "
                "may still hold it, and container restart is the only race-free "
                "repair",
                report["stable_for_seconds"], report["sightings"], report["path"],
            )
        return report
    except OSError:
        logger.debug("stuck-lock observation failed (non-fatal)", exc_info=True)
        return None


# #1596: auto-sync commits the workspace on every heartbeat and never ran git
# maintenance, so `git gc --auto` (which only stacks incremental packs under this
# write pattern) let one agent's .git reach 44GB / 2,267 packs. Consolidate when
# the pack count crosses a threshold — bounded work, self-throttling (a healthy
# repo with few packs is a no-op). Non-destructive: repack rewrites packs +
# prunes redundant/unreachable objects; it does NOT rewrite reachable history
# (an opt-in squash policy is a separate follow-up).
#
# #1595: the base image now ships `gc.auto=0` (auto-gc always detached to PID 1
# and was SIGKILLed by the orphan sweep — it never once completed), so garbage
# accumulates as LOOSE objects, not packs. The trigger therefore fires on
# pack count OR loose-object count; this pass is the single owner of repo
# maintenance for the agent's home repo.
_GIT_MAINTENANCE_PACK_THRESHOLD = int(os.getenv("GIT_MAINTENANCE_PACK_THRESHOLD", "20"))
_GIT_MAINTENANCE_LOOSE_THRESHOLD = int(
    os.getenv("GIT_MAINTENANCE_LOOSE_THRESHOLD", "6700")  # git's own gc.auto default
)

# #1595: repo-level mutual exclusion. Moving the auto-sync cycle off the event
# loop (asyncio.to_thread) removed the accidental serialization the blocking
# loop provided — without this lock an operator POST /api/git/sync could
# interleave `git add/commit/push` with the threaded cycle's own (index.lock
# roulette), or run during a repack. Non-blocking everywhere: the cycle skips
# when an operator op is in flight; operator endpoints 409 (`agent_busy`)
# while a cycle/maintenance runs.
#
# #2742 — what it does NOT exclude, because misreading this produced a plan to
# treat a successful non-blocking acquire as evidence of quiescence. It is an
# in-process `threading.Lock`: it excludes the agent server's own auto-sync
# cycle and its own operator endpoints, and NOTHING ELSE. It does not exclude
# the agent's own git (Claude Code running `git add` in its turn) — the dominant
# index writer in a Trinity agent and the entire premise of #2742 — nor the
# backend's ~22 `docker exec` git sites (the repo says so itself at
# `services/git_service.py`: "the backend's docker exec runs outside the agent
# server's _REPO_LOCK"). Never treat holding it as "no git is running here".
_REPO_LOCK = threading.Lock()


def _with_repo_lock(fn):
    """Guard a mutating git endpoint with the repo lock (409 on contention)."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        if not _REPO_LOCK.acquire(blocking=False):
            return _conflict_response(
                status_code=409,
                detail="Repository busy: auto-sync or git maintenance in progress",
                conflict_type="agent_busy",
                stderr="",
                home_dir=Path("/home/developer"),
                branch=None,
            )
        try:
            return await fn(*args, **kwargs)
        finally:
            _REPO_LOCK.release()

    return wrapper


def _maintenance_timeout_seconds() -> int:
    """Repack budget — read at CALL time (#1595): an import-time copy makes env
    monkeypatching silently inert and lets the transient-pid TTL drift from the
    real timeout. Tunable so a first pass on a multi-GB repo can converge."""
    raw = os.getenv("GIT_MAINTENANCE_TIMEOUT_SECONDS", "1800")
    try:
        value = int(raw)
        return value if value > 0 else 1800
    except ValueError:
        return 1800


def _count_git_packs(home_dir: Path) -> int:
    try:
        return sum(1 for _ in (home_dir / ".git" / "objects" / "pack").glob("*.pack"))
    except Exception:
        return 0


def _git_dir_bytes(home_dir: Path) -> Optional[int]:
    """#1596: on-disk size of the agent's ``.git`` (observability). Uses ``du -sb``
    (a fast C tree walk — safe even at millions of objects). None on any error."""
    git_dir = home_dir / ".git"
    if not git_dir.exists():
        return None
    try:
        res = run_registered(
            ["du", "-sb", str(git_dir)], timeout=60, check=True,
        )
        return int(res.stdout.split()[0])
    except Exception:
        return None


def _collect_git_object_stats(home_dir: Path) -> Dict:
    """#1595: one `git count-objects -v` → loose count, pack count, pack bytes.

    Returns {"loose_objects": int|None, "pack_count": int|None,
    "size_pack_kb": int|None} — None fields on any failure so callers degrade
    to observability-only loss, never a failed sync.
    """
    stats: Dict = {"loose_objects": None, "pack_count": None, "size_pack_kb": None}
    if not (home_dir / ".git").exists():
        return stats
    try:
        res = run_registered(
            ["git", "count-objects", "-v"], cwd=str(home_dir), timeout=60, check=True,
        )
        fields = {}
        for line in res.stdout.splitlines():
            key, _, value = line.partition(":")
            try:
                fields[key.strip()] = int(value.strip())
            except ValueError:
                continue
        stats["loose_objects"] = fields.get("count")
        stats["pack_count"] = fields.get("packs")
        stats["size_pack_kb"] = fields.get("size-pack")
    except Exception:
        logger.debug("count-objects failed; git object stats unavailable", exc_info=True)
    return stats


def _reap_stale_git_litter(home_dir: Path, *, repack_budget_seconds: int) -> None:
    """#1595: remove lock/tmp litter a SIGKILLed git process leaves behind.

    Runs at the TOP of every cycle (not only on the success+threshold path —
    a repo wedged on `index.lock` fails at `git add` and would never reach a
    maintenance-only placement). Age gates make removal race-free:
    - gc.pid / index.lock older than 1h: no legitimate git op holds either
      that long; a stale index.lock silently froze agents for 12 days.
    - tmp_pack_*/tmp_obj_*/tmp_idx_* older than the repack budget + 300s:
      maintenance is serialized (repo lock), so anything older than the
      current attempt's budget is definitionally abandoned.
    """
    git_dir = home_dir / ".git"
    if not git_dir.exists():
        return
    now = time.time()
    for rel in ("gc.pid", "index.lock"):
        target = git_dir / rel
        try:
            if target.exists() and now - target.stat().st_mtime > 3600:
                target.unlink()
                logger.warning("reaped stale git lock: %s", rel)
        except OSError:
            continue
    pack_dir = git_dir / "objects" / "pack"
    tmp_age_floor = repack_budget_seconds + 300
    for pattern in ("tmp_pack_*", "tmp_obj_*", "tmp_idx_*", "tmp_rev_*"):
        try:
            candidates = list(pack_dir.glob(pattern))
        except OSError:
            continue
        for target in candidates:
            try:
                if now - target.stat().st_mtime > tmp_age_floor:
                    target.unlink()
                    logger.info("reaped abandoned pack temp: %s", target.name)
            except OSError:
                continue


def _maybe_run_git_maintenance(home_dir: Path, stats: Dict) -> Optional[str]:
    """#1596/#1595: consolidate .git when packs or loose objects pile up.
    Best-effort, non-fatal — a failed/skipped maintenance never fails the sync.
    Returns a short status (None = not considered this cycle).

    Guards (#1595):
    - backoff: skip until `maintenance_next_attempt_at` after a failure —
      a non-converging repack must not retry every 15-minute cycle.
    - disk preflight: repack writes the full new pack BEFORE deleting old
      ones; on a near-full disk that is a worse incident than the bloat.
    - prune grace: `--unpack-unreachable=1.hour.ago` / `--prune=1.hour.ago`
      instead of `now` — Claude executions run git concurrently by design,
      and zero-grace pruning of their just-written, not-yet-referenced
      objects corrupts the in-flight operation. 1h > any single git op.
    - memory bound: pack.threads=1 + pack.windowMemory keep repack RSS
      inside the agent cgroup (an OOM kill of uvicorn IS the outage).
    """
    packs = stats.get("pack_count")
    loose = stats.get("loose_objects")
    if packs is None and loose is None:
        return None  # no data — never repack blind
    if (packs or 0) < _GIT_MAINTENANCE_PACK_THRESHOLD and (
        (loose or 0) < _GIT_MAINTENANCE_LOOSE_THRESHOLD
    ):
        return None

    state = _read_sync_state_file(home_dir)
    next_attempt = state.get("maintenance_next_attempt_at")
    if next_attempt:
        try:
            # TypeError guard: a hand-edited naive timestamp must degrade to
            # "proceed", not fail every future sync cycle (aware > naive raises).
            if datetime.fromisoformat(next_attempt) > datetime.now(timezone.utc):
                return "backoff"
        except (ValueError, TypeError):
            pass  # unparseable/naive timestamp — proceed

    size_pack_kb = stats.get("size_pack_kb")
    if size_pack_kb:
        try:
            free = shutil.disk_usage(str(home_dir)).free
            if free < size_pack_kb * 1024 * 1.1:
                logger.warning(
                    "git maintenance skipped: free disk %d < 1.1x pack bytes %d",
                    free, size_pack_kb * 1024,
                )
                return "skipped_low_disk"
        except OSError:
            pass

    timeout = _maintenance_timeout_seconds()
    try:
        # -A + --unpack-unreachable=<grace>: unreachable objects younger than
        # the grace survive as loose (concurrent-writer safety); older ones —
        # the actual bloat — are dropped. -d deletes now-redundant packs;
        # -l local only. gc then prunes aged loose garbage and expires reflogs.
        run_registered(
            [
                "git", "-c", "pack.threads=1", "-c", "pack.windowMemory=128m",
                "repack", "-A", "-d", "-l", "-q",
                "--unpack-unreachable=1.hour.ago",
            ],
            cwd=str(home_dir), timeout=timeout, check=True,
        )
        run_registered(
            ["git", "gc", "--quiet", "--prune=1.hour.ago"],
            cwd=str(home_dir), timeout=600, check=False,
        )
        after = _count_git_packs(home_dir)
        logger.info("git maintenance: consolidated %s packs -> %d", packs, after)
        return f"repacked {packs}->{after}"
    except Exception as exc:  # never let maintenance break the sync loop
        logger.warning("git maintenance failed (non-fatal): %s", exc)
        return "failed"


def _is_missing_remote_ref(stderr: Optional[str]) -> bool:
    """`git fetch origin <branch>` failed only because the branch is not on the
    remote yet (a working branch whose first push has not happened)."""
    return "couldn't find remote ref" in (stderr or "").lower()


def _is_shared_source_branch(home_dir: Path, branch: str) -> bool:
    """#3011: True when the heartbeat must NOT push — a source-mode agent
    (pull-only by contract) sitting on the repo's default branch.

    Fork-to-own agents are source-mode too but own their fork's `main`; they are
    recognised by `GIT_UPSTREAM_REPO` (baked at creation) or, since that env is
    not re-derived on recreate, by the `upstream` remote startup.sh writes into
    `.git/config` on the persistent volume.
    """
    if os.getenv("GIT_SOURCE_MODE", "").lower() != "true":
        return False
    if os.getenv("GIT_UPSTREAM_REPO"):
        return False
    upstream = run_registered(
        ["git", "remote", "get-url", "upstream"], cwd=str(home_dir), timeout=10,
    )
    if upstream.returncode == 0:
        return False
    head = run_registered(
        ["git", "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD"],
        cwd=str(home_dir), timeout=10,
    ).stdout.strip()
    if head.startswith("origin/"):
        return branch == head[len("origin/"):]
    # origin/HEAD unset (not a fresh clone): fall back to the conventional names.
    return branch in ("main", "master")


def _rebase_onto_remote(home_dir: Path, branch: str) -> Optional[str]:
    """#3011: rebase local commits onto the freshly fetched `origin/<branch>`.

    Returns None on a clean rebase, else the error summary to record. Any
    failure is aborted, so the repo is back exactly where it was — a conflict is
    never resolved automatically, the remote never overwritten, nothing reset.
    """
    try:
        rebase = run_registered(
            ["git", "rebase", "--autostash", f"origin/{branch}"],
            cwd=str(home_dir), timeout=120,
        )
    except subprocess.TimeoutExpired:
        # Killed mid-rebase: abort, or the next cycle meets a half-done rebase.
        run_registered(["git", "rebase", "--abort"], cwd=str(home_dir), timeout=60)
        return f"rebase timed out on {branch}"
    if rebase.returncode == 0:
        return None
    conflicted = run_registered(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=str(home_dir), timeout=10,
    ).stdout.strip()
    abort = run_registered(
        ["git", "rebase", "--abort"], cwd=str(home_dir), timeout=60,
    )
    if abort.returncode != 0:
        logger.error(
            "auto-sync: rebase --abort failed on %s: %s",
            branch, _summarize_git_error(abort.stderr),
        )
    output = f"{rebase.stdout or ''}\n{rebase.stderr or ''}"
    if conflicted or "CONFLICT" in output:
        return f"diverged: rebase conflict on {branch}"
    return _summarize_git_error(rebase.stderr or rebase.stdout or "rebase failed")


def _executions_in_flight() -> Optional[int]:
    """How many executions are running on this agent, or None if unknown.
    Indirection so the pull cycle's execution gate is unit-testable."""
    try:
        from ..services.process_registry import get_process_registry
        return len(get_process_registry().list_running())
    except Exception:  # noqa: BLE001 — unknown is treated as busy by the caller
        logger.debug("pull: could not read the process registry", exc_info=True)
        return None


def _record_pull(home_dir: Path, status: str, *, error: Optional[str] = None,
                 behind: Optional[int] = None) -> Dict:
    """Persist one pull outcome WITHOUT touching the push fields — the push's
    `consecutive_failures` / `last_sync_*` stay the push's (trinity-enterprise#703)."""
    _patch_sync_state(home_dir, {
        "last_pull_at": datetime.now(timezone.utc).isoformat(),
        "last_pull_status": status,
        "last_pull_error": error,
        "behind_after_pull": behind,
    })
    result = {"status": status}
    if error:
        result["error"] = error
    return result


def _integrate_remote(home_dir: Path, branch: str, ahead: int) -> Optional[str]:
    """Bring `origin/<branch>` into the checked-out branch without ever losing
    local work (trinity-enterprise#703). Returns None, or the error recorded.

    Uncommitted tracked edits are stashed EXPLICITLY — not `--autostash`: when
    incoming commits touch the same files, autostash's re-apply conflicts and
    leaves the edits only in the stash while the pull reports success, so the
    files silently lose them. Here a conflicting re-apply is undone: back to the
    pre-pull HEAD, where the stash applies cleanly to its own base, and the
    failure is recorded. Untracked files are never stashed; one an incoming
    commit would overwrite fails the fast-forward/rebase instead.
    """
    def git(*args, timeout=60):
        return run_registered(["git", *args], cwd=str(home_dir), timeout=timeout)

    pre_head = git("rev-parse", "HEAD", timeout=10).stdout.strip()
    dirty = git("status", "--porcelain", "--untracked-files=no", timeout=10).stdout.strip()
    stashed = False
    if dirty:
        push = git("stash", "push", "-m", "trinity-pull (ent#703)", timeout=30)
        if push.returncode != 0:
            return _summarize_git_error(push.stderr or push.stdout or "stash failed")
        stashed = True

    if ahead == 0:
        step = git("merge", "--ff-only", f"origin/{branch}")
        err = None if step.returncode == 0 else _summarize_git_error(
            step.stderr or step.stdout or "fast-forward failed")
    else:
        err = _rebase_onto_remote(home_dir, branch)  # aborts itself on failure

    if err:
        if stashed:  # nothing moved (ff refused / rebase aborted): put the edits back
            git("stash", "pop", timeout=30)
        return err

    if stashed:
        pop = git("stash", "pop", timeout=30)
        if pop.returncode != 0:
            # The edits collide with what came in. Undo the pull — the stash is
            # still there — and re-apply on the base it was taken from.
            git("reset", "--hard", pre_head, timeout=30)
            back = git("stash", "pop", timeout=30)
            if back.returncode != 0:
                return ("local edits conflict with incoming changes on "
                        f"{branch}; they are kept in `git stash`")
            return f"local edits conflict with incoming changes on {branch}"
    return None


def _run_pull_once(home_dir: Path) -> Dict:
    """One pull cycle (trinity-enterprise#703, invariant G3): bring origin's
    commits for the checked-out branch to the agent.

    Source-mode agents are checked out on their source branch and working-branch
    agents on their working branch, so the checked-out branch is the one to pull.

    - Never while an execution runs (a rebase under a running turn changes the
      files it is reading) — checked before the fetch and again right before the
      tree is touched; an unreadable registry counts as busy.
    - Serialised with the push cycle and the operator git endpoints on
      `_REPO_LOCK`; a busy repo skips quietly.
    - `_integrate_remote`: a fast-forward when nothing is committed locally, a
      rebase (aborted on conflict) when something is; uncommitted edits are
      stashed explicitly and re-applied, and a pull whose incoming changes
      collide with them is undone. Local work is never discarded; a failure is
      recorded, not resolved.
    """
    # The lock first: every write to sync-state.json happens under it, because
    # the push cycle read-modify-writes the same file from another thread.
    if not _REPO_LOCK.acquire(blocking=False):
        return {"status": "skipped", "reason": "repo_busy"}
    try:
        running = _executions_in_flight()
        if running is None or running > 0:
            return _record_pull(home_dir, "skipped", error=(
                "execution in flight" if running else "execution state unknown"))
        branch = run_registered(
            ["git", "symbolic-ref", "--short", "-q", "HEAD"],
            cwd=str(home_dir), timeout=10,
        ).stdout.strip()
        if not branch:
            return _record_pull(home_dir, "failed", error="detached HEAD: nothing to pull onto")
        fetch = run_registered(
            ["git", "fetch", "origin", branch], cwd=str(home_dir), timeout=120,
        )
        if fetch.returncode != 0:
            if _is_missing_remote_ref(fetch.stderr):
                return _record_pull(home_dir, "success", behind=0)  # not on origin yet
            return _record_pull(home_dir, "failed", error=_summarize_git_error(
                fetch.stderr or fetch.stdout or "fetch failed"))
        ahead, behind = _compute_ahead_behind(home_dir, branch)
        if behind == 0:
            return _record_pull(home_dir, "success", behind=0)
        running = _executions_in_flight()
        if running is None or running > 0:
            return _record_pull(home_dir, "skipped", behind=behind, error=(
                "execution in flight" if running else "execution state unknown"))
        err = _integrate_remote(home_dir, branch, ahead)
        if err:
            return _record_pull(home_dir, "failed", behind=behind, error=err)
        _, after = _compute_ahead_behind(home_dir, branch)
        logger.info("pull: %s brought in %s commit(s) on %s", home_dir, behind, branch)
        return _record_pull(home_dir, "success", behind=after)
    except subprocess.CalledProcessError as exc:
        return _record_pull(home_dir, "failed", error=_summarize_git_error(
            exc.stderr or exc.stdout or str(exc)))
    except Exception as exc:  # noqa: BLE001 — the loop must never die
        return _record_pull(home_dir, "failed", error=_summarize_git_error(str(exc)))
    finally:
        _REPO_LOCK.release()


def _run_auto_sync_once(home_dir: Path) -> Dict:
    """One auto-sync cycle: reap stale lock litter, measure, stage, commit if
    dirty, fetch + rebase onto the remote branch, push, maybe consolidate .git.
    Records outcome.

    #3011: the cycle reconciles before it pushes — a foreign push to the branch
    is rebased over (lease-protected push), a conflicting one is aborted and
    recorded as `diverged: …` for the `sync_failing` path, and a source-mode
    agent on the default branch refuses to push at all. Conflict RESOLUTION
    still belongs to the operator-initiated `sync_to_github` endpoint.

    #1595: runs in a worker thread (asyncio.to_thread) so a long repack no
    longer starves /health, the 5s liveness heartbeat, and chat. Every
    subprocess routes through `run_registered` — a bare agent-server child is
    indistinguishable from a leaked orphan and dies whenever it straddles a
    sweep tick. Serialized against the operator git endpoints via _REPO_LOCK.
    """
    now = datetime.now(timezone.utc).isoformat()

    if not _REPO_LOCK.acquire(blocking=False):
        # Operator op in flight — skip quietly; not a sync failure.
        logger.info("auto-sync: repo busy (operator git op in flight), skipping cycle")
        return {"status": "skipped", "reason": "repo_busy"}

    try:
        _reap_stale_git_litter(
            home_dir, repack_budget_seconds=_maintenance_timeout_seconds()
        )

        # Measure BEFORE the sync steps so the failure paths carry metrics too —
        # the sickest repos (wedged on a lock, failing push) must not go dark.
        stats = _collect_git_object_stats(home_dir)
        gdb = _git_dir_bytes(home_dir)

        def _fail(err: str, behind: Optional[int] = None) -> Dict:
            _write_sync_state_file(
                home_dir, "failed", last_sync_at=now, last_error_summary=err,
                git_dir_bytes=gdb,
                pack_count=stats.get("pack_count"),
                loose_objects=stats.get("loose_objects"),
                behind_after_fetch=behind,
            )
            return {"status": "failed", "error": err}

        try:
            branch = run_registered(
                ["git", "symbolic-ref", "--short", "-q", "HEAD"],
                cwd=str(home_dir), timeout=10,
            ).stdout.strip()
            if not branch:
                return _fail("detached HEAD: auto-sync needs a branch")

            # #3011: a source-mode agent is pull-only by contract. On the repo's
            # default branch its commits would land straight on `main` of a repo
            # that may deploy on push — refuse BEFORE committing, so the clone
            # stays a clean mirror. Fork-to-own agents own their fork's `main`.
            if _is_shared_source_branch(home_dir, branch):
                return _fail(f"refused: source-mode on {branch}")

            # Stage everything.
            run_registered(
                ["git", "add", "-A"], cwd=str(home_dir), timeout=30, check=True,
            )

            # Is there anything to commit? check=True: a swept/killed status
            # (rc −9, empty stdout) must fail the cycle loudly, not be
            # silently misread as "nothing to commit".
            status = run_registered(
                ["git", "status", "--porcelain"],
                cwd=str(home_dir), timeout=10, check=True,
            )
            if status.stdout.strip():
                commit_msg = f"Trinity auto-sync: {now}"
                run_registered(
                    ["git", "commit", "-m", commit_msg],
                    cwd=str(home_dir), timeout=30, check=True,
                )

            # #3011: reconcile with the remote before pushing — anyone else's
            # push to this branch otherwise fails every later cycle
            # non-fast-forward, forever.
            fetch = run_registered(
                ["git", "fetch", "origin", branch], cwd=str(home_dir), timeout=120,
            )
            behind: Optional[int] = None
            push_cmd = ["git", "push", "origin", "HEAD"]
            if fetch.returncode != 0:
                if not _is_missing_remote_ref(fetch.stderr):
                    return _fail(_summarize_git_error(
                        fetch.stderr or fetch.stdout or "fetch failed"))
                # Branch not on the remote yet (a fresh working branch): the
                # first push creates it — nothing to reconcile.
                behind = 0
            else:
                _, behind = _compute_ahead_behind(home_dir, branch)
                if behind > 0:
                    fetched_sha = run_registered(
                        ["git", "rev-parse", f"origin/{branch}"],
                        cwd=str(home_dir), timeout=10, check=True,
                    ).stdout.strip()
                    rebase_err = _rebase_onto_remote(home_dir, branch)
                    if rebase_err:
                        return _fail(rebase_err, behind)
                    # Lease on the ref we rebased onto: a push that lands
                    # between our fetch and this push is rejected, never
                    # overwritten. Never the bare forced form.
                    push_cmd = [
                        "git", "push",
                        f"--force-with-lease=refs/heads/{branch}:{fetched_sha}",
                        "origin", f"HEAD:refs/heads/{branch}",
                    ]

            push = run_registered(push_cmd, cwd=str(home_dir), timeout=300)
            if push.returncode != 0:
                return _fail(
                    _summarize_git_error(push.stderr or push.stdout or "push failed"),
                    behind,
                )

            # #1596/#1595: consolidate .git when packs or loose objects pile up
            # (self-throttling, non-fatal), then record the resulting size so
            # operators can watch the curve.
            maintenance = _maybe_run_git_maintenance(home_dir, stats)
            if maintenance and maintenance.startswith("repacked"):
                # Repack changed everything — re-measure for honest numbers.
                stats = _collect_git_object_stats(home_dir)
                gdb = _git_dir_bytes(home_dir)
            _write_sync_state_file(
                home_dir, "success", last_sync_at=now,
                git_dir_bytes=gdb,
                pack_count=stats.get("pack_count"),
                loose_objects=stats.get("loose_objects"),
                maintenance_status=maintenance,
                behind_after_fetch=behind,
            )
            result = {"status": "success"}
            if maintenance:
                result["maintenance"] = maintenance
            return result

        except subprocess.CalledProcessError as exc:
            err = _summarize_git_error(exc.stderr or exc.stdout or str(exc))
            _write_sync_state_file(
                home_dir, "failed", last_sync_at=now, last_error_summary=err,
                git_dir_bytes=gdb,
                pack_count=stats.get("pack_count"),
                loose_objects=stats.get("loose_objects"),
            )
            return {"status": "failed", "error": err}
        except Exception as exc:  # defensive — never let the loop crash
            err = _summarize_git_error(str(exc))
            _write_sync_state_file(
                home_dir, "failed", last_sync_at=now, last_error_summary=err,
                git_dir_bytes=gdb,
                pack_count=stats.get("pack_count"),
                loose_objects=stats.get("loose_objects"),
            )
            return {"status": "failed", "error": err}
    except Exception as exc:  # pre-measurement failure — keep the old contract
        err = _summarize_git_error(str(exc))
        _write_sync_state_file(home_dir, "failed",
                                last_sync_at=now, last_error_summary=err)
        return {"status": "failed", "error": err}
    finally:
        _REPO_LOCK.release()


def _redact_url_userinfo(text: str) -> str:
    """Strip credentials from URLs in git output (#1595 review).

    Kept as a name so the two git-stderr call sites read unchanged, but the rule
    itself now lives in `utils.credential_sanitizer` (ent#292): the same
    credential leaks through argv as well as stderr, so one regex serving both
    is what stops the next sink from re-deriving its own.
    """
    return redact_url_userinfo(text)


def _summarize_git_error(raw: str) -> str:
    """Trim git stderr to a 240-char one-liner (matches operator-queue field size).
    URL userinfo redacted — see :func:`_redact_url_userinfo`."""
    if not raw:
        return "unknown error"
    first = raw.strip().splitlines()[0]
    return _redact_url_userinfo(first)[:240]


def _get_pull_branch(current_branch: str, home_dir: Path) -> str:
    """Determine the upstream branch to pull from.

    For trinity/* working branches, pull from main instead of the working
    branch (which nobody pushes to externally). Falls back to current_branch
    if origin/main doesn't exist.
    """
    if not current_branch.startswith("trinity/"):
        return current_branch
    # #2742: sweep-registered (also reached from `sync_to_github` and
    # `pull_from_github` under `_with_repo_lock` — intended, see the module note).
    result = run_registered(
        ["git", "rev-parse", "--verify", "origin/main"],
        cwd=str(home_dir), timeout=10,
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
    # #2742: sweep-registered (also reached from `sync_to_github` under the lock).
    rev = run_registered(
        ["git", "rev-parse", f"origin/{branch}"],
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
                    "git_stderr": _redact_url_userinfo((stderr or "").strip())[:2000],
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


# #2742: the one repo this handler ever reads. A module constant rather than an
# inline literal so the single-flight slot (which is also module-global) and the
# computation agree on their subject by construction.
_STATUS_HOME_DIR = Path("/home/developer")

# #2742 — CALLER bound. Set at or just past the point every real client has
# already given up: the backend poller times out at 10 s, `git_service` at 30 s,
# so a follower waiting longer than 35 s can only produce work nobody awaits.
# Exceeding it is a 504, not a second computation.
_STATUS_FOLLOWER_WAIT_SECONDS = 35

# #2742 — COMPUTATION bound, deliberately a different number and a different
# kind of thing. The child timeouts sum to ~130 s nominal (see
# `_compute_git_status`'s docstring for the arithmetic) before `run_registered`'s
# post-killpg drain, and on this design a slow leader costs no follower threads
# but DOES hold the in-flight slot — so every caller in that window 504s. Cap it
# so a wedged leader releases the slot instead of monopolising it.
_STATUS_LEADER_DEADLINE_SECONDS = 90

# #2742 — the single-flight slot, keyed by resolved home path.
#
# Production has exactly one repo, so a bare `Optional[Future]` would be correct
# there — but any test that repoints `_STATUS_HOME_DIR` would then inherit a live
# slot from the previous test and be served the PREVIOUS repo's payload. Keying
# it costs three lines and removes the class.
#
# The check-and-set below is atomic WITHOUT a lock because there is no `await`
# between the test and the assignment and the agent server is single-process
# (`agent_server/main.py` calls `uvicorn.run(app, ...)` with no `workers=`). The
# BACKEND is not single-process, which is what the #2742 Redis lease is for.
_STATUS_INFLIGHT: Dict[str, "asyncio.Future"] = {}


def _parse_porcelain_z(stdout: str) -> list[dict]:
    """Parse `git status --porcelain -z` into `[{status, path[, orig_path]}]` (#2957).

    Records are NUL-separated `XY PATH`, never quoted. A rename or copy (`R`/`C`
    in either column) is followed by one extra record holding its origin, which
    is surfaced as `orig_path`. Nothing is stripped before slicing: a leading
    space IS the X column of an unstaged change.
    """
    records = [r for r in stdout.split("\0") if r]
    changes = []
    i = 0
    while i < len(records):
        rec = records[i]
        i += 1
        xy = rec[:2]
        entry = {"status": xy.strip(), "path": rec[3:]}
        if "R" in xy or "C" in xy:
            if i < len(records):
                entry["orig_path"] = records[i]
                i += 1
            else:
                logger.warning(f"git status -z: rename/copy record without origin: {rec!r}")
        changes.append(entry)
    return changes


def _compute_git_status(home_dir: Path) -> Dict:
    """The whole `/api/git/status` computation, as ONE blocking callable (#2742).

    Extracted verbatim from the route handler so the handler can become a thin
    wrapper. Nothing here is `async`: every step is a blocking `git` child, and
    running ~8 of them directly on the agent's event loop is what this extraction
    exists to stop.

    Raises the same `HTTPException` 504/500 the route always raised, so the
    contract at the boundary is unchanged.

    **Child-timeout budget, written down because the arithmetic decides the
    caller-side bounds.** Sequential worst case:
    `rev-parse` 10 + `status` 10 + `log` 10 + `fetch` **30** + `merge-base` 10 +
    `log`(ancestor) 10 + `remote get-url` 10 = 90, plus `_persist_last_remote_sha`
    10, `_get_pull_branch` 10 and `_dual_ahead_behind_payload` 10 (20 on a
    `trinity/*` branch) = **~130 s nominal**, before `run_registered`'s
    post-`killpg` drain of up to 10 s per timing-out child. The flow doc's old
    "~30 s worst case" was wrong and is corrected there.
    """
    try:
        # Get current branch
        branch_result = run_registered(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(home_dir),
            timeout=10,
        )
        current_branch = branch_result.stdout.strip() if branch_result.returncode == 0 else "unknown"

        # Get status (modified, untracked files)
        #
        # #2742 — THE call site this issue exists for. `git status --porcelain`
        # takes `.git/index.lock` on EVERY invocation in order to refresh the
        # index, whether or not it ends up rewriting it, for a window that scales
        # with index size (0.6 ms at one file, 12.8-15.4 ms at 20 000, 334-473 ms
        # measured in a real container). Driven by the backend's 60 s poll that
        # meant the platform took this agent's index lock ~2x/min, outside
        # `_REPO_LOCK`, racing the agent's own `git add` — and a sweep-killed
        # child orphaned a 0-byte lock that silently failed every later git write.
        # `--no-optional-locks` is precisely what the flag exists for.
        #
        # SCOPED to this one site on purpose. The auto-sync cycle's own status
        # (right after `git add -A`, under the repo lock) and the `sync`/`pull`
        # bodies keep the plain form: they are lock-serialized, they proceed to
        # stage/commit, and they WANT the refreshed stat cache. Flag form, not
        # `GIT_OPTIONAL_LOCKS=0`: `run_registered` has no `env=` kwarg, the env
        # form would silently change the mutating sites too, and only the argv is
        # assertable in a test. Needs git >= 2.15; bookworm ships 2.39.
        # #2957: `-z` — an outer strip ate the first line's X column, and plain
        # porcelain quotes odd paths and prints renames as `old -> new`.
        # `-z` also stops quoting non-ASCII, so git now emits raw filename bytes:
        # a strict decode of one non-UTF-8 name would 500 the whole status.
        # `backslashreplace` keeps it visible (`caf\\xe9.txt`) and JSON-safe.
        status_result = run_registered(
            ["git", "--no-optional-locks", "status", "--porcelain", "-z"],
            cwd=str(home_dir),
            timeout=10,
            errors="backslashreplace",
        )
        changes = _parse_porcelain_z(status_result.stdout) if status_result.returncode == 0 else []

        # Get last commit
        log_result = run_registered(
            ["git", "log", "-1", "--format=%H|%h|%s|%an|%ai"],
            cwd=str(home_dir),
            timeout=10,
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
        # #2742: `--no-optional-locks` closes only the index-lock half. A sweep
        # tick straddling this 30 s child still SIGKILLs it, and a killed fetch
        # can orphan `FETCH_HEAD.lock` / `refs/remotes/origin/*.lock` /
        # `packed-refs.lock` — which NO reaper covers, not even `startup.sh`'s
        # (its find is scoped to refs/ and logs/, and the other two sit directly
        # in .git/). Registering the child is what stops the poll producing them.
        fetch_result = run_registered(
            ["git", "fetch", "origin"],
            cwd=str(home_dir),
            timeout=30,
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
        pull_branch = _get_pull_branch(current_branch, home_dir)
        common_ancestor_sha = ""
        common_ancestor_age_days = None
        merge_base_result = run_registered(
            ["git", "merge-base", "HEAD", f"origin/{pull_branch}"],
            cwd=str(home_dir),
            timeout=10,
        )
        if merge_base_result.returncode == 0:
            common_ancestor_sha = merge_base_result.stdout.strip()
            if common_ancestor_sha:
                ancestor_date_result = run_registered(
                    ["git", "log", "-1", "--format=%cI", common_ancestor_sha],
                    cwd=str(home_dir),
                    timeout=10,
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
        remote_result = run_registered(
            ["git", "remote", "get-url", "origin"],
            cwd=str(home_dir),
            timeout=10,
        )
        remote_url = ""
        if remote_result.returncode == 0:
            url = remote_result.stdout.strip()
            # #2742: redact unconditionally. The old shape special-cased
            # `@github.com` and returned the URL VERBATIM otherwise — so for any
            # non-github.com remote (GitHub Enterprise, GitLab, a host-rewritten
            # origin) this response body carried the fully tokenized
            # `https://oauth2:<PAT>@host/...`, straight through
            # `git_service.get_git_status` (which returns `response.json()`
            # unmodified) to the UI and the MCP tool. ent#615 owns the broader
            # credential-in-argv class; this is the one line in it that this
            # diff was already moving.
            remote_url = redact_url_userinfo(url)

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
        # #2742: fold the boot reap's one-shot marker into sync-state BEFORE
        # reading it, so a recovery surfaces on the very first poll after the
        # restart that performed it rather than a minute later.
        _record_lock_recovery(home_dir)

        # #389: merge auto-sync heartbeat state (may be defaults if never run).
        response["sync_state"] = _read_sync_state_file(home_dir)
        # #2742: lift the recovery record to the top level too — the backend
        # rebuilds it from coerced values and never trusts the nested copy,
        # which `_read_sync_state_file` merges wholesale from agent-written JSON.
        response["lock_recovery"] = response["sync_state"].get("last_lock_recovery")
        # #3010: the auto-sync gate the loop is running with — the owner's DB
        # flag as last read, the same source `GET .../git/auto-sync` returns.
        from ..auto_sync import current_auto_sync_enabled
        response["auto_sync_enabled"] = current_auto_sync_enabled()
        # trinity-enterprise#703: the pull gate the pull loop is running with.
        from ..auto_sync import current_pull_sync_enabled
        response["pull_sync_enabled"] = current_pull_sync_enabled()
        # #2742: a currently-stuck lock is REPORTED, never removed. This is the
        # "tell the truth about state" half — a stale lock does not fail
        # `git status` (rc=0, empty stderr), so before this the read could not
        # see the wedge it was reporting on. That is the "silent" in #2742.
        response["index_lock_stuck"] = _index_lock_stuck(home_dir)
        # #2742: coalescing IS bounded staleness, and saying so is cheaper than
        # denying it. A follower arriving at t=29 s of a 30 s leader run is
        # served a 29-second-old snapshot — visibly, an operator pressing Sync
        # and seeing "1 ahead" right after a successful push. Stamp the age so
        # every consumer can see it. (TTL caching was rejected: serving late
        # followers a FRESH run reintroduces the overlapping fetch AC2 forbids.)
        response["computed_at"] = datetime.now(timezone.utc).isoformat()
        return response

    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Git operation timed out")
    except Exception as e:
        logger.error(f"Git status error: {e}")
        raise HTTPException(status_code=500, detail=f"Git status error: {str(e)}")


async def _run_status_computation(home_dir: Path) -> Dict:
    """Run ONE `_compute_git_status` in ONE worker thread, under its own deadline.

    `asyncio.to_thread` uses the loop's DEFAULT executor. On a 2-vCPU agent that
    is `min(32, cpu+4)` = 6 threads, and `services/headless_executor.py` records
    that `ctx.terminate`, auto-sync and pipe-close deliberately stay on that same
    pool — so parking followers in it is the #2433 starvation class, where a
    burst of status callers stalls EXECUTION TERMINATION. Exactly one thread per
    in-flight computation, ever; the callers coalesce on the loop instead.
    """
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_compute_git_status, home_dir),
            timeout=_STATUS_LEADER_DEADLINE_SECONDS,
        )
    except asyncio.TimeoutError:
        # The worker thread's git children are killed by their own
        # `run_registered` timeouts; what this bound protects is the SLOT.
        raise HTTPException(
            status_code=504,
            detail="Git status timed out (overall computation deadline)",
        )


@router.get("/api/git/status")
async def get_git_status():
    """
    Get git repository status including current branch, changes, and sync state.
    Only available for agents with git sync enabled.

    #2742 — single-flight, on the event loop. Three callers converge on this
    route (the backend's 60 s sync-health poll, the UI git panel's own 60 s poll
    while open, and the MCP `get_git_status` tool), and each computation runs a
    30 s `git fetch origin`, so concurrent callers used to stack overlapping
    fetches against one repo. Now the first caller starts the computation and
    every other caller awaits the SAME future.

    No 409 by design: this is a read, and `_with_repo_lock` on it would turn
    every poll into a contended write and flap the agent `unreachable`.

    Coalescing is bounded staleness, not free — see `computed_at` in the
    payload, which is the honest way to say so.
    """
    home_dir = _STATUS_HOME_DIR
    git_dir = home_dir / ".git"

    if not git_dir.exists():
        return {
            "git_enabled": False,
            "message": "Git sync not enabled for this agent"
        }

    key = str(home_dir)
    fut = _STATUS_INFLIGHT.get(key)
    if fut is None or fut.done():
        # Atomic by construction: no `await` between the test and the assignment.
        fut = asyncio.ensure_future(_run_status_computation(home_dir))
        _STATUS_INFLIGHT[key] = fut

        def _release_slot(done: "asyncio.Future", _key: str = key) -> None:
            # Only clear the slot if it still holds THIS future — a later flight
            # may already have claimed it.
            if _STATUS_INFLIGHT.get(_key) is done:
                _STATUS_INFLIGHT.pop(_key, None)
            # Mark a failure retrieved even if every waiter was cancelled, so a
            # disconnect storm cannot print "Future exception was never
            # retrieved" once per poll.
            if not done.cancelled():
                done.exception()

        fut.add_done_callback(_release_slot)

    try:
        # `shield` is load-bearing: a follower that times out or whose client
        # disconnects must not cancel the leader's computation for everybody
        # else. And the future is ALWAYS resolved — `asyncio.to_thread` ->
        # `run_in_executor` -> `_WorkItem.run` catches BaseException and calls
        # `set_exception` — so no hand-rolled set_result/set_exception pair and
        # no "leader vanished" fallback is needed here. Do not add one.
        return await asyncio.wait_for(
            asyncio.shield(fut), timeout=_STATUS_FOLLOWER_WAIT_SECONDS
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail="Git status busy: coalesced status did not complete in time",
        )


@router.post("/api/git/sync")
@_with_repo_lock
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
                timeout=300,
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
                timeout=300
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
                        timeout=300
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
@_with_repo_lock
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
@_with_repo_lock
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
