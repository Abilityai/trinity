"""
SQLAlchemy Core table definitions (#300) — QUERY-LAYER handles.

AUTO-DERIVED from db/schema.py (the single DDL source of truth) by
reflecting the sqlite schema it produces. These Table objects are used
ONLY to build dialect-agnostic queries in the migrated db/*.py modules
(table.c.<col>); schema CREATION is owned by schema.py on both backends
(init_schema for sqlite, init_schema_postgres for PostgreSQL). Column
types are coarse (Integer/Float/Text) — sufficient for query building and
matching the sqlite storage classes. Regenerate when schema.py changes.
"""

from sqlalchemy import Column, Float, MetaData, Table, Text
from sqlalchemy import Integer as _Integer
from sqlalchemy.types import TypeDecorator


class Integer(TypeDecorator):
    """INTEGER column that coerces Python bool -> 0/1 on bind (#300).

    Many INTEGER columns store booleans (enabled, is_*, *_mode, ...). SQLite
    silently accepts Python True/False for an INTEGER column, but PostgreSQL
    (psycopg2) renders them as SQL boolean and rejects assignment into an
    integer column (DatatypeMismatch). Coercing bool->int in the bind
    processor fixes the whole class of bug at the binding layer, on both
    backends, so call sites can keep passing Python bools as the pre-#300
    sqlite3 code did.
    """

    impl = _Integer
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if isinstance(value, bool):
            return int(value)
        return value


metadata = MetaData()

users = Table(
    "users",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("username", Text),
    Column("password_hash", Text),
    Column("role", Text),
    Column("auth0_sub", Text),
    Column("name", Text),
    Column("picture", Text),
    Column("email", Text),
    Column("created_at", Text),
    Column("updated_at", Text),
    Column("last_login", Text),
    Column("suspended_at", Text),
)

subscription_credentials = Table(
    "subscription_credentials",
    metadata,
    Column("id", Text, primary_key=True),
    Column("name", Text),
    Column("encrypted_credentials", Text),
    Column("subscription_type", Text),
    Column("rate_limit_tier", Text),
    Column("owner_id", Integer),
    Column("created_at", Text),
    Column("updated_at", Text),
)

agent_ownership = Table(
    "agent_ownership",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("agent_name", Text),
    Column("owner_id", Integer),
    Column("created_at", Text),
    Column("is_system", Integer),
    Column("use_platform_api_key", Integer),
    Column("autonomy_enabled", Integer),
    Column("memory_limit", Text),
    Column("cpu_limit", Text),
    Column("full_capabilities", Integer),
    Column("read_only_mode", Integer),
    Column("read_only_config", Text),
    Column("subscription_id", Text),
    Column("max_parallel_tasks", Integer),
    Column("execution_timeout_seconds", Integer),
    Column("avatar_identity_prompt", Text),
    Column("avatar_updated_at", Text),
    Column("is_default_avatar", Integer),
    Column("require_email", Integer),
    Column("open_access", Integer),
    Column("max_backlog_depth", Integer),
    Column("group_auth_mode", Text),
    Column("voice_system_prompt", Text),
    Column("voice_name", Text),
    Column("public_channel_model", Text),  # #894: per-agent public-channel model override (NULL = platform default)
    Column("guardrails_config", Text),
    Column("file_sharing_enabled", Integer),
    Column("circuit_breaker_enabled", Integer),
    Column("mcp_exposed", Integer),
    Column("deleted_at", Text),
)

agent_sharing = Table(
    "agent_sharing",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("agent_name", Text),
    Column("shared_with_email", Text),
    Column("shared_by_id", Integer),
    Column("created_at", Text),
    Column("allow_proactive", Integer),
)

mcp_api_keys = Table(
    "mcp_api_keys",
    metadata,
    Column("id", Text, primary_key=True),
    Column("name", Text),
    Column("description", Text),
    Column("key_prefix", Text),
    Column("key_hash", Text),
    Column("created_at", Text),
    Column("last_used_at", Text),
    Column("usage_count", Integer),
    Column("is_active", Integer),
    Column("user_id", Integer),
    Column("agent_name", Text),
    Column("scope", Text),
)

