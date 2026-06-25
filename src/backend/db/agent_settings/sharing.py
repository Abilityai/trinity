"""
Agent sharing database operations.

Handles share/unshare agents by email, share lookups, and permission checks.
"""

from datetime import datetime
from typing import Optional, List

from sqlalchemy import select, insert, update, delete, and_, func
from sqlalchemy.exc import IntegrityError

from ..engine import get_engine
from ..tables import agent_sharing, access_requests, users
from db_models import AgentShare
from utils.helpers import utc_now_iso


class SharingMixin:
    """Mixin for agent sharing operations. Requires self._user_ops and ownership methods."""

    @staticmethod
    def _row_to_agent_share(row) -> AgentShare:
        """Convert an agent_sharing row to an AgentShare model."""
        return AgentShare(
            id=row["id"],
            agent_name=row["agent_name"],
            shared_with_email=row["shared_with_email"],
            shared_by_id=row["shared_by_id"],
            shared_by_email=row["shared_by_email"] or "unknown",
            created_at=datetime.fromisoformat(row["created_at"]),
            allow_proactive=bool(row["allow_proactive"]) if "allow_proactive" in row.keys() else False
        )

    def share_agent(self, agent_name: str, owner_username: str, share_with_email: str) -> Optional[AgentShare]:
        """
        Share an agent with another user by email.
        - Validates owner has permission to share
        - Creates sharing record with email (user doesn't need to exist yet)
        - Clears any stale pending access_requests row for the same email+agent
          so the owner's "Pending Access Requests" list reflects reality (#446).
        - Returns share details or None if failed
        """
        # Get owner and validate permission
        owner = self._user_ops.get_user_by_username(owner_username)
        if not owner:
            return None

        # Check if user can share (owner or admin)
        if not self.can_user_share_agent(owner_username, agent_name):
            return None

        normalized_email = (share_with_email or "").strip().lower()
        if not normalized_email:
            return None

        # Prevent self-sharing (check if owner's email matches target email)
        owner_email = (owner.get("email") or "").strip().lower()
        if owner_email and owner_email == normalized_email:
            return None

        now = utc_now_iso()

        try:
            with get_engine().begin() as conn:
                result = conn.execute(
                    insert(agent_sharing).values(
                        agent_name=agent_name,
                        shared_with_email=normalized_email,
                        shared_by_id=owner["id"],
                        created_at=now,
                    )
                )
                # #446: once the email is on the allow-list, any pending access
                # request for the same (agent, email) is stale — drop it so the
                # owner's Pending list doesn't double-prompt them.
                conn.execute(
                    delete(access_requests).where(
                        and_(
                            access_requests.c.agent_name == agent_name,
                            access_requests.c.email == normalized_email,
                            access_requests.c.status == "pending",
                        )
                    )
                )
                share_id = result.inserted_primary_key[0]

            return AgentShare(
                id=share_id,
                agent_name=agent_name,
                shared_with_email=normalized_email,
                shared_by_id=owner["id"],
                shared_by_email=owner.get("email") or owner.get("username") or "unknown",
                created_at=datetime.fromisoformat(now)
            )
        except IntegrityError:
            # Already shared with this email
            return None

    def unshare_agent(self, agent_name: str, owner_username: str, share_with_email: str) -> bool:
        """Remove sharing access for an email."""
        # Validate permission
        if not self.can_user_share_agent(owner_username, agent_name):
            return False

        with get_engine().begin() as conn:
            result = conn.execute(
                delete(agent_sharing).where(
                    and_(
                        agent_sharing.c.agent_name == agent_name,
                        agent_sharing.c.shared_with_email == share_with_email.lower(),
                    )
                )
            )
            return result.rowcount > 0

    def get_agent_shares(self, agent_name: str) -> List[AgentShare]:
        """Get all emails an agent is shared with."""
        stmt = (
            select(
                agent_sharing.c.id,
                agent_sharing.c.agent_name,
                agent_sharing.c.shared_with_email,
                agent_sharing.c.shared_by_id,
                agent_sharing.c.created_at,
                agent_sharing.c.allow_proactive,
                func.coalesce(users.c.email, users.c.username).label("shared_by_email"),
            )
            .select_from(
                agent_sharing.join(users, agent_sharing.c.shared_by_id == users.c.id)
            )
            .where(agent_sharing.c.agent_name == agent_name)
            .order_by(agent_sharing.c.created_at.desc())
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_agent_share(row) for row in rows]

    def get_agent_operator_access(self, agent_name: str) -> List[dict]:
        """Operator (Trinity-user) access grants for an agent — the #17 Access tab.

        Resolves each ``agent_sharing`` allow-list email against ``users`` (the
        grantee, lower-cased on both sides). A resolved row is an **active
        operator** (username/role/last-active surfaced); an unresolved email is a
        **pending** invite (allow-listed but no account yet). Drawing this line on
        the read path is what this issue owns. The strict client-vs-operator split
        (non-user emails → a dedicated client roster) belongs to the Sharing-side
        redesign (#18/#20), so all grants stay visible here for now.
        """
        stmt = (
            select(
                agent_sharing.c.shared_with_email,
                agent_sharing.c.created_at,
                agent_sharing.c.allow_proactive,
                users.c.username,
                users.c.role,
                users.c.last_login,
            )
            .select_from(
                agent_sharing.outerjoin(
                    users,
                    func.lower(users.c.email)
                    == func.lower(agent_sharing.c.shared_with_email),
                )
            )
            .where(agent_sharing.c.agent_name == agent_name)
            .order_by(agent_sharing.c.created_at.desc())
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        out: List[dict] = []
        for r in rows:
            resolved = r["username"] is not None
            out.append({
                "email": r["shared_with_email"],
                "username": r["username"],
                "role": r["role"],
                "last_active": r["last_login"],
                "status": "active" if resolved else "pending",
                "allow_proactive": bool(r["allow_proactive"]),
            })
        return out

    def get_shared_agents(self, username: str) -> List[str]:
        """Get all agent names shared with a user (by their email)."""
        user = self._user_ops.get_user_by_username(username)
        if not user:
            return []

        user_email = user.get("email")
        if not user_email:
            return []

        stmt = select(agent_sharing.c.agent_name).where(
            agent_sharing.c.shared_with_email == user_email.lower()
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [row["agent_name"] for row in rows]

    def is_agent_shared_with_email(self, agent_name: str, email: str) -> bool:
        """Check if an agent is shared with the given email directly."""
        normalized = (email or "").strip().lower()
        if not normalized:
            return False
        stmt = select(agent_sharing.c.id).where(
            and_(
                agent_sharing.c.agent_name == agent_name,
                agent_sharing.c.shared_with_email == normalized,
            )
        )
        with get_engine().connect() as conn:
            return conn.execute(stmt).first() is not None

    def email_has_agent_access(self, agent_name: str, email: str) -> bool:
        """Cross-channel access check (Issue #311).

        Returns True when the email is owner, admin, or in agent_sharing for
        the given agent. Used by the channel router gate. Defensive
        normalization (#446): strip + lowercase at the boundary so
        mixed-case session emails can't bypass the allow-list match.
        """
        normalized = (email or "").strip().lower()
        if not normalized:
            return False
        user = self._user_ops.get_user_by_email(normalized)
        if user:
            if user.get("role") == "admin":
                return True
            owner = self.get_agent_owner(agent_name)
            if owner and owner["owner_username"] == user["username"]:
                return True
        return self.is_agent_shared_with_email(agent_name, normalized)

    def is_agent_shared_with_user(self, agent_name: str, username: str) -> bool:
        """Check if an agent is shared with a specific user (by their email)."""
        user = self._user_ops.get_user_by_username(username)
        if not user:
            return False

        user_email = user.get("email")
        if not user_email:
            return False

        stmt = select(agent_sharing.c.id).where(
            and_(
                agent_sharing.c.agent_name == agent_name,
                agent_sharing.c.shared_with_email == user_email.lower(),
            )
        )
        with get_engine().connect() as conn:
            return conn.execute(stmt).first() is not None

    def can_user_share_agent(self, username: str, agent_name: str) -> bool:
        """Check if a user can share an agent (only owner or admin)."""
        user = self._user_ops.get_user_by_username(username)
        if not user:
            return False

        if user["role"] == "admin":
            return True

        owner = self.get_agent_owner(agent_name)
        return owner and owner["owner_username"] == username

    def delete_agent_shares(self, agent_name: str) -> int:
        """Delete all sharing records for an agent (when agent is deleted)."""
        with get_engine().begin() as conn:
            result = conn.execute(
                delete(agent_sharing).where(agent_sharing.c.agent_name == agent_name)
            )
            return result.rowcount

    # =========================================================================
    # Proactive Messaging (Issue #321)
    # =========================================================================

    def can_agent_message_email(self, agent_name: str, email: str) -> bool:
        """Check if agent is authorized to send proactive messages to this email.

        Authorization requires:
        1. User is in agent_sharing for this agent AND allow_proactive = 1
        2. OR user is the owner of the agent (always allowed)
        """
        if not email:
            return False

        # Check if owner
        owner = self.get_agent_owner(agent_name)
        if owner:
            owner_user = self._user_ops.get_user_by_username(owner["owner_username"])
            if owner_user and (owner_user.get("email") or "").lower() == email.lower():
                return True

        # Check sharing with allow_proactive flag
        stmt = select(agent_sharing.c.id).where(
            and_(
                agent_sharing.c.agent_name == agent_name,
                agent_sharing.c.shared_with_email == email.lower(),
                agent_sharing.c.allow_proactive == 1,
            )
        )
        with get_engine().connect() as conn:
            return conn.execute(stmt).first() is not None

    def set_allow_proactive(
        self, agent_name: str, email: str, allow: bool, setter_username: str
    ) -> bool:
        """Update allow_proactive flag for a sharing record.

        Only the owner or admin can modify this flag.
        Returns True if updated, False if not authorized or share not found.
        """
        if not self.can_user_share_agent(setter_username, agent_name):
            return False

        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_sharing)
                .where(
                    and_(
                        agent_sharing.c.agent_name == agent_name,
                        agent_sharing.c.shared_with_email == email.lower(),
                    )
                )
                .values(allow_proactive=1 if allow else 0)
            )
            return result.rowcount > 0

    def get_proactive_enabled_shares(self, agent_name: str) -> List[str]:
        """Get all emails that have opted in to receive proactive messages from this agent."""
        stmt = select(agent_sharing.c.shared_with_email).where(
            and_(
                agent_sharing.c.agent_name == agent_name,
                agent_sharing.c.allow_proactive == 1,
            )
        )
        with get_engine().connect() as conn:
            return [row[0] for row in conn.execute(stmt).all()]
