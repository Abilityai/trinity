# First-Time Setup

Install Trinity, create your admin account, and start managing agents in minutes.

> 📺 **Watch:** [I Built a DevOps Agent That Deploys Other Agents](https://youtu.be/8RozanPd14Y) *(Apr 2026)* · [all videos](../videos.md)

## Concepts

- **Admin Account** -- The primary account with full platform access, authenticated by username and password. Created automatically from `ADMIN_PASSWORD` in `.env`.
- **Email Login** -- A passwordless authentication method where users receive a one-time code via email. Requires an email service to be configured.

## How It Works

### Prerequisites

- Docker Desktop installed and running
- Git (required for GitHub-based agent templates)
- A modern web browser

### Installation

1. Clone the repository:

   ```bash
   git clone https://github.com/abilityai/trinity.git
   cd trinity
   ```

2. Set `ADMIN_PASSWORD` in `.env` before first boot:

   ```bash
   cp .env.example .env
   # Edit .env and set ADMIN_PASSWORD to a strong password (min 12 chars)
   ```

   The `admin` account is created automatically from this value on first start. If you leave it blank, a one-time setup token is printed to the backend logs (`docker compose logs backend | grep "Setup token"`) — paste it into the setup wizard that appears on first visit. The token is shared across backend workers, so the single value printed in the logs always works regardless of how many workers are running. If the setup wizard shows a "waiting for Redis" state, the token store is temporarily unreachable; setup resumes automatically once Redis recovers, with no restart needed.

3. Start all services:

   ```bash
   ./scripts/deploy/start.sh
   ```

   On first run, this detects if the base agent image is missing and builds it automatically (takes 5-10 minutes). Then starts the backend, frontend, MCP server, Redis, scheduler, and Vector.

4. Open http://localhost in your browser.

### Logging In

**Admin login:** Enter username `admin` and the `ADMIN_PASSWORD` you set in `.env`.

**Email login (passwordless):** Enter your email address, receive a 6-digit verification code, and submit it to log in. This requires email service configuration. The admin manages allowed email addresses under Settings > Email Whitelist.

### Key URLs

| Service | URL |
|---------|-----|
| Web UI | http://localhost |
| Backend API docs | http://localhost:8000/docs |
| MCP Server | http://localhost:8080/mcp |

### Stopping and Starting

```bash
# Stop all services
./scripts/deploy/stop.sh

# Start all services
./scripts/deploy/start.sh

# Rebuild services after code changes
docker compose build --no-cache backend frontend mcp-server

# View backend logs
docker compose logs -f backend
```

### Settings Page (Admin Only)

From the Settings page, the admin can configure:

- **Email Whitelist** -- Control which email addresses can log in.
- **GitHub Templates** -- Manage template repositories for agent creation.
- **GitHub Personal Access Token** -- Platform-wide PAT so agents can pull/push GitHub repos. See [GitHub PAT Setup](../integrations/github-pat-setup.md) for the recommended setup (classic vs. fine-grained, permissions, and ongoing maintenance).
- **Platform API Keys** -- Generate and revoke API keys for programmatic access.
- **Slack Integration** -- Connect Trinity to a Slack workspace.
- **System Prompt** -- Set the system-wide Trinity prompt applied to all agents.

## For Agents

### Authentication Endpoint

```
POST /api/token
Content-Type: application/x-www-form-urlencoded

username=admin&password=YOUR_PASSWORD
```

Returns:

```json
{
  "access_token": "eyJ...",
  "token_type": "bearer"
}
```

### Using the Token

Include the token in the `Authorization` header for all authenticated requests:

```bash
curl -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  http://localhost:8000/api/agents
```

### Token Details

- JWT tokens are valid for 7 days.
- Tokens are invalidated when the backend restarts. Re-login is required.
- MCP API keys (prefixed `trinity_mcp_`) also work as Bearer tokens.

### Unauthenticated Endpoints

The following endpoints do not require authentication:

- `GET /api/auth/mode` -- Returns the current authentication mode.
- `GET /api/setup/status` -- Returns whether initial setup is complete.
- `POST /api/token` -- The login endpoint itself.

## Limitations

- Backend restarts invalidate all active JWT tokens. All users and integrations must re-authenticate.
- Email login requires a configured email service. Without it, only admin password login is available.
- Trinity requires Docker Desktop. It cannot run without Docker.

## See Also

- [Quick Start](quick-start.md) -- After setup, a **guided onboarding wizard** opens on
  your first Dashboard visit to launch your first agent (relaunch any time at
  `http://localhost/?onboarding=1`).
- [Overview](../overview.md) -- Platform overview and core concepts.
- [Creating Agents](../agents/creating-agents.md) -- Deploy your first agent.