email_whitelist = Table(
    "email_whitelist",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("email", Text),
    Column("added_by", Text),
    Column("added_at", Text),
    Column("source", Text),
    Column("default_role", Text),
)

email_login_codes = Table(
    "email_login_codes",
    metadata,
    Column("id", Text, primary_key=True),
    Column("email", Text),
    Column("code", Text),
    Column("created_at", Text),
    Column("expires_at", Text),
    Column("verified", Integer),
    Column("used_at", Text),
)

agent_schedules = Table(
    "agent_schedules",
    metadata,
    Column("id", Text, primary_key=True),
    Column("agent_name", Text),
    Column("name", Text),
    Column("cron_expression", Text),
    Column("message", Text),
    Column("enabled", Integer),
    Column("timezone", Text),
    Column("description", Text),
    Column("owner_id", Integer),
    Column("created_at", Text),
    Column("updated_at", Text),
    Column("last_run_at", Text),
    Column("next_run_at", Text),
    Column("timeout_seconds", Integer),
    Column("allowed_tools", Text),
    Column("model", Text),
    Column("max_retries", Integer),
    Column("retry_delay_seconds", Integer),
    Column("validation_enabled", Integer),
    Column("validation_prompt", Text),
    Column("validation_timeout_seconds", Integer),
    Column("webhook_token", Text),
    Column("webhook_enabled", Integer),
    Column("deleted_at", Text),
)

schedule_executions = Table(
    "schedule_executions",
    metadata,
    Column("id", Text, primary_key=True),
    Column("schedule_id", Text),
    Column("agent_name", Text),
    Column("status", Text),
    Column("started_at", Text),
    Column("completed_at", Text),
    Column("duration_ms", Integer),
    Column("message", Text),
    Column("response", Text),
    Column("error", Text),
    Column("triggered_by", Text),
    Column("context_used", Integer),
    Column("context_max", Integer),
    Column("cost", Float),
    Column("tool_calls", Text),
    Column("execution_log", Text),
    Column("model_used", Text),
    Column("subscription_id", Text),
    Column("attempt_number", Integer),
    Column("retry_of_execution_id", Text),
    Column("retry_scheduled_at", Text),
    Column("business_status", Text),
    Column("validated_at", Text),
    Column("validation_execution_id", Text),
    Column("validates_execution_id", Text),
    Column("compact_metadata", Text),
    Column("source_user_id", Integer),
    Column("source_user_email", Text),
    Column("source_agent_name", Text),
    Column("source_mcp_key_id", Text),
    Column("source_mcp_key_name", Text),
    Column("claude_session_id", Text),
    Column("queued_at", Text),
    Column("backlog_metadata", Text),
    Column("fan_out_id", Text),
    Column("retry_count", Integer),
    Column("loop_id", Text),
)

agent_loops = Table(
    "agent_loops",
    metadata,
    Column("id", Text, primary_key=True),
    Column("agent_name", Text),
    Column("message_template", Text),
    Column("max_runs", Integer),
    Column("stop_signal", Text),
    Column("delay_seconds", Integer),
    Column("timeout_per_run", Integer),
    Column("max_duration_seconds", Integer),  # #1156 — wall-clock deadline
    Column("max_cost_usd", Float),  # #1155 — per-loop USD cost budget
    Column("no_progress_threshold", Integer),  # #1157 — doom-loop detection (NULL = disabled)
    Column("model", Text),
    Column("allowed_tools", Text),
    Column("status", Text),
    Column("runs_completed", Integer),
    Column("stop_reason", Text),
    Column("last_response", Text),
    Column("error", Text),
    Column("started_by_user_id", Integer),
    Column("started_by_user_email", Text),
    Column("source_agent_name", Text),
    Column("source_mcp_key_id", Text),
    Column("source_mcp_key_name", Text),
    Column("created_at", Text),
    Column("started_at", Text),
    Column("completed_at", Text),
)

agent_loop_runs = Table(
    "agent_loop_runs",
    metadata,
    Column("id", Text, primary_key=True),
    Column("loop_id", Text),
    Column("run_number", Integer),
    Column("execution_id", Text),
    Column("status", Text),
    Column("response", Text),
    Column("error", Text),
    Column("cost", Float),
    Column("duration_ms", Integer),
    Column("started_at", Text),
    Column("completed_at", Text),
)

