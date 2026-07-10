"""
E-04 — Queued rows carry valid backlog metadata (CANARY-001 / Issue #411; #1077).

Every `schedule_executions` row in `status='queued'` must carry a non-NULL
`queued_at` AND a non-NULL, JSON-parseable `backlog_metadata`. Those two columns
are the drain-replay contract: `backlog_service.drain_next` reads
`backlog_metadata` and `json.loads`-decodes it to reconstruct the task's
identity/request before re-dispatching. A queued row with a NULL `queued_at`, a
NULL `backlog_metadata`, or a `backlog_metadata` that isn't valid JSON is a
**wedged backlog entry**: the FIFO drain will raise `JSONDecodeError` on it and
stall — the task sits queued forever behind a malformed neighbor.

## Predicate — three failure modes, one violation per row

Fire when, for a queued row: `queued_at IS NULL` **OR** `backlog_metadata IS
NULL` **OR** `json.loads(backlog_metadata)` raises. `json.loads` is exec-safe
(pure parse, no code execution); we catch `JSONDecodeError` / `TypeError`. One
violation per offending row, tagged with *which* predicate failed.

## Scoped strictly to queued rows (#1449-safe)

The snapshot collector populates `queued_meta` **only** from `status='queued'`
rows (`_collect_executions`). #1449 (deferred) will NULL `backlog_metadata` on
**terminal** rows once they leave the backlog — scoping E-04 to queued rows means
that future change can't make this check false-fire on a legitimately-drained
terminal row.

## SECURITY — never echo `backlog_metadata`

`backlog_metadata` is a drain-replay identity/request blob that may transitively
carry credentials (catalog **G-04** exists precisely to guard that). Violations
persist to `canary_violations`, so E-04's `observed_state` / `signal_query`
report **only** which predicate failed (`queued_at_null` /
`backlog_metadata_null` / `backlog_metadata_invalid_json`), the `execution_id`,
`agent_name`, and `snapshot_time` — **never** the raw value or its parsed
content. Mirrors the compatibility-collector redaction discipline (secret-bearing
content is never echoed into a persisted record).

## Older-image fail-open

An eid present in `queued_exec_ids` but absent from `queued_meta` means the
snapshot came from a DDL without the `queued_at`/`backlog_metadata` columns —
E-04 skips that eid rather than firing (consistent with the family's
fail-open-on-unavailable policy).

## Why a canary and not a writer unit test

E-04 catches **all** producers that enqueue a backlog row — including the
standalone scheduler's raw-SQL non-CAS writers (#1082 follow-up) a backend
writer test never exercises. That cross-path coverage is the load-bearing reason
this is an invariant, not a unit test.

Tier A, severity **major** — a wedged backlog entry stalls one agent's queue
(the task never drains) but does not corrupt live orchestration state.
"""

import json
from typing import List, Optional

from ..snapshot import Snapshot, ViolationReport


INVARIANT_ID = "E-04"
TIER = "A"
SEVERITY = "major"

# Predicate-failure reason codes. SECURITY: these enum-like strings are the ONLY
# thing reported about `backlog_metadata` — never the raw value or parsed content.
_REASON_QUEUED_AT_NULL = "queued_at_null"
_REASON_METADATA_NULL = "backlog_metadata_null"
_REASON_METADATA_INVALID_JSON = "backlog_metadata_invalid_json"


def _failed_predicate(meta: dict) -> Optional[str]:
    """Return the failing-predicate reason code, or None if the row is valid.

    NEVER returns the metadata value — only which check failed (see SECURITY).
    """
    if meta.get("queued_at") is None:
        return _REASON_QUEUED_AT_NULL
    raw = meta.get("backlog_metadata")
    if raw is None:
        return _REASON_METADATA_NULL
    try:
        json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return _REASON_METADATA_INVALID_JSON
    return None


def check(snapshot: Snapshot) -> List[ViolationReport]:
    """One violation per queued row with NULL/absent/unparseable backlog metadata."""
    violations: List[ViolationReport] = []

    for agent in snapshot.agents:
        for eid in agent.queued_exec_ids:
            meta = agent.queued_meta.get(eid)
            if meta is None:
                # Older-image snapshot didn't observe queued metadata → skip
                # (fail-open, consistent with the family policy).
                continue
            reason = _failed_predicate(meta)
            if reason is None:
                continue

            violations.append(
                ViolationReport(
                    invariant_id=INVARIANT_ID,
                    tier=TIER,
                    severity=SEVERITY,
                    observed_state={
                        "agent_name": agent.name,
                        "execution_id": eid,
                        # SECURITY: reason code only — never the metadata value.
                        "reason": reason,
                        "snapshot_time": snapshot.snapshot_time,
                    },
                    signal_query=(
                        f"schedule_executions queued row {eid} "
                        f"(agent={agent.name}) failed backlog-metadata "
                        f"integrity: {reason}"
                    ),
                )
            )

    return violations
