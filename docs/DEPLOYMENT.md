# Trinity Local Development Guide

This guide covers setting up Trinity for local development.

> **Deploying to a server?** Use the [trinity-ops-public](https://github.com/abilityai/trinity-ops-public) ops agent — a Claude Code agent that manages any Trinity instance (health checks, updates, rollback, log triage, provisioning guides for Hetzner / GCP / AWS / DigitalOcean).

## Prerequisites

- Docker and Docker Compose v2+
- 8 GB RAM minimum (recommended: 16 GB for multiple agents)

## Quick Start

```bash
# 1. Clone the repository
git clone https://github.com/abilityai/trinity.git
cd trinity

# 2. Copy and configure environment
cp .env.example .env
# Edit .env with your settings (see Configuration section)

# 3. Build the base agent image
./scripts/deploy/build-base-image.sh

# 4. Start all services
./scripts/deploy/start.sh

# 5. Access the platform
# Web UI: http://localhost
# API Docs: http://localhost:8000/docs
```

## Configuration

> **Database backend:** Trinity uses **SQLite by default** (zero-config). To run
> a new instance on **PostgreSQL** instead, see
> [POSTGRESQL_SETUP.md](POSTGRESQL_SETUP.md) — it is opt-in via the
> `DATABASE_URL` env var and does not affect the SQLite default (#300).

### Required Environment Variables

Edit `.env` with these required settings:

```bash
# Security - REQUIRED (generate with: openssl rand -hex 32)
SECRET_KEY=your-secret-key-here

# Admin credentials for dev mode
ADMIN_USERNAME=admin
ADMIN_PASSWORD=your-secure-password

# Anthropic API Key - Required for Claude-powered agents
ANTHROPIC_API_KEY=sk-ant-your-api-key
```

### Google API Key (Optional - for Gemini-powered agents)

To use Gemini CLI as an alternative runtime (free tier with 1M token context):

```bash
# Get from: https://makersuite.google.com/app/apikey
GOOGLE_API_KEY=your-google-api-key
```

See [Gemini Support Guide](GEMINI_SUPPORT.md) for details on multi-runtime configuration.

### GitHub Templates (Optional)

To use GitHub-based agent templates (private repositories), add your GitHub Personal Access Token:

```bash
# GitHub PAT for cloning private template repos
# Get from: https://github.com/settings/tokens (classic token with 'repo' scope)
GITHUB_PAT=github_pat_xxxxx
```

**How it works:**
- On startup, the backend automatically uploads the PAT to Redis
- GitHub templates in `config.py` reference this credential
- When creating an agent from a GitHub template, the PAT is used to clone the repo

**Note:** The PAT is stored in Redis with a fixed credential ID (`github-pat-templates`). If you update the PAT in `.env`, restart the backend to sync it to Redis.

### Authentication

Trinity supports two login methods:

#### Email Login (Primary)
Users enter email → receive 6-digit code → login. Configure email provider:
```bash
EMAIL_PROVIDER=console  # console (dev), smtp, sendgrid, resend
```

For local development, use `console` - codes are printed to backend logs.

Manage allowed emails in Settings → Email Whitelist.

#### Admin Login
Password-based login for admin user:
```bash
ADMIN_PASSWORD=your-secure-password
```

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Trinity Platform                        │
├─────────────────────────────────────────────────────────────┤
│  Frontend (Vue.js)  │  Backend (FastAPI)  │  MCP Server     │
│      Port 80        │     Port 8000       │    Port 8080    │
├─────────────────────────────────────────────────────────────┤
│  Redis (secrets)    │  SQLite (data)      │  Vector (logs)  │
│   Internal only     │   /data volume      │    Port 8686    │
├─────────────────────────────────────────────────────────────┤
│                    Agent Containers                          │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐                     │
│  │ Agent 1 │  │ Agent 2 │  │ Agent N │  ...                │
│  │ SSH:2222│  │ SSH:2223│  │ SSH:222N│                     │
│  └─────────┘  └─────────┘  └─────────┘                     │
└─────────────────────────────────────────────────────────────┘
```

## Data Persistence

| Data | Location | Backup Strategy |
|------|----------|-----------------|
| SQLite (users, agents) | `~/trinity-data/trinity.db` | Regular file backup |
| Redis (credentials) | Docker volume | Redis RDB snapshots |
| Agent workspaces | Docker volumes | Per-agent backup |

### Backup Script

```bash
# Backup database
./scripts/deploy/backup-database.sh ./backups/

# Restore from backup
./scripts/deploy/restore-database.sh ./backups/trinity_backup.db
```

## Troubleshooting

### View Logs

```bash
# All services
docker compose logs -f

# Specific service
docker compose logs -f backend
docker compose logs -f frontend

# Agent container
docker logs agent-myagent
```

### Common Issues

**Agent creation fails**
- Check if `trinity-agent-base` image exists: `docker images | grep trinity-agent-base`
- Rebuild: `./scripts/deploy/build-base-image.sh`

**Stale platform images after a `git pull` (issue #557)**

Symptom: `start.sh` finishes with "Trinity Agent Platform - Ready!", but
the UI shows "Disconnected" and `curl http://localhost:8000/health`
returns connection refused. `docker compose ps` shows `trinity-backend`
as `Up`, yet the backend logs are full of:

```
File "/app/main.py", line N, in <module>
    from <some_package> import <something>
ModuleNotFoundError: No module named '<some_package>'
```

Cause: backend source is bind-mounted from your working tree, but the
**Python environment is baked into the platform image at build time**.
After a `git pull` that adds a new dependency to
`docker/backend/Dockerfile`, `docker/scheduler/requirements.txt`,
`src/frontend/package.json`, or `src/mcp-server/package.json`, the new
source runs against the old image's environment and the import fails.
Compose keeps respawning the crash-looping worker, so port 8000 never
binds.

Fix: rebuild the affected platform images and restart, then `start.sh`
will boot a healthy stack.

```bash
docker compose build      # or: docker compose build backend frontend mcp-server scheduler
docker compose up -d
```

If you frequently pull `dev`, alias this to a single command in your
shell — Trinity does not currently auto-detect the staleness on
`start.sh` because the cost of doing so on every cold start is too high
relative to how rarely the failure occurs (see #557 for the discussion).
A future `scripts/deploy/upgrade.sh` will wrap backup + rebuild + start
+ verify for the explicit upgrade path.

**Redis connection errors**
- Ensure Redis is running: `docker compose ps redis`
- Check Redis logs: `docker compose logs redis`

**Email login not working**
- Check backend logs: `docker compose logs backend`
- Verify EMAIL_PROVIDER is set correctly
- For local dev, use `console` (codes printed to logs)

## OpenTelemetry Metrics (Optional)

Trinity agents can export metrics to an OpenTelemetry collector for external observability tools like Prometheus and Grafana. This leverages Claude Code's built-in OTel support.

### What You Get

| Metric | Description |
|--------|-------------|
| `claude_code.cost.usage` | Cost per API call in USD |
| `claude_code.token.usage` | Token consumption (input/output/cache) |
| `claude_code.lines_of_code.count` | Code added/removed |
| `claude_code.session.count` | Session lifecycle tracking |
| `claude_code.active_time.total` | Active usage duration |

### Quick Start

1. **Enable OTel in your `.env`**:
   ```bash
   OTEL_ENABLED=1
   OTEL_COLLECTOR_ENDPOINT=http://trinity-otel-collector:4317
   ```

2. **Restart the backend**:
   ```bash
   docker-compose restart backend
   ```

3. **Create new agents** - They will automatically export metrics

See `docs/drafts/OTEL_INTEGRATION.md` for full collector configuration and Grafana dashboard setup.

## Security Recommendations

1. **Protect the first-run setup window** — first-time setup (`/setup` → create the
   admin account) is **unauthenticated by design** so it works on a fresh install,
   and it carries **no setup token** (removed in trinity-enterprise#49 to keep
   self-hosted bring-up frictionless). The endpoint self-disables the moment the
   admin account is created, but until then **anyone who can reach the URL can
   claim the admin account**. On an instance reachable by anyone other than you
   before setup completes (a public IP, a shared network), **keep it behind a
   tunnel/VPN or otherwise network-restricted until you have created the admin
   account.** On localhost / a trusted LAN this is a non-issue. After setup, login
   is fully authenticated and the window is closed.
2. **Never expose Redis externally** - Keep it internal only
3. **Use strong SECRET_KEY** - Generate with `openssl rand -hex 32`
4. **Use email whitelist** - Restrict access to approved email addresses only
5. **Regular backups** - Automate database backups
6. **Keep Docker updated** - Regular security patches
