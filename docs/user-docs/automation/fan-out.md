# Fan-Out (Parallel Task Dispatch)

Dispatch multiple independent tasks to an agent in parallel and collect all results. Useful for batch processing, parallel analysis, or ensemble methods.

## How It Works

Fan-out sends N tasks to an agent concurrently (up to a configurable limit), waits for all of them to finish, and returns aggregated results. Every batch gets a server-minted `fan_out_id`; each subtask is a normal execution stamped with that id and with your own task `id`, and the aggregate is read from those execution records. So a batch can be read back while it is still running, and after the call that started it is gone. Send `async_mode: true` to skip the wait entirely: you get the `fan_out_id` back at once and poll for the outcome.

```
┌─────────────┐
│  Fan-Out    │
│  Request    │
└──────┬──────┘
       │
   ┌───┴───┬───────┬───────┐
   ▼       ▼       ▼       ▼
┌─────┐ ┌─────┐ ┌─────┐ ┌─────┐
│Task1│ │Task2│ │Task3│ │Task4│  (parallel, up to max_concurrency)
└──┬──┘ └──┬──┘ └──┬──┘ └──┬──┘
   │       │       │       │
   └───────┴───────┴───────┘
           │
     ┌─────┴─────┐
     │ Aggregate │
     │  Results  │
     └───────────┘
```

## For Agents

Fan-out is API/MCP-only — no UI. Agents use it to parallelize their own work.

### MCP Tool

```typescript
mcp__trinity__fan_out({
  agent_name: "my-agent",
  tasks: [
    { id: "q1", message: "Analyze Q1 revenue" },
    { id: "q2", message: "Analyze Q2 revenue" },
    { id: "q3", message: "Analyze Q3 revenue" }
  ],
  max_concurrency: 3,
  timeout_seconds: 300,
  model: "sonnet"
})
```

#### When the call outlives the MCP gateway

A batch runs longer than any single task in it, so `fan_out` is the dispatch tool most likely to exceed your MCP client's own call timeout. The MCP server gives up first (after `MCP_CHAT_TIMEOUT_MS`, default 25s) and answers with a **receipt** instead of a transport error:

```json
{
  "status": "fan_out_timeout",
  "agent": "my-agent",
  "fan_out_id": "fo_abc123def456",
  "execution_ids": ["exec_xyz", "exec_abc", "exec_def"],
  "task_count": 3,
  "message": "..."
}
```

The batch is still running and nothing was lost. Poll it with `get_fan_out_result(agent_name, fan_out_id)`:

```typescript
mcp__trinity__get_fan_out_result({ agent_name: "my-agent", fan_out_id: "fo_abc123def456" })
```

Receipt semantics:

- A receipt is issued only when the running batch can be attributed to *your* call unambiguously. If more than one recent batch could be yours, the tool returns an error that says so rather than guessing — check `list_recent_executions` before retrying.
- Do not re-send to "try again". An **identical** re-send is deduplicated server-side and answers with the same batch. A **reworded** re-send is a different call and dispatches all N tasks a second time, against an agent that is already working.
- If you know the batch will run long, pass `async_mode: true` from the start (see below).

#### Fire and forget: `async_mode`

```typescript
mcp__trinity__fan_out({
  agent_name: "my-agent",
  tasks: [ /* ... */ ],
  async_mode: true
})
// → { fan_out_id: "fo_abc123def456", status: "accepted", total: 3, completed: 0, failed: 0, results: [] }
```

The call returns as soon as the batch is accepted, and the tasks run in the background. Poll `get_fan_out_result(agent_name, fan_out_id)` for the outcome and match each result to your task by `task_id`. An identical async re-send is deduplicated and answers with the same accepted receipt; it never starts the batch twice. A sync call and an async call with the same tasks are treated as different calls.

### REST API

```bash
POST /api/agents/my-agent/fan-out
```

**Request:**
```json
{
  "tasks": [
    {"id": "task-1", "message": "Analyze Q1 revenue"},
    {"id": "task-2", "message": "Analyze Q2 revenue"}
  ],
  "max_concurrency": 3,
  "timeout_seconds": 300,
  "model": "sonnet"
}
```

**Response** (sync — with `"async_mode": true` the response is `status: "accepted"` with an empty `results` list, and you poll the batch instead):
```json
{
  "fan_out_id": "fo_abc123def456",
  "status": "completed",
  "total": 2,
  "completed": 2,
  "failed": 0,
  "results": [
    {
      "id": "task-1",
      "status": "completed",
      "response": "Q1 revenue was...",
      "execution_id": "exec_xyz",
      "cost": 0.05,
      "duration_ms": 8500
    },
    {
      "id": "task-2",
      "status": "completed",
      "response": "Q2 revenue was...",
      "execution_id": "exec_abc",
      "cost": 0.04,
      "duration_ms": 7200
    }
  ]
}
```

### Polling a batch

```bash
GET /api/agents/my-agent/fan-out/{fan_out_id}
```

