"""
Settings service for retrieving configuration values.

Provides centralized access to:
- Database-stored settings
- Environment variable fallbacks
- Typed conversions

This service breaks the circular dependency where services were importing
from routers.settings. Now all settings retrieval logic lives here, and
routers.settings re-exports these functions for backward compatibility.
"""
import json
import os
import time
from typing import List, Optional
from database import db

# Platform default model (#831)
PLATFORM_DEFAULT_MODEL_KEY = "platform_default_model"
PLATFORM_DEFAULT_MODEL_VALUE = "claude-sonnet-4-6"
_platform_model_cache: Optional[str] = None
_platform_model_cache_ts: float = 0.0
_PLATFORM_MODEL_CACHE_TTL = 60.0

# #894: current-gen models an agent owner may select as a per-agent override for
# public-facing channels. Mirrors the current presets in
# `frontend/src/components/ModelSelector.vue` (kept in sync by hand — there is no
# shared model registry across the Python/Vue boundary). A model removed from
# this set after it was saved is treated as unset (→ platform default) by
# `db.get_public_channel_model`, matching the #1080 graceful-degradation posture.
PUBLIC_CHANNEL_MODELS = frozenset({
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
})


def is_valid_public_channel_model(model: str) -> bool:
    """True if `model` is a selectable per-agent public-channel override (#894)."""
    return model in PUBLIC_CHANNEL_MODELS


# ============================================================================
# Ops Settings Configuration - moved from routers/settings.py
# ============================================================================

# Default values for ops settings (as specified in requirements)
OPS_SETTINGS_DEFAULTS = {
    "ops_context_warning_threshold": "75",  # Context % to trigger warning
    "ops_context_critical_threshold": "90",  # Context % to trigger reset/action
    "ops_idle_timeout_minutes": "30",  # Minutes before stuck detection
    "ops_cost_limit_daily_usd": "50.0",  # Daily cost limit (0 = unlimited)
    "ops_max_execution_minutes": "10",  # Max chat execution time
    "ops_alert_suppression_minutes": "15",  # Suppress duplicate alerts
    "ops_log_retention_days": "7",  # Days to keep container logs
    "ops_health_check_interval": "60",  # Seconds between health checks
    "ssh_access_enabled": "false",  # Enable SSH access via MCP tool
    # Issue #772: retention policy for execution_log + agent_health_checks.
    # "0" disables that prune step.
    "execution_log_retention_days": "30",  # Null `execution_log` TEXT after N days
    "execution_row_retention_days": "90",  # DELETE schedule_executions rows after N days
    "health_check_retention_days": "7",   # DELETE agent_health_checks rows after N days
    # Issue #834 Phase 1a: soft-delete retention for agents. After
    # DELETE /api/agents/{name}, the agent_ownership row is marked
    # `deleted_at = NOW` and child rows are preserved. The cleanup
    # sweep hard-deletes rows older than this many days (cascading
    # child tables via #816's purge primitive). "0" disables the
    # sweep entirely — soft-deleted rows then persist until manually
    # purged.
    "agent_soft_delete_retention_days": "180",
    # Issue #834 Phase 1b: per-schedule soft-delete. Schedules are
    # higher-churn than agents (users tweak/replace cron expressions
    # often), so default is shorter than the agent window. "0"
    # disables the sweep.
    "schedule_soft_delete_retention_days": "30",
    # Issue #918: retention for agent_reports. Rows older than this many days
    # are deleted by the cleanup sweep. "0" disables the sweep.
    "agent_reports_retention_days": "90",
}

# Descriptions for each ops setting
OPS_SETTINGS_DESCRIPTIONS = {
    "ops_context_warning_threshold": "Context usage percentage to trigger a warning (default: 75)",
    "ops_context_critical_threshold": "Context usage percentage to trigger critical alert or action (default: 90)",
    "ops_idle_timeout_minutes": "Minutes of inactivity before an agent is considered stuck (default: 30)",
    "ops_cost_limit_daily_usd": "Maximum daily cost limit in USD per agent (0 = unlimited) (default: 50.0)",
    "ops_max_execution_minutes": "Maximum allowed execution time for a single chat in minutes (default: 10)",
    "ops_alert_suppression_minutes": "Minutes to suppress duplicate alerts for same agent+type (default: 15)",
    "ops_log_retention_days": "Number of days to retain container logs (default: 7)",
    "ops_health_check_interval": "Seconds between automated health checks (default: 60)",
    "ssh_access_enabled": "Enable ephemeral SSH access to agent containers via MCP tool (default: false)",
    "execution_log_retention_days": "Days to retain the JSONL transcript on schedule_executions (default: 30, 0 = disabled, #772)",
    "execution_row_retention_days": "Days to retain finished schedule_execution rows; rows older than this are deleted (default: 90, 0 = disabled, #772)",
    "health_check_retention_days": "Days to retain agent_health_checks rows (default: 7, 0 = disabled, #772)",
    "agent_soft_delete_retention_days": "Days to retain soft-deleted agents before hard-purge (default: 180, 0 = disabled, #834)",
    "schedule_soft_delete_retention_days": "Days to retain soft-deleted schedules before hard-purge (default: 30, 0 = disabled, #834)",
    "agent_reports_retention_days": "Days to retain agent_reports rows (default: 90, 0 = disabled, #918)",
}


