"""
Operator queue database operations (OPS-001).

Persists operator queue items synced from agent JSON files.
Supports listing, filtering, responding, and statistics.

Converted from raw sqlite3 to SQLAlchemy Core for the configurable database
backend (#300): runs unchanged on both SQLite and PostgreSQL. Queries are built
from the ``operator_queue`` table in ``db/tables.py`` (dialect-agnostic
expressions, no ``?`` placeholders, no ``datetime('now')``/``julianday`` —
time math is done in Python). The public API of ``OperatorQueueOperations`` is
unchanged.
"""

import hashlib
import json
import logging
import uuid
from typing import Optional, List, Dict, Set, Tuple
from datetime import datetime, timezone

from sqlalchemy import select, update, func, and_, or_, case, delete
from sqlalchemy.exc import IntegrityError

from .engine import get_engine, make_insert
from .tables import operator_queue
from utils.helpers import utc_now_iso, iso_cutoff, parse_iso_timestamp, to_utc_iso

logger = logging.getLogger(__name__)

# trinity-enterprise#815: a cursor walk orders rows "as of" a watermark this many
# seconds before it began (`W`). Every pending → ended writer stamps `now` before
# it commits; a write whose stamp is at or before `W` but which commits after a
# page was read would move its row between sections and be returned twice. The
# margin absorbs any write that commits within it of its own stamp — the walk's
# one BOUND. SQLite's busy timeout (30 s) keeps it in practice; PostgreSQL has no
# statement or lock timeout configured, so a breach is logged at error
# (`_note_commit_lag`), never silent. ONE home: the watermark and the lag check
# read the same constant.
WALK_WATERMARK_MARGIN_S = 300


def _note_commit_lag(stamp: str) -> None:
    """After a pending → ended write commits: if it landed more than the walk
    margin after the timestamp it stamped, say so at error (ent#815). Called by
    every writer that ends an ask, once its transaction is committed."""
    lag = (datetime.now(timezone.utc) - parse_iso_timestamp(stamp)).total_seconds()
    if lag > WALK_WATERMARK_MARGIN_S:
        logger.error(
            "operator-queue end committed %ds after its timestamp; cursor walks "
            "open across it may repeat this row (ent#815)", int(lag),
        )


# #1632: generous hard "belt" caps enforced at the DB sink itself. The agent
# ingestion boundary (services/operator_queue_service.py) clamps to far smaller
# service caps *before* calling create_item, so a clamped agent item never trips
# these; platform items are small and never trip them either. They exist only as
# a second layer (#1525's validate-at-boundary-AND-at-sink philosophy) so the
# "platform bypasses the boundary" exemption stops being solely load-bearing —
# a caller that skips the service clamp still can't persist a multi-MB field.
# An order of magnitude above the service caps (title 300 / question 4000 /
# context 8 KiB / id 256).
_DB_BELT_TITLE_MAX_BYTES = 4 * 1024
_DB_BELT_QUESTION_MAX_BYTES = 16 * 1024
_DB_BELT_CONTEXT_MAX_BYTES = 64 * 1024
_DB_BELT_ID_MAX = 512

# trinity-enterprise#751: gate-raised rows (gated-skill approvals and the
# gate's notices) never count toward an AGENT's pending budget — neither the
# native ask cap nor the file poller's #1632 depth cap. The gate caps its own
# raises. NULL `raised_by` is a legacy or file row, so it counts.
_NOT_A_GATE_ROW = or_(operator_queue.c.raised_by.is_(None), operator_queue.c.raised_by != "gate")


def _own_pending_conds(agent_name: str, exclude_request_id_prefixes=None) -> list:
    """The pending rows that spend an agent's OWN ask budget — the one predicate
    both depth caps read (the file seam's #1632 cap and the native `queue_full`,
    trinity-enterprise#611), so the two cannot drift.

    #3130: the caller passes the platform's reserved id prefixes to leave out the
    rows the PLATFORM filed against the agent (its flood alarm above all — counted,
    it kept the cap tripped forever and was re-minted every cooldown window).
    Agents cannot mint those prefixes on either seam, so the exclusion cannot hide
    an agent's own row. Gate rows are left out as before (#751).
    """
    return [
        operator_queue.c.agent_name == agent_name,
        operator_queue.c.status == "pending",
        _NOT_A_GATE_ROW,
        *OperatorQueueOperations._not_prefixed(exclude_request_id_prefixes),
    ]

# trinity-enterprise#611: expiry is swept by a text comparison
# (`expires_at < now`), so a deadline written with an offset ("…+02:00") or
# without a zone compared hours off (Invariant #16). Bounded so the per-cycle
# sweep never holds an unbounded write set; the rest wait for the next cycle.
_EXPIRY_BATCH_MAX = 500


# #3247 T8: how many of the agent's pending proposals the duplicate guard reads.
_PENDING_PROPOSAL_SCAN = 200


