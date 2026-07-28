"""Single lease-reaper for pull-claimed tasks (#1081 Phase 3 — #429 / #1402).

Phase 3 of the pull/work-stealing migration. A pull worker that claims a task
(``GET /api/internal/next-task``) stamps ``schedule_executions`` with a
``claim_token`` / ``lease_expires_at`` / ``claimed_by_worker``. If that worker
dies or hangs, the row is left ``running`` with a lease in the past. This module
is the recovery path — the "collapse the 9-path cleanup pyramid into a single
lease-reaper" seam (#429), bounded by the DECIDED #1402 poison-task mechanism.

Decision logic (do NOT re-litigate — implemented as decided in #1402):

* Find every ``running`` row with a **non-NULL, past** ``lease_expires_at``
  (``db.find_expired_leases``). That ``IS NOT NULL`` filter is what keeps this
  disjoint from every non-pull row — push / #1083 fire-and-forget rows leave the
  lease columns NULL, so the reaper never double-acts with the existing
  stale-slot / ``LEASE_EXPIRED`` sweep (which keys off Redis slot TTL +
  ``claude_session_id='dispatched_async'``).
* **Under the cap** (``redelivery_count < MAX_REDELIVERY``): re-queue the SAME
  ``schedule_executions`` row (``db.requeue_expired_lease`` — status → ``queued``,
  ``redelivery_count`` ++ in one atomic UPDATE, lease columns cleared). The
  ``execution_id`` is preserved by construction — a HARD cross-cutting invariant
  (effect_guard #1084 + idempotency #525 are ``execution_id``-scoped; a new id
  would let a re-delivered turn re-send a message / re-charge a payment).
* **At the cap** (``redelivery_count >= MAX_REDELIVERY``): terminal —
  ``db.park_expired_lease`` FAILs the row (poison tag) and we create an
  operator-queue item so a human sees it (the REACTIVE park; the PROACTIVE
  fire-and-park from #1402 is out of scope here).

Every transition carries a status + lease CAS precondition in ``db/schedules.py``
(#1082 status-as-projection), so two reaper passes — or a late worker result —
can never double-act (double-increment / double-park).

Inert by default: ``PULL_MODE_PILOT_AGENTS`` is empty, so no row carries a lease
and ``find_expired_leases`` returns nothing until an agent is opted in.

Three-layer (Invariant #1): the SQL lives in ``db/schedules.py``; this service
holds the re-queue-vs-park policy; ``cleanup_service`` runs it as an additive
background sweep and closes the parked rows' open activities. ``db`` is passed in
(dependency injection) so the caller controls which handle is used and the sweep
stays unit-testable.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

import config
from utils.helpers import utc_now_iso

logger = logging.getLogger(__name__)

# Prefix on the poison-parked row's ``error`` text + the operator-queue item, so
# a lease-expired poison park is identifiable and greppable. Mirrors the
# ``_LEASE_EXPIRED_TAG`` literal convention in ``cleanup_service`` (kept a plain
# string to avoid importing ``task_execution_service`` into a leaf module).
_POISON_LEASE_TAG = "poison_lease"


@dataclass
class LeaseReapReport:
    """Outcome of one reaper pass."""
    requeued: int = 0
    parked: int = 0
    # execution_ids that were poison-parked this pass — the caller closes their
    # open dispatch activities (best-effort, async).
    parked_execution_ids: List[str] = field(default_factory=list)
    # #1804: execution_ids re-queued this pass. The re-queue is a CAS-won status
    # write (running → queued) that supersedes the dead worker's attempt, so its
    # dispatch activity must be closed too — otherwise the *previous* attempt's
    # row stays `started` while the re-delivery opens a second one, and the
    # Timeline shows the agent working twice. The caller closes them CANCELLED,
    # not FAILED: a superseded attempt is not a failure, and #1332 exists so
    # activity-derived views don't collapse the two.
    requeued_execution_ids: List[str] = field(default_factory=list)


def get_max_redelivery(agent_name: str) -> int:
    """The re-delivery cap for ``agent_name``.

    Returns the fleet-wide ``config.MAX_REDELIVERY`` (default 3). A per-agent
    override is the decided shape (#1402) but is a **deferred follow-up** — it
    would add a column/migration for a knob nothing consumes until a pilot agent
    is opted in, so the global default ships now and the per-agent override is
    flagged, not silently dropped. When added, this is the single seam to read
    it (e.g. ``agent_ownership.max_redelivery`` falling back to the global).
    """
    return config.MAX_REDELIVERY


def reap_expired_leases(
    db,
    *,
    now_iso: Optional[str] = None,
    max_redelivery: Optional[int] = None,
    limit: int = 500,
) -> LeaseReapReport:
    """Run one lease-reaper pass over ``db``'s expired pull leases.

    Args:
        db: the database facade (injected so the caller/tests control the handle).
        now_iso: the "now" cutoff (defaults to ``utc_now_iso()``); a single value
            is used for the whole pass so find + CAS agree.
        max_redelivery: cap override (tests). None ⇒ per-agent ``get_max_redelivery``.
        limit: max candidates per pass (bounds the write burst).

    Returns:
        A :class:`LeaseReapReport`.
    """
    now = now_iso or utc_now_iso()
    report = LeaseReapReport()

    for cand in db.find_expired_leases(now_iso=now, limit=limit):
        eid = cand["id"]
        agent_name = cand["agent_name"]
        count = int(cand.get("redelivery_count") or 0)
        cap = max_redelivery if max_redelivery is not None else get_max_redelivery(agent_name)

        if count >= cap:
            # At the cap → poison-park (terminal). #1081 B3: create the operator
            # alert FIRST, THEN park (FAIL) the row. Ordering is the fix: the park
            # write flips status→failed, which removes the row from
            # find_expired_leases (it requires status='running'). If the process
            # crashed BETWEEN a park and a later alert-insert, the poison would be
            # both INVISIBLE (no alert ever created) AND unrecoverable (the reaper
            # never re-finds a failed row) — a stuck task no human is told about.
            # Alert-first inverts this to a benign failure mode: a crash after the
            # alert leaves the row still running+expired, so the next pass
            # re-finds it, re-inserts the alert (idempotent via the item's
            # id-derived on_conflict_do_nothing), and parks. The alert is always
            # durable before the row goes terminal.
            #
            # Edge (rare, benign): if the park CAS then LOSES because a
            # genuinely-late worker SUCCESS won the row via its kept claim_token
            # (#1081 B2), the alert is left pending for a now-succeeded execution —
            # a VISIBLE false-positive an operator can dismiss, not silent loss.
            error = (
                f"{_POISON_LEASE_TAG}: pull lease expired and re-delivery cap "
                f"({cap}) reached — parked to operator queue"
            )
            if not _create_park_item(db, agent_name, eid, count, cap, now):
                # The alert could not be persisted (DB error). Do NOT park yet —
                # parking now would flip the row to failed with no operator alert
                # (invisible poison). Leave it running+expired so the next pass
                # retries the alert before the terminal write.
                continue
            if db.park_expired_lease(eid, error, now_iso=now):
                report.parked += 1
                report.parked_execution_ids.append(eid)
                logger.warning(
                    "[#1081 reaper] poison-parked execution %s (agent=%s) after "
                    "%d re-deliveries (cap=%d)",
                    eid, agent_name, count, cap,
                )
        else:
            # Under the cap → re-queue the SAME row (execution_id preserved).
            if db.requeue_expired_lease(eid, now_iso=now):
                report.requeued += 1
                report.requeued_execution_ids.append(eid)
                logger.info(
                    "[#1081 reaper] re-queued execution %s (agent=%s), "
                    "redelivery_count %d→%d (cap=%d)",
                    eid, agent_name, count, count + 1, cap,
                )

    return report


def _create_park_item(db, agent_name: str, execution_id: str, count: int, cap: int, now: str) -> bool:
    """Create an operator-queue park item for a poison-parked execution (OPS-001).

    Reuses the existing operator_queue create path. The item id is derived from
    the execution_id so ``create_item``'s ``on_conflict_do_nothing`` makes a
    re-insert (a re-found row, or a raced second pass) idempotent.

    Returns True when the alert is durable (inserted, or already present via
    on-conflict) and False when it could not be persisted. #1081 B3: the caller
    parks the row ONLY on True — a failed alert must NOT be followed by the
    terminal write, or the poison becomes invisible (a failed row is excluded
    from ``find_expired_leases`` and never re-found).
    """
    try:
        db.create_operator_queue_item(
            agent_name,
            {
                "id": f"poison-{execution_id}",
                "type": "alert",
                "status": "pending",
                "priority": "high",
                "title": "Task poison-parked (lease expired)",
                "question": (
                    f"Execution {execution_id} for agent '{agent_name}' exceeded the "
                    f"re-delivery cap ({cap}) after repeated worker lease expiries and "
                    f"was marked failed. Investigate the worker/task and re-trigger "
                    f"manually if appropriate."
                ),
                "context": {
                    "execution_id": execution_id,
                    "reason": _POISON_LEASE_TAG,
                    "redelivery_count": count,
                    "max_redelivery": cap,
                },
                "created_at": now,
            },
        )
        return True
    except Exception as e:  # pragma: no cover - best-effort
        logger.error(
            "[#1081 reaper] failed to create operator-queue park item for %s: %s",
            execution_id, e,
        )
        return False
