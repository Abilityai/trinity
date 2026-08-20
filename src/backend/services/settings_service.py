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
import logging
import os
import time
from typing import List, Optional
from database import db

logger = logging.getLogger(__name__)

# Platform default model (#831)
PLATFORM_DEFAULT_MODEL_KEY = "platform_default_model"
PLATFORM_DEFAULT_MODEL_VALUE = "claude-sonnet-4-6"
_platform_model_cache: Optional[str] = None
_platform_model_cache_ts: float = 0.0
_PLATFORM_MODEL_CACHE_TTL = 60.0

# #894: current-gen models an agent owner may select as a per-agent override for
# public-facing channels. Derived from the single source of truth,
# `services/model_catalog.py` (#2086) — every selectable-model list now flows from
# that one file (add a model there, re-run `scripts/gen_model_catalog.py`). A model
# removed from this set after it was saved is treated as unset (→ platform default)
# by `db.get_public_channel_model`, matching the #1080 graceful-degradation posture.
from services.model_catalog import PUBLIC_CHANNEL_MODELS  # noqa: E402  (re-export)


def is_valid_public_channel_model(model: str) -> bool:
    """True if `model` is a selectable per-agent public-channel override (#894)."""
    return model in PUBLIC_CHANNEL_MODELS


# ============================================================================
# Ops Settings Configuration - moved from routers/settings.py
# ============================================================================

# Re-exported from config.py (a leaf module) — database.py seeds these during
# init_database(), which runs at import, and this module imports `db` from
# database, so defining them here would be a circular import (#1638). Importers
# keep using `from services.settings_service import ...`.
from config import (  # noqa: E402
    COMMUNITY_FRESH_INSTALL_SEED,  # noqa: F401  (re-export)
    COMMUNITY_RETENTION_FLOOR_DAYS,  # noqa: F401  (re-export)
)

# The operator-tunable retention OPS-settings keys reported by
# `GET /api/settings/retention` (audit log excluded — separate env-driven
# 365-day floor). Membership here means "is a retention window"; it does NOT
# mean "gets the community floor" — that set is COMMUNITY_FRESH_INSTALL_SEED.
RETENTION_OPS_KEYS = (
    "execution_log_retention_days",
    "execution_row_retention_days",
    "health_check_retention_days",
    "agent_soft_delete_retention_days",
    "schedule_soft_delete_retention_days",
    # #1644: these two ARE retention windows and were missing here, so three
    # readers were silently blind to them:
    #   - `POST /api/settings/ops/reset` skips only RETENTION_OPS_KEYS, so it
    #     DELETED these two rows while reporting "retention windows unchanged";
    #   - `GET /api/settings/retention` never reported them;
    #   - `log_effective_retention_windows()` never logged them at boot — the
    #     exact observability gap that made #1638 invisible.
    # Membership means "is a retention window"; it does NOT mean "gets the
    # community floor" (that set is COMMUNITY_FRESH_INSTALL_SEED, unchanged).
    "agent_reports_retention_days",
    "operator_queue_retention_days",
    # #1296: terminal agent_reminders rows (fired/cancelled/failed). A retention
    # window (surfaced/logged/reset-protected), NOT a community-floor key.
    "agent_reminders_retention_days",
    # #2216: database-backup artifacts under /data/backups. Membership here
    # buys the write-path protections (validated /ops/config only, generic
    # PUT 422-blocked, /ops/reset skips) — but its READ is special-cased:
    # every surface renders it through
    # services.db_backup_service.effective_backup_retention_days(), whose
    # coercion is INVERTED (garbage → 14, never → 0/keep-forever), and
    # GET /api/settings/retention excludes it from the generic windows map.
    # NOT a community-floor key (fewer days = the destructive direction here).
    "backup_retention_days",
)

