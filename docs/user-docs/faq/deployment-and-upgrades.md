# Trinity FAQ — Deployment & Upgrades

> Part of the [Trinity FAQ](README.md). Short, grounded answers with links to the full documentation.

## What hardware and operating system do I need to run Trinity?

Locally you need Docker Desktop (version 24 or later recommended, which includes Docker Compose), Git, `openssl` on your PATH, and 8 GB of RAM available to Docker — Trinity runs entirely in containers, so nothing else is installed on the host. For a server, use a Linux VPS or dedicated machine (Ubuntu 22.04 LTS or later recommended) with Docker Engine 24+, the Docker Compose plugin, and a minimum of 8 GB RAM. See [Local Development](../guides/deploying/local-development.md) and [Single-Server Deployment](../guides/deploying/single-server.md).

## How many agents can I run on one machine?

RAM is the practical ceiling — Trinity itself doesn't cap fleet size. Each agent runs as its own Docker container with a configurable memory limit (options range from 1g to 64g, and an admin can set fleet-wide defaults for new containers), so budget your host memory against the limits you assign. The docs recommend 8 GB minimum and 16 GB for running multiple agents; watch host memory and CPU against the thresholds in the monitoring guide as your fleet grows. See [Agent Configuration](../agents/agent-configuration.md) and [Monitoring](../guides/deploying/monitoring.md).

## Should I deploy Trinity locally, on a single server, or with public access?

Use local development for experimentation: the dev compose bind-mounts source code, so backend and frontend changes hot-reload without rebuilds. Use single-server for production on a Linux VPS: `docker-compose.prod.yml` disables hot-reload, adds health checks and restart policies, and keeps Redis off the public network. Add public access (a Cloudflare Tunnel on top of a single-server deployment) only when you need external webhooks, public chat links, or off-VPN access for your team. See [Local Development](../guides/deploying/local-development.md), [Single-Server Deployment](../guides/deploying/single-server.md), and [Public Access](../guides/deploying/public-access.md).

## Can I run Trinity on a cheap VPS?

Yes — any Linux VM that runs Docker works, and the ops agent repo ships provisioning guides for Hetzner Cloud, GCP, AWS, DigitalOcean, and localhost in its `provision/` directory. Plan for 8 GB of RAM if you intend to run a real agent fleet, plus enough disk for Docker images, logs, and the database. Follow the single-server guide once the VM is provisioned. See [Single-Server Deployment](../guides/deploying/single-server.md) and [Ops Agent](../guides/deploying/ops-agent.md).

## What ports does Trinity use?

The frontend web UI binds host port 80, the backend API 8000, the MCP server 8080, the scheduler 8001 (health endpoint only), and Vector (log aggregation) 8686. Redis binds 6379 on loopback only (127.0.0.1), so it is never reachable from outside the host, and agent SSH access uses incrementing ports 2222–2262. See [Local Development](../guides/deploying/local-development.md).

## How do I change the port the web UI runs on?

Add `FRONTEND_PORT=8090` (or any free port) to `.env` and restart. Only the host-side mapping is tunable — the port inside the container is fixed. This is the fix when `docker compose up` fails with "port is already allocated" because another web server already holds port 80. See [Local Development](../guides/deploying/local-development.md).

## Which environment variables do I have to set myself, and which does start.sh generate?

You must set `ADMIN_PASSWORD` yourself — `start.sh` refuses to boot without it, because a generated password would lock you out of your own instance. If left blank, the script auto-generates `SECRET_KEY`, `INTERNAL_API_SECRET`, `CREDENTIAL_ENCRYPTION_KEY`, and `AGENT_AUTH_SECRET`, generates the two Redis passwords on a fresh install (it refuses if a populated Redis volume already exists), auto-detects `DOCKER_GID`, and even copies `.env.example` to `.env` if the file is missing. For production, the single-server guide recommends generating all secrets explicitly with `openssl rand` before first boot, and you'll also want `ANTHROPIC_API_KEY`, an email provider, and `FRONTEND_URL`/`PUBLIC_CHAT_URL`. See [Single-Server Deployment](../guides/deploying/single-server.md).

## What happens if I lose or change my credential encryption key?

All encrypted credentials — OAuth tokens, channel bot tokens (Slack, Telegram, WhatsApp), and subscription credentials — become permanently unrecoverable. Once `CREDENTIAL_ENCRYPTION_KEY` is set, never edit or delete it casually; a deliberate key rotation is possible via a decrypt-only secondary key and the `scripts/deploy/rotate-credential-key.py` runbook, but that's a planned procedure, not a config tweak. This is also why backing up `.env` alongside the database matters — the file is gitignored, so losing the host means losing the key. See [Backup and Restore](../guides/deploying/backup-and-restore.md).