chat_sessions = Table(
    "chat_sessions",
    metadata,
    Column("id", Text, primary_key=True),
    Column("agent_name", Text),
    Column("user_id", Integer),
    Column("user_email", Text),
    Column("started_at", Text),
    Column("last_message_at", Text),
    Column("message_count", Integer),
    Column("total_cost", Float),
    Column("total_context_used", Integer),
    Column("total_context_max", Integer),
    Column("status", Text),
    Column("subscription_id", Text),
)

chat_messages = Table(
    "chat_messages",
    metadata,
    Column("id", Text, primary_key=True),
    Column("session_id", Text),
    Column("agent_name", Text),
    Column("user_id", Integer),
    Column("user_email", Text),
    Column("role", Text),
    Column("content", Text),
    Column("timestamp", Text),
    Column("cost", Float),
    Column("context_used", Integer),
    Column("context_max", Integer),
    Column("tool_calls", Text),
    Column("execution_time_ms", Integer),
    Column("source", Text),
    Column("subscription_id", Text),
    Column("output_tokens", Integer),
)

agent_sessions = Table(
    "agent_sessions",
    metadata,
    Column("id", Text, primary_key=True),
    Column("agent_name", Text),
    Column("user_id", Integer),
    Column("user_email", Text),
    Column("started_at", Text),
    Column("last_message_at", Text),
    Column("message_count", Integer),
    Column("total_cost", Float),
    Column("total_context_used", Integer),
    Column("total_context_max", Integer),
    Column("status", Text),
    Column("subscription_id", Text),
    Column("cached_claude_session_id", Text),
    Column("last_resume_at", Text),
    Column("consecutive_resume_failures", Integer),
    Column("compact_count", Integer),
)

agent_session_messages = Table(
    "agent_session_messages",
    metadata,
    Column("id", Text, primary_key=True),
    Column("session_id", Text),
    Column("agent_name", Text),
    Column("user_id", Integer),
    Column("user_email", Text),
    Column("role", Text),
    Column("content", Text),
    Column("timestamp", Text),
    Column("cost", Float),
    Column("context_used", Integer),
    Column("context_max", Integer),
    Column("cache_read_tokens", Integer),
    Column("tool_calls", Text),
    Column("execution_time_ms", Integer),
    Column("claude_session_id", Text),
    Column("compact_metadata", Text),
)

agent_activities = Table(
    "agent_activities",
    metadata,
    Column("id", Text, primary_key=True),
    Column("agent_name", Text),
    Column("activity_type", Text),
    Column("activity_state", Text),
    Column("parent_activity_id", Text),
    Column("started_at", Text),
    Column("completed_at", Text),
    Column("duration_ms", Integer),
    Column("user_id", Integer),
    Column("triggered_by", Text),
    Column("related_chat_message_id", Text),
    Column("related_execution_id", Text),
    Column("details", Text),
    Column("error", Text),
    Column("created_at", Text),
)

agent_reports = Table(
    "agent_reports",
    metadata,
    Column("id", Text, primary_key=True),
    Column("agent_name", Text),
    Column("user_id", Integer),
    Column("report_type", Text),
    Column("title", Text),
    Column("payload", Text),
    Column("display_hint", Text),
    Column("schema_version", Integer),
    Column("period_start", Text),
    Column("period_end", Text),
    Column("created_at", Text),
)

agent_notifications = Table(
    "agent_notifications",
    metadata,
    Column("id", Text, primary_key=True),
    Column("agent_name", Text),
    Column("notification_type", Text),
    Column("title", Text),
    Column("message", Text),
    Column("priority", Text),
    Column("category", Text),
    Column("metadata", Text),
    Column("status", Text),
    Column("created_at", Text),
    Column("acknowledged_at", Text),
    Column("acknowledged_by", Text),
)

agent_permissions = Table(
    "agent_permissions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("source_agent", Text),
    Column("target_agent", Text),
    Column("created_at", Text),
    Column("created_by", Text),
)

