"""
Database migrations for Trinity platform.

Each migration function handles a specific schema change.
Migrations are idempotent - safe to run multiple times.

Migration Order (as of 2026-02-28):
1. agent_sharing - Email-based sharing (from user_id)
2. schedule_executions_observability - Context/cost/tools columns
3. mcp_api_keys_agent_scope - Agent collaboration support
4. agent_ownership_system_flag - System agent protection
5. agent_ownership_platform_key - Per-agent API key control
6. agent_git_config_source_branch - GitHub source tracking
7. agent_ownership_autonomy - Autonomous scheduling control
8. agent_ownership_resource_limits - Memory/CPU configuration
9. agent_skills - Skill ID to skill_name migration
10. agent_ownership_full_capabilities - Container security settings
11. agent_ownership_read_only_mode - Code protection mode
12. agent_schedules_execution_config - Per-schedule timeout/tools
13. agent_notifications - Notification table creation
14. execution_origin_tracking - AUDIT-001 origin fields
15. execution_session_tracking - EXEC-023 session resume
16. subscription_credentials - SUB-001 subscription table
17. agent_ownership_subscription_id - SUB-001 subscription FK
18. agent_dashboard_values - DASH-001 dashboard history table
19. setup_completed_backfill - Auto-complete setup for existing installs
20. slack_integration_tables - SLACK-001 Slack integration tables
21. agent_ownership_parallel_capacity - CAPACITY-001 parallel execution slots
22. schedule_model_selection - MODEL-001 model selection for tasks/schedules
23. nevermined_tables - NVM-001 Nevermined payment integration tables
24. agent_avatar_columns - AVATAR-001 AI-generated agent avatar support
25. operator_queue_table - OPS-001 Operator Queue & Operating Room
26. agent_ownership_default_avatar - AVATAR-003 default avatar flag
27. agent_ownership_execution_timeout - TIMEOUT-001 per-agent execution timeout
28. public_user_memory_table - MEM-001 per-user persistent memory for public link agents
29. subscription_rate_limit_tracking - SUB-003 rate-limit event tracking for auto-switch
30. execution_fan_out_id - FANOUT-001 fan-out operation linkage
31. scheduler_retry_support - RETRY-001 scheduler retry mechanism
32. validation_support - VALIDATE-001 post-execution business validation
33. agent_git_config_pat - #347 per-agent GitHub PAT support
"""
import logging
import sqlite3
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def run_all_migrations(cursor, conn):
    """Run all pending migrations in order. Called twice from init_database().

    First call: upgrades existing DBs (skips migrations whose target table
    doesn't exist yet on fresh install).
    Second call (after init_schema): records those fresh-install skips so the
    health check stays accurate — re-running is safe because every migration
    guards with PRAGMA table_info before mutating anything.

    Tracks applied migrations in schema_migrations table for idempotency and
    to prevent race conditions when multiple uvicorn workers start simultaneously.
    Raises on failure so the backend refuses to start with an inconsistent schema.
    """
    # Create tracking table — atomic, safe to call on every startup
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
    """)
    conn.commit()

    # Fetch already-applied migrations to skip them
    cursor.execute("SELECT name FROM schema_migrations")
    applied = {row[0] for row in cursor.fetchall()}

    for name, migration_fn in MIGRATIONS:
        if name in applied:
            continue
        try:
            migration_fn(cursor, conn)
            cursor.execute(
                "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
                (name, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                # Target table doesn't exist yet — this is a fresh install.
                # init_schema will create it with the correct current schema.
                logger.debug("Migration %s: table not present yet, skipping (fresh install)", name)
            else:
                logger.error("Migration FAILED (%s): %s", name, e, exc_info=True)
                raise
        except Exception as e:
            logger.error("Migration FAILED (%s): %s", name, e, exc_info=True)
            raise


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
        ("tool_calls", "TEXT"),
        ("execution_log", "TEXT")  # Full Claude Code execution transcript (JSON)
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


def _migrate_agent_ownership_system_flag(cursor, conn):
    """Add is_system column to agent_ownership table for system agent protection."""
    cursor.execute("PRAGMA table_info(agent_ownership)")
    columns = {row[1] for row in cursor.fetchall()}

    if "is_system" not in columns:
        print("Adding is_system column to agent_ownership for system agent protection...")
        cursor.execute("ALTER TABLE agent_ownership ADD COLUMN is_system INTEGER DEFAULT 0")
        conn.commit()


def _migrate_agent_ownership_platform_key(cursor, conn):
    """Add use_platform_api_key column to agent_ownership table for per-agent API key control."""
    cursor.execute("PRAGMA table_info(agent_ownership)")
    columns = {row[1] for row in cursor.fetchall()}

    if "use_platform_api_key" not in columns:
        print("Adding use_platform_api_key column to agent_ownership for per-agent API key control...")
        cursor.execute("ALTER TABLE agent_ownership ADD COLUMN use_platform_api_key INTEGER DEFAULT 1")
        conn.commit()


def _migrate_agent_git_config_source_branch(cursor, conn):
    """Add source_branch and source_mode columns to agent_git_config for GitHub source tracking."""
    cursor.execute("PRAGMA table_info(agent_git_config)")
    columns = {row[1] for row in cursor.fetchall()}

    new_columns = [
        ("source_branch", "TEXT DEFAULT 'main'"),  # Branch to pull from
        ("source_mode", "INTEGER DEFAULT 0")  # 1 = track source branch directly, 0 = legacy working branch
    ]

    for col_name, col_type in new_columns:
        if col_name not in columns:
            print(f"Adding {col_name} column to agent_git_config for GitHub source tracking...")
            cursor.execute(f"ALTER TABLE agent_git_config ADD COLUMN {col_name} {col_type}")

    conn.commit()


def _migrate_agent_ownership_autonomy(cursor, conn):
    """Add autonomy_enabled column to agent_ownership table for autonomous scheduling control."""
    cursor.execute("PRAGMA table_info(agent_ownership)")
    columns = {row[1] for row in cursor.fetchall()}

    if "autonomy_enabled" not in columns:
        print("Adding autonomy_enabled column to agent_ownership for autonomous scheduling...")
        cursor.execute("ALTER TABLE agent_ownership ADD COLUMN autonomy_enabled INTEGER DEFAULT 0")
        conn.commit()


def _migrate_agent_ownership_resource_limits(cursor, conn):
    """Add memory_limit and cpu_limit columns to agent_ownership table for per-agent resource configuration."""
    cursor.execute("PRAGMA table_info(agent_ownership)")
    columns = {row[1] for row in cursor.fetchall()}

    new_columns = [
        ("memory_limit", "TEXT"),  # e.g., "4g", "8g", "16g"
        ("cpu_limit", "TEXT")  # e.g., "2", "4", "8"
    ]

    for col_name, col_type in new_columns:
        if col_name not in columns:
            print(f"Adding {col_name} column to agent_ownership for resource configuration...")
            cursor.execute(f"ALTER TABLE agent_ownership ADD COLUMN {col_name} {col_type}")

    conn.commit()


def _migrate_agent_ownership_full_capabilities(cursor, conn):
    """Add full_capabilities column to agent_ownership table for container security settings."""
    cursor.execute("PRAGMA table_info(agent_ownership)")
    columns = {row[1] for row in cursor.fetchall()}

    if "full_capabilities" not in columns:
        print("Adding full_capabilities column to agent_ownership for container security settings...")
        cursor.execute("ALTER TABLE agent_ownership ADD COLUMN full_capabilities INTEGER DEFAULT 0")
        conn.commit()


def _migrate_agent_ownership_read_only_mode(cursor, conn):
    """Add read_only_mode and read_only_config columns to agent_ownership table for code protection."""
    cursor.execute("PRAGMA table_info(agent_ownership)")
    columns = {row[1] for row in cursor.fetchall()}

    new_columns = [
        ("read_only_mode", "INTEGER DEFAULT 0"),  # 0 = disabled, 1 = enabled
        ("read_only_config", "TEXT")  # JSON config for blocked/allowed patterns
    ]

    for col_name, col_def in new_columns:
        if col_name not in columns:
            print(f"Adding {col_name} column to agent_ownership for read-only mode...")
            cursor.execute(f"ALTER TABLE agent_ownership ADD COLUMN {col_name} {col_def}")

    conn.commit()


def _migrate_agent_schedules_execution_config(cursor, conn):
    """Add timeout_seconds and allowed_tools columns to agent_schedules table for per-schedule execution config."""
    cursor.execute("PRAGMA table_info(agent_schedules)")
    columns = {row[1] for row in cursor.fetchall()}

    new_columns = [
        ("timeout_seconds", "INTEGER DEFAULT 900"),  # Default 15 minutes
        ("allowed_tools", "TEXT")  # JSON array of allowed tools, NULL = all tools
    ]

    for col_name, col_type in new_columns:
        if col_name not in columns:
            print(f"Adding {col_name} column to agent_schedules for execution configuration...")
            cursor.execute(f"ALTER TABLE agent_schedules ADD COLUMN {col_name} {col_type}")

    conn.commit()


def _migrate_agent_notifications_table(cursor, conn):
    """Create agent_notifications table if it doesn't exist (NOTIF-001)."""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_notifications (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            notification_type TEXT NOT NULL,
            title TEXT NOT NULL,
            message TEXT,
            priority TEXT DEFAULT 'normal',
            category TEXT,
            metadata TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT NOT NULL,
            acknowledged_at TEXT,
            acknowledged_by TEXT
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_notifications_agent ON agent_notifications(agent_name, created_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_notifications_status ON agent_notifications(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_notifications_priority ON agent_notifications(priority)")
    conn.commit()


def _migrate_execution_origin_tracking(cursor, conn):
    """Add execution origin tracking columns to schedule_executions table (AUDIT-001).

    Tracks WHO triggered each execution:
    - source_user_id: User who triggered (for manual and MCP user triggers)
    - source_user_email: User email (denormalized for queries)
    - source_agent_name: Calling agent (for agent-to-agent collaboration)
    - source_mcp_key_id: MCP API key ID used (for MCP calls)
    - source_mcp_key_name: MCP API key name (denormalized)
    """
    cursor.execute("PRAGMA table_info(schedule_executions)")
    columns = {row[1] for row in cursor.fetchall()}

    new_columns = [
        ("source_user_id", "INTEGER"),
        ("source_user_email", "TEXT"),
        ("source_agent_name", "TEXT"),
        ("source_mcp_key_id", "TEXT"),
        ("source_mcp_key_name", "TEXT"),
    ]

    for col_name, col_type in new_columns:
        if col_name not in columns:
            print(f"Adding {col_name} column to schedule_executions for origin tracking...")
            cursor.execute(f"ALTER TABLE schedule_executions ADD COLUMN {col_name} {col_type}")

    conn.commit()


def _migrate_execution_session_tracking(cursor, conn):
    """Add claude_session_id column to schedule_executions table (EXEC-023).

    Enables "Continue Execution as Chat" feature by storing Claude Code's
    session_id, which can be used with --resume to continue the session.
    """
    cursor.execute("PRAGMA table_info(schedule_executions)")
    columns = {row[1] for row in cursor.fetchall()}

    if "claude_session_id" not in columns:
        print("Adding claude_session_id column to schedule_executions for session resume...")
        cursor.execute("ALTER TABLE schedule_executions ADD COLUMN claude_session_id TEXT")

    conn.commit()


def _migrate_subscription_credentials_table(cursor, conn):
    """Create subscription_credentials table (SUB-001: Claude Max/Pro Subscription Management).

    Stores encrypted OAuth credentials from Claude Max/Pro subscriptions,
    enabling centralized subscription management and assignment to agents.
    """
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subscription_credentials (
            id TEXT PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            encrypted_credentials TEXT NOT NULL,
            subscription_type TEXT,
            rate_limit_tier TEXT,
            owner_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (owner_id) REFERENCES users(id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_subscriptions_name ON subscription_credentials(name)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_subscriptions_owner ON subscription_credentials(owner_id)")
    conn.commit()


def _migrate_agent_ownership_subscription_id(cursor, conn):
    """Add subscription_id column to agent_ownership table (SUB-001).

    Links agents to their assigned subscription for Claude Max/Pro authentication.
    """
    cursor.execute("PRAGMA table_info(agent_ownership)")
    columns = {row[1] for row in cursor.fetchall()}

    if "subscription_id" not in columns:
        print("Adding subscription_id column to agent_ownership for subscription management...")
        cursor.execute("ALTER TABLE agent_ownership ADD COLUMN subscription_id TEXT REFERENCES subscription_credentials(id)")

    conn.commit()


def _migrate_agent_skills_table(cursor, conn):
    """Migrate agent_skills table if it has wrong schema (skill_id instead of skill_name)."""
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='agent_skills'")
    if not cursor.fetchone():
        return  # Table doesn't exist yet, will be created fresh

    cursor.execute("PRAGMA table_info(agent_skills)")
    columns = {row[1] for row in cursor.fetchall()}

    # Check if table has old schema (skill_id instead of skill_name)
    if "skill_id" in columns and "skill_name" not in columns:
        print("Migrating agent_skills table: renaming skill_id to skill_name...")

        # Get existing data
        cursor.execute("SELECT id, agent_name, skill_id, assigned_by, assigned_at FROM agent_skills")
        existing_data = cursor.fetchall()

        # Drop old table
        cursor.execute("DROP TABLE agent_skills")

        # Create new table with correct schema
        cursor.execute("""
            CREATE TABLE agent_skills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name TEXT NOT NULL,
                skill_name TEXT NOT NULL,
                assigned_by TEXT NOT NULL,
                assigned_at TEXT NOT NULL,
                UNIQUE(agent_name, skill_name)
            )
        """)

        # Reinsert data
        for row in existing_data:
            cursor.execute("""
                INSERT INTO agent_skills (agent_name, skill_name, assigned_by, assigned_at)
                VALUES (?, ?, ?, ?)
            """, (row[1], row[2], row[3], row[4]))

        # Recreate indexes
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_agent_skills_agent ON agent_skills(agent_name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_agent_skills_skill ON agent_skills(skill_name)")

        conn.commit()
        print(f"Migrated {len(existing_data)} agent_skills records")


def _migrate_agent_dashboard_values_table(cursor, conn):
    """Create agent_dashboard_values table for dashboard history (DASH-001).

    Stores historical widget values from agent dashboards to support:
    - Sparkline visualizations showing value trends over time
    - Historical data analysis without agent being online
    - Platform metrics injection with trend calculation
    """
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_dashboard_values (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            widget_key TEXT NOT NULL,
            widget_label TEXT,
            widget_type TEXT NOT NULL,
            value_numeric REAL,
            value_text TEXT,
            dashboard_mtime TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_dashboard_values_agent_time ON agent_dashboard_values(agent_name, captured_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_dashboard_values_widget ON agent_dashboard_values(agent_name, widget_key, captured_at DESC)")
    conn.commit()


def _migrate_setup_completed_backfill(cursor, conn):
    """Backfill setup_completed=true for existing installations.

    Prior to this migration, existing production deployments could have:
    - An admin user with a password hash
    - But no 'setup_completed' entry in system_settings

    This caused the setup wizard to appear on existing installations.
    This migration auto-completes setup for installations that already have
    an admin user with a password set.
    """
    from datetime import datetime

    # Check if setup_completed already exists
    cursor.execute("SELECT value FROM system_settings WHERE key = 'setup_completed'")
    existing = cursor.fetchone()

    if existing:
        # Already set, nothing to do
        return

    # Check if admin user exists with a password hash
    cursor.execute("""
        SELECT password_hash FROM users
        WHERE username = 'admin' AND password_hash IS NOT NULL AND password_hash != ''
    """)
    admin_with_password = cursor.fetchone()

    if admin_with_password:
        # Admin exists with password - mark setup as completed
        print("Backfilling setup_completed=true for existing installation with admin user...")
        cursor.execute("""
            INSERT OR REPLACE INTO system_settings (key, value, updated_at)
            VALUES ('setup_completed', 'true', ?)
        """, (datetime.utcnow().isoformat(),))
        conn.commit()
        print("Setup marked as completed.")


def _migrate_slack_integration_tables(cursor, conn):
    """Create Slack integration tables (SLACK-001).

    Creates three tables for Slack public link integration:
    - slack_link_connections: Links Slack workspaces to public links
    - slack_user_verifications: Tracks verified Slack users
    - slack_pending_verifications: In-progress email verifications
    """
    # Create slack_link_connections table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS slack_link_connections (
            id TEXT PRIMARY KEY,
            link_id TEXT NOT NULL UNIQUE,
            slack_team_id TEXT NOT NULL UNIQUE,
            slack_team_name TEXT,
            slack_bot_token TEXT NOT NULL,
            connected_by TEXT NOT NULL,
            connected_at TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            FOREIGN KEY (link_id) REFERENCES agent_public_links(id) ON DELETE CASCADE,
            FOREIGN KEY (connected_by) REFERENCES users(id)
        )
    """)

    # Create slack_user_verifications table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS slack_user_verifications (
            id TEXT PRIMARY KEY,
            link_id TEXT NOT NULL,
            slack_user_id TEXT NOT NULL,
            slack_team_id TEXT NOT NULL,
            verified_email TEXT NOT NULL,
            verification_method TEXT NOT NULL,
            verified_at TEXT NOT NULL,
            FOREIGN KEY (link_id) REFERENCES agent_public_links(id) ON DELETE CASCADE,
            UNIQUE(link_id, slack_user_id, slack_team_id)
        )
    """)

    # Create slack_pending_verifications table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS slack_pending_verifications (
            id TEXT PRIMARY KEY,
            link_id TEXT NOT NULL,
            slack_user_id TEXT NOT NULL,
            slack_team_id TEXT NOT NULL,
            email TEXT,
            code TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            state TEXT DEFAULT 'awaiting_email',
            FOREIGN KEY (link_id) REFERENCES agent_public_links(id) ON DELETE CASCADE
        )
    """)

    # Create indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_slack_connections_team ON slack_link_connections(slack_team_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_slack_connections_link ON slack_link_connections(link_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_slack_verifications_user ON slack_user_verifications(slack_user_id, slack_team_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_slack_verifications_link ON slack_user_verifications(link_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_slack_pending_user ON slack_pending_verifications(slack_user_id, slack_team_id)")

    conn.commit()


def _migrate_agent_ownership_parallel_capacity(cursor, conn):
    """Add max_parallel_tasks column to agent_ownership table (CAPACITY-001).

    Configures per-agent parallel execution capacity for the /task endpoint.
    Range: 1-10, Default: 3.
    """
    cursor.execute("PRAGMA table_info(agent_ownership)")
    columns = {row[1] for row in cursor.fetchall()}

    if "max_parallel_tasks" not in columns:
        print("Adding max_parallel_tasks column to agent_ownership for parallel capacity...")
        cursor.execute("ALTER TABLE agent_ownership ADD COLUMN max_parallel_tasks INTEGER DEFAULT 3")

    conn.commit()


def _migrate_schedule_model_selection(cursor, conn):
    """Add model columns for MODEL-001: Model Selection for Tasks & Schedules.

    - agent_schedules.model: Model to use when schedule fires (NULL = agent default)
    - schedule_executions.model_used: Records which model was actually used
    """
    # Add model to agent_schedules
    cursor.execute("PRAGMA table_info(agent_schedules)")
    columns = {row[1] for row in cursor.fetchall()}

    if "model" not in columns:
        print("Adding model column to agent_schedules for model selection...")
        cursor.execute("ALTER TABLE agent_schedules ADD COLUMN model TEXT")

    # Add model_used to schedule_executions
    cursor.execute("PRAGMA table_info(schedule_executions)")
    columns = {row[1] for row in cursor.fetchall()}

    if "model_used" not in columns:
        print("Adding model_used column to schedule_executions for model audit...")
        cursor.execute("ALTER TABLE schedule_executions ADD COLUMN model_used TEXT")

    conn.commit()


def _migrate_nevermined_tables(cursor, conn):
    """Create Nevermined payment integration tables (NVM-001).

    - nevermined_agent_config: Per-agent payment configuration with encrypted NVM_API_KEY
    - nevermined_payment_log: Payment verification and settlement audit trail
    """
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS nevermined_agent_config (
            id TEXT PRIMARY KEY,
            agent_name TEXT UNIQUE NOT NULL,
            encrypted_credentials TEXT NOT NULL,
            nvm_environment TEXT NOT NULL,
            nvm_agent_id TEXT NOT NULL,
            nvm_plan_id TEXT NOT NULL,
            credits_per_request INTEGER NOT NULL DEFAULT 1,
            enabled INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS nevermined_payment_log (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            execution_id TEXT,
            action TEXT NOT NULL,
            subscriber_address TEXT,
            credits_amount INTEGER,
            tx_hash TEXT,
            remaining_balance INTEGER,
            success INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            created_at TEXT NOT NULL
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_nvm_config_agent ON nevermined_agent_config(agent_name)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_nvm_payment_log_agent ON nevermined_payment_log(agent_name, created_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_nvm_payment_log_execution ON nevermined_payment_log(execution_id)")
    conn.commit()


def _migrate_agent_avatar_columns(cursor, conn):
    """Add avatar columns to agent_ownership table (AVATAR-001).

    - avatar_identity_prompt: User's character description for avatar generation
    - avatar_updated_at: ISO timestamp for cache-busting
    """
    cursor.execute("PRAGMA table_info(agent_ownership)")
    columns = {row[1] for row in cursor.fetchall()}

    new_columns = [
        ("avatar_identity_prompt", "TEXT"),
        ("avatar_updated_at", "TEXT"),
    ]

    for col_name, col_type in new_columns:
        if col_name not in columns:
            print(f"Adding {col_name} column to agent_ownership for avatar support...")
            cursor.execute(f"ALTER TABLE agent_ownership ADD COLUMN {col_name} {col_type}")

    conn.commit()


def _migrate_operator_queue_table(cursor, conn):
    """Create operator_queue table for Operator Queue & Operating Room (OPS-001).

    Stores queue items from agents requesting operator input:
    approvals, questions, and alerts with response tracking.
    """
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS operator_queue (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            priority TEXT NOT NULL DEFAULT 'medium',
            title TEXT NOT NULL,
            question TEXT NOT NULL,
            options TEXT,
            context TEXT,
            execution_id TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            response TEXT,
            response_text TEXT,
            responded_by_id TEXT,
            responded_by_email TEXT,
            responded_at TEXT,
            acknowledged_at TEXT,
            FOREIGN KEY (responded_by_id) REFERENCES users(id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_operator_queue_agent ON operator_queue(agent_name)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_operator_queue_status ON operator_queue(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_operator_queue_priority ON operator_queue(priority)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_operator_queue_type ON operator_queue(type)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_operator_queue_created ON operator_queue(created_at DESC)")
    conn.commit()


def _migrate_agent_ownership_default_avatar(cursor, conn):
    """Add is_default_avatar column to agent_ownership table (AVATAR-003).

    Tracks whether an agent's avatar was auto-generated (default) vs custom.
    Allows re-runs to skip custom avatars and overwrite stale defaults.
    """
    cursor.execute("PRAGMA table_info(agent_ownership)")
    columns = {row[1] for row in cursor.fetchall()}

    if "is_default_avatar" not in columns:
        print("Adding is_default_avatar column to agent_ownership for default avatar support...")
        cursor.execute("ALTER TABLE agent_ownership ADD COLUMN is_default_avatar INTEGER DEFAULT 0")

    conn.commit()


def _migrate_agent_ownership_execution_timeout(cursor, conn):
    """Add execution_timeout_seconds column to agent_ownership table (TIMEOUT-001).

    Per-agent configurable execution timeout. All execution paths (task API, chat,
    scheduler, MCP, paid endpoints) read from this setting when no explicit timeout
    is provided. Default: 900 seconds (15 minutes).
    """
    cursor.execute("PRAGMA table_info(agent_ownership)")
    columns = {row[1] for row in cursor.fetchall()}

    if "execution_timeout_seconds" not in columns:
        print("Adding execution_timeout_seconds column to agent_ownership for per-agent timeout...")
        cursor.execute("ALTER TABLE agent_ownership ADD COLUMN execution_timeout_seconds INTEGER DEFAULT 900")

    conn.commit()


def _migrate_public_user_memory_table(cursor, conn):
    """Create public_user_memory table for per-user persistent memory (MEM-001).

    Stores a plain-text memory blob per (agent_name, user_email) pair,
    updated via background summarization every 5 messages.
    """
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS public_user_memory (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            user_email TEXT NOT NULL,
            memory_text TEXT NOT NULL DEFAULT '',
            message_count INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(agent_name, user_email)
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_public_user_memory_lookup ON public_user_memory(agent_name, user_email)"
    )
    conn.commit()


def _migrate_subscription_rate_limit_tracking(cursor, conn):
    """Create subscription_rate_limit_events table (SUB-003: Auto-Switch).

    Tracks per-(agent, subscription) rate-limit events with timestamps.
    Used to determine when auto-switching should trigger (2+ consecutive events)
    and which subscriptions to skip (rate-limited within last 2 hours).
    """
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subscription_rate_limit_events (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            subscription_id TEXT NOT NULL,
            error_message TEXT,
            occurred_at TEXT NOT NULL,
            FOREIGN KEY (subscription_id) REFERENCES subscription_credentials(id) ON DELETE CASCADE
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_rate_limit_agent_sub "
        "ON subscription_rate_limit_events(agent_name, subscription_id, occurred_at DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_rate_limit_sub "
        "ON subscription_rate_limit_events(subscription_id, occurred_at DESC)"
    )
    conn.commit()


def _migrate_chat_messages_source_column(cursor, conn):
    """Add source column to chat_messages (VOICE-003).

    Distinguishes voice messages from text messages.
    Values: 'text' (default), 'voice'.
    """
    try:
        cursor.execute("ALTER TABLE chat_messages ADD COLUMN source TEXT DEFAULT 'text'")
        conn.commit()
    except Exception:
        pass  # Column already exists


def _migrate_agent_ownership_guardrails(cursor, conn):
    """Add guardrails_config column to agent_ownership (GUARD-001).

    Stores per-agent guardrail overrides (max_turns, disallowed_tools,
    extra_bash_deny, extra_path_deny) as a JSON blob. NULL means "use
    platform baseline only".
    """
    cursor.execute("PRAGMA table_info(agent_ownership)")
    columns = {row[1] for row in cursor.fetchall()}
    if "guardrails_config" not in columns:
        cursor.execute("ALTER TABLE agent_ownership ADD COLUMN guardrails_config TEXT")
        conn.commit()


def _migrate_agent_ownership_voice_prompt(cursor, conn):
    """Add voice_system_prompt column to agent_ownership (VOICE-005).

    Stores the per-agent voice personality prompt for Gemini Live API.
    """
    try:
        cursor.execute("ALTER TABLE agent_ownership ADD COLUMN voice_system_prompt TEXT")
        conn.commit()
    except Exception:
        pass  # Column already exists

def _migrate_slack_channel_agents(cursor, conn):
    """Add multi-agent Slack support: workspace table + channel-agent bindings.

    Separates workspace OAuth (one bot token per workspace) from agent routing
    (many agents per workspace, each bound to a channel).
    """
    # 1. Create slack_workspaces table (one per connected workspace)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS slack_workspaces (
            id TEXT PRIMARY KEY,
            team_id TEXT NOT NULL UNIQUE,
            team_name TEXT,
            bot_token TEXT NOT NULL,
            connected_by TEXT,
            connected_at TEXT NOT NULL,
            enabled INTEGER DEFAULT 1
        )
    """)

    # 2. Create slack_channel_agents table (channel → agent binding)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS slack_channel_agents (
            id TEXT PRIMARY KEY,
            team_id TEXT NOT NULL,
            slack_channel_id TEXT NOT NULL,
            slack_channel_name TEXT,
            agent_name TEXT NOT NULL,
            is_dm_default INTEGER DEFAULT 0,
            created_by TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(team_id, slack_channel_id),
            FOREIGN KEY (agent_name) REFERENCES agent_ownership(agent_name) ON DELETE CASCADE
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_slack_channel_agents_team ON slack_channel_agents(team_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_slack_channel_agents_agent ON slack_channel_agents(agent_name)")

    # 3. Create slack_active_threads table (tracks threads where bot responded)
    # Enables reply-without-mention in bot threads
    # TODO: Add cleanup job to delete threads older than 7 days (see slack-implementation-questions.md Q10)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS slack_active_threads (
            team_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            thread_ts TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (team_id, channel_id, thread_ts)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_slack_active_threads_lookup ON slack_active_threads(team_id, channel_id, thread_ts)")

    # 3. Migrate existing data from slack_link_connections → slack_workspaces
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='slack_link_connections'")
    if cursor.fetchone():
        cursor.execute("""
            INSERT OR IGNORE INTO slack_workspaces (id, team_id, team_name, bot_token, connected_by, connected_at, enabled)
            SELECT id, slack_team_id, slack_team_name, slack_bot_token, connected_by, connected_at, enabled
            FROM slack_link_connections
        """)
        migrated = cursor.rowcount
        if migrated > 0:
            print(f"Migrated {migrated} workspace(s) from slack_link_connections to slack_workspaces")

    conn.commit()


def _migrate_subscription_usage_tracking(cursor, conn):
    """Add subscription_id and output_tokens columns for per-subscription usage tracking (SUB-004).

    - chat_messages: subscription_id (snapshotted at record time), output_tokens
    - chat_sessions: subscription_id (active when session started)
    - schedule_executions: subscription_id (snapshotted at record time)
    """
    cursor.execute("PRAGMA table_info(chat_messages)")
    chat_msg_cols = {row[1] for row in cursor.fetchall()}

    if "subscription_id" not in chat_msg_cols:
        cursor.execute("ALTER TABLE chat_messages ADD COLUMN subscription_id TEXT")
    if "output_tokens" not in chat_msg_cols:
        cursor.execute("ALTER TABLE chat_messages ADD COLUMN output_tokens INTEGER")

    cursor.execute("PRAGMA table_info(chat_sessions)")
    chat_sess_cols = {row[1] for row in cursor.fetchall()}

    if "subscription_id" not in chat_sess_cols:
        cursor.execute("ALTER TABLE chat_sessions ADD COLUMN subscription_id TEXT")

    cursor.execute("PRAGMA table_info(schedule_executions)")
    exec_cols = {row[1] for row in cursor.fetchall()}

    if "subscription_id" not in exec_cols:
        cursor.execute("ALTER TABLE schedule_executions ADD COLUMN subscription_id TEXT")

    # Indexes for usage queries
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_messages_subscription "
        "ON chat_messages(subscription_id, timestamp)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_subscription "
        "ON schedule_executions(subscription_id, started_at)"
    )

    conn.commit()


def _migrate_execution_fan_out_id(cursor, conn):
    """Add fan_out_id column to schedule_executions table (FANOUT-001).

    Links individual execution records back to a parent fan-out operation
    for observability and result aggregation.
    """
    cursor.execute("PRAGMA table_info(schedule_executions)")
    columns = {row[1] for row in cursor.fetchall()}

    if "fan_out_id" not in columns:
        print("Adding fan_out_id column to schedule_executions for fan-out linkage...")
        cursor.execute("ALTER TABLE schedule_executions ADD COLUMN fan_out_id TEXT")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_executions_fan_out ON schedule_executions(fan_out_id)")

    conn.commit()


def _migrate_telegram_bindings(cursor, conn):
    """Create Telegram bot binding and chat link tables (TELEGRAM-001)."""

    # 1. telegram_bindings — one bot per agent
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS telegram_bindings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name TEXT NOT NULL UNIQUE,
            bot_token_encrypted TEXT NOT NULL,
            bot_username TEXT,
            bot_id TEXT UNIQUE,
            webhook_secret TEXT NOT NULL,
            webhook_url TEXT,
            telegram_secret_token TEXT,
            last_update_id INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_telegram_bindings_agent ON telegram_bindings(agent_name)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_telegram_bindings_bot_id ON telegram_bindings(bot_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_telegram_bindings_webhook ON telegram_bindings(webhook_secret)"
    )

    # 2. telegram_chat_links — tracks Telegram users per bot
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS telegram_chat_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            binding_id INTEGER NOT NULL REFERENCES telegram_bindings(id),
            telegram_user_id TEXT NOT NULL,
            telegram_username TEXT,
            session_id TEXT,
            message_count INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            last_active TEXT,
            UNIQUE(binding_id, telegram_user_id)
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_telegram_chat_links_binding ON telegram_chat_links(binding_id)"
    )

    conn.commit()


def _migrate_access_control(cursor, conn):
    """Add unified channel access control (issue #311).

    - agent_ownership: require_email (INTEGER), open_access (INTEGER)
    - telegram_chat_links: verified_email (TEXT), verified_at (TEXT)
    - access_requests: new table for pending per-agent access requests
    """
    # 1. agent_ownership columns
    cursor.execute("PRAGMA table_info(agent_ownership)")
    ao_cols = {row[1] for row in cursor.fetchall()}
    if "require_email" not in ao_cols:
        cursor.execute("ALTER TABLE agent_ownership ADD COLUMN require_email INTEGER DEFAULT 0")
    if "open_access" not in ao_cols:
        cursor.execute("ALTER TABLE agent_ownership ADD COLUMN open_access INTEGER DEFAULT 0")

    # 2. telegram_chat_links columns
    cursor.execute("PRAGMA table_info(telegram_chat_links)")
    tcl_cols = {row[1] for row in cursor.fetchall()}
    if "verified_email" not in tcl_cols:
        cursor.execute("ALTER TABLE telegram_chat_links ADD COLUMN verified_email TEXT")
    if "verified_at" not in tcl_cols:
        cursor.execute("ALTER TABLE telegram_chat_links ADD COLUMN verified_at TEXT")

    # 3. access_requests table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS access_requests (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            email TEXT NOT NULL,
            channel TEXT,
            requested_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            decided_by INTEGER,
            decided_at TEXT,
            UNIQUE(agent_name, email)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_access_requests_agent ON access_requests(agent_name, status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_access_requests_email ON access_requests(email)")

    conn.commit()


def _migrate_public_link_require_email_unified(cursor, conn):
    """Unify per-link require_email into agent-level policy (follow-up to #311).

    Before: `agent_public_links.require_email` gated email verification for
    public web chat, while `agent_ownership.require_email` gated Slack/Telegram.
    Two flags, two code paths — and the agent-level Channel Access Policy UI
    falsely claimed to cover web.

    This migration ORs any legacy per-link require_email=1 into the agent's
    `agent_ownership.require_email` so existing email-required links stay
    email-required under the unified gate in routers/public.py. The per-link
    column is retained (Slack legacy connection joins still read it) but is
    no longer written or consulted by the public chat flow.
    """
    cursor.execute("""
        UPDATE agent_ownership
        SET require_email = 1
        WHERE agent_name IN (
            SELECT DISTINCT agent_name
            FROM agent_public_links
            WHERE require_email = 1
        )
    """)
    conn.commit()


def _migrate_email_whitelist_default_role(cursor, conn):
    """Add default_role column to email_whitelist (#314).

    Before: `get_or_create_email_user()` hardcoded new email users to role
    `creator`, so anyone whitelisted via access-request approval, /share, or
    the public CLI self-signup endpoint silently gained agent-creation rights.

    After: whitelist rows carry the intended role. Access-based grants default
    to `user`; only admin-initiated whitelisting can request `creator`. This
    migration sets existing rows to `'user'` — a deliberate downgrade of the
    buggy default going forward. It does NOT touch `users.role`, so users who
    have already logged in and been promoted keep their current role.
    """
    cursor.execute("PRAGMA table_info(email_whitelist)")
    columns = {row[1] for row in cursor.fetchall()}

    if "default_role" not in columns:
        cursor.execute(
            "ALTER TABLE email_whitelist ADD COLUMN default_role TEXT NOT NULL DEFAULT 'user'"
        )

    conn.commit()


def _migrate_telegram_group_configs(cursor, conn):
    """Create Telegram group configuration table (TGRAM-GROUP)."""

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS telegram_group_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            binding_id INTEGER NOT NULL REFERENCES telegram_bindings(id),
            chat_id TEXT NOT NULL,
            chat_title TEXT,
            chat_type TEXT DEFAULT 'group',
            trigger_mode TEXT DEFAULT 'mention',
            welcome_enabled INTEGER DEFAULT 0,
            welcome_text TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT
        )
    """)
    cursor.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_tg_group_binding_chat ON telegram_group_configs(binding_id, chat_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_tg_group_chat_id ON telegram_group_configs(chat_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_tg_group_active ON telegram_group_configs(is_active)"
    )

    conn.commit()


def _migrate_backlog_support(cursor, conn):
    """BACKLOG-001: Persistent async task backlog.

    Adds:
    - schedule_executions.queued_at: ISO timestamp for FIFO ordering when status='queued'.
    - schedule_executions.backlog_metadata: JSON blob capturing full ParallelTaskRequest
      context (model, system_prompt, allowed_tools, timeout, identity, subscription, etc)
      so the drain path can reconstitute the request without re-reading auth.
    - agent_ownership.max_backlog_depth: per-agent cap on queued items (default 50, cap 200).
    - Partial index on (agent_name, queued_at) WHERE status='queued' for cheap FIFO claim.
    """
    # schedule_executions columns
    cursor.execute("PRAGMA table_info(schedule_executions)")
    se_cols = {row[1] for row in cursor.fetchall()}
    if "queued_at" not in se_cols:
        cursor.execute("ALTER TABLE schedule_executions ADD COLUMN queued_at TEXT")
    if "backlog_metadata" not in se_cols:
        cursor.execute("ALTER TABLE schedule_executions ADD COLUMN backlog_metadata TEXT")

    # agent_ownership column
    cursor.execute("PRAGMA table_info(agent_ownership)")
    ao_cols = {row[1] for row in cursor.fetchall()}
    if "max_backlog_depth" not in ao_cols:
        cursor.execute(
            "ALTER TABLE agent_ownership ADD COLUMN max_backlog_depth INTEGER DEFAULT 50"
        )

    # Partial index for efficient FIFO claim of queued rows
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_queued "
        "ON schedule_executions(agent_name, queued_at) "
        "WHERE status = 'queued'"
    )
    conn.commit()


def _migrate_scheduler_retry_support(cursor, conn):
    """RETRY-001: Scheduler retry mechanism for failed executions.

    Adds:
    - agent_schedules.max_retries: max retry attempts (0=disabled, default 1 for new, 0 for existing)
    - agent_schedules.retry_delay_seconds: delay between retries (default 60, range 30-600)
    - schedule_executions.attempt_number: which attempt this is (1 = first try)
    - schedule_executions.retry_of_execution_id: links retry to original execution
    - schedule_executions.retry_scheduled_at: when retry is scheduled (for restart recovery)

    Note: Existing schedules get max_retries=0 (opt-in) to avoid surprising behavior changes.
    New schedules default to max_retries=1 (resilient by default).
    """
    # agent_schedules columns
    cursor.execute("PRAGMA table_info(agent_schedules)")
    as_cols = {row[1] for row in cursor.fetchall()}

    if "max_retries" not in as_cols:
        # Default 0 for existing schedules (opt-in), schema default 1 for new
        cursor.execute("ALTER TABLE agent_schedules ADD COLUMN max_retries INTEGER DEFAULT 0")
    if "retry_delay_seconds" not in as_cols:
        cursor.execute("ALTER TABLE agent_schedules ADD COLUMN retry_delay_seconds INTEGER DEFAULT 60")

    # schedule_executions columns
    cursor.execute("PRAGMA table_info(schedule_executions)")
    se_cols = {row[1] for row in cursor.fetchall()}

    if "attempt_number" not in se_cols:
        cursor.execute("ALTER TABLE schedule_executions ADD COLUMN attempt_number INTEGER DEFAULT 1")
    if "retry_of_execution_id" not in se_cols:
        cursor.execute("ALTER TABLE schedule_executions ADD COLUMN retry_of_execution_id TEXT")
    if "retry_scheduled_at" not in se_cols:
        cursor.execute("ALTER TABLE schedule_executions ADD COLUMN retry_scheduled_at TEXT")

    # Index for finding pending retries on startup
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_pending_retry "
        "ON schedule_executions(retry_scheduled_at) "
        "WHERE retry_scheduled_at IS NOT NULL AND status = 'pending_retry'"
    )
    conn.commit()


def _migrate_validation_support(cursor, conn):
    """VALIDATE-001: Post-execution business validation.

    Adds:
    - agent_schedules.validation_enabled: enable validation for this schedule (default 0)
    - agent_schedules.validation_prompt: custom auditor instructions
    - agent_schedules.validation_timeout_seconds: timeout for validation task (default 120)
    - schedule_executions.business_status: validation result (pending_validation, validated, failed_validation, skipped)
    - schedule_executions.validated_at: when validation completed
    - schedule_executions.validation_execution_id: FK to the validation execution
    - schedule_executions.validates_execution_id: FK to execution being validated (for validation records)
    """
    # agent_schedules columns
    cursor.execute("PRAGMA table_info(agent_schedules)")
    as_cols = {row[1] for row in cursor.fetchall()}

    if "validation_enabled" not in as_cols:
        cursor.execute("ALTER TABLE agent_schedules ADD COLUMN validation_enabled INTEGER DEFAULT 0")
    if "validation_prompt" not in as_cols:
        cursor.execute("ALTER TABLE agent_schedules ADD COLUMN validation_prompt TEXT")
    if "validation_timeout_seconds" not in as_cols:
        cursor.execute("ALTER TABLE agent_schedules ADD COLUMN validation_timeout_seconds INTEGER DEFAULT 120")

    # schedule_executions columns
    cursor.execute("PRAGMA table_info(schedule_executions)")
    se_cols = {row[1] for row in cursor.fetchall()}

    if "business_status" not in se_cols:
        cursor.execute("ALTER TABLE schedule_executions ADD COLUMN business_status TEXT")
    if "validated_at" not in se_cols:
        cursor.execute("ALTER TABLE schedule_executions ADD COLUMN validated_at TEXT")
    if "validation_execution_id" not in se_cols:
        cursor.execute("ALTER TABLE schedule_executions ADD COLUMN validation_execution_id TEXT")
    if "validates_execution_id" not in se_cols:
        cursor.execute("ALTER TABLE schedule_executions ADD COLUMN validates_execution_id TEXT")

    # Indexes for validation queries
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_business_status "
        "ON schedule_executions(business_status)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_validates "
        "ON schedule_executions(validates_execution_id)"
    )
    conn.commit()


def _migrate_group_auth_mode(cursor, conn):
    """Add group_auth_mode to agent_ownership and verified_by_email to telegram_group_configs.

    Supports: 'none' (default, current behavior), 'any_verified' (at least one verified member).
    """
    # Add group_auth_mode to agent_ownership
    cursor.execute("PRAGMA table_info(agent_ownership)")
    ao_cols = {row[1] for row in cursor.fetchall()}
    if "group_auth_mode" not in ao_cols:
        cursor.execute(
            "ALTER TABLE agent_ownership ADD COLUMN group_auth_mode TEXT DEFAULT 'none'"
        )
        logger.info("Added group_auth_mode column to agent_ownership")

    # Add verified_by_email to telegram_group_configs
    cursor.execute("PRAGMA table_info(telegram_group_configs)")
    tg_cols = {row[1] for row in cursor.fetchall()}
    if "verified_by_email" not in tg_cols:
        cursor.execute(
            "ALTER TABLE telegram_group_configs ADD COLUMN verified_by_email TEXT"
        )
        logger.info("Added verified_by_email column to telegram_group_configs")
    if "verified_at" not in tg_cols:
        cursor.execute(
            "ALTER TABLE telegram_group_configs ADD COLUMN verified_at TEXT"
        )
        logger.info("Added verified_at column to telegram_group_configs")

    conn.commit()


# Ordered list of all migrations. Defined at module level (after all _migrate_* functions)
# so run_all_migrations and the health check can both reference it.
# IMPORTANT: append-only — never reorder or remove entries.
def _migrate_audit_log_table(cursor, conn):
    """Create audit_log table + indexes + immutability triggers (SEC-001 / Issue #20).

    Phase 1 of the platform audit trail. The table is also defined in
    db/schema.py for fresh installs; this migration handles existing installs.
    """
    cursor.execute("PRAGMA table_info(audit_log)")
    if cursor.fetchall():
        return  # already created (fresh install path)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT UNIQUE NOT NULL,
            event_type TEXT NOT NULL,
            event_action TEXT NOT NULL,
            actor_type TEXT NOT NULL,
            actor_id TEXT,
            actor_email TEXT,
            actor_ip TEXT,
            mcp_key_id TEXT,
            mcp_key_name TEXT,
            mcp_scope TEXT,
            target_type TEXT,
            target_id TEXT,
            timestamp TEXT NOT NULL,
            details TEXT,
            request_id TEXT,
            source TEXT NOT NULL,
            endpoint TEXT,
            previous_hash TEXT,
            entry_hash TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    for ddl in [
        "CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp ON audit_log(timestamp DESC)",
        "CREATE INDEX IF NOT EXISTS idx_audit_log_event_type ON audit_log(event_type, timestamp DESC)",
        "CREATE INDEX IF NOT EXISTS idx_audit_log_actor ON audit_log(actor_type, actor_id, timestamp DESC)",
        "CREATE INDEX IF NOT EXISTS idx_audit_log_target ON audit_log(target_type, target_id, timestamp DESC)",
        "CREATE INDEX IF NOT EXISTS idx_audit_log_mcp_key ON audit_log(mcp_key_id, timestamp DESC)",
        "CREATE INDEX IF NOT EXISTS idx_audit_log_request ON audit_log(request_id)",
        # Append-only enforcement: block UPDATE on every row, block DELETE within
        # the 365-day retention window. Operators can purge stale entries after
        # retention via a one-off script that drops the trigger.
        """
        CREATE TRIGGER IF NOT EXISTS audit_log_no_update
        BEFORE UPDATE ON audit_log
        BEGIN
            SELECT RAISE(ABORT, 'Audit log entries cannot be modified');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
        BEFORE DELETE ON audit_log
        WHEN OLD.timestamp > datetime('now', '-365 days')
        BEGIN
            SELECT RAISE(ABORT, 'Audit log entries cannot be deleted within retention period');
        END
        """,
    ]:
        cursor.execute(ddl)

    conn.commit()
    print("Created audit_log table with indexes and immutability triggers (SEC-001)")


def _migrate_agent_git_config_pat(cursor, conn):
    """Add github_pat_encrypted column to agent_git_config for per-agent GitHub PAT (#347)."""
    cursor.execute("PRAGMA table_info(agent_git_config)")
    columns = {row[1] for row in cursor.fetchall()}

    if "github_pat_encrypted" not in columns:
        print("Adding github_pat_encrypted column to agent_git_config for per-agent GitHub PAT...")
        cursor.execute("ALTER TABLE agent_git_config ADD COLUMN github_pat_encrypted TEXT")

    conn.commit()


def _migrate_proactive_messaging(cursor, conn):
    """Add allow_proactive column to agent_sharing for proactive messaging consent (#321).

    Users must explicitly opt-in to receive proactive messages from agents.
    Default is 0 (no proactive messages allowed).
    """
    cursor.execute("PRAGMA table_info(agent_sharing)")
    columns = {row[1] for row in cursor.fetchall()}

    if "allow_proactive" not in columns:
        print("Adding allow_proactive column to agent_sharing for proactive messaging consent...")
        cursor.execute("ALTER TABLE agent_sharing ADD COLUMN allow_proactive INTEGER DEFAULT 0")

    # Create index for efficient lookup of proactive-enabled shares
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_sharing_proactive "
        "ON agent_sharing(agent_name, shared_with_email) WHERE allow_proactive = 1"
    )

    conn.commit()


def _find_duplicate_working_branches(cursor):
    """Return groups of (github_repo, working_branch) pairs bound to >1 agent
    with source_mode=0.

    S7 Layer 2 pre-flight. Duplicates here are the exact bug the 2026-04-17
    alpaca incident was caused by, and they block the UNIQUE index from
    being created on an existing database. The migration surfaces them so
    an operator can rebind the losing agent to a fresh working branch
    before re-running.

    Returns:
        List of dicts: ``{github_repo, working_branch, agent_names}``.
        Empty list if no duplicates.
    """
    cursor.execute(
        """
        SELECT github_repo, working_branch, GROUP_CONCAT(agent_name, ',') AS agents,
               COUNT(*) AS n
        FROM agent_git_config
        WHERE source_mode = 0
        GROUP BY github_repo, working_branch
        HAVING n > 1
        ORDER BY github_repo, working_branch
        """
    )
    rows = cursor.fetchall()

    duplicates = []
    for row in rows:
        github_repo = row[0] if not hasattr(row, "keys") else row["github_repo"]
        working_branch = row[1] if not hasattr(row, "keys") else row["working_branch"]
        agents_csv = row[2] if not hasattr(row, "keys") else row["agents"]
        agent_names = sorted(agents_csv.split(",")) if agents_csv else []
        duplicates.append(
            {
                "github_repo": github_repo,
                "working_branch": working_branch,
                "agent_names": agent_names,
            }
        )
    return duplicates


def _migrate_agent_git_config_branch_ownership(cursor, conn):
    """Add partial UNIQUE index to agent_git_config enforcing branch ownership (S7 Layer 2 / #382).

    Enforces ``UNIQUE(github_repo, working_branch) WHERE source_mode = 0``,
    which makes it impossible for two working-branch agents to be bound to
    the same (repo, branch) pair. Source-mode agents intentionally share
    branches (e.g. every reader pointing at ``main``) and are excluded from
    the constraint.

    Pre-flight: if the database already contains duplicate bindings (e.g.
    from the 2026-04-17 alpaca incident), the migration refuses to create
    the index and raises with an actionable error that names every
    offending row. The operator must rebind duplicates to fresh working
    branches (via the UI / MCP ``initialize_github_sync`` flow) before
    re-running. We never auto-delete rows — that would mask the bug.
    """
    # Idempotency: skip if the index is already present.
    cursor.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND name='idx_git_config_repo_branch_unique'"
    )
    if cursor.fetchone():
        return

    duplicates = _find_duplicate_working_branches(cursor)
    if duplicates:
        report_lines = [
            "Cannot enforce S7 branch-ownership UNIQUE index — "
            "found existing duplicate bindings:",
        ]
        for group in duplicates:
            report_lines.append(
                f"  - repo={group['github_repo']!r} "
                f"branch={group['working_branch']!r} "
                f"agents={group['agent_names']}"
            )
            # Also log each individual agent name so tests / log search
            # can find them easily.
            for agent in group["agent_names"]:
                report_lines.append(f"      offending agent: {agent}")
        report_lines.append(
            "Resolve by assigning a fresh working branch to all but one "
            "agent in each group (see docs/memory/feature-flows/ "
            "github-sync.md), then re-run the migration."
        )
        message = "\n".join(report_lines)
        # Print so the operator sees it even if logging is suppressed.
        print(message)
        logger.error(message)
        raise RuntimeError(message)

    cursor.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_git_config_repo_branch_unique "
        "ON agent_git_config(github_repo, working_branch) WHERE source_mode = 0"
    )
    conn.commit()
    print("Created S7 partial UNIQUE index idx_git_config_repo_branch_unique "
          "on agent_git_config(github_repo, working_branch) WHERE source_mode = 0")


MIGRATIONS = [
    ("agent_sharing", _migrate_agent_sharing_table),
    ("schedule_executions_observability", _migrate_schedule_executions_observability),
    ("mcp_api_keys_agent_scope", _migrate_mcp_api_keys_agent_scope),
    ("agent_ownership_system_flag", _migrate_agent_ownership_system_flag),
    ("agent_ownership_platform_key", _migrate_agent_ownership_platform_key),
    ("agent_git_config_source_branch", _migrate_agent_git_config_source_branch),
    ("agent_ownership_autonomy", _migrate_agent_ownership_autonomy),
    ("agent_ownership_resource_limits", _migrate_agent_ownership_resource_limits),
    ("agent_skills", _migrate_agent_skills_table),
    ("agent_ownership_full_capabilities", _migrate_agent_ownership_full_capabilities),
    ("agent_ownership_read_only_mode", _migrate_agent_ownership_read_only_mode),
    ("agent_schedules_execution_config", _migrate_agent_schedules_execution_config),
    ("agent_notifications", _migrate_agent_notifications_table),
    ("execution_origin_tracking", _migrate_execution_origin_tracking),
    ("execution_session_tracking", _migrate_execution_session_tracking),
    ("subscription_credentials", _migrate_subscription_credentials_table),
    ("agent_ownership_subscription_id", _migrate_agent_ownership_subscription_id),
    ("agent_dashboard_values", _migrate_agent_dashboard_values_table),
    ("setup_completed_backfill", _migrate_setup_completed_backfill),
    ("slack_integration_tables", _migrate_slack_integration_tables),
    ("agent_ownership_parallel_capacity", _migrate_agent_ownership_parallel_capacity),
    ("schedule_model_selection", _migrate_schedule_model_selection),
    ("nevermined_tables", _migrate_nevermined_tables),
    ("agent_avatar_columns", _migrate_agent_avatar_columns),
    ("operator_queue_table", _migrate_operator_queue_table),
    ("agent_ownership_default_avatar", _migrate_agent_ownership_default_avatar),
    ("agent_ownership_execution_timeout", _migrate_agent_ownership_execution_timeout),
    ("public_user_memory_table", _migrate_public_user_memory_table),
    ("subscription_rate_limit_tracking", _migrate_subscription_rate_limit_tracking),
    ("chat_messages_source_column", _migrate_chat_messages_source_column),
    ("agent_ownership_voice_prompt", _migrate_agent_ownership_voice_prompt),
    ("slack_channel_agents", _migrate_slack_channel_agents),
    ("execution_fan_out_id", _migrate_execution_fan_out_id),
    ("telegram_bindings", _migrate_telegram_bindings),
    ("subscription_usage_tracking", _migrate_subscription_usage_tracking),
    ("telegram_group_configs", _migrate_telegram_group_configs),
    ("access_control", _migrate_access_control),
    ("public_link_require_email_unified", _migrate_public_link_require_email_unified),
    ("email_whitelist_default_role", _migrate_email_whitelist_default_role),
    ("backlog_support", _migrate_backlog_support),
    ("scheduler_retry_support", _migrate_scheduler_retry_support),
    ("validation_support", _migrate_validation_support),
    ("audit_log_table", _migrate_audit_log_table),
    ("group_auth_mode", _migrate_group_auth_mode),
    ("agent_ownership_guardrails", _migrate_agent_ownership_guardrails),
    ("agent_git_config_pat", _migrate_agent_git_config_pat),
    ("proactive_messaging", _migrate_proactive_messaging),
    ("agent_git_config_branch_ownership", _migrate_agent_git_config_branch_ownership),
]
