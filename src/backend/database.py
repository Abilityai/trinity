"""
SQLite persistence layer for Trinity platform.

This module provides the main DatabaseManager class and database initialization.
The actual database operations are organized in submodules under db/:
- db/users.py: User management
- db/agents.py: Agent ownership and sharing
- db/mcp_keys.py: MCP API key management
- db/schedules.py: Schedule and execution management
- db/chat.py: Chat session and message persistence
- db/activities.py: Activity stream logging

For backward compatibility, all models and the global `db` instance are
re-exported from this module.

Handles persistent storage for:
- Users (Auth0 and local users)
- Agent ownership (who created which agent)
- MCP API keys (with usage tracking)

Redis is still used for:
- Credential secrets (fast access)
- OAuth state (ephemeral, TTL-based)
- Sessions/cache
"""

import os
from datetime import datetime
from pathlib import Path

# Re-export models for backward compatibility
from db_models import (
    UserCreate,
    User,
    AgentOwnership,
    AgentShare,
    AgentShareRequest,
    McpApiKeyCreate,
    McpApiKey,
    McpApiKeyWithSecret,
    McpAgentKeyCreate,
    ScheduleCreate,
    Schedule,
    ScheduleExecution,
    AgentGitConfig,
    GitSyncResult,
    ChatSession,
    ChatMessage,
    AgentPermission,
    AgentPermissionInfo,
    SharedFolderConfig,
    SharedFolderConfigUpdate,
    SharedFolderMount,
    SharedFolderInfo,
    SystemSetting,
    SystemSettingUpdate,
)

# Re-export connection utilities
from db.connection import get_db_connection, DB_PATH

# Import operation classes
from db.users import UserOperations
from db.agents import AgentOperations
from db.mcp_keys import McpKeyOperations
from db.schedules import ScheduleOperations
from db.chat import ChatOperations
from db.activities import ActivityOperations
from db.permissions import PermissionOperations
from db.shared_folders import SharedFolderOperations
from db.settings import SettingsOperations


def _migrate_agent_sharing_table(cursor, conn):
    """Migrate agent_sharing table from user_id based to email based."""
    # Check if old schema exists (with shared_with_id column)
    cursor.execute("PRAGMA table_info(agent_sharing)")
    columns = {row[1] for row in cursor.fetchall()}

    if "shared_with_id" in columns and "shared_with_email" not in columns:
        print("Migrating agent_sharing table to email-based schema...")

        # Migrate existing data: join with users to get emails
        cursor.execute("""
            SELECT s.id, s.agent_name, u.email, s.shared_by_id, s.created_at
            FROM agent_sharing s
            JOIN users u ON s.shared_with_id = u.id
        """)
        existing_shares = cursor.fetchall()

        # Drop old table
        cursor.execute("DROP TABLE agent_sharing")

        # Create new table with email
        cursor.execute("""
            CREATE TABLE agent_sharing (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name TEXT NOT NULL,
                shared_with_email TEXT NOT NULL,
                shared_by_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (shared_by_id) REFERENCES users(id),
                UNIQUE(agent_name, shared_with_email)
            )
        """)

        # Re-insert migrated data
        for share in existing_shares:
            if share[2]:  # email exists
                cursor.execute("""
                    INSERT INTO agent_sharing (agent_name, shared_with_email, shared_by_id, created_at)
                    VALUES (?, ?, ?, ?)
                """, (share[1], share[2].lower(), share[3], share[4]))

        conn.commit()
        print(f"Migrated {len(existing_shares)} sharing records to email-based schema")


def _migrate_schedule_executions_observability(cursor, conn):
    """Add observability columns to schedule_executions table."""
    cursor.execute("PRAGMA table_info(schedule_executions)")
    columns = {row[1] for row in cursor.fetchall()}

    # Add new columns if they don't exist
    new_columns = [
        ("context_used", "INTEGER"),
        ("context_max", "INTEGER"),
        ("cost", "REAL"),
        ("tool_calls", "TEXT")
    ]

    for col_name, col_type in new_columns:
        if col_name not in columns:
            print(f"Adding {col_name} column to schedule_executions...")
            cursor.execute(f"ALTER TABLE schedule_executions ADD COLUMN {col_name} {col_type}")

    conn.commit()


