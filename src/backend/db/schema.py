"""
Database schema definitions for Trinity platform.

Contains all CREATE TABLE and CREATE INDEX statements.
Schema creation is idempotent via IF NOT EXISTS.

Tables are organized by feature area:
- Core: users, agent_ownership, agent_sharing
- Auth: mcp_api_keys, email_whitelist, email_login_codes
- Schedules: agent_schedules, schedule_executions
- Chat: chat_sessions, chat_messages
- Activities: agent_activities
- Permissions: agent_permissions
- Shared Folders: agent_shared_folder_config
- Shared Files (outbound): agent_shared_files
- Settings: system_settings
- Public Links: agent_public_links, public_link_verifications, public_link_usage
- Public Chat: public_chat_sessions, public_chat_messages, public_user_memory
- Git: agent_git_config
- Skills: agent_skills
- Tags: agent_tags
- System Views: system_views
- Subscriptions: subscription_credentials
- Dashboard History: agent_dashboard_values
"""

# =============================================================================
# Table Definitions
# =============================================================================

TABLES = {
    # -------------------------------------------------------------------------
    # Core Tables
    # -------------------------------------------------------------------------
    "users": """
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
            last_login TEXT,
            suspended_at TEXT
        )
    """,

    # SUB-001: Subscription credentials. Declared before agent_ownership so the
    # subscription_id foreign key target exists if FKs are ever turned on.
    "subscription_credentials": """
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
    """,

    "agent_ownership": """
        CREATE TABLE IF NOT EXISTS agent_ownership (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name TEXT UNIQUE NOT NULL,
            owner_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            is_system INTEGER DEFAULT 0,
            use_platform_api_key INTEGER DEFAULT 1,
            autonomy_enabled INTEGER DEFAULT 0,
            memory_limit TEXT,
            cpu_limit TEXT,
            full_capabilities INTEGER DEFAULT 0,
            read_only_mode INTEGER DEFAULT 0,
            read_only_config TEXT,
            subscription_id TEXT,
            max_parallel_tasks INTEGER DEFAULT 3,
            execution_timeout_seconds INTEGER DEFAULT 3600,
            avatar_identity_prompt TEXT,
            avatar_updated_at TEXT,
            is_default_avatar INTEGER DEFAULT 0,
            require_email INTEGER DEFAULT 0,
            open_access INTEGER DEFAULT 0,
            max_backlog_depth INTEGER DEFAULT 50,
            group_auth_mode TEXT DEFAULT 'none',
            voice_system_prompt TEXT,
            voice_name TEXT,
            public_channel_model TEXT,
            public_channel_system_prompt TEXT,
            guardrails_config TEXT,
            file_sharing_enabled INTEGER DEFAULT 0,
            circuit_breaker_enabled INTEGER DEFAULT 0,
            mcp_exposed INTEGER DEFAULT 0,
            tts_voice_replies_enabled INTEGER DEFAULT 0,
            tts_voice_id TEXT,
            deleted_at TEXT,
            FOREIGN KEY (owner_id) REFERENCES users(id),
            FOREIGN KEY (subscription_id) REFERENCES subscription_credentials(id)
        )
    """,

    "agent_sharing": """
        CREATE TABLE IF NOT EXISTS agent_sharing (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name TEXT NOT NULL,
            shared_with_email TEXT NOT NULL,
            shared_by_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            allow_proactive INTEGER DEFAULT 0,
            FOREIGN KEY (shared_by_id) REFERENCES users(id),
            UNIQUE(agent_name, shared_with_email)
        )
    """,

    # -------------------------------------------------------------------------
    # Auth Tables
    # -------------------------------------------------------------------------
    "mcp_api_keys": """
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
    """,

    "email_whitelist": """
        CREATE TABLE IF NOT EXISTS email_whitelist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            added_by TEXT NOT NULL,
            added_at TEXT NOT NULL,
            source TEXT NOT NULL,
            default_role TEXT NOT NULL DEFAULT 'user',
            FOREIGN KEY (added_by) REFERENCES users(id)
        )
    """,

    "email_login_codes": """
        CREATE TABLE IF NOT EXISTS email_login_codes (
            id TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            code TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            verified INTEGER DEFAULT 0,
            used_at TEXT
        )
    """,

    # -------------------------------------------------------------------------
    # Schedule Tables
    # -------------------------------------------------------------------------
    "agent_schedules": """
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
            -- #913: NULL ⇒ inherit from agent_ownership.execution_timeout_seconds.
            -- Dropped the DEFAULT 3600 that masked per-agent timeout for cron.
            timeout_seconds INTEGER,
            allowed_tools TEXT,
            model TEXT,
            max_retries INTEGER DEFAULT 0,
            retry_delay_seconds INTEGER DEFAULT 60,
            validation_enabled INTEGER DEFAULT 0,
            validation_prompt TEXT,
            validation_timeout_seconds INTEGER DEFAULT 120,
            webhook_token TEXT,
            webhook_enabled INTEGER DEFAULT 0,
            deleted_at TEXT,
            FOREIGN KEY (owner_id) REFERENCES users(id)
        )
    """,

    "schedule_executions": """
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
            execution_log TEXT,
            model_used TEXT,
            subscription_id TEXT,
            attempt_number INTEGER DEFAULT 1,
            retry_of_execution_id TEXT,
            retry_scheduled_at TEXT,
            business_status TEXT,
            validated_at TEXT,
            validation_execution_id TEXT,
            validates_execution_id TEXT,
            compact_metadata TEXT,
            source_user_id INTEGER,
            source_user_email TEXT,
            source_agent_name TEXT,
            source_mcp_key_id TEXT,
            source_mcp_key_name TEXT,
            claude_session_id TEXT,
            queued_at TEXT,
            backlog_metadata TEXT,
            fan_out_id TEXT,
            retry_count INTEGER DEFAULT 0,
            loop_id TEXT,
            FOREIGN KEY (schedule_id) REFERENCES agent_schedules(id)
        )
    """,

    # -------------------------------------------------------------------------
    # Agent Loops (#740) — sequential bounded task execution
    # -------------------------------------------------------------------------
    "agent_loops": """
        CREATE TABLE IF NOT EXISTS agent_loops (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            message_template TEXT NOT NULL,
            max_runs INTEGER NOT NULL,
            stop_signal TEXT,
            delay_seconds INTEGER NOT NULL DEFAULT 0,
            timeout_per_run INTEGER,
            max_duration_seconds INTEGER,
            max_cost_usd REAL,
            no_progress_threshold INTEGER,
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
    """,

    "agent_loop_runs": """
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
    """,

    # -------------------------------------------------------------------------
    # Chat Tables
    # -------------------------------------------------------------------------
    "chat_sessions": """
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
            subscription_id TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """,

    "chat_messages": """
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
            source TEXT DEFAULT 'text',
            subscription_id TEXT,
            output_tokens INTEGER,
            FOREIGN KEY (session_id) REFERENCES chat_sessions(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """,

    # -------------------------------------------------------------------------
    # Session Tables (Session tab — --resume-default chat surface)
    #
    # Parallel to chat_sessions / chat_messages but with cached_claude_session_id
    # to drive per-session ``claude --resume`` and per-message tracking of
    # cache_read_tokens + the actual Claude UUID a turn ran under. See
    # docs/planning/SESSION_TAB_2026-04.md.
    # -------------------------------------------------------------------------
    "agent_sessions": """
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
            compact_count INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """,

    "agent_session_messages": """
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
            compact_metadata TEXT,
            FOREIGN KEY (session_id) REFERENCES agent_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """,

    # -------------------------------------------------------------------------
    # Activity Tables
    # -------------------------------------------------------------------------
    "agent_activities": """
        CREATE TABLE IF NOT EXISTS agent_activities (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            activity_type TEXT NOT NULL,
            activity_state TEXT NOT NULL,  -- started, completed, failed, cancelled (#1332)
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
    """,

    # -------------------------------------------------------------------------
    # Agent Reports (#918) — agent-published structured telemetry/domain reports
    # -------------------------------------------------------------------------
    "agent_reports": """
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
    """,

    # -------------------------------------------------------------------------
    # Notifications (NOTIF-001)
    # -------------------------------------------------------------------------
    "agent_notifications": """
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
    """,

    # -------------------------------------------------------------------------
    # Permission Tables
    # -------------------------------------------------------------------------
    "agent_permissions": """
        CREATE TABLE IF NOT EXISTS agent_permissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_agent TEXT NOT NULL,
            target_agent TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            created_by TEXT NOT NULL,
            UNIQUE(source_agent, target_agent)
        )
    """,

    # -------------------------------------------------------------------------
    # Shared Folder Tables
    # -------------------------------------------------------------------------
    "agent_shared_folder_config": """
        CREATE TABLE IF NOT EXISTS agent_shared_folder_config (
            agent_name TEXT PRIMARY KEY,
            expose_enabled INTEGER DEFAULT 0,
            consume_enabled INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """,

    # -------------------------------------------------------------------------
    # Shared Files (outbound agent-to-user file sharing via public URL)
    # -------------------------------------------------------------------------
    "agent_shared_files": """
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
    """,

    # -------------------------------------------------------------------------
    # Settings Tables
    # -------------------------------------------------------------------------
    "system_settings": """
        CREATE TABLE IF NOT EXISTS system_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """,

    # -------------------------------------------------------------------------
    # Public Links Tables
    # -------------------------------------------------------------------------
    "agent_public_links": """
        CREATE TABLE IF NOT EXISTS agent_public_links (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            token TEXT UNIQUE NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            enabled INTEGER DEFAULT 1,
            name TEXT,
            require_email INTEGER DEFAULT 0,
            type TEXT NOT NULL DEFAULT 'chat',
            FOREIGN KEY (agent_name) REFERENCES agent_ownership(agent_name) ON DELETE CASCADE,
            FOREIGN KEY (created_by) REFERENCES users(id)
        )
    """,

    "public_link_verifications": """
        CREATE TABLE IF NOT EXISTS public_link_verifications (
            id TEXT PRIMARY KEY,
            link_id TEXT NOT NULL,
            email TEXT NOT NULL,
            code TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            verified INTEGER DEFAULT 0,
            session_token TEXT,
            session_expires_at TEXT,
            FOREIGN KEY (link_id) REFERENCES agent_public_links(id) ON DELETE CASCADE
        )
    """,

    "public_link_usage": """
        CREATE TABLE IF NOT EXISTS public_link_usage (
            id TEXT PRIMARY KEY,
            link_id TEXT NOT NULL,
            email TEXT,
            ip_address TEXT,
            message_count INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            last_used_at TEXT,
            FOREIGN KEY (link_id) REFERENCES agent_public_links(id) ON DELETE CASCADE
        )
    """,

    # -------------------------------------------------------------------------
    # Public Chat Tables
    # -------------------------------------------------------------------------
    "public_chat_sessions": """
        CREATE TABLE IF NOT EXISTS public_chat_sessions (
            id TEXT PRIMARY KEY,
            link_id TEXT NOT NULL,
            session_identifier TEXT NOT NULL,
            identifier_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_message_at TEXT NOT NULL,
            message_count INTEGER DEFAULT 0,
            total_cost REAL DEFAULT 0.0,
            FOREIGN KEY (link_id) REFERENCES agent_public_links(id) ON DELETE CASCADE,
            UNIQUE(link_id, session_identifier)
        )
    """,

    "public_chat_messages": """
        CREATE TABLE IF NOT EXISTS public_chat_messages (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            cost REAL,
            FOREIGN KEY (session_id) REFERENCES public_chat_sessions(id) ON DELETE CASCADE
        )
    """,

    "public_user_memory": """
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
    """,

    # -------------------------------------------------------------------------
    # Git Tables
    # -------------------------------------------------------------------------
    "agent_git_config": """
        CREATE TABLE IF NOT EXISTS agent_git_config (
            id TEXT PRIMARY KEY,
            agent_name TEXT UNIQUE NOT NULL,
            github_repo TEXT NOT NULL,
            working_branch TEXT NOT NULL,
            instance_id TEXT NOT NULL,
            source_branch TEXT DEFAULT 'main',
            source_mode INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            last_sync_at TEXT,
            last_commit_sha TEXT,
            sync_enabled INTEGER DEFAULT 1,
            sync_paths TEXT,
            github_pat_encrypted TEXT,
            auto_sync_enabled INTEGER DEFAULT 0,
            freeze_schedules_if_sync_failing INTEGER DEFAULT 0,
            FOREIGN KEY (agent_name) REFERENCES agent_ownership(agent_name)
        )
    """,

    # Sync health observability (#389 S1). One row per agent, tracks last
    # sync outcome + consecutive_failures so the dashboard can render a
    # health dot and the backend can emit operator-queue alerts.
    "agent_sync_state": """
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
    """,

    # -------------------------------------------------------------------------
    # Skills Tables
    # -------------------------------------------------------------------------
    "agent_skills": """
        CREATE TABLE IF NOT EXISTS agent_skills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name TEXT NOT NULL,
            skill_name TEXT NOT NULL,
            assigned_by TEXT NOT NULL,
            assigned_at TEXT NOT NULL,
            UNIQUE(agent_name, skill_name)
        )
    """,

    # -------------------------------------------------------------------------
    # Tags Tables
    # -------------------------------------------------------------------------
    "agent_tags": """
        CREATE TABLE IF NOT EXISTS agent_tags (
            agent_name TEXT NOT NULL,
            tag TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (agent_name, tag),
            FOREIGN KEY (agent_name) REFERENCES agent_ownership(agent_name) ON DELETE CASCADE
        )
    """,

    # -------------------------------------------------------------------------
    # System Views Tables
    # -------------------------------------------------------------------------
    "system_views": """
        CREATE TABLE IF NOT EXISTS system_views (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            icon TEXT,
            color TEXT,
            filter_tags TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            is_shared INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (owner_id) REFERENCES users(id)
        )
    """,

    # -------------------------------------------------------------------------
    # Monitoring Tables (MON-001)
    # -------------------------------------------------------------------------
    "agent_health_checks": """
        CREATE TABLE IF NOT EXISTS agent_health_checks (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            check_type TEXT NOT NULL,
            status TEXT NOT NULL,
            container_status TEXT,
            cpu_percent REAL,
            memory_percent REAL,
            memory_mb REAL,
            restart_count INTEGER,
            oom_killed INTEGER,
            reachable INTEGER,
            latency_ms REAL,
            runtime_available INTEGER,
            claude_available INTEGER,
            context_percent REAL,
            active_executions INTEGER,
            error_rate REAL,
            error_message TEXT,
            checked_at TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """,

    "monitoring_alert_cooldowns": """
        CREATE TABLE IF NOT EXISTS monitoring_alert_cooldowns (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            condition TEXT NOT NULL,
            last_alert_at TEXT NOT NULL,
            UNIQUE(agent_name, condition)
        )
    """,

    # -------------------------------------------------------------------------
    # Dashboard History Tables (DASH-001)
    # -------------------------------------------------------------------------
    "agent_dashboard_values": """
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
    """,

    # Dashboard cache: survives backend restarts (DASH-001)
    "agent_dashboard_cache": """
        CREATE TABLE IF NOT EXISTS agent_dashboard_cache (
            agent_name TEXT PRIMARY KEY,
            config_json TEXT NOT NULL,
            last_modified TEXT,
            updated_at TEXT NOT NULL
        )
    """,

    # -------------------------------------------------------------------------
    # Slack Integration Tables (SLACK-001)
    # -------------------------------------------------------------------------
    "slack_link_connections": """
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
    """,

    "slack_user_verifications": """
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
    """,

    "slack_pending_verifications": """
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
    """,

    # -------------------------------------------------------------------------
    # Multi-Agent Slack Tables (SLACK-002)
    # -------------------------------------------------------------------------
    "slack_workspaces": """
        CREATE TABLE IF NOT EXISTS slack_workspaces (
            id TEXT PRIMARY KEY,
            team_id TEXT NOT NULL UNIQUE,
            team_name TEXT,
            bot_token TEXT NOT NULL,
            connected_by TEXT,
            connected_at TEXT NOT NULL,
            enabled INTEGER DEFAULT 1
        )
    """,

    "slack_channel_agents": """
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
    """,

    "slack_active_threads": """
        CREATE TABLE IF NOT EXISTS slack_active_threads (
            team_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            thread_ts TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (team_id, channel_id, thread_ts)
        )
    """,

    # -------------------------------------------------------------------------
    # Telegram Integration Tables (TELEGRAM-001 / TGRAM-GROUP)
    # -------------------------------------------------------------------------
    "telegram_bindings": """
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
    """,

    # verified_email/verified_at columns rolled in from _migrate_access_control (#311)
    "telegram_chat_links": """
        CREATE TABLE IF NOT EXISTS telegram_chat_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            binding_id INTEGER NOT NULL REFERENCES telegram_bindings(id),
            telegram_user_id TEXT NOT NULL,
            telegram_username TEXT,
            session_id TEXT,
            message_count INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            last_active TEXT,
            verified_email TEXT,
            verified_at TEXT,
            UNIQUE(binding_id, telegram_user_id)
        )
    """,

    # verified_by_email/verified_at columns rolled in from _migrate_group_auth_mode
    "telegram_group_configs": """
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
            updated_at TEXT,
            verified_by_email TEXT,
            verified_at TEXT
        )
    """,

    # -------------------------------------------------------------------------
    # WhatsApp Integration Tables (WHATSAPP-001 — Twilio)
    # -------------------------------------------------------------------------
    "whatsapp_bindings": """
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
    """,

    "whatsapp_chat_links": """
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
    """,

    # -------------------------------------------------------------------------
    # VoIP Telephony Tables (VOIP-001 — Twilio Programmable Voice, #1056)
    # -------------------------------------------------------------------------
    "voip_bindings": """
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
    """,

    "voip_call_logs": """
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
    """,

    # -------------------------------------------------------------------------
    # Subscription Rate Limit Tracking (SUB-003)
    # -------------------------------------------------------------------------
    "subscription_rate_limit_events": """
        CREATE TABLE IF NOT EXISTS subscription_rate_limit_events (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            subscription_id TEXT NOT NULL,
            error_message TEXT,
            occurred_at TEXT NOT NULL,
            FOREIGN KEY (subscription_id) REFERENCES subscription_credentials(id) ON DELETE CASCADE
        )
    """,

    # -------------------------------------------------------------------------
    # Operator Queue Tables (OPS-001)
    # -------------------------------------------------------------------------
    "operator_queue": """
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
            cleared_at TEXT,
            FOREIGN KEY (responded_by_id) REFERENCES users(id)
        )
    """,

    # Nevermined Payment Integration (NVM-001)
    "nevermined_agent_config": """
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
    """,

    "nevermined_payment_log": """
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
    """,

    # -------------------------------------------------------------------------
    # Agent Event Subscription Tables (EVT-001)
    # -------------------------------------------------------------------------
    "agent_event_subscriptions": """
        CREATE TABLE IF NOT EXISTS agent_event_subscriptions (
            id TEXT PRIMARY KEY,
            subscriber_agent TEXT NOT NULL,
            source_agent TEXT NOT NULL,
            event_type TEXT NOT NULL,
            target_message TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            created_by TEXT NOT NULL,
            UNIQUE(subscriber_agent, source_agent, event_type)
        )
    """,

    "agent_events": """
        CREATE TABLE IF NOT EXISTS agent_events (
            id TEXT PRIMARY KEY,
            source_agent TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload TEXT,
            subscriptions_triggered INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """,

    # -------------------------------------------------------------------------
    # Access Requests (Issue #311 - Unified Channel Access Control)
    # -------------------------------------------------------------------------
    "access_requests": """
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
    """,

    # -------------------------------------------------------------------------
    # Platform Audit Log (SEC-001 / Issue #20) — Phase 1
    # -------------------------------------------------------------------------
    # Cross-cutting append-only audit trail for agent lifecycle, auth, MCP,
    # credentials, sharing, settings, git, and system events. Distinct from
    # the Process Engine's `audit_entries` table which is workflow-specific.
    "audit_log": """
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
    """,

    # -------------------------------------------------------------------------
    # Canary Invariant Harness (CANARY-001 / Issue #411 — Phase 1)
    # -------------------------------------------------------------------------
    # Continuous orchestration-invariant violations recorded by the canary
    # watcher service (`services/canary_service.py`). Each row is one fired
    # check; the row stores the invariant id, tier, severity, snapshot
    # timestamp, and a JSON `observed_state` payload specific to the
    # invariant. The service writes here every cycle and posts to a Slack
    # webhook (`CANARY_SLACK_WEBHOOK_URL`) on green→red transitions.
    "canary_violations": """
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
    """,
    # -------------------------------------------------------------------------
    # Idempotency (RELIABILITY-006, #525)
    # -------------------------------------------------------------------------
    # One row per claimed (scope, key). PRIMARY KEY gives the atomic-claim
    # uniqueness; rows past the 24h TTL are purged by the cleanup service.
    "idempotency_keys": """
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
    """,
    # -------------------------------------------------------------------------
    # Agent compatibility results (#668)
    # -------------------------------------------------------------------------
    # Latest-snapshot-per-agent compatibility report (one row, upserted by
    # agent_name). STATIC checks are recomputed live on each read; the persisted
    # AI verdicts are merged in so AI findings show on every Overview load
    # without re-spending tokens. `checks_json` is the full last report's check
    # list. NOTE: this table is a deliberate departure from the issue's original
    # "no DB table" note — see services/compatibility/.
    "agent_compatibility_results": """
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
    """,
}

