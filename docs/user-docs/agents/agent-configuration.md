# Agent Configuration

Per-agent settings for autonomy, read-only mode, resources, capabilities, execution timeout, and runtime.

## How It Works

Each agent has independent configuration options that control its behavior, resource usage, and execution constraints. Settings are managed through the Agent Detail page or the API.

### Autonomy Mode

Master toggle that enables or disables all scheduled operations for an agent.

- Toggle from the Dashboard, Agents page, or Agent Detail view
- When disabled, all schedules for that agent are paused
- API: `GET /api/agents/{name}/autonomy` and `PUT /api/agents/{name}/autonomy`

### Read-Only Mode

Prevents modification of source files (`*.py`, `*.js`, etc.) inside the agent container.

- Toggle in the Agent Header
- Uses `PreToolUse` hooks to intercept `Write`, `Edit`, and `NotebookEdit` tool calls
- Allowed patterns: `output/*`, `content/*` (generated files are permitted)
- API: `GET /api/agents/{name}/read-only` and `PUT /api/agents/{name}/read-only`

### Resource Allocation

Per-agent memory and CPU limits.

- Configure memory limit (MB) and CPU limit in the Config tab of Agent Detail
- **Full capabilities mode**: grants containers system-level access (Docker socket, network tools) when needed

### Execution Timeout

Configurable time limit for agent executions.

- Range: 60--7200 seconds (default: 3600 seconds / 60 minutes).
- Applies to all trigger methods: task, chat, schedule, MCP, and paid endpoints.
- Slot TTL is set to the timeout value plus a 5-minute buffer.
- **Schedule ceiling**: the agent timeout is the ceiling for every one of its schedules. Setting an agent timeout below an active schedule's `timeout_seconds` is rejected with `400 error=agent_timeout_below_active_schedules`. Conversely, creating or updating a schedule with `timeout_seconds > agent.execution_timeout_seconds` is rejected with `400 error=schedule_timeout_exceeds_agent_cap`. Raise the agent cap first, then the schedule.
- API: `GET /api/agents/{name}/timeout` and `PUT /api/agents/{name}/timeout`

### Per-Agent API Key

Controls which API key the agent uses for Claude.

- Toggle between the platform API key and the user's own Claude subscription
- The agent container is recreated when this setting changes

### Model Selection

Choose the Claude model used for tasks and scheduled executions.

- Available models: Opus, Sonnet, Haiku (latest generations).
- Custom model input is supported.
- Selection is persisted to `localStorage`; `model_used` is recorded in the execution audit trail.
- **Platform default**: when an agent has no model override, executions use the platform default model configured in Settings → Platform. The UI now surfaces this fallback so empty selections aren't mistaken for failures.

### Runtime

Set via `runtime.type` in `template.yaml`.

- `claude-code` (default)
- `gemini-cli`

## For Agents

Agents inherit their configuration at container creation time. Changes to resource allocation, API key, or runtime trigger a container recreate. Changes to autonomy, read-only mode, timeout, and model selection take effect on the next execution without restarting the container.

## See Also

- [Creating Agents](creating-agents.md)
- [Managing Agents](managing-agents.md)
