"""
Database migrations for Trinity platform.

Each migration function handles a specific schema change.
Migrations are idempotent - safe to run multiple times.

Migration Order (as of 2026-05-07):
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
30. chat_messages_source_column - Chat message source tracking
31. agent_ownership_voice_prompt - Per-agent voice system prompt
32. slack_channel_agents - SLACK-002 channel-agent bindings
33. execution_fan_out_id - FANOUT-001 fan-out operation linkage
34. telegram_bindings - TELEGRAM-001 Telegram bot integration tables
35. subscription_usage_tracking - SUB-002 usage tracking columns
36. telegram_group_configs - TGRAM-GROUP Telegram group chat configs
37. access_control - #311 unified channel access control (access_requests, verified_email)
38. public_link_require_email_unified - #311 unified require_email flag on public links
39. email_whitelist_default_role - #314 default role on email whitelist rows
40. backlog_support - BACKLOG-001 persistent FIFO overflow store
41. scheduler_retry_support - RETRY-001 scheduler retry mechanism
42. validation_support - VALIDATE-001 post-execution business validation
43. audit_log_table - SEC-001 / #20 platform audit log
44. group_auth_mode - Telegram/Slack group auth mode
45. agent_ownership_guardrails - GUARD-001 per-agent guardrails overrides
46. agent_git_config_pat - #347 per-agent GitHub PAT support
47. proactive_messaging - #321 proactive agent-to-user messaging
48. agent_git_config_branch_ownership - Working-branch ownership tracking
49. sync_health - #389 agent_sync_state for sync health observability
50. whatsapp_bindings - WHATSAPP-001 Twilio WhatsApp integration
51. agent_schedules_webhook - WEBHOOK-001 webhook tokens for schedules
52. agent_shared_files - FILES-001 outbound file sharing
53. agent_sessions_tables - SESSION_TAB --resume-default Session tab
54. session_compact_events - Session compact event tracking
55. public_links_type - SITE-001 type column on public links (chat | site)
56. slack_bot_token_encryption - #453 encrypt Slack bot tokens at rest
57. canary_violations_table - CANARY-001 / Issue #411 invariant harness violations
58. execution_retention_index - #772 partial index on schedule_executions(completed_at)
59. execution_retry_count - #678 retry_count column for reader-race auto-retry
60. agent_compatibility_results_table - #668 latest-snapshot agent compatibility reports
"""
import logging
import sqlite3
from datetime import datetime, timezone
from utils.helpers import utc_now_iso

logger = logging.getLogger(__name__)


def _safe_add_column(cursor, table: str, column: str, ddl: str, *, log_msg: str | None = None) -> bool:
    """Idempotent, concurrent-safe ALTER TABLE ADD COLUMN.

    Two uvicorn workers starting simultaneously can both pass the PRAGMA
    check before either commits its ALTER (#456 / #389). Catch the
    duplicate-column race and treat it as success.

    Returns True if this caller added the column, False if it was already
    present (either by a prior run or another worker winning the race).
    """
    cursor.execute(f"PRAGMA table_info({table})")
    if column in {row[1] for row in cursor.fetchall()}:
        return False
    if log_msg:
        print(log_msg)
    try:
        cursor.execute(ddl)
        return True
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e).lower():
            return False
        raise


def _atomic_rebuild(cursor, conn, table, create_new_sql, copy_sql, *, indexes=()):
    """Atomically rebuild ``table`` via rename-swap inside an explicit transaction.

    Closes the DROP-rebuild data-loss window (#1160). Under Python's legacy
    sqlite3 isolation an *implicit* DDL statement autocommits, so the old code's
    ``DROP TABLE`` committed before the re-INSERT — a crash in between lost the
    rows permanently. An *explicit* ``BEGIN`` holds the drop and re-create in one
    transaction that rolls back cleanly on any failure, so the old table is never
    gone until the new one is committed in the same unit of work.

    ``create_new_sql`` must create ``{table}_new``; ``copy_sql`` must
    ``INSERT INTO {table}_new ... SELECT ... FROM {table}`` (copy happens
    server-side — the rows never live only in a Python list across a commit).
    ``indexes`` are CREATE INDEX statements re-run after the rename.
    """
    new = f"{table}_new"
    conn.commit()  # clear any pending implicit transaction before BEGIN
    try:
        cursor.execute("BEGIN")
        # Drop an orphan _new from a prior crashed attempt before recreating it.
        cursor.execute(f"DROP TABLE IF EXISTS {new}")
        cursor.execute(create_new_sql)
        cursor.execute(copy_sql)
        cursor.execute(f"DROP TABLE {table}")
        cursor.execute(f"ALTER TABLE {new} RENAME TO {table}")
        for idx_sql in indexes:
            cursor.execute(idx_sql)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


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
                # Name the migration on the exception itself (#1160) so it rides
                # the crash-loop traceback, while preserving the original type.
                e.add_note(f"Trinity migration that failed: {name!r}")
                raise
        except Exception as e:
            logger.error("Migration FAILED (%s): %s", name, e, exc_info=True)
            e.add_note(f"Trinity migration that failed: {name!r}")
            raise


def migration_health(cursor):
    """Migration-completeness snapshot for the `/health` check (#1160).

    Returns ``(applied, expected, first_pending)``: how many of ``MIGRATIONS``
    are recorded in ``schema_migrations``, the total expected, and the first
    registered migration not yet recorded (``None`` when all are applied) — so a
    503 names the stuck/pending migration instead of just reporting a count.
    """
    cursor.execute("SELECT name FROM schema_migrations")
    applied_names = {row[0] for row in cursor.fetchall()}
    first_pending = next((name for name, _ in MIGRATIONS if name not in applied_names), None)
    return len(applied_names), len(MIGRATIONS), first_pending


def _migrate_agent_sharing_table(cursor, conn):
    """Migrate agent_sharing table from user_id based to email based."""
    # Check if old schema exists (with shared_with_id column)
    cursor.execute("PRAGMA table_info(agent_sharing)")
    columns = {row[1] for row in cursor.fetchall()}

    if "shared_with_id" in columns and "shared_with_email" not in columns:
        print("Migrating agent_sharing table to email-based schema...")

        # Rename-swap inside one transaction (#1160) — the copy joins users to
        # resolve emails, lowercases them, and drops NULL-email rows, exactly as
        # the previous row-by-row Python loop did (which silently skipped
        # `if share[2]`). Indexes are created by init_schema's index pass, so
        # none are recreated here (matches the prior behavior).
        _atomic_rebuild(
            cursor,
            conn,
            "agent_sharing",
            """
            CREATE TABLE agent_sharing_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name TEXT NOT NULL,
                shared_with_email TEXT NOT NULL,
                shared_by_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (shared_by_id) REFERENCES users(id),
                UNIQUE(agent_name, shared_with_email)
            )
            """,
            """
            INSERT INTO agent_sharing_new
                (agent_name, shared_with_email, shared_by_id, created_at)
            SELECT s.agent_name, lower(u.email), s.shared_by_id, s.created_at
            FROM agent_sharing s
            JOIN users u ON s.shared_with_id = u.id
            WHERE u.email IS NOT NULL AND u.email != ''
            """,
        )
        print("Migrated agent_sharing to email-based schema")


