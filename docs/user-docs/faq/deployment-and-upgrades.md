# Trinity FAQ — Deployment & Upgrades

> Part of the [Trinity FAQ](README.md). Short, grounded answers with links to the full documentation.

## What hardware and operating system do I need to run Trinity?

Locally you need Docker Desktop (version 24 or later recommended, which includes Docker Compose), Git, `openssl` on your PATH, and 8 GB of RAM available to Docker — Trinity runs entirely in containers, so nothing else is installed on the host. For a server, use a Linux VPS or dedicated machine (Ubuntu 22.04 LTS or later recommended) with Docker Engine 24+, the Docker Compose plugin, a minimum of 8 GB RAM, and outbound HTTPS (image pulls and model API calls). See [Local Development](../guides/deploying/local-development.md) and [Single-Server Deployment](../guides/deploying/single-server.md).

## How many agents can I run on one machine?

RAM is the practical ceiling — Trinity itself doesn't cap fleet size. Each agent runs as its own Docker container with a configurable memory limit (options range from 1g to 64g, and an admin can set fleet-wide defaults for new containers), so budget your host memory against the limits you assign. The docs recommend 8 GB minimum and 16 GB for running multiple agents; watch host memory and CPU against the thresholds in the monitoring guide as your fleet grows. See [Agent Configuration](../agents/agent-configuration.md) and [Monitoring](../guides/deploying/monitoring.md).

## Should I deploy Trinity locally, on a single server, or with public access?

There are four self-hosted paths, all driven by the same installer and `.env`. Local, build from source (`./scripts/deploy/start.sh`) is for your own machine — the dev compose bind-mounts source so backend and frontend changes hot-reload. On a server, the recommended path is prebuilt images (`./scripts/deploy/start.sh --hosted`), which pulls every platform image and the agent base image from GHCR and is serving in about two minutes; build from source with `docker compose -f docker-compose.prod.yml` only if you carry local patches or the enterprise overlay. The DigitalOcean Marketplace 1-Click is the prebuilt path baked into a Droplet image. Add public access (a Cloudflare Tunnel on top of a server install) only when you need external webhooks, public chat links, or off-VPN access. See [Deploying Trinity](../guides/deploying-trinity.md).

## Can I install Trinity from prebuilt images instead of building them myself?

