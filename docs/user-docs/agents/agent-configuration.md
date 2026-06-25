# Agent Configuration

Per-agent settings for autonomy, read-only mode, resources, capabilities, execution timeout, reliability, and runtime.

## How It Works

Each agent has independent configuration options that control its behavior, resource usage, and execution constraints. Settings are managed through the Agent Detail page or the API.

### The Settings Tab

The Agent Detail page has a **Settings** tab (visible to owners only) -- a sectioned home for per-agent configuration. Its first section is **Guardrails** (see [Agent Guardrails](agent-guardrails.md)); more configuration sections will move here over time. Until then, the settings below are managed from the agent header controls, toggles on the Dashboard and Agents pages, or the API.

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

Per-agent memory and CPU limits, enforced at the container level (Linux cgroups).

- Open the resource modal from the agent header (gear button, "Configure resources"). Each limit can also be left as "Inherit default".
- Memory options: 1g, 2g, 4g, 8g, 16g, 32g, 64g. CPU options: 1, 2, 4, 8, 16 cores.
- Changes take effect on the next agent restart.
- API: `GET /api/agents/{name}/resources` and `PUT /api/agents/{name}/resources`
- **Full capabilities mode**: grants containers system-level access (Docker socket, network tools) when needed

**Fleet-wide defaults (admin):** the default CPU and memory for *new* agent containers are set platform-wide via `GET`/`PUT /api/settings/agent-defaults/resources` (admin-only; CPU 1/2/4/8/16, memory 1g--32g). Changes apply to new containers only -- restart existing agents to pick up new defaults.

**Scratch space (`/tmp`):** each agent's `/tmp` is a RAM-backed tmpfs, hardened `noexec,nosuid`. Its size is operator-configurable via the `AGENT_TMP_SIZE` environment variable on the backend (default `512m`; accepts `<int>m`/`<int>g`); the `noexec,nosuid` flags are fixed. Because the tmpfs counts against the agent's memory limit and blocks execution, heavy build/scratch work (pip and npm installs, compiling extensions) is redirected to a disk-backed `TMPDIR` at `/home/developer/.tmp` on the home volume. The `/tmp` size is applied at container create, so a changed `AGENT_TMP_SIZE` is picked up on recreate, not a plain restart.

### Execution Timeout

Configurable time limit for agent executions.

- Range: 60--7200 seconds (default: 3600 seconds / 60 minutes).
- Applies to all trigger methods: task, chat, schedule, MCP, and paid endpoints.
- Slot TTL is set to the timeout value plus a 5-minute buffer.
- **Schedule ceiling**: the agent timeout is the ceiling for every one of its schedules. Setting an agent timeout below an active schedule's `timeout_seconds` is rejected with `400 error=agent_timeout_below_active_schedules`. Conversely, creating or updating a schedule with `timeout_seconds > agent.execution_timeout_seconds` is rejected with `400 error=schedule_timeout_exceeds_agent_cap`. Raise the agent cap first, then the schedule.
- API: `GET /api/agents/{name}/timeout` and `PUT /api/agents/{name}/timeout`

### Dispatch Circuit Breaker

Optional per-agent protection (default: off) that stops sending new work to an agent whose container repeatedly answers with authentication failures -- for example, an expired API key. Instead of queueing tasks that are doomed to fail, the platform rejects new executions immediately with `503` and a `Retry-After` header, and fails any tasks already queued for that agent.

How it behaves when enabled:

- After 3 consecutive authentication failures, the breaker opens and new dispatches fast-fail.
- Recovery is automatic: after a cooldown, one probe execution is let through. Success closes the breaker; failure extends the cooldown (exponential backoff).
- Timeouts and ordinary task errors do **not** trip the breaker -- only auth-type failures count.

Two switches must both be on for the breaker to engage: the per-agent toggle (owner-only) and the platform-wide `DISPATCH_BREAKER_ENABLED` environment variable (also off by default).

When a breaker is open, the agent header and the Dashboard network graph show a "⚡ circuit open" badge, and the Overview tab's health panel shows a "Circuit open" chip. Enabling or disabling the breaker is done via the API:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/circuit-breaker` | GET | Current state of both breakers (dispatch + transport), plus config flags |
| `/api/agents/{name}/circuit-breaker` | PUT | Enable or disable the per-agent breaker (`{"enabled": true}`, owner-only) |
| `/api/agents/{name}/circuit-breaker/reset` | POST | Force both breakers closed without waiting for cooldown (admin-only) |

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

Agents inherit their configuration at container creation time. Changes to resource allocation, API key, or runtime trigger a container recreate. Changes to autonomy, read-only mode, timeout, circuit breaker, and model selection take effect on the next execution without restarting the container.

See [Backend API Docs](http://localhost:8000/docs) for full request/response schemas.

## See Also

- [Creating Agents](creating-agents.md)
- [Managing Agents](managing-agents.md)
- [Agent Guardrails](agent-guardrails.md)
