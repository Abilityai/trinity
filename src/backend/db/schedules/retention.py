"""Retention prunes + #1449 backlog-metadata scrub + #1644 blast-radius counts.

`find_soft_deleted_schedules_past_retention` lives here (not crud) so its
caller, `count_soft_deleted_schedules_past_retention`, and the shared
`_soft_deleted_schedules_predicate` module-global are all co-located — a
bare-name module-global does NOT resolve via MRO, so splitting them would
NameError on the #834 purge path."""


import json

from sqlalchemy import select, update, delete, and_, func, or_

from ..engine import get_engine
from ..tables import (
    agent_schedules,
    schedule_executions,
)
from models import TaskExecutionStatus
from utils.helpers import iso_cutoff

# #772 terminal set, shared by the retention prunes and their #1644 counts.
_RETENTION_TERMINAL = ("success", "failed", "cancelled", "skipped")


def _execution_log_prune_predicate(cutoff: str):
    """WHERE clause for the #772 `execution_log` null-out sweep.

    #1644: extracted so `prune_execution_logs` and `count_execution_log_candidates`
    are the SAME predicate by construction. A hand-mirrored copy in the guard would
    silently drift the first time either side is edited — and a guard that counts a
    different set than the prune deletes is worse than no guard, because it reports
    a blast radius that isn't the one about to happen.
    """
    return and_(
        schedule_executions.c.status.in_(_RETENTION_TERMINAL),
        schedule_executions.c.completed_at.isnot(None),
        schedule_executions.c.completed_at < cutoff,
        # #1741: either column may still hold transcript-derived content. Gating
        # on `execution_log IS NOT NULL` alone would permanently strand the
        # `tool_calls` copies on rows a PREVIOUS sweep already nulled the log
        # for — the exact rows where the copy most obviously outlived what it
        # duplicates.
        or_(
            schedule_executions.c.execution_log.isnot(None),
            schedule_executions.c.tool_calls.isnot(None),
        ),
    )


def _execution_row_prune_predicate(cutoff: str):
    """WHERE clause for the #772 terminal-row DELETE sweep (see #1644 note above).

    This is the accessor that destroyed 95.7% of a production instance's history
    in #1638 — the highest-value predicate in the file to keep in one place.
    """
    return and_(
        schedule_executions.c.status.in_(_RETENTION_TERMINAL),
        schedule_executions.c.completed_at.isnot(None),
        schedule_executions.c.completed_at < cutoff,
    )


def _soft_deleted_schedules_predicate(cutoff: str):
    """WHERE clause for the #834 Phase 1b soft-deleted schedule purge (#1644)."""
    return and_(
        agent_schedules.c.deleted_at.isnot(None),
        agent_schedules.c.deleted_at < cutoff,
    )


def _bounded_count(conn, table_col, predicate, limit: int) -> int:
    """COUNT the candidate set, stopping at `limit` (#1644).

    The guard only ever asks "is this more than N?", so an exact COUNT over a
    million-row backlog is wasted work on a loop that runs every 5 minutes. The
    LIMIT subquery makes this O(limit) instead of O(candidates) while still
    answering the only question asked. Callers pass `threshold + 1` so a return
    value of exactly `limit` means "at least limit" — which is all the > test needs.
    """
    inner = select(table_col).where(predicate).limit(limit).subquery()
    return int(conn.execute(select(func.count()).select_from(inner)).scalar() or 0)


# #1449: authoritative terminal states whose `backlog_metadata` (the drain-replay
# request blob — carries `user_message`/`user_email`/`system_prompt` PII) is safe
# to scrub. Mirrors ``routers.agents._AUTHORITATIVE_TERMINALS`` /
# ``services.pull_coordination_service._AUTHORITATIVE_TERMINALS`` (#1083). FAILED is
# deliberately EXCLUDED: a FAILED row is resurrectable to SUCCESS via a late
# token-gated CAS (``park_expired_lease`` keeps the ``claim_token``), so its intent
# blob must survive; FAILED PII stays bounded by the 90-day ``prune_execution_rows``.
_AUTHORITATIVE_TERMINALS = (
    TaskExecutionStatus.SUCCESS,
    TaskExecutionStatus.CANCELLED,
    TaskExecutionStatus.SKIPPED,
)