agent_shared_folder_config = Table(
    "agent_shared_folder_config",
    metadata,
    Column("agent_name", Text, primary_key=True),
    Column("expose_enabled", Integer),
    Column("consume_enabled", Integer),
    Column("created_at", Text),
    Column("updated_at", Text),
)

agent_shared_files = Table(
    "agent_shared_files",
    metadata,
    Column("id", Text, primary_key=True),
    Column("agent_name", Text),
    Column("filename", Text),
    Column("stored_filename", Text),
    Column("size_bytes", Integer),
    Column("mime_type", Text),
    Column("download_token", Text),
    Column("created_by", Text),
    Column("created_at", Text),
    Column("expires_at", Text),
    Column("revoked_at", Text),
    Column("one_time", Integer),
    Column("consumed_at", Text),
    Column("download_count", Integer),
    Column("last_downloaded_at", Text),
)

system_settings = Table(
    "system_settings",
    metadata,
    Column("key", Text, primary_key=True),
    Column("value", Text),
    Column("updated_at", Text),
)

agent_public_links = Table(
    "agent_public_links",
    metadata,
    Column("id", Text, primary_key=True),
    Column("agent_name", Text),
    Column("token", Text),
    Column("created_by", Text),
    Column("created_at", Text),
    Column("expires_at", Text),
    Column("enabled", Integer),
    Column("name", Text),
    Column("require_email", Integer),
    Column("type", Text),
)

public_link_verifications = Table(
    "public_link_verifications",
    metadata,
    Column("id", Text, primary_key=True),
    Column("link_id", Text),
    Column("email", Text),
    Column("code", Text),
    Column("created_at", Text),
    Column("expires_at", Text),
    Column("verified", Integer),
    Column("session_token", Text),
    Column("session_expires_at", Text),
)

public_link_usage = Table(
    "public_link_usage",
    metadata,
    Column("id", Text, primary_key=True),
    Column("link_id", Text),
    Column("email", Text),
    Column("ip_address", Text),
    Column("message_count", Integer),
    Column("created_at", Text),
    Column("last_used_at", Text),
)

public_chat_sessions = Table(
    "public_chat_sessions",
    metadata,
    Column("id", Text, primary_key=True),
    Column("link_id", Text),
    Column("session_identifier", Text),
    Column("identifier_type", Text),
    Column("created_at", Text),
    Column("last_message_at", Text),
    Column("message_count", Integer),
    Column("total_cost", Float),
)

public_chat_messages = Table(
    "public_chat_messages",
    metadata,
    Column("id", Text, primary_key=True),
    Column("session_id", Text),
    Column("role", Text),
    Column("content", Text),
    Column("timestamp", Text),
    Column("cost", Float),
)

public_user_memory = Table(
    "public_user_memory",
    metadata,
    Column("id", Text, primary_key=True),
    Column("agent_name", Text),
    Column("user_email", Text),
    Column("memory_text", Text),
    Column("message_count", Integer),
    Column("created_at", Text),
    Column("updated_at", Text),
)

agent_git_config = Table(
    "agent_git_config",
    metadata,
    Column("id", Text, primary_key=True),
    Column("agent_name", Text),
    Column("github_repo", Text),
    Column("working_branch", Text),
    Column("instance_id", Text),
    Column("source_branch", Text),
    Column("source_mode", Integer),
    Column("created_at", Text),
    Column("last_sync_at", Text),
    Column("last_commit_sha", Text),
    Column("sync_enabled", Integer),
    Column("sync_paths", Text),
    Column("github_pat_encrypted", Text),
    Column("auto_sync_enabled", Integer),
    Column("freeze_schedules_if_sync_failing", Integer),
)

agent_sync_state = Table(
    "agent_sync_state",
    metadata,
    Column("agent_name", Text, primary_key=True),
    Column("last_sync_at", Text),
    Column("last_sync_status", Text),
    Column("consecutive_failures", Integer),
    Column("last_error_summary", Text),
    Column("last_remote_sha_main", Text),
    Column("last_remote_sha_working", Text),
    Column("ahead_main", Integer),
    Column("behind_main", Integer),
    Column("ahead_working", Integer),
    Column("behind_working", Integer),
    Column("last_check_at", Text),
    Column("updated_at", Text),
)

