"""
Configuration constants for the Trinity backend.
"""
import os
from urllib.parse import urlparse

# Email Authentication Mode (Phase 12.4)
# Set EMAIL_AUTH_ENABLED=true to enable email-based login with verification codes
# This is the default authentication method. Users enter email → receive code → login
# Can also be set via system_settings table (key: "email_auth_enabled", value: "true"/"false")
EMAIL_AUTH_ENABLED = os.getenv("EMAIL_AUTH_ENABLED", "true").lower() == "true"

# Public self-signup gate (trinity-enterprise#10). When OFF (the secure
# default), the unauthenticated POST /api/access/request endpoint returns 403 —
# it does NOT auto-whitelist arbitrary emails. Operators who want frictionless
# CLI onboarding (`trinity:connect`) opt in explicitly via this env var or the
# system_settings key "public_access_requests_enabled". Does not affect login
# code requests for already-whitelisted emails (separate endpoint).
PUBLIC_ACCESS_REQUESTS_ENABLED = os.getenv("PUBLIC_ACCESS_REQUESTS_ENABLED", "false").lower() == "true"

# Operator intake (trinity-enterprise#38). At first-run setup the operator may
# opt in to "occasionally receive important security & product updates"; when
# they do, their email + company are submitted once to an Ability.ai-operated
# hosted intake endpoint (a sibling endpoint on the same Cloudflare-fronted
# intake app as the #1116 in-app bug reporter). This is identifiable, explicit
# opt-in contact capture — NOT anonymous telemetry — so it only fires on an
# affirmative consent checkbox. Fire-and-forget and once-per-install: a blocked
# or failed POST never delays or breaks setup.
#
# OPERATOR_INTAKE_ENABLED=false (or the cross-tool DO_NOT_TRACK=1 convention)
# fully disables the outbound submission for air-gapped / privacy-strict
# deployments — the consent box still appears but nothing ever leaves the box.
OPERATOR_INTAKE_ENABLED = (
    os.getenv("OPERATOR_INTAKE_ENABLED", "true").lower() == "true"
    and os.getenv("DO_NOT_TRACK", "0") not in ("1", "true", "True")
)
# Stable Cloudflare-fronted vanity domain (same app as #1116's /v1/report-bug);
# /v1/ versions the contract so the backing Worker can be replaced forever.
OPERATOR_INTAKE_URL = os.getenv(
    "OPERATOR_INTAKE_URL", "https://intake.abilityai.dev/v1/operator-intake"
)

# JWT Settings
# SECURITY: SECRET_KEY must be set via environment variable in production
# Generate with: openssl rand -hex 32
_secret_key = os.getenv("SECRET_KEY", "")
if not _secret_key:
    import secrets
    _secret_key = secrets.token_hex(32)
    print("WARNING: SECRET_KEY not set - generated random key for this session")
    print("         For production, set SECRET_KEY environment variable")
elif _secret_key == "your-secret-key-change-in-production":
    print("CRITICAL: Default SECRET_KEY detected - change immediately for production!")
    print("         Generate with: openssl rand -hex 32")
SECRET_KEY = _secret_key
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 10080  # 7 days (was 30 minutes)

# Redis URL — must include credentials (Issue #589).
# docker-compose builds the URL with the `backend` ACL user + REDIS_BACKEND_PASSWORD;
# we only validate it here. Splicing fallback removed: a single source of truth
# avoids silent drift between compose env and Python config.
REDIS_URL = os.getenv("REDIS_URL", "")
_redis_parsed = urlparse(REDIS_URL) if REDIS_URL else None
if not REDIS_URL or not _redis_parsed or not _redis_parsed.username or not _redis_parsed.password:
    raise RuntimeError(
        "REDIS_URL must include credentials (redis://user:password@host:port). "
        "Generate passwords with: openssl rand -hex 24. "
        "See docs/migrations/REDIS_AUTH.md for details."
    )
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")
FRONTEND_URL = os.getenv("FRONTEND_URL", "")  # Set in .env or docker-compose for OAuth redirects

# External URL for public chat links (Tailscale Funnel, Cloudflare Tunnel, etc.)
# When set, enables "Copy External Link" button in PublicLinksPanel
PUBLIC_CHAT_URL = os.getenv("PUBLIC_CHAT_URL", "")

# Email Service Configuration (for public link verification)
EMAIL_PROVIDER = os.getenv("EMAIL_PROVIDER", "resend")  # "console", "smtp", "sendgrid", "resend"
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "noreply@trinity.example.com")
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY", "")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")