class ScheduleRetentionMixin:
    """Execution-log/row prunes, soft-delete purge counts, backlog scrub."""

    def find_soft_deleted_schedules_past_retention(
        self, retention_days: int, limit: int = 5000
    ) -> list:
        """List schedule ids whose `deleted_at` is older than `retention_days`.

        Used by the cleanup sweep to find rows ready for hard-purge.
        Bounded by `limit`.
        """
        from utils.helpers import iso_cutoff

        if retention_days <= 0 or limit <= 0:
            return []

        cutoff = iso_cutoff(hours=retention_days * 24)
        stmt = (
            select(agent_schedules.c.id)
            .where(_soft_deleted_schedules_predicate(cutoff))
            .limit(limit)
        )
        with get_engine().connect() as conn:
            return [row["id"] for row in conn.execute(stmt).mappings()]

    def count_execution_log_candidates(self, retention_days: int, limit: int) -> int:
        """#1644: how many rows `prune_execution_logs(retention_days)` would null.

        Bounded at `limit` (see `_bounded_count`). Shares the prune's predicate by
        construction, so the number the guard reports is the number that would go.
        """
        if retention_days <= 0 or limit <= 0:
            return 0
        cutoff = iso_cutoff(hours=retention_days * 24)
        with get_engine().connect() as conn:
            return _bounded_count(
                conn,
                schedule_executions.c.id,
                _execution_log_prune_predicate(cutoff),
                limit,
            )

    def count_execution_row_candidates(self, retention_days: int, limit: int) -> int:
        """#1644: how many rows `prune_execution_rows(retention_days)` would DELETE."""
        if retention_days <= 0 or limit <= 0:
            return 0
        cutoff = iso_cutoff(hours=retention_days * 24)
        with get_engine().connect() as conn:
            return _bounded_count(
                conn,
                schedule_executions.c.id,
                _execution_row_prune_predicate(cutoff),
                limit,
            )

    def count_soft_deleted_schedules_past_retention(
        self, retention_days: int, limit: int
    ) -> int:
        """#1644: how many schedules `_sweep_soft_deleted_schedules` would purge."""
        if retention_days <= 0 or limit <= 0:
            return 0
        cutoff = iso_cutoff(hours=retention_days * 24)
        with get_engine().connect() as conn:
            return _bounded_count(
                conn,
                agent_schedules.c.id,
                _soft_deleted_schedules_predicate(cutoff),
                limit,
            )

    def prune_execution_logs(self, retention_days: int, chunk_size: int = 500) -> int:
        """Null `execution_log` on terminal executions older than retention_days.

        Issue #772: the JSONL transcript dominates schedule_executions row size
        (~150–190 KB/row). Nulling preserves the row + metadata (cost, duration,
        agent, status) while reclaiming the bulk of the space. Runs in chunks
        to keep the write lock short — each chunk commits before the next, so
        a 3 GB backfill won't block a live system.

        NOTE (#1644): `chunk_size` bounds each *transaction*, NOT the call — the
        loop below drains the entire candidate set. One call deletes everything
        past the cutoff. See `_execution_log_prune_predicate` for why the
        predicate is shared with the #1644 blast-radius count.

        Args:
            retention_days: Null logs older than this. 0 disables the sweep.
            chunk_size: Max rows nulled per commit cycle (default 500).

        Returns:
            Total rows nulled across all chunks.
        """
        if retention_days <= 0 or chunk_size <= 0:
            return 0

        cutoff = iso_cutoff(hours=retention_days * 24)
        total = 0
        engine = get_engine()
        while True:
            # SELECT-then-UPDATE-by-id keeps the WHERE predicate aligned
            # with idx_executions_completed_terminal (#772) and avoids
            # depending on SQLITE_ENABLE_UPDATE_DELETE_LIMIT. Each chunk is
            # its own transaction so the write lock stays short.
            with engine.begin() as conn:
                ids = [
                    row["id"]
                    for row in conn.execute(
                        select(schedule_executions.c.id)
                        .where(_execution_log_prune_predicate(cutoff))
                        .limit(chunk_size)
                    ).mappings()
                ]
                if not ids:
                    break
                result = conn.execute(
                    update(schedule_executions)
                    .where(schedule_executions.c.id.in_(ids))
                    # #1741: `tool_calls` holds tool inputs derived from the same
                    # transcript, so it is pruned on the SAME schedule. Before
                    # this it held a verbatim copy of `execution_log` that this
                    # sweep never touched — an operator saw the log retired while
                    # an identical copy lived on until the row itself was deleted
                    # ~60 days later. A copy of the transcript must not outlive
                    # the transcript.
                    .values(execution_log=None, tool_calls=None)
                )
                total += result.rowcount
            if len(ids) < chunk_size:
                break
        return total

    def prune_execution_rows(self, retention_days: int, chunk_size: int = 500) -> int:
        """Delete terminal schedule_executions rows older than retention_days.

        Issue #772: deeper retention beyond `prune_execution_logs` — fully
        removes ancient rows. Chunked DELETE keeps the write lock short.

        Args:
            retention_days: Delete rows older than this. 0 disables the sweep.
            chunk_size: Max rows deleted per commit cycle (default 500).

        Returns:
            Total rows deleted across all chunks.
        """
        if retention_days <= 0 or chunk_size <= 0:
            return 0

        cutoff = iso_cutoff(hours=retention_days * 24)
        total = 0
        engine = get_engine()
        while True:
            with engine.begin() as conn:
                ids = [
                    row["id"]
                    for row in conn.execute(
                        select(schedule_executions.c.id)
                        .where(_execution_row_prune_predicate(cutoff))
                        .limit(chunk_size)
                    ).mappings()
                ]
                if not ids:
                    break
                result = conn.execute(
                    delete(schedule_executions).where(
                        schedule_executions.c.id.in_(ids)
                    )
                )
                total += result.rowcount
            if len(ids) < chunk_size:
                break
        return total

    def resummarize_legacy_tool_calls(self, chunk_size: int = 200, max_rows: int = 2000) -> int:
        """Rewrite legacy raw-transcript ``tool_calls`` blobs into the summary
        shape (#1741).

        Before #1741 the task path stored the whole transcript in ``tool_calls``.
        Every consumer looks for ``{"tool": …}`` entries, so those rows report
        **0 tool calls** and render an empty "Tool Calls" panel — history stays
        wrong even after the writer is fixed. This converts them in place.

        Non-destructive: the original transcript remains in ``execution_log``
        (pruned on its own schedule), so a bad conversion loses nothing
        recoverable. Idempotent: ``summarize_tool_calls_json`` returns an
        already-summarised value unchanged, and rows are re-selected only while
        they still look raw.

        Bounded per call (``max_rows``) because this is a whole-table rewrite on
        the largest table; the sweep runs every 5 minutes, so a big backlog
        drains over a few cycles instead of holding a long write lock.

        Returns: number of rows rewritten.
        """
        from services.tool_call_summary import looks_like_raw_transcript, summarize_tool_calls_json

        engine = get_engine()
        total = 0
        scanned = 0
        last_id = None
        while scanned < max_rows:
            with engine.begin() as conn:
                q = (
                    select(schedule_executions.c.id, schedule_executions.c.tool_calls)
                    .where(schedule_executions.c.tool_calls.isnot(None))
                    .order_by(schedule_executions.c.id)
                    .limit(chunk_size)
                )
                if last_id is not None:
                    q = q.where(schedule_executions.c.id > last_id)
                rows = conn.execute(q).mappings().all()
                if not rows:
                    break
                last_id = rows[-1]["id"]
                scanned += len(rows)

                for row in rows:
                    raw = row["tool_calls"]
                    try:
                        parsed = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if not looks_like_raw_transcript(parsed):
                        continue
                    summary = summarize_tool_calls_json(raw)
                    conn.execute(
                        update(schedule_executions)
                        .where(schedule_executions.c.id == row["id"])
                        .values(tool_calls=summary)
                    )
                    total += 1
            if len(rows) < chunk_size:
                break
        return total

    def scrub_terminal_backlog_metadata(self, chunk_size: int = 500) -> int:
        """NULL `backlog_metadata` on authoritative-terminal executions (#1449).

        ``backlog_service.enqueue`` json.dumps the full drain-replay request —
        including ``user_message``/``user_email``/``system_prompt`` — into
        ``backlog_metadata`` so a queued task can be reconstructed at drain.
        Nothing reads that blob once a row leaves ``status='queued'``: the drain
        claims only queued rows, the #1083/#1081 result callbacks read the POST
        payload (not the row's metadata), and canary E-04/G-04 are queued-scoped.
        On a terminal row it is therefore stale PII sitting in the DB indefinitely
        (bounded only by the 90-day ``prune_execution_rows``). Scrub it.

        Unlike the #772 sweeps this is **not age-gated** — it is a security
        invariant, not a retention window — but it mirrors their chunked
        SELECT-ids-then-UPDATE shape so the write lock stays short (each chunk is
        its own transaction). Only ``_AUTHORITATIVE_TERMINALS`` (success/cancelled/
        skipped) are scrubbed; a FAILED row's intent is kept because it is
        resurrectable to SUCCESS via a late CAS.

        Args:
            chunk_size: Max rows nulled per commit cycle (default 500).

        Returns:
            Total rows scrubbed across all chunks.
        """
        if chunk_size <= 0:
            return 0

        total = 0
        engine = get_engine()
        while True:
            with engine.begin() as conn:
                ids = [
                    row["id"]
                    for row in conn.execute(
                        select(schedule_executions.c.id)
                        .where(
                            and_(
                                schedule_executions.c.status.in_(
                                    _AUTHORITATIVE_TERMINALS
                                ),
                                schedule_executions.c.backlog_metadata.isnot(None),
                            )
                        )
                        .limit(chunk_size)
                    ).mappings()
                ]
                if not ids:
                    break
                result = conn.execute(
                    update(schedule_executions)
                    .where(schedule_executions.c.id.in_(ids))
                    .values(backlog_metadata=None)
                )
                total += result.rowcount
            if len(ids) < chunk_size:
                break
        return total