agent_skills = Table(
    "agent_skills",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("agent_name", Text),
    Column("skill_name", Text),
    Column("assigned_by", Text),
    Column("assigned_at", Text),
)

agent_tags = Table(
    "agent_tags",
    metadata,
    Column("agent_name", Text, primary_key=True),
    Column("tag", Text, primary_key=True),
    Column("created_at", Text),
)

system_views = Table(
    "system_views",
    metadata,
    Column("id", Text, primary_key=True),
    Column("name", Text),
    Column("description", Text),
    Column("icon", Text),
    Column("color", Text),
    Column("filter_tags", Text),
    Column("owner_id", Text),
    Column("is_shared", Integer),
    Column("created_at", Text),
    Column("updated_at", Text),
)

agent_health_checks = Table(
    "agent_health_checks",
    metadata,
    Column("id", Text, primary_key=True),
    Column("agent_name", Text),
    Column("check_type", Text),
    Column("status", Text),
    Column("container_status", Text),
    Column("cpu_percent", Float),
    Column("memory_percent", Float),
    Column("memory_mb", Float),
    Column("restart_count", Integer),
    Column("oom_killed", Integer),
    Column("reachable", Integer),
    Column("latency_ms", Float),
    Column("runtime_available", Integer),
    Column("claude_available", Integer),
    Column("context_percent", Float),
    Column("active_executions", Integer),
    Column("error_rate", Float),
    Column("error_message", Text),
    Column("checked_at", Text),
    Column("created_at", Text),
)

monitoring_alert_cooldowns = Table(
    "monitoring_alert_cooldowns",
    metadata,
    Column("id", Text, primary_key=True),
    Column("agent_name", Text),
    Column("condition", Text),
    Column("last_alert_at", Text),
)

agent_dashboard_values = Table(
    "agent_dashboard_values",
    metadata,
    Column("id", Text, primary_key=True),
    Column("agent_name", Text),
    Column("widget_key", Text),
    Column("widget_label", Text),
    Column("widget_type", Text),
    Column("value_numeric", Float),
    Column("value_text", Text),
    Column("dashboard_mtime", Text),
    Column("captured_at", Text),
    Column("created_at", Text),
)

agent_dashboard_cache = Table(
    "agent_dashboard_cache",
    metadata,
    Column("agent_name", Text, primary_key=True),
    Column("config_json", Text),
    Column("last_modified", Text),
    Column("updated_at", Text),
)

slack_link_connections = Table(
    "slack_link_connections",
    metadata,
    Column("id", Text, primary_key=True),
    Column("link_id", Text),
    Column("slack_team_id", Text),
    Column("slack_team_name", Text),
    Column("slack_bot_token", Text),
    Column("connected_by", Text),
    Column("connected_at", Text),
    Column("enabled", Integer),
)

slack_user_verifications = Table(
    "slack_user_verifications",
    metadata,
    Column("id", Text, primary_key=True),
    Column("link_id", Text),
    Column("slack_user_id", Text),
    Column("slack_team_id", Text),
    Column("verified_email", Text),
    Column("verification_method", Text),
    Column("verified_at", Text),
)

slack_pending_verifications = Table(
    "slack_pending_verifications",
    metadata,
    Column("id", Text, primary_key=True),
    Column("link_id", Text),
    Column("slack_user_id", Text),
    Column("slack_team_id", Text),
    Column("email", Text),
    Column("code", Text),
    Column("created_at", Text),
    Column("expires_at", Text),
    Column("state", Text),
)

slack_workspaces = Table(
    "slack_workspaces",
    metadata,
    Column("id", Text, primary_key=True),
    Column("team_id", Text),
    Column("team_name", Text),
    Column("bot_token", Text),
    Column("connected_by", Text),
    Column("connected_at", Text),
    Column("enabled", Integer),
)

slack_channel_agents = Table(
    "slack_channel_agents",
    metadata,
    Column("id", Text, primary_key=True),
    Column("team_id", Text),
    Column("slack_channel_id", Text),
    Column("slack_channel_name", Text),
    Column("agent_name", Text),
    Column("is_dm_default", Integer),
    Column("created_by", Text),
    Column("created_at", Text),
)

