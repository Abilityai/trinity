"""Data access for Workspace suggestions (trinity-enterprise#465).

SQL only — no thresholds, no wording. Every read is scoped to ONE agent, and
the viewer-scoped ones to the viewer's own (lower-cased) email.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from sqlalchemy import and_, delete, func, select

from db.engine import get_engine, make_insert
from db.tables import (
    agent_reminders,
    agent_schedules,
    enterprise_portal_messages,
    schedule_executions,
    workspace_suggestion_feedback as feedback,
)

#: Rows of message/execution text scanned for `/name` invocations, newest
#: first. A bound, not a correctness knob: the question is "ever started", and
#: a playbook someone runs is started recently.
MAX_USAGE_ROWS = 2000

#: One viewer's feedback rows. Existing keys are always updatable; a new key
#: past the cap evicts the viewer's oldest row instead of being refused — a
#: hard refusal would leave a dismiss button that silently stops working.
MAX_FEEDBACK_ROWS = 1000


def _email(email: str) -> str:
    return (email or "").lower()


def list_schedules(agent_name: str) -> List[dict]:
    """The agent's live schedules. `message` is read for the playbook check only
    and never leaves the service (§5.11: prompts do not cross this surface)."""
    stmt = select(
        agent_schedules.c.id, agent_schedules.c.name, agent_schedules.c.enabled,
        agent_schedules.c.cron_expression, agent_schedules.c.timezone,
        agent_schedules.c.message, agent_schedules.c.created_at,
        agent_schedules.c.updated_at, agent_schedules.c.last_run_at,
    ).where(
        agent_schedules.c.agent_name == agent_name,
        agent_schedules.c.deleted_at.is_(None),
    )
    with get_engine().connect() as conn:
        return [dict(r) for r in conn.execute(stmt).mappings()]


def recent_runs_by_schedule(agent_name: str, since: str, per_schedule: int,
                            schedule_ids: List[str]) -> Dict[str, List[dict]]:
    """`{schedule_id: [run, ...]}` newest first, at most `per_schedule` each.

    ONE windowed query for the whole agent (served by
    `idx_executions_agent_started`) rather than one per schedule — restricted
    to the agent's live schedules, so chat/API runs (`schedule_id='__manual__'`)
    are never ranked just to be thrown away.
    """
    if not schedule_ids:
        return {}
    rn = func.row_number().over(
        partition_by=schedule_executions.c.schedule_id,
        order_by=(schedule_executions.c.started_at.desc(), schedule_executions.c.id.desc()),
    ).label("rn")
    inner = select(
        schedule_executions.c.id, schedule_executions.c.schedule_id,
        schedule_executions.c.status, schedule_executions.c.started_at, rn,
    ).where(
        schedule_executions.c.agent_name == agent_name,
        schedule_executions.c.started_at >= since,
        schedule_executions.c.schedule_id.in_(schedule_ids),
    ).subquery()
    stmt = select(inner.c.id, inner.c.schedule_id, inner.c.status, inner.c.started_at).where(
        inner.c.rn <= per_schedule
    ).order_by(inner.c.schedule_id, inner.c.rn)
    out: Dict[str, List[dict]] = {}
    with get_engine().connect() as conn:
        for r in conn.execute(stmt).mappings():
            out.setdefault(r["schedule_id"], []).append(dict(r))
    return out


def count_overdue_reminders(agent_name: str, now_iso: str) -> int:
    """Pending reminders already past `fire_at` — held when autonomy is off (#1806)."""
    stmt = select(func.count()).select_from(agent_reminders).where(
        agent_reminders.c.agent_name == agent_name,
        agent_reminders.c.status.in_(("pending", "firing")),
        agent_reminders.c.fire_at < now_iso,
    )
    with get_engine().connect() as conn:
        return int(conn.execute(stmt).scalar() or 0)


def last_user_message_at(agent_name: str, email: str) -> Optional[str]:
    """When this viewer last wrote to this agent in the Workspace.

    The viewer's OWN messages, not the session's `last_message_at`: a scheduled
    brief delivered into the Main chat (ent#498) moves that without the person
    saying anything, and would hide exactly the dormancy this measures.
    """
    stmt = select(func.max(enterprise_portal_messages.c.created_at)).where(
        enterprise_portal_messages.c.agent_name == agent_name,
        enterprise_portal_messages.c.client_email == _email(email),
        enterprise_portal_messages.c.role == "user",
    )
    with get_engine().connect() as conn:
        return conn.execute(stmt).scalar()


def viewer_has_runs(agent_name: str, email: str, since: str) -> bool:
    """Whether this viewer started anything on this agent outside the Workspace
    (operator chat, MCP) — attributed executions count as history too."""
    stmt = select(schedule_executions.c.id).where(
        schedule_executions.c.agent_name == agent_name,
        schedule_executions.c.started_at >= since,
        func.lower(schedule_executions.c.source_user_email) == _email(email),
    ).limit(1)
    with get_engine().connect() as conn:
        return conn.execute(stmt).first() is not None


def slash_texts_by_viewer(agent_name: str, email: str, since: str) -> List[str]:
    """Texts this viewer started with `/` on this agent — Workspace messages and
    executions attributed to them (operator chat, MCP) — newest first, bounded."""
    em = _email(email)
    portal = select(enterprise_portal_messages.c.content).where(
        enterprise_portal_messages.c.agent_name == agent_name,
        enterprise_portal_messages.c.client_email == em,
        enterprise_portal_messages.c.role == "user",
        enterprise_portal_messages.c.content.like("/%"),
    ).order_by(enterprise_portal_messages.c.created_at.desc()).limit(MAX_USAGE_ROWS)
    runs = select(schedule_executions.c.message).where(
        schedule_executions.c.agent_name == agent_name,
        schedule_executions.c.started_at >= since,
        func.lower(schedule_executions.c.source_user_email) == em,
        schedule_executions.c.message.like("/%"),
    ).order_by(schedule_executions.c.started_at.desc()).limit(MAX_USAGE_ROWS)
    with get_engine().connect() as conn:
        texts = [r[0] for r in conn.execute(portal) if r[0]]
        texts += [r[0] for r in conn.execute(runs) if r[0]]
    return texts


def dismissed_fingerprints(agent_name: str, email: str) -> Dict[str, str]:
    """`{suggestion_key: fingerprint}` for this viewer's dismissals on this agent."""
    stmt = select(feedback.c.suggestion_key, feedback.c.dismissed_fingerprint).where(
        feedback.c.client_email == _email(email),
        feedback.c.agent_name == agent_name,
        feedback.c.dismissed_at.is_not(None),
    )
    with get_engine().connect() as conn:
        return {r[0]: r[1] or "" for r in conn.execute(stmt)}


def record_feedback(*, email: str, agent_name: str, key: str, source: str,
                    action: str, fingerprint: str, now: str) -> None:
    """Upsert one accept or dismiss. The two live in separate columns so an
    accept never erases a dismissal (or the reverse)."""
    em = _email(email)
    pk = and_(
        feedback.c.client_email == em,
        feedback.c.agent_name == agent_name,
        feedback.c.suggestion_key == key,
    )
    with get_engine().begin() as conn:
        exists = conn.execute(select(func.count()).select_from(feedback).where(pk)).scalar()
        if not exists:
            total = conn.execute(
                select(func.count()).select_from(feedback).where(feedback.c.client_email == em)
            ).scalar() or 0
            if total >= MAX_FEEDBACK_ROWS:
                oldest = conn.execute(
                    select(feedback.c.agent_name, feedback.c.suggestion_key)
                    .where(feedback.c.client_email == em)
                    .order_by(feedback.c.updated_at.asc())
                    .limit(total - MAX_FEEDBACK_ROWS + 1)
                ).all()
                for agent, k in oldest:
                    conn.execute(delete(feedback).where(
                        feedback.c.client_email == em,
                        feedback.c.agent_name == agent,
                        feedback.c.suggestion_key == k,
                    ))
        if action == "dismiss":
            values = {"dismissed_at": now, "dismissed_fingerprint": fingerprint}
            update = dict(values)
        else:
            values = {"accepted_at": now, "accept_count": 1}
            update = {"accepted_at": now, "accept_count": feedback.c.accept_count + 1}
        stmt = make_insert(feedback).values(
            client_email=em, agent_name=agent_name, suggestion_key=key,
            surface="agent", source=source, updated_at=now,
            accept_count=values.pop("accept_count", 0), **values,
        ).on_conflict_do_update(
            index_elements=[feedback.c.client_email, feedback.c.agent_name, feedback.c.suggestion_key],
            set_={**update, "source": source, "updated_at": now},
        )
        conn.execute(stmt)
