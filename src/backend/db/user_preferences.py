"""Per-user UI preference database operations (trinity-enterprise#413).

A **preference** is one JSON object a user owns under a short key —
``grid_layout``, ``grid_widgets``, ``grid_org`` today. The row is keyed on
``(user_id, key)`` so a write is an upsert, and ``updated_at`` is per key
because it is the compare-and-set base the PUT contract carries.

SQLAlchemy Core over ``db/tables.py::user_ui_preferences`` so it runs
unchanged on SQLite and PostgreSQL. This layer holds no policy: the key
allowlist and the value cap live in ``services/user_preferences_service.py``;
the value arrives here already serialized.

Concurrency contract — two tabs of one user are two writers, so the
conditional write is NOT the ``db/canvas.py`` read-then-write (documented as
safe only with a single writer). It is one ``UPDATE … WHERE updated_at =
:base`` whose rowcount decides, and an ``INSERT`` that relies on the primary
key to refuse a second creator. Both are atomic per statement on both
dialects.
"""

import logging
from typing import Dict, Optional

from sqlalchemy import and_, delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from .engine import get_engine
from .tables import user_ui_preferences
from utils.helpers import utc_now_iso

logger = logging.getLogger(__name__)


class UserPreferenceOperations:
    """Per-user UI preference database operations (trinity-enterprise#413)."""

    @staticmethod
    def _row_to_record(row) -> Dict:
        return {"key": row[0], "value_json": row[1], "updated_at": row[2]}

    _COLUMNS = (
        user_ui_preferences.c.key,
        user_ui_preferences.c.value_json,
        user_ui_preferences.c.updated_at,
    )

    # ------------------------------------------------------------------ read

    def get_user_preferences(self, user_id: int) -> Dict[str, Dict]:
        """Every preference row for one user, keyed by preference key."""
        stmt = select(*self._COLUMNS).where(user_ui_preferences.c.user_id == user_id)
        with get_engine().connect() as conn:
            return {row[0]: self._row_to_record(row) for row in conn.execute(stmt)}

    def get_user_preference(self, user_id: int, key: str) -> Optional[Dict]:
        """One preference row, or None."""
        stmt = select(*self._COLUMNS).where(
            and_(
                user_ui_preferences.c.user_id == user_id,
                user_ui_preferences.c.key == key,
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).first()
        return self._row_to_record(row) if row else None

    # ----------------------------------------------------------------- write

    def set_user_preference(
        self,
        user_id: int,
        key: str,
        value_json: str,
        *,
        base_updated_at: Optional[str],
    ) -> Optional[Dict]:
        """Conditionally write one preference.

        ``base_updated_at`` is tri-state by the CALLER's contract, two-state
        here: ``None`` means "insert only — I believe no row exists", a string
        means "replace only if the row still carries this ``updated_at``".
        Returns the stored record on success, ``None`` when the condition did
        not hold (the router turns that into a 409 carrying the live row). An
        unconditional write does not exist on purpose: every client write
        states what it believes the server holds, so an older tab can never
        overwrite a newer save without noticing.
        """
        now = utc_now_iso()
        with get_engine().begin() as conn:
            if base_updated_at is None:
                try:
                    conn.execute(
                        insert(user_ui_preferences).values(
                            user_id=user_id, key=key, value_json=value_json, updated_at=now
                        )
                    )
                except IntegrityError:
                    return None  # a row already exists — the PK refused the second creator
                return {"key": key, "value_json": value_json, "updated_at": now}

            result = conn.execute(
                update(user_ui_preferences)
                .where(
                    and_(
                        user_ui_preferences.c.user_id == user_id,
                        user_ui_preferences.c.key == key,
                        user_ui_preferences.c.updated_at == base_updated_at,
                    )
                )
                .values(value_json=value_json, updated_at=now)
            )
            if result.rowcount != 1:
                return None
            return {"key": key, "value_json": value_json, "updated_at": now}

    def delete_user_preference(self, user_id: int, key: str) -> bool:
        """Remove one preference; True when a row was deleted."""
        with get_engine().begin() as conn:
            result = conn.execute(
                delete(user_ui_preferences).where(
                    and_(
                        user_ui_preferences.c.user_id == user_id,
                        user_ui_preferences.c.key == key,
                    )
                )
            )
            return result.rowcount > 0

    def delete_user_preferences(self, user_id: int) -> int:
        """Remove every preference row for a user (the manual cascade hook).

        OSS has no user-deletion path today; the FK carries ``ON DELETE
        CASCADE`` for the dialect that enforces it, and this is the explicit
        call for a deleter that runs with foreign keys off (SQLite default).
        """
        with get_engine().begin() as conn:
            result = conn.execute(
                delete(user_ui_preferences).where(user_ui_preferences.c.user_id == user_id)
            )
            return result.rowcount
