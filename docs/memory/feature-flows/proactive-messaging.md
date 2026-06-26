# Proactive Agent Messaging

> **Issue**: #321, #376
> **Status**: Implemented
> **Last Updated**: 2026-04-17

## Overview

Enables agents to send proactive messages to specific users by verified email across Telegram, Slack, and web channels. Unlike reactive chat where users initiate conversation, proactive messaging allows agents to reach out first — for notifications, reminders, status updates, or any agent-initiated communication.

## Key Features

- **Explicit opt-in consent**: Users must enable `allow_proactive` flag on their sharing record
- **Redis-based rate limiting**: 10 messages per recipient per hour (survives restarts)
- **Mandatory audit logging**: All proactive sends logged via platform_audit_service
- **Multi-channel delivery**: Auto-selection tries telegram → slack → web. WhatsApp is an explicit-only channel (`channel="whatsapp"`) and not part of `auto` — Twilio's 24-hour session window makes it unreliable for inactive recipients.
- **MCP tool access**: Agents use `send_message` MCP tool for outreach

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        Proactive Messaging Flow                              │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│   [Agent via MCP]                                                           │
│        │                                                                     │
│        │ send_message(recipient_email, text, channel)                       │
│        ▼                                                                     │
│   ┌─────────────┐    HTTP POST    ┌─────────────────────────┐               │
│   │ MCP Server  │───────────────►│ Backend /messages       │               │
│   │ messages.ts │                 │ routers/messages.py     │               │
│   └─────────────┘                 └───────────┬─────────────┘               │
│                                               │                              │
│                                               ▼                              │
│                               ┌─────────────────────────────┐               │
│                               │ ProactiveMessageService     │               │
│                               │ - Authorization check       │               │
│                               │ - Rate limit check          │               │
│                               │ - Channel resolution        │               │
│                               └───────────┬─────────────────┘               │
│                                           │                                  │
│               ┌───────────────────────────┼───────────────────────────┐     │
│               ▼                           ▼                           ▼     │
│   ┌─────────────────┐       ┌─────────────────┐       ┌─────────────────┐  │
│   │ Telegram        │       │ Slack           │       │ Web             │  │
│   │ _deliver_tg()   │       │ _deliver_slack()│       │ _deliver_web()  │  │
│   │                 │       │                 │       │ (v2 deferred)   │  │
│   └────────┬────────┘       └────────┬────────┘       └────────┬────────┘  │
│            │                         │                         │            │
│            ▼                         ▼                         ▼            │
│   ┌─────────────────┐       ┌─────────────────┐       ┌─────────────────┐  │
│   │ Bot API         │       │ Slack API       │       │ WebSocket +     │  │
│   │ sendMessage     │       │ chat.postMessage│       │ DB persist      │  │
│   └─────────────────┘       └─────────────────┘       └─────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Frontend Layer

### SharingPanel Toggle (#376)

The `allow_proactive` flag is managed via a toggle switch in the Team Sharing section of the Sharing tab.

**Location**: `src/frontend/src/components/SharingPanel.vue`

**UI Structure** (lines 124-149):
```vue
<li v-for="share in shares" :key="share.id">
  <div class="flex items-center gap-4">
    <!-- Proactive messaging toggle -->
    <label title="Agent can/cannot send proactive messages">
      <span>Proactive</span>
      <switch :checked="share.allow_proactive" @click="toggleProactive(share)" />
    </label>
    <button @click="removeShare(...)">Remove</button>
  </div>
</li>
```

**Toggle Handler** (lines 224-240):
```javascript
const toggleProactive = async (share) => {
  await axios.put(
    `/api/agents/${props.agentName}/shares/proactive`,
    { email: share.shared_with_email, allow_proactive: !share.allow_proactive },
    { headers: authStore.authHeader }
  )
  share.allow_proactive = !share.allow_proactive
  showNotification(share.allow_proactive ? 'Proactive messaging enabled' : 'Proactive messaging disabled', 'success')
}
```

**Model** (`src/backend/db_models.py:53-60`):
```python
class AgentShare(BaseModel):
    # ... other fields
    allow_proactive: bool = False  # Can agent send proactive messages to this user
```

## Data Flow

### 1. Authorization Check

```
agent_sharing table:
  agent_name | shared_with_email | allow_proactive
  -----------|-------------------|----------------
  my-agent   | user@example.com  | 1  ← Can receive proactive messages
  my-agent   | other@example.com | 0  ← Cannot (default)
```

Authorization passes if:
1. Recipient is the agent owner (always allowed), OR
2. Recipient has `agent_sharing` record with `allow_proactive=1`

### 2. Rate Limiting (Redis)

```
Key: proactive_msg:{agent_name}:{recipient_email}
TTL: 3600 seconds (1 hour)
Max: 10 messages per key
```

### 3. Channel Resolution

**Auto mode** tries channels in order:
1. **Telegram**: Look up `telegram_chat_links` by `verified_email` field
2. **Slack**: Call `users.lookupByEmail` API in each connected workspace
3. **Web**: (Deferred to v2 — requires refactoring public_chat)