# Slack Integration Configuration (SLACK-001)
# Required only if Slack integration is enabled on any public link
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")
SLACK_CLIENT_ID = os.getenv("SLACK_CLIENT_ID", "")
SLACK_CLIENT_SECRET = os.getenv("SLACK_CLIENT_SECRET", "")
SLACK_AUTO_VERIFY_EMAIL = os.getenv("SLACK_AUTO_VERIFY_EMAIL", "true").lower() == "true"

# GitHub PAT for template cloning (auto-uploaded to Redis on startup)
GITHUB_PAT = os.getenv("GITHUB_PAT", "")
GITHUB_PAT_CREDENTIAL_ID = "github-pat-templates"  # Fixed ID for consistent reference

# OAuth Provider Configs
OAUTH_CONFIGS = {
    "google": {
        "client_id": os.getenv("GOOGLE_CLIENT_ID", ""),
        "client_secret": os.getenv("GOOGLE_CLIENT_SECRET", ""),
    },
    "slack": {
        "client_id": os.getenv("SLACK_CLIENT_ID", ""),
        "client_secret": os.getenv("SLACK_CLIENT_SECRET", ""),
    },
    "github": {
        "client_id": os.getenv("GITHUB_CLIENT_ID", ""),
        "client_secret": os.getenv("GITHUB_CLIENT_SECRET", ""),
    },
    "notion": {
        "client_id": os.getenv("NOTION_CLIENT_ID", ""),
        "client_secret": os.getenv("NOTION_CLIENT_SECRET", ""),
    }
}

# CORS Origins
# Add your production domains to EXTRA_CORS_ORIGINS environment variable (comma-separated)
_extra_origins = os.getenv("EXTRA_CORS_ORIGINS", "").split(",")
_extra_origins = [o.strip() for o in _extra_origins if o.strip()]

# Automatically add PUBLIC_CHAT_URL to CORS if set
if PUBLIC_CHAT_URL:
    _extra_origins.append(PUBLIC_CHAT_URL)

CORS_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:8080",
] + _extra_origins

# Google Gemini API Key (for platform image generation - IMG-001, voice chat - VOICE-001)
# Falls back to GOOGLE_API_KEY (used for Gemini-powered agents) if GEMINI_API_KEY not set
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "") or os.getenv("GOOGLE_API_KEY", "")

# Dispatch Circuit Breaker — global master switch (RELIABILITY-007, #526).
# Producer-side per-agent breaker that fast-fails NEW executions (HTTP 503)
# when an agent is auth-dead, instead of poisoning the persistent backlog.
# Default OFF: this is the global gate; per-agent opt-in lives in
# agent_ownership.circuit_breaker_enabled (also default OFF). Both must be on
# for the breaker to engage — a true opt-in canary (D7/D11).
DISPATCH_BREAKER_ENABLED = os.getenv("DISPATCH_BREAKER_ENABLED", "false").lower() == "true"

# Fire-and-Forget Dispatch — global master switch (#1083).
# When ON, eligible autonomous turns are dispatched to the agent with a 202
# accept and finalized via the result-callback endpoint, so a wedged turn
# holds zero backend coroutine/slot beyond its lease. Default OFF; flipping
# early is safe because a non-202 agent response (old image / non-Claude
# runtime) falls back to today's synchronous handling.
DISPATCH_ASYNC = os.getenv("DISPATCH_ASYNC", "false").lower() == "true"

# Triggers eligible for async dispatch (#1083 v1). ONLY {schedule, webhook}:
# these reach execute_task through the scheduler's async-poll path with no
# synchronous result consumer. `loop`/`fan_out` consume result.response and
# MUST stay sync; `event` POSTs the agent directly (bypassing execute_task).
ASYNC_DISPATCH_ELIGIBLE_TRIGGERS = frozenset({"schedule", "webhook"})

# Pull-pilot routing for agent→agent MCP chat (#946, Phase 2 PoC for Epic
# #1045 / umbrella #1081). When ON, an agent→agent (scope='agent', non-self)
# `chat_with_agent` sequential call is routed by the MCP server through the
# durable async `/task` path instead of the synchronous held `/chat` call;
# the caller receives an immediate `{accepted|queued, execution_id}` receipt
# and polls `get_execution_result`. scope='user', self-tasks, and flag-OFF keep
# today's synchronous `/chat` unchanged. Default OFF — a flag flip / MCP routing
# revert is the whole rollback. The actual routing gate lives MCP-side
# (`MCP_AGENT_CHAT_PULL_ENABLED` read in the MCP server at startup, mirroring
# `MCP_REQUIRE_API_KEY`); this backend declaration is the canonical registry
# entry and is surfaced via GET /api/settings/feature-flags
# (`mcp_agent_chat_pull_enabled`) for operator observability during the soak.
# Both services read the SAME env key, so a normal single-`.env` deployment
# can't drift.
MCP_AGENT_CHAT_PULL_ENABLED = os.getenv("MCP_AGENT_CHAT_PULL_ENABLED", "false").lower() == "true"

