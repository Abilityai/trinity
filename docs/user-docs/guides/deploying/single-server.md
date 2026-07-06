# Single-Server Deployment

Run Trinity on a Linux VPS or dedicated server with a stable public URL. This guide uses `docker-compose.prod.yml`, which disables hot-reload, adds health checks and restart policies to every service, and keeps Redis off the public network.

## Prerequisites

- Linux server (Ubuntu 22.04 LTS or later recommended), minimum 8 GB RAM
- Docker Engine 24+ and Docker Compose plugin (`docker compose` — no hyphen)
- A domain or subdomain pointing to your server's IP (e.g., `trinity.your-domain.com`)
- `openssl` on the server for secret generation
- Outbound HTTPS access from the server (for Docker image pulls and Anthropic API calls)

## 1. Clone the Repository

```bash
git clone https://github.com/abilityai/trinity.git
cd trinity
```

## 2. Configure `.env`

```bash
cp .env.example .env
```

Edit `.env` and set the variables below. Every variable in this table is forwarded by `docker-compose.prod.yml` and is required for a working production deployment.

### Security-critical (must be set before first boot)

| Variable | How to generate | Notes |
|---|---|---|
| `SECRET_KEY` | `openssl rand -hex 32` | JWT signing key. Never reuse across instances. |
| `ADMIN_PASSWORD` | Choose a strong password | Minimum 12 characters. Drives both `admin` login and the MCP server's legacy auth path. **Required** — `docker-compose.prod.yml` refuses to render if unset (issue #692). |
| `CREDENTIAL_ENCRYPTION_KEY` | `openssl rand -hex 32` | Encrypts OAuth tokens, channel bot tokens, and subscription credentials. **If lost, all encrypted credentials become unrecoverable.** |
| `INTERNAL_API_SECRET` | `openssl rand -hex 32` | Authenticates scheduler-to-backend calls (C-003). |
| `REDIS_PASSWORD` | `openssl rand -hex 24` | Admin/`default` ACL user. Used for recovery and ad-hoc ops. |
| `REDIS_BACKEND_PASSWORD` | `openssl rand -hex 24` | Runtime ACL user for `backend` and `scheduler` containers. Embedded in `REDIS_URL` at compose render time. **Required** — compose refuses to render without it. |

Generate all six at once:

```bash
echo "SECRET_KEY=$(openssl rand -hex 32)"
echo "CREDENTIAL_ENCRYPTION_KEY=$(openssl rand -hex 32)"
echo "INTERNAL_API_SECRET=$(openssl rand -hex 32)"
echo "REDIS_PASSWORD=$(openssl rand -hex 24)"
echo "REDIS_BACKEND_PASSWORD=$(openssl rand -hex 24)"
```

Paste the output into `.env`.

### Redis security note

Trinity uses two separate Redis passwords by design. `REDIS_BACKEND_PASSWORD` is the runtime credential embedded in `REDIS_URL` for the `backend` and `scheduler` containers. Even if a platform container were compromised and this password leaked, it does **not** grant access to destructive Redis commands (`FLUSHALL`, `CONFIG`, `SHUTDOWN`, etc.) — those require `REDIS_PASSWORD`. See `docs/migrations/REDIS_AUTH.md` for details on the ACL design.

### Required for agent functionality

| Variable | Notes |
|---|---|
| `ANTHROPIC_API_KEY` | Required for agents to run Claude. Can be left blank and configured in Settings after login. |
| `GITHUB_PAT` | Required to clone private GitHub template repos. |

### Required for production access

| Variable | Notes |
|---|---|
| `FRONTEND_URL` | Your public-facing domain (e.g., `https://trinity.your-domain.com`). Used for OAuth redirect callbacks and email verification links. |
| `PUBLIC_CHAT_URL` | The externally reachable URL for public chat links and webhooks. Often the same as `FRONTEND_URL`. Leave blank if all users access via VPN. |

### Email authentication

Email login is enabled by default. Set at least one email provider:

