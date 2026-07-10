# Managing Agents

Control the lifecycle, health, and resources of your Trinity agents through the UI, API, or MCP tools.

## How It Works

### The Agent Detail Page

Click any agent to open its detail page. The page lands on the **Overview** tab and organizes everything else into tabs:

| Tab | Purpose |
|-----|---------|
| **Overview** | Default landing tab — trends, health, recent activity (see below) |
| **Tasks** | Headless task execution and history |
| **Chat** | Interactive conversation -- see [Agent Chat](agent-chat.md) |
| **Schedules** | Cron-based automation -- see [Scheduling](../automation/scheduling.md) |
| **Loops** | Sequential bounded task runs -- see [Agent Loops](../automation/agent-loops.md) |
| **Playbooks** | Reusable command templates |
| **Credentials** | Credential injection and export |
| **Payments** | Paid access configuration |
| **Sharing** / **Permissions** | Access control (owners only) |
| **Git** | Git sync controls (GitHub-synced agents only) |
| **Files** | Workspace file browser |
| **Folders** | Shared folder configuration |
| **Settings** | Per-agent configuration sections (owners only) -- see [Agent Configuration](agent-configuration.md) |
| **Info** | Template metadata and capabilities |

Some tabs appear only when relevant: **Session** when the session feature is enabled, **Dashboard** when the agent ships a `dashboard.yaml`, **Git** when the agent has git sync.

When the window is too narrow to fit every tab, the trailing tabs collapse into a **More ▾** menu at the end of the tab strip. If the active tab is inside the menu, the More button carries the active highlight. Deep links via `?tab=` work either way.

### Overview Tab

![Agent detail — Overview tab showing the About lead, a needs-attention banner, and activity trend charts (executions by type, success rate, duration)](../images/agent-overview-tab.png)

The Overview tab is a glanceable, database-backed summary. It renders even when the agent is stopped:

- **About** -- display name, tagline, and description, with a "New task" shortcut.
- **Needs attention** -- a combined count of pending notifications, operator-queue items, and sync failures, linking to the [Operations page](../operations/operating-room.md). Hidden when the count is zero.
- **Activity trends** -- executions per day (stacked by trigger type), success rate, duration (avg and p95), and context consumption over a selectable 7/14/30-day window. Trigger types are bucketed as Chat/Tasks, MCP, Channels, Public, Scheduled, Loops, Agent-to-agent, Voice, and Other.
- **Health & reliability** -- current health badge, reachability, restart count, plus uptime and latency trend lines. Health history covers at most the last 7 days; if fleet monitoring has never run, the panel shows "No health data available -- the monitoring service may be off."
- **Recent activity** -- the last five executions; click one to open it in the Tasks tab.
- **Footprint** -- schedule, skill, and share counts, plus git sync status.

The persistent header above the tabs still owns live "now" state: status, CPU/memory gauges, cost, and quick controls. The Overview shows trends; the header shows the present.

- **API:** `GET /api/agents/{name}/analytics?window=7d|14d|30d`

### Start and Stop

Toggle an agent between Running and Stopped using the switch on the Dashboard, Agents page, or Agent Detail page. A loading spinner displays during state transitions.

- **UI component:** `RunningStateToggle.vue` (supports size variants)
- **API:** `POST /api/agents/{name}/start` and `POST /api/agents/{name}/stop`
- **MCP:** `start_agent(name)` and `stop_agent(name)`

### Rename

Click the pencil icon next to the agent name on the Agent Detail page to edit inline. Renaming is atomic: it updates the database, renames the Docker container, and broadcasts the change via WebSocket.

Restrictions: system agents cannot be renamed. Only owners and admins have permission to rename.

- **API:** `PUT /api/agents/{name}/rename` with body `{"new_name": "new-name"}`
- **MCP:** `rename_agent(name, new_name)`

### Delete

Use the Delete button on the Agent Detail page. A confirmation dialog is required. Deletion cleans up the container, network, sharing records, schedules, activities, and event subscriptions.

- **API:** `DELETE /api/agents/{name}`
- **MCP:** `delete_agent(name)`

### Health and Status

The agent header displays status (Running/Stopped), CPU and memory usage, network I/O, and uptime. Telemetry auto-refreshes every 10 seconds.

Fleet-wide monitoring is available at `GET /api/monitoring/fleet-health`. Health levels, from best to worst: healthy, degraded, unhealthy, critical, unknown.

- **MCP:** `get_agent_health(name)`, `get_fleet_health()`, `trigger_health_check()`

### Resource Allocation

Configure per-agent memory and CPU limits from the agent header: click the gear button ("Configure resources") to open the resource modal. Limits are enforced at the container level and take effect on the next restart -- see [Agent Configuration](agent-configuration.md#resource-allocation) for valid values and fleet-wide defaults.

Execution timeout is configurable per agent (range: 60--7200 seconds, default: 3600 seconds / 60 minutes).

The agent's timeout is the ceiling for any of its schedules — setting it below an active schedule's `timeout_seconds` is rejected with `400 error=agent_timeout_below_active_schedules`.

- **API:** `GET /api/agents/{name}/timeout` and `PUT /api/agents/{name}/timeout`

### Listing

The Agents page shows horizontal row tiles with success rate bars. Filter by name, status, or tags. The Dashboard offers a network graph view and a timeline view.

- **API:** `GET /api/agents` returns all agents
- **MCP:** `list_agents()`

## For Agents

Agents can manage other agents programmatically through the MCP tools listed above. Common patterns include orchestrator agents that start and stop worker agents on demand, or monitoring agents that poll fleet health and trigger alerts when agents become degraded or unhealthy.

## See Also

- [Creating Agents](creating-agents.md)
- [Agent Configuration](agent-configuration.md)
- [Scheduling](../automation/scheduling.md)
- [Agent Loops](../automation/agent-loops.md)
- [Agent Network](../collaboration/agent-network.md)
