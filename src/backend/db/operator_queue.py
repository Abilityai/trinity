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

import json
import uuid
from typing import Optional, List, Dict, Set
from datetime import datetime

from sqlalchemy import select, update, func, and_, or_, case, delete

from .engine import get_engine, make_insert
from .tables import operator_queue
from utils.helpers import utc_now_iso, iso_cutoff


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
    )

    def create_item(self, agent_name: str, item: Dict) -> str:
        """Create a queue item from agent JSON data.

        Args:
            agent_name: The agent that created this item
            item: Queue item data from agent's operator-queue.json

        Returns:
            The item ID (the platform-minted uuid of the row that actually
            exists — on conflict that is the pre-existing row, NOT the uuid this
            call minted).
        """
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
        stmt = make_insert(operator_queue).values(
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
            expires_at=item.get("expires_at"),
            # ent#364: already validated against the agent's roster by
            # `operator_queue_service._validated_addressee`. This layer stores it;
            # it does not decide it, and it must never derive it from `context`
            # (which is agent-authored).
            addressed_to_email=item.get("addressed_to_email"),
        ).on_conflict_do_nothing(index_elements=["agent_name", "request_id"])

        # Insert + re-read in one transaction: on conflict the insert is a no-op
        # and the surviving row carries a DIFFERENT uuid, so to honour the
        # documented contract (return the id of the row that exists) the return
        # must be that row's id, not the `new_id` this call minted and discarded.
        with get_engine().begin() as conn:
            conn.execute(stmt)
            row = conn.execute(
                select(operator_queue.c.id).where(
                    and_(
                        operator_queue.c.agent_name == agent_name,
                        operator_queue.c.request_id == request_id,
                    )
                )
            ).first()
        return row[0] if row else new_id

    def get_item(self, item_id: str) -> Optional[Dict]:
        """Get a single queue item by ID."""
        stmt = select(*self._SELECT_COLS).where(operator_queue.c.id == item_id)
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()

        if not row:
            return None
        return self._row_to_item(row)

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
    ) -> List[Dict]:
        """List queue items with optional filters.

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
        if accessible_agent_names is not None and len(accessible_agent_names) == 0:
            return []

        conds = []
        if not include_cleared:
            conds.append(operator_queue.c.cleared_at.is_(None))  # #1017

        if accessible_agent_names is not None:
            conds.append(operator_queue.c.agent_name.in_(sorted(accessible_agent_names)))
        if status:
            conds.append(operator_queue.c.status == status)
        if type:
            conds.append(operator_queue.c.type == type)
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

        # Sort: pending items by priority then age, others by created_at desc
        status_order = case(
            (operator_queue.c.status == "pending", 0),
            else_=1,
        )
        priority_order = case(
            (operator_queue.c.priority == "critical", 0),
            (operator_queue.c.priority == "high", 1),
            (operator_queue.c.priority == "medium", 2),
            (operator_queue.c.priority == "low", 3),
            else_=4,
        )

        stmt = select(*self._SELECT_COLS)
        if conds:
            stmt = stmt.where(and_(*conds))
        stmt = (
            stmt.order_by(
                status_order,
                priority_order,
                operator_queue.c.created_at.desc(),
            )
            .limit(limit)
            .offset(offset)
        )

        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()

        return [self._row_to_item(row) for row in rows]

    def respond_to_item(
        self,
        item_id: str,
        response: str,
        response_text: Optional[str],
        responded_by_id: Optional[str],
        responded_by_email: str,
    ) -> Optional[Dict]:
        """Record a response to a queue item.

        Returns the updated item or None if not found.

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
                    )
                )
                .values(
                    status="responded",
                    response=response,
                    response_text=response_text,
                    responded_by_id=responded_by_id,
                    responded_by_email=responded_by_email,
                    responded_at=now,
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
                # landed between the router's status check and this UPDATE).
                # Mark the conflict so the router can 409 instead of returning
                # a 200 for a response that was never recorded (#1017).
                item = self.get_item(item_id)
                item["_status_conflict"] = True
                return item

        return self.get_item(item_id)

    def cancel_item(self, item_id: str) -> Optional[Dict]:
        """Cancel a pending queue item."""
        with get_engine().begin() as conn:
            result = conn.execute(
                update(operator_queue)
                .where(
                    and_(
                        operator_queue.c.id == item_id,
                        operator_queue.c.status == "pending",
                    )
                )
                .values(status="cancelled")
            )

            if result.rowcount == 0:
                exists = conn.execute(
                    select(operator_queue.c.id).where(operator_queue.c.id == item_id)
                ).first()
                if not exists:
                    return None

        return self.get_item(item_id)

    def bulk_cancel_items(
        self,
        ids: List[str],
        accessible_agent_names: Optional[Set[str]] = None,
    ) -> int:
        """Cancel the listed items that are still pending (#1017).

        Only items in `ids` are touched — the caller sends the ids it actually
        showed the operator, so a sync-loop race can never cancel items the
        operator never saw. Non-pending and inaccessible ids are skipped.

        accessible_agent_names: None = no filter (admin); empty set = no-op
        (a zero-agent user must not be able to touch anything); non-empty =
        SQL-side IN filter.

        Returns the number of items actually cancelled.
        """
        if not ids:
            return 0
        if accessible_agent_names is not None and len(accessible_agent_names) == 0:
            return 0

        conds = [
            operator_queue.c.status == "pending",
            operator_queue.c.id.in_(list(ids)),
        ]
        if accessible_agent_names is not None:
            conds.append(operator_queue.c.agent_name.in_(sorted(accessible_agent_names)))

        with get_engine().begin() as conn:
            result = conn.execute(
                update(operator_queue).where(and_(*conds)).values(status="cancelled")
            )
            return result.rowcount

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
            operator_queue.c.status.in_(("acknowledged", "cancelled", "expired")),
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

    def mark_expired(self) -> int:
        """Mark pending items past their expires_at as expired.

        Returns number of items expired.
        """
        now = utc_now_iso()
        with get_engine().begin() as conn:
            result = conn.execute(
                update(operator_queue)
                .where(
                    and_(
                        operator_queue.c.status == "pending",
                        operator_queue.c.expires_at.isnot(None),
                        operator_queue.c.expires_at < now,
                    )
                )
                .values(status="expired")
            )
            return result.rowcount

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
            )
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_item(row) for row in rows]

    def get_terminal_items_for_agent(self, agent_name: str, since_hours: int = 168) -> List[Dict]:
        """Get recently cancelled/expired items for a specific agent (#1017).

        Used by the sync service to flip still-'pending' entries in the
        agent's queue file to their terminal status so the agent stops
        waiting (and so a stale 'pending' file entry can't resurrect the
        item if its row is ever purged). Deliberately NOT filtered on
        cleared_at — hidden items still need their flip delivered. Bounded
        by created_at (there is no per-status timestamp) so the per-agent
        5s sync query stays cheap.
        """
        cutoff = iso_cutoff(since_hours)
        stmt = select(*self._SELECT_COLS).where(
            and_(
                operator_queue.c.agent_name == agent_name,
                operator_queue.c.status.in_(("cancelled", "expired")),
                operator_queue.c.created_at >= cutoff,
            )
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_item(row) for row in rows]

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
        self, agent_name: str, item_type: Optional[str] = None
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
        """
        conds = [
            operator_queue.c.agent_name == agent_name,
            operator_queue.c.status == "pending",
        ]
        if item_type is not None:
            conds.append(operator_queue.c.type == item_type)
        stmt = select(func.count()).where(and_(*conds))
        with get_engine().connect() as conn:
            return int(conn.execute(stmt).scalar() or 0)