# =============================================================================
# Index Definitions
# =============================================================================

INDEXES = [
    # Core indexes
    "CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)",
    "CREATE INDEX IF NOT EXISTS idx_users_auth0_sub ON users(auth0_sub)",
    "CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)",
    "CREATE INDEX IF NOT EXISTS idx_agent_ownership_owner ON agent_ownership(owner_id)",
    "CREATE INDEX IF NOT EXISTS idx_agent_ownership_name ON agent_ownership(agent_name)",
    # Issue #834: partial index for the retention sweep — narrow scan to
    # rows that are actually soft-deleted, not the whole agent table.
    "CREATE INDEX IF NOT EXISTS idx_agent_ownership_deleted_at "
    "ON agent_ownership(deleted_at) WHERE deleted_at IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_agent_sharing_agent ON agent_sharing(agent_name)",
    "CREATE INDEX IF NOT EXISTS idx_agent_sharing_email ON agent_sharing(shared_with_email)",

    # MCP keys indexes
    "CREATE INDEX IF NOT EXISTS idx_mcp_keys_user ON mcp_api_keys(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_mcp_keys_hash ON mcp_api_keys(key_hash)",
    "CREATE INDEX IF NOT EXISTS idx_mcp_keys_agent ON mcp_api_keys(agent_name)",

    # Schedule indexes
    "CREATE INDEX IF NOT EXISTS idx_schedules_agent ON agent_schedules(agent_name)",
    # Issue #834 Phase 1b: partial index for retention sweep over
    # soft-deleted schedules.
    "CREATE INDEX IF NOT EXISTS idx_agent_schedules_deleted_at "
    "ON agent_schedules(deleted_at) WHERE deleted_at IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_schedules_owner ON agent_schedules(owner_id)",
    "CREATE INDEX IF NOT EXISTS idx_schedules_enabled ON agent_schedules(enabled)",

    # Execution indexes
    "CREATE INDEX IF NOT EXISTS idx_executions_schedule ON schedule_executions(schedule_id)",
    "CREATE INDEX IF NOT EXISTS idx_executions_agent ON schedule_executions(agent_name)",
    "CREATE INDEX IF NOT EXISTS idx_executions_status ON schedule_executions(status)",
    # PERF-001: Composite index for Tasks list queries
    "CREATE INDEX IF NOT EXISTS idx_executions_agent_started ON schedule_executions(agent_name, started_at DESC)",
    # VALIDATE-001: Business status for validation results
    "CREATE INDEX IF NOT EXISTS idx_executions_business_status ON schedule_executions(business_status)",
    "CREATE INDEX IF NOT EXISTS idx_executions_validates ON schedule_executions(validates_execution_id)",
    # Issue #772: partial index drives execution_log null + row delete retention sweeps.
    # Status values match TaskExecutionStatus terminal set (fix: #862).
    "CREATE INDEX IF NOT EXISTS idx_executions_completed_terminal "
    "ON schedule_executions(completed_at) "
    "WHERE status IN ('success', 'failed', 'cancelled', 'skipped')",

    # Git config indexes
    "CREATE INDEX IF NOT EXISTS idx_git_config_agent ON agent_git_config(agent_name)",
    "CREATE INDEX IF NOT EXISTS idx_git_config_repo ON agent_git_config(github_repo)",
    # S7 Layer 2: a working branch may only be bound to one agent within a
    # given repo. Source-mode agents intentionally share a branch (e.g. every
    # reader points at `main`) so the index is partial and excludes them.
    # See src/backend/db/migrations.py::_migrate_agent_git_config_branch_ownership
    # for the operator-assisted migration path on existing databases.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_git_config_repo_branch_unique "
    "ON agent_git_config(github_repo, working_branch) WHERE source_mode = 0",

    # Sync health state (#389 S1)
    "CREATE INDEX IF NOT EXISTS idx_sync_state_status "
    "ON agent_sync_state(last_sync_status, consecutive_failures)",

    # Chat session indexes
    "CREATE INDEX IF NOT EXISTS idx_chat_sessions_agent ON chat_sessions(agent_name)",
    "CREATE INDEX IF NOT EXISTS idx_chat_sessions_user ON chat_sessions(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_chat_sessions_status ON chat_sessions(status)",

    # Chat message indexes
    "CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_chat_messages_agent ON chat_messages(agent_name)",
    "CREATE INDEX IF NOT EXISTS idx_chat_messages_user ON chat_messages(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_chat_messages_timestamp ON chat_messages(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_chat_messages_subscription ON chat_messages(subscription_id, timestamp)",

    # Agent session indexes (Session tab)
    "CREATE INDEX IF NOT EXISTS idx_agent_sessions_agent_user ON agent_sessions(agent_name, user_id)",
    "CREATE INDEX IF NOT EXISTS idx_agent_sessions_status ON agent_sessions(status)",
    "CREATE INDEX IF NOT EXISTS idx_agent_session_messages_session ON agent_session_messages(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_agent_session_messages_user ON agent_session_messages(user_id)",

    # Execution subscription index (SUB-004)
    "CREATE INDEX IF NOT EXISTS idx_executions_subscription ON schedule_executions(subscription_id, started_at)",

    # Activity indexes
    "CREATE INDEX IF NOT EXISTS idx_activities_agent ON agent_activities(agent_name, created_at DESC)",
    # #1265: fleet-wide timeline sorts by created_at DESC with no agent_name predicate;
    # the composite idx_activities_agent can't serve that sort, so it degraded to scan+sort.
    "CREATE INDEX IF NOT EXISTS idx_activities_created ON agent_activities(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_activities_type ON agent_activities(activity_type)",
    "CREATE INDEX IF NOT EXISTS idx_activities_state ON agent_activities(activity_state)",
    "CREATE INDEX IF NOT EXISTS idx_activities_user ON agent_activities(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_activities_parent ON agent_activities(parent_activity_id)",
    "CREATE INDEX IF NOT EXISTS idx_activities_chat_msg ON agent_activities(related_chat_message_id)",
    "CREATE INDEX IF NOT EXISTS idx_activities_execution ON agent_activities(related_execution_id)",

    # Agent report indexes (#918)
    "CREATE INDEX IF NOT EXISTS idx_agent_reports_agent ON agent_reports(agent_name, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_agent_reports_type ON agent_reports(report_type, created_at DESC)",
    # Serves the retention sweep's `WHERE created_at < cutoff` scan (#918).
    "CREATE INDEX IF NOT EXISTS idx_agent_reports_created ON agent_reports(created_at)",

    # Permission indexes
    "CREATE INDEX IF NOT EXISTS idx_permissions_source ON agent_permissions(source_agent)",
    "CREATE INDEX IF NOT EXISTS idx_permissions_target ON agent_permissions(target_agent)",

    # Shared folder indexes
    "CREATE INDEX IF NOT EXISTS idx_shared_folders_expose ON agent_shared_folder_config(expose_enabled)",
    "CREATE INDEX IF NOT EXISTS idx_shared_folders_consume ON agent_shared_folder_config(consume_enabled)",

    # Shared files (outbound) indexes
    "CREATE INDEX IF NOT EXISTS idx_agent_files_agent ON agent_shared_files(agent_name)",
    "CREATE INDEX IF NOT EXISTS idx_agent_files_token ON agent_shared_files(download_token)",
    "CREATE INDEX IF NOT EXISTS idx_agent_files_expires ON agent_shared_files(expires_at) WHERE revoked_at IS NULL",

    # Public links indexes
    "CREATE INDEX IF NOT EXISTS idx_public_links_token ON agent_public_links(token)",
    "CREATE INDEX IF NOT EXISTS idx_public_links_agent ON agent_public_links(agent_name)",
    "CREATE INDEX IF NOT EXISTS idx_verifications_link ON public_link_verifications(link_id)",
    "CREATE INDEX IF NOT EXISTS idx_verifications_email ON public_link_verifications(email)",
    "CREATE INDEX IF NOT EXISTS idx_verifications_code ON public_link_verifications(code)",
    "CREATE INDEX IF NOT EXISTS idx_usage_link ON public_link_usage(link_id)",
    "CREATE INDEX IF NOT EXISTS idx_usage_ip ON public_link_usage(ip_address)",

    # Email auth indexes
    "CREATE INDEX IF NOT EXISTS idx_email_whitelist_email ON email_whitelist(email)",
    "CREATE INDEX IF NOT EXISTS idx_email_login_codes_email ON email_login_codes(email)",
    "CREATE INDEX IF NOT EXISTS idx_email_login_codes_code ON email_login_codes(code)",

    # Agent skills indexes
    "CREATE INDEX IF NOT EXISTS idx_agent_skills_agent ON agent_skills(agent_name)",
    "CREATE INDEX IF NOT EXISTS idx_agent_skills_skill ON agent_skills(skill_name)",

    # Agent tags indexes
    "CREATE INDEX IF NOT EXISTS idx_agent_tags_tag ON agent_tags(tag)",
    "CREATE INDEX IF NOT EXISTS idx_agent_tags_agent ON agent_tags(agent_name)",

    # Public chat indexes
    "CREATE INDEX IF NOT EXISTS idx_public_chat_sessions_link ON public_chat_sessions(link_id)",
    "CREATE INDEX IF NOT EXISTS idx_public_chat_sessions_identifier ON public_chat_sessions(session_identifier)",
    "CREATE INDEX IF NOT EXISTS idx_public_chat_messages_session ON public_chat_messages(session_id)",

    # Public user memory indexes (MEM-001)
    "CREATE INDEX IF NOT EXISTS idx_public_user_memory_lookup ON public_user_memory(agent_name, user_email)",

    # System views indexes
    "CREATE INDEX IF NOT EXISTS idx_system_views_owner ON system_views(owner_id)",

    # Monitoring indexes (MON-001)
    "CREATE INDEX IF NOT EXISTS idx_health_agent_time ON agent_health_checks(agent_name, checked_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_health_status ON agent_health_checks(status)",
    "CREATE INDEX IF NOT EXISTS idx_health_type ON agent_health_checks(check_type)",
    "CREATE INDEX IF NOT EXISTS idx_health_checked_at ON agent_health_checks(checked_at)",
    "CREATE INDEX IF NOT EXISTS idx_alert_cooldowns_agent ON monitoring_alert_cooldowns(agent_name)",

    # Dashboard history indexes (DASH-001)
    "CREATE INDEX IF NOT EXISTS idx_dashboard_values_agent_time ON agent_dashboard_values(agent_name, captured_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_dashboard_values_widget ON agent_dashboard_values(agent_name, widget_key, captured_at DESC)",

    # Slack integration indexes (SLACK-001)
    "CREATE INDEX IF NOT EXISTS idx_slack_connections_team ON slack_link_connections(slack_team_id)",
    "CREATE INDEX IF NOT EXISTS idx_slack_connections_link ON slack_link_connections(link_id)",
    "CREATE INDEX IF NOT EXISTS idx_slack_verifications_user ON slack_user_verifications(slack_user_id, slack_team_id)",
    "CREATE INDEX IF NOT EXISTS idx_slack_verifications_link ON slack_user_verifications(link_id)",
    "CREATE INDEX IF NOT EXISTS idx_slack_pending_user ON slack_pending_verifications(slack_user_id, slack_team_id)",

    # Operator queue indexes (OPS-001)
    "CREATE INDEX IF NOT EXISTS idx_operator_queue_agent ON operator_queue(agent_name)",
    "CREATE INDEX IF NOT EXISTS idx_operator_queue_status ON operator_queue(status)",
    "CREATE INDEX IF NOT EXISTS idx_operator_queue_priority ON operator_queue(priority)",
    "CREATE INDEX IF NOT EXISTS idx_operator_queue_type ON operator_queue(type)",
    "CREATE INDEX IF NOT EXISTS idx_operator_queue_created ON operator_queue(created_at DESC)",

    # Nevermined payment indexes (NVM-001)
    "CREATE INDEX IF NOT EXISTS idx_nvm_config_agent ON nevermined_agent_config(agent_name)",
    "CREATE INDEX IF NOT EXISTS idx_nvm_payment_log_agent ON nevermined_payment_log(agent_name, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_nvm_payment_log_execution ON nevermined_payment_log(execution_id)",

    # Agent event subscription indexes (EVT-001)
    "CREATE INDEX IF NOT EXISTS idx_event_subs_subscriber ON agent_event_subscriptions(subscriber_agent)",
    "CREATE INDEX IF NOT EXISTS idx_event_subs_source ON agent_event_subscriptions(source_agent)",
    "CREATE INDEX IF NOT EXISTS idx_event_subs_source_type ON agent_event_subscriptions(source_agent, event_type)",
    "CREATE INDEX IF NOT EXISTS idx_events_source ON agent_events(source_agent, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_events_type ON agent_events(event_type)",

    # Access requests indexes (Issue #311)
    "CREATE INDEX IF NOT EXISTS idx_access_requests_agent ON access_requests(agent_name, status)",
    "CREATE INDEX IF NOT EXISTS idx_access_requests_email ON access_requests(email)",

    # Platform audit log indexes (SEC-001 / Issue #20 — Phase 1)
    "CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp ON audit_log(timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_audit_log_event_type ON audit_log(event_type, timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_audit_log_actor ON audit_log(actor_type, actor_id, timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_audit_log_target ON audit_log(target_type, target_id, timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_audit_log_mcp_key ON audit_log(mcp_key_id, timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_audit_log_request ON audit_log(request_id)",

    # Canary violations indexes (CANARY-001 / Issue #411 — Phase 1)
    "CREATE INDEX IF NOT EXISTS idx_canary_violations_invariant ON canary_violations(invariant_id, snapshot_time DESC)",
    "CREATE INDEX IF NOT EXISTS idx_canary_violations_severity ON canary_violations(severity, snapshot_time DESC)",
    "CREATE INDEX IF NOT EXISTS idx_canary_violations_snapshot ON canary_violations(snapshot_time DESC)",

    # Idempotency key index (RELIABILITY-006, #525) — drives the TTL purge sweep
    "CREATE INDEX IF NOT EXISTS idx_idempotency_created ON idempotency_keys(created_at)",

    # Subscription credentials indexes (SUB-001)
    "CREATE INDEX IF NOT EXISTS idx_subscriptions_name ON subscription_credentials(name)",
    "CREATE INDEX IF NOT EXISTS idx_subscriptions_owner ON subscription_credentials(owner_id)",

    # Notifications indexes (NOTIF-001)
    "CREATE INDEX IF NOT EXISTS idx_notifications_agent ON agent_notifications(agent_name, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_notifications_status ON agent_notifications(status)",
    "CREATE INDEX IF NOT EXISTS idx_notifications_priority ON agent_notifications(priority)",

    # Subscription rate-limit indexes (SUB-003)
    "CREATE INDEX IF NOT EXISTS idx_rate_limit_agent_sub "
    "ON subscription_rate_limit_events(agent_name, subscription_id, occurred_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_rate_limit_sub "
    "ON subscription_rate_limit_events(subscription_id, occurred_at DESC)",

    # Multi-agent Slack indexes (SLACK-002)
    "CREATE INDEX IF NOT EXISTS idx_slack_channel_agents_team ON slack_channel_agents(team_id)",
    "CREATE INDEX IF NOT EXISTS idx_slack_channel_agents_agent ON slack_channel_agents(agent_name)",
    "CREATE INDEX IF NOT EXISTS idx_slack_active_threads_lookup ON slack_active_threads(team_id, channel_id, thread_ts)",

    # Telegram integration indexes (TELEGRAM-001 / TGRAM-GROUP)
    "CREATE INDEX IF NOT EXISTS idx_telegram_bindings_agent ON telegram_bindings(agent_name)",
    "CREATE INDEX IF NOT EXISTS idx_telegram_bindings_bot_id ON telegram_bindings(bot_id)",
    "CREATE INDEX IF NOT EXISTS idx_telegram_bindings_webhook ON telegram_bindings(webhook_secret)",
    "CREATE INDEX IF NOT EXISTS idx_telegram_chat_links_binding ON telegram_chat_links(binding_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_tg_group_binding_chat ON telegram_group_configs(binding_id, chat_id)",
    "CREATE INDEX IF NOT EXISTS idx_tg_group_chat_id ON telegram_group_configs(chat_id)",
    "CREATE INDEX IF NOT EXISTS idx_tg_group_active ON telegram_group_configs(is_active)",

    # WhatsApp integration indexes (WHATSAPP-001)
    "CREATE INDEX IF NOT EXISTS idx_whatsapp_bindings_agent ON whatsapp_bindings(agent_name)",
    "CREATE INDEX IF NOT EXISTS idx_whatsapp_bindings_webhook ON whatsapp_bindings(webhook_secret)",
    "CREATE INDEX IF NOT EXISTS idx_whatsapp_chat_links_binding ON whatsapp_chat_links(binding_id)",

    # VoIP telephony indexes (VOIP-001) — agent+started_at powers the daily-cap count
    "CREATE INDEX IF NOT EXISTS idx_voip_bindings_agent ON voip_bindings(agent_name)",
    "CREATE INDEX IF NOT EXISTS idx_voip_bindings_webhook ON voip_bindings(webhook_secret)",
    "CREATE INDEX IF NOT EXISTS idx_voip_call_logs_agent_started ON voip_call_logs(agent_name, started_at)",
    "CREATE INDEX IF NOT EXISTS idx_voip_call_logs_call_id ON voip_call_logs(call_id)",

    # Execution fan-out / backlog / retry partial indexes
    "CREATE INDEX IF NOT EXISTS idx_executions_fan_out ON schedule_executions(fan_out_id)",
    "CREATE INDEX IF NOT EXISTS idx_executions_queued "
    "ON schedule_executions(agent_name, queued_at) "
    "WHERE status = 'queued'",
    "CREATE INDEX IF NOT EXISTS idx_executions_pending_retry "
    "ON schedule_executions(retry_scheduled_at) "
    "WHERE retry_scheduled_at IS NOT NULL AND status = 'pending_retry'",

    # Schedule webhook token (WEBHOOK-001 / #291)
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_schedules_webhook_token "
    "ON agent_schedules(webhook_token) WHERE webhook_token IS NOT NULL",

    # Proactive messaging share lookup (#321)
    "CREATE INDEX IF NOT EXISTS idx_agent_sharing_proactive "
    "ON agent_sharing(agent_name, shared_with_email) WHERE allow_proactive = 1",

    # Sequential agent loops (#740)
    "CREATE INDEX IF NOT EXISTS idx_loops_agent ON agent_loops(agent_name)",
    "CREATE INDEX IF NOT EXISTS idx_loops_status ON agent_loops(status)",
    "CREATE INDEX IF NOT EXISTS idx_loops_user ON agent_loops(started_by_user_id)",
    "CREATE INDEX IF NOT EXISTS idx_loop_runs_loop ON agent_loop_runs(loop_id, run_number)",
    "CREATE INDEX IF NOT EXISTS idx_executions_loop ON schedule_executions(loop_id) "
    "WHERE loop_id IS NOT NULL",
]