**Explicit-only channels** (not in `auto`):
- **WhatsApp** (`channel="whatsapp"`, #467): look up `whatsapp_chat_links` by `verified_email`; send via Twilio REST with `_send_message`, chunked to Twilio's 1600-char body limit. Out-of-24h-window messages fail with Twilio error 63016; the failure surfaces in `DeliveryResult.error`.

### 4. Audit Logging

Every send attempt logged via `platform_audit_service`:
```python
AuditEventType.PROACTIVE_MESSAGE
event_action="send"
actor_type="agent"
actor_id=agent_name
target_type="user"
target_id=recipient_email
```

## File Locations

### Backend

| File | Purpose |
|------|---------|
| `src/backend/routers/messages.py` | REST endpoints for proactive messaging |
| `src/backend/services/proactive_message_service.py` | Core service with rate limiting, audit, delivery |
| `src/backend/db/agent_settings/sharing.py` | `can_agent_message_email()`, `set_allow_proactive()` |
| `src/backend/db/schema.py` | `allow_proactive INTEGER DEFAULT 0` column |
| `src/backend/db/migrations.py` | Migration for `allow_proactive` column |
| `src/backend/db/telegram_channels.py` | `get_chat_link_by_verified_email()` reverse lookup |
| `src/backend/db/whatsapp_channels.py` | `get_chat_link_by_verified_email()` reverse lookup (#467) |
| `src/backend/services/slack_service.py` | `get_user_by_email()` for DM delivery |
| `src/backend/adapters/whatsapp_adapter.py` | `_send_message`, `_split_message`, `format_response` used by `_deliver_whatsapp` |
| `src/backend/services/platform_audit_service.py` | `PROACTIVE_MESSAGE` event type |

### MCP Server

| File | Purpose |
|------|---------|
| `src/mcp-server/src/tools/messages.ts` | `send_message` MCP tool definition |
| `src/mcp-server/src/client.ts` | `sendUserMessage()` API method |
| `src/mcp-server/src/server.ts` | Tool registration |

## API Endpoints

### Send Proactive Message

```
POST /api/agents/{agent_name}/messages
Authorization: Bearer <jwt>
Content-Type: application/json

{
  "recipient_email": "user@example.com",
  "text": "Your report is ready!",
  "channel": "auto"  // auto | telegram | slack | whatsapp | web
}

Response 200:
{
  "success": true,
  "channel": "telegram",
  "message_id": "12345"
}

Response 403:
{
  "error": "Agent 'my-agent' is not authorized to message 'user@example.com'"
}

Response 429:
{
  "error": "Rate limit exceeded: max 10 messages per hour to this recipient"
}

Response 404:
{
  "error": "No delivery channel available for recipient"
}
```

### Update Allow Proactive Flag

```
PUT /api/agents/{agent_name}/shares/proactive
Authorization: Bearer <jwt>
Content-Type: application/json

{
  "email": "user@example.com",
  "allow_proactive": true
}

Response 200:
{
  "agent_name": "my-agent",
  "email": "user@example.com",
  "allow_proactive": true
}
```

### List Proactive-Enabled Shares

```
GET /api/agents/{agent_name}/shares/proactive
Authorization: Bearer <jwt>

Response 200:
{
  "shares": [
    {
      "shared_with_email": "user@example.com",
      "shared_by": "admin",
      "allow_proactive": true
    }
  ]
}
```

## MCP Tool

### send_message

```typescript
{
  name: "send_message",
  parameters: {
    recipient_email: z.string().email(),
    text: z.string().min(1).max(4096),
    channel: z.enum(["auto", "telegram", "slack", "whatsapp", "web"]).default("auto"),
    reply_to_thread: z.boolean().default(false),
    agent_name: z.string().optional()  // Required for user-scoped keys
  }
}
```

**Usage from agent**:
```
Send a message to user@example.com saying "Your report is ready!"
```

The agent uses its agent-scoped MCP API key, which automatically identifies the calling agent.

## Database Schema

### agent_sharing (modified)

```sql
-- Added column:
allow_proactive INTEGER DEFAULT 0  -- 1 = can receive proactive messages

-- Added partial index:
CREATE INDEX idx_agent_sharing_proactive
ON agent_sharing(agent_name, shared_with_email)
WHERE allow_proactive = 1;
```

## Security Considerations

1. **Explicit opt-in**: Default is `allow_proactive=0` — users must explicitly enable
2. **Owner authorization**: Agent owners can always be messaged (no opt-in needed)
3. **Rate limiting**: Prevents spam (10/hour per agent-recipient pair)
4. **Audit trail**: All sends logged, including failures
5. **Email verification**: Channels require verified email for identity

## Future Work (v2)

- **Web delivery**: Requires refactoring `public_chat` from link_id-based to agent+email-based sessions
- **Threading**: Support for `reply_to_thread` to continue conversations
- **Batch sends**: Send to multiple recipients in one call
- **Delivery receipts**: Track whether messages were read

## Related Flows

- [agent-sharing.md](agent-sharing.md) — Sharing system where `allow_proactive` lives
- [telegram-integration.md](telegram-integration.md) — Telegram channel delivery
- [slack-channel-routing.md](slack-channel-routing.md) — Slack DM delivery
- [unified-channel-access-control.md](unified-channel-access-control.md) — Verified email as identity
- [effect-idempotency.md](effect-idempotency.md) — `send_message` is wired through `effect_guard` (scope `effect:{execution_id}`, identity = resolved recipient+channel) so a re-delivered turn cannot re-send the same message (#1084)
