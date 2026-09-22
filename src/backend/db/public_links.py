"""
Database operations for public agent links (Phase 12.2).

Handles:
- Public link CRUD operations
- Email verification codes
- Usage tracking

Converted from raw sqlite3 to SQLAlchemy Core (#300) so it runs unchanged on
both SQLite and PostgreSQL. Queries are built from the Core table handles in
``db/tables.py`` (dialect-agnostic expressions, no ``?``/``%s`` placeholders),
and the engine is resolved from ``DATABASE_URL`` via ``db/engine.py``. The
public API of ``PublicLinkOperations`` is unchanged.
"""

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, List

from sqlalchemy import select, insert, update, delete, func, and_, or_

from .engine import get_engine
from .tables import (
    public_user_memory_writes,
    agent_public_links,
    public_link_verifications,
    public_link_usage,
    public_user_memory,
)
from utils.helpers import utc_now_iso


def _utcnow() -> datetime:
    """Return timezone-aware UTC now."""
    return datetime.now(timezone.utc)


def _parse_memory_blob(memory_text: Optional[str]) -> dict:
    """Parse the JSON blob stored in public_user_memory.memory_text (#895).

    Storage shape is a JSON object with two named keys so that the
    agent-deliberate writer (write_user_memory MCP tool) and the
    5-message conversation summarizer can update independent sections
    without clobbering each other.

    Legacy plaintext rows (written before the split) are treated as
    `conversation_summary` — that is what the original summarizer wrote.
    Malformed JSON is also treated as plaintext.
    """
    if not memory_text:
        return {"agent_notes": "", "conversation_summary": ""}
    try:
        data = json.loads(memory_text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {"agent_notes": "", "conversation_summary": memory_text}
    if not isinstance(data, dict):
        return {"agent_notes": "", "conversation_summary": memory_text}
    return {
        "agent_notes": str(data.get("agent_notes") or ""),
        "conversation_summary": str(data.get("conversation_summary") or ""),
    }


def _encode_memory_blob(agent_notes: str, conversation_summary: str) -> str:
    """Encode the two-section memory representation as JSON for storage."""
    return json.dumps(
        {
            "agent_notes": agent_notes or "",
            "conversation_summary": conversation_summary or "",
        },
        ensure_ascii=False,
    )


def _parse_aware(dt_str: str) -> datetime:
    """Parse an ISO datetime string, ensuring the result is timezone-aware (UTC)."""
    dt = datetime.fromisoformat(dt_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class PublicLinkOperations:
    """Operations for managing public agent links."""

    def __init__(self, user_ops=None, agent_ops=None):
        self._user_ops = user_ops
        self._agent_ops = agent_ops

    # =========================================================================
    # Public Link CRUD
    # =========================================================================

    def create_public_link(
        self,
        agent_name: str,
        created_by: str,
        name: Optional[str] = None,
        expires_at: Optional[str] = None,
        link_type: str = "chat",
    ) -> dict:
        """Create a new public link for an agent.

        Email verification is agent-level (agent_ownership.require_email).
        The legacy `require_email` column on agent_public_links is left at
        its DEFAULT (0) and read by the Slack legacy connection join only.

        link_type: 'chat' (default) or 'site' (live web-server proxy, SITE-001)
        """
        link_id = secrets.token_urlsafe(16)
        token = secrets.token_urlsafe(24)
        now = utc_now_iso()

        with get_engine().begin() as conn:
            conn.execute(
                insert(agent_public_links).values(
                    id=link_id,
                    agent_name=agent_name,
                    token=token,
                    created_by=created_by,
                    created_at=now,
                    expires_at=expires_at,
                    enabled=1,
                    name=name,
                    type=link_type,
                )
            )

        return self.get_public_link(link_id)

    def get_public_link(self, link_id: str) -> Optional[dict]:
        """Get a public link by ID."""
        stmt = select(
            agent_public_links.c.id,
            agent_public_links.c.agent_name,
            agent_public_links.c.token,
            agent_public_links.c.created_by,
            agent_public_links.c.created_at,
            agent_public_links.c.expires_at,
            agent_public_links.c.enabled,
            agent_public_links.c.name,
            agent_public_links.c.type,
        ).where(agent_public_links.c.id == link_id)
        with get_engine().connect() as conn:
            row = conn.execute(stmt).first()

        if not row:
            return None

        return self._row_to_link(row)

    def get_public_link_by_token(self, token: str) -> Optional[dict]:
        """Get a public link by token."""
        stmt = select(
            agent_public_links.c.id,
            agent_public_links.c.agent_name,
            agent_public_links.c.token,
            agent_public_links.c.created_by,
            agent_public_links.c.created_at,
            agent_public_links.c.expires_at,
            agent_public_links.c.enabled,
            agent_public_links.c.name,
            agent_public_links.c.type,
        ).where(agent_public_links.c.token == token)
        with get_engine().connect() as conn:
            row = conn.execute(stmt).first()

        if not row:
            return None

        return self._row_to_link(row)

    def list_agent_public_links(self, agent_name: str) -> List[dict]:
        """List all public links for an agent."""
        stmt = (
            select(
                agent_public_links.c.id,
                agent_public_links.c.agent_name,
                agent_public_links.c.token,
                agent_public_links.c.created_by,
                agent_public_links.c.created_at,
                agent_public_links.c.expires_at,
                agent_public_links.c.enabled,
                agent_public_links.c.name,
                agent_public_links.c.type,
            )
            .where(agent_public_links.c.agent_name == agent_name)
            .order_by(agent_public_links.c.created_at.desc())
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).all()

        return [self._row_to_link(row) for row in rows]

    def update_public_link(
        self,
        link_id: str,
        name: Optional[str] = None,
        enabled: Optional[bool] = None,
        expires_at: Optional[str] = None
    ) -> Optional[dict]:
        """Update a public link.

        Email verification is agent-level (agent_ownership.require_email).
        """
        values = {}

        if name is not None:
            values["name"] = name
        if enabled is not None:
            values["enabled"] = 1 if enabled else 0
        if expires_at is not None:
            values["expires_at"] = expires_at

        if not values:
            return self.get_public_link(link_id)

        with get_engine().begin() as conn:
            conn.execute(
                update(agent_public_links)
                .where(agent_public_links.c.id == link_id)
                .values(**values)
            )

        return self.get_public_link(link_id)

    def delete_public_link(self, link_id: str) -> bool:
        """Delete a public link."""
        with get_engine().begin() as conn:
            # First delete related verifications and usage
            conn.execute(
                delete(public_link_verifications).where(
                    public_link_verifications.c.link_id == link_id
                )
            )
            conn.execute(
                delete(public_link_usage).where(
                    public_link_usage.c.link_id == link_id
                )
            )
            # Then delete the link
            result = conn.execute(
                delete(agent_public_links).where(
                    agent_public_links.c.id == link_id
                )
            )
            deleted = result.rowcount > 0

        return deleted

    def delete_agent_public_links(self, agent_name: str) -> int:
        """Delete all public links for an agent (cascade on agent deletion)."""
        with get_engine().begin() as conn:
            # Get link IDs first
            link_ids = [
                row[0]
                for row in conn.execute(
                    select(agent_public_links.c.id).where(
                        agent_public_links.c.agent_name == agent_name
                    )
                ).all()
            ]

            # Delete related data
            for link_id in link_ids:
                conn.execute(
                    delete(public_link_verifications).where(
                        public_link_verifications.c.link_id == link_id
                    )
                )
                conn.execute(
                    delete(public_link_usage).where(
                        public_link_usage.c.link_id == link_id
                    )
                )

            # Delete links
            result = conn.execute(
                delete(agent_public_links).where(
                    agent_public_links.c.agent_name == agent_name
                )
            )
            deleted = result.rowcount

        return deleted

    def is_link_valid(self, token: str) -> tuple[bool, Optional[str], Optional[dict]]:
        """
        Check if a public link token is valid.
        Returns: (is_valid, reason_if_invalid, link_data_if_valid)
        """
        link = self.get_public_link_by_token(token)

        if not link:
            return False, "not_found", None

        if not link["enabled"]:
            return False, "disabled", None

        if link["expires_at"]:
            expires = _parse_aware(link["expires_at"])
            if _utcnow() > expires:
                return False, "expired", None

        return True, None, link

    # =========================================================================
    # Email Verification
    # =========================================================================

    def create_verification(
        self,
        link_id: str,
        email: str,
        expiry_minutes: int = 10
    ) -> dict:
        """Create a verification code for email."""
        verification_id = secrets.token_urlsafe(16)
        code = str(secrets.randbelow(900000) + 100000)  # 6-digit code
        now = datetime.utcnow()
        expires_at = (now + timedelta(minutes=expiry_minutes)).isoformat()

        with get_engine().begin() as conn:
            # Invalidate any existing pending verifications for this email+link
            conn.execute(
                update(public_link_verifications)
                .where(
                    and_(
                        public_link_verifications.c.link_id == link_id,
                        public_link_verifications.c.email == email.lower(),
                        public_link_verifications.c.verified == 0,
                    )
                )
                .values(verified=-1)
            )

            conn.execute(
                insert(public_link_verifications).values(
                    id=verification_id,
                    link_id=link_id,
                    email=email.lower(),
                    code=code,
                    created_at=now.isoformat(),
                    expires_at=expires_at,
                    verified=0,
                )
            )

        return {
            "id": verification_id,
            "code": code,
            "expires_at": expires_at,
            "expires_in_seconds": expiry_minutes * 60
        }

    def verify_code(
        self,
        link_id: str,
        email: str,
        code: str,
        session_hours: int = 24
    ) -> tuple[bool, Optional[str], Optional[dict]]:
        """
        Verify an email verification code.
        Returns: (success, error_reason, session_data)
        """
        with get_engine().begin() as conn:
            row = conn.execute(
                select(
                    public_link_verifications.c.id,
                    public_link_verifications.c.expires_at,
                    public_link_verifications.c.verified,
                ).where(
                    and_(
                        public_link_verifications.c.link_id == link_id,
                        public_link_verifications.c.email == email.lower(),
                        public_link_verifications.c.code == code,
                        public_link_verifications.c.verified == 0,
                    )
                )
            ).first()

            if not row:
                return False, "invalid_code", None

            verification_id, expires_at, _ = row

            # Check expiration
            if _utcnow() > _parse_aware(expires_at):
                return False, "code_expired", None

            # Generate session token
            session_token = f"session_{secrets.token_urlsafe(32)}"
            session_expires = (datetime.utcnow() + timedelta(hours=session_hours)).isoformat()

            # Mark as verified and set session
            conn.execute(
                update(public_link_verifications)
                .where(public_link_verifications.c.id == verification_id)
                .values(
                    verified=1,
                    session_token=session_token,
                    session_expires_at=session_expires,
                )
            )

        return True, None, {
            "session_token": session_token,
            "expires_at": session_expires
        }

    def validate_session(self, link_id: str, session_token: str) -> tuple[bool, Optional[str]]:
        """
        Validate a session token.
        Returns: (is_valid, email_if_valid)
        """
        stmt = select(
            public_link_verifications.c.email,
            public_link_verifications.c.session_expires_at,
        ).where(
            and_(
                public_link_verifications.c.link_id == link_id,
                public_link_verifications.c.session_token == session_token,
                public_link_verifications.c.verified == 1,
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).first()

        if not row:
            return False, None

        email, session_expires = row

        if _utcnow() > _parse_aware(session_expires):
            return False, None

        return True, email

    def validate_agent_session(
        self, agent_name: str, session_token: str
    ) -> tuple[bool, Optional[str]]:
        """
        Validate a session token against ANY public link for the agent.

        A session verified on any of an agent's public links counts as
        valid for cross-resource access — specifically, FILES-001
        downloads (/api/files/{id}) reuse this instead of minting a
        separate verification flow.

        Returns: (is_valid, email_if_valid)
        """
        stmt = (
            select(
                public_link_verifications.c.email,
                public_link_verifications.c.session_expires_at,
            )
            .select_from(
                public_link_verifications.join(
                    agent_public_links,
                    public_link_verifications.c.link_id == agent_public_links.c.id,
                )
            )
            .where(
                and_(
                    agent_public_links.c.agent_name == agent_name,
                    public_link_verifications.c.session_token == session_token,
                    public_link_verifications.c.verified == 1,
                )
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).first()

        if not row:
            return False, None

        email, session_expires = row
        if _utcnow() > _parse_aware(session_expires):
            return False, None
        return True, email

    def count_recent_verification_requests(self, email: str, minutes: int = 10) -> int:
        """Count verification requests for an email in the last N minutes."""
        cutoff = (datetime.utcnow() - timedelta(minutes=minutes)).isoformat()

        stmt = select(func.count()).where(
            and_(
                public_link_verifications.c.email == email.lower(),
                public_link_verifications.c.created_at > cutoff,
            )
        )
        with get_engine().connect() as conn:
            count = conn.execute(stmt).scalar()

        return count

    # =========================================================================
    # Usage Tracking
    # =========================================================================

    def record_usage(
        self,
        link_id: str,
        email: Optional[str] = None,
        ip_address: Optional[str] = None
    ) -> dict:
        """Record a chat usage for a public link."""
        now = utc_now_iso()

        with get_engine().begin() as conn:
            # Check for existing usage record
            row = conn.execute(
                select(
                    public_link_usage.c.id,
                    public_link_usage.c.message_count,
                ).where(
                    and_(
                        public_link_usage.c.link_id == link_id,
                        or_(
                            public_link_usage.c.email == email,
                            and_(
                                public_link_usage.c.email.is_(None),
                                email is None,
                            ),
                        ),
                        or_(
                            public_link_usage.c.ip_address == ip_address,
                            public_link_usage.c.ip_address.is_(None),
                        ),
                    )
                )
            ).first()

            if row:
                usage_id, count = row
                conn.execute(
                    update(public_link_usage)
                    .where(public_link_usage.c.id == usage_id)
                    .values(message_count=count + 1, last_used_at=now)
                )
            else:
                usage_id = secrets.token_urlsafe(16)
                conn.execute(
                    insert(public_link_usage).values(
                        id=usage_id,
                        link_id=link_id,
                        email=email,
                        ip_address=ip_address,
                        message_count=1,
                        created_at=now,
                        last_used_at=now,
                    )
                )

        return {"id": usage_id, "recorded": True}

    def get_link_usage_stats(self, link_id: str) -> dict:
        """Get usage statistics for a public link."""
        with get_engine().connect() as conn:
            # Total messages
            total_messages = conn.execute(
                select(
                    func.coalesce(func.sum(public_link_usage.c.message_count), 0)
                ).where(public_link_usage.c.link_id == link_id)
            ).scalar()

            # Unique users (emails)
            unique_users = conn.execute(
                select(
                    func.count(func.distinct(public_link_usage.c.email))
                ).where(
                    and_(
                        public_link_usage.c.link_id == link_id,
                        public_link_usage.c.email.isnot(None),
                    )
                )
            ).scalar()

            # Unique IPs
            unique_ips = conn.execute(
                select(
                    func.count(func.distinct(public_link_usage.c.ip_address))
                ).where(
                    and_(
                        public_link_usage.c.link_id == link_id,
                        public_link_usage.c.ip_address.isnot(None),
                    )
                )
            ).scalar()

            # Last used
            last_used = conn.execute(
                select(
                    func.max(public_link_usage.c.last_used_at)
                ).where(public_link_usage.c.link_id == link_id)
            ).scalar()

        return {
            "total_messages": total_messages,
            "unique_users": unique_users,
            "unique_ips": unique_ips,
            "last_used_at": last_used
        }

    def count_recent_messages_by_ip(self, ip_address: str, minutes: int = 1) -> int:
        """Count messages from an IP in the last N minutes (for rate limiting)."""
        cutoff = (datetime.utcnow() - timedelta(minutes=minutes)).isoformat()

        stmt = select(
            func.coalesce(func.sum(public_link_usage.c.message_count), 0)
        ).where(
            and_(
                public_link_usage.c.ip_address == ip_address,
                public_link_usage.c.last_used_at > cutoff,
            )
        )
        with get_engine().connect() as conn:
            count = conn.execute(stmt).scalar()

        return count

    def count_recent_messages_by_token(self, link_id: str, minutes: int = 1) -> int:
        """Count all messages to a public link in the last N minutes (secondary rate limit).

        This caps flood attacks that rotate IPs to bypass the per-IP limit.
        """
        cutoff = (datetime.utcnow() - timedelta(minutes=minutes)).isoformat()

        stmt = select(
            func.coalesce(func.sum(public_link_usage.c.message_count), 0)
        ).where(
            and_(
                public_link_usage.c.link_id == link_id,
                public_link_usage.c.last_used_at > cutoff,
            )
        )
        with get_engine().connect() as conn:
            count = conn.execute(stmt).scalar()

        return count

    # =========================================================================
    # Per-User Memory (MEM-001)
    # =========================================================================

    def get_or_create_user_memory(self, agent_name: str, user_email: str) -> dict:
        """Get or create a memory record for (agent_name, user_email).

        Returns a dict with keys: id, agent_name, user_email, agent_notes,
        conversation_summary, message_count, created_at, updated_at.

        The two memory sections are parsed from the JSON blob stored in
        ``memory_text`` (#895). Legacy plaintext rows surface as
        ``conversation_summary`` (see :func:`_parse_memory_blob`).
        """
        email = user_email.lower()
        now = utc_now_iso()

        with get_engine().begin() as conn:
            row = conn.execute(
                select(
                    public_user_memory.c.id,
                    public_user_memory.c.agent_name,
                    public_user_memory.c.user_email,
                    public_user_memory.c.memory_text,
                    public_user_memory.c.message_count,
                    public_user_memory.c.created_at,
                    public_user_memory.c.updated_at,
                ).where(
                    and_(
                        public_user_memory.c.agent_name == agent_name,
                        public_user_memory.c.user_email == email,
                    )
                )
            ).first()

            if row:
                parsed = _parse_memory_blob(row[3])
                return {
                    "id": row[0], "agent_name": row[1], "user_email": row[2],
                    "agent_notes": parsed["agent_notes"],
                    "conversation_summary": parsed["conversation_summary"],
                    "message_count": row[4],
                    "created_at": row[5], "updated_at": row[6],
                }

            # Create new record
            memory_id = secrets.token_urlsafe(16)
            conn.execute(
                insert(public_user_memory).values(
                    id=memory_id,
                    agent_name=agent_name,
                    user_email=email,
                    memory_text="",
                    message_count=0,
                    created_at=now,
                    updated_at=now,
                )
            )

        return {
            "id": memory_id, "agent_name": agent_name, "user_email": email,
            "agent_notes": "", "conversation_summary": "",
            "message_count": 0,
            "created_at": now, "updated_at": now,
        }

    def increment_message_count(self, agent_name: str, user_email: str) -> int:
        """Increment message_count for (agent_name, user_email).

        Returns the new message_count.
        """
        email = user_email.lower()
        now = utc_now_iso()

        with get_engine().begin() as conn:
            conn.execute(
                update(public_user_memory)
                .where(
                    and_(
                        public_user_memory.c.agent_name == agent_name,
                        public_user_memory.c.user_email == email,
                    )
                )
                .values(
                    message_count=public_user_memory.c.message_count + 1,
                    updated_at=now,
                )
            )

            row = conn.execute(
                select(public_user_memory.c.message_count).where(
                    and_(
                        public_user_memory.c.agent_name == agent_name,
                        public_user_memory.c.user_email == email,
                    )
                )
            ).first()

        return row[0] if row else 0

    def _update_user_memory_section(
        self,
        agent_name: str,
        user_email: str,
        *,
        agent_notes: Optional[str] = None,
        conversation_summary: Optional[str] = None,
    ) -> bool:
        """Update one named section of the memory blob without touching the other (#895).

        The row is created on demand so callers don't need to seed it. Read +
        write happen on the same connection to keep the parse-merge-write
        sequence inside SQLite's connection-level serialization.
        """
        if agent_notes is None and conversation_summary is None:
            return False

        email = user_email.lower()
        now = utc_now_iso()

        with get_engine().begin() as conn:
            row = conn.execute(
                select(public_user_memory.c.memory_text).where(
                    and_(
                        public_user_memory.c.agent_name == agent_name,
                        public_user_memory.c.user_email == email,
                    )
                )
            ).first()

            if row is None:
                memory_id = secrets.token_urlsafe(16)
                conn.execute(
                    insert(public_user_memory).values(
                        id=memory_id,
                        agent_name=agent_name,
                        user_email=email,
                        memory_text="",
                        message_count=0,
                        created_at=now,
                        updated_at=now,
                    )
                )
                current = {"agent_notes": "", "conversation_summary": ""}
            else:
                current = _parse_memory_blob(row[0])

            if agent_notes is not None:
                current["agent_notes"] = agent_notes
            if conversation_summary is not None:
                current["conversation_summary"] = conversation_summary

            new_blob = _encode_memory_blob(
                current["agent_notes"], current["conversation_summary"]
            )

            conn.execute(
                update(public_user_memory)
                .where(
                    and_(
                        public_user_memory.c.agent_name == agent_name,
                        public_user_memory.c.user_email == email,
                    )
                )
                .values(memory_text=new_blob, updated_at=now)
            )

        return True

    def update_user_memory_agent_notes(
        self, agent_name: str, user_email: str, agent_notes: str
    ) -> bool:
        """Update only the agent_notes section (written by the write_user_memory MCP tool)."""
        return self._update_user_memory_section(
            agent_name, user_email, agent_notes=agent_notes or ""
        )

    def update_user_memory_conversation_summary(
        self, agent_name: str, user_email: str, conversation_summary: str
    ) -> bool:
        """Update only the conversation_summary section (written by the background summarizer)."""
        return self._update_user_memory_section(
            agent_name, user_email, conversation_summary=conversation_summary or ""
        )

    # ---- write history (ent#637 / ent#419 layer 3) --------------------------

    def write_user_memory_agent_notes(
        self,
        agent_name: str,
        user_email: str,
        agent_notes: str,
        *,
        execution_id: Optional[str],
        triggered_by: str,
        schedule_id: Optional[str] = None,
    ) -> dict:
        """Replace the agent_notes section AND record the write (ent#637).

        The one writer behind `POST /api/agents/{name}/user-memory`. Read,
        replace and the history insert happen in ONE transaction on one
        connection, so `previous_notes` is what the row held at the instant of
        the write, never a value read a moment earlier by a different
        connection. Returns ``{"write_id", "previous_notes", "new_notes"}``.

        Recorded for every trigger, not only `schedule`: the person's "what
        touched my memory" list is only honest if a chat write is in it too,
        and undo is latest-first over the same sequence.
        """
        email = user_email.lower()
        now = utc_now_iso()
        notes = agent_notes or ""
        with get_engine().begin() as conn:
            row = conn.execute(
                select(public_user_memory.c.memory_text).where(
                    and_(
                        public_user_memory.c.agent_name == agent_name,
                        public_user_memory.c.user_email == email,
                    )
                )
            ).first()
            if row is None:
                conn.execute(
                    insert(public_user_memory).values(
                        id=secrets.token_urlsafe(16),
                        agent_name=agent_name,
                        user_email=email,
                        memory_text="",
                        message_count=0,
                        created_at=now,
                        updated_at=now,
                    )
                )
                current = {"agent_notes": "", "conversation_summary": ""}
            else:
                current = _parse_memory_blob(row[0])
            previous = current["agent_notes"]
            conn.execute(
                update(public_user_memory)
                .where(
                    and_(
                        public_user_memory.c.agent_name == agent_name,
                        public_user_memory.c.user_email == email,
                    )
                )
                .values(
                    memory_text=_encode_memory_blob(notes, current["conversation_summary"]),
                    updated_at=now,
                )
            )
            write_id = secrets.token_urlsafe(16)
            conn.execute(
                insert(public_user_memory_writes).values(
                    id=write_id,
                    agent_name=agent_name,
                    user_email=email,
                    execution_id=execution_id,
                    triggered_by=triggered_by,
                    schedule_id=schedule_id,
                    previous_notes=previous,
                    new_notes=notes,
                    written_at=now,
                )
            )
        return {"write_id": write_id, "previous_notes": previous, "new_notes": notes}

    def list_user_memory_writes(
        self, agent_name: str, user_email: str, limit: int = 20
    ) -> List[dict]:
        """Newest first, bounded. Notes ride along so the person can see WHAT changed."""
        email = user_email.lower()
        with get_engine().connect() as conn:
            rows = conn.execute(
                select(
                    public_user_memory_writes.c.id,
                    public_user_memory_writes.c.execution_id,
                    public_user_memory_writes.c.triggered_by,
                    public_user_memory_writes.c.schedule_id,
                    public_user_memory_writes.c.previous_notes,
                    public_user_memory_writes.c.new_notes,
                    public_user_memory_writes.c.written_at,
                    public_user_memory_writes.c.undone_at,
                )
                .where(
                    and_(
                        public_user_memory_writes.c.agent_name == agent_name,
                        public_user_memory_writes.c.user_email == email,
                    )
                )
                .order_by(public_user_memory_writes.c.written_at.desc(),
                          public_user_memory_writes.c.id.desc())
                .limit(max(1, min(int(limit), 100)))
            ).mappings().all()
        return [dict(r) for r in rows]

    def undo_user_memory_write(
        self, agent_name: str, user_email: str, write_id: str, *, undone_by: str
    ) -> str:
        """Revert the agent_notes to what they were before ``write_id`` (ent#637).

        Latest-first: only the most recent write that is not already undone can
        be undone, so a revert never silently discards a later change the person
        has not looked at. Returns one of ``undone`` | ``not_found`` |
        ``already_undone`` | ``not_latest``. The restore and the mark are one
        transaction.
        """
        email = user_email.lower()
        now = utc_now_iso()
        with get_engine().begin() as conn:
            target = conn.execute(
                select(
                    public_user_memory_writes.c.id,
                    public_user_memory_writes.c.previous_notes,
                    public_user_memory_writes.c.undone_at,
                ).where(
                    and_(
                        public_user_memory_writes.c.id == write_id,
                        public_user_memory_writes.c.agent_name == agent_name,
                        public_user_memory_writes.c.user_email == email,
                    )
                )
            ).first()
            if target is None:
                return "not_found"
            if target[2]:
                return "already_undone"
            latest = conn.execute(
                select(public_user_memory_writes.c.id)
                .where(
                    and_(
                        public_user_memory_writes.c.agent_name == agent_name,
                        public_user_memory_writes.c.user_email == email,
                        public_user_memory_writes.c.undone_at.is_(None),
                    )
                )
                .order_by(public_user_memory_writes.c.written_at.desc(),
                          public_user_memory_writes.c.id.desc())
                .limit(1)
            ).first()
            if latest is None or latest[0] != write_id:
                return "not_latest"
            row = conn.execute(
                select(public_user_memory.c.memory_text).where(
                    and_(
                        public_user_memory.c.agent_name == agent_name,
                        public_user_memory.c.user_email == email,
                    )
                )
            ).first()
            current = _parse_memory_blob(row[0]) if row else {"agent_notes": "", "conversation_summary": ""}
            if row is not None:
                conn.execute(
                    update(public_user_memory)
                    .where(
                        and_(
                            public_user_memory.c.agent_name == agent_name,
                            public_user_memory.c.user_email == email,
                        )
                    )
                    .values(
                        memory_text=_encode_memory_blob(target[1], current["conversation_summary"]),
                        updated_at=now,
                    )
                )
            conn.execute(
                update(public_user_memory_writes)
                .where(public_user_memory_writes.c.id == write_id)
                .values(undone_at=now, undone_by=undone_by)
            )
        return "undone"

    # =========================================================================
    # Helpers
    # =========================================================================

    def _row_to_link(self, row) -> dict:
        """Convert a database row to a link dictionary."""
        return {
            "id": row[0],
            "agent_name": row[1],
            "token": row[2],
            "created_by": row[3],
            "created_at": row[4],
            "expires_at": row[5],
            "enabled": bool(row[6]),
            "name": row[7],
            "type": row[8] if len(row) > 8 else "chat",
        }
