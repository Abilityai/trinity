# Trinity FAQ — Operations & Monitoring

> Part of the [Trinity FAQ](README.md). Short, grounded answers with links to the full documentation.

## How do I see what all my agents are doing at once?

The main Dashboard at `/` monitors every agent in real time. Three view modes render the same fleet — **Timeline**, **Grid**, and **List** — switched from the control pinned at the far right of the header, or by pressing `v` to cycle through them; Timeline is the default and your choice persists per browser. Press `/` anywhere on the page to type-filter the fleet by name (`Esc` clears), and use the shared Tags, Owner, and Time-range filters, which apply to all three modes and never reset when you switch. Agent-to-agent collaboration shows up in the Timeline as the Agent-Triggered trigger type; there is no separate activity feed or live node graph. See [Dashboard](../operations/dashboard.md).

## What's the difference between the Timeline, Grid, and List dashboard views?

Timeline (the default) arranges execution boxes per agent chronologically, color-coded by trigger type, with per-row completion rate, cost, and slot count, a time-range filter, and live progress for running executions. Grid is a tile canvas: each agent is a card with its avatar, runtime badge, inline Running and Autonomy toggles, and live status chips (git sync health, pending operator-queue items, subscription pressure) — drag tiles to rearrange, or use Tidy and Reset — and the same canvas carries fleet-level **info tiles**, shown or hidden from a **Tiles ▾** menu. List is the former standalone Agents page: one row per agent with **Name**, **Status**, **Controls**, **Success**, and **Exec / Sched** columns plus a capacity meter, sortable and filterable, with inline toggles and bulk tag actions. See [Dashboard](../operations/dashboard.md).

## Where did the Agents page go?

It is now the Dashboard's **List** view mode, and `/agents` redirects there. The row list, filters, sorting, inline Run/Autonomy toggles, and bulk tag operations all came across; tag and owner filtering now use the shared Dashboard header controls, so they apply to the Timeline and Grid views too. See [Dashboard](../operations/dashboard.md).

## Can I put fleet-level readouts on the Dashboard Grid?

Yes. The Grid canvas carries *info tiles* beside the agent tiles, and four ship today, all on by default: **Fleet summary** (running, autonomous, and stopped counts), **Recent failures** (the four newest failed executions across every agent you can access, plus a 24-hour failure total), **Executions** (the last 24 hours as hourly columns stacked by trigger type, with failures drawn as a separate red rail and live running/queued chips), and **Subscription pressure** (admins only — one row per Claude subscription showing how much of its 5-hour and 7-day limits is already spent, colour-banded). Show or hide them from the **Tiles ▾** menu at the canvas top-right; **Reset to defaults** restores the default set. They drag, swap, and tidy exactly like agent tiles, ride the Grid's existing 60-second poll (no request at all for a tile you switched off), and each opens the fuller surface behind it — the List view, the Executions tab, or Settings → Integrations → Claude Subscriptions. If one tile's data can't be read, only that tile shows an error with a **Retry** button, and a failed refresh keeps the last good numbers under a `stale` stamp rather than blanking them. See [Dashboard — Info tiles](../operations/dashboard.md#info-tiles).

## Why doesn't the Recent failures tile just say zero when there are no failures?

Because "no failures" is a claim that needs evidence. The tile shows the green **No failures in 24h ✓** only when it can positively confirm one — both the failure list and the 24-hour total must have loaded, and the fleet must be enumerable. If the fleet list can't be read, or the 24-hour count can't be read, it says exactly that rather than implying an all-clear. It also explains the case where the 24-hour total is above zero but the latest page is empty, which happens with older failures or legacy rows the list filters out. See [Dashboard](../operations/dashboard.md).

## Will my Dashboard Grid layout follow me to another browser?

Yes. Your tile positions, your info-tile selection (the **Tiles ▾** choices), and the **Zones** / **Lines** org toggles are saved to your user account on the server, each separately — so the same board comes back when you sign in from another browser or device, and two people sharing one browser never see each other's board. Your browser keeps a per-user copy so the board paints before the server answers and stays editable if the server can't be reached; a failed save shows a dismissable notice on the canvas and is retried on your next edit. **Reset** clears both the server record and the browser copy, then re-saves the default. The view mode itself, the List view's name and status filters, and the header filters stay browser-local, and a board you arranged before this shipped is adopted by the first account that signs in on that browser after upgrading. See [Dashboard](../operations/dashboard.md).