# The RETENTION_OPS_KEYS members whose prune is NOT a #1644 row sweep (#2216).
# `backup_retention_days` prunes FILE artifacts from the backup job's own tail;
# its bounded-destruction guarantee is structural (the fixed BACKUP_MIN_KEEP
# floor in db/backup_primitives.py — never zero recovery points), NOT the
# count-threshold/ack-gated `_guard_allows` refusal in cleanup_service: an
# ack-gated refusal fails in the INVERTED direction for backups (refused prune
# → backups fill the disk, #1871 class), so that prune must run unconditionally
# within its floor. `tests/unit/test_1771a_retention_edges.py` asserts every
# key in RETENTION_OPS_KEYS minus THIS set has exactly one `_guard_allows`
# call site — add a second file-artifact window HERE, or the guard fires.
NON_ROW_RETENTION_OPS_KEYS = frozenset({"backup_retention_days"})


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
    # RETENTION DEFAULTS — READ THIS BEFORE CHANGING A NUMBER BELOW (#1638).
    #
    # These are the fallback used at PRUNE time for an install with no
    # `system_settings` row, which is the default state for every install that
    # never touched retention. Lowering one of them silently hard-DELETEs the
    # existing data of every such install, ~seconds after its next boot, with no
    # error and a green /health. That is #1638; it cost ~3 months of execution
    # history on a real instance.
    #
    # So: these stay at the widest (safest) historical value. The #1039
    # community floor is applied to NEW installs by seeding rows
    # (COMMUNITY_FRESH_INSTALL_SEED), which only ever touches an empty DB.
    # If you want to shrink a window for existing installs, that is a migration
    # + a docs/migrations/ entry + an operator decision — not an edit here.
    #
    # Issue #772: retention policy for execution_log + agent_health_checks.
    # "0" disables that prune step.
    "execution_log_retention_days": "30",  # Null `execution_log` TEXT after N days (#772)
    "execution_row_retention_days": "90",  # DELETE schedule_executions rows after N days (#772)
    "health_check_retention_days": "7",    # DELETE agent_health_checks rows after N days (#772)
    # Issue #834 Phase 1a: soft-delete retention for agents. After
    # DELETE /api/agents/{name}, the agent_ownership row is marked
    # `deleted_at = NOW` and child rows are preserved. The cleanup
    # sweep hard-deletes rows older than this many days (cascading
    # child tables via #816's purge primitive) AND removes the agent's
    # data volumes (#1581). "0" disables the sweep entirely.
    # #1638: EXEMPT from the community floor in every edition — this is a
    # recovery window whose expiry destroys agent workspaces, not a log window.
    "agent_soft_delete_retention_days": "180",
    # Issue #834 Phase 1b: per-schedule soft-delete. "0" disables the sweep.
    "schedule_soft_delete_retention_days": "30",
    # Issue #918: retention for agent_reports. Rows older than this many days
    # are deleted by the cleanup sweep. "0" disables the sweep.
    "agent_reports_retention_days": "90",
    # Issue #1142: retention for terminal operator_queue rows
    # (acknowledged/cancelled/expired). "0" disables the sweep. `responded` rows
    # get a more generous fixed floor (never deleted younger than #772's guard).
    "operator_queue_retention_days": "90",
    # Issue #1296: retention for TERMINAL agent_reminders (fired/cancelled/
    # failed). Rows older than this many days are deleted; pending/firing never
    # deleted. "0" disables the sweep. Wide/safe default per the #1638 floor rule.
    "agent_reminders_retention_days": "90",
    # Issue #2216: retention for database-backup artifacts. The #1638 "widest
    # value" rule applies in spirit but the direction INVERTS: raising this
    # default costs disk on every un-configured install (#1871 class), while
    # lowering it deletes recovery points — NEVER lower it for existing
    # installs without a migration note, and never raise it casually either.
    # "0" is INVALID for this key (validated 1–3650): keep-forever is the
    # disk-fill trap; disabling backups is DB_BACKUP_ENABLED=false.
    "backup_retention_days": "14",
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
    "operator_queue_retention_days": "Days to retain terminal operator_queue rows (acknowledged/cancelled/expired; default: 90, 0 = disabled, #1142)",
    "agent_reminders_retention_days": "Days to retain terminal agent_reminders rows (fired/cancelled/failed; default: 90, 0 = disabled, #1296)",
    "backup_retention_days": "Days to retain database-backup artifacts in /data/backups (default: 14, bounds 1-3650 — 0 is invalid; the newest 3 artifacts are always kept; disable backups via DB_BACKUP_ENABLED=false, #2216)",
}


# --- Remote template registry keys (TMPL-002, trinity-enterprise#14) -------
TEMPLATE_REGISTRY_URL_KEY = "template_registry_url"
TEMPLATE_REGISTRY_ENABLED_KEY = "template_registry_enabled"
TEMPLATE_REGISTRY_GENERATION_KEY = "template_registry_generation"
TEMPLATE_REGISTRY_LKG_KEY = "template_registry_lkg"

