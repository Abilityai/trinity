"""Data-access for the multi-agent rooms module (ent#169; OSS since ent#443).

Raw ``text()`` with named params so it runs unchanged on SQLite and PostgreSQL.
The tables ARE in the OSS ``db/tables.py`` MetaData as of ent#443 (Alembic needs
them there), but the queries stay hand-written rather than Core-built — the
``client_portal/db.py`` pattern, and the shape every one of these statements was
reviewed in.

No business rules here: the service layer owns the ACL, the turn engine and the
budgets. The one thing this layer DOES own is **seq allocation**, because it is a
correctness property of the storage, not of the policy — see ``append_message``.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import select, text

from db.engine import get_engine
from db.tables import agent_ownership


# --- rooms -------------------------------------------------------------------

def create_room(room_id: str, name: str, topic: Optional[str], created_by: Optional[str],
                max_messages: int, max_cost_usd: Optional[float],
                expires_at: Optional[str], now: str) -> None:
    stmt = text(
        "INSERT INTO enterprise_rooms "
        "(id, name, topic, created_by, status, max_messages, max_cost_usd, expires_at, created_at) "
        "VALUES (:id, :name, :topic, :by, 'open', :maxm, :maxc, :exp, :now)"
    )
    with get_engine().begin() as conn:
        conn.execute(stmt, {
            "id": room_id, "name": name, "topic": topic, "by": created_by,
            "maxm": max_messages, "maxc": max_cost_usd, "exp": expires_at, "now": now,
        })


def create_room_with_participants(room_id: str, name: str, topic: Optional[str],
                                  created_by: Optional[str], max_messages: int,
                                  max_cost_usd: Optional[float], expires_at: Optional[str],
                                  participants: list[tuple[str, str, str]],
                                  system_message_id: str, system_body: str,
                                  now: str) -> None:
    """Create a room, seat its participants and post the opening system line —
    in ONE transaction (ent#220 item 6).

    Previously three-plus separate commits: a failure between them left a room
    with no members (its own creator 404'd by the membership check), or members
    with no opening line. Nothing repaired that, because a half-created room
    looks exactly like a legitimate one to every reader.

    `participants` is `[(kind, identity, role), ...]`.
    """
    room_stmt = text(
        "INSERT INTO enterprise_rooms "
        "(id, name, topic, created_by, status, max_messages, max_cost_usd, expires_at, created_at) "
        "VALUES (:id, :name, :topic, :by, 'open', :maxm, :maxc, :exp, :now)"
    )
    part_stmt = text(
        "INSERT INTO enterprise_room_participants "
        "(room_id, kind, identity, role, joined_at, last_read_seq) "
        "VALUES (:room, :kind, :identity, :role, :now, 0)"
    )
    # Column list and order mirror `append_message` — the seq is literal 1
    # because this room is being created in this same transaction, so it cannot
    # already hold a message and there is no race to lose.
    msg_stmt = text(
        "INSERT INTO enterprise_room_messages "
        "(id, room_id, seq, sender_kind, sender_identity, kind, mentions, "
        " content, execution_id, created_at) "
        "VALUES (:id, :room, 1, 'system', NULL, 'system', '[]', "
        "        :content, NULL, :now)"
    )
    with get_engine().begin() as conn:
        conn.execute(room_stmt, {
            "id": room_id, "name": name, "topic": topic, "by": created_by,
            "maxm": max_messages, "maxc": max_cost_usd, "exp": expires_at, "now": now,
        })
        for kind, identity, role in participants:
            conn.execute(part_stmt, {"room": room_id, "kind": kind,
                                     "identity": identity, "role": role, "now": now})
        conn.execute(msg_stmt, {"id": system_message_id, "room": room_id,
                                "content": system_body, "now": now})


def get_room(room_id: str) -> Optional[dict]:
    stmt = text(
        "SELECT id, name, topic, created_by, status, stop_reason, max_messages, "
        "       max_cost_usd, expires_at, created_at, closed_at "
        "FROM enterprise_rooms WHERE id = :id"
    )
    with get_engine().connect() as conn:
        row = conn.execute(stmt, {"id": room_id}).mappings().first()
        return dict(row) if row else None


def list_rooms_for_participant(kind: str, identity: str) -> list[dict]:
    """Rooms this participant is (or was) in — membership IS the visibility rule."""
    stmt = text(
        "SELECT r.id, r.name, r.topic, r.status, r.stop_reason, r.created_at, "
        "       r.expires_at, r.max_messages, r.max_cost_usd "
        "FROM enterprise_rooms r "
        "JOIN enterprise_room_participants p ON p.room_id = r.id "
        "WHERE p.kind = :kind AND p.identity = :identity "
        "ORDER BY r.created_at DESC"
    )
    with get_engine().connect() as conn:
        return [dict(r) for r in conn.execute(
            stmt, {"kind": kind, "identity": identity}).mappings()]


def list_all_rooms() -> list[dict]:
    """Admin view — every room."""
    stmt = text(
        "SELECT id, name, topic, status, stop_reason, created_at, expires_at, "
        "       max_messages, max_cost_usd FROM enterprise_rooms ORDER BY created_at DESC"
    )
    with get_engine().connect() as conn:
        return [dict(r) for r in conn.execute(stmt).mappings()]


def close_room(room_id: str, stop_reason: str, now: str) -> bool:
    """CAS close — only an OPEN room transitions, so a concurrent close (user vs
    budget sweep) produces one winner and the reason can't be overwritten."""
    stmt = text(
        "UPDATE enterprise_rooms SET status = 'closed', stop_reason = :reason, "
        "closed_at = :now WHERE id = :id AND status = 'open'"
    )
    with get_engine().begin() as conn:
        return (conn.execute(stmt, {"id": room_id, "reason": stop_reason, "now": now}).rowcount or 0) > 0


# --- participants ------------------------------------------------------------

def add_participant(room_id: str, kind: str, identity: str, role: str, now: str) -> None:
    """Idempotent — re-adding an existing participant is a no-op (the UNIQUE
    triple is the claim), so a retried create doesn't 500."""
    from sqlalchemy.exc import IntegrityError
    stmt = text(
        "INSERT INTO enterprise_room_participants "
        "(room_id, kind, identity, role, joined_at, last_read_seq) "
        "VALUES (:room, :kind, :identity, :role, :now, 0)"
    )
    try:
        with get_engine().begin() as conn:
            conn.execute(stmt, {"room": room_id, "kind": kind, "identity": identity,
                                "role": role, "now": now})
    except IntegrityError:
        pass


def list_participants(room_id: str) -> list[dict]:
    stmt = text(
        "SELECT kind, identity, role, joined_at, left_at, last_read_seq, cached_session_id "
        "FROM enterprise_room_participants WHERE room_id = :room "
        "ORDER BY kind, identity"
    )
    with get_engine().connect() as conn:
        return [dict(r) for r in conn.execute(stmt, {"room": room_id}).mappings()]


def list_participants_for_rooms(room_ids: list[str]) -> dict[str, list[dict]]:
    """Participants for MANY rooms in one query (ent#220 item 6).

    The sidebar lists every room the caller is in; per-room reads made that
    2N round-trips. Returns `{room_id: [participant, ...]}` with the same row
    shape and ordering `list_participants` gives, so the caller is unchanged
    apart from where the rows come from. An empty input short-circuits: an
    `IN ()` is a syntax error on both backends, not an empty result.
    """
    if not room_ids:
        return {}
    keys = [f"r{i}" for i in range(len(room_ids))]
    stmt = text(
        "SELECT room_id, kind, identity, role, joined_at, left_at, last_read_seq, "
        "       cached_session_id "
        "FROM enterprise_room_participants "
        "WHERE room_id IN (%s) ORDER BY room_id, kind, identity"
        % ",".join(f":{k}" for k in keys)
    )
    out: dict[str, list[dict]] = {}
    with get_engine().connect() as conn:
        for row in conn.execute(stmt, dict(zip(keys, room_ids))).mappings():
            row = dict(row)
            out.setdefault(row.pop("room_id"), []).append(row)
    return out


def count_messages_for_rooms(room_ids: list[str]) -> dict[str, int]:
    """Message counts for MANY rooms in one query (ent#220 item 6).

    A room with no rows is simply absent from the result — the caller defaults
    to 0 rather than this function inventing rows for ids it was handed.
    """
    if not room_ids:
        return {}
    keys = [f"r{i}" for i in range(len(room_ids))]
    stmt = text(
        "SELECT room_id, COUNT(*) AS n FROM enterprise_room_messages "
        "WHERE room_id IN (%s) GROUP BY room_id"
        % ",".join(f":{k}" for k in keys)
    )
    with get_engine().connect() as conn:
        return {r["room_id"]: int(r["n"] or 0)
                for r in conn.execute(stmt, dict(zip(keys, room_ids))).mappings()}


def get_participant(room_id: str, kind: str, identity: str) -> Optional[dict]:
    stmt = text(
        "SELECT kind, identity, role, joined_at, left_at, last_read_seq, "
        "       cached_session_id, consecutive_resume_failures "
        "FROM enterprise_room_participants "
        "WHERE room_id = :room AND kind = :kind AND identity = :identity"
    )
    with get_engine().connect() as conn:
        row = conn.execute(stmt, {"room": room_id, "kind": kind,
                                  "identity": identity}).mappings().first()
        return dict(row) if row else None


def remove_participant(room_id: str, kind: str, identity: str, now: str) -> bool:
    """Soft removal — the transcript keeps their messages, so a left participant
    must stay resolvable. Sets ``left_at`` rather than deleting the row."""
    stmt = text(
        "UPDATE enterprise_room_participants SET left_at = :now "
        "WHERE room_id = :room AND kind = :kind AND identity = :identity AND left_at IS NULL"
    )
    with get_engine().begin() as conn:
        return (conn.execute(stmt, {"room": room_id, "kind": kind,
                                    "identity": identity, "now": now}).rowcount or 0) > 0


def advance_read_cursor(room_id: str, identity: str, seq: int,
                        session_id: Optional[str]) -> None:
    """Move an agent's delta cursor forward after a SUCCESSFUL turn, and store
    the Claude session for the next ``--resume``.

    ``last_read_seq = MAX(current, :seq)`` — never rewinds, so an out-of-order
    completion can't re-deliver messages the agent has already seen.
    """
    stmt = text(
        "UPDATE enterprise_room_participants "
        "SET last_read_seq = CASE WHEN last_read_seq > :seq THEN last_read_seq ELSE :seq END, "
        "    cached_session_id = COALESCE(:sid, cached_session_id), "
        "    consecutive_resume_failures = 0 "
        "WHERE room_id = :room AND kind = 'agent' AND identity = :identity"
    )
    with get_engine().begin() as conn:
        conn.execute(stmt, {"room": room_id, "identity": identity, "seq": seq, "sid": session_id})


def clear_cached_session(room_id: str, identity: str) -> None:
    """Drop a dead resume handle and count the failure (Session-tab cold-fallback
    idiom) so the next turn starts cold instead of retrying a broken resume."""
    stmt = text(
        "UPDATE enterprise_room_participants "
        "SET cached_session_id = NULL, "
        "    consecutive_resume_failures = consecutive_resume_failures + 1 "
        "WHERE room_id = :room AND kind = 'agent' AND identity = :identity"
    )
    with get_engine().begin() as conn:
        conn.execute(stmt, {"room": room_id, "identity": identity})


# --- messages ----------------------------------------------------------------

def append_message(msg_id: str, room_id: str, sender_kind: str,
                   sender_identity: Optional[str], content: str,
                   mentions: Optional[list[str]], kind: str,
                   execution_id: Optional[str], now: str) -> int:
    """Append a message and return its allocated ``seq``.

    Seq allocation and the insert happen in ONE transaction: `seq` is the room's
    ordering primitive and the delta cursor's coordinate, so a gap or a duplicate
    is a correctness bug, not a cosmetic one. The UNIQUE(room_id, seq) constraint
    is the backstop — a concurrent racer loses on the constraint and retries
    rather than silently sharing a seq.
    """
    from sqlalchemy.exc import IntegrityError

    payload = {
        "id": msg_id, "room": room_id, "skind": sender_kind, "sid": sender_identity,
        "content": content, "mentions": json.dumps(mentions or []), "kind": kind,
        "eid": execution_id, "now": now,
    }
    for _attempt in range(5):
        try:
            with get_engine().begin() as conn:
                row = conn.execute(
                    text("SELECT COALESCE(MAX(seq), 0) + 1 FROM enterprise_room_messages "
                         "WHERE room_id = :room"),
                    {"room": room_id},
                ).first()
                seq = int(row[0]) if row else 1
                conn.execute(
                    text(
                        "INSERT INTO enterprise_room_messages "
                        "(id, room_id, seq, sender_kind, sender_identity, kind, mentions, "
                        " content, execution_id, created_at) "
                        "VALUES (:id, :room, :seq, :skind, :sid, :kind, :mentions, "
                        "        :content, :eid, :now)"
                    ),
                    {**payload, "seq": seq},
                )
                return seq
        except IntegrityError:
            continue  # lost the seq race — recompute and retry
    raise RuntimeError(f"could not allocate a seq for room {room_id} after 5 attempts")


def get_messages(room_id: str, since_seq: int = 0, limit: int = 500) -> list[dict]:
    stmt = text(
        "SELECT id, seq, sender_kind, sender_identity, kind, mentions, content, "
        "       execution_id, created_at "
        "FROM enterprise_room_messages WHERE room_id = :room AND seq > :since "
        "ORDER BY seq ASC LIMIT :lim"
    )
    with get_engine().connect() as conn:
        rows = [dict(r) for r in conn.execute(
            stmt, {"room": room_id, "since": since_seq, "lim": limit}).mappings()]
    for r in rows:
        try:
            r["mentions"] = json.loads(r.get("mentions") or "[]")
        except (TypeError, ValueError):
            r["mentions"] = []
    return rows


def get_recent_messages(room_id: str, limit: int) -> list[dict]:
    """The bounded tail a COLD agent gets (oldest-first for reading)."""
    stmt = text(
        "SELECT id, seq, sender_kind, sender_identity, kind, mentions, content, "
        "       execution_id, created_at "
        "FROM enterprise_room_messages WHERE room_id = :room "
        "ORDER BY seq DESC LIMIT :lim"
    )
    with get_engine().connect() as conn:
        rows = [dict(r) for r in conn.execute(
            stmt, {"room": room_id, "lim": limit}).mappings()]
    rows.reverse()
    for r in rows:
        try:
            r["mentions"] = json.loads(r.get("mentions") or "[]")
        except (TypeError, ValueError):
            r["mentions"] = []
    return rows


def count_messages(room_id: str) -> int:
    with get_engine().connect() as conn:
        row = conn.execute(
            text("SELECT COUNT(*) FROM enterprise_room_messages WHERE room_id = :room"),
            {"room": room_id},
        ).first()
        return int(row[0]) if row else 0


def count_budget_messages(room_id: str) -> int:
    """Conversational messages only — what ``max_messages`` actually budgets (ent#218).

    ``count_messages`` counts every row, system lines included, and the budget
    used to be checked against that. The room-created line is seq 1, so a room
    made with ``max_messages=1`` was dead on arrival: the creation line alone
    tripped the budget and the first human message was refused 410 before anyone
    spoke. More generally an ``N``-message room held fewer than ``N`` actual
    messages, because "Room created", "Room closed", "Mention chain stopped" and
    "could not respond" all silently consumed the operator's budget — and the
    closing line is itself posted BY the budget check, so the feature charged
    its own bookkeeping to the user.

    ``!= 'system'`` rather than ``IN ('user','agent')`` on purpose: a future
    participant kind (ent#171's external A2A sender, ent#362's workspace user)
    is conversational and must count. An allow-list would silently omit it and
    quietly widen every room's budget — and for a budget, the safe failure
    direction is counting too much, never too little.

    ``count_messages`` is left alone: it backs the ``message_count`` a room card
    displays, where "how many rows are in this transcript" is the honest answer.
    """
    with get_engine().connect() as conn:
        row = conn.execute(
            text(
                "SELECT COUNT(*) FROM enterprise_room_messages "
                "WHERE room_id = :room AND sender_kind != 'system'"
            ),
            {"room": room_id},
        ).first()
        return int(row[0]) if row else 0


def room_cost(room_id: str) -> float:
    """Room cost = SUM over the linked executions, computed on read.

    Deliberately not a maintained counter on the room row: a counter drifts the
    moment a turn fails, retries, or is finalized out-of-band, and then the
    budget enforces a number nobody can reconcile against the executions table.
    """
    stmt = text(
        "SELECT COALESCE(SUM(e.cost), 0) FROM enterprise_room_messages m "
        "JOIN schedule_executions e ON e.id = m.execution_id "
        "WHERE m.room_id = :room AND m.execution_id IS NOT NULL"
    )
    with get_engine().connect() as conn:
        row = conn.execute(stmt, {"room": room_id}).first()
        return float(row[0] or 0.0) if row else 0.0


# --- OSS reads ---------------------------------------------------------------

def agent_exists(agent_name: str) -> bool:
    stmt = select(agent_ownership.c.agent_name).where(
        agent_ownership.c.agent_name == agent_name,
        agent_ownership.c.deleted_at.is_(None),
    )
    with get_engine().connect() as conn:
        return conn.execute(stmt).first() is not None


def open_rooms_needing_sweep(now_iso: str) -> list[dict]:
    """Open rooms whose TTL has passed — the expiry sweep's candidate set."""
    stmt = text(
        "SELECT id, expires_at FROM enterprise_rooms "
        "WHERE status = 'open' AND expires_at IS NOT NULL AND expires_at <= :now"
    )
    with get_engine().connect() as conn:
        return [dict(r) for r in conn.execute(stmt, {"now": now_iso}).mappings()]
