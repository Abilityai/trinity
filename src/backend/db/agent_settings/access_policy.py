"""
Agent access policy settings (Issue #311).

Stores per-agent channel access policy:
- require_email: gate requires a verified email on incoming messages
- open_access: anyone with a verified email may talk (access_requests skipped)
- group_auth_mode: auth mode for group chats ('none', 'any_verified')
"""

from db.connection import get_db_connection


class AccessPolicyMixin:
    """Mixin for per-agent access policy (require_email, open_access, group_auth_mode)."""

    def get_access_policy(self, agent_name: str) -> dict:
        """Return access policy for an agent.

        Returns:
            {
                'require_email': bool,
                'open_access': bool,
                'group_auth_mode': str  # 'none' or 'any_verified'
            }
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT COALESCE(require_email, 0) AS require_email,
                       COALESCE(open_access, 0) AS open_access,
                       COALESCE(group_auth_mode, 'none') AS group_auth_mode
                FROM agent_ownership
                WHERE agent_name = ? AND deleted_at IS NULL
                """,
                (agent_name,),
            )
            row = cursor.fetchone()
        if not row:
            return {"require_email": False, "open_access": False, "group_auth_mode": "none"}
        return {
            "require_email": bool(row["require_email"]),
            "open_access": bool(row["open_access"]),
            "group_auth_mode": row["group_auth_mode"] or "none",
        }

    def set_access_policy(
        self,
        agent_name: str,
        require_email: bool,
        open_access: bool,
        group_auth_mode: str = "none",
    ) -> bool:
        """Update access policy for an agent.

        Args:
            agent_name: Agent name
            require_email: Require verified email for DMs
            open_access: Anyone with verified email can chat (skip access_requests)
            group_auth_mode: 'none' (no auth in groups) or 'any_verified' (at least one verified member)
        """
        if group_auth_mode not in ("none", "any_verified"):
            group_auth_mode = "none"
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE agent_ownership
                SET require_email = ?, open_access = ?, group_auth_mode = ?
                WHERE agent_name = ?
                """,
                (1 if require_email else 0, 1 if open_access else 0, group_auth_mode, agent_name),
            )
            conn.commit()
            return cursor.rowcount > 0