## What do the "sub limit", "sub 429s", and "sub auth" chips on agent tiles mean?

They flag an agent whose own Claude subscription is under strain: **sub limit** means it is rate-limited right now, **sub 429s** means it hit rate-limit errors in the last 24 hours but isn't limited at the moment, and **sub auth** means the provider rejected the token — re-register it. Hover a chip for the subscription name, the event count, and the 5-hour utilization; the same badge appears on List rows. Admins get the fleet-level picture from the **Subscription pressure** info tile, whose rows carry `auth`, `limit` (with the reset time), `429s`, `near`, or `?` chips, sorted by severity so a token that needs a person outranks a limit that just needs a wait; percentages appear only from a provider reading under 30 minutes old, otherwise the row shows a short status such as `rate-limited` or `no provider data`. What each state means, and the switching and alert settings behind it, live on the subscriptions page in Settings. See [Subscription Credentials](../credentials/subscription-credentials.md).

## What does the "ready" or "calibrating" badge on an agent mean?

It is the readiness stamp the agent's owner set. **ready** means the owner marked the agent ready; **calibrating** means it is not marked ready yet. Hover the badge for the date the stamp changed — and, on a calibrating agent whose scheduled brief is being held, a note that the brief is paused until the owner marks it ready. Agents with no stamp show no badge. The badge appears on Grid tiles and List rows. See [Dashboard](../operations/dashboard.md#grid-view).

## Why do dashboard panels show a skeleton, a scanline sweep, or a "stale" stamp?