## How do I upgrade Trinity safely?

Back up the database first, then: `git pull origin main`, rebuild the platform images with `docker compose build --no-cache backend frontend mcp-server scheduler`, restart them with `docker compose restart backend frontend mcp-server scheduler`, and run the six health probes. Agent containers keep running throughout — only platform services restart. Confirm the new build is actually live with `curl http://localhost:8000/api/version`, which reports the git commit baked into the image. See [Upgrading](../guides/deploying/upgrading.md).

## Why should I use docker compose restart instead of down and up?

`docker compose down` removes the agent network along with the platform containers, which orphans every running agent — the agents keep running but lose their network and have to be removed and recreated. `docker compose restart` (or `docker compose stop`) preserves both the agents and the network. Reserve `down` for two cases only: an intentional full teardown, or recovering from a corrupted compose state. See [Upgrading](../guides/deploying/upgrading.md).

## Why can't I start any agents after running docker compose down?

The `down` removed the `trinity-agent-network`, so new agent containers have nothing to attach to and you'll see "network not found" errors. Run `docker compose up -d` (with `-f docker-compose.prod.yml` in production) to recreate the missing network while leaving running containers intact. If a specific orphaned agent still won't start, remove its container with `docker rm <agent-container-name>` and restart the backend so it recreates the agent cleanly. See [Monitoring](../guides/deploying/monitoring.md).

## When do I need to rebuild the agent base image?

Only when `docker/base-image/Dockerfile` changes in the code you pulled — run `./scripts/deploy/build-base-image.sh` in that case. The normal upgrade command (`docker compose build --no-cache backend frontend mcp-server scheduler`) deliberately does not touch the base image, because it changes rarely and rebuilding it means every agent must be recreated to benefit. After a base-image rebuild, existing agents keep running on the old image until you stop and recreate them individually — there is no automatic roll-forward. See [Upgrading](../guides/deploying/upgrading.md).

## How do I back up my Trinity instance?

The database is the primary target. On a dev install (named volume), copy it out with a throwaway container: `docker run --rm -v trinity_trinity-data:/data -v ~/backups:/backup alpine cp /data/trinity.db /backup/trinity-$(date +%Y%m%d-%H%M%S).db` — this works while services are running. On a production server the database is a bind-mounted file, so a plain `cp /srv/trinity-data/trinity.db ~/backups/...` does the job; PostgreSQL deployments use `pg_dump` instead. Also back up `.env` (it holds your encryption key) — agent code lives in git and Redis data is ephemeral, so neither needs separate backups. See [Backup and Restore](../guides/deploying/backup-and-restore.md).

## How often should I back up, and how long should I keep backups?

Back up before every upgrade and every destructive operation, and daily on production instances via cron. The documented cron job runs at 03:00, writes a timestamped copy of `trinity.db`, and deletes backups older than 14 days: `0 3 * * * docker run --rm -v trinity_trinity-data:/data -v ~/backups:/backup alpine cp /data/trinity.db /backup/trinity-$(date +\%Y\%m\%d-\%H\%M\%S).db && find ~/backups -name "trinity-*.db" -mtime +14 -delete`. Verify a backup is readable with `sqlite3 <file> ".tables"`. See [Backup and Restore](../guides/deploying/backup-and-restore.md).

## How do I restore from a backup?

Stop the writers first — `docker compose stop backend scheduler` — to release the SQLite write lock. Copy the backup file back into place (the same alpine-container `cp` pattern in reverse for a named volume, or a plain `cp` onto the bind-mount path in production), then `docker compose start backend scheduler` and verify with `curl http://localhost:8000/health` and `curl http://localhost:8001/health`. PostgreSQL deployments restore with `psql` into an empty database instead. See [Backup and Restore](../guides/deploying/backup-and-restore.md).

## Where does Trinity actually store my data?

Platform state lives in `trinity.db`: in the named Docker volume `trinity_trinity-data` on a dev install, or in the bind-mount directory set by `TRINITY_DATA_PATH` (default `./trinity-data`; an absolute path like `/srv/trinity-data` is recommended) in production. It holds agents, schedules, chat history, user accounts, the audit log, and encrypted channel tokens. Each agent additionally gets its own Docker volumes — a durable home/workspace volume that survives container recreation — while Redis holds only ephemeral runtime state and agent source code lives in git. All of it survives `docker compose down`/`up` cycles. See [Backup and Restore](../guides/deploying/backup-and-restore.md).

