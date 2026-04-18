"""
Sync state DB operations (Issue #389 — S1).

One row per agent captures last sync outcome, consecutive_failures counter,
remote SHAs, and ahead/behind tuples. The SyncHealthService upserts here
after polling each agent; routers read here to render the dashboard dot.
"""

from typing import Dict, List, Optional

from utils.helpers import utc_now_iso

from .connection import get_db_connection

# Keep in sync with agent_sync_state column order so row indexes match.
_COLUMNS = (
    "agent_name",
    "last_sync_at",
    "last_sync_status",
    "consecutive_failures",
    "last_error_summary",
    "last_remote_sha_main",
    "last_remote_sha_working",
    "ahead_main",
    "behind_main",
    "ahead_working",
    "behind_working",
    "last_check_at",
    "updated_at",
)


def _row_to_dict(row) -> Dict:
    return {col: row[col] for col in _COLUMNS}


class SyncStateOperations:
    """CRUD for agent_sync_state (#389)."""

    def get(self, agent_name: str) -> Optional[Dict]:
        with get_db_connection() as conn:
            row = conn.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM agent_sync_state WHERE agent_name = ?",
                (agent_name,),
            ).fetchone()
        return _row_to_dict(row) if row else None

    def list_all(self) -> List[Dict]:
        with get_db_connection() as conn:
            rows = conn.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM agent_sync_state"
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def upsert(
        self,
        agent_name: str,
        last_sync_at: Optional[str] = None,
        last_sync_status: Optional[str] = None,
        last_error_summary: Optional[str] = None,
        last_remote_sha_main: Optional[str] = None,
        last_remote_sha_working: Optional[str] = None,
        ahead_main: Optional[int] = None,
        behind_main: Optional[int] = None,
        ahead_working: Optional[int] = None,
        behind_working: Optional[int] = None,
        last_check_at: Optional[str] = None,
    ) -> Dict:
        """Upsert a sync-state row.

        consecutive_failures is maintained internally: incremented on
        `failed`, reset on `success`, untouched on `never`.
        """
        now = utc_now_iso()
        existing = self.get(agent_name)

        if last_sync_status == "failed":
            consecutive_failures = (existing["consecutive_failures"] if existing else 0) + 1
        elif last_sync_status == "success":
            consecutive_failures = 0
        else:  # 'never' or None — keep prior counter
            consecutive_failures = existing["consecutive_failures"] if existing else 0

        # For partial updates, fall back to prior values for fields not passed.
        def _merged(field: str, new_value):
            if new_value is not None:
                return new_value
            return existing.get(field) if existing else None

        row = {
            "agent_name": agent_name,
            "last_sync_at": _merged("last_sync_at", last_sync_at),
            "last_sync_status": _merged("last_sync_status", last_sync_status),
            "consecutive_failures": consecutive_failures,
            "last_error_summary": last_error_summary
                if last_sync_status == "failed"
                else (None if last_sync_status == "success" else _merged(
                    "last_error_summary", last_error_summary)),
            "last_remote_sha_main": _merged("last_remote_sha_main", last_remote_sha_main),
            "last_remote_sha_working": _merged("last_remote_sha_working", last_remote_sha_working),
            "ahead_main": _merged("ahead_main", ahead_main) or 0,
            "behind_main": _merged("behind_main", behind_main) or 0,
            "ahead_working": _merged("ahead_working", ahead_working) or 0,
            "behind_working": _merged("behind_working", behind_working) or 0,
            "last_check_at": last_check_at or now,
            "updated_at": now,
        }

        with get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO agent_sync_state (
                    agent_name, last_sync_at, last_sync_status, consecutive_failures,
                    last_error_summary, last_remote_sha_main, last_remote_sha_working,
                    ahead_main, behind_main, ahead_working, behind_working,
                    last_check_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_name) DO UPDATE SET
                    last_sync_at = excluded.last_sync_at,
                    last_sync_status = excluded.last_sync_status,
                    consecutive_failures = excluded.consecutive_failures,
                    last_error_summary = excluded.last_error_summary,
                    last_remote_sha_main = excluded.last_remote_sha_main,
                    last_remote_sha_working = excluded.last_remote_sha_working,
                    ahead_main = excluded.ahead_main,
                    behind_main = excluded.behind_main,
                    ahead_working = excluded.ahead_working,
                    behind_working = excluded.behind_working,
                    last_check_at = excluded.last_check_at,
                    updated_at = excluded.updated_at
                """,
                tuple(row[c] for c in _COLUMNS),
            )
        return row

    def delete(self, agent_name: str) -> bool:
        with get_db_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM agent_sync_state WHERE agent_name = ?", (agent_name,)
            )
            return cursor.rowcount > 0