slack_active_threads = Table(
    "slack_active_threads",
    metadata,
    Column("team_id", Text, primary_key=True),
    Column("channel_id", Text, primary_key=True),
    Column("thread_ts", Text, primary_key=True),
    Column("agent_name", Text),
    Column("created_at", Text),
)

telegram_bindings = Table(
    "telegram_bindings",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("agent_name", Text),
    Column("bot_token_encrypted", Text),
    Column("bot_username", Text),
    Column("bot_id", Text),
    Column("webhook_secret", Text),
    Column("webhook_url", Text),
    Column("telegram_secret_token", Text),
    Column("last_update_id", Integer),
    Column("created_at", Text),
    Column("updated_at", Text),
)

telegram_chat_links = Table(
    "telegram_chat_links",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("binding_id", Integer),
    Column("telegram_user_id", Text),
    Column("telegram_username", Text),
    Column("session_id", Text),
    Column("message_count", Integer),
    Column("created_at", Text),
    Column("last_active", Text),
    Column("verified_email", Text),
    Column("verified_at", Text),
)

telegram_group_configs = Table(
    "telegram_group_configs",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("binding_id", Integer),
    Column("chat_id", Text),
    Column("chat_title", Text),
    Column("chat_type", Text),
    Column("trigger_mode", Text),
    Column("welcome_enabled", Integer),
    Column("welcome_text", Text),
    Column("is_active", Integer),
    Column("created_at", Text),
    Column("updated_at", Text),
    Column("verified_by_email", Text),
    Column("verified_at", Text),
)

whatsapp_bindings = Table(
    "whatsapp_bindings",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("agent_name", Text),
    Column("account_sid", Text),
    Column("auth_token_encrypted", Text),
    Column("from_number", Text),
    Column("messaging_service_sid", Text),
    Column("display_name", Text),
    Column("is_sandbox", Integer),
    Column("webhook_secret", Text),
    Column("webhook_url", Text),
    Column("enabled", Integer),
    Column("created_by", Text),
    Column("created_at", Text),
    Column("updated_at", Text),
)

whatsapp_chat_links = Table(
    "whatsapp_chat_links",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("binding_id", Integer),
    Column("wa_user_phone", Text),
    Column("wa_user_name", Text),
    Column("session_id", Text),
    Column("verified_email", Text),
    Column("verified_at", Text),
    Column("message_count", Integer),
    Column("last_active", Text),
    Column("created_at", Text),
)

voip_bindings = Table(
    "voip_bindings",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("agent_name", Text),
    Column("account_sid", Text),
    Column("auth_token_encrypted", Text),
    Column("from_number", Text),
    Column("inbound_number", Text),
    Column("webhook_secret", Text),
    Column("webhook_url", Text),
    Column("daily_call_cap", Integer),
    Column("display_name", Text),
    Column("enabled", Integer),
    Column("created_by", Text),
    Column("created_at", Text),
    Column("updated_at", Text),
)

voip_call_logs = Table(
    "voip_call_logs",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("call_id", Text),
    Column("agent_name", Text),
    Column("chat_session_id", Text),
    Column("to_number", Text),
    Column("direction", Text),
    Column("status", Text),
    Column("twilio_call_sid", Text),
    Column("initiated_by_user_id", Integer),
    Column("initiated_by_email", Text),
    Column("error", Text),
    Column("started_at", Text),
    Column("connected_at", Text),
    Column("ended_at", Text),
    Column("duration_ms", Integer),
)

subscription_rate_limit_events = Table(
    "subscription_rate_limit_events",
    metadata,
    Column("id", Text, primary_key=True),
    Column("agent_name", Text),
    Column("subscription_id", Text),
    Column("error_message", Text),
    Column("occurred_at", Text),
)