## Should I use SQLite or PostgreSQL?

Both work today. SQLite is the zero-config default and fine for local development, but PostgreSQL is the recommended backend for production — and SQLite support ends September 1, 2026, after which it stops receiving schema migrations and fixes. Switching a fresh instance is one `.env` variable (`DATABASE_URL=postgresql://trinity:your-postgres-password@your-db-host:5432/trinity`); the dev compose bundles a PostgreSQL container behind `--profile postgres`, while production expects an operator-managed database. Migrating an existing instance's data is a deliberate cutover — the Trinity Ops Agent has a dedicated migration skill for it, and a full migration guide ships in the repo under `docs/migrations/`. See [Single-Server Deployment](../guides/deploying/single-server.md#database-backend).

## How do I expose Trinity to the internet for webhooks and public chat links?

Use the built-in Cloudflare Tunnel support: create a tunnel in the Cloudflare Zero Trust dashboard, set `TUNNEL_TOKEN` and `PUBLIC_CHAT_URL=https://public.your-domain.com` in `.env`, configure the documented path-prefix ingress rules, and start with `docker compose -f docker-compose.prod.yml --profile tunnel up -d`. The tunnel container makes an outbound connection to Cloudflare's edge, so you never open inbound firewall ports or need a static IP. Once connected, Trinity constructs Telegram, WhatsApp, and schedule-webhook URLs from `PUBLIC_CHAT_URL` automatically and shows them in the UI. See [Public Access](../guides/deploying/public-access.md).

## Does the whole platform need to be exposed publicly?

No — and by default none of it is. Public exposure is only required for inbound webhooks (Slack, Telegram, WhatsApp/Twilio), public chat links, paid chat, the agent website proxy, and off-VPN access for team members or MCP clients; if all your users and webhook sources can reach the server directly (for example over a VPN), you don't need the tunnel at all. The recommended ingress rules whitelist specific path prefixes, so anything not listed returns 404 at Cloudflare's edge before reaching your server, and Redis stays loopback-only regardless. See [Public Access](../guides/deploying/public-access.md).

## What is the Trinity Ops Agent, and when should I use it instead of raw Docker commands?

It's a Claude Code agent (from the public `trinity-ops-public` repo) for operating any Trinity instance: health checks, log triage, restarts, updates, backups, rollbacks, and agent management, driven by a single `.env` that points at localhost or at a remote server over SSH. Use it for day-to-day operations — its scripts codify the runbooks, like always restarting with `docker compose restart` and never rebuilding the base image during a routine update. Fall back to raw Docker only for one-off debugging or when the ops agent itself is unavailable. See [Ops Agent](../guides/deploying/ops-agent.md).

## How do I verify my deployment is healthy after an upgrade or restart?

Run the six-probe health check: backend (`curl http://localhost:8000/health`), scheduler (`curl http://localhost:8001/health`), frontend (expect HTTP 200), Redis (`docker exec trinity-redis redis-cli ping` → `PONG`), MCP server (`curl http://localhost:8080/health`), and Vector (`http://localhost:8686/health`). All six must pass before you declare the change complete; `./scripts/deploy/verify-platform.sh` runs them for you. After an upgrade, also check `curl http://localhost:8000/api/version` to confirm the running build matches what you deployed. See [Monitoring](../guides/deploying/monitoring.md).

## Why is Docker using so much CPU when I run Trinity locally on Docker Desktop?

Vector's default Docker log source misbehaves on Docker Desktop and other VM-based Docker runtimes: the virtualized log relay keeps closing its follow streams, and the resulting reconnect storm can peg the Docker VM's CPU. Native Linux servers are unaffected. `start.sh` handles this automatically — when it detects Docker Desktop it creates a `docker-compose.override.yml` that switches Vector to an on-disk file source (local logs then land in `/data/logs/local-*.json`); you can force the behavior with `TRINITY_LOCAL_LOG_SOURCE=file` or opt out with `TRINITY_LOCAL_LOG_SOURCE=docker`. See [Local Development](../guides/deploying/local-development.md).

## Why do I have to log in again after restarting the backend?

JWT tokens are invalidated whenever the backend restarts, so every web UI session must log in again — this is expected after any upgrade or restart, not a bug. MCP clients such as Claude Code also need to reconnect: run `/mcp` in your session or restart the client. See [Monitoring](../guides/deploying/monitoring.md).