Each means something different, and none is an error. A **skeleton** in the shape of the content (rows for Timeline and List, tile outlines for Grid) means no data has arrived yet; a pane that already has data never regresses to one. The **scanline** sweep plays over a chart zone while its data loads and wipes the chart in once — a background refresh never replays it. Background polls swap values in place, so a poll that fails after a successful one keeps the numbers on screen and marks them **stale** (for example, the Executions tile's stamp becomes `24h · stale`) instead of blanking them. If the fleet itself can't be read you get **Couldn't load agents** with a **Retry** button rather than an empty list that looks like a fleet with no agents, and a fleet with none shows **No agents yet** with a **Get started** button that opens the Create Agent dialog (first-run setup is a separate overlay that opens over the Dashboard on a fresh install). Polls pause while the tab is hidden and refresh immediately when you return. See [Dashboard](../operations/dashboard.md).

## Do new agents show up on the Dashboard right away?

Yes. Creating an agent — from the Create Agent dialog, another browser tab, the API, or another agent — sends a live event, and the Dashboard refetches the fleet list at once instead of waiting for its 30-second poll. A burst of creates, such as first-run setup seeding several agents, is gathered into one refetch, and closing the first-run overlay refetches too. Deleted agents drop out the same way. If an agent still seems missing, check the Tags and Owner filters in the header. See [Dashboard](../operations/dashboard.md).

## Can I show my fleet as an org chart?

Yes — the Grid view has an org overlay. Departments render as labelled zones around their member tiles, and reporting lines render as arrows between tiles. Drop a tile into a zone to assign it, drag from a tile's connect port to another tile to draw a reporting line, and drag a zone header to move a whole department; every change offers Undo. Both are stored as ordinary agent tags (`dept-<name>` and `reports-to-<agent>`), so nothing new is persisted and you can bulk-edit them from the tag surfaces. Adding or removing org tags is human-only — agent API keys are rejected. See [Dashboard](../operations/dashboard.md).

## How do I see structured results an agent produced, without reading its chat?

Agents can publish **reports** — a titled, typed payload rendered as a table, KPI tiles, markdown, a timeline, or raw JSON. Read them on an agent's **Reports** tab or fleet-wide under **Operations → Reports**, filter by type, time window, or free-text search, and export any report to Excel or PDF. Large tables page as you scroll, so opening a multi-megabyte report stays fast. See [Agent Reports](../operations/agent-reports.md).

## Does a green "completion" number mean the work was good?

No. Completion means the run finished cleanly — the process exited without error. Quality is a separate axis recorded as an **evaluation**, written by the platform or a human admin and never by the agent being graded. An ungraded run has no quality score at all, which is different from scoring zero. That is also why the fleet stat card now reads "Completion" rather than "Success rate". See [Executions](../operations/executions.md).

## What is the Operations page and what do its tabs show?

The Operations page at `/operations` is the single fleet-operations surface, with six tabs: **Needs Response** (pending operator-queue items — approval requests, questions, and heads-ups from agents, plus alerts the platform files itself), **Notifications** (agent notifications with filters and bulk actions), **Health** (fleet health monitoring, admin-only), **Executions** (all task runs across the fleet), **Reports** (structured results published by every agent you can access), and **Resolved** (terminal operator-queue items). Tabs are addressable via `?tab=`, and the old `/monitoring`, `/executions`, `/events`, and `/operating-room` routes redirect here. See [Operations](../operations/operating-room.md).

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

An agent raises the request with the `ask_operator` MCP tool, or writes it to its operator queue file (a sync service polls running agents every 5 seconds); either way the item appears on the Operations page under **Needs Response**, where each card carries a type pill — **Needs approval**, **Question**, or **Heads up** — and the control matches the type. For an approval you pick one of the options the agent offered (`approve` / `reject`, say), optionally add a note, and send: the decision must be one of the agent's own options or **Something else** with your instruction for what to do instead (the agent then carries out none of its options); any other free-text decision is refused (a 422 on the API). A question takes a typed answer; a heads-up just needs **Got it**. If the item stopped being pending while you were answering — another operator got there first, Clear All landed, or it expired — your response is not recorded and the page says so. The same cards work from the mobile admin at `/m`, where an approval is a tap on the option, an optional note, then an explicit **Send** — nothing goes out on a single tap. What the decision means to the agent, and when it acts on it, is on the approvals page. See [Approvals](../automation/approvals.md).

## Why are there items in Needs Response that no agent sent?

The platform files its own alerts into the same tab, so you only have to watch one place: **git sync failures** (an agent whose GitHub sync has failed three times in a row — a broken remote, an expired PAT, or upstream divergence), **weekly-limit subscription alerts** (*Subscription '<name>' passed N% of its weekly limit*, escalating at 90%, or *All N subscriptions are near their weekly limit* when every one is saturated), a notice after a **push whose `.gitignore` sweep changed which files are tracked** (naming the paths, so an unattended scheduled sync can't untrack files for weeks unnoticed), **failed or stale database backups**, **retention prunes awaiting approval**, platform-health alerts about the system agent or an agent's circuit breaker, a **flood alert** when an agent goes over its pending-item cap, and — on an install upgraded from a single skills-library setting — a **Legacy skills-library adoption refused** row per legacy address that matches none of your skill sources. Heads-ups with no decision to make are acknowledged with **Got it**; a sync or subscription alert points at the thing to fix. Many of these alerts track a condition and end themselves when it clears. The one exception to **Got it** is the legacy skills-library row: to dismiss it before the platform ends it, use **Clear All** (which cancels it) — an acknowledged copy moves to Resolved and stays there, because it waits for a delivery to an agent that does not exist. See [Operations](../operations/operating-room.md).

## Can I clear or cancel a pile of pending operator-queue items at once?

Yes — each operator tab has a **Clear All** button with a confirmation dialog. On Needs Response it cancels the pending items currently shown and tells the waiting agents their requests were cancelled; on Notifications it dismisses every non-dismissed notification from your accessible agents; on Resolved it clears items from view (items still awaiting agent confirmation are kept). All clear operations are scoped to agents you can access and recorded in the audit log. If you submit a response to an item that a bulk operation just cancelled, you get a 409 — refresh the queue and re-check. See [Operations](../operations/operating-room.md).

## Why does an alert card say "seen 12 times"?

Some platform alerts describe a condition — a subscription near its weekly limit, a refused skills-library address, the system agent's stale base image, a long-open circuit breaker. Trinity keeps one pending card per condition and updates it in place on every new reading, instead of filing a new card each time. **seen N times · last seen 5m ago** tells you the condition is still being observed. A worse reading escalates the same card rather than adding another. See [Operations — Platform alerts are conditions](../operations/operating-room.md#platform-alerts-are-conditions-not-messages).

## Why did an alert leave Needs Response without anyone answering it?

Either the condition cleared or the alert expired. When the platform sees the problem is gone, it ends the card itself, and Resolved shows **Ended by the platform — the condition cleared**. If nothing clears it, a condition alert expires 14 days after its last reading (`OPERATOR_PLATFORM_ALERT_LIFETIME_DAYS`) and reads **Expired — nobody acted on it**. And once a person ends one, the same reading files nothing for 7 days (`OPERATOR_PLATFORM_ALERT_SNOOZE_DAYS`) unless it gets worse. See [Operations](../operations/operating-room.md#platform-alerts-are-conditions-not-messages).

## What is the "Cancel N closed by the agent" button?

It appears on Needs Response when an agent removed or closed pending requests in its own queue file without anyone answering them. Those cards stay visible so nothing silently disappears; the button cancels just those items, sends nothing to the agents, and moves them to Resolved. A line under the tabs counts these, along with any answers or cancellations that did not reach their agent. See [Operations — Clear All](../operations/operating-room.md#clear-all).

## How do I see notifications my agents send me?

The **Notifications** tab on the Operations page is the consolidated view: filter by agent, type, priority, or status, optionally show dismissed items, and use the stats cards for pending, acknowledged, total, and per-agent counts. Updates arrive in real time over WebSocket, and pending notifications count toward the unified Operations badge. Agents send them from inside their container with the MCP tool `send_notification(agent_name, message, priority)`. See [Operations](../operations/operating-room.md).

## How do I see every task my agents have run?

Open the **Executions** tab on the Operations page. It lists all executions across the fleet (admins see every agent; other users see only owned or shared agents) with filters for agent, status, trigger type, time range (1 hour to 30 days, or all time), and free-text search over task messages. Trigger types cover every way a run can start — manual, schedule, chat, Workspace session, agent-to-agent, MCP, public link, webhook, fan-out, loop, reminder, room, A2A, the messaging channels, automatic `retry` of a failed scheduled run, `operator_response` (an answer to a parked request waking an agent that has the ask-ending wake turned on), `operator_ending` (a cancelled or expired request waking it), and `skill_gate` (a gated-skill request run after an operator approved it). Stat cards show Total, Completion, Failed, and Cost for the selected window, while running and queued counts are always live. A status dot shows **Live** when WebSocket updates are connected or **Polling** as fallback, and the list loads 50 rows at a time with **Load more**. See [Executions](../operations/executions.md).

## Can I watch a running execution live?

Yes. Click any running execution to open its detail page, where a green pulsing "Live" indicator streams the output in real time. The Timeline dashboard view also shows running executions progressing live, and the Executions tab shows a "N running now" strip whenever work is in flight. See [Executions](../operations/executions.md).

## Can I see what my agents are running from inside the Workspace?

Yes, in the chat's vocabulary rather than the ledger's. When a message starts a longer job, a live card under it shows the status, how long it has run, what the agent is doing, and — where the agent publishes a pipeline — the steps; its controls are **Stop** (where a stop would be accepted), **Open in Work**, and after a failed, timed-out, stopped, or lost job, **Ask about it**, which pre-fills a question in the composer and never sends on its own. The rail's **Work** tab has two sections — **Now** (a live card per job in flight) and **Earlier** (the last 30 days, latest three shown) — plus one line, *N asks waiting on you · Open in Inbox*, while something waits on you — with rows labelled by kind (**You asked**, **Handed on**, **Loop run**, **Scheduled**, **Room turn**, **Background**) and by outcome (**Working**, **Done**, **Failed**, **Timed out**, **Stopped by you**, **Skipped**, or **No longer tracked**). A job the agent delegated appears here even though it ran on another agent. External clients see none of this. See [Executions — Work in the Workspace](../operations/executions.md#work-in-the-workspace).

## Why does an execution's response start with a "Background work lost" warning?

Because the turn ended while a background command it had started was still running. Nothing an agent starts survives the end of a headless turn: a background shell command still running at exit is killed a few seconds later, so the result the agent announced may describe work that never finished. Rather than record that as a clean success, Trinity prefixes the stored response with a visible notice (*⚠️ Background work lost: N background task(s) … were killed at CLI exit*) and attaches a structured `turn_integrity` record to the row on the API; an empty record means "no evidence", never "verified healthy". If the agent needs work to outlive a turn, it should use a Trinity loop, reminder, or schedule instead of a background process. See [Executions](../operations/executions.md).

## How do I stop an execution that's running too long?

Open the execution's detail page and click **Stop**. The system sends SIGINT first, then SIGKILL if the process doesn't exit; the capacity slot is released and the activity is tracked. A run you stop yourself terminates as `cancelled` — a distinct state, not a failure — with its own filter option and badge in the Executions list, rendered neutral (not red) on the activity timeline. See [Executions](../operations/executions.md).

## How does Trinity track what my agents cost?

Every execution records its cost and model, visible on the execution detail page and in each agent's Tasks-tab history. At fleet level, the Executions tab shows a Cost stat card for the selected time window, and the Timeline dashboard view shows each agent's total cost on its row. On the agent page, the persistent header shows the agent's current cost. See [Executions](../operations/executions.md).

## Can I set a daily spending limit or get cost alerts?

Admins can set a daily cost limit (`ops_cost_limit_daily_usd`, default $50; 0 = unlimited) through the ops settings API (`PUT /api/settings/ops/config`). The admin cost endpoint `GET /api/ops/costs` then reports total cost, a per-model cost and token breakdown, and threshold alerts — a warning once spend passes 80% of the limit and a critical alert when the limit is reached. This is alerting, not enforcement: agents keep running, and the alert recommends pausing schedules or stopping non-essential agents. Cost metrics come from the OpenTelemetry collector, which is enabled by default (`OTEL_ENABLED`).

## What does the "circuit open" badge on my agent mean?

The dispatch circuit breaker has tripped: the agent's container answered several executions in a row with authentication failures (for example, an expired API key), so the platform stopped sending it new work instead of queueing tasks that are doomed to fail. New executions fast-fail with a 503 and a `Retry-After` header; after a cooldown, one probe execution is let through — success closes the breaker, failure extends the cooldown with exponential backoff. Timeouts and ordinary task errors do not trip the breaker, only auth-type failures. The badge appears in the agent header and on the agent's Grid tile on the Dashboard, with a "Circuit open" chip on the Overview tab's health panel. See [Agent Configuration](../agents/agent-configuration.md).

## How do I turn the circuit breaker on for an agent, or reset one that's open?

The breaker is off by default and needs two switches: the per-agent toggle in the agent's **Settings → Reliability** section (or `PUT /api/agents/{name}/circuit-breaker` with `{"enabled": true}`, owner-only) and the platform-wide `DISPATCH_BREAKER_ENABLED` environment variable. `GET` on the same endpoint shows the current state of both the dispatch and transport breakers. If a breaker is open and you've fixed the underlying problem, an admin can force both closed immediately — without waiting for the cooldown — via `POST /api/agents/{name}/circuit-breaker/reset`. See [Agent Configuration](../agents/agent-configuration.md).

## How do I show business numbers like revenue on an agent's dashboard?

Declare each metric in the `metrics:` block of the agent's `template.yaml`, then have the agent record values with the `record_metrics` MCP tool. The metrics appear as tiles on the agent's **Dashboard** tab, even without a `dashboard.yaml` and even while the agent is stopped. Each tile shows when its value was recorded. You can also bind a `dashboard.yaml` widget to a declared metric with `metric: <name>`. See [Dynamic Dashboards](../advanced/dynamic-dashboards.md#declared-metrics).

## Why is a metric tile on my agent's dashboard marked stale?

No new value has arrived within twice the metric's declared `cadence`. The tile keeps showing the last value, with a warning chip, so you know it may no longer be true. Check that the agent's recording schedule is still running. A metric with no `cadence` is never marked stale. See [Dynamic Dashboards](../advanced/dynamic-dashboards.md#freshness).

## Why does my agent's metrics.json no longer show up?

Trinity no longer reads `metrics.json`. Declare the metrics in `template.yaml` and record them with `record_metrics`. The agent's compatibility report names the keys the file still holds and which are not declared yet. Delete the file once you have switched. See [Dynamic Dashboards](../advanced/dynamic-dashboards.md#retired-metricsjson).

## What happens if my agent records a corrected value for a metric point it already sent?

The stored point is updated. A point's identity is its metric, timestamp, and dimensions: sending the same value again is a duplicate and writes nothing, while a different value at the same identity replaces the stored one. `record_metrics` reports it as `corrected`, beside `recorded` and `deduplicated`. The last write wins, and only the newest value is kept. To restate a point, keep its original `ts` and dimensions. See [Dynamic Dashboards](../advanced/dynamic-dashboards.md#declared-metrics).

## What are agent reports and where do I find them?

Reports are structured results an agent publishes — tables, KPI tiles, markdown, timelines, or raw JSON — so you can see its output on the platform without reading chat transcripts. Agents publish them with the MCP `report` tool, and they appear on the **Reports** tab of the agent's detail page, rendered according to each report's display hint. Lists show title and type; the full payload loads when you expand a card. Reports are pruned after 90 days by default. See [Agent Reports](../operations/agent-reports.md).

## What does the Overview tab on an agent's page show?

Overview is the default landing tab and owns the "trend over time" picture, while the persistent agent header shows current status and cost. It includes an About section, a needs-attention count linking to the Operations page (hidden at zero), execution trend charts over a selectable 7, 14, or 30-day window (daily counts by trigger type, success rate, duration, context usage), a health panel with uptime and latency (limited to the last 7 days by retention), per-schedule performance rollups, recent activity, and footprint chips. The data is read from the database, so the tab renders even when the agent is stopped. Hovering a bar shows its breakdown in a tooltip that opens below the bar when there is no room above it, so it is never cut off at the top of the page. See [Managing Agents](../agents/managing-agents.md).

## Where can I see the CPU, memory, and disk usage of the host machine?

Host telemetry is displayed inline in the Dashboard's top header, regardless of which view mode is active — the old Graph-view header that used to carry it has been removed. The same data is available from the API at `GET /api/telemetry/host`, with aggregate container stats served from a short-lived cache so the endpoint never blocks on Docker. See [Dashboard](../operations/dashboard.md).

## Do my agents come back after a host reboot?

Yes. Agent containers are created with Docker's `unless-stopped` restart policy, so after a host reboot or a Docker daemon restart every agent that was running comes back on its own and its schedules resume — nobody has to start agents by hand. An agent you deliberately stopped stays stopped, because the policy honours Docker's manual-stop flag; a quarantined agent is never resurrected by a reboot. While Docker is bringing agents back they may briefly show as stopped. The policy is applied when a container is created, so agents created before it shipped adopt it on their next recreate (a resource, runtime, or base-image change), not on a plain restart — an upgrading install can sweep its fleet once with `docker update --restart unless-stopped`. One consequence: the "restarting frequently" health issue and its operator alert now fire for regular agents, which is what makes a crash loop visible. See [Managing Agents](../agents/managing-agents.md#start-and-stop).

## Where did the Terminal tab on the agent page go?

It was retired. Shell access to an agent container is now by SSH with a short-lived key you supply: an admin enables **SSH Access** under **Settings → Access**, then requests credentials for a running agent with `POST /api/agents/{name}/ssh-access` (or the MCP tool `get_agent_ssh_access`), passing your public key and a TTL; the response carries a ready-to-run `ssh` command on the agent's own port in the 2222–2262 range, and the key is removed when the TTL expires. The WebSocket PTY route the tab used (`/api/agents/{name}/terminal`) remains available to API clients, but there is no browser terminal in the UI. See [Agent Terminal](../agents/agent-terminal.md).

## Where is the "Help improve Trinity" usage-sharing ask, and how do I see what it sent?

It is the **Usage sharing** step of first-run setup — the overlay that opens over the Dashboard for an admin who has not yet decided — headed **Help improve Trinity** with an **Off by default** badge and a **See what would be sent** expander. Its choices: **Share anonymous usage** (turns sharing on and backfills the last 30 days of local history), **Don't ask again** (a once-per-install marker that stops the ask on every browser and device), and **Skip — later in Settings → General** (leaves it undecided in this browser only; another browser or device is still asked). After the instance's first successful scheduled or webhook run the step is headed **Your first scheduled run just completed** instead, but that alone never reopens a setup you closed — you see it when the overlay opens for another reason or you re-run it from **Settings → General**. Once sharing is on, **Settings → General → Usage sharing** is where you inspect it: the reversible toggle, the exact payload preview, the share id, and **Recent sends** — the last five attempts, each with its time, the window it covered, **the receiver it went to**, and the HTTP status or error class, expandable to the payload it carried. The panel's status line is decided from that record, so it names the receiver that acknowledged the last delivery and flags a mismatch if sharing is now configured for a different address. Nothing leaves the box unless both the stored consent and the `TELEMETRY_SHARING_ENABLED` config switch are on. See [Product Telemetry](../operations/telemetry.md).

## Will Trinity ever automatically delete my execution history, logs, or deleted agents?

Yes, on retention windows you control. A background cleanup service prunes execution logs (30 days by default) and terminal execution rows (90), health-check records (7), agent reports (90), terminal operator-queue rows (90), fired or cancelled agent reminders (90), subscription headroom history and failure events (30 each), recorded metric points (365), and database backup files (14, newest three always kept); purges agents (180) and schedules (30) that have been soft-deleted past their window; and the audit log has a protected 365-day floor. Setting any window to `0` disables that sweep — except the backup window, which rejects `0` (disable backups with `DB_BACKUP_ENABLED=false` instead). The agent-purge sweep is special because it also removes the agent's data volumes, so treat its window as a recovery window, not a log window. Every install owns its windows: on each boot Trinity writes an explicit row for any window that has none, at the value already in force, so a later change to the built-in defaults never silently changes what an existing install keeps. **Settings → Retention** shows every window and where its value came from. In the Community edition that panel is read-only (editing windows there requires an enterprise entitlement); an admin changes a window with `PUT /api/settings/ops/config`, which validates and audit-logs every change. The generic settings endpoint refuses these keys, and **Reset to defaults** for the other operator settings deliberately leaves retention alone. See [Monitoring — Retention Sweeps](../operations/monitoring.md#retention-sweeps).

## Why was a data cleanup blocked until I approved it?

Trinity has a blast-radius guard: any single retention sweep that would delete more than a fixed safety threshold (1,000 rows) of one table refuses to run, logs an error, and raises an operator-queue alarm instead of deleting. **Settings → Retention** then shows a **Deletion awaiting your approval** banner with an **Approve deletion** button per pending prune, and the sweep proceeds only after an admin clicks it. The approval is admin- and human-only (agent-scoped keys cannot approve), bound to the exact retention window in force, and single-use — the guard re-arms after each prune, and agent-purge sweeps always require an acknowledgement because they destroy data volumes. See [Monitoring](../operations/monitoring.md).

## Can two Trinity installs share one Docker host without deleting each other's data?

Yes. The cleanup service reclaims orphaned agent data volumes — volumes whose agent no longer exists — but only volumes this install labelled as its own. A second Trinity stack on the same Docker daemon is never touched. Volumes created before that label existed are never reclaimed automatically; if they look orphaned, the backend names them in one WARNING log line so a person can decide. See [Monitoring — Cleanup Service](../operations/monitoring.md#cleanup-service).

## Does Trinity back up its database automatically?

Yes — nightly at 03:30 UTC, plus a copy before migrations at boot, into `backups/` under the data directory. Admins can check the last result, its age, and whether backups are stale in the `backup` block of `GET /api/settings/retention`; a failed, skipped or stale backup also raises an alert in Needs Response. The backups sit on the same disk as the database, so copy them elsewhere to survive a disk loss. See [Backup and Restore](../guides/deploying/backup-and-restore.md).