#: Every registry key the generic `PUT /api/settings/{key}` must refuse, so the
#: dedicated validated route is the only write path. The URL alone would be
#: enough to matter (the SSRF gate lives on that route); `generation` and `lkg`
#: are here because they are the cache itself, and a writable cache is a
#: poisonable one. Same 422-with-a-pointer shape as RETENTION_OPS_KEYS (ent#297)
#: and SKILLS_AUTOMATION_KEYS (ent#236).
TEMPLATE_REGISTRY_KEYS = frozenset({
    TEMPLATE_REGISTRY_URL_KEY,
    TEMPLATE_REGISTRY_ENABLED_KEY,
    TEMPLATE_REGISTRY_GENERATION_KEY,
    TEMPLATE_REGISTRY_LKG_KEY,
})


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

    # =========================================================================
    # Credential-bearing settings — encrypted at rest (trinity-enterprise#435)
    # =========================================================================
    #
    # These rows hold LIVE third-party credentials and used to sit in cleartext
    # (CWE-312). They now resolve encrypted-row → legacy-row-with-lazy-migration
    # → env → '', with the policy in ``services/secret_settings.py``.
    #
    # Resolution order matters twice over. Encrypted-first means the steady state
    # never touches the legacy key at all — one read, same cost as before. The
    # legacy leg is not dead code that the migration made unreachable: a
    # pre-fix backup restore, a rollback-then-roll-forward, or a direct DB write
    # can all put cleartext back, and the read path is the only thing that
    # notices. So it re-encrypts and DELETEs on sight, which makes cleartext
    # transient by construction rather than merely absent right now.
    #
    # Fail-open on READ (an unreadable envelope degrades to env, never a 500 —
    # ``get_elevenlabs_api_key``'s rule) and fail-CLOSED on WRITE (no encryption
    # key ⇒ refuse, never silently store cleartext, which is the whole defect).

    def _resolve_secret_setting(self, key: str, env_var: str) -> str:
        """Encrypted row → legacy cleartext row (migrated on sight) → env → ''."""
        from services.secret_settings import (
            decrypt_secret_setting,
            encrypted_key_for,
            looks_like_envelope,
        )

        envelope = self.get_setting(encrypted_key_for(key))
        if envelope:
            value = decrypt_secret_setting(key, envelope)
            if value:
                return value
            # Unreadable envelope (wrong/rotated key, corrupt row). Fall through
            # to env rather than raising — but do NOT fall through to the legacy
            # row: a stale cleartext value silently outranking the current
            # encrypted one is worse than being unconfigured.
            return os.getenv(env_var, '')

        legacy = self.get_setting(key)
        if legacy and legacy.strip():
            if looks_like_envelope(legacy):
                # Legacy key already holds an envelope — a half-applied older
                # in-place scheme, or a hand-written row. Decrypt it, and let the
                # migration below normalise it onto the encrypted key name.
                decrypted = decrypt_secret_setting(key, legacy)
                if decrypted:
                    self._migrate_legacy_secret_setting(key, decrypted)
                    return decrypted
                return os.getenv(env_var, '')
            self._migrate_legacy_secret_setting(key, legacy)
            return legacy

        return os.getenv(env_var, '')

    def _migrate_legacy_secret_setting(self, key: str, value: str) -> None:
        """Encrypt ``value`` onto the encrypted key and drop the cleartext row.

        Best-effort: this runs on a READ path, so a failure must return the
        credential the caller asked for rather than break agent startup. The
        one-shot migration and the next read both retry it.

        ``--workers 2``-safe without a lock: two workers racing produce two
        different envelopes of the SAME plaintext, the upsert is last-write-wins,
        and both DELETEs of the legacy row are idempotent. Order is
        encrypt-then-write-then-delete, so a crash between steps leaves the
        cleartext row intact and the next read retries — never a lost credential.
        """
        from services.secret_settings import encrypt_secret_setting, encrypted_key_for

        try:
            db.set_setting(encrypted_key_for(key), encrypt_secret_setting(key, value))
            db.delete_setting(key)
            logger.warning(
                f"Migrated cleartext credential setting '{key}' to "
                f"'{encrypted_key_for(key)}' on read (ent#435). Rotate this "
                f"credential: historical backups still contain the plaintext."
            )
        except Exception as e:  # noqa: BLE001 — read path must not break
            logger.error(f"Failed to encrypt legacy credential setting '{key}': {e}")

    def set_secret_setting(self, key: str, value: str) -> None:
        """Persist a credential-bearing setting AES-256-GCM encrypted.

        The ONLY supported writer for the keys in ``SECRET_SETTING_KEYS`` — the
        cleartext row is refused at the sink. Also clears any legacy cleartext
        row, so re-setting a credential on a not-yet-migrated install is itself a
        migration.
        """
        from services.secret_settings import (
            SECRET_SETTING_KEYS,
            encrypt_secret_setting,
            encrypted_key_for,
        )

        if key not in SECRET_SETTING_KEYS:
            raise ValueError(
                f"'{key}' is not a registered credential setting; add it to "
                f"services.secret_settings.SECRET_SETTING_KEYS first"
            )
        db.set_setting(encrypted_key_for(key), encrypt_secret_setting(key, value.strip()))
        db.delete_setting(key)

    def clear_secret_setting(self, key: str) -> bool:
        """Remove a credential setting (both forms). True if anything was removed."""
        from services.secret_settings import encrypted_key_for

        removed_encrypted = db.delete_setting(encrypted_key_for(key))
        removed_legacy = db.delete_setting(key)
        return removed_encrypted or removed_legacy

    def has_secret_setting(self, key: str) -> bool:
        """Whether the credential is configured via settings (either form).

        Backs the ``source: "settings" | "env"`` fields on the admin status
        endpoints. Deliberately presence-only — it never decrypts, so a row
        written under a rotated key still reports "settings", which is the
        honest answer to *where is this configured*.
        """
        from services.secret_settings import encrypted_key_for

        return bool(self.get_setting(encrypted_key_for(key)) or self.get_setting(key))

    def get_anthropic_api_key(self) -> str:
        """Get Anthropic API key: encrypted setting → legacy → env → ''."""
        return self._resolve_secret_setting('anthropic_api_key', 'ANTHROPIC_API_KEY')

    def get_github_pat(self) -> str:
        """Get GitHub PAT: encrypted setting → legacy → env → ''."""
        return self._resolve_secret_setting('github_pat', 'GITHUB_PAT')

    def get_google_api_key(self) -> str:
        """Get Google API key: encrypted setting → legacy → env → ''."""
        return self._resolve_secret_setting('google_api_key', 'GOOGLE_API_KEY')

    # =========================================================================
    # ElevenLabs / outbound-voice (TTS) settings (trinity-enterprise#117)
    # =========================================================================
    #
    # The key is stored AES-256-GCM encrypted (Invariant #12) under
    # ``elevenlabs_api_key_encrypted`` — unlike the plaintext anthropic/github/google
    # keys above — because it is a delivery secret with no env-only precedent to honor.
    # Resolution precedence: stored (encrypted) setting → ``ELEVENLABS_API_KEY`` env →
    # unavailable. Uncached (one SQLite read per call) for --workers 2 consistency, and
    # fail-open (any decrypt/read error falls back to env) so a bad row never 500s the
    # voice path or the feature-flags endpoint.

    _ELEVENLABS_KEY_SETTING = 'elevenlabs_api_key_encrypted'
    _DEFAULT_VOICE_SETTING = 'tts_default_voice_id'

    def get_elevenlabs_api_key(self) -> str:
        """Resolve the ElevenLabs API key: decrypted stored setting → env → ''."""
        envelope = self.get_setting(self._ELEVENLABS_KEY_SETTING)
        if envelope:
            try:
                from services.credential_encryption import CredentialEncryptionService
                decrypted = CredentialEncryptionService().decrypt(envelope)
                key = (decrypted.get('elevenlabs_api_key') or '').strip()
                if key:
                    return key
            except Exception as e:
                logger.error(f"Failed to decrypt ElevenLabs API key setting: {e}")
        return os.getenv('ELEVENLABS_API_KEY', '')

    def elevenlabs_key_source(self) -> str:
        """Report where the key resolves from, for the admin panel: override|env|none."""
        if self.get_setting(self._ELEVENLABS_KEY_SETTING):
            return 'override'
        if os.getenv('ELEVENLABS_API_KEY', '').strip():
            return 'env'
        return 'none'

    def set_elevenlabs_api_key(self, key: str) -> None:
        """Encrypt + persist the ElevenLabs API key (AES-256-GCM envelope)."""
        from services.credential_encryption import CredentialEncryptionService
        envelope = CredentialEncryptionService().encrypt({'elevenlabs_api_key': key.strip()})
        db.set_setting(self._ELEVENLABS_KEY_SETTING, envelope)

    def clear_elevenlabs_api_key(self) -> bool:
        """Remove the stored key (reverts to env/unavailable)."""
        return db.delete_setting(self._ELEVENLABS_KEY_SETTING)

    def get_default_voice_id(self) -> Optional[str]:
        """Platform default ElevenLabs voice id (plaintext setting; NULL when unset)."""
        value = self.get_setting(self._DEFAULT_VOICE_SETTING)
        return (value or '').strip() or None

    def set_default_voice_id(self, voice_id: str) -> None:
        db.set_setting(self._DEFAULT_VOICE_SETTING, voice_id.strip())

    def clear_default_voice_id(self) -> bool:
        return db.delete_setting(self._DEFAULT_VOICE_SETTING)

    # =========================================================================
    # Slack Integration Settings (SLACK-001)
    # =========================================================================

    def get_slack_client_id(self) -> str:
        """Get Slack Client ID from settings, fallback to env var.

        Deliberately NOT encrypted (ent#435): an OAuth client_id is a public
        identifier — ``slack_service.get_oauth_url`` puts it verbatim in the
        browser-visible authorize URL. Recorded as a reviewed exemption in
        ``secret_settings.PUBLIC_CREDENTIAL_SHAPED_KEYS``.
        """
        key = self.get_setting('slack_client_id')
        if key:
            return key
        return os.getenv('SLACK_CLIENT_ID', '')

    def get_slack_client_secret(self) -> str:
        """Get Slack Client Secret: encrypted setting → legacy → env (ent#435)."""
        return self._resolve_secret_setting('slack_client_secret', 'SLACK_CLIENT_SECRET')

    def get_slack_signing_secret(self) -> str:
        """Get Slack Signing Secret: encrypted setting → legacy → env (ent#435)."""
        return self._resolve_secret_setting('slack_signing_secret', 'SLACK_SIGNING_SECRET')

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
        """Slack App-Level Token (xapp-…) for Socket Mode: encrypted → legacy → env."""
        return self._resolve_secret_setting('slack_app_token', 'SLACK_APP_TOKEN')

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
    # Brain Orb feature flags (trinity-enterprise#58/#60/#61; admin-configurable #85)
    # =========================================================================

    def _resolve_bool_flag(self, setting_key: str, env_var: str, default: bool = False) -> bool:
        """Shared stored→env→default boolean flag resolution (#85).

        Resolves in this order:
        1. system_settings row `setting_key` ("true"/"false") — wins in BOTH
           directions, so an admin toggle overrides the env var until cleared
        2. `env_var` honored as OPT-IN ("true"/"1"/"yes") — existing deployments
           with the env flag keep working unchanged
        3. `default`

        Fail-open (the #506 `clamp_to_ceiling` discipline): a settings-read
        failure falls back to the env/default leg instead of raising — these
        resolvers feed GET /api/settings/feature-flags, where an exception
        would 500 the endpoint and zero EVERY flag in the frontend store.

        Deliberately NO TTL cache: the backend runs `--workers 2`; a cache
        would let a stale worker keep serving a just-flipped flag (#506
        rationale). One SQLite read per gate check is negligible at brain-orb
        QPS — don't "helpfully" add one.
        """
        try:
            stored = self.get_setting(setting_key)
        except Exception:
            stored = None
        if stored is not None:
            return str(stored).lower() in ("true", "1", "yes")
        env_val = os.getenv(env_var, "").strip().lower()
        if env_val in ("true", "1", "yes"):
            return True
        return default

    def is_brain_orb_enabled(self) -> bool:
        """Brain Orb base platform flag — gates the orb page, tab, and every
        proxied `/brain-orb/*` route (trinity-enterprise#58). Runtime-resolved
        so an admin toggle applies without a backend restart (#85)."""
        return self._resolve_bool_flag("brain_orb_enabled", "BRAIN_ORB_ENABLED")

    def is_brain_orb_voice_enabled(self) -> bool:
        """Brain Orb voice tile flag (trinity-enterprise#60). Voice additionally
        requires the base flag AND GEMINI_API_KEY (env-only secret) — composed
        at the consumers, not here."""
        return self._resolve_bool_flag("brain_orb_voice_enabled", "BRAIN_ORB_VOICE_ENABLED")

    def is_brain_orb_write_enabled(self) -> bool:
        """Brain Orb KB-write kill-switch (trinity-enterprise#61). Distinct from
        the base flag so the write/exec surface can be downed without downing
        read/voice. Write routes also require the base flag (composed at the
        router)."""
        return self._resolve_bool_flag("brain_orb_write_enabled", "BRAIN_ORB_WRITE_ENABLED")

    # =========================================================================
    # Workspace / portal session policy (ent#375)
    # =========================================================================

    def get_portal_session_policy(self) -> tuple:
        """`(idle_seconds, absolute_seconds)` for the sliding Workspace session.

        OSS owns the mechanism and these safe defaults; the entitled Settings
        panel owns the *setter* that writes the overrides — the core-primitive +
        enterprise-knob split `users.suspended_at` uses (#995). A community
        install slides on the defaults, it just cannot retune them.

        NO env leg, deliberately. `_resolve_bool_flag` has one because those
        flags predate their Settings surface; these keys are new, and an env leg
        would let a stale variable override a row an operator actually set — the
        #1638 "two sources for one policy" trap.

        Clamped on READ, not only on write. The entitled setter validates, but a
        direct DB write or a future default regression must not be able to mint a
        session that outlives its own cap. Read-side clamping is what makes
        `idle <= absolute` hold regardless of who wrote the row (#506
        clamp-on-use).

        Fail-safe: any read failure returns the code defaults rather than
        raising. This runs on the auth path — an exception here would 500 every
        Workspace request instead of degrading to the shipped policy.
        """
        from config import (
            PORTAL_SESSION_ABSOLUTE_DAYS_DEFAULT,
            PORTAL_SESSION_IDLE_DAYS_DEFAULT,
            PORTAL_SESSION_MAX_ABSOLUTE_DAYS,
            PORTAL_SESSION_MIN_IDLE_MINUTES,
        )

        def _read(key: str, default_days: float) -> float:
            try:
                raw = self.get_setting(key)
            except Exception:
                return float(default_days)
            if raw is None:
                return float(default_days)
            try:
                val = float(raw)
            except (TypeError, ValueError):
                return float(default_days)
            return val if val > 0 else float(default_days)

        idle_s = int(_read("portal_session_idle_days", PORTAL_SESSION_IDLE_DAYS_DEFAULT) * 86400)
        abs_s = int(_read("portal_session_absolute_days", PORTAL_SESSION_ABSOLUTE_DAYS_DEFAULT) * 86400)

        # Floors/ceilings first, then the ordering invariant. A cap shorter than
        # the idle window would kill every session at the cap while the idle
        # window claimed otherwise.
        idle_s = max(idle_s, PORTAL_SESSION_MIN_IDLE_MINUTES * 60)
        abs_s = min(abs_s, PORTAL_SESSION_MAX_ABSOLUTE_DAYS * 86400)
        abs_s = max(abs_s, idle_s)
        return idle_s, abs_s

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

    # =========================================================================
    # Remote Template Registry (TMPL-002, trinity-enterprise#14)
    # =========================================================================
    #
    # Four keys, all four blocked on the generic `PUT /api/settings/{key}` via
    # TEMPLATE_REGISTRY_KEYS: `url` because the SSRF gate would otherwise be one
    # unvalidated PUT away from bypass, `enabled` because it is a security
    # control, and `generation`/`lkg` because they ARE the cache — writing them
    # directly is cache poisoning with extra steps.

    def get_template_registry_url(self) -> str:
        """Effective registry URL: admin override row, else the config default.

        No cache. `--workers 2` (the #506 rationale) — one SQLite read per
        catalog assembly is negligible next to the HTTP fetch it gates, and a
        cached URL is exactly how one worker keeps fetching a repointed
        registry's old address.
        """
        from config import TEMPLATE_REGISTRY_URL

        stored = self.get_setting(TEMPLATE_REGISTRY_URL_KEY)
        if stored and str(stored).strip():
            return str(stored).strip()
        return TEMPLATE_REGISTRY_URL

    def is_template_registry_enabled(self) -> bool:
        """Composed switch: the config hard kill switch AND the admin toggle.

        Deliberately NOT `_resolve_bool_flag` — see the config.py comment. That
        helper treats its env var as opt-in only, so `default=True` would make
        `TEMPLATE_REGISTRY_ENABLED=false` inert (#1039 class). Here the hard
        switch is evaluated FIRST and no stored row can override it.

        Fail-open on a settings-read failure (the #506 `clamp_to_ceiling`
        discipline): the toggle defaults to on, so a transient DB error must not
        silently disable a working registry — and the fetch itself is fenced.
        """
        from config import TEMPLATE_REGISTRY_ENABLED

        if not TEMPLATE_REGISTRY_ENABLED:
            return False
        try:
            stored = self.get_setting(TEMPLATE_REGISTRY_ENABLED_KEY)
        except Exception:
            stored = None
        if stored is None:
            return True
        return str(stored).strip().lower() in ("1", "true", "yes", "on")

    def is_template_registry_hard_disabled(self) -> bool:
        """True when config alone has switched the registry off, so the panel
        can render the toggle as inert instead of pretending it works
        (the `TelemetrySharingPanel` `hard_disabled` shape)."""
        from config import TEMPLATE_REGISTRY_ENABLED

        return not TEMPLATE_REGISTRY_ENABLED

    def get_template_registry_generation(self) -> int:
        """Monotonic counter bumped on every registry settings write.

        This is the cross-worker cache invalidation primitive. A per-process
        `invalidate_registry_cache()` clears only the calling worker, so under
        `--workers 2` an admin who repoints the registry sees the change apply
        on roughly half their page loads — a nondeterministic setting, which is
        worse than a slow one. A cached entry stamped with a stale generation is
        discarded on read.

        Mandatory, not nice-to-have, once the registry TTL is an hour: the two
        decisions are coupled and must not be split. Fail-open to 0 — an
        unreadable counter must not disable the cache.
        """
        try:
            raw = self.get_setting(TEMPLATE_REGISTRY_GENERATION_KEY)
            return int(raw) if raw is not None else 0
        except Exception:  # noqa: BLE001 — unreadable counter must not disable the cache
            return 0

    def bump_template_registry_generation(self) -> int:
        """Invalidate every worker's registry cache. Called on every write."""
        nxt = self.get_template_registry_generation() + 1
        db.set_setting(TEMPLATE_REGISTRY_GENERATION_KEY, str(nxt))
        return nxt

    def set_template_registry_config(
        self, *, url: Optional[str] = None, enabled: Optional[bool] = None
    ) -> None:
        """Partial update. An omitted field is left untouched (the
        `/api/settings/skills-library` shape). Always bumps the generation."""
        if url is not None:
            db.set_setting(TEMPLATE_REGISTRY_URL_KEY, url)
        if enabled is not None:
            db.set_setting(
                TEMPLATE_REGISTRY_ENABLED_KEY, "true" if enabled else "false"
            )
        self.bump_template_registry_generation()

    def delete_template_registry_config(self) -> bool:
        """Reset to the config defaults.

        Also drops the durable last-known-good: it was captured under the
        overridden URL, and serving it after a reset would attribute one
        registry's catalog to another.
        """
        removed_url = db.delete_setting(TEMPLATE_REGISTRY_URL_KEY)
        removed_enabled = db.delete_setting(TEMPLATE_REGISTRY_ENABLED_KEY)
        db.delete_setting(TEMPLATE_REGISTRY_LKG_KEY)
        self.bump_template_registry_generation()
        return bool(removed_url or removed_enabled)

    def get_template_registry_lkg(self) -> Optional[dict]:
        """Durable last-known-good parse, or None.

        Never raises: a corrupt row is indistinguishable from no row, which is
        the correct degradation — the caller falls back to the bundled floor.
        """
        raw = None
        try:
            raw = self.get_setting(TEMPLATE_REGISTRY_LKG_KEY)
            if raw is None:
                return None
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            logger.warning(
                "[template-registry] stored last-known-good is unreadable; ignoring it"
            )
            return None

    def set_template_registry_lkg(self, payload: dict) -> None:
        """Persist the sanitized PARSED registry (never the raw YAML).

        Storing raw YAML would mean re-parsing an untrusted document out of our
        own database on a path where the network guards no longer apply.
        """
        db.set_setting(TEMPLATE_REGISTRY_LKG_KEY, json.dumps(payload))

    def clear_template_registry_lkg(self) -> None:
        db.delete_setting(TEMPLATE_REGISTRY_LKG_KEY)

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


