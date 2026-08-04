# Credential Management Guide

## Overview

Trinity Agent Platform includes comprehensive credential management for secure authentication with external services. This system allows agents to connect to Google Workspace, Slack, GitHub, Notion, and other services with proper authentication.

## Architecture

### Storage
- **Redis**: Credentials stored in Redis with metadata separation
- **Security**: Secrets stored separately from metadata
- **Encryption**: Environment variables encrypted at rest
- **Audit**: All credential operations logged

### Components
1. **Backend API**: FastAPI endpoints for CRUD operations
2. **Frontend UI**: Vue.js interface for credential management
3. **OAuth Integration**: Support for OAuth 2.0 flows
4. **Agent Injection**: Automatic credential injection into containers

## Quick Start

### 1. Configure OAuth Providers

Copy the example environment file:
```bash
cp .env.example .env
```

Edit `.env` with your OAuth credentials:
```bash
GOOGLE_CLIENT_ID=your-google-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-google-client-secret

SLACK_CLIENT_ID=your-slack-client-id
SLACK_CLIENT_SECRET=your-slack-client-secret
```

### 2. Add Credentials via UI

There are three ways to add credentials:

#### Option A: Bulk Import (Recommended for Templates)

The easiest way to add multiple credentials at once, especially for agent templates:

1. Navigate to http://localhost:3000/credentials
2. Click "Bulk Import Credentials"
3. **Optional**: Select an agent template from the dropdown and click "Copy Template" to get the list of required credential keys
4. Paste your `.env`-style content (KEY=VALUE pairs from 1Password or similar)
5. Click "Import Credentials"

Example bulk import format:
```bash
# Comments are ignored
HEYGEN_API_KEY=your-heygen-key
TWITTER_API_KEY=your-twitter-key
TWITTER_API_SECRET=your-twitter-secret
CLOUDINARY_API_KEY=your-cloudinary-key

# Quoted values work too
ANTHROPIC_API_KEY="sk-ant-..."
```

The system automatically:
- Parses KEY=VALUE pairs (handles quotes, comments, empty lines)
- Infers the service name from the key prefix (e.g., `TWITTER_` → `twitter`)
- Detects credential type from suffix (e.g., `_API_KEY` → `api_key`, `_TOKEN` → `token`)

#### Option B: Single Credential Entry

1. Navigate to http://localhost:3000/credentials
2. Click "Add Credential"
3. Fill in name, service, type, and value
4. Click "Save"

#### Option C: OAuth Flow

1. Navigate to http://localhost:3000/credentials
2. Click the OAuth button for your service (Google, Slack, GitHub, Notion)
3. Complete the authorization flow in the popup
4. Credential is automatically saved

### 3. Copy Credential Template

Before creating an agent from a template, you can get a list of all required credentials:

1. Go to http://localhost:3000/credentials
2. Click "Bulk Import Credentials"
3. Select the template from the dropdown (e.g., "ruby-social-media-agent")
4. Click "Copy Template" - this copies all required KEY= placeholders to clipboard
5. Paste into 1Password or your password manager
6. Fill in the values
7. Copy back and paste into the bulk import textarea

Example copied template:
```bash
# Credentials for ruby-social-media-agent
HEYGEN_API_KEY=
TWITTER_API_KEY=
TWITTER_API_SECRET=
TWITTER_ACCESS_TOKEN=
TWITTER_ACCESS_TOKEN_SECRET=
CLOUDINARY_API_KEY=
CLOUDINARY_API_SECRET=
CLOUDINARY_CLOUD_NAME=
```

### 4. Assign Credentials to Agents

When creating an agent:
```bash
POST /api/agents/{agent_name}/credentials/{mcp_server}
{
  "cred_id": "credential-id-here"
}
```

Or via UI:
1. Go to Agents page
2. Select agent
3. Click "Manage Credentials"
4. Assign credentials to MCP servers

## API Reference

### Create Credential
```http
POST /api/credentials
Authorization: Bearer <token>
Content-Type: application/json

{
  "name": "My OpenAI Key",
  "service": "openai",
  "type": "api_key",
  "credentials": {
    "api_key": "sk-..."
  },
  "description": "Production API key"
}
```

### List Credentials
```http
GET /api/credentials
Authorization: Bearer <token>
```

### Get Credential
```http
GET /api/credentials/{cred_id}
Authorization: Bearer <token>
```

### Update Credential
```http
PUT /api/credentials/{cred_id}
Authorization: Bearer <token>
Content-Type: application/json

{
  "name": "Updated Name",
  "description": "Updated description"
}
```

