"""
Operator queue database operations (OPS-001).

Persists operator queue items synced from agent JSON files.
Supports listing, filtering, responding, and statistics.
"""

import json
from typing import Any, Optional, List, Dict, Set
from datetime import datetime

from .connection import get_db_connection
from utils.helpers import utc_now_iso


class OperatorQueueOperations:
    """Database operations for the operator queue."""

    @staticmethod
    def _row_to_item(row) -> Dict:
        """Convert a database row to a queue item dict."""
        return {
            "id": row[0],
            "agent_name": row[1],
            "type": row[2],
            "status": row[3],
            "priority": row[4],
            "title": row[5],
            "question": row[6],
            "options": json.loads(row[7]) if row[7] else None,
            "context": json.loads(row[8]) if row[8] else None,
            "execution_id": row[9],
            "created_at": row[10],
            "expires_at": row[11],
            "response": row[12],
            "response_text": row[13],
            "responded_by_id": row[14],
            "responded_by_email": row[15],
            "responded_at": row[16],
            "acknowledged_at": row[17],
        }

    _SELECT_COLS = """
        id, agent_name, type, status, priority, title, question,
        options, context, execution_id, created_at, expires_at,
        response, response_text, responded_by_id, responded_by_email,
        responded_at, acknowledged_at
    """

    def create_item(self, agent_name: str, item: Dict) -> str:
        """Create a queue item from agent JSON data.

        Args:
            agent_name: The agent that created this item
            item: Queue item data from agent's operator-queue.json

        Returns:
            The item ID
        """
        item_id = item["id"]
        options_json = json.dumps(item.get("options")) if item.get("options") else None
        context_json = json.dumps(item.get("context")) if item.get("context") else None

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR IGNORE INTO operator_queue (
                    id, agent_name, type, status, priority, title, question,
                    options, context, execution_id, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                item_id,
                agent_name,
                item.get("type", "question"),
                item.get("status", "pending"),
                item.get("priority", "medium"),
                item["title"],
                item["question"],
                options_json,
                context_json,
                item.get("context", {}).get("execution_id") if item.get("context") else None,
                item["created_at"],
                item.get("expires_at"),
            ))
            conn.commit()
            return item_id

    def get_item(self, item_id: str) -> Optional[Dict]:
        """Get a single queue item by ID."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"""
                SELECT {self._SELECT_COLS}
                FROM operator_queue WHERE id = ?
            """, (item_id,))
            row = cursor.fetchone()

        if not row:
            return None
        return self._row_to_item(row)

    def list_items(
        self,
        status: Optional[str] = None,
        type: Optional[str] = None,
        priority: Optional[str] = None,
        agent_name: Optional[str] = None,
        since: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        accessible_agent_names: Optional[Set[str]] = None,
    ) -> List[Dict]:
        """List queue items with optional filters.

        accessible_agent_names: if None, no access filter (admin). If a set,
        only items whose agent_name is in the set are returned. Empty set
        short-circuits to [] (user has no accessible agents).
        """
        if accessible_agent_names is not None and len(accessible_agent_names) == 0:
            return []

        query = f"SELECT {self._SELECT_COLS} FROM operator_queue WHERE 1=1"
        params = []

        if accessible_agent_names is not None:
            placeholders = ",".join(["?"] * len(accessible_agent_names))
            query += f" AND agent_name IN ({placeholders})"
            params.extend(sorted(accessible_agent_names))

        if status:
            query += " AND status = ?"
            params.append(status)
        if type:
            query += " AND type = ?"
            params.append(type)
        if priority:
            query += " AND priority = ?"
            params.append(priority)
        if agent_name:
            query += " AND agent_name = ?"
            params.append(agent_name)
        if since:
            query += " AND created_at >= ?"
            params.append(since)

        # Sort: pending items by priority then age, others by created_at desc
        query += """
            ORDER BY
                CASE status WHEN 'pending' THEN 0 ELSE 1 END,
                CASE priority
                    WHEN 'critical' THEN 0
                    WHEN 'high' THEN 1
                    WHEN 'medium' THEN 2
                    WHEN 'low' THEN 3
                    ELSE 4
                END,
                created_at DESC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            rows = cursor.fetchall()

        return [self._row_to_item(row) for row in rows]

    def respond_to_item(
        self,
        item_id: str,
        response: str,
        response_text: Optional[str],
        responded_by_id: str,
        responded_by_email: str,
    ) -> Optional[Dict]:
        """Record an operator response to a queue item.

        Returns the updated item or None if not found.
        """
        now = utc_now_iso()

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE operator_queue
                SET status = 'responded',
                    response = ?,
                    response_text = ?,
                    responded_by_id = ?,
                    responded_by_email = ?,
                    responded_at = ?
                WHERE id = ? AND status = 'pending'
            """, (response, response_text, responded_by_id, responded_by_email, now, item_id))
            conn.commit()

            if cursor.rowcount == 0:
                # Check if item exists at all
                cursor.execute("SELECT id, status FROM operator_queue WHERE id = ?", (item_id,))
                row = cursor.fetchone()
                if not row:
                    return None
                # Item exists but not pending — return it anyway
                return self.get_item(item_id)

        return self.get_item(item_id)

    def cancel_item(self, item_id: str) -> Optional[Dict]:
        """Cancel a pending queue item."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE operator_queue
                SET status = 'cancelled'
                WHERE id = ? AND status = 'pending'
            """, (item_id,))
            conn.commit()

            if cursor.rowcount == 0:
                cursor.execute("SELECT id FROM operator_queue WHERE id = ?", (item_id,))
                if not cursor.fetchone():
                    return None

        return self.get_item(item_id)

    def mark_pending_items_recovered(
        self,
        agent_name: str,
        item_type: str,
        recovery_evidence: Dict[str, Any],
    ) -> List[Dict]:
        """Mark pending items recovered without deleting operator history.

        This is intentionally narrow: callers must provide both an agent and a
        queue type so human-action-required items are not swept up by a broad
        recovery pass.
        """
        now = utc_now_iso()
        recovered_ids: List[str] = []

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, context, priority
                FROM operator_queue
                WHERE agent_name = ?
                  AND type = ?
                  AND status = 'pending'
            """, (agent_name, item_type))
            rows = cursor.fetchall()

            for row in rows:
                context = self._parse_context(row[1])
                context["resolution_state"] = "recovered_unacknowledged"
                context["recovered_at"] = now
                context["recovered_by"] = "system"
                context["previous_priority"] = row[2]
                context["recovery_evidence"] = recovery_evidence

                cursor.execute("""
                    UPDATE operator_queue
                    SET status = 'recovered',
                        priority = 'low',
                        context = ?
                    WHERE id = ?
                      AND status = 'pending'
                """, (json.dumps(context), row[0]))
                if cursor.rowcount:
                    recovered_ids.append(row[0])

            conn.commit()

        return [
            item for item_id in recovered_ids
            if (item := self.get_item(item_id)) is not None
        ]

    def mark_acknowledged(self, item_id: str) -> bool:
        """Mark an item as acknowledged by the agent."""
        now = utc_now_iso()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE operator_queue
                SET status = 'acknowledged', acknowledged_at = ?
                WHERE id = ? AND status = 'responded'
            """, (now, item_id))
            conn.commit()
            return cursor.rowcount > 0

    def mark_expired(self) -> int:
        """Mark pending items past their expires_at as expired.

        Returns number of items expired.
        """
        now = utc_now_iso()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE operator_queue
                SET status = 'expired'
                WHERE status = 'pending'
                  AND expires_at IS NOT NULL
                  AND expires_at < ?
            """, (now,))
            conn.commit()
            return cursor.rowcount

    def get_stats(self, accessible_agent_names: Optional[Set[str]] = None) -> Dict:
        """Get queue statistics.

        accessible_agent_names: if None, no access filter (admin). If a set,
        only items for accessible agents are counted. Empty set returns zeros.
        """
        if accessible_agent_names is not None and len(accessible_agent_names) == 0:
            return {
                "by_status": {},
                "by_type": {},
                "by_priority": {},
                "by_agent": {},
                "pending_count": 0,
                "avg_response_seconds": None,
                "responded_today": 0,
            }

        # Build access filter fragment applied to every sub-query
        access_filter = ""
        access_params: List = []
        if accessible_agent_names is not None:
            placeholders = ",".join(["?"] * len(accessible_agent_names))
            access_filter = f" AND agent_name IN ({placeholders})"
            access_params = sorted(accessible_agent_names)

        with get_db_connection() as conn:
            cursor = conn.cursor()

            # Counts by status
            cursor.execute(
                f"SELECT status, COUNT(*) FROM operator_queue WHERE 1=1{access_filter} GROUP BY status",
                access_params,
            )
            by_status = {row[0]: row[1] for row in cursor.fetchall()}

            # Counts by type (pending only)
            cursor.execute(
                f"SELECT type, COUNT(*) FROM operator_queue WHERE status = 'pending'{access_filter} GROUP BY type",
                access_params,
            )
            by_type = {row[0]: row[1] for row in cursor.fetchall()}

            # Counts by priority (pending only)
            cursor.execute(
                f"SELECT priority, COUNT(*) FROM operator_queue WHERE status = 'pending'{access_filter} GROUP BY priority",
                access_params,
            )
            by_priority = {row[0]: row[1] for row in cursor.fetchall()}

            # Counts by agent (pending only)
            cursor.execute(
                f"SELECT agent_name, COUNT(*) FROM operator_queue WHERE status = 'pending'{access_filter} GROUP BY agent_name",
                access_params,
            )
            by_agent = {row[0]: row[1] for row in cursor.fetchall()}

            # Average response time (for responded items)
            cursor.execute(
                f"""SELECT AVG(
                    (julianday(responded_at) - julianday(created_at)) * 86400
                ) FROM operator_queue
                WHERE responded_at IS NOT NULL{access_filter}""",
                access_params,
            )
            avg_row = cursor.fetchone()
            avg_response_seconds = round(avg_row[0], 1) if avg_row[0] else None

            # Items responded today
            today = datetime.utcnow().strftime("%Y-%m-%d")
            cursor.execute(
                f"""SELECT COUNT(*) FROM operator_queue
                WHERE responded_at IS NOT NULL
                  AND responded_at >= ?{access_filter}""",
                [today] + access_params,
            )
            responded_today = cursor.fetchone()[0]

        return {
            "by_status": by_status,
            "by_type": by_type,
            "by_priority": by_priority,
            "by_agent": by_agent,
            "pending_count": by_status.get("pending", 0),
            "avg_response_seconds": avg_response_seconds,
            "responded_today": responded_today,
        }

    def get_pending_item_ids(self) -> List[str]:
        """Get IDs of all pending items (for sync service to check)."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM operator_queue WHERE status = 'pending'")
            return [row[0] for row in cursor.fetchall()]

    def get_responded_items_for_agent(self, agent_name: str) -> List[Dict]:
        """Get responded (not yet acknowledged) items for a specific agent.

        Used by sync service to write responses back to agent files.
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"""
                SELECT {self._SELECT_COLS}
                FROM operator_queue
                WHERE agent_name = ? AND status = 'responded'
            """, (agent_name,))
            return [self._row_to_item(row) for row in cursor.fetchall()]

    def item_exists(self, item_id: str) -> bool:
        """Check if an item exists in the database."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM operator_queue WHERE id = ?", (item_id,))
            return cursor.fetchone() is not None

    @staticmethod
    def _parse_context(raw_context) -> Dict:
        """Parse queue context defensively for recovery annotation."""
        if not raw_context:
            return {}
        try:
            parsed = json.loads(raw_context)
        except (TypeError, json.JSONDecodeError):
            return {"previous_context": raw_context}
        if isinstance(parsed, dict):
            return parsed
        return {"previous_context": parsed}