# =============================================================================
# Triggers — Append-only enforcement for audit_log (SEC-001)
# =============================================================================

TRIGGERS = [
    # Block UPDATE on every audit_log row.
    """
    CREATE TRIGGER IF NOT EXISTS audit_log_no_update
    BEFORE UPDATE ON audit_log
    BEGIN
        SELECT RAISE(ABORT, 'Audit log entries cannot be modified');
    END
    """,
    # Block DELETE during the 365-day retention window.
    """
    CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
    BEFORE DELETE ON audit_log
    WHEN OLD.timestamp > datetime('now', '-365 days')
    BEGIN
        SELECT RAISE(ABORT, 'Audit log entries cannot be deleted within retention period');
    END
    """,
]


# =============================================================================
# Schema Functions
# =============================================================================

def create_all_tables(cursor):
    """Create all tables. Safe to call multiple times (uses IF NOT EXISTS)."""
    for table_name, create_sql in TABLES.items():
        cursor.execute(create_sql)


def create_all_indexes(cursor):
    """Create all indexes. Safe to call multiple times (uses IF NOT EXISTS)."""
    for index_sql in INDEXES:
        cursor.execute(index_sql)


def create_all_triggers(cursor):
    """Create all triggers. Safe to call multiple times (uses IF NOT EXISTS)."""
    for trigger_sql in TRIGGERS:
        cursor.execute(trigger_sql)