def get_github_pat_for_agent(agent_name: str) -> str:
    """Resolve a GitHub PAT for an agent: per-agent PAT → global.

    This is the **2-tier** ladder used by the recreate/restart env-rebuild path
    (services/agent_service/lifecycle.py, helpers.check_github_pat_env_matches).
    It deliberately does NOT consult the per-user tier (ent#162): re-deriving a
    live per-user PAT here would make ``check_github_pat_env_matches`` reactive,
    so adding/rotating a personal PAT in Settings would force-recreate the
    owner's running agents and kill in-flight work. The per-user tier is a
    create-time input only — see ``resolve_github_pat`` below.

    Relocated from ``routers/git.py`` so services stop importing a router
    (Invariant #1); ``routers/git.py`` re-exports it for backward compatibility.
    """
    agent_pat = db.get_agent_github_pat(agent_name)
    if agent_pat:
        return agent_pat
    return get_github_pat()


def resolve_github_pat(agent_name: Optional[str] = None,
                       owner_id: Optional[int] = None) -> tuple:
    """Resolve a GitHub PAT with tier provenance, for the agent-CREATE path (ent#162).

    Returns ``(pat, tier)`` where ``tier`` is one of:
      - ``"per_agent"`` — the agent already has its own PAT (explicit override)
      - ``"per_user"``  — the owner's personal PAT, read **live** by ``owner_id``
      - ``"global"``    — the admin-set / env global PAT
      - ``"none"``      — nothing configured (``pat`` is ``""``)

    The tier is what the create path keys its persist decision on: persist the
    resolved value as the agent's #347 per-agent PAT for ``per_agent``/``per_user``,
    but **NEVER** for ``global``. A global-fallback agent must keep
    ``github_pat_encrypted`` NULL so ``github_pat_propagation_service`` continues
    to reach it on admin global-PAT rotation (ent#162 Decision 2).

    ``owner_id`` is the agent owner's user id — resolution keys on ownership only,
    never on a calling/sharing user, so a sharee can never inject their PAT as the
    agent's git identity.
    """
    if agent_name:
        agent_pat = db.get_agent_github_pat(agent_name)
        if agent_pat:
            return agent_pat, "per_agent"
    if owner_id is not None:
        user_pat = db.get_user_github_pat(owner_id)
        if user_pat:
            return user_pat, "per_user"
    global_pat = get_github_pat()
    if global_pat:
        return global_pat, "global"
    return "", "none"


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