| Variable | Notes |
|---|---|
| `EMAIL_PROVIDER` | `resend` (recommended), `sendgrid`, `smtp`, or `console` (logs codes — dev only) |
| `RESEND_API_KEY` | Required when `EMAIL_PROVIDER=resend`. Get from [resend.com](https://resend.com/api-keys). |
| `SENDGRID_API_KEY` | Required when `EMAIL_PROVIDER=sendgrid`. |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` | Required when `EMAIL_PROVIDER=smtp`. |
| `SMTP_FROM` | From address for verification emails (e.g., `noreply@your-domain.com`). |

### Optional integrations

| Variable | Notes |
|---|---|
| `SLACK_CLIENT_ID` / `SLACK_CLIENT_SECRET` / `SLACK_SIGNING_SECRET` | Slack OAuth and channel adapter. |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Google Workspace OAuth. |
| `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` | GitHub OAuth. |
| `NOTION_CLIENT_ID` / `NOTION_CLIENT_SECRET` | Notion OAuth. |
| `GEMINI_API_KEY` | Platform image generation. |

### Data path

The prod compose uses a bind-mount directory for `trinity.db` instead of a named Docker volume:

```
TRINITY_DATA_PATH=./trinity-data
```

The default `./trinity-data` is relative to the repo root. Use an absolute path on a server for clarity:

```
TRINITY_DATA_PATH=/srv/trinity-data
```

Create the directory before starting:

```bash
mkdir -p /srv/trinity-data
```

### Database backend

Trinity stores platform state in **SQLite** by default, in `trinity.db` under your `TRINITY_DATA_PATH` bind mount (`/data/trinity.db` inside the container). SQLite works with zero configuration — but **PostgreSQL is now the recommended backend for production**, and **SQLite support ends September 1, 2026** (after that date it stops receiving schema migrations and fixes).

To run on PostgreSQL, set one variable in `.env`:

```
DATABASE_URL=postgresql://trinity:your-postgres-password@your-db-host:5432/trinity
```

Both the backend and the scheduler pick it up. Notes for the prod compose:

- `docker-compose.prod.yml` ships **no bundled PostgreSQL service** — point `DATABASE_URL` at an operator-managed instance (a managed cloud database or your own PostgreSQL server). The bundled `--profile postgres` container exists only in the dev compose.
- Selection is non-sticky and non-destructive: comment `DATABASE_URL` out and the next restart is back on SQLite.
- A fresh PostgreSQL database is initialized automatically on first boot (Alembic-managed migrations).
- **Migrating an existing SQLite instance?** Use the Trinity Ops Agent's `/migrate-to-postgres` skill ([ops-agent guide](ops-agent.md)) — a validate-then-cutover flow that never writes to your SQLite file, so rollback is one line. New-instance setup details: `docs/POSTGRESQL_SETUP.md` in the repo.
- On PostgreSQL, back up with `pg_dump` instead of copying `trinity.db` — see [Backup and Restore](backup-and-restore.md).

On every backend boot, a versioned migration runner brings the schema up to date (the bespoke SQLite runner or Alembic for PostgreSQL). The runner is crash-safe and concurrency-safe: a cross-process lock serialises it so multiple workers and the scheduler cannot race each other, and table rebuilds run inside a transaction that rolls back cleanly on a mid-migration crash. If a migration is still pending or has failed, the backend's `/health` endpoint returns `503` with a `migrations` block (`applied`, `expected`, `first_pending`) naming the stuck migration — so a 503 from `curl http://localhost:8000/health` during an upgrade is actionable, not opaque.

## 3. Build the Base Agent Image

```bash
./scripts/deploy/build-base-image.sh
```

This builds `trinity-agent-base:latest` — the image every agent container inherits. Required before you can create any agents. Takes 5–10 minutes on first build.

## 4. Build and Start Platform Services

```bash
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d
```

This starts: `backend`, `frontend`, `redis`, `mcp-server`, `scheduler`, `vector`, and `otel-collector`.

The `cloudflared` tunnel service is **not started** by default — it requires an explicit `--profile tunnel` flag. See [public-access.md](public-access.md).

## 5. First Login

Open your domain in a browser. Log in with:
- **Username:** `admin`
- **Password:** the `ADMIN_PASSWORD` you set in `.env`

After login, go to **Settings → Email Whitelist** to allow team members to log in via email verification.

## 6. Connect from Claude Code

Create an MCP API key:
1. Log in to the web UI
2. Go to **Keys** in the top navigation
3. Create a new key and copy it

Then connect from your Claude Code session:

```bash
/trinity:connect
# URL: http://your-server:8080/mcp  (or https://trinity.your-domain.com/mcp if behind a reverse proxy)
# API Key: (your MCP API key)
```

## Restart vs. Down

> **Use `docker compose restart`, not `down/up`.** `docker compose down` removes the `trinity-agent-network`, which orphans every running agent container — they keep running but lose their network and have to be removed and recreated. `restart` preserves both the agents and the network. The only times to use `down` are: (1) intentional full teardown, (2) recovering from a corrupted compose state.

```bash
# Correct way to restart platform services
docker compose -f docker-compose.prod.yml restart backend frontend mcp-server scheduler

# Full stop (agents will need to be restarted/recreated)
docker compose -f docker-compose.prod.yml down
```

## Verify Service Health

After starting, verify all services are healthy:

```bash
# Backend
curl -s http://localhost:8000/health

# Scheduler
curl -s http://localhost:8001/health

# Frontend
curl -s -o /dev/null -w '%{http_code}' http://localhost

# Redis
docker exec trinity-redis redis-cli ping

# MCP Server
curl -s http://localhost:8080/health

# Vector
docker exec trinity-vector wget -q -O - http://localhost:8686/health
```

See [monitoring.md](monitoring.md) for the full monitoring guide.

## See Also

- [Public Access](public-access.md) — Cloudflare Tunnel for webhooks and public chat
- [Upgrading](upgrading.md) — How to update Trinity safely
- [Backup and Restore](backup-and-restore.md) — Protecting your database
- [Monitoring](monitoring.md) — Health checks and recovery patterns
- [Ops Agent](ops-agent.md) — Automated day-to-day operations
