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
| `/api/agents/{name}/schedules/{id}/webhook/secret` | POST | JWT | Enable / rotate **signature auth**; returns the signing secret **once** |
| `/api/agents/{name}/schedules/{id}/webhook/secret` | DELETE | JWT | Disable signature auth (URL stays live, unauthenticated) |
| `/api/webhooks/{token}` | POST | Token (in URL) | Public trigger — returns `202 Accepted`; optional `{"context": "..."}` body (≤4000 chars) is appended to the schedule message |

### Configuring a webhook from the UI

Open **Agent → Schedules**, expand a schedule, and click **Webhook**:

1. **Enable webhook** mints the URL. Use **Reveal** / **Copy URL** and the ready-to-paste **Example request** (`curl`) to wire up your caller.
2. **Rotate URL** issues a new token (the old URL 404s immediately); **Revoke** turns the webhook off entirely.

Access follows the schedule-management model — any user who can manage the agent's schedules can mint/rotate/revoke a webhook.

### Securing a webhook with a signature (recommended)

By default the URL token *is* the whole credential, so a leaked URL can trigger the schedule. Turn on **Signature authentication** to require callers to prove possession of a shared secret:

1. In the webhook panel, under **Signature authentication**, click **Enable**. Trinity shows the **signing secret exactly once** (`whsec_…`) — copy it now; it is stored only encrypted (AES-256-GCM) and never shown again.
2. Each request must include an `X-Trinity-Signature: sha256=<hex>` header, where `<hex>` is `HMAC-SHA256(secret, raw_request_body)`. Requests with a missing or invalid signature are rejected **401**. An empty body is signed as the empty string.
3. **Rotate secret** issues a new one (old signatures stop working); **Disable** removes it. Rotating the *URL* also clears the secret — re-enable signing afterward.

Example (bash):

```bash
SECRET='whsec_xxxxxxxx'
BODY='{"context":"deploy #4213 finished"}'
SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" -r | cut -d' ' -f1)"
curl -X POST 'https://your-domain.com/api/webhooks/<token>' \
  -H 'Content-Type: application/json' \
  -H "X-Trinity-Signature: $SIG" \
  -d "$BODY"
```

All webhook calls are audit-logged (caller IP, schedule, agent). Signature auth is off by default; enabling it never changes the URL.

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