def init_schema(cursor, conn):
    """Initialize complete database schema.

    Creates all tables, indexes, and triggers. Safe to call on existing database.
    """
    create_all_tables(cursor)
    create_all_indexes(cursor)
    create_all_triggers(cursor)
    conn.commit()


# =============================================================================
# PostgreSQL DDL (#300) — schema.py stays the single source of truth
# =============================================================================
#
# Rather than maintain a second hand-written schema (a fresh source of drift —
# see #655/#691/#721), we translate the SAME `TABLES`/`INDEXES` strings into
# PostgreSQL DDL. The dialect deltas are small and mechanical:
#   * INTEGER PRIMARY KEY AUTOINCREMENT  → SERIAL PRIMARY KEY
#   * DEFAULT (datetime('now')) / CURRENT_TIMESTAMP on TEXT cols
#                                        → a text-typed UTC timestamp expr
#   * the two audit_log RAISE(ABORT) triggers → PL/pgSQL equivalents
# Everything else (partial indexes, REAL, INTEGER-booleans, ON DELETE CASCADE,
# IF NOT EXISTS) is valid on both backends as-is.
#
# A FRESH PostgreSQL database created from these strings is already at head, so
# the sqlite-only PRAGMA-based migrations in db/migrations.py do NOT run on PG
# (they exist solely to upgrade pre-existing sqlite files). PostgreSQL is an
# experimental backend (#300) — start fresh.
import re as _re