# --- Credential-bearing settings (ent#435) ---------------------------------

def set_secret_setting(key: str, value: str) -> None:
    """Persist a credential-bearing setting AES-256-GCM encrypted."""
    settings_service.set_secret_setting(key, value)


def clear_secret_setting(key: str) -> bool:
    """Remove a credential-bearing setting (encrypted + any legacy row)."""
    return settings_service.clear_secret_setting(key)


def has_secret_setting(key: str) -> bool:
    """Whether a credential-bearing setting is configured in the DB."""
    return settings_service.has_secret_setting(key)


def is_session_tab_enabled() -> bool:
    """Session tab feature flag (Phase 1.6 of SESSION_TAB_2026-04)."""
    return settings_service.is_session_tab_enabled()


# Brain Orb flags (trinity-enterprise#85) — the (setting_key, env_var) registry
# shared by the resolvers above and the dedicated admin routes in
# routers/settings.py (GET/PUT /api/settings/brain-orb). Field names match the
# BrainOrbSettingsUpdate model.
BRAIN_ORB_FLAGS = {
    "enabled": ("brain_orb_enabled", "BRAIN_ORB_ENABLED"),
    "voice_enabled": ("brain_orb_voice_enabled", "BRAIN_ORB_VOICE_ENABLED"),
    "write_enabled": ("brain_orb_write_enabled", "BRAIN_ORB_WRITE_ENABLED"),
}