# Correlated-Failure / Thundering-Herd Controls (#1085) — re-delivery governor.
# These guard the live #1083 fire-and-forget callback path (and, unchanged, the
# future pull-mode re-delivery path) against a fleet-wide retry storm: a backend
# restart re-sends ~N persisted terminal envelopes plus in-flight callback
# retries, all hammering POST /api/agents/{name}/executions/{id}/result.
#
# REDELIVERY_GOVERNOR_ENABLED is the single master switch for the BACKEND
# controls (re-delivery rate caps + shared-cause pause reads). Default OFF — the
# governor is inert until flipped, and a flip back is the whole rollback.
# Agent-side jitter (Part A) is behaviorally safe and ships UNFLAGGED.
# Everything here is fail-open: a Redis blip degrades to allow/no-op, never to
# blocking or dropping a terminal.
REDELIVERY_GOVERNOR_ENABLED = os.getenv("REDELIVERY_GOVERNOR_ENABLED", "false").lower() == "true"

# Fleet-wide re-delivery cap (~10/s default) — bounds total callback admissions
# across all agents over a rolling window.
REDELIVERY_FLEET_LIMIT = int(os.getenv("REDELIVERY_FLEET_LIMIT", "600"))
REDELIVERY_FLEET_WINDOW_SECONDS = int(os.getenv("REDELIVERY_FLEET_WINDOW_SECONDS", "60"))

# Per-agent re-delivery cap — bounds one agent's callback admissions so a single
# crash-looping agent can't exhaust the fleet budget.
REDELIVERY_AGENT_LIMIT = int(os.getenv("REDELIVERY_AGENT_LIMIT", "20"))
REDELIVERY_AGENT_WINDOW_SECONDS = int(os.getenv("REDELIVERY_AGENT_WINDOW_SECONDS", "60"))

# Shared-cause detector: when this many DISTINCT agents post an AUTH/BILLING
# terminal within the rolling window, a fleet-wide cause is inferred (Claude API
# outage, expired platform key, a bad skill pushed fleet-wide) and re-delivery is
# paused for the whole fleet.
CORRELATED_FAILURE_THRESHOLD = int(os.getenv("CORRELATED_FAILURE_THRESHOLD", "20"))
CORRELATED_FAILURE_WINDOW_SECONDS = int(os.getenv("CORRELATED_FAILURE_WINDOW_SECONDS", "120"))

# Pause flag TTL — the pause auto-expires (no explicit unpause, so there is no
# stuck-pause failure mode). Kept well under the lease window (timeout +
# SLOT_TTL_BUFFER, buffer=300) so a held row is never failed during the pause.
CORRELATED_PAUSE_TTL_SECONDS = int(os.getenv("CORRELATED_PAUSE_TTL_SECONDS", "300"))

# Retry-After hint (seconds) returned on a 503 while paused/throttled — jittered
# at the callsite so throttled callbacks don't realign on the same backoff edge.
REDELIVERY_PAUSE_RETRY_AFTER_SECONDS = int(os.getenv("REDELIVERY_PAUSE_RETRY_AFTER_SECONDS", "30"))

# Voice Chat Configuration (VOICE-001)
VOICE_ENABLED = os.getenv("VOICE_ENABLED", "true").lower() == "true"
# Coalesce empty → default (#1076): os.getenv(name, default) returns the
# default only when the var is UNSET, not when it is set-but-empty. A blank
# VOICE_MODEL (a stray `.env` line, a manual export, or an older compose that
# injected `${VOICE_MODEL:-}`) would otherwise shadow the default and send
# model="" to Gemini Live ("model is required" → every voice path DOA). `or`
# defends against an empty value from any source. This line is the authoritative
# source of the default model id — keep compose/.env.example in agreement.
# (mirrors the GEMINI_API_KEY `or` coalesce above.)
VOICE_MODEL = os.getenv("VOICE_MODEL") or "models/gemini-3.1-flash-live-preview"
VOICE_MAX_DURATION = int(os.getenv("VOICE_MAX_DURATION", "300"))  # seconds

# Per-agent voice selection (#28). Canonical set of Gemini Live prebuilt voices
# offered by Trinity; the single source of truth shared by the persisted-voice
# write validation and the read-path fallback (and mirrored by the frontend
# picker). DEFAULT_VOICE_NAME is the historical hardcoded default and the
# fallback for an unset or no-longer-valid persisted value.
DEFAULT_VOICE_NAME = "Kore"
GEMINI_VOICE_NAMES = ("Kore", "Zephyr", "Puck", "Aoede", "Charon", "Fenrir", "Gacrux")

