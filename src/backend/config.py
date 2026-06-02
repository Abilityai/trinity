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

# Voice Chat Configuration (VOICE-001)
VOICE_ENABLED = os.getenv("VOICE_ENABLED", "true").lower() == "true"
VOICE_MODEL = os.getenv("VOICE_MODEL", "models/gemini-3.1-flash-live-preview")
VOICE_MAX_DURATION = int(os.getenv("VOICE_MAX_DURATION", "300"))  # seconds

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
