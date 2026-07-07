"""
Database operations for public chat session persistence (Phase 12.2.5: PUB-005).

Handles:
- Public chat session management (email and anonymous)
- Message persistence
- Context building for multi-turn conversations

Converted from raw sqlite3 to SQLAlchemy Core (#300) so it runs unchanged on
both SQLite and PostgreSQL. Queries are built from the
``public_chat_sessions`` / ``public_chat_messages`` tables in ``db/tables.py``
(dialect-agnostic expressions, no ``?`` placeholders), and the engine is
resolved from ``DATABASE_URL`` via ``db/engine.py``. The public API of
``PublicChatOperations`` is unchanged.
"""

import secrets
from datetime import datetime
from typing import Optional, List

from sqlalchemy import select, insert, update, delete, and_
from sqlalchemy.exc import IntegrityError

from .engine import get_engine
from .tables import public_chat_sessions, public_chat_messages
from db_models import PublicChatSession, PublicChatMessage
from utils.helpers import utc_now_iso

# Header injected into every public chat request so agents know they're serving a public user
PUBLIC_LINK_MODE_HEADER = "### Trinity: Public Link Access Mode"


class PublicChatOperations:
    """Operations for managing public chat sessions and messages."""

    @staticmethod
    def _row_to_session(row) -> PublicChatSession:
        """Convert a database row to a PublicChatSession model."""
        return PublicChatSession(
            id=row["id"],
            link_id=row["link_id"],
            session_identifier=row["session_identifier"],
            identifier_type=row["identifier_type"],
            created_at=datetime.fromisoformat(row["created_at"]),
            last_message_at=datetime.fromisoformat(row["last_message_at"]),
            message_count=row["message_count"],
            total_cost=row["total_cost"]
        )

    @staticmethod
    def _row_to_message(row) -> PublicChatMessage:
        """Convert a database row to a PublicChatMessage model."""
        return PublicChatMessage(
            id=row["id"],
            session_id=row["session_id"],
            role=row["role"],
            content=row["content"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            cost=row["cost"],
            # #903: nullable on pre-migration rows — `row.get` keeps the mapper
            # backward-compatible if a row is projected without the columns.
            sender_email=row.get("sender_email"),
            sender_label=row.get("sender_label"),
        )

    def get_or_create_session(
        self,
        link_id: str,
        session_identifier: str,
        identifier_type: str
    ) -> PublicChatSession:
        """
        Get existing session or create a new one for the given link and identifier.

        Args:
            link_id: The public link ID
            session_identifier: Email (lowercase) or anonymous token
            identifier_type: 'email' or 'anonymous'

        Returns:
            PublicChatSession model
        """
        # Normalize email identifiers
        normalized_identifier = session_identifier.lower() if identifier_type == 'email' else session_identifier

        select_existing = select(public_chat_sessions).where(
            and_(
                public_chat_sessions.c.link_id == link_id,
                public_chat_sessions.c.session_identifier == normalized_identifier,
            )
        )

        with get_engine().begin() as conn:
            # Try to find existing session
            row = conn.execute(select_existing).mappings().first()
            if row:
                return self._row_to_session(row)

            # Create new session
            session_id = secrets.token_urlsafe(16)
            now = utc_now_iso()

            # F-RACE (#903): thread-scoping drops sender_id from the channel key,
            # so two *different* users can now race select-then-insert on a
            # brand-new thread. The UNIQUE(link_id, session_identifier) constraint
            # makes the loser's INSERT raise; catch it, re-SELECT the winner's row
            # and return that (the idempotency_service.claim() pattern).
            # Dialect-safe: SQLite + PostgreSQL both surface IntegrityError.
            try:
                with conn.begin_nested():
                    conn.execute(
                        insert(public_chat_sessions).values(
                            id=session_id,
                            link_id=link_id,
                            session_identifier=normalized_identifier,
                            identifier_type=identifier_type,
                            created_at=now,
                            last_message_at=now,
                            message_count=0,
                            total_cost=0.0,
                        )
                    )
            except IntegrityError:
                row = conn.execute(select_existing).mappings().first()
                if row:
                    return self._row_to_session(row)
                raise

            # Return the new session
            row = conn.execute(
                select(public_chat_sessions).where(public_chat_sessions.c.id == session_id)
            ).mappings().first()
            return self._row_to_session(row)

    def get_session_by_identifier(
        self,
        link_id: str,
        session_identifier: str
    ) -> Optional[PublicChatSession]:
        """Look up a session by link ID and identifier."""
        with get_engine().connect() as conn:
            row = conn.execute(
                select(public_chat_sessions).where(
                    and_(
                        public_chat_sessions.c.link_id == link_id,
                        public_chat_sessions.c.session_identifier == session_identifier.lower(),
                    )
                )
            ).mappings().first()
            return self._row_to_session(row) if row else None

    def get_session(self, session_id: str) -> Optional[PublicChatSession]:
        """Get a session by ID."""
        with get_engine().connect() as conn:
            row = conn.execute(
                select(public_chat_sessions).where(public_chat_sessions.c.id == session_id)
            ).mappings().first()
            return self._row_to_session(row) if row else None

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        cost: Optional[float] = None,
        sender_email: Optional[str] = None,
        sender_label: Optional[str] = None,
    ) -> PublicChatMessage:
        """
        Add a message to a session and update session stats.

        Args:
            session_id: The chat session ID
            role: 'user' or 'assistant'
            content: Message content
            cost: Optional cost for assistant messages
            sender_email: Verified email of the speaker (#903) — nullable; drives
                sender-filtered per-user memory summarization. Assistant/DM/web
                messages leave it null.
            sender_label: Display label for attributed history replay (#903) —
                e.g. ``"John Smith (@johndoe)"`` for a channel user, agent name
                for the assistant. Null falls back to a role label.

        Returns:
            PublicChatMessage model
        """
        with get_engine().begin() as conn:
            # Create message
            message_id = secrets.token_urlsafe(16)
            now = utc_now_iso()

            conn.execute(
                insert(public_chat_messages).values(
                    id=message_id,
                    session_id=session_id,
                    role=role,
                    content=content,
                    timestamp=now,
                    cost=cost,
                    sender_email=sender_email,
                    sender_label=sender_label,
                )
            )

            # Update session stats
            conn.execute(
                update(public_chat_sessions)
                .where(public_chat_sessions.c.id == session_id)
                .values(
                    last_message_at=now,
                    message_count=public_chat_sessions.c.message_count + 1,
                    total_cost=public_chat_sessions.c.total_cost + (cost or 0),
                )
            )

            # Return the created message
            row = conn.execute(
                select(public_chat_messages).where(public_chat_messages.c.id == message_id)
            ).mappings().first()
            return self._row_to_message(row)

    def get_session_messages(
        self,
        session_id: str,
        limit: int = 20,
        sender_email: Optional[str] = None,
    ) -> List[PublicChatMessage]:
        """
        Get messages for a session, ordered oldest-first for context building.

        Args:
            session_id: The chat session ID
            limit: Maximum number of messages to return
            sender_email: When set, only that speaker's messages (#903) — used by
                the MEM-001 summarizer so a shared thread never feeds one user's
                turns into another user's memory.

        Returns:
            List of messages, oldest first
        """
        conditions = [public_chat_messages.c.session_id == session_id]
        if sender_email is not None:
            conditions.append(public_chat_messages.c.sender_email == sender_email)
        with get_engine().connect() as conn:
            rows = conn.execute(
                select(public_chat_messages)
                .where(and_(*conditions))
                .order_by(public_chat_messages.c.timestamp.asc())
                .limit(limit)
            ).mappings().all()
            return [self._row_to_message(row) for row in rows]

    def get_recent_messages(
        self,
        session_id: str,
        limit: int = 20,
        sender_email: Optional[str] = None,
    ) -> List[PublicChatMessage]:
        """
        Get the most recent N messages, ordered for display (oldest first).
        Uses subquery to get the most recent, then orders ascending.

        Args:
            sender_email: When set, only that speaker's messages (#903) — the
                MEM-001 summarizer passes the current user's email so a shared
                channel thread never leaks another participant's turns into
                their memory.
        """
        conditions = [public_chat_messages.c.session_id == session_id]
        if sender_email is not None:
            conditions.append(public_chat_messages.c.sender_email == sender_email)
        with get_engine().connect() as conn:
            # Get the most recent N messages, then reverse for chronological order
            recent = (
                select(public_chat_messages)
                .where(and_(*conditions))
                .order_by(public_chat_messages.c.timestamp.desc())
                .limit(limit)
                .subquery()
            )
            rows = conn.execute(
                select(recent).order_by(recent.c.timestamp.asc())
            ).mappings().all()
            return [self._row_to_message(row) for row in rows]

    def clear_session(self, session_id: str) -> bool:
        """
        Delete a session and all its messages (cascade).

        Returns:
            True if session was deleted, False if not found
        """
        with get_engine().begin() as conn:
            # Delete messages first (FK constraint)
            conn.execute(
                delete(public_chat_messages).where(
                    public_chat_messages.c.session_id == session_id
                )
            )

            # Delete session
            result = conn.execute(
                delete(public_chat_sessions).where(
                    public_chat_sessions.c.id == session_id
                )
            )
            deleted = result.rowcount > 0

        return deleted

    def build_context_prompt(
        self,
        session_id: str,
        new_message: str,
        max_turns: int = 10
    ) -> str:
        """
        Build a prompt with conversation history for context injection.

        Args:
            session_id: The chat session ID
            new_message: The new user message to append
            max_turns: Maximum number of previous exchanges (user+assistant pairs)

        Returns:
            Formatted prompt with history and new message, prefixed with public mode header
        """
        # Get recent messages (max_turns * 2 to get pairs)
        messages = self.get_recent_messages(session_id, limit=max_turns * 2)

        # Start with public link mode header
        parts = [PUBLIC_LINK_MODE_HEADER, ""]

        if messages:
            # Add conversation history
            parts.append("Previous conversation:")
            for msg in messages:
                # #903: attribute each turn by its stored sender_label so a
                # multi-participant thread replays as `Alice:` / `Bob:` rather
                # than a flat `User:`. Falls back to the role label for null
                # senders (web/DM turns and pre-migration rows).
                if msg.sender_label:
                    speaker = msg.sender_label
                elif msg.role == "user":
                    speaker = "User"
                else:
                    speaker = "Assistant"
                parts.append(f"{speaker}: {msg.content}")
            parts.append("")

        # Add current message
        parts.append("Current message:")
        parts.append(f"User: {new_message}")

        return "\n".join(parts)

    def delete_link_sessions(self, link_id: str) -> int:
        """
        Delete all sessions for a public link (cascade on link deletion).

        Returns:
            Number of sessions deleted
        """
        with get_engine().begin() as conn:
            # Get session IDs first
            session_ids = [
                row[0]
                for row in conn.execute(
                    select(public_chat_sessions.c.id).where(
                        public_chat_sessions.c.link_id == link_id
                    )
                ).all()
            ]

            # Delete messages for all sessions
            for session_id in session_ids:
                conn.execute(
                    delete(public_chat_messages).where(
                        public_chat_messages.c.session_id == session_id
                    )
                )

            # Delete sessions
            result = conn.execute(
                delete(public_chat_sessions).where(
                    public_chat_sessions.c.link_id == link_id
                )
            )
            deleted = result.rowcount

        return deleted
