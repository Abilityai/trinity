"""
Configuration constants for the Trinity backend.
"""
import os

# Development Mode
# Set DEV_MODE_ENABLED=true to enable local username/password login
# When false (default), only Auth0 OAuth is allowed
DEV_MODE_ENABLED = os.getenv("DEV_MODE_ENABLED", "false").lower() == "true"

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
ACCESS_TOKEN_EXPIRE_MINUTES = 30

# Service URLs
AUDIT_URL = os.getenv("AUDIT_URL", "http://audit-logger:8001")

# Redis URL - supports password via REDIS_PASSWORD env var or in URL
_redis_password = os.getenv("REDIS_PASSWORD", "")
_redis_base_url = os.getenv("REDIS_URL", "redis://redis:6379")
if _redis_password and "://@" not in _redis_base_url and "://:" not in _redis_base_url:
    # Insert password into URL if not already present
    if "://" in _redis_base_url:
        scheme, rest = _redis_base_url.split("://", 1)
        REDIS_URL = f"{scheme}//:{_redis_password}@{rest}"
    else:
        REDIS_URL = _redis_base_url
else:
    REDIS_URL = _redis_base_url
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")

# Email Service Configuration (for public link verification)
EMAIL_PROVIDER = os.getenv("EMAIL_PROVIDER", "resend")  # "console", "smtp", "sendgrid", "resend"
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "noreply@trinity.example.com")
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY", "")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")

# GitHub PAT for template cloning (auto-uploaded to Redis on startup)
GITHUB_PAT = os.getenv("GITHUB_PAT", "")
GITHUB_PAT_CREDENTIAL_ID = "github-pat-templates"  # Fixed ID for consistent reference

# Auth0 Configuration
# Set these environment variables to enable Auth0 authentication
AUTH0_DOMAIN = os.getenv("AUTH0_DOMAIN", "")  # e.g., "your-tenant.us.auth0.com"
AUTH0_ALLOWED_DOMAIN = os.getenv("AUTH0_ALLOWED_DOMAIN", "")  # e.g., "your-company.com" (leave empty to allow all)

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

CORS_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:8080",
] + _extra_origins

# GitHub Templates
# Configure your own GitHub agent templates here or via a config file.
# Format: github:owner/repo - requires GITHUB_PAT credential for private repos
# See docs/AGENT_TEMPLATE_SPEC.md for template structure
GITHUB_TEMPLATES = [
    {
        "id": "github:abilityai/agent-ruby",
        "display_name": "Ruby - Content & Publishing",
        "description": "Content creation and multi-platform social media distribution agent",
        "github_repo": "abilityai/agent-ruby",
        "github_credential_id": GITHUB_PAT_CREDENTIAL_ID,
        "source": "github",
        "resources": {"cpu": "2", "memory": "4g"},
        "mcp_servers": [],
        "required_credentials": ["HEYGEN_API_KEY", "TWITTER_API_KEY", "CLOUDINARY_API_KEY"],
        # Optional: shared_folders - default expose/consume settings (Req 9.11)
        # "shared_folders": {"expose": True, "consume": False}
    },
    {
        "id": "github:abilityai/agent-cornelius",
        "display_name": "Cornelius - Knowledge Manager",
        "description": "Knowledge base manager for Obsidian vault and insight extraction",
        "github_repo": "abilityai/agent-cornelius",
        "github_credential_id": GITHUB_PAT_CREDENTIAL_ID,
        "source": "github",
        "resources": {"cpu": "2", "memory": "4g"},
        "mcp_servers": [],
        "required_credentials": ["SMART_VAULT_PATH", "GEMINI_API_KEY"]
    },
    {
        "id": "github:abilityai/agent-corbin",
        "display_name": "Corbin - Business Assistant",
        "description": "Business operations and Google Workspace management agent",
        "github_repo": "abilityai/agent-corbin",
        "github_credential_id": GITHUB_PAT_CREDENTIAL_ID,
        "source": "github",
        "resources": {"cpu": "2", "memory": "4g"},
        "mcp_servers": [],
        "required_credentials": ["GOOGLE_CLOUD_PROJECT_ID", "LINKEDIN_API_KEY"]
    },
    # Ruby Multi-Agent Content System
    {
        "id": "github:abilityai/ruby-orchestrator",
        "display_name": "Ruby Orchestrator - Calendar & State Manager",
        "description": "Master coordinator for content scheduling, state management, and agent triggering",
        "github_repo": "abilityai/ruby-orchestrator",
        "github_credential_id": GITHUB_PAT_CREDENTIAL_ID,
        "source": "github",
        "resources": {"cpu": "2", "memory": "4g"},
        "mcp_servers": ["trinity"],
        "required_credentials": ["TRINITY_API_URL", "TRINITY_API_KEY"]
    },
    {
        "id": "github:abilityai/ruby-content",
        "display_name": "Ruby Content - Discovery & Production",
        "description": "Content discovery, classification, and production agent with AI short generation",
        "github_repo": "abilityai/ruby-content",
        "github_credential_id": GITHUB_PAT_CREDENTIAL_ID,
        "source": "github",
        "resources": {"cpu": "2", "memory": "4g"},
        "mcp_servers": ["heygen", "cloudinary-asset-mgmt", "aistudio", "giphy"],
        "required_credentials": ["HEYGEN_API_KEY", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET", "CLOUDINARY_CLOUD_NAME", "GEMINI_API_KEY", "GIPHY_API_KEY", "KLAP_API_KEY", "KLAP_BASE_URL", "CREATOMATE_API_KEY"]
    },
    {
        "id": "github:abilityai/ruby-engagement",
        "display_name": "Ruby Engagement - Social & Growth",
        "description": "Engagement monitoring, viral reply hunting, and comment response agent",
        "github_repo": "abilityai/ruby-engagement",
        "github_credential_id": GITHUB_PAT_CREDENTIAL_ID,
        "source": "github",
        "resources": {"cpu": "2", "memory": "4g"},
        "mcp_servers": ["twitter-mcp", "mcp-metricool", "aistudio"],
        "required_credentials": ["TWITTER_API_KEY", "TWITTER_API_SECRET_KEY", "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_TOKEN_SECRET", "METRICOOL_USER_TOKEN", "METRICOOL_USER_ID", "GEMINI_API_KEY", "BLOTATO_API_KEY", "BLOTATO_BASE_URL", "BLOTATO_YOUTUBE_ID", "BLOTATO_INSTAGRAM_ID", "BLOTATO_LINKEDIN_ID", "BLOTATO_TWITTER_ID", "BLOTATO_THREADS_ID", "BLOTATO_TIKTOK_ID"]
    }
]

# Combined templates list
ALL_GITHUB_TEMPLATES = GITHUB_TEMPLATES
