"""
Agent session and session-message persistence (Session tab).

Parallels db/chat.py but adds Claude Code --resume UUID caching and per-message
audit fields (cache_read_tokens, claude_session_id). See
docs/planning/SESSION_TAB_2026-04.md for the design.
"""

import secrets
from datetime import datetime
from typing import Optional, List

from .connection import get_db_connection
from db_models import AgentSession, AgentSessionMessage
from utils.helpers import utc_now_iso


def _parse_dt(value) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.fromisoformat(value)


class SessionOperations:
    """Agent session and session-message database operations."""

    @staticmethod
    def _row_to_session(row) -> AgentSession:
        row_keys = row.keys()
        return AgentSession(
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
            subscription_id=row["subscription_id"],
            cached_claude_session_id=row["cached_claude_session_id"],
            last_resume_at=_parse_dt(row["last_resume_at"]),
            consecutive_resume_failures=row["consecutive_resume_failures"],
            compact_count=(row["compact_count"] or 0) if "compact_count" in row_keys else 0,
        )

    @staticmethod
    def _row_to_message(row) -> AgentSessionMessage:
        row_keys = row.keys()
        return AgentSessionMessage(
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
            cache_read_tokens=row["cache_read_tokens"],
            tool_calls=row["tool_calls"],
            execution_time_ms=row["execution_time_ms"],
            claude_session_id=row["claude_session_id"],
            compact_metadata=row["compact_metadata"] if "compact_metadata" in row_keys else None,
        )

    # ---- session lifecycle -------------------------------------------------

    def create_session(
        self,
        agent_name: str,
        user_id: int,
        user_email: str,
        subscription_id: Optional[str] = None,
    ) -> AgentSession:
        """Create a new agent_sessions row with empty cached_claude_session_id."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            session_id = secrets.token_urlsafe(16)
            now = utc_now_iso()

            cursor.execute("""
                INSERT INTO agent_sessions (
                    id, agent_name, user_id, user_email,
                    started_at, last_message_at, subscription_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (session_id, agent_name, user_id, user_email, now, now, subscription_id))

            cursor.execute("SELECT * FROM agent_sessions WHERE id = ?", (session_id,))
            return self._row_to_session(cursor.fetchone())

    def get_session(self, session_id: str) -> Optional[AgentSession]:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM agent_sessions WHERE id = ?", (session_id,))
            row = cursor.fetchone()
            return self._row_to_session(row) if row else None

    def list_sessions(
        self,
        agent_name: str,
        user_id: Optional[int] = None,
        status: Optional[str] = None,
    ) -> List[AgentSession]:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            query = "SELECT * FROM agent_sessions WHERE agent_name = ?"
            params: list = [agent_name]
            if user_id is not None:
                query += " AND user_id = ?"
                params.append(user_id)
            if status:
                query += " AND status = ?"
                params.append(status)
            query += " ORDER BY last_message_at DESC"
            cursor.execute(query, params)
            return [self._row_to_session(r) for r in cursor.fetchall()]

    def delete_session(self, session_id: str) -> bool:
        """Delete the session and all its messages.

        agent_session_messages has ON DELETE CASCADE, but the platform doesn't
        enable PRAGMA foreign_keys, so we delete messages explicitly.
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM agent_session_messages WHERE session_id = ?", (session_id,))
            cursor.execute("DELETE FROM agent_sessions WHERE id = ?", (session_id,))
            return cursor.rowcount > 0

    # ---- messages ----------------------------------------------------------

    def add_session_message(
        self,
        session_id: str,
        agent_name: str,
        user_id: int,
        user_email: str,
        role: str,
        content: str,
        cost: Optional[float] = None,
        context_used: Optional[int] = None,
        context_max: Optional[int] = None,
        cache_read_tokens: Optional[int] = None,
        tool_calls: Optional[str] = None,
        execution_time_ms: Optional[int] = None,
        claude_session_id: Optional[str] = None,
        compact_metadata: Optional[str] = None,
        compact_event_count: int = 0,
    ) -> AgentSessionMessage:
        """Insert a session message and update session aggregate stats.

        ``compact_metadata`` is a JSON-encoded list of CompactEvent dicts (the
        agent server's stream parser captures them from
        ``{"type":"system","subtype":"compact_boundary"}`` events). The matching
        ``compact_event_count`` bumps the session's running ``compact_count``
        tally so the frontend can drive the inline reset-memory hint without
        scanning per-message rows.
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            message_id = secrets.token_urlsafe(16)
            now = utc_now_iso()

            cursor.execute("""
                INSERT INTO agent_session_messages (
                    id, session_id, agent_name, user_id, user_email,
                    role, content, timestamp,
                    cost, context_used, context_max, cache_read_tokens,
                    tool_calls, execution_time_ms, claude_session_id,
                    compact_metadata
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                message_id, session_id, agent_name, user_id, user_email,
                role, content, now,
                cost, context_used, context_max, cache_read_tokens,
                tool_calls, execution_time_ms, claude_session_id,
                compact_metadata,
            ))

            # total_context_used reflects the most recent assistant turn's
            # cache size, capped at total_context_max. Claude Code's auto-
            # compact (~85% of the model window) silently resets the cache
            # mid-turn, so a watermark would asymptote near the compact
            # threshold and stop conveying anything useful — the value
            # users care about is "what did the last turn cost," which
            # bounces honestly between low (post-compact rebuild) and high
            # (heavy turn that didn't compact).
            cursor.execute("""
                UPDATE agent_sessions
                SET last_message_at = ?,
                    message_count = message_count + 1,
                    total_cost = total_cost + COALESCE(?, 0),
                    total_context_used = MIN(
                        COALESCE(?, total_context_used),
                        total_context_max
                    ),
                    total_context_max = COALESCE(?, total_context_max),
                    compact_count = compact_count + ?
                WHERE id = ?
            """, (now, cost or 0, context_used, context_max, compact_event_count, session_id))

            cursor.execute("SELECT * FROM agent_session_messages WHERE id = ?", (message_id,))
            return self._row_to_message(cursor.fetchone())

    def get_session_messages(
        self, session_id: str, limit: int = 100
    ) -> List[AgentSessionMessage]:
        """Return the most recent ``limit`` messages, oldest-first for display."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM (
                    SELECT * FROM agent_session_messages
                    WHERE session_id = ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                ) sub ORDER BY timestamp ASC
            """, (session_id, limit))
            return [self._row_to_message(r) for r in cursor.fetchall()]

    # ---- Claude session UUID cache ----------------------------------------

    def get_cached_claude_session_id(self, session_id: str) -> Optional[str]:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT cached_claude_session_id FROM agent_sessions WHERE id = ?",
                (session_id,),
            )
            row = cursor.fetchone()
            return row["cached_claude_session_id"] if row else None

    def update_cached_claude_session_id(
        self, session_id: str, claude_session_id: str
    ) -> bool:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE agent_sessions
                SET cached_claude_session_id = ?
                WHERE id = ?
            """, (claude_session_id, session_id))
            return cursor.rowcount > 0

    def clear_cached_claude_session_id(self, session_id: str) -> bool:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE agent_sessions
                SET cached_claude_session_id = NULL
                WHERE id = ?
            """, (session_id,))
            return cursor.rowcount > 0

    # ---- resume health -----------------------------------------------------

    def mark_resume_failure(self, session_id: str) -> int:
        """Increment consecutive_resume_failures and return the new count."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE agent_sessions
                SET consecutive_resume_failures = consecutive_resume_failures + 1
                WHERE id = ?
            """, (session_id,))
            cursor.execute(
                "SELECT consecutive_resume_failures FROM agent_sessions WHERE id = ?",
                (session_id,),
            )
            row = cursor.fetchone()
            return row["consecutive_resume_failures"] if row else 0

    def mark_resume_success(self, session_id: str) -> bool:
        """Reset failure counter and stamp last_resume_at."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE agent_sessions
                SET consecutive_resume_failures = 0,
                    last_resume_at = ?
                WHERE id = ?
            """, (utc_now_iso(), session_id))
            return cursor.rowcount > 0

    # ---- cleanup support (Phase 4.2) --------------------------------------

    def list_active_claude_session_ids(self, agent_name: str) -> List[str]:
        """All currently-cached Claude UUIDs for an agent — the keep set.

        The Phase 4.2 cleanup service uses this as the allowlist when reaping
        orphan JSONLs from ``~/.claude/projects/-home-developer/`` inside the
        agent container. A JSONL whose UUID is not in this set has no session
        row referencing it (deleted, reset, or post-fallback orphan) and is
        safe to delete after the age-guard window.
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT cached_claude_session_id
                FROM agent_sessions
                WHERE agent_name = ?
                  AND cached_claude_session_id IS NOT NULL
            """, (agent_name,))
            return [row["cached_claude_session_id"] for row in cursor.fetchall()]