class SettingsService:
    """
    Centralized service for retrieving settings.

    Hierarchy:
    1. Database setting (if exists)
    2. Environment variable (fallback)
    3. Default value (if provided)
    """

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Get a setting from database with optional default."""
        value = db.get_setting_value(key, None)
        return value if value is not None else default

    def get_anthropic_api_key(self) -> str:
        """Get Anthropic API key from settings, fallback to env var."""
        key = self.get_setting('anthropic_api_key')
        if key:
            return key
        return os.getenv('ANTHROPIC_API_KEY', '')

    def get_github_pat(self) -> str:
        """Get GitHub PAT from settings, fallback to env var."""
        key = self.get_setting('github_pat')
        if key:
            return key
        return os.getenv('GITHUB_PAT', '')

    def get_google_api_key(self) -> str:
        """Get Google API key from settings, fallback to env var."""
        key = self.get_setting('google_api_key')
        if key:
            return key
        return os.getenv('GOOGLE_API_KEY', '')

    # =========================================================================
    # Slack Integration Settings (SLACK-001)
    # =========================================================================

    def get_slack_client_id(self) -> str:
        """Get Slack Client ID from settings, fallback to env var."""
        key = self.get_setting('slack_client_id')
        if key:
            return key
        return os.getenv('SLACK_CLIENT_ID', '')

    def get_slack_client_secret(self) -> str:
        """Get Slack Client Secret from settings, fallback to env var."""
        key = self.get_setting('slack_client_secret')
        if key:
            return key
        return os.getenv('SLACK_CLIENT_SECRET', '')

    def get_slack_signing_secret(self) -> str:
        """Get Slack Signing Secret from settings, fallback to env var."""
        key = self.get_setting('slack_signing_secret')
        if key:
            return key
        return os.getenv('SLACK_SIGNING_SECRET', '')

    def get_public_chat_url(self) -> str:
        """Get Public Chat URL from settings, fallback to env var."""
        url = self.get_setting('public_chat_url')
        if url:
            return url.rstrip('/')
        return os.getenv('PUBLIC_CHAT_URL', '').rstrip('/')

    def get_slack_transport_mode(self) -> str:
        """Get Slack transport mode: 'socket' (default) or 'webhook'."""
        mode = self.get_setting('slack_transport_mode')
        if mode:
            return mode
        return os.getenv('SLACK_TRANSPORT_MODE', 'socket')

    def get_slack_app_token(self) -> str:
        """Get Slack App-Level Token (xapp-...) for Socket Mode."""
        token = self.get_setting('slack_app_token')
        if token:
            return token
        return os.getenv('SLACK_APP_TOKEN', '')

    # =========================================================================
    # Session tab feature flag (Phase 1.6 of SESSION_TAB_2026-04)
    # =========================================================================

    def is_session_tab_enabled(self) -> bool:
        """
        Whether the Session tab UI surface is exposed to users.

        Resolves in this order:
        1. system_settings row 'session_tab_enabled' ("true"/"false")
        2. SESSION_TAB_ENABLED env var (only honored as "false"/"0"/"no" to opt out)
        3. Default: True (GA — Phase 5.3, 2026-05-04)

        Admins can opt out by setting ``session_tab_enabled=false`` in
        system_settings or by exporting ``SESSION_TAB_ENABLED=false``.

        The flag gates only the new UI surface and the new
        ``/api/agents/{name}/session*`` endpoints. Chat is unaffected.
        """
        stored = self.get_setting('session_tab_enabled')
        if stored is not None:
            return str(stored).lower() in ("true", "1", "yes")
        env_val = os.getenv('SESSION_TAB_ENABLED', '').strip().lower()
        if env_val in ("false", "0", "no"):
            return False
        return True

    # =========================================================================
    # Workspace feature flag (#860)
    # =========================================================================

    def is_workspace_enabled(self) -> bool:
        """
        Whether the Agent Workspace (voice + canvas) surface is exposed to users.

        Resolves in this order:
        1. system_settings row 'workspace_enabled' ("true"/"false")
        2. WORKSPACE_ENABLED env var (only honored as "true"/"1"/"yes" to opt in)
        3. Default: False (BETA — opt-in required)

        Admins opt in by setting ``workspace_enabled=true`` in system_settings
        or by exporting ``WORKSPACE_ENABLED=true``.

        Note: workspace also requires voice to be available (VOICE_ENABLED +
        GEMINI_API_KEY). The feature-flags endpoint combines both conditions.
        """
        stored = self.get_setting('workspace_enabled')
        if stored is not None:
            return str(stored).lower() in ("true", "1", "yes")
        env_val = os.getenv('WORKSPACE_ENABLED', '').strip().lower()
        if env_val in ("true", "1", "yes"):
            return True
        return False

    # =========================================================================
    # GitHub Templates (TMPL-001)
    # =========================================================================

    def get_github_templates(self) -> Optional[List[dict]]:
        """
        Get admin-configured GitHub templates from system_settings.

        Returns:
            list[dict] - configured templates (may be empty list)
            None - no configuration (use hardcoded defaults)
        """
        raw = self.get_setting('github_templates')
        if raw is None:
            return None
        try:
            templates = json.loads(raw)
            if not isinstance(templates, list):
                return None
            return templates
        except (json.JSONDecodeError, TypeError):
            return None

    def set_github_templates(self, templates: List[dict]) -> None:
        """Save GitHub templates configuration to system_settings."""
        db.set_setting('github_templates', json.dumps(templates))

    def delete_github_templates(self) -> bool:
        """Delete GitHub templates configuration (revert to defaults)."""
        return db.delete_setting('github_templates')

    def get_platform_default_model(self) -> str:
        """
        Return the platform-wide default Claude model (#831).

        Resolution: system_settings.platform_default_model → PLATFORM_DEFAULT_MODEL_VALUE.
        Result is cached for 60 s to avoid per-turn SQLite reads during burst drain.
        """
        global _platform_model_cache, _platform_model_cache_ts
        now = time.monotonic()
        if _platform_model_cache is not None and (now - _platform_model_cache_ts) < _PLATFORM_MODEL_CACHE_TTL:
            return _platform_model_cache
        value = self.get_setting(PLATFORM_DEFAULT_MODEL_KEY, PLATFORM_DEFAULT_MODEL_VALUE)
        _platform_model_cache = value or PLATFORM_DEFAULT_MODEL_VALUE
        _platform_model_cache_ts = now
        return _platform_model_cache

    def get_ops_setting(self, key: str, as_type: type = str):
        """
        Get an ops setting with type conversion.

        Uses defaults from OPS_SETTINGS_DEFAULTS if not set.
        """
        default = OPS_SETTINGS_DEFAULTS.get(key, "")
        value = self.get_setting(key, default)

        if as_type == int:
            return int(value)
        elif as_type == float:
            return float(value)
        elif as_type == bool:
            return str(value).lower() in ("true", "1", "yes")
        return value


# Singleton instance
settings_service = SettingsService()


# Convenience functions for backward compatibility
def get_anthropic_api_key() -> str:
    """Get Anthropic API key from settings, fallback to env var."""
    return settings_service.get_anthropic_api_key()


def get_github_pat() -> str:
    """Get GitHub PAT from settings, fallback to env var."""
    return settings_service.get_github_pat()


def get_google_api_key() -> str:
    """Get Google API key from settings, fallback to env var."""
    return settings_service.get_google_api_key()


# Slack Integration Settings (SLACK-001)
def get_slack_client_id() -> str:
    """Get Slack Client ID from settings, fallback to env var."""
    return settings_service.get_slack_client_id()


def get_slack_client_secret() -> str:
    """Get Slack Client Secret from settings, fallback to env var."""
    return settings_service.get_slack_client_secret()


def get_slack_signing_secret() -> str:
    """Get Slack Signing Secret from settings, fallback to env var."""
    return settings_service.get_slack_signing_secret()


def get_public_chat_url() -> str:
    """Get Public Chat URL from settings, fallback to env var."""
    return settings_service.get_public_chat_url()


def get_slack_transport_mode() -> str:
    """Get Slack transport mode: 'socket' or 'webhook'."""
    return settings_service.get_slack_transport_mode()


def get_slack_app_token() -> str:
    """Get Slack App-Level Token for Socket Mode."""
    return settings_service.get_slack_app_token()


def is_session_tab_enabled() -> bool:
    """Session tab feature flag (Phase 1.6 of SESSION_TAB_2026-04)."""
    return settings_service.is_session_tab_enabled()


def get_ops_setting(key: str, as_type: type = str):
    """Get an ops setting with type conversion."""
    return settings_service.get_ops_setting(key, as_type)


# ============================================================================
# Agent Quota Settings (QUOTA-001)
# ============================================================================

# Per-role defaults for agent creation limits (0 = unlimited)
AGENT_QUOTA_DEFAULTS = {
    "max_agents_creator": "10",
    "max_agents_operator": "3",
    "max_agents_user": "1",
}

AGENT_QUOTA_DESCRIPTIONS = {
    "max_agents_creator": "Maximum agents a creator can own (0 = unlimited, default: 10)",
    "max_agents_operator": "Maximum agents an operator can own (0 = unlimited, default: 3)",
    "max_agents_user": "Maximum agents a regular user can own (0 = unlimited, default: 1)",
}


def get_agent_quota_for_role(role: str) -> int:
    """
    Get the agent creation quota for a given user role.

    Admin users are always exempt (returns 0 = unlimited).
    Other roles check max_agents_{role}, falling back to the legacy
    max_agents_per_user setting, then to role-specific defaults.

    Returns:
        int: Maximum agents allowed (0 = unlimited)
    """
    if role == "admin":
        return 0

    # Check per-role setting first
    role_key = f"max_agents_{role}"
    value = settings_service.get_setting(role_key)
    if value is not None:
        return int(value)

    # Fall back to legacy global setting
    legacy = settings_service.get_setting("max_agents_per_user")
    if legacy is not None:
        return int(legacy)

    # Fall back to role-specific default
    default = AGENT_QUOTA_DEFAULTS.get(role_key, "3")
    return int(default)


def get_agent_full_capabilities() -> bool:
    """
    Get system-wide agent full capabilities setting.

    When True: Agents run with Docker default capabilities (can apt-get install, etc.)
    When False: Agents run with restricted capabilities (more secure, but limited)

    Default: True (agents have full control of their container environment)
    """
    value = settings_service.get_setting('agent_full_capabilities', 'true')
    return str(value).lower() in ('true', '1', 'yes')


# ============================================================================
# Skills Library Settings
# ============================================================================

def get_skills_library_url() -> Optional[str]:
    """
    Get the skills library GitHub repository URL.

    Returns None if not configured (feature disabled).

    Example: "github.com/Abilityai/skills-library-41"
    """
    return settings_service.get_setting('skills_library_url')


def get_skills_library_branch() -> str:
    """
    Get the skills library branch to use.

    Default: "main"
    """
    return settings_service.get_setting('skills_library_branch', 'main')


# ============================================================================
# Agent Default Resources (RES-001)
# ============================================================================

AGENT_DEFAULT_CPU_KEY = "agent_default_cpu"
AGENT_DEFAULT_MEMORY_KEY = "agent_default_memory"
AGENT_DEFAULT_CPU = "2"
AGENT_DEFAULT_MEMORY = "4g"


def get_agent_default_resources() -> dict:
    """
    Get system-wide default CPU and memory for new agent containers.

    Returns dict with 'cpu' (number of processors, string) and 'memory' (e.g. '4g').
    These are used as fallback when no per-agent resource limits are configured.
    """
    cpu = db.get_setting_value(AGENT_DEFAULT_CPU_KEY, AGENT_DEFAULT_CPU)
    memory = db.get_setting_value(AGENT_DEFAULT_MEMORY_KEY, AGENT_DEFAULT_MEMORY)
    return {"cpu": cpu or AGENT_DEFAULT_CPU, "memory": memory or AGENT_DEFAULT_MEMORY}


# ============================================================================
# Max Parallel Tasks Ceiling (#506 — fleet-wide cap on per-agent concurrency)
# ============================================================================

MAX_PARALLEL_TASKS_CEILING_KEY = "max_parallel_tasks_ceiling"
MAX_PARALLEL_TASKS_CEILING_DEFAULT = 10
MAX_PARALLEL_TASKS_CEILING_MIN = 1
MAX_PARALLEL_TASKS_CEILING_MAX = 32


def get_max_parallel_tasks_ceiling() -> int:
    """Fleet-wide ceiling on any single agent's ``max_parallel_tasks`` (#506).

    An admin sets it (1–32); owners pick a per-agent value within it. Stored
    in the generic ``system_settings`` key/value store (no migration), so the
    value comes back as a string and is parsed to int. A garbage/absent value
    falls back to the code default, and an out-of-range integer is clamped into
    ``[MIN, MAX]`` — the defensive backstop for the dedicated range-validated
    PUT (so a stray store value can neither fail-close the fleet nor defeat the
    host cap).

    No per-process cache: the backend runs ``--workers 2``; a TTL cache would
    let a stale worker keep admitting above a just-lowered ceiling. Read-through
    every call — one cheap SQLite read, negligible next to the Redis ops already
    on the admit path, and the ceiling applies instantly across workers.
    """
    try:
        raw = settings_service.get_setting(MAX_PARALLEL_TASKS_CEILING_KEY)
    except Exception:
        # Fail-open on the admit hot path: a settings read failure (DB down /
        # unit-test stub) must never crash dispatch. Default ceiling applies.
        return MAX_PARALLEL_TASKS_CEILING_DEFAULT
    if raw is None:
        return MAX_PARALLEL_TASKS_CEILING_DEFAULT
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return MAX_PARALLEL_TASKS_CEILING_DEFAULT
    # #506 defense-in-depth: the dedicated PUT range-validates and the generic
    # PUT is blocked for this key, but this getter is the single read-through
    # feeding BOTH owner validation (agent_config) AND the runtime clamp
    # (capacity_manager / bypasses). If an out-of-range value ever reaches the
    # generic key/value store (a direct DB write, a future writer that skips
    # the validated route), enforce the [MIN, MAX] invariant here. Otherwise a
    # stray "0" makes every agent's effective cap min(stored, 0) == 0 — a
    # fleet-wide fail-CLOSED (no agent can ever acquire a slot) — and a "999"
    # silently defeats the host-protection cap. Clamp (don't default) so a
    # configured-low or configured-high intent is preserved within bounds.
    if parsed < MAX_PARALLEL_TASKS_CEILING_MIN:
        return MAX_PARALLEL_TASKS_CEILING_MIN
    if parsed > MAX_PARALLEL_TASKS_CEILING_MAX:
        return MAX_PARALLEL_TASKS_CEILING_MAX
    return parsed


def clamp_to_ceiling(n: int) -> int:
    """Clamp a per-agent concurrency cap to the fleet ceiling (#506).

    Single source of truth for the runtime clamp used by the
    ``CapacityManager`` facade. "Clamp on use, never auto-rewrite the stored
    value" — the agent keeps its chosen ``max_parallel_tasks``; only the
    *effective* admit limit is capped.
    """
    return min(n, get_max_parallel_tasks_ceiling())


def get_effective_max_parallel_tasks(agent_name: str) -> int:
    """Effective per-agent concurrency = stored cap clamped to the ceiling (#506).

    Used by the two genuine ``CapacityManager`` facade-bypasses
    (``backlog_service``, ``agent_call_limiter``).
    """
    from database import db
    return clamp_to_ceiling(db.get_max_parallel_tasks(agent_name))


# ============================================================================
# Agent Default Access Policy (#1129 — secure-by-default require_email)
# ============================================================================

AGENT_DEFAULT_REQUIRE_EMAIL_KEY = "agent_default_require_email"
# Secure-by-default: new agents require a verified email on incoming
# DMs / public chat / shared access unless the owner opts a specific agent
# out. The default is read only at creation time, never applied
# retroactively — existing agents keep their per-agent value.
AGENT_DEFAULT_REQUIRE_EMAIL = True


def get_agent_default_require_email() -> bool:
    """System-wide default for the per-agent ``require_email`` access-policy
    flag (#311), seeded onto new agents at creation (#1129).

    Stored in ``system_settings`` as ``'1'``/``'0'``; when the key is absent
    (fresh installs, or instances that never set it) the code default ON
    applies — so the platform is secure-by-default without a data migration.
    """
    raw = db.get_setting_value(AGENT_DEFAULT_REQUIRE_EMAIL_KEY, None)
    if raw is None:
        return AGENT_DEFAULT_REQUIRE_EMAIL
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


# GitHub Templates (TMPL-001)
def get_github_templates() -> Optional[List[dict]]:
    """Get admin-configured GitHub templates, or None for defaults."""
    return settings_service.get_github_templates()


def get_platform_default_model() -> str:
    """Return the platform-wide default Claude model (#831)."""
    return settings_service.get_platform_default_model()
