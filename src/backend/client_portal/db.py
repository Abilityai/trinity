"""Data-access for the enterprise client-portal module (#79). Private.

Reads/writes two keys in the OSS ``system_settings`` table (the same
write-through pattern the retention module uses) — no private table. The engine
is resolved via OSS ``db/engine.py`` so it runs unchanged on SQLite and
PostgreSQL. No business rules here; the service layer owns validation +
fallback.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select, func, and_, or_, text, bindparam

from db.engine import get_engine, make_insert
from db.tables import system_settings, agent_sharing, agent_ownership, users


def get_setting(key: str, default: str = "") -> str:
    """Read an OSS ``system_settings`` value."""
    stmt = select(system_settings.c.value).where(system_settings.c.key == key)
    with get_engine().connect() as conn:
        row = conn.execute(stmt).first()
        return row[0] if row and row[0] is not None else default


def set_setting(key: str, value: str, now: str) -> None:
    """Upsert an OSS ``system_settings`` value. Mirrors ``db/settings.py:set_setting``."""
    stmt = (
        make_insert(system_settings)
        .values(key=key, value=value, updated_at=now)
        .on_conflict_do_update(
            index_elements=[system_settings.c.key],
            set_={"value": value, "updated_at": now},
        )
    )
    with get_engine().begin() as conn:
        conn.execute(stmt)


def get_agent_tts_voice_id(agent_name: str) -> Optional[str]:
    """The agent's configured ElevenLabs voice id (``agent_ownership.tts_voice_id``),
    or None. Drives portal voice mode (#78) — reuses the same per-agent voice the
    channel adapters speak with."""
    stmt = select(agent_ownership.c.tts_voice_id).where(
        agent_ownership.c.agent_name == agent_name
    )
    with get_engine().connect() as conn:
        row = conn.execute(stmt).first()
        return row[0] if row and row[0] else None


def get_shared_roster(email: str) -> list[dict]:
    """Every agent shared with ``email`` — the client's "My Agents" roster.

    Joins ``agent_sharing`` → ``agent_ownership`` (→ ``users`` for the owner
    label). Excludes soft-deleted and system agents. Read-only over OSS tables.
    """
    email = (email or "").strip().lower()
    if not email:
        return []

    stmt = (
        select(
            agent_sharing.c.agent_name,
            agent_sharing.c.created_at.label("shared_at"),
            agent_ownership.c.avatar_updated_at,
            agent_ownership.c.is_default_avatar,
            agent_ownership.c.tts_voice_id,          # portal voice (#78): drives voice_available
            users.c.username.label("owner"),
        )
        .select_from(
            agent_sharing
            .join(agent_ownership, agent_ownership.c.agent_name == agent_sharing.c.agent_name)
            .join(users, users.c.id == agent_ownership.c.owner_id, isouter=True)
        )
        .where(
            and_(
                func.lower(agent_sharing.c.shared_with_email) == email,
                agent_ownership.c.deleted_at.is_(None),
                or_(agent_ownership.c.is_system == 0, agent_ownership.c.is_system.is_(None)),
            )
        )
        .order_by(agent_sharing.c.agent_name)
    )
    with get_engine().connect() as conn:
        return [dict(row) for row in conn.execute(stmt).mappings()]


def get_owned_roster(email: str) -> list[dict]:
    """Every agent OWNED by the user whose sign-in email is ``email`` (ent#357).

    The shared roster is joined through ``agent_sharing``, and Trinity refuses a
    self-share ("Cannot share an agent with yourself"), so an owner can never
    appear in their own roster: they reach the Workspace in one click and land
    on an empty page with a dead "New chat" button. This is the other half —
    the agents they own — and it is unioned in ONLY for a platform session
    (`get_portal_principal().is_platform`). An external client's roster is
    untouched: it stays exactly what was shared with them.

    Same column shape as :func:`get_shared_roster` so the caller can merge the
    two lists without special-casing either. ``shared_at`` is the ownership row's
    ``created_at`` — the roster sorts by name anyway, and the field means "when
    this agent entered your list", which for an owned agent is when it was made.

    Identity is the users-row email, matched case-insensitively, exactly as the
    shared path matches ``shared_with_email``. Excludes soft-deleted and system
    agents, like the shared query.
    """
    email = (email or "").strip().lower()
    if not email:
        return []

    stmt = (
        select(
            agent_ownership.c.agent_name,
            agent_ownership.c.created_at.label("shared_at"),
            agent_ownership.c.avatar_updated_at,
            agent_ownership.c.is_default_avatar,
            agent_ownership.c.tts_voice_id,
            users.c.username.label("owner"),
        )
        .select_from(
            agent_ownership.join(users, users.c.id == agent_ownership.c.owner_id)
        )
        .where(
            and_(
                func.lower(users.c.email) == email,
                agent_ownership.c.deleted_at.is_(None),
                or_(agent_ownership.c.is_system == 0, agent_ownership.c.is_system.is_(None)),
            )
        )
        .order_by(agent_ownership.c.agent_name)
    )
    with get_engine().connect() as conn:
        return [dict(row) for row in conn.execute(stmt).mappings()]


# --- Portal chat history (#78): private enterprise_portal_messages table ------
# Raw text() with named params so it runs unchanged on SQLite + PostgreSQL (the
# table isn't in the OSS db/tables.py MetaData).

def add_portal_message(msg_id: str, agent_name: str, client_email: str,
                       role: str, content: str, cost, now: str,
                       session_id: Optional[str] = None) -> None:
    stmt = text(
        "INSERT INTO enterprise_portal_messages "
        "(id, agent_name, client_email, session_id, role, content, cost, created_at) "
        "VALUES (:id, :agent, :email, :session, :role, :content, :cost, :now)"
    )
    with get_engine().begin() as conn:
        conn.execute(stmt, {
            "id": msg_id, "agent": agent_name, "email": (client_email or "").lower(),
            "session": session_id, "role": role, "content": content, "cost": cost, "now": now,
        })


def get_portal_messages(agent_name: str, client_email: str, limit: int = 100,
                        session_id: Optional[str] = None) -> list[dict]:
    """The most-recent ``limit`` messages for a conversation, oldest-first for
    display. Scoped to one ``session_id`` when given (the multi-session read);
    with no session it falls back to the whole (agent, client) history — the
    legacy single-thread behaviour."""
    where = "agent_name = :agent AND client_email = :email"
    params = {"agent": agent_name, "email": (client_email or "").lower(), "lim": limit}
    if session_id is not None:
        where += " AND session_id = :session"
        params["session"] = session_id
    stmt = text(
        f"SELECT role, content, cost, created_at FROM enterprise_portal_messages "
        f"WHERE {where} ORDER BY created_at DESC LIMIT :lim"
    )
    with get_engine().connect() as conn:
        rows = [dict(r) for r in conn.execute(stmt, params).mappings()]
    rows.reverse()  # oldest-first for the chat view
    return rows


# --- Portal chat sessions (#78): one conversation thread per row --------------

def create_portal_session(session_id: str, agent_name: str, client_email: str,
                          now: str, title: Optional[str] = None) -> None:
    stmt = text(
        "INSERT INTO enterprise_portal_sessions "
        "(id, agent_name, client_email, title, created_at, last_message_at, message_count) "
        "VALUES (:id, :agent, :email, :title, :now, NULL, 0)"
    )
    with get_engine().begin() as conn:
        conn.execute(stmt, {
            "id": session_id, "agent": agent_name, "email": (client_email or "").lower(),
            "title": title, "now": now,
        })


def list_portal_sessions(agent_name: str, client_email: str) -> list[dict]:
    """A client's conversation threads with one agent, most-recently-active first.
    Sessions with no messages yet sort by ``created_at`` (``last_message_at`` NULL)."""
    stmt = text(
        "SELECT id, title, created_at, last_message_at, message_count "
        "FROM enterprise_portal_sessions "
        "WHERE agent_name = :agent AND client_email = :email "
        "ORDER BY COALESCE(last_message_at, created_at) DESC"
    )
    with get_engine().connect() as conn:
        return [dict(r) for r in conn.execute(stmt, {
            "agent": agent_name, "email": (client_email or "").lower(),
        }).mappings()]


def get_portal_session(session_id: str, agent_name: str, client_email: str) -> Optional[dict]:
    """One session row, scoped to (agent, client) so a client can't read another's
    thread by id. Returns None on miss."""
    stmt = text(
        "SELECT id, title, created_at, last_message_at, message_count "
        "FROM enterprise_portal_sessions "
        "WHERE id = :id AND agent_name = :agent AND client_email = :email"
    )
    with get_engine().connect() as conn:
        row = conn.execute(stmt, {
            "id": session_id, "agent": agent_name, "email": (client_email or "").lower(),
        }).mappings().first()
        return dict(row) if row else None


def get_latest_portal_session_id(agent_name: str, client_email: str) -> Optional[str]:
    """The most-recently-active session id for (agent, client), or None if the
    client has never chatted with this agent."""
    stmt = text(
        "SELECT id FROM enterprise_portal_sessions "
        "WHERE agent_name = :agent AND client_email = :email "
        "ORDER BY COALESCE(last_message_at, created_at) DESC LIMIT 1"
    )
    with get_engine().connect() as conn:
        row = conn.execute(stmt, {
            "agent": agent_name, "email": (client_email or "").lower(),
        }).first()
        return row[0] if row else None


def search_portal_sessions(client_email: str, like_pattern: str,
                           agent_names: list[str], limit: int = 30) -> list[dict]:
    """Search a client's conversation threads (across their rostered agents) whose
    TITLE or any MESSAGE content matches ``like_pattern`` (an already-lowercased,
    LIKE-escaped ``%needle%``). ``snippet`` is the most-recent matching message's
    content (NULL when only the title matched). Newest-active first.

    ``lower(col) LIKE :q`` is case-insensitive on both SQLite and PostgreSQL
    (PG ``LIKE`` is case-sensitive, so we lower both sides). ``ESCAPE '\\'`` lets
    the caller escape a literal ``%``/``_`` in the needle."""
    if not agent_names:
        return []
    sql = text(
        "SELECT s.id, s.agent_name, s.title, s.last_message_at, s.created_at, "
        "  (SELECT m.content FROM enterprise_portal_messages m "
        "   WHERE m.session_id = s.id AND lower(m.content) LIKE :q ESCAPE '\\' "
        "   ORDER BY m.created_at DESC LIMIT 1) AS snippet "
        "FROM enterprise_portal_sessions s "
        "WHERE lower(s.client_email) = :email AND s.agent_name IN :agents AND ( "
        "  lower(s.title) LIKE :q ESCAPE '\\' OR EXISTS ( "
        "    SELECT 1 FROM enterprise_portal_messages m2 "
        "    WHERE m2.session_id = s.id AND lower(m2.content) LIKE :q ESCAPE '\\')) "
        "ORDER BY COALESCE(s.last_message_at, s.created_at) DESC LIMIT :lim"
    ).bindparams(bindparam("agents", expanding=True))
    with get_engine().connect() as conn:
        return [dict(r) for r in conn.execute(sql, {
            "email": (client_email or "").lower(), "q": like_pattern,
            "agents": list(agent_names), "lim": limit,
        }).mappings()]


def set_portal_session_title(session_id: str, title: str) -> None:
    """Overwrite a thread's title (ent#186 — the generated title replacing the
    derived fallback ``touch_portal_session`` already stored). Unconditional by
    design: the caller decides *whether* to generate (first exchange only), this
    just lands the result."""
    stmt = text("UPDATE enterprise_portal_sessions SET title = :title WHERE id = :id")
    with get_engine().begin() as conn:
        conn.execute(stmt, {"title": title, "id": session_id})


def touch_portal_session(session_id: str, now: str, added: int = 2,
                         title_if_empty: Optional[str] = None) -> None:
    """After a turn: advance ``last_message_at``, add ``added`` to the count, and
    set ``title`` from the first message when it's still empty. One statement so
    the title only lands on the first turn (``title IS NULL``)."""
    stmt = text(
        "UPDATE enterprise_portal_sessions "
        "SET last_message_at = :now, "
        "    message_count = message_count + :added, "
        "    title = COALESCE(title, :title) "
        "WHERE id = :id"
    )
    with get_engine().begin() as conn:
        conn.execute(stmt, {"now": now, "added": added, "title": title_if_empty, "id": session_id})


# --- Blocked client identities (ent#281) --------------------------------------
# Private `enterprise_client_blocks`, keyed on the verified email. Raw text() for
# the same reason as the tables above: not in the OSS MetaData. Deliberately
# holds no portal-specific column — ent#21 reads these rows for channel
# identities, so anything portal-shaped here would be wrong by the time it does.

def is_client_blocked(email: str) -> bool:
    """True iff ``email`` is blocked. The gate every identity path consults.

    Not fail-open: the caller decides what a DB failure means. A block that
    silently evaporates on a bad read is not a block, so no exception is
    swallowed here.
    """
    email = (email or "").strip().lower()
    if not email:
        return False
    stmt = text("SELECT 1 FROM enterprise_client_blocks WHERE email = :email")
    with get_engine().connect() as conn:
        return conn.execute(stmt, {"email": email}).first() is not None


def get_client_block(email: str) -> Optional[dict]:
    """The full block row for ``email``, or None — for the operator's status view."""
    email = (email or "").strip().lower()
    if not email:
        return None
    stmt = text(
        "SELECT email, blocked_at, blocked_by_id, blocked_by_email, reason "
        "FROM enterprise_client_blocks WHERE email = :email"
    )
    with get_engine().connect() as conn:
        row = conn.execute(stmt, {"email": email}).mappings().first()
    return dict(row) if row else None


def list_client_blocks(emails: list[str]) -> dict[str, dict]:
    """Block rows for ``emails``, keyed by email — one query for a whole roster
    instead of N (the roster view renders every shared client at once)."""
    wanted = [e.strip().lower() for e in emails if e and e.strip()]
    if not wanted:
        return {}
    stmt = text(
        "SELECT email, blocked_at, blocked_by_id, blocked_by_email, reason "
        "FROM enterprise_client_blocks WHERE email IN :emails"
    ).bindparams(bindparam("emails", expanding=True))
    with get_engine().connect() as conn:
        rows = conn.execute(stmt, {"emails": wanted}).mappings()
        return {r["email"]: dict(r) for r in rows}


def block_client(email: str, now: str, blocked_by_id: Optional[str],
                 blocked_by_email: Optional[str], reason: Optional[str]) -> None:
    """Insert-or-refresh the block for ``email``. Idempotent: re-blocking an
    already-blocked client updates who/when/why rather than erroring, so a
    double-click is not an error the operator has to interpret."""
    email = (email or "").strip().lower()
    stmt = text(
        "INSERT INTO enterprise_client_blocks "
        "(email, blocked_at, blocked_by_id, blocked_by_email, reason) "
        "VALUES (:email, :now, :by_id, :by_email, :reason) "
        "ON CONFLICT (email) DO UPDATE SET "
        "  blocked_at = EXCLUDED.blocked_at, "
        "  blocked_by_id = EXCLUDED.blocked_by_id, "
        "  blocked_by_email = EXCLUDED.blocked_by_email, "
        "  reason = EXCLUDED.reason"
    )
    with get_engine().begin() as conn:
        conn.execute(stmt, {
            "email": email, "now": now, "by_id": blocked_by_id,
            "by_email": blocked_by_email, "reason": reason,
        })


def unblock_client(email: str) -> bool:
    """Remove the block. Returns True when a row was actually removed, so the
    caller can report "was not blocked" honestly instead of a bare 200."""
    email = (email or "").strip().lower()
    stmt = text("DELETE FROM enterprise_client_blocks WHERE email = :email")
    with get_engine().begin() as conn:
        return conn.execute(stmt, {"email": email}).rowcount > 0


def list_agent_client_emails(agent_name: str) -> list[dict]:
    """Emails an agent is shared with, plus each client's portal activity.

    The roster the operator acts on. `agent_sharing` is the grant (a share IS
    the portal account, #78), so it is the source of truth for "who can sign in
    to this agent"; the LEFT JOIN adds last-seen from the client's own portal
    threads, which is the only activity signal that exists — portal sessions are
    stateless JWTs, so there is no live-session count to report and the roster
    must not pretend otherwise.
    """
    stmt = text(
        "SELECT s.shared_with_email AS email, s.created_at AS shared_at, "
        "       MAX(p.last_message_at) AS last_active, "
        "       COALESCE(SUM(p.message_count), 0) AS message_count "
        "FROM agent_sharing s "
        "LEFT JOIN enterprise_portal_sessions p "
        "       ON LOWER(p.client_email) = LOWER(s.shared_with_email) "
        "      AND p.agent_name = s.agent_name "
        "WHERE s.agent_name = :agent "
        "GROUP BY s.shared_with_email, s.created_at "
        "ORDER BY s.shared_with_email"
    )
    with get_engine().connect() as conn:
        return [dict(r) for r in conn.execute(stmt, {"agent": agent_name}).mappings()]
def list_agent_share_emails(agent_name: str) -> list[str]:
    """Every email an agent is shared with (ent#308).

    Used to decide whether a pre-ent#308 inbox directory can be migrated safely:
    if two shared addresses collapse to the same legacy name, that directory has
    two claimants and no record of which file belongs to whom.
    """
    stmt = select(agent_sharing.c.shared_with_email).where(
        agent_sharing.c.agent_name == agent_name
    )
    with get_engine().connect() as conn:
        return [r[0] for r in conn.execute(stmt) if r[0]]
