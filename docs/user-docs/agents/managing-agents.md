# Managing Agents

Control the lifecycle, health, and resources of your Trinity agents through the UI, API, or MCP tools.

## How It Works

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

Configure per-agent memory and CPU limits in the Config tab. Execution timeout is configurable per agent (range: 60--7200 seconds, default: 3600 seconds / 60 minutes).

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
- [Agent Scheduling](../scheduling/agent-scheduling.md)
- [Agent Communication](../communication/agent-communication.md)
