"""
Chat session and message persistence database operations.

Handles chat session management, message storage, and history retrieval.

Converted from raw sqlite3 to SQLAlchemy Core (#300) so it runs unchanged on
both SQLite and PostgreSQL. Queries are built from the ``chat_sessions`` and
``chat_messages`` tables in ``db/tables.py`` (dialect-agnostic expressions, no
``?`` placeholders), and the engine is resolved via ``db/engine.py``.
"""

import secrets
from datetime import datetime
from typing import Optional, List

from sqlalchemy import select, insert, update, delete, and_

from .engine import get_engine
from .tables import chat_sessions, chat_messages
from .write_params import ChatMessageFields
from db_models import ChatSession, ChatMessage
from utils.helpers import utc_now_iso


class ChatOperations:
    """Chat session and message database operations."""

    @staticmethod
    def _row_to_chat_session(row) -> ChatSession:
        """Convert a chat_sessions row to a ChatSession model."""
        keys = row.keys()
        return ChatSession(
            id=row["id"],
            agent_name=row["agent_name"],
            user_id=row["user_id"],
            user_email=row["user_email"],
            started_at=datetime.fromisoformat(row["started_at"]),
            last_message_at=datetime.fromisoformat(row["last_message_at"]),
            message_count=row["message_count"],
            total_cost=row["total_cost"],
            total_context_used=row["total_context_used"],
            total_context_max=row["total_context_max"],
            status=row["status"],
            subscription_id=row["subscription_id"] if "subscription_id" in keys else None,
        )

    @staticmethod
    def _row_to_chat_message(row) -> ChatMessage:
        """Convert a chat_messages row to a ChatMessage model."""
        keys = row.keys()
        return ChatMessage(
            id=row["id"],
            session_id=row["session_id"],
            agent_name=row["agent_name"],
            user_id=row["user_id"],
            user_email=row["user_email"],
            role=row["role"],
            content=row["content"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            cost=row["cost"],
            context_used=row["context_used"],
            context_max=row["context_max"],
            tool_calls=row["tool_calls"],
            execution_time_ms=row["execution_time_ms"],
            source=row["source"] if "source" in keys else "text",
            subscription_id=row["subscription_id"] if "subscription_id" in keys else None,
            output_tokens=row["output_tokens"] if "output_tokens" in keys else None,
        )

    def get_or_create_chat_session(
        self,
        agent_name: str,
        user_id: int,
        user_email: str,
        subscription_id: Optional[str] = None,
    ) -> ChatSession:
        """
        Get the active chat session for a user+agent, or create a new one.
        Returns the most recent active session if it exists.
        """
        with get_engine().begin() as conn:
            # Try to find an active session for this user+agent
            row = conn.execute(
                select(chat_sessions)
                .where(
                    and_(
                        chat_sessions.c.agent_name == agent_name,
                        chat_sessions.c.user_id == user_id,
                        chat_sessions.c.status == "active",
                    )
                )
                .order_by(chat_sessions.c.last_message_at.desc())
                .limit(1)
            ).mappings().first()
            if row:
                return self._row_to_chat_session(row)

            # Create a new session
            session_id = secrets.token_urlsafe(16)
            now = utc_now_iso()

            conn.execute(
                insert(chat_sessions).values(
                    id=session_id,
                    agent_name=agent_name,
                    user_id=user_id,
                    user_email=user_email,
                    started_at=now,
                    last_message_at=now,
                    subscription_id=subscription_id,
                )
            )

            # Return the newly created session
            row = conn.execute(
                select(chat_sessions).where(chat_sessions.c.id == session_id)
            ).mappings().first()
            return self._row_to_chat_session(row)

    def add_chat_message(
        self,
        session_id: str,
        agent_name: str,
        user_id: int,
        user_email: str,
        role: str,
        content: str,
        fields: Optional[ChatMessageFields] = None,
    ) -> ChatMessage:
        """Add a message to a chat session and update session stats.

        `fields` carries every optional column (#1482); None is a plain text
        message with no cost/context/timing recorded.
        """
        f = fields or ChatMessageFields()
        cost, context_used, context_max = f.cost, f.context_used, f.context_max
        with get_engine().begin() as conn:
            # Create message
            message_id = secrets.token_urlsafe(16)
            now = utc_now_iso()

            conn.execute(
                insert(chat_messages).values(
                    id=message_id,
                    session_id=session_id,
                    agent_name=agent_name,
                    user_id=user_id,
                    user_email=user_email,
                    role=role,
                    content=content,
                    timestamp=now,
                    cost=cost,
                    context_used=context_used,
                    context_max=context_max,
                    tool_calls=f.tool_calls,
                    execution_time_ms=f.execution_time_ms,
                    source=f.source or "text",
                    subscription_id=f.subscription_id,
                    output_tokens=f.output_tokens,
                )
            )

            # Update session stats
            conn.execute(
                update(chat_sessions)
                .where(chat_sessions.c.id == session_id)
                .values(
                    last_message_at=now,
                    message_count=chat_sessions.c.message_count + 1,
                    total_cost=chat_sessions.c.total_cost + (cost or 0),
                    total_context_used=context_used
                    if context_used is not None
                    else chat_sessions.c.total_context_used,
                    total_context_max=context_max
                    if context_max is not None
                    else chat_sessions.c.total_context_max,
                )
            )

            # Return the created message
            row = conn.execute(
                select(chat_messages).where(chat_messages.c.id == message_id)
            ).mappings().first()
            return self._row_to_chat_message(row)

    def get_chat_session(self, session_id: str) -> Optional[ChatSession]:
        """Get a specific chat session by ID."""
        with get_engine().connect() as conn:
            row = conn.execute(
                select(chat_sessions).where(chat_sessions.c.id == session_id)
            ).mappings().first()
            return self._row_to_chat_session(row) if row else None

    def get_chat_messages(self, session_id: str, limit: int = 100) -> List[ChatMessage]:
        """Get messages for a chat session (oldest first for display order)."""
        with get_engine().connect() as conn:
            # Inner query: newest `limit` messages; outer: re-sort oldest-first.
            inner = (
                select(chat_messages)
                .where(chat_messages.c.session_id == session_id)
                .order_by(chat_messages.c.timestamp.desc())
                .limit(limit)
                .subquery()
            )
            stmt = select(inner).order_by(inner.c.timestamp.asc())
            rows = conn.execute(stmt).mappings().all()
            return [self._row_to_chat_message(row) for row in rows]

    def get_agent_chat_history(
        self,
        agent_name: str,
        user_id: Optional[int] = None,
        limit: int = 100
    ) -> List[ChatMessage]:
        """
        Get chat history for an agent.
        If user_id is provided, filter to that user's messages.
        Returns messages across all sessions, newest first.
        """
        conds = [chat_messages.c.agent_name == agent_name]
        if user_id:
            conds.append(chat_messages.c.user_id == user_id)
        stmt = (
            select(chat_messages)
            .where(and_(*conds))
            .order_by(chat_messages.c.timestamp.desc())
            .limit(limit)
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()
            return [self._row_to_chat_message(row) for row in rows]

    def get_agent_chat_sessions(
        self,
        agent_name: str,
        user_id: Optional[int] = None,
        status: Optional[str] = None
    ) -> List[ChatSession]:
        """
        Get all chat sessions for an agent.
        Optionally filter by user_id and/or status.
        """
        conds = [chat_sessions.c.agent_name == agent_name]
        if user_id:
            conds.append(chat_sessions.c.user_id == user_id)
        if status:
            conds.append(chat_sessions.c.status == status)
        stmt = (
            select(chat_sessions)
            .where(and_(*conds))
            .order_by(chat_sessions.c.last_message_at.desc())
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()
            return [self._row_to_chat_session(row) for row in rows]

    def close_chat_session(self, session_id: str) -> bool:
        """Mark a chat session as closed."""
        with get_engine().begin() as conn:
            result = conn.execute(
                update(chat_sessions)
                .where(chat_sessions.c.id == session_id)
                .values(status="closed")
            )
            return result.rowcount > 0

    def create_new_chat_session(
        self,
        agent_name: str,
        user_id: int,
        user_email: str,
        subscription_id: Optional[str] = None,
    ) -> ChatSession:
        """
        Create a new chat session, closing any existing active sessions for this user+agent.
        Use this when user explicitly wants a new conversation (e.g., "New Chat" button).
        """
        with get_engine().begin() as conn:
            # Close any existing active sessions for this user+agent
            conn.execute(
                update(chat_sessions)
                .where(
                    and_(
                        chat_sessions.c.agent_name == agent_name,
                        chat_sessions.c.user_id == user_id,
                        chat_sessions.c.status == "active",
                    )
                )
                .values(status="closed")
            )

            # Create a new session
            session_id = secrets.token_urlsafe(16)
            now = utc_now_iso()

            conn.execute(
                insert(chat_sessions).values(
                    id=session_id,
                    agent_name=agent_name,
                    user_id=user_id,
                    user_email=user_email,
                    started_at=now,
                    last_message_at=now,
                    subscription_id=subscription_id,
                )
            )

            # Return the newly created session
            row = conn.execute(
                select(chat_sessions).where(chat_sessions.c.id == session_id)
            ).mappings().first()
            return self._row_to_chat_session(row)

    def delete_chat_session(self, session_id: str) -> bool:
        """Delete a chat session and all its messages."""
        with get_engine().begin() as conn:
            # Delete messages first (foreign key constraint)
            conn.execute(
                delete(chat_messages).where(chat_messages.c.session_id == session_id)
            )

            # Delete session
            result = conn.execute(
                delete(chat_sessions).where(chat_sessions.c.id == session_id)
            )

            return result.rowcount > 0