Reads the batch back from its execution rows. It is the only surface that answers while the batch is still running, and the one to use after an `async_mode` call or a deadline.

**Response:**
```json
{
  "agent_name": "my-agent",
  "fan_out_id": "fo_abc123def456",
  "status": "running",
  "total": 3,
  "completed": 1,
  "failed": 0,
  "running": 2,
  "results": [
    {
      "execution_id": "exec_xyz",
      "task_id": "q1",
      "status": "success",
      "message": "Analyze Q1 revenue",
      "response": "Q1 revenue was...",
      "cost": 0.05,
      "duration_ms": 8500,
      "model_used": "sonnet",
      "started_at": "...",
      "completed_at": "..."
    },
    { "execution_id": "exec_abc", "task_id": "q2", "status": "running", "message": "Analyze Q2 revenue" },
    { "execution_id": "exec_def", "task_id": "q3", "status": "queued", "message": "Analyze Q3 revenue" }
  ]
}
```

| Batch `status` | Meaning |
|----------------|---------|
| `running` | Any subtask can still change. Keep polling. |
| `completed` | Every subtask succeeded. |
| `partial` | Some succeeded. Fan-out is best-effort, so this is a normal outcome, not an error. |
| `failed` | None succeeded. |

Per-task `status` is the **execution** status (`queued`, `running`, `success`, `failed`, …), so a subtask waiting for a slot is distinguishable from one that ran. `task_id` is the `id` you gave the task, so match results to your request by it. Batches recorded before task ids were stored carry no `task_id`; match those by `message`. A subtask gets its execution row only when it is dispatched, so while a batch is still waiting for slots the poll can show fewer tasks than you sent. Compare `total` with the number you sent before you treat the batch as finished. An unknown, malformed, or another agent's `fan_out_id` is a uniform `404`.

## Parameters

| Parameter | Default | Range | Description |
|-----------|---------|-------|-------------|
| `tasks` | — | 1-50 | Array of `{id, message}` objects |
| `max_concurrency` | 3 | 1-10 | Max parallel tasks |
| `timeout_seconds` | none | 10-3600 | How long the call waits for the batch. Reaching it does not stop the tasks (see below) |
| `model` | agent default | — | LLM model override |
| `policy` | best-effort | — | Only `best-effort` supported |
| `async_mode` | `false` | — | Return `{fan_out_id, status: "accepted"}` at once instead of waiting |

## Task IDs

Each task needs a unique ID (1-64 alphanumeric characters, dashes, underscores). Results are returned in input order with matching IDs. The ID is also stored on the task's execution, so the polling surface returns it as `task_id`.

## Timeout Handling

`timeout_seconds` bounds how long the call **waits** for the batch, not the work. If the wait runs out, the dispatch response reports:
- `status` becomes `"deadline_exceeded"`
- Completed tasks have their results
- Tasks that are still open (running, or still waiting for a slot) show `status: "running"` and **keep running**

Each subtask is still bounded by the agent's own execution timeout. After a deadline, poll `GET /api/agents/{name}/fan-out/{fan_out_id}` (or `get_fan_out_result`) for the final outcome. Omit `timeout_seconds` and the call waits out the whole batch: up to the agent's execution timeout for each wave of tasks that can run at once, plus a short buffer. `deadline_exceeded` is the dispatcher's verdict on its own deadline and never appears on the polling surface, which reports what the rows say.

## Idempotency

`POST /api/agents/{name}/fan-out` accepts an `Idempotency-Key` header, and the MCP tool derives one from its arguments. A duplicate sent while the batch is still running returns `409` whose `execution_id` field carries the batch's `fan_out_id` to poll; a duplicate after it finished replays the original response with `X-Idempotent-Replay: true`. For an `async_mode` call, the replayed response is the original `accepted` receipt, so poll the batch for its outcome. See [Chat API → Idempotency](../api-reference/chat-api.md#idempotency).

## Observability

Each subtask creates its own execution record with:
- `triggered_by: "fan_out"`
- `fan_out_id: "fo_..."` (shared across all subtasks)
- the task's own `id`, returned as `task_id` when you poll the batch

View them on the Operations page's Executions tab, or read the whole batch with `GET /api/agents/{name}/fan-out/{fan_out_id}` / `get_fan_out_result`.

## Limitations

- **Self-only**: Currently fan-out only works on the calling agent itself. Cross-agent fan-out is planned.
- **No UI**: API and MCP access only.
- **Capacity slots**: Each subtask consumes a parallel execution slot.
- **Deadlines don't cancel**: Reaching `timeout_seconds` ends the wait, not the tasks. To stop a subtask, terminate its execution.
- **Restarts**: Subtasks that were still waiting for a slot when the backend restarted are not dispatched afterwards. Tasks already dispatched are unaffected.

## See Also

- [MCP Server](../integrations/mcp-server.md) — `fan_out` and `get_fan_out_result` alongside the other orchestration tools
- [Scheduling](scheduling.md) — Cron-based task automation
- [Executions](../operations/executions.md) — Viewing execution history
