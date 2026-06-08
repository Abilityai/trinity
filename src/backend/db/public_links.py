"""
Database operations for public agent links (Phase 12.2).

Handles:
- Public link CRUD operations
- Email verification codes
- Usage tracking
"""

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, List

from db.connection import get_db_connection
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

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO agent_public_links
                (id, agent_name, token, created_by, created_at, expires_at, enabled, name, type)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            """, (link_id, agent_name, token, created_by, now, expires_at, name, link_type))
            conn.commit()

        return self.get_public_link(link_id)

    def get_public_link(self, link_id: str) -> Optional[dict]:
        """Get a public link by ID."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, agent_name, token, created_by, created_at, expires_at,
                       enabled, name, type
                FROM agent_public_links
                WHERE id = ?
            """, (link_id,))
            row = cursor.fetchone()

        if not row:
            return None

        return self._row_to_link(row)

    def get_public_link_by_token(self, token: str) -> Optional[dict]:
        """Get a public link by token."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, agent_name, token, created_by, created_at, expires_at,
                       enabled, name, type
                FROM agent_public_links
                WHERE token = ?
            """, (token,))
            row = cursor.fetchone()

        if not row:
            return None

        return self._row_to_link(row)

    def list_agent_public_links(self, agent_name: str) -> List[dict]:
        """List all public links for an agent."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, agent_name, token, created_by, created_at, expires_at,
                       enabled, name, type
                FROM agent_public_links
                WHERE agent_name = ?
                ORDER BY created_at DESC
            """, (agent_name,))
            rows = cursor.fetchall()

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
        updates = []
        values = []

        if name is not None:
            updates.append("name = ?")
            values.append(name)
        if enabled is not None:
            updates.append("enabled = ?")
            values.append(1 if enabled else 0)
        if expires_at is not None:
            updates.append("expires_at = ?")
            values.append(expires_at)

        if not updates:
            return self.get_public_link(link_id)

        values.append(link_id)

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"""
                UPDATE agent_public_links
                SET {", ".join(updates)}
                WHERE id = ?
            """, values)
            conn.commit()

        return self.get_public_link(link_id)

    def delete_public_link(self, link_id: str) -> bool:
        """Delete a public link."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            # First delete related verifications and usage
            cursor.execute("DELETE FROM public_link_verifications WHERE link_id = ?", (link_id,))
            cursor.execute("DELETE FROM public_link_usage WHERE link_id = ?", (link_id,))
            # Then delete the link
            cursor.execute("DELETE FROM agent_public_links WHERE id = ?", (link_id,))
            deleted = cursor.rowcount > 0
            conn.commit()

        return deleted

    def delete_agent_public_links(self, agent_name: str) -> int:
        """Delete all public links for an agent (cascade on agent deletion)."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            # Get link IDs first
            cursor.execute("SELECT id FROM agent_public_links WHERE agent_name = ?", (agent_name,))
            link_ids = [row[0] for row in cursor.fetchall()]

            # Delete related data
            for link_id in link_ids:
                cursor.execute("DELETE FROM public_link_verifications WHERE link_id = ?", (link_id,))
                cursor.execute("DELETE FROM public_link_usage WHERE link_id = ?", (link_id,))

            # Delete links
            cursor.execute("DELETE FROM agent_public_links WHERE agent_name = ?", (agent_name,))
            deleted = cursor.rowcount
            conn.commit()

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

        with get_db_connection() as conn:
            cursor = conn.cursor()
            # Invalidate any existing pending verifications for this email+link
            cursor.execute("""
                UPDATE public_link_verifications
                SET verified = -1
                WHERE link_id = ? AND email = ? AND verified = 0
            """, (link_id, email.lower()))

            cursor.execute("""
                INSERT INTO public_link_verifications
                (id, link_id, email, code, created_at, expires_at, verified)
                VALUES (?, ?, ?, ?, ?, ?, 0)
            """, (verification_id, link_id, email.lower(), code, now.isoformat(), expires_at))
            conn.commit()

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
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, expires_at, verified
                FROM public_link_verifications
                WHERE link_id = ? AND email = ? AND code = ? AND verified = 0
            """, (link_id, email.lower(), code))
            row = cursor.fetchone()

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
            cursor.execute("""
                UPDATE public_link_verifications
                SET verified = 1, session_token = ?, session_expires_at = ?
                WHERE id = ?
            """, (session_token, session_expires, verification_id))
            conn.commit()

        return True, None, {
            "session_token": session_token,
            "expires_at": session_expires
        }

    def validate_session(self, link_id: str, session_token: str) -> tuple[bool, Optional[str]]:
        """
        Validate a session token.
        Returns: (is_valid, email_if_valid)
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT email, session_expires_at
                FROM public_link_verifications
                WHERE link_id = ? AND session_token = ? AND verified = 1
            """, (link_id, session_token))
            row = cursor.fetchone()

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
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT v.email, v.session_expires_at
                  FROM public_link_verifications v
                  JOIN agent_public_links l ON v.link_id = l.id
                 WHERE l.agent_name = ?
                   AND v.session_token = ?
                   AND v.verified = 1
                """,
                (agent_name, session_token),
            )
            row = cursor.fetchone()

        if not row:
            return False, None

        email, session_expires = row
        if _utcnow() > _parse_aware(session_expires):
            return False, None
        return True, email

    def count_recent_verification_requests(self, email: str, minutes: int = 10) -> int:
        """Count verification requests for an email in the last N minutes."""
        cutoff = (datetime.utcnow() - timedelta(minutes=minutes)).isoformat()

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*)
                FROM public_link_verifications
                WHERE email = ? AND created_at > ?
            """, (email.lower(), cutoff))
            count = cursor.fetchone()[0]

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
        with get_db_connection() as conn:
            cursor = conn.cursor()
            now = utc_now_iso()

            # Check for existing usage record
            cursor.execute("""
                SELECT id, message_count
                FROM public_link_usage
                WHERE link_id = ? AND (email = ? OR (email IS NULL AND ? IS NULL))
                  AND (ip_address = ? OR ip_address IS NULL)
            """, (link_id, email, email, ip_address))
            row = cursor.fetchone()

            if row:
                usage_id, count = row
                cursor.execute("""
                    UPDATE public_link_usage
                    SET message_count = ?, last_used_at = ?
                    WHERE id = ?
                """, (count + 1, now, usage_id))
            else:
                usage_id = secrets.token_urlsafe(16)
                cursor.execute("""
                    INSERT INTO public_link_usage
                    (id, link_id, email, ip_address, message_count, created_at, last_used_at)
                    VALUES (?, ?, ?, ?, 1, ?, ?)
                """, (usage_id, link_id, email, ip_address, now, now))

            conn.commit()

        return {"id": usage_id, "recorded": True}

    def get_link_usage_stats(self, link_id: str) -> dict:
        """Get usage statistics for a public link."""
        with get_db_connection() as conn:
            cursor = conn.cursor()

            # Total messages
            cursor.execute("""
                SELECT COALESCE(SUM(message_count), 0)
                FROM public_link_usage
                WHERE link_id = ?
            """, (link_id,))
            total_messages = cursor.fetchone()[0]

            # Unique users (emails)
            cursor.execute("""
                SELECT COUNT(DISTINCT email)
                FROM public_link_usage
                WHERE link_id = ? AND email IS NOT NULL
            """, (link_id,))
            unique_users = cursor.fetchone()[0]

            # Unique IPs
            cursor.execute("""
                SELECT COUNT(DISTINCT ip_address)
                FROM public_link_usage
                WHERE link_id = ? AND ip_address IS NOT NULL
            """, (link_id,))
            unique_ips = cursor.fetchone()[0]

            # Last used
            cursor.execute("""
                SELECT MAX(last_used_at)
                FROM public_link_usage
                WHERE link_id = ?
            """, (link_id,))
            last_used = cursor.fetchone()[0]

        return {
            "total_messages": total_messages,
            "unique_users": unique_users,
            "unique_ips": unique_ips,
            "last_used_at": last_used
        }

    def count_recent_messages_by_ip(self, ip_address: str, minutes: int = 1) -> int:
        """Count messages from an IP in the last N minutes (for rate limiting)."""
        cutoff = (datetime.utcnow() - timedelta(minutes=minutes)).isoformat()

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COALESCE(SUM(message_count), 0)
                FROM public_link_usage
                WHERE ip_address = ? AND last_used_at > ?
            """, (ip_address, cutoff))
            count = cursor.fetchone()[0]

        return count

    def count_recent_messages_by_token(self, link_id: str, minutes: int = 1) -> int:
        """Count all messages to a public link in the last N minutes (secondary rate limit).

        This caps flood attacks that rotate IPs to bypass the per-IP limit.
        """
        cutoff = (datetime.utcnow() - timedelta(minutes=minutes)).isoformat()

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COALESCE(SUM(message_count), 0)
                FROM public_link_usage
                WHERE link_id = ? AND last_used_at > ?
            """, (link_id, cutoff))
            count = cursor.fetchone()[0]

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

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, agent_name, user_email, memory_text, message_count, created_at, updated_at
                FROM public_user_memory
                WHERE agent_name = ? AND user_email = ?
            """, (agent_name, email))
            row = cursor.fetchone()

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
            cursor.execute("""
                INSERT INTO public_user_memory
                (id, agent_name, user_email, memory_text, message_count, created_at, updated_at)
                VALUES (?, ?, ?, '', 0, ?, ?)
            """, (memory_id, agent_name, email, now, now))
            conn.commit()

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

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE public_user_memory
                SET message_count = message_count + 1, updated_at = ?
                WHERE agent_name = ? AND user_email = ?
            """, (now, agent_name, email))
            conn.commit()

            cursor.execute("""
                SELECT message_count FROM public_user_memory
                WHERE agent_name = ? AND user_email = ?
            """, (agent_name, email))
            row = cursor.fetchone()

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

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT memory_text FROM public_user_memory
                WHERE agent_name = ? AND user_email = ?
            """, (agent_name, email))
            row = cursor.fetchone()

            if row is None:
                memory_id = secrets.token_urlsafe(16)
                cursor.execute("""
                    INSERT INTO public_user_memory
                    (id, agent_name, user_email, memory_text, message_count, created_at, updated_at)
                    VALUES (?, ?, ?, '', 0, ?, ?)
                """, (memory_id, agent_name, email, now, now))
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

            cursor.execute("""
                UPDATE public_user_memory
                SET memory_text = ?, updated_at = ?
                WHERE agent_name = ? AND user_email = ?
            """, (new_blob, now, agent_name, email))
            conn.commit()

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
