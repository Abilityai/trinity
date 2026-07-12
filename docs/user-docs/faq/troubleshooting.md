# Trinity FAQ — Troubleshooting

> Part of the [Trinity FAQ](README.md). Symptom → cause → fix, with links to the full documentation.

## Why did everything log me out all of a sudden?

The backend restarted. JWT tokens are invalidated whenever the backend restarts — after an upgrade, a `docker compose restart backend`, or a crash — so every active web session becomes invalid at once. Log in again through the web UI. MCP clients are affected too: if Claude Code shows the Trinity MCP server as disconnected, run `/mcp` in your session or restart the client to reconnect. Nothing is lost — agents, schedules, and chat history all persist across restarts. See [Upgrading](../guides/deploying/upgrading.md).

## My new agent shows as unhealthy right after I created it

The most common cause for a GitHub-template agent is a failed repository clone: the container starts fine, but the template never landed in the workspace, so the health check reports an issue like "Agent identity clone failed" — a running-but-empty agent. Open the agent's **Logs** tab and look for the git clone error near the top; it is usually an authentication failure. Verify your GitHub PAT can read the repository (private repos need a token with repo access), fix or set the per-agent PAT, then stop and start the agent — the clone re-runs on startup when the workspace has no repository yet. See [GitHub PAT Setup](../integrations/github-pat-setup.md).

## My agent won't start

First check the backend logs for the actual error: `docker compose logs backend --tail 100`, or the agent's **Logs** tab if the container came up briefly. A classic cause after maintenance is a missing agent network: `docker compose down` removes the `trinity-agent-network`, and no agent can start until it exists again — run `docker compose up -d` to recreate the networks without touching running containers (and prefer `docker compose restart` over `down`/`up` in the future). Agents also claim incrementing SSH ports starting at 2222; if something else on the host occupies a port in that range, the container can fail to bind — check with `lsof -i :2222`. See [Monitoring — Recovery Patterns](../guides/deploying/monitoring.md#recovery-patterns).

## Chat says "Another turn on this session is in progress"

