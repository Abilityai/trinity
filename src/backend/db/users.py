"""
User management database operations.

Handles user CRUD, authentication, and profile management.
"""

from datetime import datetime
from typing import Optional, Dict, List, Any

from .connection import get_db_connection
from db_models import UserCreate


class UserOperations:
    """User database operations."""

    # SQL query fragments for reuse
    _USER_COLUMNS = """id, username, password_hash, role, auth0_sub, name, picture, email,
                       created_at, updated_at, last_login"""

    @staticmethod
    def _row_to_user_dict(row) -> Dict:
        """Convert a user row to a dictionary."""
        return {
            "id": row["id"],
            "username": row["username"],
            "password": row["password_hash"],  # Keep as "password" for backward compat
            "role": row["role"],
            "auth0_sub": row["auth0_sub"],
            "name": row["name"],
            "picture": row["picture"],
            "email": row["email"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_login": row["last_login"]
        }

    def _get_user_by_field(self, field: str, value: Any) -> Optional[Dict]:
        """Generic user lookup by any field."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"""
                SELECT {self._USER_COLUMNS}
                FROM users WHERE {field} = ?
            """, (value,))
            row = cursor.fetchone()
            return self._row_to_user_dict(row) if row else None

    def get_user_by_username(self, username: str) -> Optional[Dict]:
        """Get user by username."""
        return self._get_user_by_field("username", username)

    def get_user_by_auth0_sub(self, auth0_sub: str) -> Optional[Dict]:
        """Get user by Auth0 subject ID."""
        return self._get_user_by_field("auth0_sub", auth0_sub)

    def get_user_by_id(self, user_id: int) -> Optional[Dict]:
        """Get user by ID."""
        return self._get_user_by_field("id", user_id)

    def get_user_by_email(self, email: str) -> Optional[Dict]:
        """Get user by email address."""
        return self._get_user_by_field("email", email)

    def create_user(self, user_data: UserCreate) -> Dict:
        """Create a new user."""
        now = datetime.utcnow().isoformat()

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO users (username, password_hash, role, auth0_sub, name, picture, email, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                user_data.username,
                user_data.password,
                user_data.role,
                user_data.auth0_sub,
                user_data.name,
                user_data.picture,
                user_data.email or user_data.username,  # Use username as email if not provided
                now,
                now
            ))
            conn.commit()
            user_id = cursor.lastrowid

            return {
                "id": user_id,
                "username": user_data.username,
                "password": user_data.password,
                "role": user_data.role,
                "auth0_sub": user_data.auth0_sub,
                "name": user_data.name,
                "picture": user_data.picture,
                "email": user_data.email or user_data.username,
                "created_at": now,
                "updated_at": now,
                "last_login": None
            }

    def update_user(self, username: str, updates: Dict) -> Optional[Dict]:
        """Update user fields."""
        with get_db_connection() as conn:
            cursor = conn.cursor()

            # Build dynamic update query
            set_clauses = []
            params = []
            for key, value in updates.items():
                if key in ["name", "picture", "role", "email"]:
                    set_clauses.append(f"{key} = ?")
                    params.append(value)

            if not set_clauses:
                return self.get_user_by_username(username)

            set_clauses.append("updated_at = ?")
            params.append(datetime.utcnow().isoformat())
            params.append(username)

            cursor.execute(f"""
                UPDATE users SET {", ".join(set_clauses)} WHERE username = ?
            """, params)
            conn.commit()

            return self.get_user_by_username(username)

    def update_user_password(self, username: str, hashed_password: str) -> bool:
        """Update user's password hash.

        Args:
            username: The username to update
            hashed_password: The bcrypt-hashed password

        Returns:
            True if the user was found and updated, False otherwise
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            now = datetime.utcnow().isoformat()
            cursor.execute("""
                UPDATE users SET password_hash = ?, updated_at = ? WHERE username = ?
            """, (hashed_password, now, username))
            conn.commit()
            return cursor.rowcount > 0

    def update_last_login(self, username: str):
        """Update user's last login timestamp."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            now = datetime.utcnow().isoformat()
            cursor.execute("""
                UPDATE users SET last_login = ?, updated_at = ? WHERE username = ?
            """, (now, now, username))
            conn.commit()

    def get_or_create_auth0_user(self, auth0_sub: str, email: str, name: str = None, picture: str = None) -> Dict:
        """Get or create a user from Auth0 authentication."""
        # First try to find by auth0_sub
        user = self.get_user_by_auth0_sub(auth0_sub)
        if user:
            # Update profile info if changed
            updates = {}
            if name and name != user.get("name"):
                updates["name"] = name
            if picture and picture != user.get("picture"):
                updates["picture"] = picture
            if updates:
                self.update_user(user["username"], updates)
                user = self.get_user_by_username(user["username"])
            return user

        # Try to find by email (username)
        user = self.get_user_by_username(email)
        if user:
            # Link auth0_sub to existing user
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE users SET auth0_sub = ?, name = ?, picture = ?, updated_at = ?
                    WHERE username = ?
                """, (auth0_sub, name, picture, datetime.utcnow().isoformat(), email))
                conn.commit()
            return self.get_user_by_username(email)

        # Create new user
        user_data = UserCreate(
            username=email,
            password=None,  # Auth0 users don't have local passwords
            role="user",
            auth0_sub=auth0_sub,
            name=name,
            picture=picture,
            email=email
        )
        return self.create_user(user_data)

    def list_users(self) -> List[Dict]:
        """List all users (admin only)."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, username, role, auth0_sub, name, picture, email,
                       created_at, updated_at, last_login
                FROM users ORDER BY created_at DESC
            """)
            return [dict(row) for row in cursor.fetchall()]
