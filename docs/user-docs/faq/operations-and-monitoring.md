# Trinity FAQ — Operations & Monitoring

> Part of the [Trinity FAQ](README.md). Short, grounded answers with links to the full documentation.

## How do I see what all my agents are doing at once?

The main Dashboard at `/` monitors all agents and their activities in real time, with a live activity feed of collaborations, task starts and completions, schedule executions, and errors. Use the toggle in the top-right to switch between three view modes — Grid, Graph, and Timeline. Timeline is the default, and your choice persists per browser. See [Dashboard](../operations/dashboard.md).

## What's the difference between the Grid, Graph, and Timeline dashboard views?

Grid is a tile canvas: each agent is a card with its avatar, runtime badge, inline Running and Autonomy toggles, and live status chips (git sync health, pending operator-queue items) — drag tiles to rearrange, or use Tidy and Reset. Graph is a network view: agents as draggable nodes colored by status, animated edges when agents communicate, tag clouds for filtering, and host telemetry plus a capacity meter in the header. Timeline (the default) arranges execution boxes per agent chronologically, color-coded by trigger type, with per-row success rate, cost, and slot count, a time-range filter, and live progress for running executions. See [Dashboard](../operations/dashboard.md).

## What is the Operations page and what do its tabs show?

The Operations page at `/operations` is the single fleet-operations surface, with five tabs: **Needs Response** (pending operator-queue items — questions, approval requests, alerts from agents), **Notifications** (agent notifications with filters and bulk actions), **Health** (fleet health monitoring, admin-only), **Executions** (all task runs across the fleet), and **Resolved** (terminal operator-queue items). Tabs are addressable via `?tab=`, and the old `/monitoring`, `/executions`, and `/operating-room` routes redirect here. See [Operations](../operations/operating-room.md).

## What does the badge on the Operations entry in the navigation bar mean?

It's a single unified count: pending operator-queue items plus pending notifications across your accessible agents. The badge pulses when any pending item is critical or urgent, so a quiet badge means nothing is waiting on you. See [Operations](../operations/operating-room.md).

## How do I turn on fleet health monitoring?

The periodic health-check loop is disabled by default. On the Health tab of the Operations page, a status badge shows "Monitoring Active" or "Monitoring Disabled" with an **Enable monitoring** / **Disable monitoring** button next to it (admin only); the same control exists at `POST /api/monitoring/enable` and `/disable`. The choice is persisted, so an enabled loop resumes automatically after a backend restart. The check interval (30 seconds by default) and other options are configured via `GET`/`PUT /api/monitoring/config`. See [Monitoring](../operations/monitoring.md).

## Why is my agent shown as unhealthy?

Health aggregates three layers — Docker (container status, resource usage, OOM detection), network (HTTP reachability with latency), and business (runtime availability, context usage, stuck executions). An agent is **unhealthy** when it's unreachable, its health endpoint returns a server error, its runtime isn't available, or its workspace clone failed; it's **critical** when the container is missing, stopped, or was killed by the out-of-memory killer; it's **degraded** for softer problems like very high CPU, context usage above 95%, or stuck executions. The Health tab lists the specific issues per agent, and you can force a fresh check per agent or fleet-wide with **Check All**. See [Monitoring](../operations/monitoring.md).

## Why is my new GitHub-template agent unhealthy even though its container is running?

The likely cause is a failed template clone: the container started, but cloning the GitHub repository that gives the agent its identity (instructions, skills, files) failed — for example because of a bad repository URL or a personal access token that can't read the repo. The agent reports its clone status on its health endpoint, so monitoring marks it unhealthy with the issue "Agent identity clone failed" instead of reporting a running-but-empty agent as healthy. Check the agent's logs, then verify the template repository exists and your GitHub PAT has access to it. See [Monitoring](../operations/monitoring.md).

## What does an alert about missed agent heartbeats mean?

Every running agent (on a current image) pushes a small heartbeat to the backend roughly every 5 seconds, independently of the health-check loop. After 3 consecutive missed beats, one soft, high-priority operator alert fires per loss episode, and a recovery notification fires when beats resume. The alert is advisory — a missed beat can also mean a transient network issue, so it never hard-marks an agent as down by itself. Agents on older images that never sent a beat show as `unsupported` and are never treated as dead. See [Monitoring](../operations/monitoring.md).

## How do agents ask me questions or request approval before doing something risky?

An agent writes an approval item (with a title, question, options like `["approve", "reject"]`, and optional context) to its operator queue file; a sync service polls running agents every 5 seconds and the item appears on the Operations page under **Needs Response**. You pick an option and can add a text comment; the decision is written back to the agent, which acknowledges it and continues. Items can carry an optional expiry, after which they move to `expired`. See [Approvals](../automation/approvals.md).

## Can I clear or cancel a pile of pending operator-queue items at once?

Yes — each operator tab has a **Clear All** button with a confirmation dialog. On Needs Response it cancels the pending items currently shown and tells the waiting agents their requests were cancelled; on Notifications it dismisses every non-dismissed notification from your accessible agents; on Resolved it clears items from view (items still awaiting agent confirmation are kept). All clear operations are scoped to agents you can access and recorded in the audit log. If you submit a response to an item that a bulk operation just cancelled, you get a 409 — refresh the queue and re-check. See [Operations](../operations/operating-room.md).

