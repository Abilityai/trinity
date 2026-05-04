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
from typing import List, Optional
from database import db


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


# GitHub Templates (TMPL-001)
def get_github_templates() -> Optional[List[dict]]:
    """Get admin-configured GitHub templates, or None for defaults."""
    return settings_service.get_github_templates()
