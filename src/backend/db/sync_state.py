"""
Sync state DB operations (Issue #389 — S1).

One row per agent captures last sync outcome, consecutive_failures counter,
remote SHAs, and ahead/behind tuples. The SyncHealthService upserts here
after polling each agent; routers read here to render the dashboard dot.

Converted from raw sqlite3 to SQLAlchemy Core (#300) so it runs unchanged on
both SQLite and PostgreSQL.
"""

from typing import Dict, List, Optional

from sqlalchemy import select, delete

from utils.helpers import utc_now_iso

from .engine import get_engine, make_insert
from .tables import agent_sync_state

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
    "git_dir_bytes",  # #1596: agent .git on-disk size (bloat observability)
    "pack_count",  # #1595: packs from `git count-objects -v`
    "loose_objects",  # #1595: loose objects (gc-health signal)
    "maintenance_failures",  # #1595: consecutive failed maintenance attempts
    "last_check_at",
    "updated_at",
)

# Non-key columns updated on conflict (everything except the agent_name PK).
_UPSERT_SET_COLUMNS = tuple(c for c in _COLUMNS if c != "agent_name")


def _row_to_dict(row) -> Dict:
    return {col: row[col] for col in _COLUMNS}


class SyncStateOperations:
    """CRUD for agent_sync_state (#389)."""

    def get(self, agent_name: str) -> Optional[Dict]:
        stmt = select(
            *[agent_sync_state.c[col] for col in _COLUMNS]
        ).where(agent_sync_state.c.agent_name == agent_name)
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return _row_to_dict(row) if row else None

    def list_all(self) -> List[Dict]:
        stmt = select(*[agent_sync_state.c[col] for col in _COLUMNS])
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()
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
        git_dir_bytes: Optional[int] = None,
        pack_count: Optional[int] = None,
        loose_objects: Optional[int] = None,
        maintenance_failures: Optional[int] = None,
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
            "git_dir_bytes": _merged("git_dir_bytes", git_dir_bytes),  # #1596
            "pack_count": _merged("pack_count", pack_count),  # #1595
            "loose_objects": _merged("loose_objects", loose_objects),  # #1595
            # 0 is a meaningful reset here (post-success), not "unset" — it
            # passes _merged as-is; only a true None falls back to prior.
            "maintenance_failures": _merged("maintenance_failures", maintenance_failures) or 0,
            "last_check_at": last_check_at or now,
            "updated_at": now,
        }

        stmt = make_insert(agent_sync_state).values(**row)
        stmt = stmt.on_conflict_do_update(
            index_elements=[agent_sync_state.c.agent_name],
            set_={col: row[col] for col in _UPSERT_SET_COLUMNS},
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)
        return row

    def delete(self, agent_name: str) -> bool:
        stmt = delete(agent_sync_state).where(
            agent_sync_state.c.agent_name == agent_name
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            return result.rowcount > 0