### Delete Credential
```http
DELETE /api/credentials/{cred_id}
Authorization: Bearer <token>
```

### Bulk Import Credentials
```http
POST /api/credentials/bulk
Authorization: Bearer <token>
Content-Type: application/json

{
  "content": "HEYGEN_API_KEY=xxx\nTWITTER_API_KEY=yyy\nTWITTER_API_SECRET=zzz"
}

Response:
{
  "created": 3,
  "skipped": 0,
  "errors": [],
  "credentials": [
    {"id": "abc123", "name": "HEYGEN_API_KEY", "service": "heygen"},
    {"id": "def456", "name": "TWITTER_API_KEY", "service": "twitter"},
    {"id": "ghi789", "name": "TWITTER_API_SECRET", "service": "twitter"}
  ]
}
```

The content field accepts `.env`-style format:
- One KEY=VALUE per line
- Lines starting with `#` are comments (ignored)
- Empty lines are ignored
- Values can be quoted with single or double quotes
- Existing credentials with the same name are skipped (not overwritten)

> **Note**: the former `GET /api/templates/env-template` endpoint (pre-filled `.env` skeleton for a template) no longer exists. A template's required credential variables are extracted at agent-creation time by `template_service.extract_agent_credentials()` from `.mcp.json`/`.mcp.json.template` (`${VAR}` patterns), `template.yaml` (credentials section), and `.env.example`, and surfaced per template via `GET /api/templates` / `GET /api/templates/{id}` (`required_credentials`).

## OAuth Flows

### Supported Providers
- **Google**: Gmail, Calendar, Drive
- **Slack**: Messaging, channels
- **GitHub**: Repositories, issues, PRs
- **Notion**: Pages, databases

### OAuth Configuration

#### Google Workspace
1. Go to https://console.cloud.google.com
2. Create OAuth 2.0 Client ID
3. Add redirect URI: `http://localhost:8000/api/oauth/google/callback`
4. Copy Client ID and Secret to `.env`

#### Slack
1. Go to https://api.slack.com/apps
2. Create new app
3. Add OAuth redirect: `http://localhost:8000/api/oauth/slack/callback`
4. Add scopes: `chat:write`, `channels:read`, `users:read`
5. Copy credentials to `.env`

#### GitHub
1. Go to https://github.com/settings/developers
2. Create OAuth App
3. Set callback: `http://localhost:8000/api/oauth/github/callback`
4. Copy credentials to `.env`

#### Notion
1. Go to https://www.notion.so/my-integrations
2. Create new integration
3. Set redirect: `http://localhost:8000/api/oauth/notion/callback`
4. Copy credentials to `.env`

### OAuth Flow Process

1. **Initiate**: User clicks OAuth button in UI
2. **Redirect**: Backend generates auth URL with state
3. **Authorize**: User grants permissions on provider site
4. **Callback**: Provider redirects to backend callback
5. **Exchange**: Backend exchanges code for tokens
6. **Store**: Tokens stored as credential
7. **Complete**: User redirected back to UI

### Start OAuth Flow
```http
POST /api/oauth/{provider}/init
Authorization: Bearer <token>

Response:
{
  "auth_url": "https://accounts.google.com/o/oauth2/...",
  "state": "random-state-token"
}
```

### OAuth Callback (automatic)
```http
GET /api/oauth/{provider}/callback?code=...&state=...

Response:
{
  "message": "OAuth authentication successful",
  "credential_id": "new-credential-id",
  "redirect": "http://localhost:3000/credentials"
}
```

## Credential Injection

### How It Works

Credentials are injected into agent containers **at startup time only**. The process differs depending on whether the agent uses a template:

#### For Template-Based Agents

1. **Template contains `.mcp.json` with placeholders**: Uses `${VAR_NAME}` syntax
2. **Backend generates credential files**: Reads template's `.mcp.json`, replaces placeholders with real values from Redis
3. **Files mounted into container**: Generated files mounted at `/generated-creds/`
4. **Startup script copies files**: `startup.sh` copies `.mcp.json` and `.env` to `/home/developer/`
5. **MCP servers read credentials**: From the generated `.mcp.json` file

#### For Non-Template Agents

1. **Backend creates credentials JSON**: All assigned credentials written to a JSON file
2. **File mounted read-only**: At `/config/credentials.json`
3. **Environment variables set**: Credentials also available as env vars

### Important Limitations

