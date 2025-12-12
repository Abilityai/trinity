"""
Configuration constants for the Trinity backend.
"""
import os

# Development Mode
# Set DEV_MODE_ENABLED=true to enable local username/password login
# When false (default), only Auth0 OAuth is allowed
DEV_MODE_ENABLED = os.getenv("DEV_MODE_ENABLED", "false").lower() == "true"

# JWT Settings
SECRET_KEY = os.getenv("SECRET_KEY", "your-secret-key-change-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

# Service URLs
AUDIT_URL = os.getenv("AUDIT_URL", "http://audit-logger:8001")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")

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
        "required_credentials": ["HEYGEN_API_KEY", "TWITTER_API_KEY", "CLOUDINARY_API_KEY"]
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
    }
]

# Combined templates list
ALL_GITHUB_TEMPLATES = GITHUB_TEMPLATES
