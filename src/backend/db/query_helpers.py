"""Shared query helpers (#1265).

Small, backend-agnostic SQLAlchemy Core building blocks reused by more than one
``XOperations`` class so the window/aggregation SQL lives in one place instead
of being copy-pasted per domain.
"""
from __future__ import annotations

from typing import Iterable, List, Optional

from sqlalchemy import select, func, or_, and_
from sqlalchemy import Column

from .engine import get_engine


def latest_per_group(
    columns,
    partition_col: "Column",
    order_col: "Column",
    filter_col: "Column",
    values: Iterable,
) -> List:
    """Return the newest row per group in a single query.

    Runs ``ROW_NUMBER() OVER (PARTITION BY partition_col ORDER BY order_col
    DESC)`` over the rows where ``filter_col IN values`` and keeps ``rn == 1``
    per group. Returns name-accessible mapping rows (the ``rn`` helper column is
    dropped), so callers map by column name — never by position.

    Keep ``columns`` narrow: project only what the caller needs, not the whole
    table (avoids streaming large TEXT blobs the caller never reads). Empty
    ``values`` short-circuits to ``[]``.
    """
    values = list(values)
    if not values:
        return []

    rn = func.row_number().over(
        partition_by=partition_col,
        order_by=order_col.desc(),
    ).label("rn")
    subq = select(*columns, rn).where(filter_col.in_(values)).subquery()
    # Drop the trailing rn helper column; keep the projected columns by name.
    projected = list(subq.c)[:-1]
    stmt = select(*projected).where(subq.c.rn == 1)

    with get_engine().connect() as conn:
        return conn.execute(stmt).mappings().all()


# #3139: the triggers whose runs belong to the agent rather than to a person, so
# every viewer of the agent may account for them. A scheduled run is the agent's
# own work; everything else was started by someone.
VIEWER_SHARED_TRIGGERS = ("schedule",)


def viewer_scope(table, viewer_email: Optional[str]):
    """WHERE clause: the executions a Workspace client can account for (#3139).

    Their own turns (`source_user_email`), the runs those turns spawned (the
    inherited `source_channel_client`), and the agent's scheduled runs. Nothing
    another person started: their run id, timing and trigger are theirs.

    A scheduled run is shared UNLESS its schedule delivers to one person
    (`agent_schedules.deliver_to_workspace_email`, #498 — a seat's brief): that
    run is that person's, and its timing and schedule name are not another
    client's to read. So a schedule run is admitted when its schedule has no
    delivery target, or delivers to this viewer.

    Emails compare lower-cased on both sides, because the writers do not share a
    normaliser. A `None` viewer scopes to the shared runs only, so a caller that
    forgets to pass one fails closed rather than seeing everything.
    """
    from .tables import agent_schedules as sch
    v = (viewer_email or "").strip().lower()
    target = func.lower(func.trim(sch.c.deliver_to_workspace_email))
    someone_elses = select(sch.c.id).where(and_(
        sch.c.deliver_to_workspace_email.isnot(None),
        target != "",
        target != v,
    ))
    clauses = [and_(
        table.c.triggered_by.in_(VIEWER_SHARED_TRIGGERS),
        or_(table.c.schedule_id.is_(None), table.c.schedule_id.notin_(someone_elses)),
    )]
    if v:
        clauses.append(func.lower(table.c.source_user_email) == v)
        clauses.append(func.lower(table.c.source_channel_client) == v)
    return or_(*clauses)
