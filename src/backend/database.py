"""
SQLite persistence layer for Trinity platform.

This module provides the DatabaseManager class - a facade for all database operations.
The actual implementations are organized in submodules under db/:
- db/migrations.py: Schema migrations
- db/schema.py: Table and index definitions
- db/users.py: User management
- db/agents.py: Agent ownership and sharing
- db/mcp_keys.py: MCP API key management
- db/schedules.py: Schedule and execution management
- db/chat.py: Chat session and message persistence
- db/activities.py: Activity stream logging

For backward compatibility, all models and the global `db` instance are
re-exported from this module.

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
    AgentShare,
    AgentShareRequest,
    McpApiKeyCreate,
    McpApiKey,
    McpApiKeyWithSecret,
    ScheduleCreate,
    Schedule,
    ScheduleExecution,
    AgentGitConfig,
    GitSyncResult,
    ChatSession,
    ChatMessage,
    AgentPermission,
    SharedFolderConfig,
    SharedFolderMount,
    SystemSetting,
    SystemSettingUpdate,
    # Public Agent Links (Phase 12.2)
    PublicLinkCreate,
    PublicLink,
    PublicLinkUpdate,
    PublicLinkWithUrl,
    PublicLinkInfo,
    VerificationRequest,
    VerificationConfirm,
    VerificationResponse,
    PublicChatRequest,
    PublicChatResponse,
    # Public Chat Persistence (Phase 12.2.5)
    PublicChatSession,
    PublicChatMessage,
    # Email Authentication (Phase 12.4)
    EmailWhitelistAdd,
    EmailLoginRequest,
    EmailLoginVerify,
    EmailLoginResponse,
    # Agent Notifications (NOTIF-001)
    NotificationCreate,
    Notification,
    NotificationList,
    NotificationAcknowledge,
    # Subscription Credential Models (SUB-001)
    SubscriptionCredentialCreate,
    SubscriptionCredential,
    SubscriptionWithAgents,
    AgentAuthStatus,
    # Agent Event Subscriptions (EVT-001)
    EventSubscriptionCreate,
    EventSubscriptionUpdate,
    EventSubscription,
    EventSubscriptionList,
    AgentEvent,
    AgentEventList,
    # Monitoring Models (MON-001)
    AgentHealthStatus,
    DockerHealthCheck,
    NetworkHealthCheck,
    BusinessHealthCheck,
    AgentHealthDetail,
    AgentHealthSummary,
    FleetHealthSummary,
    FleetHealthStatus,
    MonitoringConfig,
)

# Re-export connection utilities
from db.connection import get_db_connection, DB_PATH
from utils.helpers import utc_now_iso

# Import schema and migration utilities
from db.migrations import run_all_migrations
from db.schema import init_schema

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
from db.public_links import PublicLinkOperations
from db.email_auth import EmailAuthOperations
from db.skills import SkillsOperations
from db.public_chat import PublicChatOperations
from db.tags import TagOperations
from db.system_views import SystemViewOperations
from db.notifications import NotificationOperations
from db.subscriptions import SubscriptionOperations
from db.monitoring import MonitoringOperations
from db.dashboard_history import DashboardHistoryOperations
from db.slack import SlackOperations
from db.slack_channels import SlackChannelOperations
from db.nevermined import NeverminedOperations
from db.operator_queue import OperatorQueueOperations
from db.event_subscriptions import EventSubscriptionOperations
from db.telegram_channels import TelegramChannelOperations
from db.access_requests import AccessRequestOperations
from db.audit import PlatformAuditOperations
from db.sync_state import SyncStateOperations


def init_database():
    """Initialize the SQLite database with all required tables.

    1. Creates database directory if needed
    2. Runs all migrations (idempotent)
    3. Creates schema (tables and indexes)
    4. Ensures admin user exists
    """
    db_path = Path(DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with get_db_connection() as conn:
        cursor = conn.cursor()

        # Run migrations first (upgrade existing DB; skips if tables don't exist yet)
        run_all_migrations(cursor, conn)

        # Create schema (tables and indexes)
        init_schema(cursor, conn)

        # Second pass: record any migrations skipped on fresh install. On first
        # startup the target tables don't exist yet so those migrations are
        # skipped-but-not-recorded. init_schema has now created them with the
        # correct current schema, so re-running is a no-op — but it records
        # them, keeping the health check accurate.
        run_all_migrations(cursor, conn)

        # Create default admin user if not exists
        _ensure_admin_user(cursor, conn)


def _ensure_admin_user(cursor, conn):
    """Ensure the admin user exists with properly hashed password."""
    admin_password = os.getenv("ADMIN_PASSWORD", "")
    admin_username = os.getenv("ADMIN_USERNAME", "admin")

    # Warn operators if ADMIN_PASSWORD doesn't meet complexity requirements
    if admin_password:
        try:
            from utils.password_validation import validate_password_strength
            pw_errors = validate_password_strength(admin_password)
            if pw_errors:
                print(f"WARNING: ADMIN_PASSWORD does not meet complexity requirements:")
                for err in pw_errors:
                    print(f"         - {err}")
                print("         Recommended: 12+ chars with uppercase, lowercase, digits, and special characters")
        except ImportError:
            pass  # password_validation module not available during early init

    cursor.execute("SELECT id, password_hash FROM users WHERE username = ?", (admin_username,))
    existing = cursor.fetchone()

    if existing is None:
        # Create admin user
        if not admin_password:
            print("WARNING: ADMIN_PASSWORD not set - skipping admin user creation")
            print("         Set ADMIN_PASSWORD environment variable to create admin user")
            return

        now = utc_now_iso()
        # Hash password using bcrypt
        from passlib.context import CryptContext
        pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
        hashed = pwd_context.hash(admin_password)

        cursor.execute("""
            INSERT INTO users (username, password_hash, role, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
        """, (admin_username, hashed, "admin", now, now))
        conn.commit()
        print(f"Created admin user '{admin_username}' with hashed password")
    else:
        # Check if existing password needs update (migration or change)
        existing_hash = existing[1]
        should_update = False

        from passlib.context import CryptContext
        pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

        if existing_hash and not existing_hash.startswith("$2"):
            # Password is likely plaintext (bcrypt hashes start with $2)
            if admin_password and existing_hash == admin_password:
                should_update = True
                print(f"Migrating admin user '{admin_username}' password from plaintext to bcrypt")
        elif admin_password:
            # Check if environment password matches DB hash
            try:
                if not pwd_context.verify(admin_password, existing_hash):
                    should_update = True
                    print(f"Environment password changed - updating admin user '{admin_username}'")
            except Exception as e:
                print(f"Error verifying password hash: {e}")
                should_update = True

        if should_update and admin_password:
            hashed = pwd_context.hash(admin_password)
            cursor.execute("""
                UPDATE users SET password_hash = ?, updated_at = ?
                WHERE username = ?
            """, (hashed, utc_now_iso(), admin_username))
            conn.commit()


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
        self._public_link_ops = PublicLinkOperations(self._user_ops, self._agent_ops)
        self._email_auth_ops = EmailAuthOperations(self._user_ops)
        self._skills_ops = SkillsOperations()
        self._public_chat_ops = PublicChatOperations()
        self._tag_ops = TagOperations()
        self._system_view_ops = SystemViewOperations()
        self._notification_ops = NotificationOperations()
        self._subscription_ops = SubscriptionOperations()
        self._monitoring_ops = MonitoringOperations()
        self._dashboard_history_ops = DashboardHistoryOperations()
        self._slack_ops = SlackOperations()
        self._slack_channel_ops = SlackChannelOperations()
        self._nevermined_ops = NeverminedOperations()
        self._operator_queue_ops = OperatorQueueOperations()
        self._event_subscription_ops = EventSubscriptionOperations()
        self._telegram_channel_ops = TelegramChannelOperations()
        self._access_request_ops = AccessRequestOperations()
        self._audit_ops = PlatformAuditOperations()
        self._sync_state_ops = SyncStateOperations()  # #389 sync health

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

    def update_user_password(self, username: str, hashed_password: str):
        return self._user_ops.update_user_password(username, hashed_password)

    def get_or_create_auth0_user(self, auth0_sub: str, email: str, name: str = None, picture: str = None):
        return self._user_ops.get_or_create_auth0_user(auth0_sub, email, name, picture)

    def list_users(self):
        return self._user_ops.list_users()

    def update_user_role(self, username: str, role: str):
        return self._user_ops.update_user_role(username, role)

    # =========================================================================
    # Agent Ownership Management (delegated to db/agents.py)
    # =========================================================================

    def register_agent_owner(self, agent_name: str, owner_username: str, is_system: bool = False):
        return self._agent_ops.register_agent_owner(agent_name, owner_username, is_system)

    def get_agent_owner(self, agent_name: str):
        return self._agent_ops.get_agent_owner(agent_name)

    def get_agents_by_owner(self, owner_username: str):
        return self._agent_ops.get_agents_by_owner(owner_username)

    def delete_agent_ownership(self, agent_name: str):
        return self._agent_ops.delete_agent_ownership(agent_name)

    def rename_agent(self, old_name: str, new_name: str):
        return self._agent_ops.rename_agent(old_name, new_name)


    def can_user_access_agent(self, username: str, agent_name: str):
        return self._agent_ops.can_user_access_agent(username, agent_name)

    def can_user_delete_agent(self, username: str, agent_name: str):
        return self._agent_ops.can_user_delete_agent(username, agent_name)

    def can_user_rename_agent(self, username: str, agent_name: str):
        return self._agent_ops.can_user_rename_agent(username, agent_name)

    def is_system_agent(self, agent_name: str):
        return self._agent_ops.is_system_agent(agent_name)

    # Access policy (issue #311)
    def get_access_policy(self, agent_name: str):
        return self._agent_ops.get_access_policy(agent_name)

    def set_access_policy(
        self, agent_name: str, require_email: bool, open_access: bool, group_auth_mode: str = "none"
    ):
        return self._agent_ops.set_access_policy(agent_name, require_email, open_access, group_auth_mode)

    def email_has_agent_access(self, agent_name: str, email: str):
        return self._agent_ops.email_has_agent_access(agent_name, email)

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
    # Proactive Messaging (Issue #321) - delegated to db/agents.py
    # =========================================================================

    def can_agent_message_email(self, agent_name: str, email: str):
        """Check if agent can send proactive messages to this email."""
        return self._agent_ops.can_agent_message_email(agent_name, email)

    def set_allow_proactive(self, agent_name: str, email: str, allow: bool, setter_username: str):
        """Update allow_proactive flag for a sharing record."""
        return self._agent_ops.set_allow_proactive(agent_name, email, allow, setter_username)

    def get_proactive_enabled_shares(self, agent_name: str):
        """Get all emails that have opted in to proactive messages from this agent."""
        return self._agent_ops.get_proactive_enabled_shares(agent_name)

    # =========================================================================
    # Agent API Key Settings (delegated to db/agents.py)
    # =========================================================================

    def get_use_platform_api_key(self, agent_name: str):
        return self._agent_ops.get_use_platform_api_key(agent_name)

    def set_use_platform_api_key(self, agent_name: str, use_platform_key: bool):
        return self._agent_ops.set_use_platform_api_key(agent_name, use_platform_key)

    # =========================================================================
    # Agent Autonomy Mode (delegated to db/agents.py)
    # =========================================================================

    def get_autonomy_enabled(self, agent_name: str):
        return self._agent_ops.get_autonomy_enabled(agent_name)

    def set_autonomy_enabled(self, agent_name: str, enabled: bool):
        return self._agent_ops.set_autonomy_enabled(agent_name, enabled)

    def get_all_agents_autonomy_status(self):
        return self._agent_ops.get_all_agents_autonomy_status()

    # =========================================================================
    # Batch Metadata Query (N+1 Fix) - delegated to db/agents.py
    # =========================================================================

    def get_all_agent_metadata(self, user_email: str = None):
        return self._agent_ops.get_all_agent_metadata(user_email)

    def get_accessible_agent_names(self, user_email: str, is_admin: bool = False):
        """Get list of agent names the user can access (owned + shared, or all if admin)."""
        return self._agent_ops.get_accessible_agent_names(user_email, is_admin)

    # =========================================================================
    # Agent Resource Limits (delegated to db/agents.py)
    # =========================================================================

    def get_resource_limits(self, agent_name: str):
        return self._agent_ops.get_resource_limits(agent_name)

    def set_resource_limits(self, agent_name: str, memory: str = None, cpu: str = None):
        return self._agent_ops.set_resource_limits(agent_name, memory, cpu)

    # =========================================================================
    # Agent Read-Only Mode (delegated to db/agents.py)
    # =========================================================================

    def get_read_only_mode(self, agent_name: str):
        return self._agent_ops.get_read_only_mode(agent_name)

    def set_read_only_mode(self, agent_name: str, enabled: bool, config: dict = None):
        return self._agent_ops.set_read_only_mode(agent_name, enabled, config)

    # =========================================================================
    # Agent Guardrails (GUARD-001)
    # =========================================================================

    def get_guardrails_config(self, agent_name: str) -> dict:
        return self._agent_ops.get_guardrails_config(agent_name)

    def set_guardrails_config(self, agent_name: str, config: dict = None) -> bool:
        return self._agent_ops.set_guardrails_config(agent_name, config)

    # =========================================================================
    # Parallel Capacity (delegated to db/agents.py) - CAPACITY-001
    # =========================================================================

    def get_max_parallel_tasks(self, agent_name: str):
        return self._agent_ops.get_max_parallel_tasks(agent_name)

    def set_max_parallel_tasks(self, agent_name: str, max_tasks: int):
        return self._agent_ops.set_max_parallel_tasks(agent_name, max_tasks)

    def get_all_agents_parallel_capacity(self):
        return self._agent_ops.get_all_agents_parallel_capacity()

    # =========================================================================
    # Execution Timeout (delegated to db/agents.py) - TIMEOUT-001
    # =========================================================================

    def get_execution_timeout(self, agent_name: str) -> int:
        return self._agent_ops.get_execution_timeout(agent_name)

    def get_all_execution_timeouts(self) -> dict:
        return self._agent_ops.get_all_execution_timeouts()

    def set_execution_timeout(self, agent_name: str, timeout_seconds: int) -> bool:
        return self._agent_ops.set_execution_timeout(agent_name, timeout_seconds)

    # =========================================================================
    # Backlog Depth (delegated to db/agent_settings/resources.py) - BACKLOG-001
    # =========================================================================

    def get_max_backlog_depth(self, agent_name: str) -> int:
        return self._agent_ops.get_max_backlog_depth(agent_name)

    def set_max_backlog_depth(self, agent_name: str, depth: int) -> bool:
        return self._agent_ops.set_max_backlog_depth(agent_name, depth)

    # =========================================================================
    # Backlog Execution Queries (delegated to db/schedules.py) - BACKLOG-001
    # =========================================================================

    def update_execution_to_queued(self, execution_id: str, backlog_metadata: str, queued_at: str) -> bool:
        return self._schedule_ops.update_execution_to_queued(execution_id, backlog_metadata, queued_at)

    def claim_next_queued(self, agent_name: str):
        return self._schedule_ops.claim_next_queued(agent_name)

    def release_claim_to_queued(self, execution_id: str) -> bool:
        return self._schedule_ops.release_claim_to_queued(execution_id)

    def get_queued_count(self, agent_name: str) -> int:
        return self._schedule_ops.get_queued_count(agent_name)

    def cancel_queued_execution(self, execution_id: str, reason: str = "cancelled") -> bool:
        return self._schedule_ops.cancel_queued_execution(execution_id, reason)

    def cancel_queued_for_agent(self, agent_name: str, reason: str = "agent_deleted") -> int:
        return self._schedule_ops.cancel_queued_for_agent(agent_name, reason)

    def expire_stale_queued(self, max_age_hours: float = 24) -> int:
        return self._schedule_ops.expire_stale_queued(max_age_hours)

    def list_agents_with_queued(self):
        return self._schedule_ops.list_agents_with_queued()

    # =========================================================================
    # Avatar Identity (delegated to db/agents.py) - AVATAR-001
    # =========================================================================

    def set_avatar_identity(self, agent_name: str, prompt: str, updated_at: str):
        return self._agent_ops.set_avatar_identity(agent_name, prompt, updated_at)

    def get_avatar_identity(self, agent_name: str):
        return self._agent_ops.get_avatar_identity(agent_name)

    def clear_avatar_identity(self, agent_name: str):
        return self._agent_ops.clear_avatar_identity(agent_name)

    def get_agents_without_custom_avatar(self):
        return self._agent_ops.get_agents_without_custom_avatar()

    def set_default_avatar(self, agent_name: str, identity_prompt: str, updated_at: str):
        return self._agent_ops.set_default_avatar(agent_name, identity_prompt, updated_at)

    # =========================================================================
    # GitHub PAT (delegated to db/agents.py) - #347
    # =========================================================================

    def get_agent_github_pat(self, agent_name: str):
        return self._agent_ops.get_agent_github_pat(agent_name)

    def set_agent_github_pat(self, agent_name: str, pat: str) -> bool:
        return self._agent_ops.set_agent_github_pat(agent_name, pat)

    def clear_agent_github_pat(self, agent_name: str) -> bool:
        return self._agent_ops.clear_agent_github_pat(agent_name)

    def has_agent_github_pat(self, agent_name: str) -> bool:
        return self._agent_ops.has_agent_github_pat(agent_name)

    # =========================================================================
    # Voice System Prompt (delegated to db/agents.py) - VOICE-005
    # =========================================================================

    def get_voice_system_prompt(self, agent_name: str):
        return self._agent_ops.get_voice_system_prompt(agent_name)

    def set_voice_system_prompt(self, agent_name: str, prompt: str):
        return self._agent_ops.set_voice_system_prompt(agent_name, prompt)

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

    def list_all_disabled_schedules(self):
        return self._schedule_ops.list_all_disabled_schedules()

    def list_all_schedules(self):
        """List all schedules across all agents."""
        return self._schedule_ops.list_all_schedules()

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

    def create_task_execution(
        self,
        agent_name: str,
        message: str,
        triggered_by: str = "manual",
        source_user_id: int = None,
        source_user_email: str = None,
        source_agent_name: str = None,
        source_mcp_key_id: str = None,
        source_mcp_key_name: str = None,
        model_used: str = None,
        fan_out_id: str = None,
        subscription_id: str = None,
    ):
        """Create an execution record for a manual/API-triggered task (no schedule)."""
        return self._schedule_ops.create_task_execution(
            agent_name, message, triggered_by,
            source_user_id=source_user_id,
            source_user_email=source_user_email,
            source_agent_name=source_agent_name,
            source_mcp_key_id=source_mcp_key_id,
            source_mcp_key_name=source_mcp_key_name,
            model_used=model_used,
            fan_out_id=fan_out_id,
            subscription_id=subscription_id,
        )

    def create_schedule_execution(
        self,
        schedule_id: str,
        agent_name: str,
        message: str,
        triggered_by: str = "schedule",
        source_user_id: int = None,
        source_user_email: str = None,
        source_agent_name: str = None,
        source_mcp_key_id: str = None,
        source_mcp_key_name: str = None,
        subscription_id: str = None,
    ):
        return self._schedule_ops.create_schedule_execution(
            schedule_id, agent_name, message, triggered_by,
            source_user_id=source_user_id,
            source_user_email=source_user_email,
            source_agent_name=source_agent_name,
            source_mcp_key_id=source_mcp_key_id,
            source_mcp_key_name=source_mcp_key_name,
            subscription_id=subscription_id,
        )

    def update_execution_status(self, execution_id: str, status: str, response: str = None, error: str = None,
                                context_used: int = None, context_max: int = None, cost: float = None, tool_calls: str = None, execution_log: str = None,
                                claude_session_id: str = None):
        return self._schedule_ops.update_execution_status(execution_id, status, response, error,
                                                          context_used, context_max, cost, tool_calls, execution_log, claude_session_id)

    def mark_execution_dispatched(self, execution_id: str) -> bool:
        return self._schedule_ops.mark_execution_dispatched(execution_id)

    def get_schedule_executions(self, schedule_id: str, limit: int = 50):
        return self._schedule_ops.get_schedule_executions(schedule_id, limit)

    def get_agent_executions(self, agent_name: str, limit: int = 50):
        return self._schedule_ops.get_agent_executions(agent_name, limit)

    def get_agent_executions_summary(self, agent_name: str, limit: int = 50):
        """Get execution summaries for list view - excludes large text fields.

        PERF-001: Task List Performance Optimization
        """
        return self._schedule_ops.get_agent_executions_summary(agent_name, limit)

    def get_execution(self, execution_id: str):
        return self._schedule_ops.get_execution(execution_id)

    def get_all_agents_execution_stats(self, hours: int = 24):
        """Get execution statistics for all agents."""
        return self._schedule_ops.get_all_agents_execution_stats(hours)

    def get_all_agents_execution_stats_dual(self):
        """Get execution statistics for all agents with both 24h and 7d windows."""
        return self._schedule_ops.get_all_agents_execution_stats_dual()

    def get_all_agents_schedule_counts(self):
        """Get schedule counts (total and enabled) for all agents."""
        return self._schedule_ops.get_all_agents_schedule_counts()

    # =========================================================================
    # Git Configuration Management (delegated to db/schedules.py)
    # =========================================================================

    def create_git_config(
        self,
        agent_name: str,
        github_repo: str,
        working_branch: str,
        instance_id: str,
        sync_paths=None,
        source_branch: str = "main",
        source_mode: bool = False
    ):
        return self._schedule_ops.create_git_config(
            agent_name, github_repo, working_branch, instance_id, sync_paths,
            source_branch=source_branch, source_mode=source_mode
        )

    def get_git_config(self, agent_name: str):
        return self._schedule_ops.get_git_config(agent_name)

    def update_git_sync(self, agent_name: str, commit_sha: str):
        return self._schedule_ops.update_git_sync(agent_name, commit_sha)

    def set_git_sync_enabled(self, agent_name: str, enabled: bool):
        return self._schedule_ops.set_git_sync_enabled(agent_name, enabled)

    # #389 sync health observability
    def set_git_auto_sync_enabled(self, agent_name: str, enabled: bool):
        return self._schedule_ops.set_git_auto_sync_enabled(agent_name, enabled)

    def set_freeze_schedules_if_sync_failing(self, agent_name: str, enabled: bool):
        return self._schedule_ops.set_freeze_schedules_if_sync_failing(agent_name, enabled)

    def get_git_auto_sync_enabled(self, agent_name: str):
        return self._schedule_ops.get_git_auto_sync_enabled(agent_name)

    def get_freeze_schedules_if_sync_failing(self, agent_name: str):
        return self._schedule_ops.get_freeze_schedules_if_sync_failing(agent_name)

    def delete_git_config(self, agent_name: str):
        return self._schedule_ops.delete_git_config(agent_name)

    def list_git_enabled_agents(self):
        return self._schedule_ops.list_git_enabled_agents()

    def find_duplicate_bindings(self):
        """#390 S6: agents sharing (repo, working_branch), excluding source_mode."""
        return self._schedule_ops.find_duplicate_bindings()

    # =========================================================================
    # Sync State Operations (#389 — delegated to db/sync_state.py)
    # =========================================================================

    def get_sync_state(self, agent_name: str):
        return self._sync_state_ops.get(agent_name)

    def list_sync_states(self):
        return self._sync_state_ops.list_all()

    def upsert_sync_state(self, agent_name: str, **fields):
        return self._sync_state_ops.upsert(agent_name, **fields)

    def delete_sync_state(self, agent_name: str):
        return self._sync_state_ops.delete(agent_name)

    # =========================================================================
    # Chat Session and Message Operations (delegated to db/chat.py)
    # =========================================================================

    def get_or_create_chat_session(self, agent_name: str, user_id: int, user_email: str, subscription_id: str = None):
        return self._chat_ops.get_or_create_chat_session(agent_name, user_id, user_email, subscription_id=subscription_id)

    def add_chat_message(self, session_id: str, agent_name: str, user_id: int, user_email: str,
                         role: str, content: str, cost: float = None, context_used: int = None,
                         context_max: int = None, tool_calls: str = None, execution_time_ms: int = None,
                         source: str = "text", subscription_id: str = None, output_tokens: int = None):
        return self._chat_ops.add_chat_message(session_id, agent_name, user_id, user_email,
                                               role, content, cost, context_used, context_max,
                                               tool_calls, execution_time_ms, source=source,
                                               subscription_id=subscription_id, output_tokens=output_tokens)

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

    def create_new_chat_session(self, agent_name: str, user_id: int, user_email: str, subscription_id: str = None):
        return self._chat_ops.create_new_chat_session(agent_name, user_id, user_email, subscription_id=subscription_id)

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
    # Cleanup Operations (for CleanupService)
    # =========================================================================

    def get_running_executions(self):
        """Get all schedule executions currently in 'running' status."""
        return self._schedule_ops.get_running_executions()

    def mark_stale_executions_failed(self, timeout_minutes: int = 30):
        """Mark executions stuck in 'running' past threshold as failed."""
        return self._schedule_ops.mark_stale_executions_failed(timeout_minutes)

    def mark_no_session_executions_failed(self, timeout_seconds: int = 60):
        """Mark running executions with no claude_session_id as failed (Issue #106)."""
        return self._schedule_ops.mark_no_session_executions_failed(timeout_seconds)

    def fail_stale_slot_execution(self, execution_id: str, error: str) -> bool:
        """Mark a running execution as failed when its slot is reclaimed (#219)."""
        return self._schedule_ops.fail_stale_slot_execution(execution_id, error)

    def finalize_orphaned_skipped_executions(self):
        """Finalize skipped executions missing completed_at (Issue #106)."""
        return self._schedule_ops.finalize_orphaned_skipped_executions()

    def get_running_executions_with_agent_info(self):
        """Get all running executions with schedule timeout info (Issue #129)."""
        return self._schedule_ops.get_running_executions_with_agent_info()

    def mark_execution_failed_by_watchdog(self, execution_id: str, error_message: str) -> bool:
        """Mark a running execution as failed by watchdog with race guard (Issue #129)."""
        return self._schedule_ops.mark_execution_failed_by_watchdog(execution_id, error_message)

    def mark_stale_activities_failed(self, timeout_minutes: int = 30):
        """Mark activities stuck in 'started' past threshold as failed."""
        return self._activity_ops.mark_stale_activities_failed(timeout_minutes)

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

    def get_all_permission_edges(self, accessible_agents: set = None):
        return self._permission_ops.get_all_permission_edges(accessible_agents)

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

    # =========================================================================
    # Public Agent Links (delegated to db/public_links.py) - Phase 12.2
    # =========================================================================

    def create_public_link(self, agent_name: str, created_by: str, name: str = None,
                           expires_at: str = None):
        return self._public_link_ops.create_public_link(agent_name, created_by, name, expires_at)

    def get_public_link(self, link_id: str):
        return self._public_link_ops.get_public_link(link_id)

    def get_public_link_by_token(self, token: str):
        return self._public_link_ops.get_public_link_by_token(token)

    def list_agent_public_links(self, agent_name: str):
        return self._public_link_ops.list_agent_public_links(agent_name)

    def update_public_link(self, link_id: str, name: str = None, enabled: bool = None,
                           expires_at: str = None):
        return self._public_link_ops.update_public_link(link_id, name, enabled, expires_at)

    def delete_public_link(self, link_id: str):
        return self._public_link_ops.delete_public_link(link_id)

    def delete_agent_public_links(self, agent_name: str):
        return self._public_link_ops.delete_agent_public_links(agent_name)

    def is_public_link_valid(self, token: str):
        return self._public_link_ops.is_link_valid(token)

    # Email verification methods
    def create_verification(self, link_id: str, email: str, expiry_minutes: int = 10):
        return self._public_link_ops.create_verification(link_id, email, expiry_minutes)

    def verify_code(self, link_id: str, email: str, code: str, session_hours: int = 24):
        return self._public_link_ops.verify_code(link_id, email, code, session_hours)

    def validate_session(self, link_id: str, session_token: str):
        return self._public_link_ops.validate_session(link_id, session_token)

    def count_recent_verification_requests(self, email: str, minutes: int = 10):
        return self._public_link_ops.count_recent_verification_requests(email, minutes)

    # Usage tracking methods
    def record_public_link_usage(self, link_id: str, email: str = None, ip_address: str = None):
        return self._public_link_ops.record_usage(link_id, email, ip_address)

    def get_public_link_usage_stats(self, link_id: str):
        return self._public_link_ops.get_link_usage_stats(link_id)

    def count_recent_messages_by_ip(self, ip_address: str, minutes: int = 1):
        return self._public_link_ops.count_recent_messages_by_ip(ip_address, minutes)

    def count_recent_messages_by_token(self, link_id: str, minutes: int = 1):
        return self._public_link_ops.count_recent_messages_by_token(link_id, minutes)

    # =========================================================================
    # Email Authentication (delegated to db/email_auth.py) - Phase 12.4
    # =========================================================================

    def is_email_whitelisted(self, email: str):
        return self._email_auth_ops.is_email_whitelisted(email)

    def add_to_whitelist(self, email: str, added_by: str, source: str, *, default_role: str):
        return self._email_auth_ops.add_to_whitelist(
            email, added_by, source, default_role=default_role
        )

    def get_whitelist_default_role(self, email: str):
        return self._email_auth_ops.get_whitelist_default_role(email)

    def remove_from_whitelist(self, email: str):
        return self._email_auth_ops.remove_from_whitelist(email)

    def list_whitelist(self, limit: int = 100):
        return self._email_auth_ops.list_whitelist(limit)

    def create_login_code(self, email: str, expiry_minutes: int = 10):
        return self._email_auth_ops.create_login_code(email, expiry_minutes)

    def verify_login_code(self, email: str, code: str):
        return self._email_auth_ops.verify_login_code(email, code)

    def count_recent_code_requests(self, email: str, minutes: int = 10):
        return self._email_auth_ops.count_recent_code_requests(email, minutes)

    def cleanup_old_codes(self, days: int = 1):
        return self._email_auth_ops.cleanup_old_codes(days)

    def get_or_create_email_user(self, email: str):
        return self._email_auth_ops.get_or_create_email_user(email)

    # =========================================================================
    # Agent Skills (delegated to db/skills.py) - Skills Management System
    # =========================================================================

    def get_agent_skills(self, agent_name: str):
        return self._skills_ops.get_agent_skills(agent_name)

    def get_agent_skill_names(self, agent_name: str):
        return self._skills_ops.get_agent_skill_names(agent_name)

    def assign_skill(self, agent_name: str, skill_name: str, assigned_by: str):
        return self._skills_ops.assign_skill(agent_name, skill_name, assigned_by)

    def unassign_skill(self, agent_name: str, skill_name: str):
        return self._skills_ops.unassign_skill(agent_name, skill_name)

    def set_agent_skills(self, agent_name: str, skill_names: list, assigned_by: str):
        return self._skills_ops.set_agent_skills(agent_name, skill_names, assigned_by)

    def delete_agent_skills(self, agent_name: str):
        return self._skills_ops.delete_agent_skills(agent_name)

    def is_skill_assigned(self, agent_name: str, skill_name: str):
        return self._skills_ops.is_skill_assigned(agent_name, skill_name)

    def get_agents_with_skill(self, skill_name: str):
        return self._skills_ops.get_agents_with_skill(skill_name)

    # =========================================================================
    # Public Chat Sessions (delegated to db/public_chat.py) - Phase 12.2.5
    # =========================================================================

    def get_or_create_public_chat_session(self, link_id: str, session_identifier: str, identifier_type: str):
        return self._public_chat_ops.get_or_create_session(link_id, session_identifier, identifier_type)

    def get_public_chat_session_by_identifier(self, link_id: str, session_identifier: str):
        return self._public_chat_ops.get_session_by_identifier(link_id, session_identifier)

    def get_public_chat_session(self, session_id: str):
        return self._public_chat_ops.get_session(session_id)

    def add_public_chat_message(self, session_id: str, role: str, content: str, cost: float = None):
        return self._public_chat_ops.add_message(session_id, role, content, cost)

    def get_public_chat_messages(self, session_id: str, limit: int = 20):
        return self._public_chat_ops.get_session_messages(session_id, limit)

    def get_recent_public_chat_messages(self, session_id: str, limit: int = 20):
        return self._public_chat_ops.get_recent_messages(session_id, limit)

    def clear_public_chat_session(self, session_id: str):
        return self._public_chat_ops.clear_session(session_id)

    def build_public_chat_context(self, session_id: str, new_message: str, max_turns: int = 10):
        return self._public_chat_ops.build_context_prompt(session_id, new_message, max_turns)

    def delete_public_link_sessions(self, link_id: str):
        return self._public_chat_ops.delete_link_sessions(link_id)

    # Public User Memory (MEM-001)
    def get_or_create_public_user_memory(self, agent_name: str, user_email: str) -> dict:
        return self._public_link_ops.get_or_create_user_memory(agent_name, user_email)

    def increment_public_user_memory_count(self, agent_name: str, user_email: str) -> int:
        return self._public_link_ops.increment_message_count(agent_name, user_email)

    def update_public_user_memory(self, agent_name: str, user_email: str, memory_text: str) -> bool:
        return self._public_link_ops.update_user_memory(agent_name, user_email, memory_text)

    # =========================================================================
    # Agent Tags (delegated to db/tags.py) - ORG-001
    # =========================================================================

    def get_agent_tags(self, agent_name: str):
        return self._tag_ops.get_agent_tags(agent_name)

    def set_agent_tags(self, agent_name: str, tags: list):
        return self._tag_ops.set_agent_tags(agent_name, tags)

    def add_agent_tag(self, agent_name: str, tag: str):
        return self._tag_ops.add_tag(agent_name, tag)

    def remove_agent_tag(self, agent_name: str, tag: str):
        return self._tag_ops.remove_tag(agent_name, tag)

    def list_all_tags(self):
        return self._tag_ops.list_all_tags()

    def get_agents_by_tag(self, tag: str):
        return self._tag_ops.get_agents_by_tag(tag)

    def get_agents_by_tags(self, tags: list):
        return self._tag_ops.get_agents_by_tags(tags)

    def delete_agent_tags(self, agent_name: str):
        return self._tag_ops.delete_agent_tags(agent_name)

    def get_tags_for_agents(self, agent_names: list):
        return self._tag_ops.get_tags_for_agents(agent_names)

    # =========================================================================
    # System Views (delegated to db/system_views.py) - ORG-001 Phase 2
    # =========================================================================

    def create_system_view(self, owner_id: str, data):
        return self._system_view_ops.create_view(owner_id, data)

    def get_system_view(self, view_id: str):
        return self._system_view_ops.get_view(view_id)

    def list_user_system_views(self, user_id: str):
        return self._system_view_ops.list_user_views(user_id)

    def list_all_system_views(self):
        return self._system_view_ops.list_all_views()

    def update_system_view(self, view_id: str, data):
        return self._system_view_ops.update_view(view_id, data)

    def delete_system_view(self, view_id: str):
        return self._system_view_ops.delete_view(view_id)

    def can_user_view_system_view(self, user_id: str, view_id: str):
        return self._system_view_ops.can_user_view(user_id, view_id)

    def can_user_edit_system_view(self, user_id: str, view_id: str, is_admin: bool = False):
        return self._system_view_ops.can_user_edit_view(user_id, view_id, is_admin)

    # =========================================================================
    # Agent Notifications (delegated to db/notifications.py) - NOTIF-001
    # =========================================================================

    def create_notification(self, agent_name: str, data):
        return self._notification_ops.create_notification(agent_name, data)

    def get_notification(self, notification_id: str):
        return self._notification_ops.get_notification(notification_id)

    def list_notifications(self, agent_name=None, status=None, priority=None, category=None, limit=100):
        return self._notification_ops.list_notifications(agent_name, status, priority, category, limit)

    def list_agent_notifications(self, agent_name: str, status=None, limit=50):
        return self._notification_ops.list_agent_notifications(agent_name, status, limit)

    def acknowledge_notification(self, notification_id: str, acknowledged_by: str):
        return self._notification_ops.acknowledge_notification(notification_id, acknowledged_by)

    def dismiss_notification(self, notification_id: str, dismissed_by: str):
        return self._notification_ops.dismiss_notification(notification_id, dismissed_by)

    def delete_agent_notifications(self, agent_name: str):
        return self._notification_ops.delete_agent_notifications(agent_name)

    def count_pending_notifications(self, agent_name=None):
        return self._notification_ops.count_pending_notifications(agent_name)

    # =========================================================================
    # Subscription Credentials (delegated to db/subscriptions.py) - SUB-002
    # =========================================================================

    def create_subscription(self, name: str, token: str, owner_id: int,
                            subscription_type: str = None, rate_limit_tier: str = None):
        return self._subscription_ops.create_subscription(
            name, token, owner_id, subscription_type, rate_limit_tier
        )

    def get_subscription(self, subscription_id: str):
        return self._subscription_ops.get_subscription(subscription_id)

    def get_subscription_by_name(self, name: str):
        return self._subscription_ops.get_subscription_by_name(name)

    def get_subscription_token(self, subscription_id: str):
        return self._subscription_ops.get_subscription_token(subscription_id)

    def list_subscriptions(self, owner_id: int = None):
        return self._subscription_ops.list_subscriptions(owner_id)

    def list_subscriptions_with_agents(self, owner_id: int = None):
        return self._subscription_ops.list_subscriptions_with_agents(owner_id)

    def delete_subscription(self, subscription_id: str):
        return self._subscription_ops.delete_subscription(subscription_id)

    def assign_subscription_to_agent(self, agent_name: str, subscription_id: str):
        return self._subscription_ops.assign_subscription_to_agent(agent_name, subscription_id)

    def clear_agent_subscription(self, agent_name: str):
        return self._subscription_ops.clear_agent_subscription(agent_name)

    def get_agent_subscription(self, agent_name: str):
        return self._subscription_ops.get_agent_subscription(agent_name)

    def get_agents_by_subscription(self, subscription_id: str):
        return self._subscription_ops.get_agents_by_subscription(subscription_id)

    def get_agent_subscription_id(self, agent_name: str):
        return self._subscription_ops.get_agent_subscription_id(agent_name)

    def get_least_used_subscription(self):
        return self._subscription_ops.get_least_used_subscription()

    # --- SUB-003: Rate-Limit Tracking ---

    def record_rate_limit_event(self, agent_name: str, subscription_id: str, error_message: str = ""):
        return self._subscription_ops.record_rate_limit_event(agent_name, subscription_id, error_message)

    def is_subscription_rate_limited(self, subscription_id: str):
        return self._subscription_ops.is_subscription_rate_limited(subscription_id)

    def clear_rate_limit_events(self, agent_name: str, subscription_id: str):
        return self._subscription_ops.clear_rate_limit_events(agent_name, subscription_id)

    def cleanup_old_rate_limit_events(self):
        return self._subscription_ops.cleanup_old_rate_limit_events()

    def select_best_alternative_subscription(self, current_subscription_id: str):
        return self._subscription_ops.select_best_alternative_subscription(current_subscription_id)

    def get_subscription_usage(self, subscription_id: str):
        """Return rolling usage totals for a subscription (SUB-004)."""
        return self._subscription_ops.get_subscription_usage(subscription_id)

    # =========================================================================
    # Agent Monitoring (delegated to db/monitoring.py) - MON-001
    # =========================================================================

    def create_health_check(self, agent_name: str, check_type: str, status: str,
                            docker_metrics: dict = None, network_metrics: dict = None,
                            business_metrics: dict = None, error_message: str = None):
        return self._monitoring_ops.create_health_check(
            agent_name, check_type, status, docker_metrics,
            network_metrics, business_metrics, error_message
        )

    def get_latest_health_check(self, agent_name: str, check_type: str = "aggregate"):
        return self._monitoring_ops.get_latest_health_check(agent_name, check_type)

    def get_agent_health_history(self, agent_name: str, check_type: str = "aggregate",
                                  hours: int = 24, limit: int = 100):
        return self._monitoring_ops.get_agent_health_history(agent_name, check_type, hours, limit)

    def get_all_latest_health_checks(self, agent_names: list = None, check_type: str = "aggregate"):
        return self._monitoring_ops.get_all_latest_health_checks(agent_names, check_type)

    def get_health_summary(self, agent_names: list = None):
        return self._monitoring_ops.get_health_summary(agent_names)

    def calculate_uptime_percent(self, agent_name: str, hours: int = 24):
        return self._monitoring_ops.calculate_uptime_percent(agent_name, hours)

    def calculate_avg_latency(self, agent_name: str, hours: int = 24):
        return self._monitoring_ops.calculate_avg_latency(agent_name, hours)

    def cleanup_old_health_records(self, days: int = 7):
        return self._monitoring_ops.cleanup_old_records(days)

    def get_alert_cooldown(self, agent_name: str, condition: str):
        return self._monitoring_ops.get_cooldown(agent_name, condition)

    def set_alert_cooldown(self, agent_name: str, condition: str):
        return self._monitoring_ops.set_cooldown(agent_name, condition)

    def clear_alert_cooldown(self, agent_name: str, condition: str):
        return self._monitoring_ops.clear_cooldown(agent_name, condition)

    def is_in_alert_cooldown(self, agent_name: str, condition: str, cooldown_seconds: int):
        return self._monitoring_ops.is_in_cooldown(agent_name, condition, cooldown_seconds)

    def cleanup_alert_cooldowns(self, agent_name: str = None):
        return self._monitoring_ops.cleanup_cooldowns(agent_name)

    # =========================================================================
    # Dashboard History (delegated to db/dashboard_history.py) - DASH-001
    # =========================================================================

    def capture_dashboard_snapshot(self, agent_name: str, config: dict, dashboard_mtime: str):
        return self._dashboard_history_ops.capture_dashboard_snapshot(agent_name, config, dashboard_mtime)

    def get_widget_history(self, agent_name: str, widget_key: str, hours: int = 24, limit: int = 100):
        return self._dashboard_history_ops.get_widget_history(agent_name, widget_key, hours, limit)

    def get_all_widget_history(self, agent_name: str, hours: int = 24):
        return self._dashboard_history_ops.get_all_widget_history(agent_name, hours)

    def calculate_widget_stats(self, values: list):
        return self._dashboard_history_ops.calculate_widget_stats(values)

    def get_last_captured_mtime(self, agent_name: str):
        return self._dashboard_history_ops.get_last_captured_mtime(agent_name)

    def cleanup_old_dashboard_snapshots(self, days: int = 30):
        return self._dashboard_history_ops.cleanup_old_snapshots(days)

    def delete_agent_dashboard_history(self, agent_name: str):
        return self._dashboard_history_ops.delete_agent_dashboard_history(agent_name)

    def cache_valid_dashboard(self, agent_name: str, config: dict, last_modified: str = None):
        return self._dashboard_history_ops.cache_valid_dashboard(agent_name, config, last_modified)

    def get_cached_dashboard(self, agent_name: str):
        return self._dashboard_history_ops.get_cached_dashboard(agent_name)

    def delete_cached_dashboard(self, agent_name: str):
        return self._dashboard_history_ops.delete_cached_dashboard(agent_name)

    def has_cached_dashboard(self, agent_name: str) -> bool:
        return self._dashboard_history_ops.has_cached_dashboard(agent_name)

    def get_agent_execution_stats(self, agent_name: str, hours: int = 24):
        return self._schedule_ops.get_agent_execution_stats(agent_name, hours)

    # =========================================================================
    # Slack Integration (delegated to db/slack.py) - SLACK-001
    # =========================================================================

    def create_slack_connection(self, link_id: str, slack_team_id: str, slack_team_name: str,
                                 slack_bot_token: str, connected_by: str):
        return self._slack_ops.create_slack_connection(
            link_id, slack_team_id, slack_team_name, slack_bot_token, connected_by
        )

    def get_slack_connection(self, connection_id: str):
        return self._slack_ops.get_slack_connection(connection_id)

    def get_slack_connection_by_link(self, link_id: str):
        return self._slack_ops.get_slack_connection_by_link(link_id)

    def get_slack_connection_by_team(self, slack_team_id: str):
        return self._slack_ops.get_slack_connection_by_team(slack_team_id)

    def update_slack_connection(self, connection_id: str, enabled: bool = None, slack_team_name: str = None):
        return self._slack_ops.update_slack_connection(connection_id, enabled, slack_team_name)

    def delete_slack_connection(self, connection_id: str):
        return self._slack_ops.delete_slack_connection(connection_id)

    def delete_slack_connection_by_link(self, link_id: str):
        return self._slack_ops.delete_slack_connection_by_link(link_id)

    def get_slack_user_verification(self, link_id: str, slack_user_id: str, slack_team_id: str):
        return self._slack_ops.get_user_verification(link_id, slack_user_id, slack_team_id)

    def create_slack_user_verification(self, link_id: str, slack_user_id: str, slack_team_id: str,
                                        verified_email: str, verification_method: str):
        return self._slack_ops.create_user_verification(
            link_id, slack_user_id, slack_team_id, verified_email, verification_method
        )

    def get_slack_pending_verification(self, slack_user_id: str, slack_team_id: str):
        return self._slack_ops.get_pending_verification(slack_user_id, slack_team_id)

    def create_slack_pending_verification(self, link_id: str, slack_user_id: str, slack_team_id: str,
                                           email: str = None, code: str = None, state: str = "awaiting_email"):
        return self._slack_ops.create_pending_verification(
            link_id, slack_user_id, slack_team_id, email, code, state
        )

    def update_slack_pending_verification(self, slack_user_id: str, slack_team_id: str,
                                           email: str = None, code: str = None, state: str = None):
        return self._slack_ops.update_pending_verification(slack_user_id, slack_team_id, email, code, state)

    def delete_slack_pending_verification(self, slack_user_id: str, slack_team_id: str):
        return self._slack_ops.delete_pending_verification(slack_user_id, slack_team_id)

    def cleanup_expired_slack_pending_verifications(self):
        return self._slack_ops.cleanup_expired_pending_verifications()

    # =========================================================================
    # Slack Multi-Agent Channels (delegated to db/slack_channels.py)
    # =========================================================================

    def create_slack_workspace(self, team_id, team_name, bot_token, connected_by=None):
        return self._slack_channel_ops.create_workspace(team_id, team_name, bot_token, connected_by)

    def get_slack_workspace(self, team_id):
        return self._slack_channel_ops.get_workspace_by_team(team_id)

    def get_slack_workspace_bot_token(self, team_id):
        return self._slack_channel_ops.get_workspace_bot_token(team_id)

    def get_all_slack_workspaces(self):
        return self._slack_channel_ops.get_all_workspaces()

    def delete_slack_workspace(self, team_id):
        return self._slack_channel_ops.delete_workspace(team_id)

    def bind_slack_channel_to_agent(self, team_id, slack_channel_id, slack_channel_name,
                                     agent_name, created_by=None, is_dm_default=False):
        return self._slack_channel_ops.bind_channel_to_agent(
            team_id, slack_channel_id, slack_channel_name, agent_name, created_by, is_dm_default
        )

    def get_slack_channel_agent(self, team_id, slack_channel_id):
        return self._slack_channel_ops.get_channel_agent(team_id, slack_channel_id)

    def get_slack_agent_name_for_channel(self, team_id, slack_channel_id):
        return self._slack_channel_ops.get_agent_name_for_channel(team_id, slack_channel_id)

    def get_slack_dm_default_agent(self, team_id):
        return self._slack_channel_ops.get_dm_default_agent(team_id)

    def get_slack_agents_for_workspace(self, team_id):
        return self._slack_channel_ops.get_agents_for_workspace(team_id)

    def get_slack_channel_for_agent(self, team_id, agent_name):
        return self._slack_channel_ops.get_channel_for_agent(team_id, agent_name)

    def unbind_slack_agent(self, team_id, agent_name):
        return self._slack_channel_ops.unbind_agent(team_id, agent_name)

    def register_slack_active_thread(self, team_id, channel_id, thread_ts, agent_name):
        return self._slack_channel_ops.register_active_thread(team_id, channel_id, thread_ts, agent_name)

    def is_slack_active_thread(self, team_id, channel_id, thread_ts):
        return self._slack_channel_ops.is_active_thread(team_id, channel_id, thread_ts)

    # =========================================================================
    # Telegram Bot Integration (delegated to db/telegram_channels.py) - TELEGRAM-001
    # =========================================================================

    def create_telegram_binding(self, agent_name, bot_token, bot_username=None, bot_id=None):
        return self._telegram_channel_ops.create_binding(agent_name, bot_token, bot_username, bot_id)

    def get_telegram_binding(self, agent_name):
        return self._telegram_channel_ops.get_binding_by_agent(agent_name)

    def get_telegram_binding_by_bot_id(self, bot_id):
        return self._telegram_channel_ops.get_binding_by_bot_id(bot_id)

    def get_telegram_binding_by_webhook_secret(self, webhook_secret):
        return self._telegram_channel_ops.get_binding_by_webhook_secret(webhook_secret)

    def get_telegram_bot_token(self, agent_name):
        return self._telegram_channel_ops.get_decrypted_bot_token(agent_name)

    def get_all_telegram_bindings(self):
        return self._telegram_channel_ops.get_all_bindings()

    def update_telegram_webhook_url(self, agent_name, webhook_url):
        return self._telegram_channel_ops.update_webhook_url(agent_name, webhook_url)

    def update_telegram_last_update_id(self, agent_name, update_id):
        return self._telegram_channel_ops.update_last_update_id(agent_name, update_id)

    def delete_telegram_binding(self, agent_name):
        return self._telegram_channel_ops.delete_binding(agent_name)

    def get_or_create_telegram_chat_link(self, binding_id, telegram_user_id, telegram_username=None):
        return self._telegram_channel_ops.get_or_create_chat_link(binding_id, telegram_user_id, telegram_username)

    def increment_telegram_message_count(self, chat_link_id):
        return self._telegram_channel_ops.increment_message_count(chat_link_id)

    def get_telegram_verified_email(self, binding_id, telegram_user_id):
        return self._telegram_channel_ops.get_verified_email(binding_id, telegram_user_id)

    def set_telegram_verified_email(self, binding_id, telegram_user_id, email):
        return self._telegram_channel_ops.set_verified_email(binding_id, telegram_user_id, email)

    def clear_telegram_verified_email(self, binding_id, telegram_user_id):
        return self._telegram_channel_ops.clear_verified_email(binding_id, telegram_user_id)

    def get_telegram_chat_link_by_verified_email(self, binding_id, email):
        """Reverse lookup: find chat link by verified email for proactive messaging (#321)."""
        return self._telegram_channel_ops.get_chat_link_by_verified_email(binding_id, email)

    # Telegram Group Configs (TGRAM-GROUP)

    def get_or_create_telegram_group_config(self, binding_id, chat_id, chat_title=None, chat_type="group"):
        return self._telegram_channel_ops.get_or_create_group_config(binding_id, chat_id, chat_title, chat_type)

    def get_telegram_group_config(self, binding_id, chat_id):
        return self._telegram_channel_ops.get_group_config(binding_id, chat_id)

    def get_telegram_groups_for_agent(self, agent_name):
        return self._telegram_channel_ops.get_groups_for_agent(agent_name)

    def update_telegram_group_config(self, group_config_id, trigger_mode=None, welcome_enabled=None, welcome_text=None):
        return self._telegram_channel_ops.update_group_config(group_config_id, trigger_mode, welcome_enabled, welcome_text)

    def deactivate_telegram_group_config(self, binding_id, chat_id):
        return self._telegram_channel_ops.deactivate_group_config(binding_id, chat_id)

    # Telegram Group Verification (group_auth_mode support)

    def is_telegram_group_verified(self, binding_id, chat_id):
        return self._telegram_channel_ops.is_group_verified(binding_id, chat_id)

    def get_telegram_group_verified_email(self, binding_id, chat_id):
        return self._telegram_channel_ops.get_group_verified_email(binding_id, chat_id)

    def set_telegram_group_verified(self, binding_id, chat_id, email):
        return self._telegram_channel_ops.set_group_verified(binding_id, chat_id, email)

    def clear_telegram_group_verification(self, binding_id, chat_id):
        return self._telegram_channel_ops.clear_group_verification(binding_id, chat_id)

    # =========================================================================
    # Nevermined Payment Integration (delegated to db/nevermined.py) - NVM-001
    # =========================================================================

    def create_or_update_nevermined_config(self, agent_name, nvm_api_key, nvm_environment,
                                            nvm_agent_id, nvm_plan_id, credits_per_request=1):
        return self._nevermined_ops.create_or_update_config(
            agent_name, nvm_api_key, nvm_environment, nvm_agent_id, nvm_plan_id, credits_per_request
        )

    def get_nevermined_config(self, agent_name):
        return self._nevermined_ops.get_config(agent_name)

    def get_nevermined_config_with_key(self, agent_name):
        return self._nevermined_ops.get_config_with_key(agent_name)

    def delete_nevermined_config(self, agent_name):
        return self._nevermined_ops.delete_config(agent_name)

    def set_nevermined_enabled(self, agent_name, enabled):
        return self._nevermined_ops.set_enabled(agent_name, enabled)

    def is_nevermined_enabled(self, agent_name):
        return self._nevermined_ops.is_nevermined_enabled(agent_name)

    def log_nevermined_payment(self, agent_name, action, success, **kwargs):
        return self._nevermined_ops.log_payment(agent_name, action, success, **kwargs)

    def get_nevermined_payment_log(self, agent_name, limit=50):
        return self._nevermined_ops.get_payment_log(agent_name, limit)

    def get_nevermined_settlement_failures(self, limit=50):
        return self._nevermined_ops.get_settlement_failures(limit)

    def get_nevermined_payment_log_entry(self, log_id):
        return self._nevermined_ops.get_payment_log_entry(log_id)

    # =========================================================================
    # Operator Queue (delegated to db/operator_queue.py) - OPS-001
    # =========================================================================

    def create_operator_queue_item(self, agent_name, item):
        return self._operator_queue_ops.create_item(agent_name, item)

    def get_operator_queue_item(self, item_id):
        return self._operator_queue_ops.get_item(item_id)

    def list_operator_queue_items(self, **kwargs):
        return self._operator_queue_ops.list_items(**kwargs)

    def respond_to_operator_queue_item(self, item_id, response, response_text,
                                        responded_by_id, responded_by_email):
        return self._operator_queue_ops.respond_to_item(
            item_id, response, response_text, responded_by_id, responded_by_email
        )

    def cancel_operator_queue_item(self, item_id):
        return self._operator_queue_ops.cancel_item(item_id)

    def mark_operator_queue_acknowledged(self, item_id):
        return self._operator_queue_ops.mark_acknowledged(item_id)

    def mark_operator_queue_expired(self):
        return self._operator_queue_ops.mark_expired()

    def get_operator_queue_stats(self):
        return self._operator_queue_ops.get_stats()

    def get_operator_queue_responded_for_agent(self, agent_name):
        return self._operator_queue_ops.get_responded_items_for_agent(agent_name)

    def operator_queue_item_exists(self, item_id):
        return self._operator_queue_ops.item_exists(item_id)

    # =========================================================================
    # Agent Event Subscriptions (delegated to db/event_subscriptions.py) - EVT-001
    # =========================================================================

    def create_event_subscription(self, subscriber_agent, data, created_by):
        return self._event_subscription_ops.create_subscription(subscriber_agent, data, created_by)

    def get_event_subscription(self, subscription_id):
        return self._event_subscription_ops.get_subscription(subscription_id)

    def list_event_subscriptions(self, subscriber_agent=None, source_agent=None, enabled_only=False, limit=100):
        return self._event_subscription_ops.list_subscriptions(subscriber_agent, source_agent, enabled_only, limit)

    def update_event_subscription(self, subscription_id, event_type=None, target_message=None, enabled=None):
        return self._event_subscription_ops.update_subscription(subscription_id, event_type, target_message, enabled)

    def delete_event_subscription(self, subscription_id):
        return self._event_subscription_ops.delete_subscription(subscription_id)

    def delete_agent_event_subscriptions(self, agent_name):
        return self._event_subscription_ops.delete_agent_subscriptions(agent_name)

    def find_matching_event_subscriptions(self, source_agent, event_type):
        return self._event_subscription_ops.find_matching_subscriptions(source_agent, event_type)

    def create_agent_event(self, source_agent, event_type, payload=None, subscriptions_triggered=0):
        return self._event_subscription_ops.create_event(source_agent, event_type, payload, subscriptions_triggered)

    def list_agent_events(self, source_agent=None, event_type=None, limit=50):
        return self._event_subscription_ops.list_events(source_agent, event_type, limit)

    # =========================================================================
    # Access Requests (Issue #311)
    # =========================================================================

    def upsert_access_request(self, agent_name: str, email: str, channel: str = None):
        return self._access_request_ops.upsert_pending(agent_name, email, channel)

    def list_access_requests(self, agent_name: str, status: str = "pending"):
        return self._access_request_ops.list_for_agent(agent_name, status)

    def get_access_request(self, request_id: str):
        return self._access_request_ops.get(request_id)

    def decide_access_request(self, request_id: str, approve: bool, decided_by_user_id: int):
        return self._access_request_ops.decide(request_id, approve, decided_by_user_id)

    def delete_access_requests_for_agent(self, agent_name: str):
        return self._access_request_ops.delete_for_agent(agent_name)

    # =========================================================================
    # Platform Audit Log (SEC-001 / Issue #20 — Phase 1)
    # =========================================================================

    def create_audit_entry(self, entry: dict):
        """Insert an audit log entry (append-only)."""
        return self._audit_ops.create_audit_entry(entry)

    def get_audit_entries(self, **filters):
        """Query audit entries with optional filters. See PlatformAuditOperations for kwargs."""
        return self._audit_ops.get_audit_entries(**filters)

    def count_audit_entries(self, **filters):
        """Count audit entries matching a filter (independent of limit/offset)."""
        return self._audit_ops.count_audit_entries(**filters)

    def get_audit_entry(self, event_id: str):
        """Fetch a single audit entry by its event_id (UUID)."""
        return self._audit_ops.get_audit_entry(event_id)

    def get_audit_entries_range(self, start_id: int, end_id: int):
        """Fetch entries by primary-key range (used by hash-chain verification in Phase 4)."""
        return self._audit_ops.get_audit_entries_range(start_id, end_id)

    def get_audit_stats(self, start_time: str = None, end_time: str = None):
        """Aggregate counts by event_type and actor_type for the dashboard."""
        return self._audit_ops.get_audit_stats(start_time=start_time, end_time=end_time)


# Global database manager instance
db = DatabaseManager()
