"""
G-03 — Clock sanity on terminal rows (CANARY-001 / Issue #411; #1077).

A terminal `schedule_executions` row must not have finished before it started:
`started_at <= completed_at`. A row where `started_at > completed_at` means the
two timestamps were written by different calls against a skewed clock, or a
row's timestamps were mis-assigned (a producer copying the wrong execution's
`started_at`). Either way, every duration/rollup derived from the pair is
garbage.

## Reduced from the catalog form

The catalog phrases G-03 as `created_at <= started_at <= completed_at`, but
`schedule_executions` has **no `created_at` column** — the realizable check is
`started_at <= completed_at` on terminal rows where both are present. E-03 owns
the NULL-`completed_at` case, so G-03 skips any row missing either timestamp
(never double-fires with E-03).

## ~1s tolerance, not strict `>`

`started_at` and `completed_at` are written by different calls, possibly on
different uvicorn workers (`--workers 2`). A sub-second backward NTP step
between the two writes would make a strict `started > completed` fire minor
noise on a perfectly healthy row. G-03 fires only when `started - completed`
exceeds **1 second**, which is well below any real mis-assignment (those are
seconds-to-minutes apart) yet immune to clock jitter.

## UTC-aware parsing (survives #1474 mixed formats)

`started_at`/`completed_at` share one TEXT column that, mid-#1474, may hold a
legacy naive value beside a `Z`-suffixed one. The local `_to_utc` parser
(copied in shape from E-06's — NOT E-01's naive `_parse_iso`, which strips `Z`
and returns naive and would raise `TypeError` the instant it met an
offset-bearing operand) normalizes `Z`, an explicit offset, and a naive value
all to aware UTC before comparing — the learning from #1472/#41: convert to
UTC, never strip tzinfo. An unparseable timestamp skips the row rather than
firing.

## Why a canary and not a writer unit test

Like E-03, G-03 catches **all** producers that write a terminal row — including
the standalone scheduler's raw-SQL writers (#1082) a backend test never
exercises. That is the reason it is an invariant.

Tier A, severity **minor** — a start-after-finish pair is a data-quality defect
worth surfacing but has no runtime orchestration consequence, and (see the ~1s
tolerance) is more likely infrastructure clock drift than a Trinity bug.
"""

from datetime import datetime, timezone
from typing import List

from ..snapshot import Snapshot, ViolationReport


INVARIANT_ID = "G-03"
TIER = "A"
SEVERITY = "minor"

# Tolerance for cross-write / cross-worker clock skew (see module docstring).
CLOCK_SKEW_TOLERANCE_SECONDS = 1.0


def _to_utc(ts: str) -> datetime:
    """ISO-8601 → aware UTC datetime. Handles a trailing 'Z', an explicit
    offset, and a naive value — normalizing all three to aware UTC so a mixed
    naive/`Z` pair (#1474) compares without raising. Mirrors
    e06_no_overdue_next_run._to_utc (family convention; NOT E-01's naive parser,
    which raises on an offset operand)."""
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def check(snapshot: Snapshot) -> List[ViolationReport]:
    """One violation per terminal row where started_at > completed_at + 1s."""
    violations: List[ViolationReport] = []

    for row in snapshot.terminal_rows:
        started_at = row.get("started_at")
        completed_at = row.get("completed_at")
        # E-03 owns the NULL-completed_at case; G-03 needs both timestamps.
        if not started_at or not completed_at:
            continue
        try:
            started_dt = _to_utc(started_at)
            completed_dt = _to_utc(completed_at)
        except (ValueError, TypeError):
            # Unparseable timestamp — skip (don't false-fire on a format we
            # can't reason about; other checks own malformed-value bug classes).
            continue

        skew_seconds = (started_dt - completed_dt).total_seconds()
        if skew_seconds <= CLOCK_SKEW_TOLERANCE_SECONDS:
            continue

        eid = row.get("id")
        agent_name = row.get("agent_name")
        violations.append(
            ViolationReport(
                invariant_id=INVARIANT_ID,
                tier=TIER,
                severity=SEVERITY,
                observed_state={
                    "agent_name": agent_name,
                    "execution_id": eid,
                    "status": row.get("status"),
                    "started_at": started_at,
                    "completed_at": completed_at,
                    "skew_seconds": round(skew_seconds, 3),
                    "snapshot_time": snapshot.snapshot_time,
                },
                signal_query=(
                    f"schedule_executions row {eid} (agent={agent_name}) "
                    f"started_at={started_at} > completed_at={completed_at} "
                    f"by {round(skew_seconds, 3)}s "
                    f"(> {CLOCK_SKEW_TOLERANCE_SECONDS}s tolerance)"
                ),
            )
        )

    return violations
