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
    and os.getenv("DO_NOT_TRACK", "0").strip().lower() in ("0", "", "false")
)
# Stable Cloudflare-fronted vanity domain (same app as #1116's /v1/report-bug);
# /v1/ versions the contract so the backing Worker can be replaced forever.
OPERATOR_INTAKE_URL = os.getenv(
    "OPERATOR_INTAKE_URL", "https://intake.abilityai.dev/v1/operator-intake"
)

# --- Tier-2 telemetry sharing (ent#12) -----------------------------------
# The opt-IN fleet-sharing channel (Tier-2) on top of the Tier-1 local capture
# (ent#184). Egress NEVER fires unless the operator explicitly opted in (the
# `telemetry_sharing_enabled` system-setting, default-off) AND this hard
# off-switch is on. `TELEMETRY_SHARING_ENABLED=false` (or the cross-tool
# `DO_NOT_TRACK=1`) is an operator/air-gapped kill switch that disables egress
# regardless of any stored consent — the toggle still appears, nothing leaves.
TELEMETRY_SHARING_ENABLED = (
    os.getenv("TELEMETRY_SHARING_ENABLED", "true").lower() == "true"
    and os.getenv("DO_NOT_TRACK", "0").strip().lower() in ("0", "", "false")
)
# Same Cloudflare-fronted hosted-intake app as operator intake; a sibling
# versioned path. The hosted aggregation/benchmark service is a separate issue.
TELEMETRY_SHARING_URL = os.getenv(
    "TELEMETRY_SHARING_URL", "https://intake.abilityai.dev/v1/telemetry-share"
)
# Periodic share cadence (hours) + default backfill window offered at consent.
TELEMETRY_SHARING_INTERVAL_HOURS = int(os.getenv("TELEMETRY_SHARING_INTERVAL_HOURS", "24"))
TELEMETRY_SHARING_BACKFILL_DEFAULT_DAYS = int(
    os.getenv("TELEMETRY_SHARING_BACKFILL_DEFAULT_DAYS", "30")
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

# --- Workspace / portal session policy (ent#375) ---------------------------
#
# The Workspace session SLIDES: it renews while in use, dies on inactivity, and
# is still bounded by an absolute cap no amount of use can extend. Replaces the
# flat 12-hour absolute lifetime, which made anyone using the surface on two
# consecutive days redo the email OTP.
#
# Defaults are the shipped policy for every install; the entitled Settings panel
# writes overrides into `system_settings`, read (and clamped) by
# `settings_service.get_portal_session_policy`.
#
# NOT env-configurable, deliberately: an env leg would give one policy two
# sources, and the stale one wins silently (#1638).
PORTAL_SESSION_IDLE_DAYS_DEFAULT = 7      # no requests for this long -> sign in again
PORTAL_SESSION_ABSOLUTE_DAYS_DEFAULT = 30  # hard ceiling from first sign-in

# #2157: the `schedule_executions.source_channel` stamp identifying a Workspace
# turn. `triggered_by="public"` is shared with public links and x402 chat, so it
# cannot answer "did this turn arrive from the Workspace?" — and that answer
# decides what `send_voice_reply` tells an agent that tries to speak there. It is
# NOT a messaging channel: portal rows carry no `source_channel_chat_id`, so every
# channel consumer (the completion-report resolver map, `voice_reply_service`'s
# supported set) already ignores it. It lives here so the writer (client_portal)
# and the reader (routers/agents) share one spelling without importing each other.
PORTAL_SOURCE_CHANNEL = "portal"

# Bounds enforced on READ, so a bad row cannot widen the window (#506).
PORTAL_SESSION_MIN_IDLE_MINUTES = 15
PORTAL_SESSION_MAX_ABSOLUTE_DAYS = 90

# Re-mint only once a session is this far into its idle window. Rotation revokes
# the previous `jti`, so rotating on EVERY request would make a browser with two
# in-flight requests race: the second arrives bearing a token the first just
# revoked. A staleness threshold keeps rotations rare and the race window small;
# `PORTAL_SESSION_ROTATION_GRACE_SECONDS` then keeps the superseded token alive
# just long enough for requests already in flight.
PORTAL_SESSION_ROTATE_AFTER_FRACTION = 0.5
PORTAL_SESSION_ROTATION_GRACE_SECONDS = 60

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

# MCP inline email auth (#848). Same env key the mcp-server reads, so a
# single-.env deploy cannot drift between the two halves. Default OFF: with it
# off the whole /api/internal/mcp-auth surface 404s, so the backend does not
# depend on the MCP server's own gate for its default-OFF posture. Gating BOTH
# halves matters because this surface bypasses the email whitelist and creates
# accounts — it must not be live on an install that never opted in.
MCP_INLINE_AUTH_ENABLED = os.getenv("MCP_INLINE_AUTH_ENABLED", "false").lower() == "true"

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

# Pull-pilot CONSUMER opt-in for the agent-side worker pool (#946 Phase 2, Epic
# #1045 / umbrella #1081). ORTHOGONAL to MCP_AGENT_CHAT_PULL_ENABLED above: that
# flag is the global PRODUCER switch (how agent→agent chat is enqueued, decided
# in the MCP server); this is a per-agent CONSUMER allowlist (which agents PULL
# their queued work via GET /api/internal/next-task instead of being pushed to).
# Comma-separated agent names; empty ⇒ no agent pulls (default). An allowlisted
# agent gets TRINITY_PULL_MODE / TRINITY_MAX_PARALLEL_TASKS injected at
# create/recreate (services/agent_service/pull_mode.py) — NOT the master internal
# secret: the worker authenticates with the agent's own scoped TRINITY_MCP_API_KEY
# (#307/#1159). Every other
# agent's push path is unchanged. Default OFF — clearing the list is the rollback.
PULL_MODE_PILOT_AGENTS = os.getenv("PULL_MODE_PILOT_AGENTS", "")

# Lease-reaper poison-task cap (#1081 Phase 3 — #429 / #1402). MAX_REDELIVERY is
# how many times a pull-claimed task whose worker died/hung (a `running` row with
# a past `lease_expires_at`) is re-delivered — the reaper re-queues the SAME
# execution_id and bumps `schedule_executions.redelivery_count` — before it is
# poison-parked to the operator queue (FAILED + a human-facing park item). The
# fleet-wide default; a per-agent override is a deferred follow-up (see
# services/lease_reaper_service.py). Inert until a PULL_MODE_PILOT_AGENTS agent is
# opted in (no non-pull row ever carries a lease).
MAX_REDELIVERY = int(os.getenv("MAX_REDELIVERY", "3"))

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
# Outbound A2A calls (#736) — a Trinity agent tasking an EXTERNAL A2A agent.
# RUNTIME-RESOLVED like the Brain Orb flags, deliberately: no import-time
# constant here, so a stale module value can never shadow an admin toggle, and —
# more importantly — a compose file that forgets to forward the variable cannot
# make the feature unreachable. Resolution lives in
# `services/a2a_outbound_service.is_outbound_enabled()`: system_settings row →
# A2A_OUTBOUND_ENABLED env opt-in → default OFF. Default OFF because this is the
# platform's first backend-executed, credentialed, agent-triggerable outbound
# fetcher, and every comparable surface (DISPATCH_ASYNC, CANARY_ENABLED,
# VOIP_ENABLED, MCP_INLINE_AUTH_ENABLED, BRAIN_ORB_*) ships default-OFF.
# Both routes 404 when off; `a2a_outbound_available` reports it in
# GET /api/settings/feature-flags.
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
#
# Intentionally EMPTY (#1931). The bundled list had gone stale — a pre-2026 repo
# set no install had ever overridden — so every operator browsed the same dead
# catalog. Curating GitHub templates is an explicit operator act, not a bundled
# default. An empty default costs nothing: any repo is still creatable at any
# time via `template: github:owner/repo` (`template_service.get_github_template`
# resolves an unconfigured id through its dynamic branch), so this removes a
# *browse* surface, never a *create* capability. It also means `GET /api/templates`
# makes zero outbound GitHub calls on a cold metadata cache.
#
# Do NOT refill this list. Keep the constant: TMPL-001's None-vs-[] fallback,
# routers/settings.py, and the Settings "defaults" badge all reference it. It is
# now the FLOOR under the remote template registry rather than the only source —
# trinity-enterprise#14 repointed this seam, so the resolution order is
# `admin DB override -> remote registry -> this list`. Refilling it would give
# every install a second, un-curatable catalog that no registry edit can remove.
DEFAULT_GITHUB_TEMPLATE_REPOS: list[str] = []


# --- Remote template registry (TMPL-002, trinity-enterprise#14) -------------
# The GitHub half of the catalog is sourced at RUNTIME from a `registry.yaml`
# fetched over HTTPS, so curating which starter agents an install offers is a
# vendor file edit rather than a Trinity release. Purely additive: it fills the
# branch the empty list above leaves, is never consulted when an admin has
# curated their own list, and every failure mode degrades back to that floor.
#
# `TEMPLATE_REGISTRY_ENABLED=false` is the HARD kill switch — the air-gap /
# policy answer. No `system_settings` row can turn it back on; the admin toggle
# (`template_registry_enabled`, default true when absent) is composed with this
# at the consumer, exactly as OPERATOR_INTAKE / TELEMETRY_SHARING compose theirs.
#
# Deliberately NOT resolved through `settings_service._resolve_bool_flag`: that
# helper's env leg is OPT-IN ONLY ("true"/"1"/"yes" -> True, anything else falls
# through to `default`), so with `default=True` it would silently swallow
# `TEMPLATE_REGISTRY_ENABLED=false` and ship an inert kill switch (#1039 class).
#
# Deliberately NOT coupled to DO_NOT_TRACK, unlike the two flags above. Those
# honour it because they SEND data about the operator. A registry fetch sends
# nothing — it is a package-index read, and npm and Homebrew do not disable
# their default registries under DNT. It is still outbound egress on a default
# install, which is a real behavioural change and carries a release note.
TEMPLATE_REGISTRY_ENABLED = (
    os.getenv("TEMPLATE_REGISTRY_ENABLED", "true").strip().lower() == "true"
)
# Vendor-operated default. `raw.githubusercontent.com` deliberately: no new
# infra, a public audit trail, and curation is literally a git commit. The
# document served here must exist before the release ships — an empty
# `version: 1` / `templates: []` is a valid registry and gives day-one
# behaviour of "fetch succeeds, zero entries, catalog unchanged, no warnings"
# (the ent#137 ship prerequisite).
TEMPLATE_REGISTRY_URL = os.getenv(
    "TEMPLATE_REGISTRY_URL",
    "https://raw.githubusercontent.com/Abilityai/trinity-templates/main/registry.yaml",
)


# ============================================================================
# Retention (#1039 community floor / #1638 upgrade safety)
# ============================================================================
# These live in config.py — a leaf module — rather than services/settings_service.py
# because database.py seeds them during init_database(), which runs at import
# time. settings_service imports `from database import db`, so reaching into it
# from the seed path is a circular import (#1638). settings_service re-exports
# both names, so `from services.settings_service import ...` keeps working.

# Community retention floor (days). The audit log is EXEMPT — it keeps a
# 365-day integrity floor (see audit_retention_service). This is also the value
# the enterprise `retention` module clamps unentitled writes to.
COMMUNITY_RETENTION_FLOOR_DAYS = 5

# #1638: the windows a FRESH install is seeded with, applying the community
# floor to new installs only. Seeded once, against an empty database, by
# database._seed_fresh_install_retention() — never written to an install that
# already has data.
#
# This is the ONLY mechanism that may apply the floor. Do NOT apply it by
# lowering OPS_SETTINGS_DEFAULTS: those are read at prune time as the fallback
# for an install with no row, so lowering one retroactively hard-DELETEs the
# existing data of every install that never opted in — silently, on its next
# boot. That is #1638, and it cost a production instance ~3 months of history.
#
# `agent_soft_delete_retention_days` is deliberately ABSENT: it is a *recovery*
# window, not a log window. Its expiry chains purge_agent_ownership ->
# clear_agent_runtime_state -> remove_agent_volumes (#1581), destroying the
# agent's workspace/public/shared volumes and any declared `data_paths` runtime
# data (#1169). The floor does not apply to it in ANY edition.
# ent#237: the bundled community skills source, written into a FRESH install so
# the library is never empty out of the box (AC#3). Seeded as a row rather than
# resolved as a code default at read time, for the same reason as the retention
# floor above: an existing install must not silently acquire a source it never
# configured, and an admin who deletes or disables this one must have that stick
# across restarts. A code-default fallback would resurrect it on every boot.
#
# `ref_type: tag` is AC#5 — the community catalog takes PRs from strangers and
# skills carry executables the ent#139 runner runs, so instances follow a tag we
# bump deliberately, never the branch head. The repo itself is ent#296; until it
# cuts its first tag this seed points at a 404 and `sync_library` reports a
# failed source (fail-soft by design — it never raises).
#
# The tag name must match what ent#296 actually publishes: that issue's plan
# (vybe, 2026-08-04) cuts **v0.1.0** once the seed content lands, so a `v1.0.0`
# default would leave every fresh install seeded with a source that can never
# sync — and the failure is quiet (a failed row in Settings), not loud. Bump
# this in lockstep with the catalog's releases; the env var is the escape hatch
# for an instance that wants to pin an older or newer catalog.
#
# TRINITY_DEFAULT_SKILL_SOURCE="" disables the seed entirely for an operator who
# wants no community catalog (mirrors TRINITY_DEFAULT_SYSTEM_MANIFEST).
DEFAULT_SKILL_SOURCE_URL = os.getenv(
    "TRINITY_DEFAULT_SKILL_SOURCE", "github.com/abilityai/trinity-skills"
)
DEFAULT_SKILL_SOURCE_REF = os.getenv("TRINITY_DEFAULT_SKILL_SOURCE_REF", "v0.1.0")
DEFAULT_SKILL_SOURCE_NAME = "Trinity Community Skills"

COMMUNITY_FRESH_INSTALL_SEED = {
    "execution_log_retention_days": str(COMMUNITY_RETENTION_FLOOR_DAYS),
    "execution_row_retention_days": str(COMMUNITY_RETENTION_FLOOR_DAYS),
    "health_check_retention_days": str(COMMUNITY_RETENTION_FLOOR_DAYS),
    "schedule_soft_delete_retention_days": str(COMMUNITY_RETENTION_FLOOR_DAYS),
}


# ============================================================================
# Ops-settings value validation (ent#297)
# ============================================================================
# `PUT /api/settings/ops/config` took `Dict[str, str]` and wrote it straight to
# `db.set_setting` with NO type or range check, so every ops setting — including
# the eight retention windows that drive irreversible deletion — accepted any
# string at all.
#
# Read the failure modes precisely, because they are asymmetric and the
# intuitive one is the harmless one:
#
#   * GARBAGE FAILS SAFE. Every reader coerces via `max(int(raw), 0)` inside a
#     try/except returning 0, and 0 means "sweep disabled". So "abc" widens
#     retention to forever. Wrong, but not destructive.
#   * A SMALL VALID INTEGER IS THE CATASTROPHIC INPUT. `{"execution_row_
#     retention_days": "1"}` is well-typed, in range for any naive check, and
#     deletes the fleet's execution history on the next 5-minute cleanup cycle.
#
# So validation is NOT what stops the ent#297 attack, and it must not be sold as
# such: a legitimate admin may genuinely want a 1-day window, and no range check
# can tell that apart from an attack. The controls that stop it are the admin
# gate rejecting agent principals (the root fix) and the #1644 blast-radius
# guard refusing an over-threshold sweep. What validation buys is narrower and
# still worth having — a malformed write fails LOUDLY at the boundary instead of
# silently coercing to a value nobody chose, which is the #1525
# validate-at-the-boundary rule and the same reasoning as the #506 ceiling's
# dedicated range-checked route.
# 10 years. Chosen to MATCH the enterprise `retention` module's own
# `_MAX_DAYS` (`RetentionConfigUpdate` validates every window `ge=0, le=3650`),
# not picked independently — these are two write paths to the same values, and
# a wider OSS bound would let an admin store a window the managed panel then
# refuses to edit, i.e. a value its own GET surfaces and its own PUT rejects.
# The enterprise constant can't be imported here (private submodule, and OSS
# must build without it), so the alignment is by value + this note.
_DAYS_MAX = 3650
_PERCENT = (0, 100)
_MINUTES = (0, 525600)              # 1 year

# key -> (kind, min, max); `None` bound = unbounded on that side.
OPS_SETTINGS_VALIDATION = {
    "ops_context_warning_threshold": ("int", *_PERCENT),
    "ops_context_critical_threshold": ("int", *_PERCENT),
    "ops_idle_timeout_minutes": ("int", *_MINUTES),
    "ops_cost_limit_daily_usd": ("float", 0, 1_000_000),
    "ops_max_execution_minutes": ("int", *_MINUTES),
    "ops_alert_suppression_minutes": ("int", *_MINUTES),
    "ops_log_retention_days": ("int", 0, _DAYS_MAX),
    "ops_health_check_interval": ("int", 1, 86400),
    "ssh_access_enabled": ("bool", None, None),
    # The row-retention windows (RETENTION_OPS_KEYS, minus #2216's backup key
    # below). `0` is a documented, meaningful value on every one of THESE —
    # "disable this sweep" — so the lower bound is 0 and NOT the community floor. Clamping to the floor here would be
    # wrong twice over: it would silently rewrite an operator's explicit choice,
    # and the floor is a fresh-install SEED plus an enterprise entitlement
    # clamp, deliberately NOT an OSS hard limit (#1039/#1638).
    "execution_log_retention_days": ("int", 0, _DAYS_MAX),
    "execution_row_retention_days": ("int", 0, _DAYS_MAX),
    "health_check_retention_days": ("int", 0, _DAYS_MAX),
    "agent_soft_delete_retention_days": ("int", 0, _DAYS_MAX),
    "schedule_soft_delete_retention_days": ("int", 0, _DAYS_MAX),
    "agent_reports_retention_days": ("int", 0, _DAYS_MAX),
    "operator_queue_retention_days": ("int", 0, _DAYS_MAX),
    "agent_reminders_retention_days": ("int", 0, _DAYS_MAX),
    # ent#433 — the two subscription-telemetry windows. `0` means "disable this
    # sweep" on both, same as the row windows above.
    "subscription_headroom_retention_days": ("int", 0, _DAYS_MAX),
    "subscription_failure_event_retention_days": ("int", 0, _DAYS_MAX),
    # #2216: the backup window's fail-safe direction is INVERTED vs the rows
    # above — for backups "never prune" fills the disk (#1871 class), so `0`
    # ("disable the sweep" everywhere else = keep-forever here) is REJECTED.
    # Disabling backups is the separate, explicit DB_BACKUP_ENABLED=false.
    # The lower bound 1 plus the fixed BACKUP_MIN_KEEP=3 floor in
    # db/backup_primitives.py carry the "small valid integer" (#1644) safety.
    "backup_retention_days": ("int", 1, _DAYS_MAX),
}


def validate_ops_setting(key: str, value: str) -> str:
    """Validate one ops setting; return it normalized, or raise ValueError.

    Unknown keys pass through untouched — `PUT /ops/config` already filters to
    `OPS_SETTINGS_DEFAULTS` and reports the rest as `ignored`, and turning that
    into a hard reject is a separate contract change.
    """
    spec = OPS_SETTINGS_VALIDATION.get(key)
    if spec is None:
        return value

    kind, low, high = spec
    raw = (value or "").strip()

    if kind == "bool":
        if raw.lower() not in ("true", "false"):
            raise ValueError(f"{key} must be 'true' or 'false' (got {value!r})")
        return raw.lower()

    try:
        parsed = int(raw) if kind == "int" else float(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"{key} must be {'an integer' if kind == 'int' else 'a number'} "
            f"(got {value!r}). An unparseable value used to coerce to 0, which "
            f"for a retention window silently means 'sweep disabled'."
        )

    if low is not None and parsed < low:
        raise ValueError(f"{key} must be >= {low} (got {parsed})")
    if high is not None and parsed > high:
        raise ValueError(f"{key} must be <= {high} (got {parsed})")

    return str(parsed)
