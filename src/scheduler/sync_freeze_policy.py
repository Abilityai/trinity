"""Sync-health policy: state, reason, recommendation and the freeze (trinity-enterprise#706).

ONE place decides whether an agent and its repository agree. Every surface that
shows sync health (the dashboard dot, `GET /api/agents/sync-health`,
`GET /api/agents/{name}/git/sync-state`, the internal freeze endpoint) and the
scheduler's own freeze gate call :func:`classify` — the frontend owns no
threshold.

CANONICAL SOURCE: src/backend/services/sync_freeze_policy.py. The scheduler is a
standalone image that cannot import the backend, so src/scheduler/sync_freeze_policy.py
is a BYTE-IDENTICAL vendored mirror (the failure_classifier.py precedent, #1088;
Invariant #5), pinned by tests/unit/test_ent706_sync_policy_parity.py. Edit this
file, then `cp` it over the mirror.

Stdlib only, no I/O, never raises on a malformed row: the scheduler calls this
on its fire path, and every number in the row was written by the agent.

Rules, evaluated together (the first red clause names the reason, the rest
append):

  unknown  no row, or the poller never observed the agent
  red      the last sync failed (any binding)
           a WORK agent diverged from origin for more than 24 h
           a WORK agent's tree dirty for more than 24 h
           auto-sync on and no heartbeat for 7 days (fresh observation only)
  yellow   a work agent diverged for 24 h or less
           a DEPLOYMENT diverged at any age, or dirty for more than 24 h —
           a deployment being behind is normal; it is never red on age
  green    otherwise

A work agent is `source_mode = 0 OR auto_sync_enabled = 1`: a working branch,
or a fork-to-own / bound-to-own agent that auto-syncs to its own repository
(those keep `source_mode = 1` in the DB). Everything else is a deployment.

freeze = freeze_schedules_if_sync_failing AND (
             sync_failing                      # #1808, unchanged, any binding
             OR (work agent AND diverged > 24 h AND observation fresh))

The freshness guard fails OPEN on purpose: the poller writes nothing for an
unreachable agent, so a stale `diverged_since` must never keep an agent frozen
after it may already have pushed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

SYNC_FAILURE_FREEZE_THRESHOLD = 3  # consecutive POLLS that saw `failed` (#1808)
DIVERGENCE_RED_SECONDS = 24 * 3600
DIRTY_RED_SECONDS = 24 * 3600
NO_HEARTBEAT_FLOOR_SECONDS = 7 * 24 * 3600
OBSERVATION_FRESH_SECONDS = 15 * 60
ERROR_EXCERPT_CHARS = 120

STATES = ("green", "yellow", "red", "unknown")

# The agent's own refusal text for a source-mode agent on the shared default
# branch (#3011, `failed: "refused: source-mode on <branch>"`).
_SOURCE_MODE_REFUSAL = "refused: source-mode"


def parse_ts(value: Any) -> Optional[datetime]:
    """An aware-UTC datetime from an ISO string or a datetime; None otherwise.

    A naive value is UTC — the convention of `parse_scheduler_ts` and of the
    backend's `parse_iso_timestamp`. Every comparison happens here in Python,
    never lexicographically in SQL (Invariant #16).
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and 0 < len(value) <= 64:
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_age(seconds: int) -> str:
    """`45m`, `26h`, `9d` — the shape of the operator-facing reason strings."""
    seconds = max(int(seconds), 0)
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 48 * 3600:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int) and value > 0:
        return value
    return 0