Yes. Clone the repo, fill in `.env`, add `TRINITY_IMAGE_TAG=v0.9.0` (the release you want), and run `./scripts/deploy/start.sh --hosted --unattended`. `--hosted` swaps in `docker-compose.hosted.yml` — the production compose with every `build:` replaced by a GHCR image — and pulls the agent base image too, tagging it locally as `trinity-agent-base:latest`; nothing is compiled on the box. The checkout still has to stay beside the compose file, because it mounts `config/` (templates, manifests, Vector and OTel configs) from the repo. A failed pull is fatal rather than falling back to a build, and every day-two `docker compose` command on a hosted install needs `-f docker-compose.hosted.yml` (`stop.sh` and `start.sh --hosted` pick the file for you). See [Single-Server Deployment → Prebuilt images](../guides/deploying/single-server.md#option-a-prebuilt-images-recommended).

## Why did re-running start.sh --hosted upgrade my instance?

Because `TRINITY_IMAGE_TAG` was unset or `latest`. Every release re-points `latest`, so an unpinned hosted install turns each re-run of `start.sh --hosted` into an unscheduled upgrade of the platform *and* the agent base image. Pin a version in `.env` (`echo 'TRINITY_IMAGE_TAG=v0.9.0' >> .env` — the last line wins) on any install you intend to keep; each release publishes `v0.9.0`, `0.9.0`, `0.9`, `latest`, and `sha-<short>` for one digest, and a pre-release tag such as `v0.9.5-rc1` publishes its own tags but never moves `latest`. See [Upgrading](../guides/deploying/upgrading.md).

## How does the DigitalOcean Marketplace 1-Click work, and where is my admin password?

Create a Droplet from the Trinity image (4 GB RAM minimum, 8 GB recommended). First boot runs once with no input: it generates a 24-character admin password, writes `/opt/trinity/.env`, locks the firewall down to 22/80/443, brings up Caddy with a Let's Encrypt certificate for the Droplet's own IP, and runs `start.sh --hosted --unattended` from images already baked into the snapshot — Trinity is serving over HTTPS about ninety seconds later. The password is printed in the login banner (Droplet → Console for password auth, or `ssh root@<droplet-ip>` if you chose SSH keys) and can be re-read with `cat /etc/trinity/admin-credentials`. Your data lives in `/opt/trinity/trinity-data/` (database and automatic backups); after first login an admin sees a **Secure this instance** card on the Dashboard that walks through adding a real domain and, optionally, a Cloudflare Tunnel. See [Single-Server Deployment → DigitalOcean 1-Click](../guides/deploying/single-server.md#digitalocean-marketplace-1-click).

## Can I run Trinity on a cheap VPS?

Yes — any Linux VM that runs Docker works, and the ops agent repo ships provisioning guides for Hetzner Cloud, GCP, AWS, DigitalOcean, and localhost in its `provision/` directory. With prebuilt images (`start.sh --hosted`) a fresh VM is serving in about two minutes. Plan for 8 GB of RAM if you intend to run a real agent fleet (the DigitalOcean image boots on 4 GB for one or two agents), plus enough disk for Docker images, logs, and the database. See [Single-Server Deployment](../guides/deploying/single-server.md) and [Ops Agent](../guides/deploying/ops-agent.md).

## Does Trinity run on ARM hosts?

Building from source does: the agent base image build installs the Claude Code native binary for the host's own architecture as a required step and fails the build rather than shipping an image with a dead CLI, so an arm64 host gets a working base image from `./scripts/deploy/build-base-image.sh`. The prebuilt GHCR images are published for `linux/amd64` only, so `start.sh --hosted` and the DigitalOcean 1-Click are x86-64 paths — on an ARM server, build from source. See [Single-Server Deployment → Build from source](../guides/deploying/single-server.md#option-b-build-from-source).

## Which docker compose files go together?

Trinity ships three complete, standalone stacks: `docker-compose.yml` (dev, source build, named data volume), `docker-compose.prod.yml` (server, source build, bind-mounted data), and `docker-compose.hosted.yml` (server, prebuilt images, bind-mounted data). Never stack the dev file under prod or hosted — Compose concatenates list keys, so the combination either fails validation or silently gives the frontend two port mappings — and never run a bare `docker compose up -d` on a production host, because that loads the dev file and boots a healthy-looking backend on an empty database while your real one sits untouched in `TRINITY_DATA_PATH`. `start.sh` (and `./quickstart.sh`, which is now just an alias for it) refuses that crossing in both directions and prints the file set the host was installed with. The other compose files are narrow overlays, not a fourth stack. See [Single-Server Deployment → Which compose files go together](../guides/deploying/single-server.md#which-compose-files-go-together).

## What ports does Trinity use?

The frontend web UI binds host port 80, the backend API 8000, the MCP server 8080, and Vector (log aggregation) 8686. Redis binds 6379 on loopback only (127.0.0.1), so it is never reachable from outside the host, and agent SSH access uses incrementing ports 2222–2262. The scheduler's health endpoint on 8001 is *not* published to the host by any compose file — probe it with `docker exec trinity-scheduler curl -sf http://localhost:8001/health`. See [Local Development](../guides/deploying/local-development.md).

## How do I change the port the web UI runs on?

Add `FRONTEND_PORT=8090` (or any free port) to `.env` and restart. Only the host-side mapping is tunable — the port inside the container is fixed, and the setting is honoured by the dev, prod, and hosted compose files alike. This is the fix when `docker compose up` fails with "port is already allocated" because another web server already holds port 80; `start.sh` warns about busy ports before it starts. See [Local Development](../guides/deploying/local-development.md).

## Which environment variables do I have to set myself, and which does start.sh generate?

You must set `ADMIN_PASSWORD` yourself — `start.sh` refuses to boot while it is blank, because a generated password would lock you out of your own instance (under `--unattended` it generates one and prints it once). If left blank, the script auto-generates `SECRET_KEY`, `INTERNAL_API_SECRET`, `CREDENTIAL_ENCRYPTION_KEY`, and `AGENT_AUTH_SECRET`, generates the two Redis passwords on a fresh install (it refuses if a populated Redis volume already exists), auto-detects `DOCKER_GID`, and even copies `.env.example` to `.env` if the file is missing. For a source-built production install (`docker-compose.prod.yml` directly, without `start.sh`) generate all secrets explicitly with `openssl rand` before first boot; either way you'll also want `ANTHROPIC_API_KEY`, an email provider, and `FRONTEND_URL`/`PUBLIC_CHAT_URL`. See [Single-Server Deployment](../guides/deploying/single-server.md).

## How do I change the admin password?

Edit `ADMIN_PASSWORD` in `.env` and restart the backend (`docker compose restart backend`, with `-f docker-compose.prod.yml` or `-f docker-compose.hosted.yml` on a server). `.env` is the source of truth: the backend re-applies that value on every start, so a password changed anywhere else is overwritten at the next boot, and there is no change-password form in the UI. On a DigitalOcean Droplet the file is `/opt/trinity/.env`. See [First-Time Setup](../getting-started/setup.md).

## Can I have an AI agent install Trinity for me, or run a no-prompt install?

Yes. Run `./scripts/deploy/start.sh --unattended` (or set `TRINITY_UNATTENDED=1`; `./quickstart.sh --defaults` is the same thing) for a no-prompt install: the happy path never stops to ask for input, and the script auto-generates the `admin` password (and any missing secrets), then prints the admin password in the final summary. Save it — it is stored in `.env` and shown only once. Combine with `--hosted` on a server for a pull-only unattended install. You can also have Claude Code drive the entire local install for you: point it at the versioned runbook at `docs/AGENT_INSTALL_GUIDE.md`, a deterministic step-by-step guide that stays in sync with the installer. See [Local Development](../guides/deploying/local-development.md).

## What happens if I lose or change my credential encryption key?

All encrypted credentials — OAuth tokens, channel bot tokens (Slack, Telegram, WhatsApp), and subscription credentials — become permanently unrecoverable. Once `CREDENTIAL_ENCRYPTION_KEY` is set, never edit or delete it casually; a deliberate key rotation is possible via a decrypt-only secondary key and the `scripts/deploy/rotate-credential-key.py` runbook, but that's a planned procedure, not a config tweak. This is also why backing up `.env` alongside the database matters — the file is gitignored, so losing the host means losing the key. See [Backup and Restore](../guides/deploying/backup-and-restore.md).

## How do I upgrade Trinity safely?

Back up the database first (the platform also takes a pre-migration copy at boot, but a copy you can name is the one to roll back to). On a source install: `git pull origin main`, rebuild the platform images with `docker compose build --no-cache backend frontend mcp-server scheduler`, restart them with `docker compose restart backend frontend mcp-server scheduler`, and run the six health probes. On a hosted install: pin the new release (`echo 'TRINITY_IMAGE_TAG=v0.9.1' >> .env`), `git fetch --tags && git checkout v0.9.1` so the compose files and mounted `config/` move with the images, then re-run `./scripts/deploy/start.sh --hosted` — never a bare `docker compose pull`, which skips the agent base image. Agent containers keep running throughout; confirm the new build is live with `curl http://localhost:8000/api/version`. One post-upgrade item for older installs: if you upgraded past the change that encrypts platform credential settings at rest, every pre-upgrade backup still holds those tokens (Anthropic key, GitHub PAT, Google key, Slack secrets) in plaintext, so rotate them — see the [runbook](../../migrations/SECRET_SETTINGS_ENCRYPTION_2026-08.md). See [Upgrading](../guides/deploying/upgrading.md).

## Why should I use docker compose restart instead of down and up?

`docker compose down` removes the `trinity-agent-network` along with the platform containers, and because agent containers carry Docker's `unless-stopped` restart policy, every agent then sits in a restart loop against a network that no longer exists. `docker compose restart` (or `docker compose stop`) preserves both the agents and the network. `./scripts/deploy/stop.sh` runs `stop`, never `down`, and reads the running stack's label to add `-f docker-compose.hosted.yml` when needed (it does not detect a source-built prod stack — use the explicit `-f docker-compose.prod.yml stop` there). Reserve `down` for an intentional full teardown or recovering from a corrupted compose state. See [Upgrading](../guides/deploying/upgrading.md).

## Why can't I start any agents after running docker compose down?

The `down` removed the `trinity-agent-network`, so new agent containers have nothing to attach to ("network not found"), and existing agents loop in `Restarting` while the roster shows them as stopped. Run `docker compose up -d` (with `-f docker-compose.prod.yml` or `-f docker-compose.hosted.yml` on a server) to recreate the missing network while leaving running containers intact, then `docker rm -f` each stale agent container and start it again from the UI (the agent's Start button, or Operations → Restart All). The agent's workspace volume is untouched by the container removal. See [Monitoring → Recovery Patterns](../guides/deploying/monitoring.md#recovery-patterns).

## Do my agents come back after the host reboots?

Yes. Every platform service and every agent container Trinity creates carries Docker's `unless-stopped` restart policy, so a host reboot or Docker daemon restart brings the whole fleet back and schedules resume without anyone starting agents by hand. An agent you stopped on purpose (Stop, quarantine, emergency stop) stays stopped — the policy honours a manual stop, which is the difference from `always`. Agents created before this policy shipped keep Docker's default (`no`) until they are recreated; the upgrade guide has a one-shot `docker update --restart unless-stopped` sweep to fix an existing fleet in place. See [Upgrading → Agent restart policy](../guides/deploying/upgrading.md#agent-restart-policy-one-time-after-upgrading).

## When do I need to rebuild the agent base image?

Only when `docker/base-image/Dockerfile` changes in the code you pulled — run `./scripts/deploy/build-base-image.sh` in that case. The normal source upgrade command (`docker compose build --no-cache backend frontend mcp-server scheduler`) deliberately does not touch the base image, because it changes rarely and rebuilding it means every agent must be recreated to benefit. On a hosted install there is no separate step: `start.sh --hosted` re-pulls the release's base image and retags it as `trinity-agent-base:latest` on every run. Either way, existing agents adopt the new image at their next cold start, not automatically. See [Upgrading](../guides/deploying/upgrading.md).

## How do I back up my Trinity instance?

Trinity backs up its own database: nightly at 03:30 UTC plus a `pre-migration-<timestamp>.db` copy at boot whenever a schema migration is about to run, into `backups/` under the data directory (`/data/backups/` inside the backend container — the `trinity_trinity-data` volume on a dev install, `TRINITY_DATA_PATH` on a server, `/opt/trinity/trinity-data/backups/` on a DigitalOcean Droplet). Each artifact is a consistent online copy (`pg_dump -Fc` on PostgreSQL), verified before it is kept, and pruned after `backup_retention_days` (default 14, newest three always kept). For an extra copy before something risky, use SQLite's online backup — never a plain `cp` of a live database, which can be torn by a write in flight. Also back up `.env` yourself (it holds your encryption key); agent code lives in git and Redis data is ephemeral. See [Backup and Restore](../guides/deploying/backup-and-restore.md).

## Do I still need a backup cron job, and what do backup-database.sh and restore-database.sh do?

No cron job — the built-in nightly job replaced the crontab recipe the docs used to suggest. What you should still automate is shipping artifacts off-host: they live on the same disk as the database, so they protect against corruption and mistakes, not against losing the disk (an `rsync` of `backups/` after the 03:30 UTC run, or your provider's disk snapshots). Check status without a shell via `GET /api/settings/retention` → `backup`; a failed or stale backup raises an item in Operations → Needs Response. The two repo scripts serve one topology only — a GCP VM managed from a workstation with `gcloud` and a `deploy.config`: `backup-database.sh` does not create a backup, it downloads the newest automatic artifact and verifies it locally, and `restore-database.sh` uploads a SQLite file, stops the writers, clears stale journal sidecars, copies it into place, and restarts. See [Backup and Restore](../guides/deploying/backup-and-restore.md).

## How do I restore from a backup?

Stop both writers first — `docker compose stop backend scheduler` (with the `-f` flag for your server compose file) — to release the SQLite write lock. Remove any stale `-wal`/`-shm`/`-journal` sidecars beside the target (SQLite would otherwise replay a journal that belongs to the old database), copy the artifact into place (the alpine-container `cp` pattern for a named volume, or a plain `cp` onto the bind-mount path on a server), then `docker compose start backend scheduler` and verify with `curl http://localhost:8000/health` and `docker exec trinity-scheduler curl -sf http://localhost:8001/health`. PostgreSQL deployments `pg_restore` the `-Fc` dump into an empty database instead. See [Backup and Restore → Recovery](../guides/deploying/backup-and-restore.md#recovery-restore).

## Where does Trinity actually store my data?

Platform state lives in `trinity.db`: in the named Docker volume `trinity_trinity-data` on a dev install, or in the bind-mount directory set by `TRINITY_DATA_PATH` (default `./trinity-data` in the checkout; an absolute path like `/srv/trinity-data` is recommended, and the DigitalOcean image uses `/opt/trinity/trinity-data`) on prod and hosted installs. It holds agents, schedules, chat history, user accounts, the audit log, encrypted channel tokens, and the automatic `backups/`. Each agent additionally gets its own Docker volumes — a durable home/workspace volume that survives container recreation — while Redis holds only ephemeral runtime state and agent source code lives in git. All of it survives `docker compose down`/`up` cycles (only the agent network does not). See [Backup and Restore](../guides/deploying/backup-and-restore.md).

## How long does Trinity keep my data, and does upgrading change it?

Retention windows are configurable per data type — execution logs and rows, health checks, agent reports, terminal operator-queue items, and soft-deleted agents and schedules — under Settings → Retention, and any window can be set to `0` to disable that sweep. Every install stores its windows as explicit per-install settings, written at the values already in force the first time a release boots without them, so an upgrade never widens or narrows what your instance keeps; fresh community installs are seeded with a 5-day minimum on the log windows (the agent soft-delete window is exempt because its purge destroys data volumes). A blast-radius guard blocks any sweep that would delete more than a fixed threshold of a table's rows until an admin approves it. See [Monitoring](../operations/monitoring.md).

## Should I use SQLite or PostgreSQL?

Both work today. SQLite is the zero-config default and fine for local development, but PostgreSQL is the recommended backend for production — and SQLite support ends September 1, 2026, after which it stops receiving schema migrations and fixes. Switching a fresh instance is one `.env` variable (`DATABASE_URL=postgresql://trinity:your-postgres-password@your-db-host:5432/trinity`); the dev compose bundles a PostgreSQL container behind `--profile postgres`, while production expects an operator-managed database. Migrating an existing instance's data is a deliberate cutover — the Trinity Ops Agent has a dedicated migration skill for it, and a full migration guide ships in the repo under `docs/migrations/`. See [Single-Server Deployment](../guides/deploying/single-server.md#database-backend).

## How do I get HTTPS on a plain VM?

Trinity serves plain HTTP and terminates TLS outside the application — no compose file carries an HTTPS listener or a certificate step. Pick one posture before putting an instance on a public address: a Cloudflare Tunnel (HTTPS at a real hostname with no inbound ports open — the default for a public instance), a private network such as Tailscale or WireGuard (HTTP over an encrypted tunnel is a finished posture, but inbound integrations like Telegram or webhooks need a public hostname), or a reverse proxy you run (Caddy or nginx with Let's Encrypt). Plain HTTP on a public IP with none of these is the one combination to avoid — credentials and session tokens cross the network in the clear. The DigitalOcean 1-Click is the deliberate exception: it ships its own Caddy with a short-lived certificate for the Droplet's IP. See [Single-Server Deployment → TLS on a bare VM](../guides/deploying/single-server.md#tls-on-a-bare-vm).

## How do I expose Trinity to the internet for webhooks and public chat links?

Use the built-in Cloudflare Tunnel support: create a tunnel in the Cloudflare Zero Trust dashboard, set `TUNNEL_TOKEN` and `PUBLIC_CHAT_URL=https://public.your-domain.com` in `.env`, and route the hostname to `http://trinity-frontend:8080` — the production frontend's nginx already proxies `/api/`, `/mcp`, `/ws`, and `/health`, so one catch-all rule serves the UI, every webhook, and MCP (narrower path-split rules are documented if you want only the webhook surface). On a hosted install `start.sh --hosted` starts the tunnel profile itself when `TUNNEL_TOKEN` is set; on a source build pass `--profile tunnel` explicitly. The tunnel container makes an outbound connection to Cloudflare's edge, so you never open inbound firewall ports, and Trinity then constructs Telegram, WhatsApp, and schedule-webhook URLs from `PUBLIC_CHAT_URL` automatically. See [Public Access](../guides/deploying/public-access.md).

## Does the whole platform need to be exposed publicly?

No — and by default none of it is. Public exposure is only required for inbound webhooks (Slack, Telegram, WhatsApp/Twilio), public chat links, paid chat, the agent website proxy, and off-VPN access for team members or MCP clients; if all your users and webhook sources can reach the server directly (for example over a VPN), you don't need the tunnel at all. Path-split ingress rules can whitelist specific prefixes so anything not listed returns 404 at Cloudflare's edge before reaching your server, and Redis stays loopback-only regardless. See [Public Access](../guides/deploying/public-access.md).

## Can MCP clients connect if only ports 80 and 443 are open?

Yes. The production frontend proxies `/mcp` to the MCP server, so an install that exposes only 80/443 — a tunnel, a reverse proxy, the DigitalOcean 1-Click — serves MCP at the same hostname as the web UI: point Claude Code at `https://trinity.your-domain.com/mcp` with an MCP API key from Settings → MCP Keys (port 8080 also works wherever it is reachable). **Settings → MCP Keys → MCP Server URL** sets the URL the UI shows to users; leave it empty to derive it from the hostname. The dev Vite server does not proxy `/mcp` — locally the endpoint is `http://localhost:8080/mcp`. See [Single-Server Deployment → Connect from Claude Code](../guides/deploying/single-server.md#connect-from-claude-code).

## What is the Trinity Ops Agent, and when should I use it instead of raw Docker commands?

It's a Claude Code agent (from the public `trinity-ops-public` repo) for operating any Trinity instance: health checks, log triage, restarts, updates, backups, rollbacks, and agent management, driven by a single `.env` that points at localhost or at a remote server over SSH. Use it for day-to-day operations — its scripts codify the runbooks, like always restarting with `docker compose restart` and never rebuilding the base image during a routine update. One caveat: its `/update` skill builds from source, so on a hosted install (prebuilt images, including the DigitalOcean 1-Click) upgrade with `start.sh --hosted` instead; health checks, logs, restarts, and backups work the same on either. See [Ops Agent](../guides/deploying/ops-agent.md).

## How do I verify my deployment is healthy after an upgrade or restart?

Run the six-probe health check: backend (`curl http://localhost:8000/health`), scheduler (`docker exec trinity-scheduler curl -sf http://localhost:8001/health` — the port is not published to the host), frontend (expect HTTP 200), Redis (`docker exec trinity-redis redis-cli ping` → `PONG`), MCP server (`curl http://localhost:8080/health`), and Vector (`http://localhost:8686/health`). All six must pass before you declare the change complete; `./scripts/deploy/verify-platform.sh` runs them for you and treats a failed scheduler probe as a warning. A `503` from the backend with a `migrations` block names a pending or failed schema migration, not a dead backend. After an upgrade, also check `curl http://localhost:8000/api/version` to confirm the running build matches what you deployed. See [Monitoring](../guides/deploying/monitoring.md).

## Why is Docker using so much CPU when I run Trinity locally on Docker Desktop?

Vector's default Docker log source misbehaves on Docker Desktop and other VM-based Docker runtimes: the virtualized log relay keeps closing its follow streams, and the resulting reconnect storm can peg the Docker VM's CPU. Native Linux servers are unaffected. `start.sh` handles this automatically — when it detects Docker Desktop it creates a `docker-compose.override.yml` that switches Vector to an on-disk file source (local logs then land in `/data/logs/local-*.json`); you can force the behavior with `TRINITY_LOCAL_LOG_SOURCE=file` or opt out with `TRINITY_LOCAL_LOG_SOURCE=docker`. See [Local Development](../guides/deploying/local-development.md).

## Why do I have to log in again after restarting the backend?

JWT tokens are invalidated whenever the backend restarts, so every web UI session must log in again — this is expected after any upgrade or restart, not a bug. MCP clients such as Claude Code also need to reconnect: run `/mcp` in your session or restart the client. See [Monitoring](../guides/deploying/monitoring.md).

## After rebuilding the base image, how do agents pick it up?

At each agent's next **cold start** — the next time it goes from stopped to running. Trinity compares the running container's image against the current base image and recreates the container when they differ. A running agent is never replaced out from under you: drift is detected but not acted on until the agent is next started cold. The quickest way to roll it out is a fleet restart, which sends each agent through the normal lifecycle so drifted ones are recreated and up-to-date ones keep their containers. The system agent follows the same rule and is deliberately never swapped mid-operation — a stale image raises an operator alert instead. See [Upgrading](../guides/deploying/upgrading.md).

## How do I confirm the running deployment is actually the build I just shipped?

`curl -s http://localhost:8000/api/version`. The `git_commit_short`, `git_branch`, `git_commit_subject`, and `build_date` fields report the code that is *actually running*, not merely what was baked in at build time — so a container running mounted source, or one that drifted from its image, is reported honestly. The response also carries `edition`, `enterprise_features`, and `install_source` (how the instance was provisioned), so you can confirm the entitlements you expect are registered. `"unknown"` values on a source install mean the image was built without the deploy script's build args; on a hosted install the fields describe the commit the release workflow built. The same data is in the UI via the version chip in the navigation bar. See [Upgrading](../guides/deploying/upgrading.md).

## Can a container log fill my disk?

Not by default. Docker's JSON log driver has no size limit out of the box, and an unbounded log will eventually fill the Docker data root and wedge the whole fleet at once — so Trinity caps every container it controls, platform services and agents alike, at 10 MB per file and 3 files. Tune with `CONTAINER_LOG_MAX_SIZE`/`CONTAINER_LOG_MAX_FILE` (platform) and `AGENT_LOG_MAX_SIZE`/`AGENT_LOG_MAX_FILE` (agents). Both malformed *and* absurd-but-well-formed values fall back to the bounded default with a warning; there is no way to configure an unbounded log. Platform services adopt a change on the next `docker compose up`, agents on recreate. Only `docker logs` history shortens — Vector's aggregate at `/data/logs` is the primary record. See [Monitoring](../guides/deploying/monitoring.md).