def is_brain_orb_enabled() -> bool:
    """Brain Orb base platform flag — runtime-resolved (#85)."""
    return settings_service.is_brain_orb_enabled()


def is_brain_orb_voice_enabled() -> bool:
    """Brain Orb voice tile flag — runtime-resolved (#85)."""
    return settings_service.is_brain_orb_voice_enabled()


def is_brain_orb_write_enabled() -> bool:
    """Brain Orb KB-write kill-switch — runtime-resolved (#85)."""
    return settings_service.is_brain_orb_write_enabled()


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

# trinity-enterprise#69 — ephemeral "ghost" agent limits. Separate from the
# durable per-role quota (burst parallelism is the use case; the durable quota
# would starve it). NO admin exemption — the spawner is usually an agent-scoped
# key that resolves to its owner (often an admin), and this quota exists to
# bound runaway spawning, not to police humans.
EPHEMERAL_AGENT_DEFAULTS = {
    "max_ephemeral_agents_per_owner": "5",
    "ephemeral_ttl_ceiling_seconds": "86400",  # 24h
}


def get_ephemeral_agent_quota() -> int:
    """Max live ephemeral agents per owner (0 = unlimited, default 5)."""
    value = settings_service.get_setting("max_ephemeral_agents_per_owner")
    if value is not None:
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    return int(EPHEMERAL_AGENT_DEFAULTS["max_ephemeral_agents_per_owner"])