def _flag(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true")
    return bool(value)


def _age(now: datetime, then: Optional[datetime]) -> Optional[int]:
    if then is None:
        return None
    return max(int((now - then).total_seconds()), 0)


def is_work_agent(source_mode: Any, auto_sync_enabled: Any) -> bool:
    """`source_mode = 0 OR auto_sync_enabled = 1` (see the module docstring)."""
    return (not _flag(source_mode)) or _flag(auto_sync_enabled)


def classify(
    row: Optional[Dict[str, Any]],
    cfg: Optional[Dict[str, Any]],
    now: Optional[datetime] = None,
    *,
    push_denied: bool = False,
) -> Dict[str, Any]:
    """The verdict for one agent.

    `row` is its `agent_sync_state` row (or None). `cfg` carries the
    `agent_git_config` flags: `source_mode`, `auto_sync_enabled`,
    `freeze_schedules_if_sync_failing`, and optionally `created_at` (the
    no-heartbeat floor's start when no heartbeat was ever recorded).
    `push_denied` is `git_service.is_push_denied(last_error_summary)`, computed
    by the backend caller so this module stays a leaf.
    """
    now = parse_ts(now) or datetime.now(timezone.utc)
    cfg = cfg or {}
    auto_sync = _flag(cfg.get("auto_sync_enabled"))
    work = is_work_agent(cfg.get("source_mode"), auto_sync)
    freeze_flag = _flag(cfg.get("freeze_schedules_if_sync_failing"))

    verdict: Dict[str, Any] = {
        "state": "unknown",
        "reason": "no sync observation yet",
        "recommendation": None,
        "binding": "agent" if work else "deployment",
        "work_agent": work,
        "divergence_age_s": None,
        "dirty_age_s": None,
        "sync_failing": False,
        "freeze": False,
        "freeze_cause": None,
        "freeze_reason": None,
        "stale_observation": False,
    }
    if not row:
        return verdict
    observed_age = _age(now, parse_ts(row.get("last_check_at")))
    if observed_age is None:
        return verdict
    stale = observed_age > OBSERVATION_FRESH_SECONDS

    ahead = _count(row.get("ahead_working"))
    behind = _count(row.get("behind_working"))
    ahead_main = _count(row.get("ahead_main"))
    behind_main = _count(row.get("behind_main"))
    failures = _count(row.get("consecutive_failures"))
    dirty = _count(row.get("dirty_files"))
    error = row.get("last_error_summary")
    error = error.strip() if isinstance(error, str) else ""
    failed = row.get("last_sync_status") == "failed"
    sync_failing = failed and failures >= SYNC_FAILURE_FREEZE_THRESHOLD

    divergence_age = _age(now, parse_ts(row.get("diverged_since")))
    dirty_age = _age(now, parse_ts(row.get("dirty_since"))) if dirty else None

    reds: List[str] = []
    yellows: List[str] = []

    failed_clause = None
    if failed:
        plural = "" if failures == 1 else "s"
        failed_clause = f"last sync failed (seen on {failures} poll{plural})"
        if error:
            failed_clause += f": {error[:ERROR_EXCERPT_CHARS]}"
        reds.append(failed_clause)

    divergence_clause = None
    divergence_red = False
    if divergence_age is not None:
        divergence_clause = (
            f"diverged {behind} behind / {ahead} ahead for {format_age(divergence_age)}"
        )
        # D7: `main` moving under a working branch is information, not divergence.
        if behind_main and (ahead_main, behind_main) != (ahead, behind):
            divergence_clause += f" (main is {behind_main} ahead)"
        divergence_red = work and divergence_age > DIVERGENCE_RED_SECONDS
        (reds if divergence_red else yellows).append(divergence_clause)

    if dirty_age is not None and dirty_age > DIRTY_RED_SECONDS:
        clause = f"dirty: {dirty} files uncommitted for {format_age(dirty_age)}"
        (reds if work else yellows).append(clause)

    heartbeat_missing = False
    if auto_sync and not stale:
        heartbeat = parse_ts(row.get("last_sync_at")) or parse_ts(cfg.get("created_at"))
        heartbeat_age = _age(now, heartbeat)
        if heartbeat_age is not None and heartbeat_age > NO_HEARTBEAT_FLOOR_SECONDS:
            heartbeat_missing = True
            reds.append(f"auto-sync on, no heartbeat for {format_age(heartbeat_age)}")

    if reds:
        state, reason = "red", "; ".join(reds + yellows)
    elif yellows:
        state, reason = "yellow", "; ".join(yellows)
    else:
        state, reason = "green", "in sync"
    if stale:
        reason += f" (last observed {format_age(observed_age)} ago)"

    freeze_cause = freeze_reason = None
    if freeze_flag:
        if sync_failing:
            freeze_cause, freeze_reason = "sync_failing", failed_clause
        elif divergence_red and not stale:
            freeze_cause, freeze_reason = "divergence", divergence_clause

    if failed:
        if push_denied:
            recommendation = "credential is read-only"
        elif _SOURCE_MODE_REFUSAL in error:
            recommendation = "deployment: turn auto-sync off"
        else:
            recommendation = "push via git_sync strategy=pull_first"
    elif work and ahead and not auto_sync:
        recommendation = "enable auto-sync"
    elif behind:
        recommendation = "pull via git_pull"
    elif heartbeat_missing:
        recommendation = "restart the agent to resume the auto-sync heartbeat"
    else:
        recommendation = None

    verdict.update(
        state=state,
        reason=reason,
        recommendation=recommendation,
        divergence_age_s=divergence_age,
        dirty_age_s=dirty_age,
        sync_failing=sync_failing,
        freeze=freeze_cause is not None,
        freeze_cause=freeze_cause,
        freeze_reason=freeze_reason,
        stale_observation=stale,
    )
    return verdict