def _migrate_schedule_executions_observability(cursor, conn):
    """Add observability columns to schedule_executions table."""
    new_columns = [
        ("context_used", "INTEGER"),
        ("context_max", "INTEGER"),
        ("cost", "REAL"),
        ("tool_calls", "TEXT"),
        ("execution_log", "TEXT")  # Full Claude Code execution transcript (JSON)
    ]

    for col_name, col_type in new_columns:
        _safe_add_column(
            cursor,
            "schedule_executions",
            col_name,
            f"ALTER TABLE schedule_executions ADD COLUMN {col_name} {col_type}",
            log_msg=f"Adding {col_name} column to schedule_executions...",
        )

    conn.commit()


def _migrate_mcp_api_keys_agent_scope(cursor, conn):
    """Add agent_name and scope columns to mcp_api_keys table for agent-to-agent collaboration."""
    new_columns = [
        ("agent_name", "TEXT"),  # Which agent owns this key (NULL for user keys)
        ("scope", "TEXT DEFAULT 'user'")  # "user" or "agent"
    ]

    for col_name, col_type in new_columns:
        _safe_add_column(
            cursor,
            "mcp_api_keys",
            col_name,
            f"ALTER TABLE mcp_api_keys ADD COLUMN {col_name} {col_type}",
            log_msg=f"Adding {col_name} column to mcp_api_keys for agent collaboration...",
        )

    conn.commit()


def _migrate_agent_ownership_system_flag(cursor, conn):
    """Add is_system column to agent_ownership table for system agent protection."""
    _safe_add_column(
        cursor,
        "agent_ownership",
        "is_system",
        "ALTER TABLE agent_ownership ADD COLUMN is_system INTEGER DEFAULT 0",
        log_msg="Adding is_system column to agent_ownership for system agent protection...",
    )
    conn.commit()


def _migrate_agent_ownership_platform_key(cursor, conn):
    """Add use_platform_api_key column to agent_ownership table for per-agent API key control."""
    _safe_add_column(
        cursor,
        "agent_ownership",
        "use_platform_api_key",
        "ALTER TABLE agent_ownership ADD COLUMN use_platform_api_key INTEGER DEFAULT 1",
        log_msg="Adding use_platform_api_key column to agent_ownership for per-agent API key control...",
    )
    conn.commit()


def _migrate_agent_git_config_source_branch(cursor, conn):
    """Add source_branch and source_mode columns to agent_git_config for GitHub source tracking."""
    new_columns = [
        ("source_branch", "TEXT DEFAULT 'main'"),  # Branch to pull from
        ("source_mode", "INTEGER DEFAULT 0")  # 1 = track source branch directly, 0 = legacy working branch
    ]

    for col_name, col_type in new_columns:
        _safe_add_column(
            cursor,
            "agent_git_config",
            col_name,
            f"ALTER TABLE agent_git_config ADD COLUMN {col_name} {col_type}",
            log_msg=f"Adding {col_name} column to agent_git_config for GitHub source tracking...",
        )

    conn.commit()


def _migrate_agent_ownership_autonomy(cursor, conn):
    """Add autonomy_enabled column to agent_ownership table for autonomous scheduling control."""
    _safe_add_column(
        cursor,
        "agent_ownership",
        "autonomy_enabled",
        "ALTER TABLE agent_ownership ADD COLUMN autonomy_enabled INTEGER DEFAULT 0",
        log_msg="Adding autonomy_enabled column to agent_ownership for autonomous scheduling...",
    )
    conn.commit()


def _migrate_agent_ownership_resource_limits(cursor, conn):
    """Add memory_limit and cpu_limit columns to agent_ownership table for per-agent resource configuration."""
    new_columns = [
        ("memory_limit", "TEXT"),  # e.g., "4g", "8g", "16g"
        ("cpu_limit", "TEXT")  # e.g., "2", "4", "8"
    ]

    for col_name, col_type in new_columns:
        _safe_add_column(
            cursor,
            "agent_ownership",
            col_name,
            f"ALTER TABLE agent_ownership ADD COLUMN {col_name} {col_type}",
            log_msg=f"Adding {col_name} column to agent_ownership for resource configuration...",
        )

    conn.commit()


def _migrate_agent_ownership_full_capabilities(cursor, conn):
    """Add full_capabilities column to agent_ownership table for container security settings."""
    _safe_add_column(
        cursor,
        "agent_ownership",
        "full_capabilities",
        "ALTER TABLE agent_ownership ADD COLUMN full_capabilities INTEGER DEFAULT 0",
        log_msg="Adding full_capabilities column to agent_ownership for container security settings...",
    )
    conn.commit()


def _migrate_agent_ownership_read_only_mode(cursor, conn):
    """Add read_only_mode and read_only_config columns to agent_ownership table for code protection."""
    new_columns = [
        ("read_only_mode", "INTEGER DEFAULT 0"),  # 0 = disabled, 1 = enabled
        ("read_only_config", "TEXT")  # JSON config for blocked/allowed patterns
    ]

    for col_name, col_def in new_columns:
        _safe_add_column(
            cursor,
            "agent_ownership",
            col_name,
            f"ALTER TABLE agent_ownership ADD COLUMN {col_name} {col_def}",
            log_msg=f"Adding {col_name} column to agent_ownership for read-only mode...",
        )

    conn.commit()


def _migrate_agent_schedules_execution_config(cursor, conn):
    """Add timeout_seconds and allowed_tools columns to agent_schedules table for per-schedule execution config."""
    new_columns = [
        ("timeout_seconds", "INTEGER DEFAULT 900"),  # Default 15 minutes
        ("allowed_tools", "TEXT")  # JSON array of allowed tools, NULL = all tools
    ]

    for col_name, col_type in new_columns:
        _safe_add_column(
            cursor,
            "agent_schedules",
            col_name,
            f"ALTER TABLE agent_schedules ADD COLUMN {col_name} {col_type}",
            log_msg=f"Adding {col_name} column to agent_schedules for execution configuration...",
        )

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
    new_columns = [
        ("source_user_id", "INTEGER"),
        ("source_user_email", "TEXT"),
        ("source_agent_name", "TEXT"),
        ("source_mcp_key_id", "TEXT"),
        ("source_mcp_key_name", "TEXT"),
    ]

    for col_name, col_type in new_columns:
        _safe_add_column(
            cursor,
            "schedule_executions",
            col_name,
            f"ALTER TABLE schedule_executions ADD COLUMN {col_name} {col_type}",
            log_msg=f"Adding {col_name} column to schedule_executions for origin tracking...",
        )

    conn.commit()


