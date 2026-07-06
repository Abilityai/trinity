# Webhook Triggers

External event triggers and internal execution endpoints for programmatic agent invocation.

**Note:** All endpoints require JWT Bearer token unless noted. See [Authentication](authentication.md) for details. Full request/response schemas available at [Backend API Docs](http://localhost:8000/docs).

## Endpoints

### Schedule Webhook Triggers (WEBHOOK-001)

Expose a public URL that fires an agent schedule from an external system (CI/CD, CRM, monitoring) — no Trinity account or JWT needed; a 256-bit opaque token in the URL is the credential.

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/api/agents/{name}/schedules/{id}/webhook` | POST | JWT | Generate (or rotate) a webhook token for a schedule |
| `/api/agents/{name}/schedules/{id}/webhook` | GET | JWT | Get the current token status + URL |
| `/api/agents/{name}/schedules/{id}/webhook` | DELETE | JWT | Revoke the token (old URL immediately 404s) |
| `/api/webhooks/{token}` | POST | Token (in URL) | Public trigger — returns `202 Accepted`; optional `{"context": "..."}` body (≤4000 chars) is appended to the schedule message |

**Creation precondition (#1445):** creating a schedule (`POST /api/agents/{name}/schedules`) and generating a webhook token both require the target agent to **exist and be live** (not deleted). Calling either on a nonexistent or deleted agent returns **404 Not Found**; callers without access to the agent get **403 Forbidden** regardless of whether the agent exists. This guarantees a webhook URL always points at a schedule of a live agent — you cannot mint a token that would later 404 at trigger time.

### Internal Execution (no auth -- internal network only)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/internal/execute-task` | POST | Execute task (used by scheduler, supports async_mode) |
| `/api/internal/decrypt-and-inject` | POST | Auto-import credentials on agent startup |

### Process Triggers

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/processes/{id}/execute` | POST | Start process execution |

### Slack Events

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/public/slack/events` | POST | Slack event receiver |

### Event Emission

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/events` | POST | Emit event (triggers subscriptions) |
| `/api/agents/{name}/emit-event` | POST | Emit for specific agent |

**Note:** The `/api/internal/*` endpoints are not authenticated and should only be accessible within the Docker network. They are used by the scheduler service and agent containers.

## See Also

- [Authentication](authentication.md) -- JWT token usage and login flow
- [Agent API](agent-api.md) -- Agent lifecycle and configuration endpoints
- [Chat API](chat-api.md) -- Chat, voice, and streaming endpoints
- [Backend API Docs](http://localhost:8000/docs) -- Interactive Swagger documentation