## How do I see notifications my agents send me?

The **Notifications** tab on the Operations page is the consolidated view: filter by agent, type, priority, or status, optionally show dismissed items, and use the stats cards for pending, acknowledged, total, and per-agent counts. Updates arrive in real time over WebSocket, and pending notifications count toward the unified Operations badge. Agents send them from inside their container with the MCP tool `send_notification(agent_name, message, priority)`. See [Operations](../operations/operating-room.md).

## How do I see every task my agents have run?

Open the **Executions** tab on the Operations page. It lists all executions across the fleet (admins see every agent; other users see only owned or shared agents) with filters for agent, status, trigger type, time range (1 hour to 30 days, or all time), and free-text search over task messages. Stat cards show Total, Success rate, and Cost for the selected window, while running and queued counts are always live. A status dot shows **Live** when WebSocket updates are connected or **Polling** as fallback, and the list loads 50 rows at a time with **Load more**. See [Executions](../operations/executions.md).

## Can I watch a running execution live?

Yes. Click any running execution to open its detail page, where a green pulsing "Live" indicator streams the output in real time. The Timeline dashboard view also shows running executions progressing live, and the Executions tab shows a "N running now" strip whenever work is in flight. See [Executions](../operations/executions.md).

## How do I stop an execution that's running too long?

Open the execution's detail page and click **Stop**. The system sends SIGINT first, then SIGKILL if the process doesn't exit; the capacity slot is released and the activity is tracked. A run you stop yourself terminates as `cancelled` — a distinct state, not a failure — with its own filter option and badge in the Executions list, rendered neutral (not red) on the activity timeline. See [Executions](../operations/executions.md).

## How does Trinity track what my agents cost?

Every execution records its cost and model, visible on the execution detail page and in each agent's Tasks-tab history. At fleet level, the Executions tab shows a Cost stat card for the selected time window, and the Timeline dashboard view shows each agent's total cost on its row. On the agent page, the persistent header shows the agent's current cost. See [Executions](../operations/executions.md).

## Can I set a daily spending limit or get cost alerts?

Admins can set a daily cost limit (`ops_cost_limit_daily_usd`, default $50; 0 = unlimited) through the ops settings API (`PUT /api/settings/ops/config`). The admin cost endpoint `GET /api/ops/costs` then reports total cost, a per-model cost and token breakdown, and threshold alerts — a warning once spend passes 80% of the limit and a critical alert when the limit is reached. This is alerting, not enforcement: agents keep running, and the alert recommends pausing schedules or stopping non-essential agents. Cost metrics come from the OpenTelemetry collector, which is enabled by default (`OTEL_ENABLED`).

## What does the "circuit open" badge on my agent mean?

The dispatch circuit breaker has tripped: the agent's container answered several executions in a row with authentication failures (for example, an expired API key), so the platform stopped sending it new work instead of queueing tasks that are doomed to fail. New executions fast-fail with a 503 and a `Retry-After` header; after a cooldown, one probe execution is let through — success closes the breaker, failure extends the cooldown with exponential backoff. Timeouts and ordinary task errors do not trip the breaker, only auth-type failures. The badge appears in the agent header and the Dashboard graph, with a "Circuit open" chip on the Overview tab's health panel. See [Agent Configuration](../agents/agent-configuration.md).

## How do I turn the circuit breaker on for an agent, or reset one that's open?

The breaker is off by default and needs two switches: the per-agent toggle (`PUT /api/agents/{name}/circuit-breaker` with `{"enabled": true}`, owner-only) and the platform-wide `DISPATCH_BREAKER_ENABLED` environment variable. `GET` on the same endpoint shows the current state of both the dispatch and transport breakers. If a breaker is open and you've fixed the underlying problem, an admin can force both closed immediately — without waiting for the cooldown — via `POST /api/agents/{name}/circuit-breaker/reset`. See [Agent Configuration](../agents/agent-configuration.md).

## What are agent reports and where do I find them?

Reports are structured results an agent publishes — tables, KPI tiles, markdown, timelines, or raw JSON — so you can see its output on the platform without reading chat transcripts. Agents publish them with the MCP `report` tool, and they appear on the **Reports** tab of the agent's detail page, rendered according to each report's display hint. Lists show title and type; the full payload loads when you expand a card. Reports are pruned after 90 days by default.

## What does the Overview tab on an agent's page show?

Overview is the default landing tab and owns the "trend over time" picture, while the persistent agent header shows current status and cost. It includes an About section, a needs-attention count linking to the Operations page (hidden at zero), execution trend charts over a selectable 7, 14, or 30-day window (daily counts by trigger type, success rate, duration, context usage), a health panel with uptime and latency (limited to the last 7 days by retention), per-schedule performance rollups, recent activity, and footprint chips. The data is read from the database, so the tab renders even when the agent is stopped.

## Where can I see the CPU, memory, and disk usage of the host machine?

Host telemetry is displayed in the header of the Dashboard's Graph view, alongside a capacity meter showing parallel execution slot usage. The same data is available from the API at `GET /api/telemetry/host`, with aggregate container stats served from a short-lived cache so the endpoint never blocks on Docker. See [Dashboard](../operations/dashboard.md).