def get_ephemeral_ttl_ceiling_seconds() -> int:
    """Hard TTL ceiling for ephemeral agents (default 24h).

    Every ghost gets ``ephemeral_expires_at`` stamped at creation — when the
    caller gives only ``max_executions``, the TTL defaults to this ceiling so
    no ghost is immortal. A requested TTL above the ceiling is a 400.
    """
    value = settings_service.get_setting("ephemeral_ttl_ceiling_seconds")
    if value is not None:
        try:
            ceiling = int(value)
            if ceiling > 0:
                return ceiling
        except (TypeError, ValueError):
            pass
    return int(EPHEMERAL_AGENT_DEFAULTS["ephemeral_ttl_ceiling_seconds"])


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


# ---------------------------------------------------------------------------
# Library lifecycle automation (trinity-enterprise#236)
# ---------------------------------------------------------------------------
#
# All three default to the pre-#236 behavior (no scheduled sync, no fleet
# sweep), so a zero-config install is byte-identical to before. Resolved fresh
# on every read — the loop re-reads each cycle, which is what lets an admin
# change the interval or flip a flag without a backend restart, and matches the
# #506 "no per-process cache under --workers 2" discipline.

SKILLS_AUTO_SYNC_ENABLED_KEY = "skills_library_auto_sync_enabled"
SKILLS_AUTO_SYNC_INTERVAL_KEY = "skills_library_auto_sync_interval_seconds"
SKILLS_AUTO_REINJECT_ENABLED_KEY = "skills_library_auto_reinject_enabled"

