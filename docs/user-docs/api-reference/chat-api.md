# Chat API

API endpoints for agent chat, voice, streaming, and public chat access.

**Note:** All endpoints require JWT Bearer token unless noted. See [Authentication](authentication.md) for details. Full request/response schemas available at [Backend API Docs](http://localhost:8000/docs).

## Endpoints

### Authenticated Chat

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/chat` | POST | Send message (stream-json output); accepts `files` attachments (see below) |
| `/api/agents/{name}/chat/sessions` | GET | List sessions |
| `/api/agents/{name}/chat/sessions/{id}` | GET | Session with messages |
| `/api/agents/{name}/chat/sessions/{id}/close` | POST | Close session |
| `/api/agents/{name}/chat/history/persistent` | GET | Persistent history |
| `/api/agents/{name}/chat/history` | DELETE | Reset session |
| `/api/agents/{name}/activity` | GET | Activity summary |

### Voice Chat

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/voice/start` | POST | Start voice session |
| `/api/agents/{name}/voice/stop` | POST | Stop session |
| `/api/agents/{name}/voice/status` | GET | Session status |
| `/ws/voice/{session_id}` | WS | Audio WebSocket bridge (URL returned by `voice/start`) |

### Public Chat (no auth)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/public/chat/{token}` | POST | Public chat |
| `/api/public/history/{token}` | GET | Public history |
| `/api/public/executions/{token}/{execution_id}/status` | GET | Status of a turn started from this link |
| `/api/public/executions/{token}/{execution_id}/stream` | GET | Live activity for that turn (SSE) |
| `/api/public/executions/{token}/{execution_id}/terminate` | POST | Stop a turn started from this link. Scoped per link **and** per trigger: it stops only turns this public link started — never a scheduled run, an operator chat, or a Workspace turn on the same agent. A link with email verification requires the same `session_token` that started the turn. |

### Paid Chat (x402)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/paid/{agent_name}/chat` | POST | Paid chat (402/200) |
| `/api/paid/{agent_name}/info` | GET | Payment requirements |

### Task Execution

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/task` | POST | Submit a stateless task. `async_mode: true` returns at once with `status: "accepted"` and an `execution_id`; otherwise the call holds until the task finishes (see below). Accepts `files` attachments. |
| `/api/agents/{name}/executions` | GET | List executions |
| `/api/agents/{name}/executions/{id}` | GET | Execution details (status, response, cost) — the polling target for `async_mode` and for a receipt |
| `/api/agents/{name}/executions/{id}/log` | GET | Full execution transcript (tool calls and results) |
| `/api/agents/{name}/executions/{id}/stream` | GET | Live execution log (SSE) while it runs |
| `/api/agents/{name}/executions/running` | GET | Executions currently running on the agent |
| `/api/agents/{name}/executions/{id}/terminate` | POST | Stop a running or queued execution. Answers `terminated`, `cancelled_while_queued`, `cancelled_while_parked`, or `already_finished`; a stopped run ends as `cancelled`, not `failed`. |
| `/api/agents/{name}/fan-out` | POST | Dispatch N tasks in parallel — see [Fan-Out](../automation/fan-out.md) |
| `/api/agents/{name}/fan-out/{fan_out_id}` | GET | Poll a fan-out batch while it runs — see [Fan-Out](../automation/fan-out.md#polling-a-batch) |

#### Sync task calls and the `execution_id` receipt

A synchronous `/task` call (the default, `async_mode: false`) holds the HTTP connection for the whole run — at capacity it queues on the same connection and long-polls until the execution reaches a terminal state. The MCP `chat_with_agent(parallel=true)` tool wraps this route and gives up before the MCP gateway does, answering with `{status: "queued_timeout", execution_id}` so you poll `GET /api/agents/{name}/executions/{id}` instead of re-sending — see [MCP Server](../integrations/mcp-server.md#key-tools-worth-knowing). If you know the task will run long, send `async_mode: true` from the start.

A sync `/task` that fails, times out, or is cancelled releases its `Idempotency-Key` claim, so a legitimate retry with the same key goes through instead of answering `409` for the rest of the day; a call whose long-poll timed out while the execution was still queued or running completes the claim with the same `queued_timeout` receipt, so a replay answers `200` with the execution to poll.

#### File attachments

`POST /chat` and `POST /task` accept a `files` array of `{name, mimetype, size, data_base64}` (raw base64 or a `data:` URI). Images are passed to the agent as vision content; other files land in `/home/developer/uploads/` inside the container. Accepted: images, plain text, CSV, JSON, and **ZIP** (stored unextracted — the agent unpacks it itself). Rejected: PDF, tar/gzip/rar, audio, video. Web limits: 3 files per message, 5 MB per file, 10 MB of images in total.

#### Deprecated: per-task `timeout_seconds`

The `timeout_seconds` field on the task request body is **deprecated** and will be removed in a future release. The agent's execution timeout (`GET/PUT /api/agents/{name}/timeout`) is authoritative.

Current behavior: the field is still honored, but values above the agent's timeout cap are clamped down to the cap (the server logs a deprecation warning). Omit the field — the task then uses the agent's configured timeout. To run longer tasks, raise the agent's timeout cap instead.

## Idempotency

Endpoints that trigger an execution accept an optional `Idempotency-Key` header so you can retry safely without creating duplicate executions. Pick any unique string per logical request (e.g., a UUID) and resend it on retry:

```bash
curl -X POST http://localhost:8000/api/agents/my-agent/task \
  -H "Authorization: Bearer <token>" \
  -H "Idempotency-Key: 7f3a2c1e-..." \
  -H "Content-Type: application/json" \
  -d '{"message": "Summarize the latest reports"}'
```

- The same key within 24 hours returns the original result with the header `X-Idempotent-Replay: true` — no second execution is created.
- A duplicate sent while the first request is still running returns **409** with the original `execution_id` to poll (for `/fan-out`, that field carries the batch's `fan_out_id`).
- If the first attempt was rejected before dispatch (e.g., at capacity), the key is released so the retry goes through.
- The header is optional and fail-open: omitting it preserves normal behavior, and a dedup-layer error never blocks a real request.

Wired boundaries: `/api/agents/{name}/chat`, `/api/agents/{name}/task`, `/api/agents/{name}/fan-out`, `/api/agents/{name}/voip/call`, [webhook triggers](webhook-triggers.md) (key auto-derived from token + body when the header is absent), and the MCP `chat_with_agent` / `fan_out` tools (deterministic key derived from the call arguments).

## See Also

- [Authentication](authentication.md) -- JWT token usage and login flow
- [Agent API](agent-api.md) -- Agent lifecycle and configuration endpoints
- [Backend API Docs](http://localhost:8000/docs) -- Interactive Swagger documentation