def _migrate_mcp_api_keys_agent_scope(cursor, conn):
    """Add agent_name and scope columns to mcp_api_keys table for agent-to-agent collaboration."""
    cursor.execute("PRAGMA table_info(mcp_api_keys)")
    columns = {row[1] for row in cursor.fetchall()}

    # Add new columns if they don't exist
    new_columns = [
        ("agent_name", "TEXT"),  # Which agent owns this key (NULL for user keys)
        ("scope", "TEXT DEFAULT 'user'")  # "user" or "agent"
    ]

    for col_name, col_type in new_columns:
        if col_name not in columns:
            print(f"Adding {col_name} column to mcp_api_keys for agent collaboration...")
            cursor.execute(f"ALTER TABLE mcp_api_keys ADD COLUMN {col_name} {col_type}")

    conn.commit()


def init_database():
    """Initialize the SQLite database with all required tables."""
    db_path = Path(DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with get_db_connection() as conn:
        cursor = conn.cursor()

        # Run migrations first (if tables exist)
        try:
            _migrate_agent_sharing_table(cursor, conn)
        except Exception as e:
            print(f"Migration check (agent_sharing): {e}")

        try:
            _migrate_schedule_executions_observability(cursor, conn)
        except Exception as e:
            print(f"Migration check (schedule_executions): {e}")

        try:
            _migrate_mcp_api_keys_agent_scope(cursor, conn)
        except Exception as e:
            print(f"Migration check (mcp_api_keys agent scope): {e}")

        # Users table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT,
                role TEXT NOT NULL DEFAULT 'user',
                auth0_sub TEXT UNIQUE,
                name TEXT,
                picture TEXT,
                email TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_login TEXT
            )
        """)

        # Agent ownership table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS agent_ownership (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name TEXT UNIQUE NOT NULL,
                owner_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (owner_id) REFERENCES users(id)
            )
        """)

        # Agent sharing table - stores email directly (user may not exist yet)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS agent_sharing (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name TEXT NOT NULL,
                shared_with_email TEXT NOT NULL,
                shared_by_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (shared_by_id) REFERENCES users(id),
                UNIQUE(agent_name, shared_with_email)
            )
        """)

        # MCP API keys table (with agent collaboration support)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS mcp_api_keys (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                key_prefix TEXT NOT NULL,
                key_hash TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL,
                last_used_at TEXT,
                usage_count INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                user_id INTEGER NOT NULL,
                agent_name TEXT,
                scope TEXT DEFAULT 'user',
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        # Agent schedules table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS agent_schedules (
                id TEXT PRIMARY KEY,
                agent_name TEXT NOT NULL,
                name TEXT NOT NULL,
                cron_expression TEXT NOT NULL,
                message TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                timezone TEXT DEFAULT 'UTC',
                description TEXT,
                owner_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_run_at TEXT,
                next_run_at TEXT,
                FOREIGN KEY (owner_id) REFERENCES users(id)
            )
        """)

        # Schedule executions table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS schedule_executions (
                id TEXT PRIMARY KEY,
                schedule_id TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                duration_ms INTEGER,
                message TEXT NOT NULL,
                response TEXT,
                error TEXT,
                triggered_by TEXT NOT NULL,
                context_used INTEGER,
                context_max INTEGER,
                cost REAL,
                tool_calls TEXT,
                FOREIGN KEY (schedule_id) REFERENCES agent_schedules(id)
            )
        """)

        # Agent git configuration table (Phase 7: GitHub Bidirectional Sync)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS agent_git_config (
                id TEXT PRIMARY KEY,
                agent_name TEXT UNIQUE NOT NULL,
                github_repo TEXT NOT NULL,
                working_branch TEXT NOT NULL,
                instance_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_sync_at TEXT,
                last_commit_sha TEXT,
                sync_enabled INTEGER DEFAULT 1,
                sync_paths TEXT,
                FOREIGN KEY (agent_name) REFERENCES agent_ownership(agent_name)
            )
        """)

        # Chat sessions table (persistent chat tracking)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS chat_sessions (
                id TEXT PRIMARY KEY,
                agent_name TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                user_email TEXT NOT NULL,
                started_at TEXT NOT NULL,
                last_message_at TEXT NOT NULL,
                message_count INTEGER DEFAULT 0,
                total_cost REAL DEFAULT 0.0,
                total_context_used INTEGER DEFAULT 0,
                total_context_max INTEGER DEFAULT 200000,
                status TEXT DEFAULT 'active',
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        # Chat messages table (all user and assistant messages)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                user_email TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                cost REAL,
                context_used INTEGER,
                context_max INTEGER,
                tool_calls TEXT,
                execution_time_ms INTEGER,
                FOREIGN KEY (session_id) REFERENCES chat_sessions(id),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        # Agent activities table (unified activity stream - Phase 9.7)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS agent_activities (
                id TEXT PRIMARY KEY,
                agent_name TEXT NOT NULL,
                activity_type TEXT NOT NULL,
                activity_state TEXT NOT NULL,
                parent_activity_id TEXT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                duration_ms INTEGER,
                user_id INTEGER,
                triggered_by TEXT NOT NULL,
                related_chat_message_id TEXT,
                related_execution_id TEXT,
                details TEXT,
                error TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (parent_activity_id) REFERENCES agent_activities(id),
                FOREIGN KEY (related_chat_message_id) REFERENCES chat_messages(id),
                FOREIGN KEY (related_execution_id) REFERENCES schedule_executions(id)
            )
        """)

        # Agent permissions table (Phase 9.10: Agent-to-Agent Permissions)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS agent_permissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_agent TEXT NOT NULL,
                target_agent TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                created_by TEXT NOT NULL,
                UNIQUE(source_agent, target_agent)
            )
        """)

        # Agent shared folder configuration table (Phase 9.11: Agent Shared Folders)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS agent_shared_folder_config (
                agent_name TEXT PRIMARY KEY,
                expose_enabled INTEGER DEFAULT 0,
                consume_enabled INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        # System settings table (system-wide configuration)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        # Indexes
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_auth0_sub ON users(auth0_sub)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_agent_ownership_owner ON agent_ownership(owner_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_agent_ownership_name ON agent_ownership(agent_name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_agent_sharing_agent ON agent_sharing(agent_name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_agent_sharing_email ON agent_sharing(shared_with_email)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_mcp_keys_user ON mcp_api_keys(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_mcp_keys_hash ON mcp_api_keys(key_hash)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_mcp_keys_agent ON mcp_api_keys(agent_name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_schedules_agent ON agent_schedules(agent_name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_schedules_owner ON agent_schedules(owner_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_schedules_enabled ON agent_schedules(enabled)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_executions_schedule ON schedule_executions(schedule_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_executions_agent ON schedule_executions(agent_name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_executions_status ON schedule_executions(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_git_config_agent ON agent_git_config(agent_name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_git_config_repo ON agent_git_config(github_repo)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_sessions_agent ON chat_sessions(agent_name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_sessions_user ON chat_sessions(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_sessions_status ON chat_sessions(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(session_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_agent ON chat_messages(agent_name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_user ON chat_messages(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_timestamp ON chat_messages(timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_activities_agent ON agent_activities(agent_name, created_at DESC)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_activities_type ON agent_activities(activity_type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_activities_state ON agent_activities(activity_state)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_activities_user ON agent_activities(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_activities_parent ON agent_activities(parent_activity_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_activities_chat_msg ON agent_activities(related_chat_message_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_activities_execution ON agent_activities(related_execution_id)")
        # Agent permissions indexes (Phase 9.10)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_permissions_source ON agent_permissions(source_agent)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_permissions_target ON agent_permissions(target_agent)")
        # Shared folder indexes (Phase 9.11)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_shared_folders_expose ON agent_shared_folder_config(expose_enabled)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_shared_folders_consume ON agent_shared_folder_config(consume_enabled)")

        conn.commit()

        # Create default admin user if not exists
        _ensure_admin_user(cursor, conn)


def _ensure_admin_user(cursor, conn):
    """Ensure the admin user exists."""
    admin_password = os.getenv("ADMIN_PASSWORD", "changeme")

    cursor.execute("SELECT id FROM users WHERE username = ?", ("admin",))
    if cursor.fetchone() is None:
        now = datetime.utcnow().isoformat()
        # Store password as plaintext for now (consistent with current implementation)
        # In production, use proper hashing
        cursor.execute("""
            INSERT INTO users (username, password_hash, role, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
        """, ("admin", admin_password, "admin", now, now))
        conn.commit()
        print("Created default admin user")


class DatabaseManager:
    """
    Manages SQLite database operations for Trinity platform.

    This class composes operations from specialized submodules:
    - User management: db/users.py
    - Agent ownership/sharing: db/agents.py
    - MCP API keys: db/mcp_keys.py
    - Schedules/executions: db/schedules.py
    - Chat sessions/messages: db/chat.py
    - Activity stream: db/activities.py

    All methods are delegated to the appropriate submodule while
    maintaining the same interface for backward compatibility.
    """

    def __init__(self):
        init_database()

        # Initialize operation handlers
        self._user_ops = UserOperations()
        self._agent_ops = AgentOperations(self._user_ops)
        self._mcp_key_ops = McpKeyOperations(self._user_ops)
        self._schedule_ops = ScheduleOperations(self._user_ops, self._agent_ops)
        self._chat_ops = ChatOperations()
        self._activity_ops = ActivityOperations()
        self._permission_ops = PermissionOperations(self._user_ops, self._agent_ops)
        self._shared_folder_ops = SharedFolderOperations(self._permission_ops)
        self._settings_ops = SettingsOperations()

    # =========================================================================
    # User Management (delegated to db/users.py)
    # =========================================================================

    def get_user_by_username(self, username: str):
        return self._user_ops.get_user_by_username(username)

    def get_user_by_auth0_sub(self, auth0_sub: str):
        return self._user_ops.get_user_by_auth0_sub(auth0_sub)

    def get_user_by_id(self, user_id: int):
        return self._user_ops.get_user_by_id(user_id)

    def get_user_by_email(self, email: str):
        return self._user_ops.get_user_by_email(email)

    def create_user(self, user_data: UserCreate):
        return self._user_ops.create_user(user_data)

    def update_user(self, username: str, updates: dict):
        return self._user_ops.update_user(username, updates)

    def update_last_login(self, username: str):
        return self._user_ops.update_last_login(username)

    def get_or_create_auth0_user(self, auth0_sub: str, email: str, name: str = None, picture: str = None):
        return self._user_ops.get_or_create_auth0_user(auth0_sub, email, name, picture)

    def list_users(self):
        return self._user_ops.list_users()

    # =========================================================================
    # Agent Ownership Management (delegated to db/agents.py)
    # =========================================================================

    def register_agent_owner(self, agent_name: str, owner_username: str):
        return self._agent_ops.register_agent_owner(agent_name, owner_username)

    def get_agent_owner(self, agent_name: str):
        return self._agent_ops.get_agent_owner(agent_name)

    def get_agents_by_owner(self, owner_username: str):
        return self._agent_ops.get_agents_by_owner(owner_username)

    def delete_agent_ownership(self, agent_name: str):
        return self._agent_ops.delete_agent_ownership(agent_name)

    def can_user_access_agent(self, username: str, agent_name: str):
        return self._agent_ops.can_user_access_agent(username, agent_name)

    def can_user_delete_agent(self, username: str, agent_name: str):
        return self._agent_ops.can_user_delete_agent(username, agent_name)

    # =========================================================================
    # Agent Sharing Management (delegated to db/agents.py)
    # =========================================================================

    def share_agent(self, agent_name: str, owner_username: str, share_with_email: str):
        return self._agent_ops.share_agent(agent_name, owner_username, share_with_email)

    def unshare_agent(self, agent_name: str, owner_username: str, share_with_email: str):
        return self._agent_ops.unshare_agent(agent_name, owner_username, share_with_email)

    def get_agent_shares(self, agent_name: str):
        return self._agent_ops.get_agent_shares(agent_name)

    def get_shared_agents(self, username: str):
        return self._agent_ops.get_shared_agents(username)

    def is_agent_shared_with_user(self, agent_name: str, username: str):
        return self._agent_ops.is_agent_shared_with_user(agent_name, username)

    def can_user_share_agent(self, username: str, agent_name: str):
        return self._agent_ops.can_user_share_agent(username, agent_name)

    def delete_agent_shares(self, agent_name: str):
        return self._agent_ops.delete_agent_shares(agent_name)

    # =========================================================================
    # MCP API Key Management (delegated to db/mcp_keys.py)
    # =========================================================================

    def create_mcp_api_key(self, username: str, key_data: McpApiKeyCreate):
        return self._mcp_key_ops.create_mcp_api_key(username, key_data)

    def create_agent_mcp_api_key(self, agent_name: str, owner_username: str, description: str = None):
        return self._mcp_key_ops.create_agent_mcp_api_key(agent_name, owner_username, description)

    def get_agent_mcp_api_key(self, agent_name: str):
        return self._mcp_key_ops.get_agent_mcp_api_key(agent_name)

    def delete_agent_mcp_api_key(self, agent_name: str):
        return self._mcp_key_ops.delete_agent_mcp_api_key(agent_name)

    def validate_mcp_api_key(self, api_key: str):
        return self._mcp_key_ops.validate_mcp_api_key(api_key)

    def get_mcp_api_key(self, key_id: str, username: str):
        return self._mcp_key_ops.get_mcp_api_key(key_id, username)

    def list_mcp_api_keys(self, username: str):
        return self._mcp_key_ops.list_mcp_api_keys(username)

    def list_all_mcp_api_keys(self):
        return self._mcp_key_ops.list_all_mcp_api_keys()

    def revoke_mcp_api_key(self, key_id: str, username: str):
        return self._mcp_key_ops.revoke_mcp_api_key(key_id, username)

    def delete_mcp_api_key(self, key_id: str, username: str):
        return self._mcp_key_ops.delete_mcp_api_key(key_id, username)

    # =========================================================================
    # Schedule Management (delegated to db/schedules.py)
    # =========================================================================

    def create_schedule(self, agent_name: str, username: str, schedule_data: ScheduleCreate):
        return self._schedule_ops.create_schedule(agent_name, username, schedule_data)

    def get_schedule(self, schedule_id: str):
        return self._schedule_ops.get_schedule(schedule_id)

    def list_agent_schedules(self, agent_name: str):
        return self._schedule_ops.list_agent_schedules(agent_name)

    def list_all_enabled_schedules(self):
        return self._schedule_ops.list_all_enabled_schedules()

    def update_schedule(self, schedule_id: str, username: str, updates: dict):
        return self._schedule_ops.update_schedule(schedule_id, username, updates)

    def delete_schedule(self, schedule_id: str, username: str):
        return self._schedule_ops.delete_schedule(schedule_id, username)

    def set_schedule_enabled(self, schedule_id: str, enabled: bool):
        return self._schedule_ops.set_schedule_enabled(schedule_id, enabled)

    def update_schedule_run_times(self, schedule_id: str, last_run_at=None, next_run_at=None):
        return self._schedule_ops.update_schedule_run_times(schedule_id, last_run_at, next_run_at)

    def delete_agent_schedules(self, agent_name: str):
        return self._schedule_ops.delete_agent_schedules(agent_name)

    # =========================================================================
    # Schedule Execution Management (delegated to db/schedules.py)
    # =========================================================================

    def create_schedule_execution(self, schedule_id: str, agent_name: str, message: str, triggered_by: str = "schedule"):
        return self._schedule_ops.create_schedule_execution(schedule_id, agent_name, message, triggered_by)

    def update_execution_status(self, execution_id: str, status: str, response: str = None, error: str = None,
                                context_used: int = None, context_max: int = None, cost: float = None, tool_calls: str = None):
        return self._schedule_ops.update_execution_status(execution_id, status, response, error,
                                                          context_used, context_max, cost, tool_calls)

    def get_schedule_executions(self, schedule_id: str, limit: int = 50):
        return self._schedule_ops.get_schedule_executions(schedule_id, limit)

    def get_agent_executions(self, agent_name: str, limit: int = 50):
        return self._schedule_ops.get_agent_executions(agent_name, limit)

    def get_execution(self, execution_id: str):
        return self._schedule_ops.get_execution(execution_id)

    # =========================================================================
    # Git Configuration Management (delegated to db/schedules.py)
    # =========================================================================

    def create_git_config(self, agent_name: str, github_repo: str, working_branch: str, instance_id: str, sync_paths=None):
        return self._schedule_ops.create_git_config(agent_name, github_repo, working_branch, instance_id, sync_paths)

    def get_git_config(self, agent_name: str):
        return self._schedule_ops.get_git_config(agent_name)

    def update_git_sync(self, agent_name: str, commit_sha: str):
        return self._schedule_ops.update_git_sync(agent_name, commit_sha)

    def set_git_sync_enabled(self, agent_name: str, enabled: bool):
        return self._schedule_ops.set_git_sync_enabled(agent_name, enabled)

    def delete_git_config(self, agent_name: str):
        return self._schedule_ops.delete_git_config(agent_name)

    def list_git_enabled_agents(self):
        return self._schedule_ops.list_git_enabled_agents()

    # =========================================================================
    # Chat Session and Message Operations (delegated to db/chat.py)
    # =========================================================================

    def get_or_create_chat_session(self, agent_name: str, user_id: int, user_email: str):
        return self._chat_ops.get_or_create_chat_session(agent_name, user_id, user_email)

    def add_chat_message(self, session_id: str, agent_name: str, user_id: int, user_email: str,
                         role: str, content: str, cost: float = None, context_used: int = None,
                         context_max: int = None, tool_calls: str = None, execution_time_ms: int = None):
        return self._chat_ops.add_chat_message(session_id, agent_name, user_id, user_email,
                                               role, content, cost, context_used, context_max,
                                               tool_calls, execution_time_ms)

    def get_chat_session(self, session_id: str):
        return self._chat_ops.get_chat_session(session_id)

    def get_chat_messages(self, session_id: str, limit: int = 100):
        return self._chat_ops.get_chat_messages(session_id, limit)

    def get_agent_chat_history(self, agent_name: str, user_id: int = None, limit: int = 100):
        return self._chat_ops.get_agent_chat_history(agent_name, user_id, limit)

    def get_agent_chat_sessions(self, agent_name: str, user_id: int = None, status: str = None):
        return self._chat_ops.get_agent_chat_sessions(agent_name, user_id, status)

    def close_chat_session(self, session_id: str):
        return self._chat_ops.close_chat_session(session_id)

    def delete_chat_session(self, session_id: str):
        return self._chat_ops.delete_chat_session(session_id)

    # =========================================================================
    # Activity Stream Methods (delegated to db/activities.py)
    # =========================================================================

    def create_activity(self, activity):
        return self._activity_ops.create_activity(activity)

    def complete_activity(self, activity_id: str, status: str = "completed", details: dict = None, error: str = None):
        return self._activity_ops.complete_activity(activity_id, status, details, error)

    def get_activity(self, activity_id: str):
        return self._activity_ops.get_activity(activity_id)

    def get_agent_activities(self, agent_name: str, activity_type: str = None, activity_state: str = None, limit: int = 100):
        return self._activity_ops.get_agent_activities(agent_name, activity_type, activity_state, limit)

    def get_activities_in_range(self, start_time: str = None, end_time: str = None, activity_types: list = None, limit: int = 100):
        return self._activity_ops.get_activities_in_range(start_time, end_time, activity_types, limit)

    def get_current_activities(self, agent_name: str):
        return self._activity_ops.get_current_activities(agent_name)

    # =========================================================================
    # Agent Permissions (delegated to db/permissions.py) - Phase 9.10
    # =========================================================================

    def get_permitted_agents(self, source_agent: str):
        return self._permission_ops.get_permitted_agents(source_agent)

    def get_permission_details(self, source_agent: str):
        return self._permission_ops.get_permission_details(source_agent)

    def is_agent_permitted(self, source_agent: str, target_agent: str):
        return self._permission_ops.is_permitted(source_agent, target_agent)

    def add_agent_permission(self, source_agent: str, target_agent: str, created_by: str):
        return self._permission_ops.add_permission(source_agent, target_agent, created_by)

    def remove_agent_permission(self, source_agent: str, target_agent: str):
        return self._permission_ops.remove_permission(source_agent, target_agent)

    def set_agent_permissions(self, source_agent: str, target_agents: list, created_by: str):
        return self._permission_ops.set_permissions(source_agent, target_agents, created_by)

    def delete_agent_permissions(self, agent_name: str):
        return self._permission_ops.delete_agent_permissions(agent_name)

    def grant_default_permissions(self, agent_name: str, owner_username: str):
        return self._permission_ops.grant_default_permissions(agent_name, owner_username)

    # =========================================================================
    # Shared Folder Configuration (delegated to db/shared_folders.py) - Phase 9.11
    # =========================================================================

    def get_shared_folder_config(self, agent_name: str):
        return self._shared_folder_ops.get_shared_folder_config(agent_name)

    def upsert_shared_folder_config(self, agent_name: str, expose_enabled=None, consume_enabled=None):
        return self._shared_folder_ops.upsert_shared_folder_config(agent_name, expose_enabled, consume_enabled)

    def delete_shared_folder_config(self, agent_name: str):
        return self._shared_folder_ops.delete_shared_folder_config(agent_name)

    def get_agents_exposing_folders(self):
        return self._shared_folder_ops.get_agents_exposing_folders()

    def get_available_shared_folders(self, requesting_agent: str):
        return self._shared_folder_ops.get_available_shared_folders(requesting_agent)

    def get_consuming_agents(self, source_agent: str):
        return self._shared_folder_ops.get_consuming_agents(source_agent)

    @staticmethod
    def get_shared_volume_name(agent_name: str):
        return SharedFolderOperations.get_shared_volume_name(agent_name)

    @staticmethod
    def get_shared_mount_path(source_agent: str):
        return SharedFolderOperations.get_shared_mount_path(source_agent)

    # =========================================================================
    # System Settings (delegated to db/settings.py)
    # =========================================================================

    def get_setting(self, key: str):
        return self._settings_ops.get_setting(key)

    def get_setting_value(self, key: str, default: str = None):
        return self._settings_ops.get_setting_value(key, default)

    def set_setting(self, key: str, value: str):
        return self._settings_ops.set_setting(key, value)

    def delete_setting(self, key: str):
        return self._settings_ops.delete_setting(key)

    def get_all_settings(self):
        return self._settings_ops.get_all_settings()

    def get_settings_dict(self):
        return self._settings_ops.get_settings_dict()


# Global database manager instance
db = DatabaseManager()