operator_queue = Table(
    "operator_queue",
    metadata,
    Column("id", Text, primary_key=True),
    Column("agent_name", Text),
    Column("type", Text),
    Column("status", Text),
    Column("priority", Text),
    Column("title", Text),
    Column("question", Text),
    Column("options", Text),
    Column("context", Text),
    Column("execution_id", Text),
    Column("created_at", Text),
    Column("expires_at", Text),
    Column("response", Text),
    Column("response_text", Text),
    Column("responded_by_id", Text),
    Column("responded_by_email", Text),
    Column("responded_at", Text),
    Column("acknowledged_at", Text),
    Column("cleared_at", Text),  # #1017 — Clear All hide flag
)

nevermined_agent_config = Table(
    "nevermined_agent_config",
    metadata,
    Column("id", Text, primary_key=True),
    Column("agent_name", Text),
    Column("encrypted_credentials", Text),
    Column("nvm_environment", Text),
    Column("nvm_agent_id", Text),
    Column("nvm_plan_id", Text),
    Column("credits_per_request", Integer),
    Column("enabled", Integer),
    Column("created_at", Text),
    Column("updated_at", Text),
)

nevermined_payment_log = Table(
    "nevermined_payment_log",
    metadata,
    Column("id", Text, primary_key=True),
    Column("agent_name", Text),
    Column("execution_id", Text),
    Column("action", Text),
    Column("subscriber_address", Text),
    Column("credits_amount", Integer),
    Column("tx_hash", Text),
    Column("remaining_balance", Integer),
    Column("success", Integer),
    Column("error", Text),
    Column("created_at", Text),
)

agent_event_subscriptions = Table(
    "agent_event_subscriptions",
    metadata,
    Column("id", Text, primary_key=True),
    Column("subscriber_agent", Text),
    Column("source_agent", Text),
    Column("event_type", Text),
    Column("target_message", Text),
    Column("enabled", Integer),
    Column("created_at", Text),
    Column("updated_at", Text),
    Column("created_by", Text),
)

agent_events = Table(
    "agent_events",
    metadata,
    Column("id", Text, primary_key=True),
    Column("source_agent", Text),
    Column("event_type", Text),
    Column("payload", Text),
    Column("subscriptions_triggered", Integer),
    Column("created_at", Text),
)

access_requests = Table(
    "access_requests",
    metadata,
    Column("id", Text, primary_key=True),
    Column("agent_name", Text),
    Column("email", Text),
    Column("channel", Text),
    Column("requested_at", Text),
    Column("status", Text),
    Column("decided_by", Integer),
    Column("decided_at", Text),
)

audit_log = Table(
    "audit_log",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("event_id", Text),
    Column("event_type", Text),
    Column("event_action", Text),
    Column("actor_type", Text),
    Column("actor_id", Text),
    Column("actor_email", Text),
    Column("actor_ip", Text),
    Column("mcp_key_id", Text),
    Column("mcp_key_name", Text),
    Column("mcp_scope", Text),
    Column("target_type", Text),
    Column("target_id", Text),
    Column("timestamp", Text),
    Column("details", Text),
    Column("request_id", Text),
    Column("source", Text),
    Column("endpoint", Text),
    Column("previous_hash", Text),
    Column("entry_hash", Text),
    Column("created_at", Text),
)

canary_violations = Table(
    "canary_violations",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("invariant_id", Text),
    Column("tier", Text),
    Column("severity", Text),
    Column("snapshot_time", Text),
    Column("observed_state", Text),
    Column("signal_query", Text),
    Column("created_at", Text),
)

idempotency_keys = Table(
    "idempotency_keys",
    metadata,
    Column("scope", Text, primary_key=True),
    Column("idempotency_key", Text, primary_key=True),
    Column("execution_id", Text),
    Column("status", Text),
    Column("response_snapshot", Text),
    Column("created_at", Text),
    Column("updated_at", Text),
)

# Agent compatibility results (#668) — latest snapshot per agent (upserted).
agent_compatibility_results = Table(
    "agent_compatibility_results",
    metadata,
    Column("agent_name", Text, primary_key=True),
    Column("overall_status", Text),
    Column("checks_json", Text),
    Column("hard_count", Integer),
    Column("soft_count", Integer),
    Column("info_count", Integer),
    Column("container_running", Integer),
    Column("ai_ran_at", Text),
    Column("static_ran_at", Text),
    Column("updated_at", Text),
)
