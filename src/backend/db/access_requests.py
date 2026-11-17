"""
Database operations for per-agent access requests (Issue #311).

When a user tries to talk to an agent across any channel without being
owner / admin / shared / and the agent is not open-access, we record a
pending request. Owners can approve (which inserts into agent_sharing)
or deny.

Converted from raw sqlite3 to SQLAlchemy Core for the configurable database
backend (#300): runs unchanged on both SQLite and PostgreSQL. Queries are built
from the ``access_requests`` table in ``db/tables.py`` (no ``?`` placeholders,
no dialect-specific SQL), and the engine is resolved via ``db/engine.py``. The
public API of ``AccessRequestOperations`` is unchanged.
"""

import secrets
from typing import List, Optional

from sqlalchemy import select, update, delete, func

from .engine import get_engine, make_insert
from .tables import access_requests
from utils.helpers import utc_now_iso


# trinity-enterprise#737 — who asked, as the channel saw them. Every field is
# optional: what the channel does not expose stays None, never fabricated.
REQUESTER_FIELDS = (
    "requester_channel_id",  # the channel's stable user id (Telegram id, Slack member id, phone)
    "requester_handle",      # @username / Slack handle
    "requester_name",        # display name
    "requester_chat_id",     # the chat it arrived in, when that is not the user itself
    "requester_thread_id",   # the thread, on channels that have one
)


def _identity_values(identity: Optional[dict]) -> dict:
    identity = identity or {}
    out = {}
    for field in REQUESTER_FIELDS:
        value = identity.get(field)
        value = str(value).strip() if value is not None else ""
        out[field] = value[:256] or None
    return out


class AccessRequestOperations:
    """Operations for access_requests table."""

    @staticmethod
    def _row_to_dict(row) -> dict:
        return {
            "id": row["id"],
            "agent_name": row["agent_name"],
            "email": row["email"],
            "channel": row["channel"],
            "requested_at": row["requested_at"],
            "status": row["status"],
            "decided_by": row["decided_by"],
            "decided_at": row["decided_at"],
            **{field: row.get(field) for field in REQUESTER_FIELDS},
        }

    def upsert_pending(
        self,
        agent_name: str,
        email: str,
        channel: Optional[str] = None,
        identity: Optional[dict] = None,
    ) -> dict:
        """Insert or refresh a pending access request for (agent_name, email).

        If an approved/denied record exists and the user now has no access
        anyway (e.g. share removed), we reset to pending so the owner sees it.

        ``identity`` (trinity-enterprise#737) is who asked, as the channel saw
        them (``REQUESTER_FIELDS``). It describes THIS request, so a refresh
        replaces all of it: a request from a channel that exposes less (the
        public link exposes none) never inherits another channel's identity.
        """
        ident = _identity_values(identity)
        email = email.lower()
        now = utc_now_iso()
        rid = secrets.token_urlsafe(16)

        ins = make_insert(access_requests).values(
            id=rid,
            agent_name=agent_name,
            email=email,
            channel=channel,
            requested_at=now,
            status="pending",
            **ident,
        )
        # On conflict with the existing (agent_name, email) row: refresh status
        # to pending, bump the timestamp, keep the existing channel when no new
        # channel is supplied, and clear any prior decision.
        stmt = ins.on_conflict_do_update(
            index_elements=["agent_name", "email"],
            set_={
                "status": "pending",
                "requested_at": now,
                "channel": func.coalesce(ins.excluded.channel, access_requests.c.channel),
                "decided_by": None,
                "decided_at": None,
                **{field: getattr(ins.excluded, field) for field in REQUESTER_FIELDS},
            },
        )

        with get_engine().begin() as conn:
            conn.execute(stmt)
            row = conn.execute(
                select(access_requests).where(
                    (access_requests.c.agent_name == agent_name)
                    & (access_requests.c.email == email)
                )
            ).mappings().first()
        return self._row_to_dict(row) if row else None

    def list_for_agent(
        self,
        agent_name: str,
        status: Optional[str] = "pending",
    ) -> List[dict]:
        """List access requests for an agent, optionally filtered by status."""
        stmt = select(access_requests).where(access_requests.c.agent_name == agent_name)
        if status:
            stmt = stmt.where(access_requests.c.status == status)
        stmt = stmt.order_by(access_requests.c.requested_at.desc())
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_dict(r) for r in rows]

    def list_by_requester(self, channel: str, channel_user_id: str) -> List[dict]:
        """Every request — any agent, any status — this channel identity made
        (trinity-enterprise#737). The caller filters to what its viewer may see."""
        stmt = (
            select(access_requests)
            .where(access_requests.c.channel == channel)
            .where(access_requests.c.requester_channel_id == channel_user_id)
            .order_by(access_requests.c.requested_at.desc())
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_dict(r) for r in rows]

    def get(self, request_id: str) -> Optional[dict]:
        stmt = select(access_requests).where(access_requests.c.id == request_id)
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return self._row_to_dict(row) if row else None

    def decide(
        self,
        request_id: str,
        approve: bool,
        decided_by_user_id: int,
    ) -> Optional[dict]:
        """Mark a request approved or denied. Returns updated row."""
        now = utc_now_iso()
        new_status = "approved" if approve else "denied"
        with get_engine().begin() as conn:
            conn.execute(
                update(access_requests)
                .where(access_requests.c.id == request_id)
                .values(status=new_status, decided_by=decided_by_user_id, decided_at=now)
            )
            row = conn.execute(
                select(access_requests).where(access_requests.c.id == request_id)
            ).mappings().first()
        return self._row_to_dict(row) if row else None

    def delete_for_agent(self, agent_name: str) -> int:
        """Delete all access requests for an agent (on agent deletion)."""
        with get_engine().begin() as conn:
            result = conn.execute(
                delete(access_requests).where(access_requests.c.agent_name == agent_name)
            )
            return result.rowcount