def _migrate_execution_session_tracking(cursor, conn):
    """Add claude_session_id column to schedule_executions table (EXEC-023).

    Enables "Continue Execution as Chat" feature by storing Claude Code's
    session_id, which can be used with --resume to continue the session.
    """
    _safe_add_column(
        cursor,
        "schedule_executions",
        "claude_session_id",
        "ALTER TABLE schedule_executions ADD COLUMN claude_session_id TEXT",
        log_msg="Adding claude_session_id column to schedule_executions for session resume...",
    )
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
    _safe_add_column(
        cursor,
        "agent_ownership",
        "subscription_id",
        "ALTER TABLE agent_ownership ADD COLUMN subscription_id TEXT REFERENCES subscription_credentials(id)",
        log_msg="Adding subscription_id column to agent_ownership for subscription management...",
    )
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

        # Rename-swap inside one transaction (#1160) — straight column copy
        # (skill_id -> skill_name). Recreates the two indexes the old code did.
        _atomic_rebuild(
            cursor,
            conn,
            "agent_skills",
            """
            CREATE TABLE agent_skills_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name TEXT NOT NULL,
                skill_name TEXT NOT NULL,
                assigned_by TEXT NOT NULL,
                assigned_at TEXT NOT NULL,
                UNIQUE(agent_name, skill_name)
            )
            """,
            """
            INSERT INTO agent_skills_new
                (agent_name, skill_name, assigned_by, assigned_at)
            SELECT agent_name, skill_id, assigned_by, assigned_at FROM agent_skills
            """,
            indexes=(
                "CREATE INDEX IF NOT EXISTS idx_agent_skills_agent ON agent_skills(agent_name)",
                "CREATE INDEX IF NOT EXISTS idx_agent_skills_skill ON agent_skills(skill_name)",
            ),
        )
        print("Migrated agent_skills to skill_name schema")


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
        """, (utc_now_iso(),))
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
    _safe_add_column(
        cursor,
        "agent_ownership",
        "max_parallel_tasks",
        "ALTER TABLE agent_ownership ADD COLUMN max_parallel_tasks INTEGER DEFAULT 3",
        log_msg="Adding max_parallel_tasks column to agent_ownership for parallel capacity...",
    )
    conn.commit()


def _migrate_schedule_model_selection(cursor, conn):
    """Add model columns for MODEL-001: Model Selection for Tasks & Schedules.

    - agent_schedules.model: Model to use when schedule fires (NULL = agent default)
    - schedule_executions.model_used: Records which model was actually used
    """
    _safe_add_column(
        cursor,
        "agent_schedules",
        "model",
        "ALTER TABLE agent_schedules ADD COLUMN model TEXT",
        log_msg="Adding model column to agent_schedules for model selection...",
    )
    _safe_add_column(
        cursor,
        "schedule_executions",
        "model_used",
        "ALTER TABLE schedule_executions ADD COLUMN model_used TEXT",
        log_msg="Adding model_used column to schedule_executions for model audit...",
    )
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
    new_columns = [
        ("avatar_identity_prompt", "TEXT"),
        ("avatar_updated_at", "TEXT"),
    ]

    for col_name, col_type in new_columns:
        _safe_add_column(
            cursor,
            "agent_ownership",
            col_name,
            f"ALTER TABLE agent_ownership ADD COLUMN {col_name} {col_type}",
            log_msg=f"Adding {col_name} column to agent_ownership for avatar support...",
        )

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
    _safe_add_column(
        cursor,
        "agent_ownership",
        "is_default_avatar",
        "ALTER TABLE agent_ownership ADD COLUMN is_default_avatar INTEGER DEFAULT 0",
        log_msg="Adding is_default_avatar column to agent_ownership for default avatar support...",
    )
    conn.commit()


def _migrate_agent_ownership_execution_timeout(cursor, conn):
    """Add execution_timeout_seconds column to agent_ownership table (TIMEOUT-001).

    Per-agent configurable execution timeout. All execution paths (task API, chat,
    scheduler, MCP, paid endpoints) read from this setting when no explicit timeout
    is provided. Default: 900 seconds (15 minutes).
    """
    _safe_add_column(
        cursor,
        "agent_ownership",
        "execution_timeout_seconds",
        "ALTER TABLE agent_ownership ADD COLUMN execution_timeout_seconds INTEGER DEFAULT 900",
        log_msg="Adding execution_timeout_seconds column to agent_ownership for per-agent timeout...",
    )
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
    _safe_add_column(
        cursor,
        "chat_messages",
        "source",
        "ALTER TABLE chat_messages ADD COLUMN source TEXT DEFAULT 'text'",
    )
    conn.commit()


def _migrate_agent_ownership_guardrails(cursor, conn):
    """Add guardrails_config column to agent_ownership (GUARD-001).

    Stores per-agent guardrail overrides (max_turns, disallowed_tools,
    extra_bash_deny, extra_path_deny) as a JSON blob. NULL means "use
    platform baseline only".
    """
    _safe_add_column(
        cursor,
        "agent_ownership",
        "guardrails_config",
        "ALTER TABLE agent_ownership ADD COLUMN guardrails_config TEXT",
    )
    conn.commit()


def _migrate_agent_ownership_circuit_breaker(cursor, conn):
    """Add circuit_breaker_enabled column to agent_ownership (RELIABILITY-007, #526).

    Per-agent opt-in for the dispatch circuit breaker. Default 0 (OFF) — a true
    opt-in canary; the global DISPATCH_BREAKER_ENABLED master switch must ALSO
    be on for the breaker to engage (D7/D11).
    """
    _safe_add_column(
        cursor,
        "agent_ownership",
        "circuit_breaker_enabled",
        "ALTER TABLE agent_ownership ADD COLUMN circuit_breaker_enabled INTEGER DEFAULT 0",
    )
    conn.commit()


def _migrate_agent_ownership_voice_prompt(cursor, conn):
    """Add voice_system_prompt column to agent_ownership (VOICE-005).

    Stores the per-agent voice personality prompt for Gemini Live API.
    """
    _safe_add_column(
        cursor,
        "agent_ownership",
        "voice_system_prompt",
        "ALTER TABLE agent_ownership ADD COLUMN voice_system_prompt TEXT",
    )
    conn.commit()

def _migrate_agent_ownership_voice_name(cursor, conn):
    """Add voice_name column to agent_ownership (#28).

    Persists the per-agent Gemini Live voice (e.g. 'Kore', 'Puck'). NULL means
    unset — the getter falls back to DEFAULT_VOICE_NAME ('Kore'), preserving the
    prior hardcoded behaviour for existing agents.
    """
    _safe_add_column(
        cursor,
        "agent_ownership",
        "voice_name",
        "ALTER TABLE agent_ownership ADD COLUMN voice_name TEXT",
    )
    conn.commit()


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
    _safe_add_column(
        cursor, "chat_messages", "subscription_id",
        "ALTER TABLE chat_messages ADD COLUMN subscription_id TEXT",
    )
    _safe_add_column(
        cursor, "chat_messages", "output_tokens",
        "ALTER TABLE chat_messages ADD COLUMN output_tokens INTEGER",
    )
    _safe_add_column(
        cursor, "chat_sessions", "subscription_id",
        "ALTER TABLE chat_sessions ADD COLUMN subscription_id TEXT",
    )
    _safe_add_column(
        cursor, "schedule_executions", "subscription_id",
        "ALTER TABLE schedule_executions ADD COLUMN subscription_id TEXT",
    )

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
    _safe_add_column(
        cursor,
        "schedule_executions",
        "fan_out_id",
        "ALTER TABLE schedule_executions ADD COLUMN fan_out_id TEXT",
        log_msg="Adding fan_out_id column to schedule_executions for fan-out linkage...",
    )
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
    _safe_add_column(
        cursor, "agent_ownership", "require_email",
        "ALTER TABLE agent_ownership ADD COLUMN require_email INTEGER DEFAULT 0",
    )
    _safe_add_column(
        cursor, "agent_ownership", "open_access",
        "ALTER TABLE agent_ownership ADD COLUMN open_access INTEGER DEFAULT 0",
    )

    # 2. telegram_chat_links columns
    _safe_add_column(
        cursor, "telegram_chat_links", "verified_email",
        "ALTER TABLE telegram_chat_links ADD COLUMN verified_email TEXT",
    )
    _safe_add_column(
        cursor, "telegram_chat_links", "verified_at",
        "ALTER TABLE telegram_chat_links ADD COLUMN verified_at TEXT",
    )

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
    _safe_add_column(
        cursor,
        "email_whitelist",
        "default_role",
        "ALTER TABLE email_whitelist ADD COLUMN default_role TEXT NOT NULL DEFAULT 'user'",
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
    _safe_add_column(
        cursor, "schedule_executions", "queued_at",
        "ALTER TABLE schedule_executions ADD COLUMN queued_at TEXT",
    )
    _safe_add_column(
        cursor, "schedule_executions", "backlog_metadata",
        "ALTER TABLE schedule_executions ADD COLUMN backlog_metadata TEXT",
    )

    # agent_ownership column
    _safe_add_column(
        cursor, "agent_ownership", "max_backlog_depth",
        "ALTER TABLE agent_ownership ADD COLUMN max_backlog_depth INTEGER DEFAULT 50",
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
    - agent_schedules.max_retries: max retry attempts (0=disabled, default 0; #476 flipped from 1)
    - agent_schedules.retry_delay_seconds: delay between retries (default 60, range 30-600)
    - schedule_executions.attempt_number: which attempt this is (1 = first try)
    - schedule_executions.retry_of_execution_id: links retry to original execution
    - schedule_executions.retry_scheduled_at: when retry is scheduled (for restart recovery)

    Note: Existing schedules get max_retries=0 (opt-in) to avoid surprising behavior changes.
    New schedules also default to max_retries=0 since #476 (was 1 at migration time);
    retries amplified load during multi-hour subscription outages.
    """
    # agent_schedules columns
    # Default 0 for existing schedules (opt-in); schema default is also 0 since #476.
    _safe_add_column(
        cursor, "agent_schedules", "max_retries",
        "ALTER TABLE agent_schedules ADD COLUMN max_retries INTEGER DEFAULT 0",
    )
    _safe_add_column(
        cursor, "agent_schedules", "retry_delay_seconds",
        "ALTER TABLE agent_schedules ADD COLUMN retry_delay_seconds INTEGER DEFAULT 60",
    )

    # schedule_executions columns
    _safe_add_column(
        cursor, "schedule_executions", "attempt_number",
        "ALTER TABLE schedule_executions ADD COLUMN attempt_number INTEGER DEFAULT 1",
    )
    _safe_add_column(
        cursor, "schedule_executions", "retry_of_execution_id",
        "ALTER TABLE schedule_executions ADD COLUMN retry_of_execution_id TEXT",
    )
    _safe_add_column(
        cursor, "schedule_executions", "retry_scheduled_at",
        "ALTER TABLE schedule_executions ADD COLUMN retry_scheduled_at TEXT",
    )

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
    _safe_add_column(
        cursor, "agent_schedules", "validation_enabled",
        "ALTER TABLE agent_schedules ADD COLUMN validation_enabled INTEGER DEFAULT 0",
    )
    _safe_add_column(
        cursor, "agent_schedules", "validation_prompt",
        "ALTER TABLE agent_schedules ADD COLUMN validation_prompt TEXT",
    )
    _safe_add_column(
        cursor, "agent_schedules", "validation_timeout_seconds",
        "ALTER TABLE agent_schedules ADD COLUMN validation_timeout_seconds INTEGER DEFAULT 120",
    )

    # schedule_executions columns
    _safe_add_column(
        cursor, "schedule_executions", "business_status",
        "ALTER TABLE schedule_executions ADD COLUMN business_status TEXT",
    )
    _safe_add_column(
        cursor, "schedule_executions", "validated_at",
        "ALTER TABLE schedule_executions ADD COLUMN validated_at TEXT",
    )
    _safe_add_column(
        cursor, "schedule_executions", "validation_execution_id",
        "ALTER TABLE schedule_executions ADD COLUMN validation_execution_id TEXT",
    )
    _safe_add_column(
        cursor, "schedule_executions", "validates_execution_id",
        "ALTER TABLE schedule_executions ADD COLUMN validates_execution_id TEXT",
    )

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
    if _safe_add_column(
        cursor, "agent_ownership", "group_auth_mode",
        "ALTER TABLE agent_ownership ADD COLUMN group_auth_mode TEXT DEFAULT 'none'",
    ):
        logger.info("Added group_auth_mode column to agent_ownership")

    # Add verified_by_email + verified_at to telegram_group_configs
    if _safe_add_column(
        cursor, "telegram_group_configs", "verified_by_email",
        "ALTER TABLE telegram_group_configs ADD COLUMN verified_by_email TEXT",
    ):
        logger.info("Added verified_by_email column to telegram_group_configs")
    if _safe_add_column(
        cursor, "telegram_group_configs", "verified_at",
        "ALTER TABLE telegram_group_configs ADD COLUMN verified_at TEXT",
    ):
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
    _safe_add_column(
        cursor,
        "agent_git_config",
        "github_pat_encrypted",
        "ALTER TABLE agent_git_config ADD COLUMN github_pat_encrypted TEXT",
        log_msg="Adding github_pat_encrypted column to agent_git_config for per-agent GitHub PAT...",
    )
    conn.commit()


def _migrate_sync_health(cursor, conn):
    """Issue #389 (S1): create agent_sync_state + new columns on agent_git_config.

    - agent_sync_state table holds per-agent sync outcome, counters, SHAs.
    - agent_git_config gets auto_sync_enabled and freeze_schedules_if_sync_failing
      so operators can opt in to the 15-min heartbeat and pause schedules when
      sync breaks.
    Idempotent and concurrent-safe: ALTER TABLE goes through _safe_add_column
    which catches the duplicate-column race between workers (#456); CREATE
    TABLE uses IF NOT EXISTS, which SQLite handles atomically.
    """
    # 1. New columns on agent_git_config
    _safe_add_column(
        cursor,
        "agent_git_config",
        "auto_sync_enabled",
        "ALTER TABLE agent_git_config ADD COLUMN auto_sync_enabled INTEGER DEFAULT 0",
        log_msg="Adding auto_sync_enabled column to agent_git_config for auto-sync cadence (#389)...",
    )
    _safe_add_column(
        cursor,
        "agent_git_config",
        "freeze_schedules_if_sync_failing",
        "ALTER TABLE agent_git_config ADD COLUMN freeze_schedules_if_sync_failing INTEGER DEFAULT 0",
        log_msg="Adding freeze_schedules_if_sync_failing column to agent_git_config (#389)...",
    )

    # 2. New agent_sync_state table (CREATE TABLE IF NOT EXISTS is atomic)
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_sync_state (
            agent_name TEXT PRIMARY KEY,
            last_sync_at TEXT,
            last_sync_status TEXT,
            consecutive_failures INTEGER DEFAULT 0,
            last_error_summary TEXT,
            last_remote_sha_main TEXT,
            last_remote_sha_working TEXT,
            ahead_main INTEGER DEFAULT 0,
            behind_main INTEGER DEFAULT 0,
            ahead_working INTEGER DEFAULT 0,
            behind_working INTEGER DEFAULT 0,
            last_check_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (agent_name) REFERENCES agent_ownership(agent_name)
        )
        """
    )

    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_sync_state_status "
        "ON agent_sync_state(last_sync_status, consecutive_failures)"
    )

    conn.commit()


def _migrate_proactive_messaging(cursor, conn):
    """Add allow_proactive column to agent_sharing for proactive messaging consent (#321).

    Users must explicitly opt-in to receive proactive messages from agents.
    Default is 0 (no proactive messages allowed).
    """
    _safe_add_column(
        cursor,
        "agent_sharing",
        "allow_proactive",
        "ALTER TABLE agent_sharing ADD COLUMN allow_proactive INTEGER DEFAULT 0",
        log_msg="Adding allow_proactive column to agent_sharing for proactive messaging consent...",
    )

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


def _migrate_whatsapp_bindings(cursor, conn):
    """Create WhatsApp (Twilio) channel tables (WHATSAPP-001).

    - whatsapp_bindings: one Twilio sender per agent (AccountSid + encrypted AuthToken + from_number).
    - whatsapp_chat_links: WhatsApp user phone → session map, includes verified_email for unified
      access control (#311). Columns added up-front so Phase 2 is additive code only.
    """
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS whatsapp_bindings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name TEXT NOT NULL UNIQUE,
            account_sid TEXT NOT NULL,
            auth_token_encrypted TEXT NOT NULL,
            from_number TEXT NOT NULL,
            messaging_service_sid TEXT,
            display_name TEXT,
            is_sandbox INTEGER DEFAULT 0,
            webhook_secret TEXT NOT NULL UNIQUE,
            webhook_url TEXT,
            enabled INTEGER DEFAULT 1,
            created_by TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_whatsapp_bindings_agent ON whatsapp_bindings(agent_name)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_whatsapp_bindings_webhook ON whatsapp_bindings(webhook_secret)"
    )

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS whatsapp_chat_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            binding_id INTEGER NOT NULL REFERENCES whatsapp_bindings(id),
            wa_user_phone TEXT NOT NULL,
            wa_user_name TEXT,
            session_id TEXT,
            verified_email TEXT,
            verified_at TEXT,
            message_count INTEGER DEFAULT 0,
            last_active TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(binding_id, wa_user_phone)
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_whatsapp_chat_links_binding ON whatsapp_chat_links(binding_id)"
    )

    conn.commit()


def _migrate_voip_tables(cursor, conn):
    """Create VoIP telephony tables (VOIP-001, #1056 — Phase 1).

    - voip_bindings: one Twilio voice sender per agent (AccountSid + encrypted
      AuthToken + from_number + per-binding daily_call_cap). Separate from
      whatsapp_bindings — voice and messaging are different Twilio products.
      `inbound_number` is shipped up-front (nullable) so Phase 2 is additive.
    - voip_call_logs: one row per outbound call; (agent_name, started_at) index
      backs the durable daily-cap count and seeds Phase 3 observability.
    """
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS voip_bindings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name TEXT NOT NULL UNIQUE,
            account_sid TEXT NOT NULL,
            auth_token_encrypted TEXT NOT NULL,
            from_number TEXT NOT NULL,
            inbound_number TEXT,
            webhook_secret TEXT NOT NULL UNIQUE,
            webhook_url TEXT,
            daily_call_cap INTEGER DEFAULT 50,
            display_name TEXT,
            enabled INTEGER DEFAULT 1,
            created_by TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_voip_bindings_agent ON voip_bindings(agent_name)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_voip_bindings_webhook ON voip_bindings(webhook_secret)"
    )

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS voip_call_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            call_id TEXT NOT NULL UNIQUE,
            agent_name TEXT NOT NULL,
            chat_session_id TEXT,
            to_number TEXT NOT NULL,
            direction TEXT NOT NULL DEFAULT 'outbound',
            status TEXT NOT NULL DEFAULT 'initiated',
            twilio_call_sid TEXT,
            initiated_by_user_id INTEGER,
            initiated_by_email TEXT,
            error TEXT,
            started_at TEXT NOT NULL,
            connected_at TEXT,
            ended_at TEXT,
            duration_ms INTEGER
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_voip_call_logs_agent_started "
        "ON voip_call_logs(agent_name, started_at)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_voip_call_logs_call_id ON voip_call_logs(call_id)"
    )

    conn.commit()


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


def _migrate_agent_schedules_webhook(cursor, conn):
    """Add webhook_token and webhook_enabled columns to agent_schedules (WEBHOOK-001, #291)."""
    _safe_add_column(
        cursor, "agent_schedules", "webhook_token",
        "ALTER TABLE agent_schedules ADD COLUMN webhook_token TEXT",
    )
    _safe_add_column(
        cursor, "agent_schedules", "webhook_enabled",
        "ALTER TABLE agent_schedules ADD COLUMN webhook_enabled INTEGER DEFAULT 0",
    )

    # Partial unique index — only applies to rows where a token is set
    cursor.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_schedules_webhook_token "
        "ON agent_schedules(webhook_token) WHERE webhook_token IS NOT NULL"
    )
    conn.commit()


def _migrate_agent_shared_files(cursor, conn):
    """Create agent_shared_files table and add file_sharing_enabled to agent_ownership.

    FILES-001 / amazing-file-outbound: outbound file sharing via public URL that
    inherits the agent's channel-access policy. Agents call the `share_file` MCP
    tool; backend extracts the named file via Docker SDK `get_archive` and mints
    a token-scoped download URL under /api/files/{id}.

    FK uses ON UPDATE CASCADE so rows self-heal if an agent is renamed
    (defense-in-depth alongside the explicit UPDATE in
    db.agent_settings.metadata.rename_agent).
    """
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_shared_files (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            filename TEXT NOT NULL,
            stored_filename TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            mime_type TEXT,
            download_token TEXT UNIQUE NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            revoked_at TEXT,
            one_time INTEGER DEFAULT 0,
            consumed_at TEXT,
            download_count INTEGER DEFAULT 0,
            last_downloaded_at TEXT,
            FOREIGN KEY (agent_name) REFERENCES agent_ownership(agent_name)
                ON DELETE CASCADE ON UPDATE CASCADE
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_files_agent ON agent_shared_files(agent_name)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_files_token ON agent_shared_files(download_token)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_files_expires ON agent_shared_files(expires_at) WHERE revoked_at IS NULL"
    )

    cursor.execute("PRAGMA table_info(agent_ownership)")
    columns = {row[1] for row in cursor.fetchall()}
    if "file_sharing_enabled" not in columns:
        cursor.execute(
            "ALTER TABLE agent_ownership ADD COLUMN file_sharing_enabled INTEGER DEFAULT 0"
        )

    conn.commit()


def _migrate_agent_sessions_tables(cursor, conn):
    """Create agent_sessions + agent_session_messages tables (Session tab).

    The Session tab is a parallel surface to Chat that uses
    ``claude --print --resume <uuid>`` to preserve tool memory and reasoning
    state across turns. Each ``agent_sessions`` row owns one Claude Code
    JSONL via ``cached_claude_session_id``. See
    ``docs/planning/SESSION_TAB_2026-04.md``.

    Schema lives independently from ``chat_sessions`` / ``chat_messages``;
    no foreign keys or shared columns. Cleanup on session delete cascades
    to messages.
    """
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_sessions (
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
            subscription_id TEXT,
            cached_claude_session_id TEXT,
            last_resume_at TEXT,
            consecutive_resume_failures INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_session_messages (
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
            cache_read_tokens INTEGER,
            tool_calls TEXT,
            execution_time_ms INTEGER,
            claude_session_id TEXT,
            FOREIGN KEY (session_id) REFERENCES agent_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_sessions_agent_user "
        "ON agent_sessions(agent_name, user_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_sessions_status "
        "ON agent_sessions(status)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_session_messages_session "
        "ON agent_session_messages(session_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_session_messages_user "
        "ON agent_session_messages(user_id)"
    )
    conn.commit()


def _migrate_session_compact_events(cursor, conn):
    """Add compact-event observability columns (auto-compact surfacing).

    Claude Code's auto-compact (~85% of the model window) silently summarizes
    ~170k of conversation into a ~10k summary mid-turn. The events appear in
    the stream-json output as ``{"type":"system","subtype":"compact_boundary"}``.
    Capturing them gives users honest signal about what happened to a long-
    running session beyond the silent latency cost.

    Three additive, nullable columns:
      - ``agent_session_messages.compact_metadata`` — JSON list of events that
        fired during the turn that produced this assistant message.
      - ``agent_sessions.compact_count`` — running tally for fast reads;
        drives the inline reset hint without scanning per-message rows.
      - ``schedule_executions.compact_metadata`` — same JSON, denormalized so
        TasksPanel can render the badge without joining session messages.
    """
    _safe_add_column(
        cursor,
        "agent_session_messages",
        "compact_metadata",
        "ALTER TABLE agent_session_messages ADD COLUMN compact_metadata TEXT",
        log_msg="Adding compact_metadata column to agent_session_messages...",
    )
    _safe_add_column(
        cursor,
        "agent_sessions",
        "compact_count",
        "ALTER TABLE agent_sessions ADD COLUMN compact_count INTEGER DEFAULT 0",
        log_msg="Adding compact_count column to agent_sessions...",
    )
    _safe_add_column(
        cursor,
        "schedule_executions",
        "compact_metadata",
        "ALTER TABLE schedule_executions ADD COLUMN compact_metadata TEXT",
        log_msg="Adding compact_metadata column to schedule_executions...",
    )
    conn.commit()


def _migrate_public_links_type(cursor, conn):
    """Add type column to agent_public_links for SITE-001.

    'chat' is the legacy default; 'site' is the new type for live web-server proxying.
    """
    cursor.execute("PRAGMA table_info(agent_public_links)")
    columns = {row[1] for row in cursor.fetchall()}
    if "type" not in columns:
        cursor.execute(
            "ALTER TABLE agent_public_links ADD COLUMN type TEXT NOT NULL DEFAULT 'chat'"
        )
    conn.commit()


def _migrate_slack_bot_token_encryption(cursor, conn):
    """Encrypt plaintext Slack bot tokens at rest (#453).

    Walks `slack_link_connections.slack_bot_token` and `slack_workspaces.bot_token`
    once. For each row whose value starts with `xoxb-` (real Slack bot tokens
    always do), re-encrypt in place via `CredentialEncryptionService` (AES-256-GCM,
    JSON envelope) — same primitive Telegram and WhatsApp bindings already use.

    Idempotent at row level: rows that are already JSON envelopes are skipped.
    Idempotent at migration level: the runner tracks applied migrations in
    `schema_migrations` and won't re-run this on subsequent restarts.

    Hard-fail on missing CREDENTIAL_ENCRYPTION_KEY: matches the pattern already
    used by subscription_credentials, nevermined_agent_config, and the channel
    encryption helpers in db/{telegram,whatsapp,slack}_channels.py. The key MUST
    be set before this migration runs; otherwise the backend won't start.

    Background context: `slack_workspaces.bot_token` was rolled out with lazy
    encryption (encrypt-on-write + plaintext-fallback-on-read at
    `slack_channels.py:47-49`). That left two sources of plaintext on disk:
    rows written before the encryption rollout, and rows copied from the
    legacy `slack_link_connections` table by `_migrate_slack_channel_agents`.
    This one-shot pass closes both gaps. See project memory
    `project_453_slack_encryption.md` for full background.
    """
    from services.credential_encryption import CredentialEncryptionService
    encryption = CredentialEncryptionService()  # raises ValueError if key unset

    def _encrypt_table(table_name: str, token_column: str) -> tuple[int, int]:
        """Returns (encrypted_count, skipped_count). Skips rows that already
        look like JSON envelopes (the xoxb-* prefix is the only signal we
        trust for "this is plaintext that needs encrypting").
        """
        # Defensive: skip if the table doesn't exist on this DB
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        )
        if not cursor.fetchone():
            logger.info(f"#453 migration: table {table_name} not present, skipping")
            return (0, 0)

        cursor.execute(f"SELECT id, {token_column} FROM {table_name}")
        rows = cursor.fetchall()
        encrypted, skipped = 0, 0
        for row_id, value in rows:
            if not value or not value.startswith("xoxb-"):
                # Already encrypted (JSON envelope) or NULL — leave alone
                skipped += 1
                continue
            envelope = encryption.encrypt({"bot_token": value})
            cursor.execute(
                f"UPDATE {table_name} SET {token_column} = ? WHERE id = ?",
                (envelope, row_id),
            )
            encrypted += 1
        return (encrypted, skipped)

    link_enc, link_skip = _encrypt_table("slack_link_connections", "slack_bot_token")
    ws_enc, ws_skip = _encrypt_table("slack_workspaces", "bot_token")

    if link_enc or ws_enc:
        logger.info(
            f"#453 migration: encrypted {link_enc} slack_link_connections rows + "
            f"{ws_enc} slack_workspaces rows (skipped {link_skip + ws_skip} already-encrypted)"
        )
    else:
        logger.info(
            f"#453 migration: nothing to encrypt — all {link_skip + ws_skip} rows "
            f"already encrypted (or empty tables)"
        )
    conn.commit()


def _migrate_canary_violations_table(cursor, conn):
    """Create canary_violations table + indexes (CANARY-001 / Issue #411 — Phase 1).

    Stores orchestration-invariant violations recorded by the continuous
    canary harness. Schema is also defined in db/schema.py for fresh
    installs; this migration handles existing installs.
    """
    cursor.execute("PRAGMA table_info(canary_violations)")
    if cursor.fetchall():
        return  # already created (fresh-install path via init_schema)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS canary_violations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invariant_id TEXT NOT NULL,
            tier TEXT NOT NULL,
            severity TEXT NOT NULL,
            snapshot_time TEXT NOT NULL,
            observed_state TEXT NOT NULL,
            signal_query TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    for ddl in [
        "CREATE INDEX IF NOT EXISTS idx_canary_violations_invariant "
        "ON canary_violations(invariant_id, snapshot_time DESC)",
        "CREATE INDEX IF NOT EXISTS idx_canary_violations_severity "
        "ON canary_violations(severity, snapshot_time DESC)",
        "CREATE INDEX IF NOT EXISTS idx_canary_violations_snapshot "
        "ON canary_violations(snapshot_time DESC)",
    ]:
        cursor.execute(ddl)

    conn.commit()
    print("Created canary_violations table with indexes (CANARY-001)")


def _migrate_execution_retry_count(cursor, conn):
    """Issue #678: track auto-retry attempts on schedule_executions.

    Backend's HTTPError handler detects the reader-race signature and
    retries once with the same execution_id (cap at 1). The column lets
    operators distinguish first-attempt failures from auto-retry outcomes
    and is surfaced through the `get_execution_result` MCP tool so callers
    can see when an implicit retry fired.
    """
    _safe_add_column(
        cursor,
        "schedule_executions",
        "retry_count",
        "ALTER TABLE schedule_executions ADD COLUMN retry_count INTEGER DEFAULT 0",
        log_msg="Issue #678: adding retry_count to schedule_executions",
    )
    conn.commit()


def _migrate_execution_retention_index(cursor, conn):
    """Issue #772: partial index on schedule_executions(completed_at) for terminal rows.

    Drives the execution_log null sweep and the row-delete sweep in
    cleanup_service. Partial predicate matches the WHERE clause of both sweeps
    so the planner uses an index range scan rather than a table scan.
    Idempotent: CREATE INDEX IF NOT EXISTS.
    """
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_completed_terminal "
        "ON schedule_executions(completed_at) "
        "WHERE status IN ('completed', 'failed', 'terminated')"
    )
    conn.commit()


def _migrate_fix_retention_index_status_values(cursor, conn):
    """Issue #862: fix idx_executions_completed_terminal to use correct status values.

    The original migration (_migrate_execution_retention_index, #772) created
    the partial index with status IN ('completed', 'failed', 'terminated'), but
    TaskExecutionStatus uses 'success', 'failed', 'cancelled', 'skipped'.
    Only 'failed' rows were ever in the index — 'success' rows (99%+) were
    silently excluded, causing the execution_log and row-delete sweeps to
    report 0 pruned rows on every cycle despite eligible rows accumulating.

    Fix: drop the wrong index and recreate with the correct status set.
    CREATE INDEX IF NOT EXISTS alone would be a no-op on existing installs
    because the index already exists (with wrong predicate).
    """
    cursor.execute("DROP INDEX IF EXISTS idx_executions_completed_terminal")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_completed_terminal "
        "ON schedule_executions(completed_at) "
        "WHERE status IN ('success', 'failed', 'cancelled', 'skipped')"
    )
    conn.commit()


def _migrate_null_legacy_schedule_timeouts(cursor, conn):
    """Issue #913: NULL out legacy default schedule timeouts so the
    per-agent value actually takes effect.

    Before #913, `agent_schedules.timeout_seconds` was always populated
    (DEFAULT 900, later rewritten to 3600 by
    `_migrate_default_execution_timeout_to_3600`). The scheduler passed
    that value directly to `/api/internal/execute-task`, which meant the
    backend's per-agent fallback at `task_execution_service.py:281` was
    dead code on the scheduler path — and `PUT /api/agents/{name}/timeout`
    was silently ineffective for scheduled runs.

    The fix at the code layer is to round-trip None through the scheduler
    so the backend's per-agent fallback fires. But existing rows still
    carry a concrete 900/3600 value that came from the platform default,
    not from an operator choice — so the canary harness keeps firing S-03
    (slot TTL below per-agent floor) and E-01 (terminal-state closure)
    until those rows are cleared.

    We null out only rows whose value matches one of the historical
    defaults (900, 3600). Operators who explicitly set a different value
    are untouched. Same fidelity tradeoff as
    `_migrate_default_execution_timeout_to_3600` — accepted there for
    #665, accepted here for #913.

    Idempotent: subsequent runs find no matching rows and no-op.
    """
    cursor.execute(
        "UPDATE agent_schedules SET timeout_seconds = NULL "
        "WHERE timeout_seconds IN (900, 3600)"
    )
    conn.commit()


def _migrate_default_execution_timeout_to_3600(cursor, conn):
    """Issue #665: bump default chat execution timeout from 15min → 60min.

    Rewrites any `agent_ownership.execution_timeout_seconds` row that
    is still at the old 900s default to the new 3600s. Rows where the
    operator explicitly set a different value (which is every value
    except 900) are untouched — the assumption is that 900 means
    "never customised; tracking the platform default."

    Risk: an operator who deliberately set 900 to keep the old window
    gets upgraded. Acceptable per the issue text; the per-agent
    timeout endpoint (PUT /api/agents/{name}/timeout) is the explicit
    override for that case.

    Idempotent: subsequent runs find nothing at 900 and no-op.
    """
    cursor.execute(
        "UPDATE agent_ownership SET execution_timeout_seconds = 3600 "
        "WHERE execution_timeout_seconds = 900"
    )
    # Per-schedule override column has its own DEFAULT 900 in the DDL.
    # Without this bump the watchdog's
    # COALESCE(s.timeout_seconds, ao.execution_timeout_seconds, 900)
    # would still pick up 900 from the schedule row before falling
    # through to the agent's 3600 — defeating the intent of #665 for
    # scheduled tasks.
    cursor.execute(
        "UPDATE agent_schedules SET timeout_seconds = 3600 "
        "WHERE timeout_seconds = 900"
    )
    conn.commit()


def _migrate_agent_schedules_soft_delete(cursor, conn):
    """Issue #834 Phase 1b: soft-delete column + partial index on agent_schedules.

    Mirrors Phase 1a's agent_ownership column. `DELETE /api/agents/{name}/
    schedules/{id}` becomes UPDATE deleted_at; the cleanup_service sweep
    hard-deletes rows past `schedule_soft_delete_retention_days`.

    Partial index narrows the retention-sweep scan to actually-deleted
    rows so it stays cheap as the schedule count grows.
    """
    _safe_add_column(
        cursor,
        "agent_schedules",
        "deleted_at",
        "ALTER TABLE agent_schedules ADD COLUMN deleted_at TEXT",
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_schedules_deleted_at "
        "ON agent_schedules(deleted_at) WHERE deleted_at IS NOT NULL"
    )
    conn.commit()


def _migrate_agent_ownership_soft_delete(cursor, conn):
    """Issue #834 Phase 1a: soft-delete column + partial index on agent_ownership.

    Adds `deleted_at TEXT` (NULL = live). The hard-delete path on
    `agent_ownership` becomes a soft-delete (UPDATE deleted_at = now);
    the retention sweep in cleanup_service hard-deletes rows whose
    deleted_at is older than the configured retention window (default
    180 days) and runs the #816 cascade_delete at that point to clean
    child tables.

    The partial index narrows the retention-sweep scan to
    actually-deleted rows so it stays cheap as the live agent count
    grows.
    """
    _safe_add_column(
        cursor,
        "agent_ownership",
        "deleted_at",
        "ALTER TABLE agent_ownership ADD COLUMN deleted_at TEXT",
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_ownership_deleted_at "
        "ON agent_ownership(deleted_at) WHERE deleted_at IS NOT NULL"
    )
    conn.commit()


def _migrate_idempotency_keys_table(cursor, conn):
    """Create idempotency_keys table + index (RELIABILITY-006 / Issue #525).

    Backs the Idempotency-Key contract at every execution trigger boundary.
    Schema is also defined in db/schema.py for fresh installs; this migration
    handles existing installs.
    """
    cursor.execute("PRAGMA table_info(idempotency_keys)")
    if cursor.fetchall():
        return  # already created (fresh-install path via init_schema)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS idempotency_keys (
            scope TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            execution_id TEXT,
            status TEXT NOT NULL,
            response_snapshot TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (scope, idempotency_key)
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_idempotency_created "
        "ON idempotency_keys(created_at)"
    )
    conn.commit()
    print("Created idempotency_keys table with index (RELIABILITY-006, #525)")


def _migrate_agent_loops_tables(cursor, conn):
    """Create agent_loops + agent_loop_runs tables and link to schedule_executions (#740).

    Adds the two new tables for sequential bounded task execution and a
    `loop_id` column on `schedule_executions` so each iteration's
    execution row joins back to its parent loop (timeline tagging).
    """
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_loops (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            message_template TEXT NOT NULL,
            max_runs INTEGER NOT NULL,
            stop_signal TEXT,
            delay_seconds INTEGER NOT NULL DEFAULT 0,
            timeout_per_run INTEGER,
            model TEXT,
            allowed_tools TEXT,
            status TEXT NOT NULL,
            runs_completed INTEGER NOT NULL DEFAULT 0,
            stop_reason TEXT,
            last_response TEXT,
            error TEXT,
            started_by_user_id INTEGER,
            started_by_user_email TEXT,
            source_agent_name TEXT,
            source_mcp_key_id TEXT,
            source_mcp_key_name TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_loop_runs (
            id TEXT PRIMARY KEY,
            loop_id TEXT NOT NULL,
            run_number INTEGER NOT NULL,
            execution_id TEXT,
            status TEXT NOT NULL,
            response TEXT,
            error TEXT,
            cost REAL,
            duration_ms INTEGER,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY (loop_id) REFERENCES agent_loops(id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_loops_agent ON agent_loops(agent_name)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_loops_status ON agent_loops(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_loops_user ON agent_loops(started_by_user_id)")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_loop_runs_loop ON agent_loop_runs(loop_id, run_number)"
    )
    _safe_add_column(
        cursor,
        "schedule_executions",
        "loop_id",
        "ALTER TABLE schedule_executions ADD COLUMN loop_id TEXT",
        log_msg="Adding loop_id column to schedule_executions for #740 loop linkage...",
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_loop "
        "ON schedule_executions(loop_id) WHERE loop_id IS NOT NULL"
    )
    conn.commit()


def _migrate_users_suspended_at(cursor, conn):
    """#995 — user deactivation primitive.

    Adds `suspended_at TEXT` (NULL = active) to `users`. The OSS auth path
    (`get_current_user`) rejects any user whose `suspended_at` is set, so
    setting it blocks new logins AND invalidates live tokens on the next
    request. Edition-agnostic primitive: only the enterprise
    user-management module exposes a way to set/clear it (core-primitive +
    enterprise-knob, same pattern as #834).
    """
    _safe_add_column(
        cursor,
        "users",
        "suspended_at",
        "ALTER TABLE users ADD COLUMN suspended_at TEXT",
    )
    conn.commit()


def _migrate_activities_created_index(cursor, conn):
    """Issue #1265: standalone index on agent_activities(created_at DESC).

    The cross-agent timeline (/api/activities/timeline) sorts by created_at DESC
    with no agent_name predicate, which the composite idx_activities_agent
    (agent_name, created_at DESC) cannot serve as a sort — it degraded to a
    full scan + sort as activity volume grew with fleet size. Idempotent.
    """
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_activities_created "
        "ON agent_activities(created_at DESC)"
    )
    conn.commit()


def _migrate_operator_queue_cleared_at(cursor, conn):
    """#1017 — Clear All on the Operations Resolved tab.

    Adds `cleared_at TEXT` (NULL = visible) to `operator_queue`. Clearing
    the Resolved tab hides rows rather than deleting them: a DELETE would
    let the 5s sync loop resurrect any item whose agent-file entry still
    says 'pending' (always true for expired items, and for cancelled items
    whose status flip hasn't been written back yet). Actual deletion is the
    retention sweep's job (#1142).
    """
    _safe_add_column(
        cursor,
        "operator_queue",
        "cleared_at",
        "ALTER TABLE operator_queue ADD COLUMN cleared_at TEXT",
    )
    conn.commit()


def _migrate_agent_compatibility_results_table(cursor, conn):
    """Create agent_compatibility_results table (#668).

    Latest-snapshot-per-agent compatibility report (one row, upserted by
    agent_name). Schema is also defined in db/schema.py for fresh installs;
    this migration handles existing installs. Idempotent.
    """
    cursor.execute("PRAGMA table_info(agent_compatibility_results)")
    if cursor.fetchall():
        return  # already created (fresh-install path via init_schema)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_compatibility_results (
            agent_name TEXT PRIMARY KEY,
            overall_status TEXT NOT NULL,
            checks_json TEXT NOT NULL,
            hard_count INTEGER NOT NULL DEFAULT 0,
            soft_count INTEGER NOT NULL DEFAULT 0,
            info_count INTEGER NOT NULL DEFAULT 0,
            container_running INTEGER NOT NULL DEFAULT 0,
            ai_ran_at TEXT,
            static_ran_at TEXT,
            updated_at TEXT NOT NULL
        )
    """)
    conn.commit()
    print("Created agent_compatibility_results table (#668)")


def _migrate_agent_loops_max_duration(cursor, conn):
    """#1156 — loop-level wall-clock deadline.

    Adds `max_duration_seconds INTEGER` (NULL = no deadline) to `agent_loops`.
    The runner stops the loop at the next iteration boundary once the deadline
    measured from `started_at` is exceeded (stop_reason='deadline_exceeded'),
    bounding total loop duration alongside the existing `max_runs` cap.
    """
    _safe_add_column(
        cursor,
        "agent_loops",
        "max_duration_seconds",
        "ALTER TABLE agent_loops ADD COLUMN max_duration_seconds INTEGER",
    )
    conn.commit()


def _migrate_agent_reports_table(cursor, conn):
    """Create agent_reports table (#918).

    Agent-published structured reports (telemetry / domain reports) surfaced on
    the dashboard. Schema is also defined in db/schema.py for fresh installs;
    this migration handles existing installs. Idempotent. Mirrored by the
    Alembic revision 0006_agent_reports for PostgreSQL.
    """
    cursor.execute("PRAGMA table_info(agent_reports)")
    if cursor.fetchall():
        return  # already created (fresh-install path via init_schema)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_reports (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            user_id INTEGER,
            report_type TEXT NOT NULL,
            title TEXT NOT NULL,
            payload TEXT NOT NULL,
            display_hint TEXT,
            schema_version INTEGER DEFAULT 1,
            period_start TEXT,
            period_end TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_reports_agent "
        "ON agent_reports(agent_name, created_at DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_reports_type "
        "ON agent_reports(report_type, created_at DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_reports_created "
        "ON agent_reports(created_at)"
    )
    conn.commit()
    print("Created agent_reports table (#918)")


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
    ("sync_health", _migrate_sync_health),
    ("whatsapp_bindings", _migrate_whatsapp_bindings),
    ("agent_schedules_webhook", _migrate_agent_schedules_webhook),
    ("agent_shared_files", _migrate_agent_shared_files),
    ("agent_sessions_tables", _migrate_agent_sessions_tables),
    ("session_compact_events", _migrate_session_compact_events),
    ("public_links_type", _migrate_public_links_type),
    ("slack_bot_token_encryption", _migrate_slack_bot_token_encryption),
    ("canary_violations_table", _migrate_canary_violations_table),
    ("execution_retention_index", _migrate_execution_retention_index),
    ("default_execution_timeout_to_3600", _migrate_default_execution_timeout_to_3600),
    ("fix_retention_index_status_values", _migrate_fix_retention_index_status_values),
    ("agent_ownership_soft_delete", _migrate_agent_ownership_soft_delete),
    ("execution_retry_count", _migrate_execution_retry_count),
    ("agent_schedules_soft_delete", _migrate_agent_schedules_soft_delete),
    # #913 — must come AFTER default_execution_timeout_to_3600 so we null out
    # both 900 (legacy) and 3600 (post-#665 rewrite) in one pass.
    ("null_legacy_schedule_timeouts", _migrate_null_legacy_schedule_timeouts),
    ("idempotency_keys_table", _migrate_idempotency_keys_table),
    ("agent_loops_tables", _migrate_agent_loops_tables),
    ("users_suspended_at", _migrate_users_suspended_at),
    ("agent_ownership_circuit_breaker", _migrate_agent_ownership_circuit_breaker),
    ("voip_tables", _migrate_voip_tables),
    ("operator_queue_cleared_at", _migrate_operator_queue_cleared_at),
    ("activities_created_index", _migrate_activities_created_index),
    ("agent_compatibility_results_table", _migrate_agent_compatibility_results_table),
    ("agent_ownership_voice_name", _migrate_agent_ownership_voice_name),
    ("agent_loops_max_duration", _migrate_agent_loops_max_duration),
    ("agent_reports_table", _migrate_agent_reports_table),
]
