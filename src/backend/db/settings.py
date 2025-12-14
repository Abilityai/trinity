"""
System settings database operations.

Stores system-wide configuration like the Trinity prompt that applies to all agents.
"""

import sqlite3
from datetime import datetime
from typing import Optional, List, Dict, Any

from .connection import get_db_connection
from db_models import SystemSetting


class SettingsOperations:
    """System settings database operations."""

    @staticmethod
    def _row_to_setting(row) -> SystemSetting:
        """Convert a database row to SystemSetting model."""
        return SystemSetting(
            key=row["key"],
            value=row["value"],
            updated_at=datetime.fromisoformat(row["updated_at"])
        )

    # =========================================================================
    # Settings CRUD Operations
    # =========================================================================

    def get_setting(self, key: str) -> Optional[SystemSetting]:
        """
        Get a system setting by key.

        Returns None if the setting doesn't exist.
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT key, value, updated_at
                FROM system_settings
                WHERE key = ?
            """, (key,))
            row = cursor.fetchone()
            if row:
                return self._row_to_setting(row)
            return None

    def get_setting_value(self, key: str, default: str = None) -> Optional[str]:
        """
        Get just the value of a setting.

        Returns the default if the setting doesn't exist.
        """
        setting = self.get_setting(key)
        if setting:
            return setting.value
        return default

    def set_setting(self, key: str, value: str) -> SystemSetting:
        """
        Set a system setting value (upsert).

        Creates the setting if it doesn't exist, updates if it does.
        Returns the updated setting.
        """
        now = datetime.utcnow().isoformat()

        with get_db_connection() as conn:
            cursor = conn.cursor()

            # Use INSERT OR REPLACE for upsert
            cursor.execute("""
                INSERT OR REPLACE INTO system_settings (key, value, updated_at)
                VALUES (?, ?, ?)
            """, (key, value, now))
            conn.commit()

            return SystemSetting(
                key=key,
                value=value,
                updated_at=datetime.fromisoformat(now)
            )

    def delete_setting(self, key: str) -> bool:
        """
        Delete a system setting.

        Returns True if the setting was deleted.
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM system_settings
                WHERE key = ?
            """, (key,))
            conn.commit()
            return cursor.rowcount > 0

    def get_all_settings(self) -> List[SystemSetting]:
        """
        Get all system settings.

        Returns a list of all settings.
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT key, value, updated_at
                FROM system_settings
                ORDER BY key
            """)
            rows = cursor.fetchall()
            return [self._row_to_setting(row) for row in rows]

    def get_settings_dict(self) -> Dict[str, str]:
        """
        Get all settings as a simple key-value dictionary.

        Useful for quick access to multiple settings.
        """
        settings = self.get_all_settings()
        return {s.key: s.value for s in settings}