In Session mode, only one turn can run per session at a time — each turn resumes the same Claude Code working memory, and concurrent turns would corrupt it, so a second message while one is in flight is rejected with a busy (429) error. Wait for the current turn to finish; the session tracks a turn-in-progress flag, so if your browser tab slept or disconnected mid-turn, the UI reattaches and shows the assistant reply once the turn lands instead of failing. If you need parallel work against the same agent, use the **Tasks** tab (headless executions run in parallel up to the agent's slot limit) or start a second session. See [Agent Session](../agents/agent-session.md).

## My scheduled task didn't run

Work down this checklist. (1) **Autonomy** is the master switch — if the agent's autonomy toggle is off, no schedule fires. (2) The individual schedule may be disabled — check its toggle on the **Schedules** tab. (3) The agent must be running; executions against a stopped container fail. (4) If the agent has the **freeze schedules if sync failing** flag on and its git sync has failed 3+ times in a row, the scheduler skips firing until sync recovers. (5) If the template ships a pre-check hook, an empty result records a `skipped` execution at zero cost — that's by design, and you'll see the row in the execution history. (6) Missed firings are only caught up within a 1-hour grace window after a scheduler restart; older ones are dropped. Also confirm the scheduler itself is healthy: `curl -s http://localhost:8001/health`. See [Scheduling](../automation/scheduling.md).

## An execution has been stuck in "queued" for a long time

Queued means the agent's parallel slots are full — each agent runs at most `max_parallel_tasks` executions concurrently (default 3) and queues the rest, draining the queue as slots free up. Check what's occupying the slots on the **Executions** tab (running count is always live) and either wait, stop a running execution, or raise the agent's Parallel Capacity in the **Settings** tab. Slots have a TTL of the agent's timeout plus a 5-minute buffer, so even a wedged run releases its slot eventually, and a background cleanup pass marks stale runs failed. Queued rows that never get a slot are automatically expired to `failed` after 24 hours. See [Executions](../operations/executions.md) and [Agent Configuration](../agents/agent-configuration.md#parallel-capacity).

## My execution failed with a timeout

Every execution is bounded by the agent's execution timeout — default 3600 seconds (60 minutes), configurable per agent from 60 to 7200 seconds. Schedules can set their own `timeout_seconds`, but never above the agent's cap: the API rejects a schedule timeout above the agent cap with `schedule_timeout_exceeds_agent_cap`, and rejects lowering the agent cap below an active schedule with `agent_timeout_below_active_schedules`. So to give a long-running schedule more time, raise the agent's timeout first (agent **Settings** or `PUT /api/agents/{name}/timeout`), then the schedule's. Also consider whether the task should be split — loops or fan-out make long work observable in bounded pieces. See [Agent Configuration](../agents/agent-configuration.md#execution-timeout).

## The git sync indicator is red / sync keeps failing

Trinity polls git-enabled agents every 60 seconds and tracks consecutive sync failures; at 3+ failures it raises an operator alert and the sync status chip on the dashboard and agent Overview turns unhealthy. Open the agent's **Git** tab to see the failure and its classification — the conflict modal explains each case in plain English. Auth failures mean the agent's GitHub PAT is invalid or expired: update it and sync again. For a deadlocked history (both sides diverged with no common ancestor), use the recovery reset: it adopts the upstream main branch while preserving the workspace state the agent flagged for persistence, available via the Git conflict flow or `POST /api/agents/{name}/git/reset-to-main-preserve-state`. See [GitHub Sync](../integrations/github-sync.md).

## My webhook suddenly returns 404 or 401

A 404 means the token in the URL is no longer valid: rotating a webhook mints a new token and the old URL 404s immediately, and revoking the webhook makes all triggers 404 until you generate a new one. Grab the current URL from the schedule's **Webhook** panel. A 401 means signature authentication is enabled and your request is missing or mis-computing the `X-Trinity-Signature: sha256=<hex>` header (HMAC-SHA256 of the raw request body with the signing secret). Note that rotating the URL also clears the signing secret — re-enable signing and update the caller with the new secret. See [Webhook Triggers](../api-reference/webhook-triggers.md).

## Credentials I updated aren't taking effect

Update credentials through the agent's **Credentials** tab (or the inject API), not by hand-editing files: the hot-reload path rewrites `.env` and regenerates `.mcp.json` on the running agent immediately, with no restart needed. Remember that `.mcp.json` is generated from `.mcp.json.template` plus `.env` — direct edits to `.mcp.json` are overwritten on the next regeneration, so change the `.env` value instead. For subscription tokens, hot-reload applies to the *next* Claude subprocess; a turn already in flight finishes on the old token. On older agent base images without the hot-reload endpoint, the platform falls back to recreating the container, which drops in-flight executions. See [Credential Management](../credentials/credential-management.md).

## My agent's context is at 90%+ / it hit the context limit

High context usage (warning above 75%, critical above 90%) means the conversation history is close to the model's window. In Session mode, Claude Code auto-compacts on its own at roughly 85% — it summarizes the history mid-turn, which adds a couple of minutes to that turn and is normal; after several compacts, response quality degrades. When that happens, click **Reset memory**: the visible message log stays, but the agent starts the next turn with a clean working memory. Use **+ New Session** instead when you want a fresh transcript and cost bucket too. In classic chat, use the reset option on the Chat tab, or restart the agent container. See [Agent Session](../agents/agent-session.md).

## I'm getting rate-limit or auth errors from Claude mid-run

If the agent uses a shared subscription and hits a rate-limit (429) or auth-class failure, Trinity auto-switches it to a different registered subscription — the new token is hot-reloaded into the running container, so in-flight work keeps running. If runs keep failing instead of switching, check three things in **Settings → Subscriptions**: that more than one subscription is registered (with only one, there is nothing to switch to), that the auto-switch toggle is on (it defaults to on), and the health status of each subscription. Auto-switch depends on the failure surfacing as a 429 or auth error, so other failure shapes won't trigger it. See [Subscription Credentials](../credentials/subscription-credentials.md).

## Docker Desktop pegs my CPU and my fans won't stop

On Docker Desktop and other VM-based Docker runtimes (Colima, Rancher Desktop), Vector's default Docker-API log source gets stuck in a reconnect storm — the VM's log relay closes each stream immediately, Vector reconnects without backoff, and the Docker VM sits at several cores while the log file grows by gigabytes a day. Native Linux is unaffected. The fix is the file-source override: `scripts/deploy/start.sh` detects Docker Desktop and creates `docker-compose.override.yml` automatically; if yours is missing, run `cp docker-compose.override.example.yml docker-compose.override.yml && ./scripts/deploy/start.sh`, or force it with `TRINITY_LOCAL_LOG_SOURCE=file ./scripts/deploy/start.sh`. The trade-off is that aggregated logs land in a single ID-keyed `local-*.json` file instead of the name-split platform/agent files. See [Querying Logs](../../QUERYING_LOGS.md).

## I see "database is locked" errors in the backend logs

Trinity's default database is SQLite, which allows one writer at a time — lock errors show up under heavy write load or, more often, when two backend containers are accidentally running against the same file. Check with `docker ps | grep trinity-backend` (expect exactly one line), then `docker compose restart backend`. Back up the database before major changes with `scripts/deploy/backup-database.sh`; a daily VACUUM (04:30 UTC) keeps the file compact. If lock errors persist under sustained load, move to PostgreSQL by setting `DATABASE_URL` — the migration guide is at [SQLite to PostgreSQL](../../migrations/SQLITE_TO_POSTGRES.md). See [Monitoring — Recovery Patterns](../guides/deploying/monitoring.md#recovery-patterns).

## Port 80 or 8000 is already in use when I start Trinity

A "port is already allocated" error on `docker compose up` means another process on the host owns that port. The frontend's host port is configurable: set `FRONTEND_PORT` in `.env` (default 80) and restart. The backend maps host port 8000 in `docker-compose.yml` (`"8000:8000"`); either stop whatever else is using 8000 or edit that mapping. Redis binds only to `127.0.0.1:6379`, and agents claim incrementing SSH ports in the 2222–2262 range — if an agent fails to start, check for a squatter in that range with `lsof -i :2222`. See [Setup](../getting-started/setup.md).

## Where do I find the logs for X?

Three places, by scope. For one agent, the **Logs** tab on its detail page shows the container's stdout/stderr with auto-refresh. For a platform service, use Docker directly: `docker compose logs -f backend` (or `frontend`, `scheduler`, `mcp-server`). For structured, queryable history, Vector aggregates everything into daily-rotated JSON files — `docker exec trinity-vector sh -c "tail -50 /data/logs/platform-$(date +%Y-%m-%d).json" | jq .` for platform logs, and the matching `agents-*.json` for agent containers (on Docker Desktop with the file-source override, it's a single `local-*.json` file instead). See [Agent Logs](../agents/agent-logs.md) and [Querying Logs](../../QUERYING_LOGS.md).

## My disk keeps filling up

Check where the space went first: `df -h /` and `docker system df`. The usual suspects are unused Docker images and build layers — reclaim them with `docker system prune -f` (safe to run). Vector log files rotate daily and are archived automatically after the retention period (default 90 days); check sizes with `docker exec trinity-vector ls -lh /data/logs/` and trigger archival early via `POST /api/logs/archive` (admin). The database stays lean on its own: retention sweeps prune old execution logs, execution rows, and health checks on configurable windows, and a daily VACUUM reclaims the freed pages. Treat under 20% free disk as a warning and under 5% as critical. See [Monitoring](../guides/deploying/monitoring.md) and [Operations Monitoring](../operations/monitoring.md#retention-sweeps).

## My loop stopped before finishing all its runs

Check the loop's stop reason in the **Loops** tab — loops have several intentional early exits. `stop signal matched` means the agent's response contained your sentinel substring, which is until-mode working as designed. `budget exhausted` means the total cost met your `max_cost_usd` at a run boundary; a deadline (`max_duration_seconds`) similarly ends the loop between runs. A loop also stops for no progress: if several consecutive runs return an identical response (default threshold 3 for new loops; set `no_progress_threshold: 0` to disable), Trinity assumes a doom loop and stops it. Finally, loops are fail-fast — any failed iteration ends the loop with status `failed` — and a backend restart marks in-flight loops `interrupted` with no auto-resume; start a new loop. See [Agent Loops](../automation/agent-loops.md).

## My agent can't reach another agent

Inter-agent calls are denied by default — an agent cannot call another until you grant permission explicitly, and grants are directional (allowing A→B does not allow B→A). The calling agent's `chat_with_agent` tool returns an error and the target won't even appear in its `list_agents` results until permitted. Open the *target* agent's **Permissions** tab and toggle on the agent that should be allowed to call it; repeat in the other direction if the conversation flows both ways. Permissions also gate shared folders and event subscriptions between agents. See [Agent Permissions](../collaboration/agent-permissions.md).

## I deleted an agent by accident

Deletes are soft: the agent's container is removed and its schedules stop firing immediately, but the database records are kept and recoverable for a retention window (default 180 days) before being purged. An admin can list recoverable agents via `GET /api/admin/soft-deleted/agents` and restore one with `POST /api/admin/soft-deleted/agents/{name}/recover`. Recovery restores the records only — it does not recreate the container — so start the agent afterward with `POST /api/agents/{name}/start` or the UI toggle. Deleted schedules are soft-deleted the same way (default 30-day window) with matching recovery endpoints. See [Managing Agents](../agents/managing-agents.md).
