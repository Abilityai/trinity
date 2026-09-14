# Agent API

Core REST API endpoints for agent lifecycle management, configuration, files, and metadata.

**Note:** All endpoints require JWT Bearer token unless noted. See [Authentication](authentication.md) for details. Full request/response schemas available at [Backend API Docs](http://localhost:8000/docs).

## Endpoints

### Agent CRUD and Lifecycle

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents` | GET | List all agents |
| `/api/agents` | POST | Create agent |
| `/api/agents/{name}` | GET | Get agent details |
| `/api/agents/{name}` | DELETE | Delete agent |
| `/api/agents/{name}/start` | POST | Start container |
| `/api/agents/{name}/stop` | POST | Stop container |
| `/api/agents/{name}/rename` | PUT | Rename agent (moves the slug) |
| `/api/agents/{name}/label` | GET/PUT | Display label (`{"label": "My Agent"}`; `null` or empty clears it back to the slug) — see [Managing Agents](../agents/managing-agents.md#display-label) |

### Agent Info and Files

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/info` | GET | Template metadata |
| `/api/agents/{name}/files` | GET/PUT/DELETE | Workspace file tree / save a text file / delete a file — see [Agent Files](../agents/agent-files.md) |
| `/api/agents/{name}/files/download` | GET | Download file |
| `/api/agents/{name}/logs` | GET | Container logs |
| `/api/agents/{name}/stats` | GET | Live telemetry |
| `/api/agents/{name}/analytics` | GET | Multi-day execution analytics for the Overview tab (`?window=7d\|14d\|30d`, default `7d`) |

### Configuration

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/autonomy` | GET/PUT | Autonomy mode |
| `/api/agents/{name}/read-only` | GET/PUT | Read-only mode |
| `/api/agents/{name}/timeout` | GET/PUT | Execution timeout (PUT rejects a cap below any active schedule's timeout with 400) |
| `/api/agents/{name}/ssh-access` | POST | Generate SSH credentials |
| `/api/agents/{name}/circuit-breaker` | GET/PUT | Circuit breaker state / per-agent enable-disable (owner-only) — see [Agent Configuration](../agents/agent-configuration.md) |
| `/api/agents/{name}/circuit-breaker/reset` | POST | Reset both breakers to closed (admin-only) |
| `/api/agents/{name}/resources` | GET/PUT | Memory and CPU limits (applied on the next recreate) |
| `/api/agents/{name}/capabilities` | GET/PUT | `full_capabilities` — Docker default capabilities (`apt-get` works) vs the restricted secure default — see [Agent Configuration](../agents/agent-configuration.md) |
| `/api/agents/{name}/mcp-exposed` | GET/PUT | Publish the agent as its own `chat_with_<slug>` MCP tool — see [MCP Server](../integrations/mcp-server.md#dedicated-agent-tools-expose-via-mcp) |
| `/api/agents/{name}/mcp-key` | GET | The agent's own MCP key health (never the secret); `POST …/mcp-key/verify` probes the container, `POST …/mcp-key/regenerate` rotates it — see [MCP Server](../integrations/mcp-server.md#each-agents-own-mcp-key) |

### Credentials

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/credentials/status` | GET | Check credential files |
| `/api/agents/{name}/credentials/inject` | POST | Inject credentials |
| `/api/agents/{name}/credentials/export` | POST | Export encrypted |
| `/api/agents/{name}/credentials/import` | POST | Import encrypted |
| `/api/agents/{name}/credential-requirements` | GET | Per-variable setup checklist — see [Credential Management](../credentials/credential-management.md) |

### Sharing

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/share` | POST | Share with email |
| `/api/agents/{name}/share/{email}` | DELETE | Remove share |
| `/api/agents/{name}/shares` | GET | List shares |

### Schedules

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/schedules` | GET/POST | List/create |
| `/api/agents/{name}/schedules/{id}` | GET/PUT/DELETE | CRUD |
| `/api/agents/{name}/schedules/{id}/enable` | POST | Enable |
| `/api/agents/{name}/schedules/{id}/disable` | POST | Disable |
| `/api/agents/{name}/schedules/{id}/trigger` | POST | Manual trigger |
| `/api/agents/{name}/schedules/{id}/executions` | GET | History |
| `/api/agents/{name}/schedules/{id}/analytics` | GET | Per-schedule analytics (`?window_hours=24\|168\|720`) — see [Scheduling](../automation/scheduling.md) |

### Loops

Sequential bounded task execution against one agent — see [Agent Loops](../automation/agent-loops.md) for concepts.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/loops` | POST | Start a loop (202 with `loop_id`) |
| `/api/agents/{name}/loops` | GET | List the agent's loops (`?status=`, `?limit=`) |
| `/api/loops/{loop_id}` | GET | Loop status, per-run summaries, last response |
| `/api/loops/{loop_id}/stop` | POST | Graceful stop (current iteration finishes) |

Start-loop body: `message` (required), `max_runs` (1–100, required), and optional `stop_signal`, `delay_seconds`, `timeout_per_run`, `max_duration_seconds` (wall-clock deadline), `max_cost_usd` (> 0; total USD budget), `no_progress_threshold` (default 3; `0` disables), `on_failure` (`abort` default, or `continue`), `max_consecutive_failures` (default 3), `model`, `allowed_tools`. Budget, deadline, and no-progress stops are all **iteration-boundary** stops — checked between runs, so the current run always finishes. The loop's `stop_reason` is one of `max_runs_reached`, `stop_signal_matched`, `user_stopped`, `deadline_exceeded`, `budget_exhausted`, `no_progress`, or `error` (`interrupted` appears only on rows from older builds — loops now survive a backend restart); `GET /api/loops/{loop_id}` also returns `max_cost_usd` and `total_cost`. Parameter semantics are documented in [Agent Loops](../automation/agent-loops.md).

### VoIP

Outbound phone calls — flag-gated, off by default. See [VoIP Telephony](../advanced/voip-telephony.md).

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/voip` | GET/PUT/DELETE | Voice binding status / configure / remove (owner) |
| `/api/agents/{name}/voip/call` | POST | Place an outbound call (rate-limited, daily-capped; accepts `Idempotency-Key`) |

### Shared Folders

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/folders` | GET/PUT | Folder config |
| `/api/agents/{name}/folders/available` | GET | Mountable folders |
| `/api/agents/{name}/folders/consumers` | GET | Consuming agents |

### Bulk/Fleet

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/context-stats` | GET | All agents context stats |
| `/api/agents/autonomy-status` | GET | All agents autonomy |
| `/api/executions` | GET | Paginated fleet execution list (filters: `status`, `triggered_by`, `hours`, `agent`, `search`) — see [Executions](../operations/executions.md) |
| `/api/executions/stats` | GET | Fleet execution stat cards (windowed by `hours`; live running/queued counts) |

### Agent-Internal

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/heartbeat` | POST | Liveness heartbeat posted by the agent container every 5s; authenticated with the agent's own agent-scoped MCP key. Not for external callers. |

### Lifecycle notes

- Agent containers are created with Docker restart policy `unless-stopped`: after a host reboot or a Docker daemon restart, every agent that was running comes back on its own, and an agent you stopped (`POST …/stop`, the Operating Room emergency stop) stays stopped. The policy is applied at container creation, so agents created before this behaviour shipped adopt it on their next recreate (a resource, runtime, or base-image change), not on a plain restart.

## Idempotency

Execution-triggering endpoints accept an optional `Idempotency-Key` header for safe retries — see [Chat API → Idempotency](chat-api.md#idempotency).

## See Also

- [Authentication](authentication.md) -- JWT token usage and login flow
- [Backend API Docs](http://localhost:8000/docs) -- Interactive Swagger documentation