> ⚠️ **Credentials are injected at AGENT CREATION time only.**
>
> This is a critical limitation to understand:
> - Credentials are read from Redis and injected into the container when the agent is **created**
> - Simply stopping and starting an agent does **NOT** reload credentials
> - If you add new credentials after an agent exists, you must **delete and recreate** the agent
> - There is no mechanism to push credential updates to existing containers
>
> **Workflow for adding credentials to an existing agent:**
> 1. Note the agent's configuration (template, resources, etc.)
> 2. Delete the agent via UI or API
> 3. Add/update credentials via Credentials page
> 4. Recreate the agent with the same configuration
> 5. New credentials will be injected at creation

### Template Placeholder Syntax

Templates use `${VAR_NAME}` placeholders in `.mcp.json` files:

```json
{
  "mcpServers": {
    "twitter": {
      "command": "npx",
      "args": ["-y", "@anthropic/twitter-mcp"],
      "env": {
        "TWITTER_API_KEY": "${TWITTER_API_KEY}",
        "TWITTER_API_SECRET": "${TWITTER_API_SECRET}",
        "TWITTER_ACCESS_TOKEN": "${TWITTER_ACCESS_TOKEN}"
      }
    }
  }
}
```

At agent creation, the backend replaces `${TWITTER_API_KEY}` with the actual value from the credential named `TWITTER_API_KEY` in Redis.

### Container File Locations

| File | Path in Container | Source |
|------|-------------------|--------|
| Generated `.mcp.json` | `/home/developer/.mcp.json` | Copied from `/generated-creds/.mcp.json` |
| Generated `.env` | `/home/developer/.env` | Copied from `/generated-creds/.env` |
| Credentials JSON | `/config/credentials.json` | Mounted directly |
| Helper script | `/scripts/load-credentials.sh` | Built into base image |

### Environment Variables

Credentials are exposed as environment variables with standardized naming:

```bash
# API Keys (format: {SERVICE}_API_KEY)
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...

# Tokens (format: {SERVICE}_TOKEN)
SLACK_TOKEN=xoxb-...
GITHUB_TOKEN=ghp_...

# OAuth (format: {SERVICE}_ACCESS_TOKEN, {SERVICE}_REFRESH_TOKEN)
GOOGLE_ACCESS_TOKEN=ya29...
GOOGLE_REFRESH_TOKEN=1//...

# Basic Auth (format: {SERVICE}_USERNAME, {SERVICE}_PASSWORD)
CUSTOM_USERNAME=user
CUSTOM_PASSWORD=pass
```

Service names are uppercased and hyphens replaced with underscores (e.g., `google-workspace` becomes `GOOGLE_WORKSPACE_ACCESS_TOKEN`).

### Credentials JSON File

JSON file mounted at `/config/credentials.json`:
```json
{
  "google-workspace": {
    "access_token": "ya29...",
    "refresh_token": "1//...",
    "token_type": "Bearer"
  },
  "slack": {
    "token": "xoxb-..."
  },
  "openai": {
    "api_key": "sk-..."
  }
}
```

### Using Credentials in Agent Code

**Python example:**
```python
import os
import json

# Method 1: Environment variables (preferred for simple cases)
openai_key = os.getenv('OPENAI_API_KEY')
slack_token = os.getenv('SLACK_TOKEN')

# Method 2: JSON file (for accessing all credentials)
if os.path.exists('/config/credentials.json'):
    with open('/config/credentials.json') as f:
        creds = json.load(f)

    openai_key = creds.get('openai', {}).get('api_key')
    slack_token = creds.get('slack', {}).get('token')
```

**MCP servers** typically read from the generated `.mcp.json` file automatically when using Claude Code.

### Creating Template-Compatible Credentials

For templates to work properly with credentials:

1. **Template's `.mcp.json`** must use `${VAR_NAME}` placeholders
2. **Credentials in Redis** must have names matching the placeholder variable names
3. **Credential values** should be stored with appropriate keys (`api_key`, `token`, etc.)

Example: If template has `${TWITTER_API_KEY}`, create a credential with:
- Name: `TWITTER_API_KEY`
- Type: `api_key`
- Value: The actual API key

## Security

### Best Practices
- ✅ Credentials encrypted at rest (Redis)
- ✅ Secrets never logged or exposed in API responses
- ✅ Audit logging for all credential operations
- ✅ User-scoped credentials (no sharing between users)
- ✅ Credentials mounted read-only in containers
- ✅ Environment variables isolated per agent