SKILLS_AUTO_SYNC_INTERVAL_DEFAULT = 3600
# Floor is a real guard, not decoration: each cycle forks `git fetch` against
# GitHub for the whole install, so a 10-second interval is a self-inflicted
# rate-limit ban. Ceiling keeps "enabled" meaningful.
SKILLS_AUTO_SYNC_INTERVAL_MIN = 300
SKILLS_AUTO_SYNC_INTERVAL_MAX = 86400


def is_skills_auto_sync_enabled() -> bool:
    """Scheduled skills-library auto-sync flag (ent#236). Default OFF."""
    return settings_service._resolve_bool_flag(
        SKILLS_AUTO_SYNC_ENABLED_KEY, "SKILLS_LIBRARY_AUTO_SYNC_ENABLED", False
    )


def is_skills_auto_reinject_enabled() -> bool:
    """Fleet-wide re-inject-after-sync flag (ent#236). Default OFF."""
    return settings_service._resolve_bool_flag(
        SKILLS_AUTO_REINJECT_ENABLED_KEY, "SKILLS_LIBRARY_AUTO_REINJECT_ENABLED", False
    )


def get_skills_auto_sync_interval() -> int:
    """Auto-sync interval in seconds, clamped into [MIN, MAX] (ent#236).

    Read-side clamping (the #506 `clamp_to_ceiling` shape) so a stray value
    written straight to the DB — or by a future unvalidated path — cannot spin
    the loop into a tight fetch flood or park it past a day. Fail-safe on a bad
    row: fall back to the default rather than raising inside the loop.
    """
    raw = settings_service.get_setting(SKILLS_AUTO_SYNC_INTERVAL_KEY)
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError, AttributeError):
        return SKILLS_AUTO_SYNC_INTERVAL_DEFAULT
    return max(
        SKILLS_AUTO_SYNC_INTERVAL_MIN, min(SKILLS_AUTO_SYNC_INTERVAL_MAX, value)
    )


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


# ============================================================================
# Proactive channel-message rate limits (#1609)
# ============================================================================
#
# Admin-tunable anti-spam caps on agent-INITIATED ("proactive") sends —
# introduced hardcoded by #349/#350/#321. Inbound replies (DM/@mention/thread
# via the channel adapters) are NEVER gated by these. All share a fixed 1-hour
# window (as shipped). ``0`` = unlimited (disabled), matching the agent-quota
# convention; the PUT warns when a cap is disabled. Runtime-resolved (no cache,
# ``--workers 2``-consistent, no migration — same rationale as #506).

PROACTIVE_RATE_LIMIT_WINDOW_SECONDS = 3600  # 1 hour, fixed (matches #349/#350/#321)
# key → shipped default (the pre-#1609 hardcoded value). Defaults reproduce
# current behavior exactly.
PROACTIVE_RATE_LIMIT_DEFAULTS = {
    "slack_proactive_per_channel": 10,
    "slack_proactive_per_agent": 100,
    "telegram_proactive_per_group": 10,
    "telegram_proactive_per_agent": 100,
    "proactive_dm_per_recipient": 10,
}
PROACTIVE_RATE_LIMIT_DESCRIPTIONS = {
    "slack_proactive_per_channel": "Max proactive Slack messages per hour to a single channel (0 = unlimited)",
    "slack_proactive_per_agent": "Max proactive Slack messages per hour across all channels for one agent (0 = unlimited)",
    "telegram_proactive_per_group": "Max proactive Telegram messages per hour to a single group (0 = unlimited)",
    "telegram_proactive_per_agent": "Max proactive Telegram messages per hour across all groups for one agent (0 = unlimited)",
    "proactive_dm_per_recipient": "Max proactive direct messages per hour to a single recipient (0 = unlimited)",
}
PROACTIVE_RATE_LIMIT_MAX = 1_000_000  # sanity upper bound


def get_proactive_rate_limit(key: str) -> int:
    """Fail-open read-through for a #1609 proactive cap (per hour).

    Returns the configured integer (``0`` = unlimited → callers skip the limiter),
    the shipped default on an absent/garbage value, clamped into ``[0, MAX]`` so
    a stray store value can neither fail-closed the channel nor overflow. No
    per-process cache (``--workers 2`` consistency), fail-open on a settings-read
    failure so a proactive send is never blocked by a DB hiccup.
    """
    default = PROACTIVE_RATE_LIMIT_DEFAULTS.get(key, 0)
    try:
        raw = settings_service.get_setting(key)
    except Exception:
        return default
    if raw is None:
        return default
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return default
    if parsed < 0:
        return default
    return min(parsed, PROACTIVE_RATE_LIMIT_MAX)


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


# Remote Template Registry (TMPL-002, trinity-enterprise#14)
def get_template_registry_url() -> str:
    """Effective registry URL — admin override row, else the config default."""
    return settings_service.get_template_registry_url()


def is_template_registry_enabled() -> bool:
    """Config hard switch AND the admin toggle (default on when unset)."""
    return settings_service.is_template_registry_enabled()


def get_platform_default_model() -> str:
    """Return the platform-wide default Claude model (#831)."""
    return settings_service.get_platform_default_model()