# Outbound voice messages across channels (epic #24). Shared TTS layer used by
# the channel adapters (Telegram #25 first) to speak agent replies. ElevenLabs is
# the provider; the key gates the whole feature (empty ⇒ voice-out unavailable,
# adapters fall back to text). TTS_MAX_CHARS is the shared cost guardrail — a
# reply longer than this is delivered as text instead of paying to synthesize it.
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_MODEL_ID = os.getenv("ELEVENLABS_MODEL_ID") or "eleven_multilingual_v2"
TTS_MAX_CHARS = int(os.getenv("TTS_MAX_CHARS", "1500"))

# Gemini text/audio models (#1130). Hardcoded `gemini-2.0-flash` was retired by
# Google (404 NOT_FOUND) with no config escape hatch — these env overrides make
# the next model retirement a config change instead of a code change. Same `or`
# coalesce as VOICE_MODEL above (#1076): empty string must not shadow the default.
# Two separate vars because the modalities can diverge: TEXT is text-only
# (image-gen prompt refinement), TRANSCRIPTION needs inline-audio support
# (Telegram voice messages). Both default to the same model today.
GEMINI_TEXT_MODEL = os.getenv("GEMINI_TEXT_MODEL") or "gemini-3.5-flash"
GEMINI_TRANSCRIPTION_MODEL = os.getenv("GEMINI_TRANSCRIPTION_MODEL") or "gemini-3.5-flash"

# VoIP Telephony Configuration (VOIP-001, #1056 — Phase 1, outbound)
# Default OFF — mirrors the workspace_available opt-in (#860). The feature
# also requires a per-agent voip_bindings row to function. `voip_available`
# in GET /api/settings/feature-flags is `VOIP_ENABLED and bool(GEMINI_API_KEY)`.
VOIP_ENABLED = os.getenv("VOIP_ENABLED", "false").lower() == "true"
# Brain Orb flags (trinity-enterprise#58/#60/#61) are RUNTIME-RESOLVED as of #85
# — no import-time constants here, so a stale module value can never shadow an
# admin toggle. Resolution lives in services/settings_service.py
# (is_brain_orb_enabled / is_brain_orb_voice_enabled / is_brain_orb_write_enabled):
# system_settings override → BRAIN_ORB_ENABLED / BRAIN_ORB_VOICE_ENABLED /
# BRAIN_ORB_WRITE_ENABLED env opt-in → default OFF. Admin surface:
# GET/PUT /api/settings/brain-orb.
# VoIP-specific max call duration (seconds) — deliberately distinct from the
# inherited 300s VOICE_MAX_DURATION so phone calls aren't silently cut at 5min.
VOIP_MAX_CALL_DURATION = int(os.getenv("VOIP_MAX_CALL_DURATION", "600"))
# Durable per-agent daily call cap (overridable per binding). Bounds PSTN spend.
VOIP_DEFAULT_DAILY_CALL_CAP = int(os.getenv("VOIP_DEFAULT_DAILY_CALL_CAP", "50"))
# WSS ticket TTL for the Twilio Media Streams socket — wide enough to cover
# PSTN dial + ring (the 30s browser default is too short, call setup > 30s).
VOIP_TICKET_TTL_SECONDS = int(os.getenv("VOIP_TICKET_TTL_SECONDS", "180"))
# Redis staged-intent TTL (seconds) — consumed at WS-connect, sized for ringing.
VOIP_INTENT_TTL_SECONDS = int(os.getenv("VOIP_INTENT_TTL_SECONDS", "180"))
# Outbound-call trigger rate limit (per owner+destination sliding window).
VOIP_CALL_RATE_LIMIT = int(os.getenv("VOIP_CALL_RATE_LIMIT", "5"))
VOIP_CALL_RATE_WINDOW = int(os.getenv("VOIP_CALL_RATE_WINDOW", "60"))  # seconds

# Default GitHub Template Repositories
# Just repo identifiers — metadata is fetched from each repo's template.yaml at runtime.
# Admins can override this list via Settings → GitHub Templates (stored in system_settings).
DEFAULT_GITHUB_TEMPLATE_REPOS = [
    "abilityai/agent-ruby",
    "abilityai/agent-cornelius",
    "abilityai/agent-corbin",
    "abilityai/ruby-orchestrator",
    "abilityai/ruby-content",
    "abilityai/ruby-engagement",
]