# Applied in order to each CREATE TABLE string.
#
# FK stripping: the platform runs with foreign keys DISABLED on sqlite (PRAGMA
# foreign_keys is never turned on — cascades are performed in application code,
# e.g. the agent delete handler + rename_agent). Several FK declarations are
# therefore aspirational AND have mismatched column types (e.g. TEXT
# added_by → INTEGER users.id) that sqlite silently accepts but PostgreSQL
# rejects at CREATE. We strip FK constraints for PG so runtime behavior is
# identical (no enforcement on either backend). Table-level FKs are removed
# first, then any remaining inline ``REFERENCES`` column qualifiers.
_PG_TABLE_SUBS = [
    (_re.compile(
        r",\s*FOREIGN\s+KEY\s*\([^)]*\)\s*REFERENCES\s+\w+\s*\([^)]*\)"
        r"(?:\s+ON\s+DELETE\s+\w+)?(?:\s+ON\s+UPDATE\s+\w+)?",
        _re.IGNORECASE),
     ""),
    (_re.compile(
        r"\s+REFERENCES\s+\w+\s*\([^)]*\)"
        r"(?:\s+ON\s+DELETE\s+\w+)?(?:\s+ON\s+UPDATE\s+\w+)?",
        _re.IGNORECASE),
     ""),
    (_re.compile(r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", _re.IGNORECASE),
     "SERIAL PRIMARY KEY"),
    # TEXT timestamp columns whose sqlite default is datetime('now'); keep them
    # text-typed on PG so the column type is unchanged (ISO-ish space format,
    # matching sqlite's CURRENT_TIMESTAMP output).
    (_re.compile(r"DEFAULT\s+\(datetime\('now'\)\)", _re.IGNORECASE),
     "DEFAULT (to_char((now() at time zone 'utc'), 'YYYY-MM-DD HH24:MI:SS'))"),
    (_re.compile(r"DEFAULT\s+CURRENT_TIMESTAMP", _re.IGNORECASE),
     "DEFAULT (to_char((now() at time zone 'utc'), 'YYYY-MM-DD HH24:MI:SS'))"),
]


def to_postgres_table_ddl(create_sql: str) -> str:
    """Translate one sqlite CREATE TABLE string to PostgreSQL."""
    out = create_sql
    for pattern, repl in _PG_TABLE_SUBS:
        out = pattern.sub(repl, out)
    return out


# PostgreSQL equivalents of the audit_log append-only triggers (SEC-001).
# CREATE OR REPLACE TRIGGER requires PostgreSQL 14+ (we target 16).
POSTGRES_TRIGGERS = [
    """
    CREATE OR REPLACE FUNCTION audit_log_no_update_fn() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'Audit log entries cannot be modified';
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    CREATE OR REPLACE TRIGGER audit_log_no_update
    BEFORE UPDATE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_no_update_fn()
    """,
    # audit_log.timestamp is ISO-Z text (utc_now_iso). Compare lexicographically
    # against an identically-formatted cutoff — the same logic as the sqlite
    # trigger's datetime('now','-365 days') guard (Invariant #16).
    """
    CREATE OR REPLACE FUNCTION audit_log_no_delete_fn() RETURNS trigger AS $$
    BEGIN
        IF OLD.timestamp > to_char(
            (now() at time zone 'utc') - interval '365 days',
            'YYYY-MM-DD"T"HH24:MI:SS"Z"'
        ) THEN
            RAISE EXCEPTION 'Audit log entries cannot be deleted within retention period';
        END IF;
        RETURN OLD;
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    CREATE OR REPLACE TRIGGER audit_log_no_delete
    BEFORE DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_no_delete_fn()
    """,
]


def init_schema_postgres(engine) -> None:
    """Create the full schema on a PostgreSQL engine (#300, experimental).

    Idempotent: tables/indexes use IF NOT EXISTS; triggers use CREATE OR
    REPLACE. Mirrors ``init_schema`` for sqlite but emits PostgreSQL DDL.
    """
    from sqlalchemy import text

    with engine.begin() as conn:
        for create_sql in TABLES.values():
            conn.execute(text(to_postgres_table_ddl(create_sql)))
        for index_sql in INDEXES:
            conn.execute(text(index_sql))
        for trigger_sql in POSTGRES_TRIGGERS:
            conn.execute(text(trigger_sql))