def _canon_json(value) -> str:
    """Key-order- and whitespace-insensitive form of a stored proposal."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class _RollBack(Exception):
    """Unwind a native create's transaction and answer with `outcome` (#3247).

    Raised INSIDE `Engine.begin()` so the context manager rolls back (the CAS
    on the predecessor included) and caught OUTSIDE it; never escapes.
    """

    def __init__(self, outcome: Dict):
        super().__init__(outcome["outcome"])
        self.outcome = outcome


def _iso_z_deadline(value) -> Optional[str]:
    """A deadline as ISO-8601 UTC with a `Z` suffix; text that does not parse —
    or lands past the year range once moved to UTC — is kept as written, and a
    falsy value is None. Never raises: a raise here fails the create, and the
    poller quarantines the ask instead of showing it.

    Kept verbatim rather than dropped because the #2915 fingerprint
    (`operator_queue_service._normalise_expires`) compares an unparseable value
    as its own text — dropping it here would read as the agent rewriting the
    deadline on every cycle.
    """
    if not value:
        return None
    try:
        return to_utc_iso(parse_iso_timestamp(str(value)))
    except (TypeError, ValueError, OverflowError):
        return str(value)


def _operator_queue_prune_predicate(
    retention_days: int, responded_retention_days: int
):
    """WHERE clause for the #1142 terminal operator_queue retention sweep.

    #1644: extracted so `prune_terminal_items` and `count_terminal_candidates`
    share one definition. This predicate is the reason sharing is mandatory rather
    than merely tidy — it derives a second cutoff (`resp_days`) internally, so any
    hand-mirrored copy drifts the moment either window is edited.

    `pending` rows are never matched (and so never deleted) — see the caller.
    """
    terminal_cutoff = iso_cutoff(hours=retention_days * 24)
    # `responded` never uses a shorter window than the terminal one.
    resp_days = max(responded_retention_days, retention_days)
    responded_cutoff = iso_cutoff(hours=resp_days * 24)
    return or_(
        and_(
            operator_queue.c.status.in_(("acknowledged", "cancelled", "expired")),
            operator_queue.c.created_at < terminal_cutoff,
        ),
        and_(
            operator_queue.c.status == "responded",
            operator_queue.c.created_at < responded_cutoff,
        ),
    )


class OperatorQueueOperations:
    """Database operations for the operator queue."""

    @staticmethod
    def _row_to_item(row) -> Dict:
        """Convert a database row (RowMapping) to a queue item dict."""
        return {
            "id": row["id"],
            "agent_name": row["agent_name"],
            "request_id": row["request_id"],  # #1631 — agent-authored id
            "type": row["type"],
            "status": row["status"],
            "priority": row["priority"],
            "title": row["title"],
            "question": row["question"],
            "options": json.loads(row["options"]) if row["options"] else None,
            "context": json.loads(row["context"]) if row["context"] else None,
            "execution_id": row["execution_id"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "response": row["response"],
            "response_text": row["response_text"],
            "responded_by_id": row["responded_by_id"],
            "responded_by_email": row["responded_by_email"],
            "responded_at": row["responded_at"],
            "acknowledged_at": row["acknowledged_at"],
            "cleared_at": row["cleared_at"],  # #1017
            "addressed_to_email": row["addressed_to_email"],  # ent#364
            # #2915 — what the poller last established, and whether the answer landed
            "sync_state": row["sync_state"],
            "sync_detail": row["sync_detail"],
            "sync_updated_at": row["sync_updated_at"],
            "last_confirmed_at": row["last_confirmed_at"],
            "delivery_state": row["delivery_state"],
            "delivery_detail": row["delivery_detail"],
            "delivery_updated_at": row["delivery_updated_at"],
            # #2989 review — the operator answered a diverged item knowingly; the
            # write-back delivers into the entry as it is now
            "divergence_acknowledged_at": row["divergence_acknowledged_at"],
            # trinity-enterprise#611 — how the ask ended (NULL on a row that
            # ended before the ledger: read the ending from `status`)
            "disposition": row["disposition"],
            "disposed_at": row["disposed_at"],
            "disposed_by": row["disposed_by"],
            "disposed_by_email": row["disposed_by_email"],
            "disposition_reason": row["disposition_reason"],
            "batch_id": row["batch_id"],
            # trinity-enterprise#611 — the agent-raised ask (platform-owned)
            "raised_by": row["raised_by"],
            "channel": row["channel"],
            "to_role": row["to_role"],
            "resolved_to": json.loads(row["resolved_to"]) if row["resolved_to"] else None,
            "proposal": json.loads(row["proposal"]) if row["proposal"] else None,
            "supersedes_expired": row["supersedes_expired"],
            # #3246 — the platform alert's condition key and its latest reading
            "subject": row["subject"],
            "last_seen_at": row["last_seen_at"],
            # #3247 — the replace link, one uuid each way
            "replaces": row["replaces"],
            "replaced_by": row["replaced_by"],
        }

    # Columns selected for a full queue-item record, in the canonical order.
    _SELECT_COLS = (
        operator_queue.c.id,
        operator_queue.c.agent_name,
        operator_queue.c.request_id,  # #1631 — agent-authored id
        operator_queue.c.type,
        operator_queue.c.status,
        operator_queue.c.priority,
        operator_queue.c.title,
        operator_queue.c.question,
        operator_queue.c.options,
        operator_queue.c.context,
        operator_queue.c.execution_id,
        operator_queue.c.created_at,
        operator_queue.c.expires_at,
        operator_queue.c.response,
        operator_queue.c.response_text,
        operator_queue.c.responded_by_id,
        operator_queue.c.responded_by_email,
        operator_queue.c.responded_at,
        operator_queue.c.acknowledged_at,
        operator_queue.c.cleared_at,  # #1017 — Clear All hide flag
        operator_queue.c.addressed_to_email,  # ent#364 — the human it is for
        operator_queue.c.sync_state,  # #2915
        operator_queue.c.sync_detail,
        operator_queue.c.sync_updated_at,
        operator_queue.c.last_confirmed_at,
        operator_queue.c.delivery_state,
        operator_queue.c.delivery_detail,
        operator_queue.c.delivery_updated_at,
        operator_queue.c.divergence_acknowledged_at,
        operator_queue.c.disposition,  # trinity-enterprise#611 — the endings ledger
        operator_queue.c.disposed_at,
        operator_queue.c.disposed_by,
        operator_queue.c.disposed_by_email,
        operator_queue.c.disposition_reason,
        operator_queue.c.batch_id,
        operator_queue.c.raised_by,  # trinity-enterprise#611 — the agent-raised ask
        operator_queue.c.channel,
        operator_queue.c.to_role,
        operator_queue.c.resolved_to,
        operator_queue.c.proposal,
        operator_queue.c.supersedes_expired,
        operator_queue.c.subject,  # #3246 — platform alerts: kind:key
        operator_queue.c.last_seen_at,
        operator_queue.c.replaces,  # #3247 — the replace link
        operator_queue.c.replaced_by,
    )

    def create_item(
        self,
        agent_name: str,
        item: Dict,
        *,
        channel: Optional[str] = None,
        raised_by: Optional[str] = None,
    ) -> str:
        """Create a queue item from agent JSON data.

        Args:
            agent_name: The agent that created this item
            item: Queue item data from agent's operator-queue.json
            channel / raised_by: trinity-enterprise#611 — how the ask arrived
                (`file` | `mcp`) and who raised it (`agent` | `gate`). KEYWORD-ONLY
                and never read from `item`: the item is agent-authored, and a
                file entry that could carry `"channel": "mcp"` would forge its
                own provenance. A platform alarm passes neither (NULL).

        Returns:
            The item ID (the platform-minted uuid of the row that actually
            exists — on conflict that is the pre-existing row, NOT the uuid this
            call minted).
        """
        return self.create_item_with_outcome(
            agent_name, item, channel=channel, raised_by=raised_by,
        )[0]

    def create_item_with_outcome(
        self,
        agent_name: str,
        item: Dict,
        *,
        channel: Optional[str] = None,
        raised_by: Optional[str] = None,
    ) -> Tuple[str, bool]:
        """`create_item`, plus whether THIS call inserted the row
        (trinity-enterprise#611).

        The (agent_name, request_id) conflict makes a repeat a silent no-op
        that returns the surviving row's uuid — indistinguishable, to the
        caller, from a fresh create. The file poller used to count such a
        repeat as an admission (a phantom admit against the depth cap, and a
        "new" broadcast); with the flag it skips it. `inserted` is the INSERT's
        rowcount, so it is the database's answer, not a prior read's guess.
        """
        request_id, values = self._insert_values(
            agent_name, item, channel=channel, raised_by=raised_by,
        )
        stmt = make_insert(operator_queue).values(**values).on_conflict_do_nothing(
            index_elements=["agent_name", "request_id"]
        )
        # Insert + re-read in one transaction: on conflict the insert is a no-op
        # and the surviving row carries a DIFFERENT uuid, so to honour the
        # documented contract (return the id of the row that exists) the return
        # must be that row's id, not the `new_id` this call minted and discarded.
        with get_engine().begin() as conn:
            inserted = bool(conn.execute(stmt).rowcount)
            row = conn.execute(
                select(operator_queue.c.id).where(
                    and_(
                        operator_queue.c.agent_name == agent_name,
                        operator_queue.c.request_id == request_id,
                    )
                )
            ).first()
        return (row[0] if row else values["id"]), inserted

    def create_native_item(
        self,
        agent_name: str,
        item: Dict,
        *,
        max_pending: Optional[int],
        channel: str,
        raised_by: str,
        to_role: Optional[str],
        resolved_to: Optional[List[str]],
        proposal: Optional[Dict],
        supersedes_expired: Optional[str],
        exclude_request_id_prefixes=None,
        replaces: Optional[str] = None,
        guard_pending_proposal: bool = False,
    ) -> Dict:
        """Create an agent-raised ask, atomically per agent (trinity-enterprise#611).

        ONE serialized step: the replay check, the depth count and the insert
        run inside a per-agent lock (PostgreSQL `pg_advisory_xact_lock`; SQLite
        `BEGIN IMMEDIATE`, the db/audit.py precedent), so N concurrent calls at
        depth `max_pending - 1` admit exactly one. A count-then-insert across
        workers would make the cap a rate limit, not a bound.

        Returns `{"outcome": "created" | "replayed" | "queue_full", "row"}`:
        - `replayed` — `(agent_name, request_id)` exists; `row` is the FIRST
          row, untouched (a retry gets its first receipt, whatever it sends).
          Checked before the cap, so a retry is never refused a slot it holds.
          Also the answer when a file entry re-using the id lands between that
          check and the insert: the file poller does not take this lock, so on
          PostgreSQL it can, and the insert's `ON CONFLICT DO NOTHING` turns
          what would be a unique-index error (a 500 for the agent) into a
          replay of the row that won.
        - `queue_full` — `max_pending` of this agent's asks are pending; `row`
          is None and nothing is written. The count leaves gate rows out, and
          `max_pending=None` (a gate raise) skips the cap: the gate caps its own
          raises (trinity-enterprise#751).
        - `created` — `row` is the new row.

        The row never takes part in the file contract: `delivery_state` is
        `not_applicable` (`mcp_raised`) so no write-back set selects it, and
        `sync_state` stays NULL — there is no file entry to be out of sync with.
        Every platform column is a keyword-only argument, never read from
        `item` (the item is agent-authored).

        `replaces` (#3247) — the uuid of one of the agent's OWN pending asks
        that this ask replaces. Inside the same lock, after the replay check
        and before the count: a compare-and-set ends the predecessor
        (`cancelled` / `disposed_by='agent'` / `disposition_reason='replaced'`,
        `replaced_by` = the uuid this call minted) if it is still pending, not
        past its deadline, and carries `raised_by='agent'` under this agent —
        the belt beneath the sink's ownership gate. The successor is inserted
        with `replaces` = the predecessor's uuid. A replay never re-applies the
        replace. Extra outcomes:
        - `replaces_ended` — the predecessor already ended (a person's answer
          always wins); `row` is None, `predecessor` is the row as it stands,
          and NOTHING was written — except when it was still pending past its
          deadline: then it is expired here as the clock would (`expired` /
          `timeout`, `expired_now=True`) so the refusal names a state the row
          really carries and `supersedes_expired` works at once.
        - `replaces_not_own` — the row is not this agent's own pending
          agent-raised ask (missing, another agent's, NULL raiser, gate);
          nothing written, `predecessor` is the row if it is this agent's.
        Over the cap after the CAS, or a colliding insert after it (a file
        entry re-using the id landed between the replay check and the insert
        on PostgreSQL), the WHOLE transaction rolls back: a half-replace — the
        predecessor ended, no successor shown — must never commit. `created`
        then also carries `predecessor` (post-CAS row).

        `guard_pending_proposal` (#3247 T8) — refuse the ask when one of this
        agent's own pending agent-raised approvals already carries the same
        canonical `proposal`: outcome `already_pending` with that ask's
        `request_id`, nothing written. Checked INSIDE this lock, after the
        replace's compare-and-set (the ask it replaces is no longer pending on
        this connection, so it is not a duplicate of itself) and before the
        count, so two concurrent raises with one proposal have exactly one
        winner; a refusal rolls the CAS back with everything else. The caller
        decides when it applies (an agent's approval with a non-empty proposal).
        """
        request_id, values = self._insert_values(
            agent_name, item, channel=channel, raised_by=raised_by,
        )
        values.update(
            to_role=to_role,
            resolved_to=json.dumps(list(resolved_to)) if resolved_to else None,
            proposal=json.dumps(proposal) if proposal is not None else None,
            supersedes_expired=supersedes_expired,
            delivery_state="not_applicable",
            delivery_detail="mcp_raised",
        )
        mine = and_(
            operator_queue.c.agent_name == agent_name,
            operator_queue.c.request_id == request_id,
        )
        values["replaces"] = replaces
        predecessor = None
        early = None
        try:
            with get_engine().begin() as conn:
                self._lock_agent_for_create(conn, agent_name)
                existing = conn.execute(select(*self._SELECT_COLS).where(mine)).mappings().first()
                if existing:
                    return {"outcome": "replayed", "row": self._row_to_item(existing)}
                if replaces is not None:
                    predecessor, refusal = self._replace_predecessor(
                        conn, agent_name, replaces, values["id"])
                    if refusal is not None:
                        # commits: the only write on this path is the expire-now
                        early = {**refusal, "row": None, "predecessor": predecessor}
                if early is None:
                    if guard_pending_proposal and proposal is not None:
                        dup = self._pending_proposal_match(conn, agent_name, proposal)
                        if dup is not None:
                            raise _RollBack({"outcome": "already_pending", "row": None, "request_id": dup})
                    if max_pending is not None:
                        pending = conn.execute(
                            select(func.count()).where(and_(
                                *_own_pending_conds(agent_name, exclude_request_id_prefixes)))
                        ).scalar() or 0
                        if pending >= max_pending:
                            if replaces is not None:
                                raise _RollBack({"outcome": "queue_full", "row": None})
                            return {"outcome": "queue_full", "row": None}
                    inserted = bool(conn.execute(
                        make_insert(operator_queue).values(**values).on_conflict_do_nothing(
                            index_elements=["agent_name", "request_id"])
                    ).rowcount)
                    if not inserted and replaces is not None:
                        raise _RollBack({"outcome": "replayed", "row": None})
                    row = conn.execute(select(*self._SELECT_COLS).where(mine)).mappings().first()
        except _RollBack as rolled:
            # `Engine.begin()` rolled the CAS back with the exception (the PG
            # advisory xact lock released with it); the predecessor stands.
            out = rolled.outcome
            if out["outcome"] == "replayed":
                with get_engine().connect() as conn:
                    winner = conn.execute(select(*self._SELECT_COLS).where(mine)).mappings().first()
                out["row"] = self._row_to_item(winner) if winner else None
            return out
        if predecessor is not None and (early is None or early.get("expired_now")):
            # the replace's compare-and-set, or the expire-now beside it, ended
            # the predecessor and that write is committed (ent#815)
            _note_commit_lag(predecessor["disposed_at"])
        if early is not None:
            return early
        out = {"outcome": "created" if inserted else "replayed", "row": self._row_to_item(row)}
        if replaces is not None:
            out["predecessor"] = predecessor
        return out

    def _replace_predecessor(
        self, conn, agent_name: str, pred_id: str, new_id: str,
    ) -> Tuple[Optional[Dict], Optional[Dict]]:
        """End the predecessor of a replace by compare-and-set, on the create's
        own connection (#3247). Returns `(predecessor_row, refusal)`: a won CAS
        gives `(ended row, None)`; a lost one gives the row as it stands and the
        outcome dict the create returns instead of inserting.

        The predicate is respond's (`pending` AND not past the deadline) plus
        the ownership belt (`agent_name`, `raised_by='agent'`): one writer sees
        `rowcount 1` whatever races it — a person's answer, a cancel, the expiry
        sweep — and "denied by timeout" cannot be sidestepped by a replace.
        """
        now = utc_now_iso()
        mine = and_(operator_queue.c.id == pred_id, operator_queue.c.agent_name == agent_name)
        result = conn.execute(
            update(operator_queue)
            .where(and_(
                mine,
                operator_queue.c.raised_by == "agent",
                operator_queue.c.status == "pending",
                or_(operator_queue.c.expires_at.is_(None), operator_queue.c.expires_at > now),
            ))
            .values(
                status="cancelled",
                disposition="cancelled",
                disposed_at=now,
                disposed_by="agent",
                disposed_by_email=None,
                disposition_reason="replaced",
                replaced_by=new_id,
            )
        )
        if result.rowcount:
            row = conn.execute(select(*self._SELECT_COLS).where(mine)).mappings().first()
            return self._row_to_item(row), None
        row = conn.execute(select(*self._SELECT_COLS).where(mine)).mappings().first()
        if row is None:
            return None, {"outcome": "replaces_not_own"}
        if row["status"] == "pending":
            if row["expires_at"] is not None and row["expires_at"] <= now and row["raised_by"] == "agent":
                # T5b: still pending past its deadline — end it as the clock
                # would, in this transaction, so the refusal states a real
                # disposition instead of a dead zone until the sweep.
                expired = conn.execute(
                    update(operator_queue)
                    .where(and_(mine, operator_queue.c.status == "pending"))
                    .values(status="expired", disposition="expired",
                            disposed_at=now, disposed_by="timeout")
                ).rowcount
                row = conn.execute(select(*self._SELECT_COLS).where(mine)).mappings().first()
                return self._row_to_item(row), {"outcome": "replaces_ended", "expired_now": bool(expired)}
            # pending but not this agent's own agent-raised ask: the belt held
            return self._row_to_item(row), {"outcome": "replaces_not_own"}
        return self._row_to_item(row), {"outcome": "replaces_ended", "expired_now": False}

    @staticmethod
    def _pending_proposal_match(conn, agent_name: str, proposal: Dict) -> Optional[str]:
        """The `request_id` of this agent's own pending agent-raised APPROVAL that
        carries `proposal` (canonical JSON), newest first, or None (#3247 T8).
        Read on the create's connection, under its lock. An empty proposal is
        not a proposal: `{}` never matches and is never matched."""
        if not proposal:
            return None
        wanted = _canon_json(proposal)
        rows = conn.execute(
            select(operator_queue.c.request_id, operator_queue.c.proposal)
            .where(and_(
                operator_queue.c.agent_name == agent_name,
                operator_queue.c.status == "pending",
                operator_queue.c.raised_by == "agent",
                operator_queue.c.type == "approval",
                operator_queue.c.proposal.isnot(None),
            ))
            .order_by(operator_queue.c.created_at.desc())
            .limit(_PENDING_PROPOSAL_SCAN)
        ).all()
        for request_id, stored in rows:
            try:
                parsed = json.loads(stored)
            except (TypeError, ValueError):
                continue
            if parsed and _canon_json(parsed) == wanted:
                return request_id
        return None

    @staticmethod
    def _lock_agent_for_create(conn, agent_name: str) -> None:
        """Serialize native creates for ONE agent until COMMIT/ROLLBACK.

        Fails CLOSED (an unusable lock raises) — the db/audit.py rule: an
        unserialized count-then-insert silently turns the cap into a rate limit,
        which is the defect the lock exists to remove.
        """
        dialect = conn.engine.dialect.name
        if dialect == "postgresql":
            key = int.from_bytes(
                hashlib.sha256(f"opq-native:{agent_name}".encode("utf-8")).digest()[:8],
                "big", signed=True,
            )
            conn.exec_driver_sql("SELECT pg_advisory_xact_lock(%s)", (key,))
        elif dialect == "sqlite":
            # pysqlite has not sent BEGIN yet (it defers until DML), so this opens
            # the transaction with the RESERVED lock already held.
            conn.exec_driver_sql("BEGIN IMMEDIATE")

    def _insert_values(
        self,
        agent_name: str,
        item: Dict,
        *,
        channel: Optional[str],
        raised_by: Optional[str],
        subject: Optional[str] = None,
        last_seen_at: Optional[str] = None,
    ) -> Tuple[str, Dict]:
        """The DB-sink belts and the column values of a new row, shared by every
        create so the belts cannot drift between the file and native paths."""
        # #1525: `id` was the last hard-indexed field. The sync loop guards on a
        # truthy id before calling, but keep the DB boundary self-defensive so an
        # id-less item can never KeyError-hot-loop here (raise a clear ValueError
        # the caller quarantines, rather than an opaque KeyError).
        request_id = item.get("id")
        if not request_id:
            raise ValueError("operator-queue item is missing a required 'id'")

        # #1632: generous DB-sink belt. Reject a pathologically large field an
        # order of magnitude past the service caps (a clamped agent item or a
        # small platform item never trips this). Raised as ValueError so the sync
        # loop quarantines it (#1525) rather than hot-looping on an opaque error.
        if len(str(request_id)) > _DB_BELT_ID_MAX:
            raise ValueError(f"operator-queue item 'id' exceeds {_DB_BELT_ID_MAX} chars")
        title = item.get("title")
        if title and len(str(title).encode("utf-8")) > _DB_BELT_TITLE_MAX_BYTES:
            raise ValueError(f"operator-queue 'title' exceeds {_DB_BELT_TITLE_MAX_BYTES} bytes")
        question = item.get("question")
        if question and len(str(question).encode("utf-8")) > _DB_BELT_QUESTION_MAX_BYTES:
            raise ValueError(f"operator-queue 'question' exceeds {_DB_BELT_QUESTION_MAX_BYTES} bytes")

        options_json = json.dumps(item.get("options")) if item.get("options") else None
        context_json = json.dumps(item.get("context")) if item.get("context") else None
        if context_json and len(context_json.encode("utf-8")) > _DB_BELT_CONTEXT_MAX_BYTES:
            raise ValueError(f"operator-queue 'context' exceeds {_DB_BELT_CONTEXT_MAX_BYTES} bytes")

        # #1632: context may be authored as a non-dict (agents write free-form
        # JSON). Only pull execution_id when it is actually a dict — a str/list
        # `.get(...)` used to raise AttributeError here and hot-loop the sync.
        context = item.get("context")
        context_execution_id = context.get("execution_id") if isinstance(context, dict) else None
        # #1677 (fold): belt the derived COLUMN value like the id above — a
        # non-str or over-_DB_BELT_ID_MAX execution_id becomes None, never a
        # truncation (a truncated id matches nothing while feigning validity).
        # The context JSON blob keeps its own 64 KiB cap; only the column is
        # belted. `""` passes unchanged (existing callers send
        # `execution_id or ""`).
        if context_execution_id is not None and (
            not isinstance(context_execution_id, str)
            or len(context_execution_id) > _DB_BELT_ID_MAX
        ):
            context_execution_id = None

        # #1631: the agent's id served both the platform's global row handle AND
        # the agent's private correlation key — so two agents choosing the same
        # id collided on the PK and the second item was silently dropped. Split
        # them: `id` is a platform-minted uuid (globally unique by construction),
        # the agent's string lives in `request_id`, and uniqueness is scoped per
        # agent via the (agent_name, request_id) index. The conflict target moves
        # off `id` to that index, so a same-agent re-insert stays idempotent.
        new_id = uuid.uuid4().hex

        # Agents author operator-queue.json free-form, so the sync boundary must
        # be defensive (#1426): a required field missing from one item used to
        # raise KeyError here, and because the item stayed `pending` in the agent
        # file the 5s sync loop retried and error-logged it forever — the request
        # never reached the Operating Room. Default the hard-indexed fields
        # (mirrors how type/status/priority are already defaulted) so the item is
        # created once and the loop stops (the next cycle sees it via exists()).
        # `created_at` defaults to now (ingest time) per the issue's preferred fix.
        values = dict(
            id=new_id,
            agent_name=agent_name,
            request_id=request_id,
            type=item.get("type", "question"),
            status=item.get("status", "pending"),
            priority=item.get("priority", "medium"),
            title=item.get("title") or "Agent request",
            question=item.get("question") or item.get("title") or "(no details provided)",
            options=options_json,
            context=context_json,
            execution_id=context_execution_id,
            created_at=item.get("created_at") or utc_now_iso(),
            # trinity-enterprise#611: ISO-Z at the sink, whichever path wrote it —
            # expiry and the respond deadline compare it as text (Invariant #16).
            expires_at=_iso_z_deadline(item.get("expires_at")),
            channel=channel,
            raised_by=raised_by,
            # ent#364: already validated against the agent's roster by
            # `operator_queue_service._validated_addressee`. This layer stores it;
            # it does not decide it, and it must never derive it from `context`
            # (which is agent-authored).
            addressed_to_email=item.get("addressed_to_email"),
            # #3246: platform-owned, keyword-only — never read from `item`.
            subject=subject,
            last_seen_at=last_seen_at,
        )
        return request_id, values

    # ------------------------------------------------------------------
    # #3246 — platform alerts: one pending row per (agent, subject)
    # ------------------------------------------------------------------
    _RENDERED_FIELDS = ("title", "question", "priority")

    def find_pending_by_subject(self, agent_name: str, subject: str) -> Optional[Dict]:
        """The one pending row for `(agent, subject)`, or None (#3246)."""
        with get_engine().connect() as conn:
            row = self._find_pending_by_subject(conn, agent_name, subject)
        return self._row_to_item(row) if row else None

    def _find_pending_by_subject(self, conn, agent_name: str, subject: str):
        return conn.execute(
            select(*self._SELECT_COLS).where(and_(
                operator_queue.c.agent_name == agent_name,
                operator_queue.c.subject == subject,
                operator_queue.c.status == "pending",
            ))
        ).mappings().first()

    def find_person_ended_by_subject(
        self, agent_name: str, subject: str, since: str,
    ) -> Optional[Dict]:
        """The newest row for `(agent, subject)` a PERSON ended at or after
        `since` (ISO-Z text, Invariant #16), any terminal status — the seam's
        snooze read (#3246). Served by `idx_operator_queue_agent_subject`."""
        stmt = (
            select(*self._SELECT_COLS)
            .where(and_(
                operator_queue.c.agent_name == agent_name,
                operator_queue.c.subject == subject,
                operator_queue.c.status != "pending",
                operator_queue.c.disposed_by == "person",
                operator_queue.c.disposed_at.isnot(None),
                operator_queue.c.disposed_at >= since,
            ))
            .order_by(operator_queue.c.disposed_at.desc(), operator_queue.c.id.desc())
            .limit(1)
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return self._row_to_item(row) if row else None

    def create_platform_item(
        self,
        agent_name: str,
        item: Dict,
        *,
        subject: Optional[str],
        max_pending_for_type: Optional[int] = None,
    ) -> Dict:
        """Record a platform reading: find → touch → count → insert, in ONE
        locked transaction (#3246).

        `subject` is the condition key (`kind:key`); None is an event, which is
        always a new row. With a subject, the pending row for `(agent, subject)`
        is updated in place — title / question / priority / context /
        `last_seen_at` / `expires_at`, `context.seen_count` + 1 — by a
        compare-and-set on `status = 'pending'`: a row a person ended first is
        never overwritten (the lock covers creates, not `respond_to_item`), and
        a lost CAS falls through to a fresh row. The find runs BEFORE the
        #1677 per-type count, so a would-be update is never refused at budget;
        `max_pending_for_type` is the cap for a budgeted kind (None: unbudgeted).

        Returns `{"outcome": "created" | "updated" | "refused_at_budget",
        "row", "changed"}`; `changed` is whether a RENDERED field (title /
        question / priority / context minus `seen_count`) moved, so a bare
        repeat reading sends no broadcast. The partial unique index
        `uq_operator_queue_pending_subject` is the backstop for a lock that
        failed open: its `IntegrityError` is caught and the call re-finds and
        touches the row that won.
        """
        now = utc_now_iso()
        for attempt in (1, 2):
            try:
                with get_engine().begin() as conn:
                    self._lock_agent_for_create(conn, agent_name)
                    if subject is not None:
                        current = self._find_pending_by_subject(conn, agent_name, subject)
                        if current is not None:
                            touched = self._touch(conn, current, item, now=now)
                            if touched is not None:
                                return touched
                    if attempt == 2:
                        # The index said a pending row existed and it is gone
                        # (ended between the two transactions): fall through.
                        pass
                    if max_pending_for_type is not None:
                        pending = conn.execute(
                            select(func.count()).where(and_(
                                *_own_pending_conds(agent_name),
                                operator_queue.c.type == item.get("type", "question"),
                            ))
                        ).scalar() or 0
                        if pending >= max_pending_for_type:
                            return {"outcome": "refused_at_budget", "row": None, "changed": False}
                    context = item.get("context")
                    context = dict(context) if isinstance(context, dict) else {}
                    if subject is not None:
                        context["seen_count"] = 1
                    values_item = {**item, "context": context or None}
                    request_id, values = self._insert_values(
                        agent_name, values_item, channel=None, raised_by=None,
                        subject=subject, last_seen_at=now,
                    )
                    conn.execute(
                        make_insert(operator_queue).values(**values).on_conflict_do_nothing(
                            index_elements=["agent_name", "request_id"])
                    )
                    row = conn.execute(select(*self._SELECT_COLS).where(and_(
                        operator_queue.c.agent_name == agent_name,
                        operator_queue.c.request_id == request_id,
                    ))).mappings().first()
                    return {"outcome": "created", "row": self._row_to_item(row), "changed": True}
            except IntegrityError:
                if subject is None or attempt == 2:
                    raise
                # the partial unique index won the race the lock did not cover:
                # a pending row for the subject now exists — re-find and touch it.
                continue

    def _touch(self, conn, current, item: Dict, *, now: str) -> Optional[Dict]:
        """Update the pending row `current` in place; None when the CAS lost."""
        old_context = json.loads(current["context"]) if current["context"] else {}
        if not isinstance(old_context, dict):
            old_context = {}
        new_context = item.get("context")
        new_context = dict(new_context) if isinstance(new_context, dict) else {}
        seen = old_context.get("seen_count")
        new_context["seen_count"] = (seen if isinstance(seen, int) and seen > 0 else 1) + 1
        changed = any(
            (item.get(f) if item.get(f) is not None else current[f]) != current[f]
            for f in self._RENDERED_FIELDS
        ) or {k: v for k, v in old_context.items() if k != "seen_count"} !=             {k: v for k, v in new_context.items() if k != "seen_count"}
        context_json = json.dumps(new_context)
        if len(context_json.encode("utf-8")) > _DB_BELT_CONTEXT_MAX_BYTES:
            raise ValueError(f"operator-queue 'context' exceeds {_DB_BELT_CONTEXT_MAX_BYTES} bytes")
        result = conn.execute(
            update(operator_queue)
            .where(and_(operator_queue.c.id == current["id"], operator_queue.c.status == "pending"))
            .values(
                title=item.get("title") or current["title"],
                question=item.get("question") or current["question"],
                priority=item.get("priority") or current["priority"],
                context=context_json,
                last_seen_at=now,
                expires_at=_iso_z_deadline(item.get("expires_at")),
            )
        )
        if not result.rowcount:
            return None
        row = conn.execute(
            select(*self._SELECT_COLS).where(operator_queue.c.id == current["id"])
        ).mappings().first()
        return {"outcome": "updated", "row": self._row_to_item(row), "changed": bool(changed)}

    def end_items_by_platform(
        self, ids: List[str], *, reason: str, batch_id: Optional[str] = None,
    ) -> Dict:
        """End the listed pending rows as the PLATFORM (#3246): the
        `bulk_cancel_items` shape — one compare-and-set UPDATE on
        `status = 'pending'` stamping the ent#611 ledger with
        `disposed_by = 'platform'`, no email — re-selected by `batch_id`, so
        the rows returned are exactly the ones THIS call ended; a row a person
        ended first is skipped, never re-ended. Returns `{"batch_id", "rows"}`;
        `batch_id` is None when nothing was ended."""
        empty = {"batch_id": None, "rows": []}
        ids = list(dict.fromkeys(ids or ()))
        if not ids:
            return empty
        batch_id = batch_id or uuid.uuid4().hex
        now = utc_now_iso()
        with get_engine().begin() as conn:
            result = conn.execute(
                update(operator_queue)
                .where(and_(operator_queue.c.status == "pending", operator_queue.c.id.in_(ids)))
                .values(
                    status="cancelled",
                    disposition="cancelled",
                    disposed_at=now,
                    disposed_by="platform",
                    disposed_by_email=None,
                    disposition_reason=reason,
                    batch_id=batch_id,
                )
            )
            if result.rowcount == 0:
                return empty
            rows = conn.execute(
                select(*self._SELECT_COLS).where(and_(
                    operator_queue.c.id.in_(ids),
                    operator_queue.c.batch_id == batch_id,
                ))
            ).mappings().all()
        _note_commit_lag(now)
        return {"batch_id": batch_id, "rows": [self._row_to_item(r) for r in rows]}

    def get_item(self, item_id: str) -> Optional[Dict]:
        """Get a single queue item by ID."""
        stmt = select(*self._SELECT_COLS).where(operator_queue.c.id == item_id)
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()

        if not row:
            return None
        return self._row_to_item(row)

    def get_item_for_agent_by_request_id(self, agent_name: str, request_id: str) -> Optional[Dict]:
        """One agent's ask by the id the AGENT chose (trinity-enterprise#611).

        The agent's own readback. Scoped to `(agent_name, request_id)` — the
        uniqueness the #1631 index enforces — so one agent can never read
        another's ask by guessing its id. Deliberately NOT filtered on
        `cleared_at`: Clear All hides a row from the operator's list, and must
        not hide how the ask ended from the agent that raised it.
        """
        stmt = select(*self._SELECT_COLS).where(
            and_(
                operator_queue.c.agent_name == agent_name,
                operator_queue.c.request_id == request_id,
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return self._row_to_item(row) if row else None

    def list_pending_proposals_for_agent(
        self, agent_name: str, limit: int, raised_by: Optional[str] = None,
    ) -> List[Dict]:
        """`{id, request_id, proposal}` of this agent's PENDING asks that carry a
        proposal, newest first (#3247 T8). With `raised_by`, only that raiser's.

        Read by the native create's pending-proposal guard: an agent that
        repeats the exact action it is already asking about is refused
        `already_pending` unless the new ask replaces that one.
        """
        conditions = [
            operator_queue.c.agent_name == agent_name,
            operator_queue.c.status == "pending",
            operator_queue.c.proposal.isnot(None),
        ]
        if raised_by is not None:
            conditions.append(operator_queue.c.raised_by == raised_by)
        stmt = (
            select(operator_queue.c.id, operator_queue.c.request_id, operator_queue.c.proposal)
            .where(and_(*conditions))
            .order_by(operator_queue.c.created_at.desc())
            .limit(limit)
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).all()
        out = []
        for item_id, request_id, proposal in rows:
            try:
                parsed = json.loads(proposal)
            except (TypeError, ValueError):
                continue
            out.append({"id": item_id, "request_id": request_id, "proposal": parsed})
        return out

    def list_expired_proposals_for_agent(
        self, agent_name: str, limit: int, raised_by: Optional[str] = None,
    ) -> List[Dict]:
        """`{request_id, proposal}` of this agent's EXPIRED asks that carried a
        proposal, most recent ending first (trinity-enterprise#611). With
        `raised_by`, only that raiser's: the guard compares an ask with the
        same raiser's denials, never across raisers.

        Read by the native create's re-ask guard: an agent that repeats the exact
        action a timeout already denied must link the expired ask
        (`supersedes_expired`) so the person sees it is asking again. Bounded;
        the proposal is returned parsed so the caller compares values, not the
        stored JSON's key order.
        """
        conditions = [
            operator_queue.c.agent_name == agent_name,
            operator_queue.c.status == "expired",
            operator_queue.c.proposal.isnot(None),
        ]
        if raised_by is not None:
            conditions.append(operator_queue.c.raised_by == raised_by)
        stmt = (
            select(operator_queue.c.request_id, operator_queue.c.proposal)
            .where(and_(*conditions))
            .order_by(func.coalesce(operator_queue.c.disposed_at, operator_queue.c.created_at).desc())
            .limit(limit)
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).all()
        out = []
        for request_id, proposal in rows:
            try:
                out.append({"request_id": request_id, "proposal": json.loads(proposal)})
            except (TypeError, ValueError):
                continue
        return out

    def list_pending_asks_for_agent(
        self,
        agent_name: str,
        limit: int,
        exclude_request_id_prefixes=None,
    ) -> List[Dict]:
        """The agent's own pending asks, OLDEST first (#3247 — the Execution
        Context's `Pending asks` line).

        Exactly the rows that spend the agent's open-ask budget
        (`_own_pending_conds`), so the line and `queue_full` cannot disagree.
        Oldest first: the stale duplicates a scheduled run piles up are the
        oldest, and newest-first would hide them behind "and N more". The id,
        type, who raised it and when it was filed — never a title, a question,
        an answer or anything a person wrote.
        """
        stmt = (
            select(
                operator_queue.c.request_id,
                operator_queue.c.type,
                operator_queue.c.raised_by,
                operator_queue.c.created_at,
            )
            .where(and_(*_own_pending_conds(agent_name, exclude_request_id_prefixes)))
            .order_by(operator_queue.c.created_at.asc(), operator_queue.c.request_id.asc())
            .limit(limit)
        )
        with get_engine().connect() as conn:
            return [dict(r) for r in conn.execute(stmt).mappings().all()]

    def list_recent_endings_for_agent(
        self,
        agent_name: str,
        since: str,
        limit: int,
        exclude_request_id_prefixes=None,
    ) -> List[Dict]:
        """The asks this agent raised that ENDED at or after `since`, newest first
        (trinity-enterprise#611 — the Execution Context line).

        Ids and the ending only — `request_id`, `disposition`, `disposed_at`,
        `disposed_by` (#3247: `agent` reads `replaced`) — never a title, an answer or a reason: the line reaches every composed
        turn. Only rows that carry the ledger (a row that ended before it has no
        ending time to show); platform alarms excluded by prefix (the agent
        raised none of them, ent#499).
        """
        stmt = (
            select(
                operator_queue.c.request_id,
                operator_queue.c.disposition,
                operator_queue.c.disposed_at,
                operator_queue.c.disposed_by,
            )
            .where(
                and_(
                    operator_queue.c.agent_name == agent_name,
                    operator_queue.c.disposition.isnot(None),
                    operator_queue.c.disposed_at >= since,
                    *self._not_prefixed(exclude_request_id_prefixes),
                )
            )
            .order_by(operator_queue.c.disposed_at.desc())
            .limit(limit)
        )
        with get_engine().connect() as conn:
            return [dict(r) for r in conn.execute(stmt).mappings().all()]

    def _list_conditions(
        self,
        status: Optional[str] = None,
        type: Optional[str] = None,
        priority: Optional[str] = None,
        agent_name: Optional[str] = None,
        since: Optional[str] = None,
        accessible_agent_names: Optional[Set[str]] = None,
        include_cleared: bool = False,
        addressed_to_email: Optional[str] = None,
        hide_ended_before: Optional[str] = None,
        types: Optional[Tuple[str, ...]] = None,
        context_contains: Optional[Tuple[str, ...]] = None,
        exclude_request_id_prefixes: Optional[Tuple[str, ...]] = None,
    ) -> Optional[list]:
        """The WHERE conditions `list_items`, `count_items` and
        `list_item_agent_names` share (#3059) — ONE definition, so a page, its
        total and the agents it spans can never be computed over different
        filters. `None` means "matches nothing" (an empty access set); see
        `list_items` for what each filter means.
        """
        if accessible_agent_names is not None and len(accessible_agent_names) == 0:
            return None

        conds = []
        if not include_cleared:
            conds.append(operator_queue.c.cleared_at.is_(None))  # #1017

        if accessible_agent_names is not None:
            conds.append(operator_queue.c.agent_name.in_(sorted(accessible_agent_names)))
        if status:
            conds.append(operator_queue.c.status == status)
        if type:
            conds.append(operator_queue.c.type == type)
        if types is not None:
            conds.append(operator_queue.c.type.in_(list(types)))
        if priority:
            conds.append(operator_queue.c.priority == priority)
        if agent_name:
            conds.append(operator_queue.c.agent_name == agent_name)
        if addressed_to_email is not None:
            # `is not None`, deliberately NOT the truthiness the filters above
            # use. For this argument's callers it IS the authorization boundary
            # — "the asks addressed to this person" — so a falsy value has to
            # match NOTHING rather than silently widening to everyone's. The
            # other filters narrow a view the caller is already entitled to see;
            # this one decides entitlement, which is why it diverges.
            conds.append(
                func.lower(operator_queue.c.addressed_to_email)
                == addressed_to_email.strip().lower()
            )
        if since:
            conds.append(operator_queue.c.created_at >= since)
        if hide_ended_before:
            conds.append(or_(
                operator_queue.c.status == "pending",
                func.coalesce(
                    operator_queue.c.disposed_at,
                    operator_queue.c.responded_at,
                    operator_queue.c.created_at,
                ) >= hide_ended_before,
            ))
        for fragment in context_contains or ():
            # trinity-enterprise#610: a PREFILTER on the stored JSON text, for a
            # key the platform writes (the caller re-checks the parsed context —
            # a nested look-alike key matches this text too). LIKE-escaped, so a
            # `_` or `%` in the fragment matches only itself.
            escaped = fragment.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            conds.append(operator_queue.c.context.like(f"%{escaped}%", escape="\\"))
        conds.extend(self._request_id_not_prefixed_ci(exclude_request_id_prefixes))
        return conds

    def count_items(self, **filters) -> int:
        """How many rows `list_items(**filters)` would return with no limit (#3059)."""
        conds = self._list_conditions(**filters)
        if conds is None:
            return 0
        stmt = select(func.count()).select_from(operator_queue)
        if conds:
            stmt = stmt.where(and_(*conds))
        with get_engine().connect() as conn:
            return int(conn.execute(stmt).scalar() or 0)

    def list_item_agent_names(self, **filters) -> List[str]:
        """The distinct agents the rows matching `filters` belong to (#3059) —
        so a caller can apply a per-agent predicate (the Workspace roster
        re-check) BEFORE paging instead of after the limit."""
        conds = self._list_conditions(**filters)
        if conds is None:
            return []
        stmt = select(operator_queue.c.agent_name).distinct()
        if conds:
            stmt = stmt.where(and_(*conds))
        with get_engine().connect() as conn:
            return sorted(r[0] for r in conn.execute(stmt) if r[0])

    def list_items(
        self,
        status: Optional[str] = None,
        type: Optional[str] = None,
        priority: Optional[str] = None,
        agent_name: Optional[str] = None,
        since: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        accessible_agent_names: Optional[Set[str]] = None,
        include_cleared: bool = False,
        addressed_to_email: Optional[str] = None,
        hide_ended_before: Optional[str] = None,
        types: Optional[Tuple[str, ...]] = None,
        context_contains: Optional[Tuple[str, ...]] = None,
        exclude_request_id_prefixes: Optional[Tuple[str, ...]] = None,
    ) -> List[Dict]:
        """List queue items with optional filters.

        exclude_request_id_prefixes (trinity-enterprise#815): leave out rows
        whose `request_id` starts with one of these prefixes — the platform's
        heads-ups ABOUT a person, which a machine key never receives. A SQL
        condition, not a filter on the result, for the `addressed_to_email`
        reason below: dropped after the limit, they emptied a machine's page
        while its own rows sat just below the cut. Case-insensitive and
        leading-whitespace-tolerant, and a NULL `request_id` is kept — the
        rule `operator_queue_service.is_about_a_person` applies in Python.

        types (#3059): narrow to a set of item types in SQL, so a caller that
        shows only some kinds pages and counts over exactly what it shows.

        hide_ended_before (trinity-enterprise#611): an ISO-Z cutoff. Rows that
        ENDED before it are left out; pending rows are unaffected. A row's ending
        time is `disposed_at`, else (a row that ended before the ledger) its
        answer time, else its filing time — the only window signal such a row has.

        accessible_agent_names: if None, no access filter (admin). If a set,
        only items whose agent_name is in the set are returned. Empty set
        short-circuits to [] (user has no accessible agents).

        include_cleared: rows hidden by Clear All (#1017) are excluded by
        default. Only listing honors this — get_item and the sync-service
        accessors never filter on cleared_at.

        addressed_to_email: narrow to the asks addressed to ONE person
        (ent#364/ent#428). This has to be a SQL condition rather than a filter
        the caller applies to the result: the ordering is status, then priority,
        then age, and `limit` is applied before the caller ever sees a row — so
        a post-hoc filter reads "the newest N pending items in the FLEET, some
        of which happen to be yours", and one person's low-priority ask falls
        out of the window as soon as the fleet is busy. It disappears from their
        sidebar while still sitting pending in the queue, which is the one
        failure this surface cannot have.

        Compared case-insensitively. The ingestion boundary lowercases before it
        stores (`_validated_addressee`), so today every stored value is already
        lower — but `create_item` is a public writer and the read must not
        silently depend on every future caller remembering that.

        `None` means "do not filter"; any other value — including `""` — filters,
        and an empty one therefore matches nothing. See the comment at the
        condition for why this one argument does not use truthiness like the rest.
        """
        conds = self._list_conditions(
            status=status, type=type, priority=priority, agent_name=agent_name,
            since=since, accessible_agent_names=accessible_agent_names,
            include_cleared=include_cleared, addressed_to_email=addressed_to_email,
            hide_ended_before=hide_ended_before, types=types,
            context_contains=context_contains,
            exclude_request_id_prefixes=exclude_request_id_prefixes,
        )
        if conds is None:
            return []

        # Sort: pending items by priority then age; ended items by WHEN THEY
        # ENDED, newest first (trinity-enterprise#611, #627 AC6) — sorting a
        # cancellation made this morning by the day the ask was filed buried it.
        # A row that ended before the ledger falls back to its answer time, then
        # to its filing time (no other timestamp exists for it).
        is_pending = operator_queue.c.status == "pending"
        status_order = case((is_pending, 0), else_=1)
        priority_order = case(
            (operator_queue.c.priority == "critical", 0),
            (operator_queue.c.priority == "high", 1),
            (operator_queue.c.priority == "medium", 2),
            (operator_queue.c.priority == "low", 3),
            else_=4,
        )
        pending_priority = case((is_pending, priority_order), else_=0)
        sort_time = case(
            (is_pending, operator_queue.c.created_at),
            else_=func.coalesce(
                operator_queue.c.disposed_at,
                operator_queue.c.responded_at,
                operator_queue.c.created_at,
            ),
        )

        stmt = select(*self._SELECT_COLS)
        if conds:
            stmt = stmt.where(and_(*conds))
        stmt = (
            stmt.order_by(
                status_order,
                pending_priority,
                sort_time.desc(),
                # #3059: the last key, so rows that tie on everything above
                # (raised in the same instant) still order the same way on every
                # read — which is what lets an offset page without overlapping
                # or skipping a row.
                operator_queue.c.id,
            )
            .limit(limit)
            .offset(offset)
        )

        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()

        return [self._row_to_item(row) for row in rows]

    # ------------------------------------------------------------------
    # trinity-enterprise#815 — the keyset (cursor) walk
    # ------------------------------------------------------------------

    @staticmethod
    def _walk_sort_keys(watermark: str, snapshot: Optional[Dict[str, int]] = None):
        """The walk's sort key `(sec, prk, st)`, ordered `sec ASC, prk ASC,
        st DESC, id ASC` — today's order, taken "as of" `watermark`.

        A row is in the pending section (`sec = 0`) if it is pending NOW or
        ended after `watermark` (every pending → ended writer stamps
        `disposed_at` in the same UPDATE, and nothing returns a row to pending),
        so a row answered mid-walk keeps the key it had when the walk began.
        Nothing else moves a key, except a pending platform alert's priority
        (#3246 `_touch`): `snapshot` maps those rows' ids to the priority rank
        recorded when the walk began, and an alert not in it (raised during the
        walk) ranks 4 — a fixed value, so its key cannot move either.

        `coalesce(…, '')` keeps a NULL out of every comparison (a NULL key would
        fail them all and be skipped); `db/tables.py` declares these columns
        nullable even though the DDL does not.
        """
        oq = operator_queue.c
        as_pending = or_(oq.status == "pending", oq.disposed_at > watermark)
        sec = case((as_pending, 0), else_=1)
        rank = case(
            (oq.priority == "critical", 0),
            (oq.priority == "high", 1),
            (oq.priority == "medium", 2),
            (oq.priority == "low", 3),
            else_=4,
        )
        if snapshot is not None:
            by_rank: Dict[int, List[str]] = {}
            for item_id, r in snapshot.items():
                by_rank.setdefault(int(r), []).append(item_id)
            whens = [(oq.id.in_(sorted(ids)), r) for r, ids in sorted(by_rank.items())]
            whens.append((oq.subject.isnot(None), 4))
            rank = case(*whens, else_=rank)
        prk = case((as_pending, rank), else_=0)
        st = case(
            (as_pending, func.coalesce(oq.created_at, "")),
            else_=func.coalesce(oq.disposed_at, oq.responded_at, oq.created_at, ""),
        )
        return sec, prk, st

    def _walk_query(
        self,
        *,
        watermark: str,
        snapshot: Optional[Dict[str, int]],
        after: Optional[Tuple[int, int, str, str]],
        limit: int,
        **filters,
    ):
        """`(statement, (sec, prk, st))` for one walk page — None when the
        filters match nothing. The after-cursor predicate, the ORDER BY and the
        read-back key columns use the SAME expression objects, so the three
        compare text under one collation and cannot disagree on either dialect.
        The predicate is an OR-expansion (portable; handles the mixed
        directions)."""
        conds = self._list_conditions(**filters)
        if conds is None:
            return None, None
        sec, prk, st = self._walk_sort_keys(watermark, snapshot)
        where = list(conds)
        if after is not None:
            s, p, t, i = after
            where.append(or_(
                sec > s,
                and_(sec == s, prk > p),
                and_(sec == s, prk == p, st < t),
                and_(sec == s, prk == p, st == t, operator_queue.c.id > i),
            ))
        stmt = select(*self._SELECT_COLS, sec.label("_k_sec"), prk.label("_k_prk"),
                      st.label("_k_st"))
        if where:
            stmt = stmt.where(and_(*where))
        stmt = stmt.order_by(sec, prk, st.desc(), operator_queue.c.id).limit(limit)
        return stmt, (sec, prk, st)

    def list_items_walk(
        self,
        *,
        watermark: str,
        snapshot: Optional[Dict[str, int]] = None,
        after: Optional[Tuple[int, int, str, str]] = None,
        limit: int = 100,
        **filters,
    ) -> List[Tuple[Dict, Tuple[int, int, str, str]]]:
        """One page of a cursor walk (trinity-enterprise#815): `(item, key)`
        pairs in walk order, after `after`. The key is read back from SQL —
        Python never re-derives it — so a cursor built from the last row is
        exactly where the next page starts. Same `_list_conditions` as
        `list_items`/`count_items`, so the page and `total` share one WHERE.
        `list_items` (and its callers' order) is untouched."""
        stmt, _ = self._walk_query(watermark=watermark, snapshot=snapshot,
                                   after=after, limit=limit, **filters)
        if stmt is None:
            return []
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [
            (self._row_to_item(r), (int(r["_k_sec"]), int(r["_k_prk"]), r["_k_st"], r["id"]))
            for r in rows
        ]

    def walk_alert_priorities(self, *, watermark: str, **filters) -> Dict[str, int]:
        """`{id: priority rank}` of the platform alerts (`subject` set, #3246)
        the walk orders as pending — the snapshot a walk takes when it begins
        (trinity-enterprise#815), over the same filters as its pages."""
        conds = self._list_conditions(**filters)
        if conds is None:
            return {}
        _, prk, _ = self._walk_sort_keys(watermark)
        oq = operator_queue.c
        as_pending = or_(oq.status == "pending", oq.disposed_at > watermark)
        stmt = select(oq.id, prk).where(and_(*conds, oq.subject.isnot(None), as_pending))
        with get_engine().connect() as conn:
            return {r[0]: int(r[1]) for r in conn.execute(stmt).all()}

    def respond_to_item(
        self,
        item_id: str,
        response: str,
        response_text: Optional[str],
        responded_by_id: Optional[str],
        responded_by_email: str,
        divergence_acknowledged: bool = False,
        terminal: bool = False,
    ) -> Optional[Dict]:
        """Record a response to a queue item.

        `terminal` (#2372): the row has no agent audience (a platform-minted
        alert — the caller decides, this layer knows no prefixes), so the
        answer is its last event: the same compare-and-set writes
        `status='acknowledged'` + `acknowledged_at` instead of `responded`. No
        agent will ever acknowledge it, and `responded` is kept from Clear All
        and retained for the 90-day floor. `acknowledged_at` therefore means
        "the agent acknowledged" on an agent's ask and "an operator
        acknowledged" on a platform alert.

        `divergence_acknowledged` (#2989 review): the operator saw that the agent
        had rewritten or closed the entry and answered anyway; the write-back then
        delivers into the entry as it is now instead of refusing it.

        Returns the updated item or None if not found.

        trinity-enterprise#611: the same UPDATE writes the endings ledger
        (`disposition='answered'`, `disposed_by='person'`), and the compare-and-set
        also requires the deadline not to have passed. An answer that arrives
        after `expires_at` but before the poller sweeps the row returns the row
        with `_status_conflict` while its status still reads `pending` — the
        caller names that `expired`; an approval must never land after the
        deadline the rider calls "denied by timeout".

        `responded_by_id` is Optional on purpose (ent#364/ent#428): it is a
        `users` id, and an ask answered by a Workspace client has no row there.
        Writing one would be a lie in the audit trail, so a client answer is
        recorded as NULL id + the answering email — and THAT pair is what
        distinguishes "answered by a client" from "answered by an operator whose
        account was since deleted", which keeps its id. The annotation says so
        because the alternative is someone later "tidying" it back to `str` and
        quietly making the two indistinguishable.
        """
        now = utc_now_iso()

        with get_engine().begin() as conn:
            result = conn.execute(
                update(operator_queue)
                .where(
                    and_(
                        operator_queue.c.id == item_id,
                        operator_queue.c.status == "pending",
                        or_(
                            operator_queue.c.expires_at.is_(None),
                            operator_queue.c.expires_at > now,
                        ),
                    )
                )
                .values(
                    status="acknowledged" if terminal else "responded",
                    acknowledged_at=now if terminal else None,
                    response=response,
                    response_text=response_text,
                    responded_by_id=responded_by_id,
                    responded_by_email=responded_by_email,
                    responded_at=now,
                    divergence_acknowledged_at=now if divergence_acknowledged else None,
                    disposition="answered",
                    disposed_at=now,
                    disposed_by="person",
                    disposed_by_email=responded_by_email,
                )
            )

            if result.rowcount == 0:
                # Check if item exists at all
                row = conn.execute(
                    select(operator_queue.c.id, operator_queue.c.status).where(
                        operator_queue.c.id == item_id
                    )
                ).mappings().first()
                if not row:
                    return None
                # Item exists but not pending — lost a race (e.g. bulk-cancel
                # landed between the router's status check and this UPDATE) — or
                # still pending past its deadline (#611). Mark the conflict so
                # the caller can 409 instead of returning a 200 for a response
                # that was never recorded (#1017).
                item = self.get_item(item_id)
                item["_status_conflict"] = True
                return item

        _note_commit_lag(now)
        return self.get_item(item_id)

    def cancel_item(
        self,
        item_id: str,
        *,
        disposed_by_email: str,
        reason: Optional[str] = None,
        disposition: str = "cancelled",
    ) -> Optional[Dict]:
        """Cancel a pending queue item, recording who ended it (trinity-enterprise#611).

        `disposition` (trinity-enterprise#748) is `cancelled` (an operator ended
        it) or `dismissed` (the person it was addressed to chose not to answer).
        Both leave `status='cancelled'`, so every terminal-status reader is
        unchanged; only the ledger says which of the two it was.

        The status flip and the endings ledger are ONE compare-and-set, the mirror
        of `respond_to_item`: a caller that loses the race (the ask was answered,
        cancelled or expired first) gets the row back with `_status_conflict` and
        writes nothing, so the ending it would have recorded never overwrites the
        one that happened. Returns None when the item does not exist.
        """
        if disposition not in ("cancelled", "dismissed"):
            raise ValueError(f"not a cancel disposition: {disposition!r}")
        now = utc_now_iso()
        with get_engine().begin() as conn:
            result = conn.execute(
                update(operator_queue)
                .where(
                    and_(
                        operator_queue.c.id == item_id,
                        operator_queue.c.status == "pending",
                    )
                )
                .values(
                    status="cancelled",
                    disposition=disposition,
                    disposed_at=now,
                    disposed_by="person",
                    disposed_by_email=disposed_by_email,
                    disposition_reason=reason,
                )
            )

            if result.rowcount == 0:
                exists = conn.execute(
                    select(operator_queue.c.id).where(operator_queue.c.id == item_id)
                ).first()
                if not exists:
                    return None
                item = self.get_item(item_id)
                item["_status_conflict"] = True
                return item

        _note_commit_lag(now)
        return self.get_item(item_id)

    def set_discussion_link(self, item_id: str, key: str, chat_id: str) -> Optional[Dict]:
        """Link a PENDING ask to the chat a person opened to discuss it
        (trinity-enterprise#747), at most once.

        Writes `context[key] = chat_id` with a compare-and-set on the stored
        context text as it was read, plus `status='pending'`, so two Discuss
        clicks racing link ONE chat: the loser writes nothing. Returns the row as
        it stands after the attempt (its `context[key]` is the chat that won, or
        absent when the ask was no longer pending), or None when it does not
        exist. The key is platform-only (stripped from agent content at
        ingestion), so nothing an agent wrote can pre-empt it.
        """
        with get_engine().begin() as conn:
            row = conn.execute(
                select(operator_queue.c.status, operator_queue.c.context)
                .where(operator_queue.c.id == item_id)
            ).mappings().first()
            if not row:
                return None
            raw = row["context"]
            context = json.loads(raw) if raw else {}
            if not isinstance(context, dict):
                context = {}
            if row["status"] == "pending" and not context.get(key):
                context[key] = chat_id
                same_text = (operator_queue.c.context.is_(None) if raw is None
                             else operator_queue.c.context == raw)
                conn.execute(
                    update(operator_queue)
                    .where(and_(operator_queue.c.id == item_id,
                                operator_queue.c.status == "pending",
                                same_text))
                    .values(context=json.dumps(context))
                )
        return self.get_item(item_id)

    def bulk_cancel_items(
        self,
        ids: List[str],
        accessible_agent_names: Optional[Set[str]] = None,
        *,
        disposed_by_email: str,
        reason: Optional[str] = None,
    ) -> Dict:
        """Cancel the listed items that are still pending (#1017).

        Only items in `ids` are touched — the caller sends the ids it actually
        showed the operator, so a sync-loop race can never cancel items the
        operator never saw. Non-pending and inaccessible ids are skipped.

        accessible_agent_names: None = no filter (admin); empty set = no-op
        (a zero-agent user must not be able to touch anything); non-empty =
        SQL-side IN filter.

        trinity-enterprise#611: the sweep mints ONE `batch_id` and the same
        compare-and-set UPDATE stamps it with the endings ledger, so re-selecting
        `id IN (:ids) AND batch_id = :b` returns exactly the rows THIS sweep
        flipped — never a row another writer ended first (dialect-agnostic, no
        RETURNING). Returns `{"batch_id", "rows"}`; `batch_id` is None when the
        sweep ended nothing.
        """
        empty = {"batch_id": None, "rows": []}
        if not ids:
            return empty
        if accessible_agent_names is not None and len(accessible_agent_names) == 0:
            return empty

        ids = list(ids)
        conds = [
            operator_queue.c.status == "pending",
            operator_queue.c.id.in_(ids),
        ]
        if accessible_agent_names is not None:
            conds.append(operator_queue.c.agent_name.in_(sorted(accessible_agent_names)))

        batch_id = uuid.uuid4().hex
        now = utc_now_iso()
        with get_engine().begin() as conn:
            result = conn.execute(
                update(operator_queue).where(and_(*conds)).values(
                    status="cancelled",
                    disposition="cancelled",
                    disposed_at=now,
                    disposed_by="person",
                    disposed_by_email=disposed_by_email,
                    disposition_reason=reason,
                    batch_id=batch_id,
                )
            )
            if result.rowcount == 0:
                return empty
            rows = conn.execute(
                select(*self._SELECT_COLS).where(
                    and_(
                        operator_queue.c.id.in_(ids),
                        operator_queue.c.batch_id == batch_id,
                    )
                )
            ).mappings().all()
        _note_commit_lag(now)
        return {"batch_id": batch_id, "rows": [self._row_to_item(r) for r in rows]}

    def clear_resolved_items(
        self,
        agent_name: Optional[str] = None,
        accessible_agent_names: Optional[Set[str]] = None,
    ) -> int:
        """Hide terminal queue items — Clear All on the Resolved tab (#1017).

        Sets cleared_at on acknowledged/cancelled/expired rows; list_items
        excludes them by default. 'responded' rows are intentionally kept
        visible: the sync service still has to deliver the operator's answer
        to the agent file. A hide flag — NOT a DELETE — because the 5s sync
        loop re-creates any DB-missing item whose agent-file entry still says
        'pending' (always true for expired items, and for cancelled items
        whose flip hasn't been written back yet); deleting those rows would
        resurrect them. Actual row deletion is the retention sweep's job
        (#1142).

        Same tri-state accessible_agent_names contract as bulk_cancel_items.
        Returns the number of rows hidden.
        """
        if accessible_agent_names is not None and len(accessible_agent_names) == 0:
            return 0

        now = utc_now_iso()
        conds = [
            or_(
                operator_queue.c.status.in_(("acknowledged", "cancelled", "expired")),
                # trinity-enterprise#611: a native ask is never acknowledged
                # through a file, so `responded` is its last state and there is
                # no write-back to wait for.
                and_(operator_queue.c.status == "responded", ~self._file_contract()),
            ),
            operator_queue.c.cleared_at.is_(None),
        ]
        if accessible_agent_names is not None:
            conds.append(operator_queue.c.agent_name.in_(sorted(accessible_agent_names)))
        if agent_name:
            conds.append(operator_queue.c.agent_name == agent_name)

        with get_engine().begin() as conn:
            result = conn.execute(
                update(operator_queue).where(and_(*conds)).values(cleared_at=now)
            )
            return result.rowcount

    def prune_terminal_items(
        self,
        retention_days: int,
        responded_retention_days: int,
        limit: int = 5000,
    ) -> int:
        """#1142: hard-DELETE old terminal operator-queue rows (retention sweep).

        The counterpart to #1017's ``clear_resolved_items`` (which only *hides*
        via ``cleared_at`` because the 5s sync loop would resurrect a deleted row
        still ``pending`` in the agent file). By retention age those rows are long
        settled, so they can be removed:

        - ``acknowledged`` / ``cancelled`` / ``expired`` older than ``retention_days``;
        - ``responded`` only older than the more generous ``responded_retention_days``
          — the write-back loop still has to deliver the operator's answer to the
          agent file, and a stopped agent picks it up on restart, so a young
          ``responded`` row must survive. ``pending`` rows are never deleted.

        Age is measured on ``created_at`` (always set). Capped at ``limit`` rows
        per call (select-ids-then-delete, portable across SQLite/PostgreSQL). A
        disabled window (``retention_days <= 0``) prunes nothing. Returns the
        count deleted.
        """
        if retention_days <= 0 or limit <= 0:
            return 0

        id_stmt = (
            select(operator_queue.c.id)
            .where(
                _operator_queue_prune_predicate(retention_days, responded_retention_days)
            )
            .limit(limit)
        )
        with get_engine().begin() as conn:
            ids = [r[0] for r in conn.execute(id_stmt).all()]
            if not ids:
                return 0
            result = conn.execute(
                delete(operator_queue).where(operator_queue.c.id.in_(ids))
            )
            return result.rowcount

    def count_terminal_candidates(
        self,
        retention_days: int,
        responded_retention_days: int,
        limit: int,
    ) -> int:
        """#1644: how many rows `prune_terminal_items` would DELETE.

        Shares the prune's predicate — which matters more here than anywhere else,
        because that predicate derives a *second* cutoff internally
        (``resp_days = max(responded_retention_days, retention_days)``). A
        hand-mirrored count would drift the first time either knob is edited.
        """
        if retention_days <= 0 or limit <= 0:
            return 0
        inner = (
            select(operator_queue.c.id)
            .where(
                _operator_queue_prune_predicate(retention_days, responded_retention_days)
            )
            .limit(limit)
            .subquery()
        )
        with get_engine().connect() as conn:
            return int(
                conn.execute(select(func.count()).select_from(inner)).scalar() or 0
            )

    def mark_acknowledged(self, agent_name: str, request_id: str) -> Optional[str]:
        """Mark an item as acknowledged by the agent.

        #1631: scoped to (agent_name, request_id) — the agent's file carries its
        own `request_id`, not the platform uuid `id`. Matching on `request_id`
        alone would let agent B's acknowledgement flip agent A's identically-id'd
        row (a real cross-agent write bug), so the agent_name must be part of the
        predicate.

        Returns the acknowledged row's platform uuid `id` (or None if no
        `responded` row matched) — the WS `operator_queue_acknowledged` event and
        the frontend store both key items by that uuid, so the caller must
        broadcast it, not the agent's `request_id`.
        """
        now = utc_now_iso()
        with get_engine().begin() as conn:
            result = conn.execute(
                update(operator_queue)
                .where(
                    and_(
                        operator_queue.c.agent_name == agent_name,
                        operator_queue.c.request_id == request_id,
                        operator_queue.c.status == "responded",
                        # #2989 review: an agent-side `acknowledged` is an ack of OUR
                        # answer only when that answer reached the entry. Without
                        # this, an entry the agent closed itself flipped the row and
                        # the platform claimed an ack of an answer never seen.
                        operator_queue.c.delivery_state == "delivered",
                    )
                )
                .values(status="acknowledged", acknowledged_at=now)
            )
            if result.rowcount == 0:
                return None
            row = conn.execute(
                select(operator_queue.c.id).where(
                    and_(
                        operator_queue.c.agent_name == agent_name,
                        operator_queue.c.request_id == request_id,
                    )
                )
            ).first()
            return row[0] if row else None

    def mark_expired(self) -> List[Dict]:
        """Expire pending items past their deadline; return the rows THIS call ended.

        trinity-enterprise#611: a bounded candidate select, then one
        compare-and-set UPDATE per id (`status = 'pending'` still) that also
        writes the ledger with `disposed_by = 'timeout'`. The rowcount of each
        UPDATE is the identity of the winner, so two overlapping sweeps (a Redis
        flap can briefly give two leaders) end — and later wake — each row once.
        Edge-triggered: a second pass over the same rows ends nothing.
        """
        now = utc_now_iso()
        candidates = (
            select(operator_queue.c.id)
            .where(
                and_(
                    operator_queue.c.status == "pending",
                    operator_queue.c.expires_at.isnot(None),
                    operator_queue.c.expires_at < now,
                )
            )
            .order_by(operator_queue.c.expires_at.asc())
            .limit(_EXPIRY_BATCH_MAX)
        )
        won = []
        with get_engine().begin() as conn:
            for (item_id,) in conn.execute(candidates).all():
                result = conn.execute(
                    update(operator_queue)
                    .where(
                        and_(
                            operator_queue.c.id == item_id,
                            operator_queue.c.status == "pending",
                            # #3246: a platform alert refreshed between the
                            # candidate select and this CAS moved its deadline;
                            # it is not expired, and the refresh wins.
                            operator_queue.c.expires_at.isnot(None),
                            operator_queue.c.expires_at < now,
                        )
                    )
                    .values(
                        status="expired",
                        disposition="expired",
                        disposed_at=now,
                        disposed_by="timeout",
                    )
                )
                if result.rowcount:
                    won.append(item_id)
            if not won:
                return []
            rows = conn.execute(
                select(*self._SELECT_COLS).where(operator_queue.c.id.in_(won))
            ).mappings().all()
        _note_commit_lag(now)
        return [self._row_to_item(r) for r in rows]

    def get_stats(self, accessible_agent_names: Optional[Set[str]] = None) -> Dict:
        """Get queue statistics.

        accessible_agent_names: if None, no access filter (admin). If a set,
        only items for accessible agents are counted. Empty set returns zeros.
        """
        if accessible_agent_names is not None and len(accessible_agent_names) == 0:
            return {
                "by_status": {},
                "by_type": {},
                "by_priority": {},
                "by_agent": {},
                "pending_count": 0,
                "avg_response_seconds": None,
                "responded_today": 0,
            }

        # Access filter applied to every aggregate query.
        access_cond = None
        if accessible_agent_names is not None:
            access_cond = operator_queue.c.agent_name.in_(sorted(accessible_agent_names))

        def _with_access(*conds):
            all_conds = list(conds)
            if access_cond is not None:
                all_conds.append(access_cond)
            return all_conds

        with get_engine().connect() as conn:
            # Counts by status
            status_stmt = select(
                operator_queue.c.status, func.count()
            ).group_by(operator_queue.c.status)
            access_only = _with_access()
            if access_only:
                status_stmt = status_stmt.where(and_(*access_only))
            by_status = {row[0]: row[1] for row in conn.execute(status_stmt).all()}

            # Counts by type (pending only)
            type_stmt = (
                select(operator_queue.c.type, func.count())
                .where(and_(*_with_access(operator_queue.c.status == "pending")))
                .group_by(operator_queue.c.type)
            )
            by_type = {row[0]: row[1] for row in conn.execute(type_stmt).all()}

            # Counts by priority (pending only)
            priority_stmt = (
                select(operator_queue.c.priority, func.count())
                .where(and_(*_with_access(operator_queue.c.status == "pending")))
                .group_by(operator_queue.c.priority)
            )
            by_priority = {row[0]: row[1] for row in conn.execute(priority_stmt).all()}

            # Counts by agent (pending only)
            agent_stmt = (
                select(operator_queue.c.agent_name, func.count())
                .where(and_(*_with_access(operator_queue.c.status == "pending")))
                .group_by(operator_queue.c.agent_name)
            )
            by_agent = {row[0]: row[1] for row in conn.execute(agent_stmt).all()}

            # Average response time (for responded items). Computed in Python
            # from the ISO-Z timestamp strings — julianday() is SQLite-only.
            resp_conds = [operator_queue.c.responded_at.isnot(None)]
            avg_stmt = select(
                operator_queue.c.created_at, operator_queue.c.responded_at
            ).where(and_(*_with_access(*resp_conds)))
            deltas = []
            for created_at, responded_at in conn.execute(avg_stmt).all():
                if not created_at or not responded_at:
                    continue
                try:
                    c = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    r = datetime.fromisoformat(responded_at.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    continue
                deltas.append((r - c).total_seconds())
            avg_response_seconds = round(sum(deltas) / len(deltas), 1) if deltas else None

            # Items responded today
            today = datetime.utcnow().strftime("%Y-%m-%d")
            today_conds = [
                operator_queue.c.responded_at.isnot(None),
                operator_queue.c.responded_at >= today,
            ]
            today_stmt = select(func.count()).where(and_(*_with_access(*today_conds)))
            responded_today = conn.execute(today_stmt).scalar() or 0

        return {
            "by_status": by_status,
            "by_type": by_type,
            "by_priority": by_priority,
            "by_agent": by_agent,
            "pending_count": by_status.get("pending", 0),
            "avg_response_seconds": avg_response_seconds,
            "responded_today": responded_today,
        }

    def get_responded_items_for_agent(self, agent_name: str) -> List[Dict]:
        """Get responded (not yet acknowledged) items for a specific agent.

        Used by sync service to write responses back to agent files.
        """
        stmt = select(*self._SELECT_COLS).where(
            and_(
                operator_queue.c.agent_name == agent_name,
                operator_queue.c.status == "responded",
                self._file_contract(),  # trinity-enterprise#611: nothing to write for a native ask
            )
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_item(row) for row in rows]

    def get_terminal_items_for_agent(self, agent_name: str, limit: int = 200) -> List[Dict]:
        """Cancelled/expired items whose terminal flip has not been delivered (#1017, #2915).

        Used by the sync service to flip still-'pending' entries in the agent's
        queue file to their terminal status so the agent stops waiting. Bounded by
        DELIVERY STATE, not by a `created_at` window: before #2915 a row that went
        terminal more than 168 h after it was created was never fetched, so it
        was neither flipped nor recorded as undeliverable — the card said
        "cancelled", the agent's file said "pending", and nothing said so. A row
        leaves this set when the flip lands (`delivered`) or is platform-minted
        (`not_applicable`); `undelivered` rows are retried each cycle (the read is
        already paid for) and bounded by the retention sweep. Deliberately NOT
        filtered on cleared_at — hidden items still need their flip delivered.
        """
        stmt = (
            select(*self._SELECT_COLS)
            .where(
                and_(
                    operator_queue.c.agent_name == agent_name,
                    operator_queue.c.status.in_(("cancelled", "expired")),
                    self._file_contract(),  # trinity-enterprise#611
                    or_(
                        operator_queue.c.delivery_state.is_(None),
                        operator_queue.c.delivery_state == "undelivered",
                    ),
                    # #2989 review: a flip whose entry the agent already dropped can
                    # never land — it stays recorded, but leaves the retry set so the
                    # cap cannot starve rows that still can. Oldest first, same reason.
                    func.coalesce(operator_queue.c.delivery_detail, "") != "entry_missing",
                )
            )
            .order_by(operator_queue.c.created_at.asc())
            .limit(limit)
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_item(row) for row in rows]

    # ------------------------------------------------------------------
    # #2915 — sync honesty accessors. Written ONLY by the leader-locked poller.
    # Every writer is edge-triggered: the WHERE excludes rows already carrying
    # the value, and the returned rowcount IS the transition — never read-then-
    # write, so two overlapping leaders cannot double-record one change.
    # ------------------------------------------------------------------

    def get_sync_index_for_agent(self, agent_name: str) -> Dict:
        """Everything the poller needs to reconcile one agent's file in two reads.

        `open` — full rows for pending + responded items (the ones the file is
        expected to carry); `terminal` — `{request_id: {"id", "status",
        "sync_state", "delivery_state", "delivery_detail"}}` for every other
        status, so a pending file entry whose id matches a row that already went
        terminal is recognised as `stale_id` instead of being re-admitted (the
        on-conflict create returns the surviving uuid silently, so without this
        index it would count against the depth cap and broadcast "new" every
        cycle). The delivery columns tell that re-use apart from the ORIGINAL
        entry this cycle's write-back is about to flip (#3024).
        """
        open_stmt = select(*self._SELECT_COLS).where(
            and_(
                operator_queue.c.agent_name == agent_name,
                operator_queue.c.status.in_(("pending", "responded")),
                self._file_contract(),
            )
        )
        term_stmt = select(
            operator_queue.c.request_id,
            operator_queue.c.id,
            operator_queue.c.status,
            operator_queue.c.sync_state,
            operator_queue.c.delivery_state,  # #3024
            operator_queue.c.delivery_detail,
        ).where(
            and_(
                operator_queue.c.agent_name == agent_name,
                operator_queue.c.status.notin_(("pending", "responded")),
                self._file_contract(),
            )
        )
        # trinity-enterprise#611: the request_ids of this agent's NATIVE asks. A
        # file entry reusing one is skipped before the create branch — filtering
        # native rows out of the two sets above without naming them here would
        # read such an entry as brand new every cycle (the phantom admit).
        foreign_stmt = select(operator_queue.c.request_id).where(
            and_(
                operator_queue.c.agent_name == agent_name,
                ~self._file_contract(),
            )
        )
        with get_engine().connect() as conn:
            open_rows = conn.execute(open_stmt).mappings().all()
            term_rows = conn.execute(term_stmt).mappings().all()
            foreign = [r[0] for r in conn.execute(foreign_stmt).all()]
        return {
            "open": [self._row_to_item(r) for r in open_rows],
            "terminal": {
                r["request_id"]: {
                    "id": r["id"], "status": r["status"], "sync_state": r["sync_state"],
                    "delivery_state": r["delivery_state"], "delivery_detail": r["delivery_detail"],
                }
                for r in term_rows
            },
            "foreign": foreign,
        }

    def set_sync_state(
        self, item_id: str, state: str, detail: Optional[str], now: str,
    ) -> bool:
        """Record what the poller established; True iff the row CHANGED.

        `confirmed` also stamps `last_confirmed_at`. The predicate is spelled
        `IS NULL OR !=` because `IS DISTINCT FROM` is PostgreSQL-only.
        """
        detail_v = detail or ""
        values = {"sync_state": state, "sync_detail": detail, "sync_updated_at": now}
        if state == "confirmed":
            values["last_confirmed_at"] = now
        with get_engine().begin() as conn:
            result = conn.execute(
                update(operator_queue)
                .where(
                    and_(
                        operator_queue.c.id == item_id,
                        or_(
                            operator_queue.c.sync_state.is_(None),
                            operator_queue.c.sync_state != state,
                            func.coalesce(operator_queue.c.sync_detail, "") != detail_v,
                        ),
                    )
                )
                .values(**values)
            )
            return result.rowcount > 0

    def refresh_last_confirmed(self, agent_name: str, now: str, older_than: str) -> int:
        """One batched UPDATE per agent per cycle: `last_confirmed_at = now` for
        confirmed open rows whose stamp is older than `older_than` (a minute
        cadence — the card can say "last confirmed at HH:MM" without a write per
        row per 5 s cycle)."""
        with get_engine().begin() as conn:
            result = conn.execute(
                update(operator_queue)
                .where(
                    and_(
                        operator_queue.c.agent_name == agent_name,
                        operator_queue.c.status.in_(("pending", "responded")),
                        operator_queue.c.sync_state == "confirmed",
                        or_(
                            operator_queue.c.last_confirmed_at.is_(None),
                            operator_queue.c.last_confirmed_at < older_than,
                        ),
                    )
                )
                .values(last_confirmed_at=now)
            )
            return result.rowcount

    def set_delivery_state(
        self, item_id: str, state: str, detail: Optional[str], now: str,
    ) -> bool:
        """Record whether the answer (or terminal flip) reached the agent's file;
        True iff the row CHANGED (same edge rule as `set_sync_state`)."""
        detail_v = detail or ""
        with get_engine().begin() as conn:
            result = conn.execute(
                update(operator_queue)
                .where(
                    and_(
                        operator_queue.c.id == item_id,
                        or_(
                            operator_queue.c.delivery_state.is_(None),
                            operator_queue.c.delivery_state != state,
                            func.coalesce(operator_queue.c.delivery_detail, "") != detail_v,
                        ),
                    )
                )
                .values(delivery_state=state, delivery_detail=detail, delivery_updated_at=now)
            )
            return result.rowcount > 0

    @staticmethod
    def _file_contract():
        """SQL: the row takes part in the agent's `operator-queue.json` contract
        (trinity-enterprise#611). NULL covers every row created before the
        channel column; a native (`mcp`) ask has no file entry to reconcile,
        flag or write back into."""
        return or_(operator_queue.c.channel.is_(None), operator_queue.c.channel == "file")

    # Exactly the characters `str.strip()` removes (`str.isspace()`: ASCII
    # whitespace, the ASCII separators U+001C-U+001F, NEL, NBSP and the Unicode
    # spaces), so the SQL trims what `is_about_a_person` trims (ent#815 fix 1).
    # `ltrim(text, chars)` is multi-byte aware on SQLite and PostgreSQL alike.
    # Ingest refuses such ids (`_ID_RE`); only a legacy row can carry one.
    _PY_WHITESPACE = (
        "\t\n\x0b\x0c\r\x1c\x1d\x1e\x1f \x85\xa0\u1680"
        "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
        "\u2028\u2029\u202f\u205f\u3000"
    )

    @staticmethod
    def _request_id_not_prefixed_ci(prefixes):
        """`request_id` does not start with any of `prefixes`, compared the way
        `operator_queue_service.is_about_a_person` compares in Python
        (trinity-enterprise#815), so the two rules exclude exactly the same rows
        and `total` is exact: case-insensitive, leading whitespace ignored, and
        a NULL `request_id` kept (`coalesce`: a bare `substr(NULL) != p` is
        NULL, which would DROP the row).

        Equal to Python's `.strip().lower().startswith(p)` for these all-ASCII
        prefixes on SQLite and on a UTF-8 PostgreSQL database with a
        non-Turkic collation (the supported deployment). Under a Turkic
        collation (`tr`/`az`) `lower('I')` is `ı`, so a legacy upper-case
        `PORTAL-INBOX-…` row passes this exclusion; the Python belt then drops
        it and the response carries `total: null` with a warning — never
        wider, never narrower. `ltrim` takes Python's whitespace set;
        U+212A KELVIN SIGN is the one non-ASCII code point whose Python
        `.lower()` is ASCII (`k`), and SQLite's `lower` is ASCII-only, so it is
        rewritten first; U+0130 lowers to `i` + U+0307 in Python but to a bare
        `i` under PostgreSQL's glibc `lower`, so it is rewritten to Python's
        form first. `lower`, `replace`, `ltrim(text, chars)`, `coalesce` and
        `substr` exist on SQLite and PostgreSQL alike — no dialect branch.

        Deliberately not `_not_prefixed` below: that one is the sweeps' and the
        own-budget predicate's (case-sensitive, NULL-dropping), and is left
        exactly as it is."""
        normalised = func.lower(func.replace(func.replace(
            func.ltrim(
                func.coalesce(operator_queue.c.request_id, ""),
                OperatorQueueOperations._PY_WHITESPACE,
            ),
            "\u212a", "k"), "\u0130", "i\u0307"))
        return [
            func.substr(normalised, 1, len(p)) != p.lower()
            for p in (prefixes or ())
        ]

    @staticmethod
    def _not_prefixed(prefixes):
        """`request_id` does not start with any reserved platform prefix — the SQL
        twin of `is_platform_minted` (#2989 review: a platform alarm was never in
        the agent's file, so no file-derived state may be written on it)."""
        return [
            func.substr(operator_queue.c.request_id, 1, len(p)) != p
            for p in (prefixes or ())
        ]

    def mark_undelivered_for_stopped_agents(
        self, now: str, *, running_agents: List[str],
        exclude_request_id_prefixes=None,
    ) -> List[Dict]:
        """Every answer / terminal flip still owed to an agent that is NOT running
        becomes `undelivered:agent_not_running` (#2989 review, AC3) — and the
        transitioned rows are returned so the caller can audit each once.
        Edge-triggered; an empty running list is an explicit branch."""
        conds = [
            operator_queue.c.status.in_(("responded", "cancelled", "expired")),
            or_(
                operator_queue.c.delivery_state.is_(None),
                and_(
                    operator_queue.c.delivery_state == "undelivered",
                    func.coalesce(operator_queue.c.delivery_detail, "") != "agent_not_running",
                ),
            ),
            *self._not_prefixed(exclude_request_id_prefixes),
            self._file_contract(),  # trinity-enterprise#611: nothing is owed to a file
        ]
        if running_agents:
            conds.append(operator_queue.c.agent_name.notin_(list(running_agents)))
        with get_engine().begin() as conn:
            rows = conn.execute(
                select(operator_queue.c.id, operator_queue.c.status, operator_queue.c.agent_name)
                .where(and_(*conds))
            ).mappings().all()
            if not rows:
                return []
            conn.execute(
                update(operator_queue)
                .where(operator_queue.c.id.in_([r["id"] for r in rows]))
                .values(delivery_state="undelivered", delivery_detail="agent_not_running",
                        delivery_updated_at=now)
            )
        return [dict(r) for r in rows]

    def mark_unconfirmed(
        self, detail: str, now: str, *,
        agent_name: Optional[str] = None,
        exclude_agents: Optional[List[str]] = None,
        exclude_request_id_prefixes=None,
    ) -> int:
        """Flip open rows to `unconfirmed:<detail>` — for ONE agent (`agent_name`,
        the read-failure path) or for every agent NOT in `exclude_agents` (the
        per-cycle not-running sweep). An EMPTY exclude list means every open row,
        spelled as an explicit branch because SQLAlchemy's `notin_([])` warns and
        matches every row by accident. Edge-triggered: rows already carrying the
        value are excluded, so the steady state writes nothing."""
        conds = [
            operator_queue.c.status.in_(("pending", "responded")),
            or_(
                operator_queue.c.sync_state.is_(None),
                operator_queue.c.sync_state != "unconfirmed",
                func.coalesce(operator_queue.c.sync_detail, "") != detail,
            ),
        ]
        if agent_name is not None:
            conds.append(operator_queue.c.agent_name == agent_name)
        elif exclude_agents:
            conds.append(operator_queue.c.agent_name.notin_(list(exclude_agents)))
        conds.extend(self._not_prefixed(exclude_request_id_prefixes))
        conds.append(self._file_contract())  # trinity-enterprise#611: a native ask has no file to read
        with get_engine().begin() as conn:
            result = conn.execute(
                update(operator_queue)
                .where(and_(*conds))
                .values(sync_state="unconfirmed", sync_detail=detail, sync_updated_at=now)
            )
            return result.rowcount

    def count_flags(self, accessible_agent_names: Optional[Set[str]] = None) -> Dict[str, int]:
        """`{"undelivered": n, "closed_by_filer": n}` over the rows the caller may
        see — the visible escalation the Operations header renders (#2915)."""
        base = []
        if accessible_agent_names is not None:
            if not accessible_agent_names:
                return {"undelivered": 0, "closed_by_filer": 0}
            base.append(operator_queue.c.agent_name.in_(list(accessible_agent_names)))
        # #2989 review: a cancellation whose entry the agent already dropped is
        # recorded but is not an escalation — nothing is left for anyone to do.
        undelivered = select(func.count()).select_from(operator_queue).where(
            and_(*base, operator_queue.c.delivery_state == "undelivered",
                 operator_queue.c.cleared_at.is_(None),
                 ~and_(operator_queue.c.status.in_(("cancelled", "expired")),
                       func.coalesce(operator_queue.c.delivery_detail, "") == "entry_missing"))
        )
        closed = select(func.count()).select_from(operator_queue).where(
            and_(*base, operator_queue.c.status == "pending",
                 operator_queue.c.sync_state == "closed_by_filer")
        )
        with get_engine().connect() as conn:
            u = conn.execute(undelivered).scalar() or 0
            c = conn.execute(closed).scalar() or 0
        return {"undelivered": int(u), "closed_by_filer": int(c)}

    def item_exists(self, agent_name: str, item_id: str) -> bool:
        """Check whether this agent already created an item for a request id.

        #1631: scoped to (agent_name, request_id). The old id-only check was the
        collision bug — agent A's id read as "exists" for agent B, so B's item
        (a distinct request) was never created. `item_id` here is the agent's
        `request_id`, not the platform uuid `id`.
        """
        stmt = select(operator_queue.c.id).where(
            and_(
                operator_queue.c.agent_name == agent_name,
                operator_queue.c.request_id == item_id,
            )
        )
        with get_engine().connect() as conn:
            return conn.execute(stmt).first() is not None

    def count_pending_for_agent(
        self, agent_name: str, item_type: Optional[str] = None,
        exclude_request_id_prefixes=None,
    ) -> int:
        """#1632: count an agent's currently-pending operator-queue rows.

        The primary, Redis-independent depth bound for the ingestion cap: the
        sync service admits new agent items only while this count (plus what it
        has admitted this cycle) stays under OPERATOR_QUEUE_MAX_PENDING_PER_AGENT.
        Dialect-agnostic (SQLite + PostgreSQL, #300).

        #1677: the optional ``item_type`` narrows the count to one type — the
        per-(agent, type) budget read for agent-influenceable platform alert
        emitters (``operator_queue_service.create_bounded_alert``). ``None``
        keeps the #1632 all-types semantics unchanged. Query-only change — no
        schema change, so no migration on either track.

        trinity-enterprise#751: gate-raised rows are left out — they are not
        the agent's budget to spend.

        #3130: ``exclude_request_id_prefixes`` leaves out platform-minted rows
        (`_own_pending_conds`). The ingestion cap passes it; the #1677 per-type
        budget read does not — platform rows are exactly what it budgets.
        """
        conds = _own_pending_conds(agent_name, exclude_request_id_prefixes)
        if item_type is not None:
            conds.append(operator_queue.c.type == item_type)
        stmt = select(func.count()).where(and_(*conds))
        with get_engine().connect() as conn:
            return int(conn.execute(stmt).scalar() or 0)