### Audit Events
All credential operations are logged:
- `credential_management.create`
- `credential_access.list`
- `credential_access.get`
- `credential_management.update`
- `credential_management.delete`
- `oauth.init`
- `oauth.callback`
- `credential_management.assign_to_agent`

View audit logs:
```bash
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/audit/logs?event_type=credential_management
```

## Troubleshooting

### OAuth Not Working
```
Error: OAuth not configured for google
```
**Solution**: Set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in `.env`

### Credentials Not Appearing in Agent
```
Agent starts but MCP server says "unauthorized"
```
**Solution**:
1. Check credential assigned: `GET /api/agents/{name}/credentials`
2. Verify credential exists: `GET /api/credentials/{id}`
3. Check agent logs: `docker logs agent-{name}`

### Updated Credentials Not Taking Effect
```
Changed credential in UI but agent still uses old value
```
**Solution**: Credentials are injected at agent **creation** time only (not restart). You must delete and recreate the agent:
1. Note agent configuration (template, resources, name)
2. Delete the agent: `DELETE /api/agents/{name}` or via UI
3. Update credentials on the Credentials page
4. Recreate the agent with the same configuration
5. Verify new credentials: `docker exec agent-{name} cat /home/developer/.mcp.json`

### Bulk Imported Credentials Not Showing as Configured
```
Imported credentials via bulk import but agent shows them as "missing"
```
**Solution**: The credential matching is case-insensitive and matches by name. Ensure:
1. Credential names match the required variable names exactly (e.g., `HEYGEN_API_KEY`)
2. The agent was created **after** importing credentials
3. If agent existed before import, delete and recreate it

### Template Placeholders Not Replaced
```
.mcp.json still shows ${VAR_NAME} instead of actual value
```
**Solution**:
1. Ensure credential name in Redis matches the placeholder exactly (e.g., `TWITTER_API_KEY`)
2. Check the template's `.mcp.json` uses correct syntax: `${VAR_NAME}` (not `{VAR_NAME}` or `$VAR_NAME`)
3. Verify credential was created before agent was started

### OAuth Callback Fails
```
Error: Invalid or expired OAuth state
```
**Solution**: OAuth state expires in 10 minutes. Restart flow.

### Redis Connection Error
```
Failed to create credential: Connection refused
```
**Solution**: Ensure Redis is running: `docker ps | grep redis`

## Examples

### Add OpenAI Key
```bash
curl -X POST http://localhost:8000/api/credentials \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "OpenAI Production",
    "service": "openai",
    "type": "api_key",
    "credentials": {
      "api_key": "sk-..."
    }
  }'
```

### Add Slack Token
```bash
curl -X POST http://localhost:8000/api/credentials \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Slack Bot",
    "service": "slack",
    "type": "token",
    "credentials": {
      "token": "xoxb-..."
    }
  }'
```

### Assign to Agent
```bash
curl -X POST http://localhost:8000/api/agents/my-agent/credentials/slack \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "cred_id": "credential-id-here"
  }'
```

## Architecture Diagram

```
┌─────────────────┐
│   Frontend UI   │
│  (Vue.js)       │
└────────┬────────┘
         │
         │ HTTPS
         ▼
┌─────────────────┐
│   Backend API   │
│   (FastAPI)     │
└────────┬────────┘
         │
         ├──────────────┐
         │              │
         ▼              ▼
┌─────────────┐  ┌──────────────┐
│    Redis    │  │ Audit Logger │
│ (Credentials)│  │   (SQLite)   │
└─────────────┘  └──────────────┘
         │
         │ At agent creation:
         │ 1. Read credentials from Redis
         │ 2. Generate .mcp.json, .env files
         │ 3. Replace ${PLACEHOLDERS}
         ▼
┌─────────────────────────────────────────┐
│           Agent Container               │
│                                         │
│  /generated-creds/  ──copy──►  /home/developer/  │
│    .mcp.json                    .mcp.json       │
│    .env                         .env            │
│                                                 │
│  /config/credentials.json (mounted read-only)  │
│                                                 │
│  Environment variables:                         │
│    OPENAI_API_KEY, SLACK_TOKEN, etc.           │
│                                                 │
│  MCP Servers read from .mcp.json               │
└─────────────────────────────────────────┘

Note: Credentials are injected at startup only.
      To update credentials, restart the agent.
```

## Next Steps

1. Configure OAuth providers in `.env`
2. Add credentials via UI
3. Create agents with MCP servers
4. Assign credentials to agents
5. Test authentication with external services

For support, see [HONEST_STATUS.md](../HONEST_STATUS.md) for known issues and limitations.

