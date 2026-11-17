# Proactive Agent Messaging

> **Issue**: #321, #376
> **Status**: Implemented
> **Last Updated**: 2026-04-17

## Overview

Enables agents to send proactive messages to specific users by verified email across Telegram, Slack, and web channels. Unlike reactive chat where users initiate conversation, proactive messaging allows agents to reach out first — for notifications, reminders, status updates, or any agent-initiated communication.

## Key Features

- **Explicit opt-in consent**: Users must enable `allow_proactive` flag on their sharing record
- **Two consent units, by channel shape (ent#223)**: a **DM** to a person is consented
  **per recipient** (`agent_sharing.allow_proactive`, keyed by verified email). A post to a
  **Slack channel** is consented **per channel binding**
  (`slack_channel_agents.allow_proactive`) — in an open Slack workspace users never
  authenticate, so there is no verified-email recipient record to opt in, and the channel
  is the only meaningful consent unit. Default posture: a **new** binding denies (binding
  an agent is not itself consent); **existing** bindings were backfilled to allow, because
  channel posts had no consent gate before ent#223 and must not silently break. Toggled in
  the Slack channel panel, or via
  `PUT /api/agents/{name}/slack/channels/{channel_id}/proactive`. Refusal is a named
  `proactive_not_allowed` (403), kept distinct from *not bound* (404) and *rate capped*
  (429) so the agent can relay **why**. Replying to a user's own message never needs it.
- **Redis-based rate limiting**: 10 messages per recipient per hour (survives restarts)
- **Mandatory audit logging**: All proactive sends logged via platform_audit_service
- **Multi-channel delivery**: Auto-selection tries telegram → slack → web. WhatsApp is an explicit-only channel (`channel="whatsapp"`) and not part of `auto` — Twilio's 24-hour session window makes it unreliable for inactive recipients.
- **MCP tool access**: Agents use `send_message` MCP tool for outreach. Its description repeats the delegation contract's rule sentence (ent#568): a send that errored without a named refusal may still have reached the person, so it is never re-sent under a new `dedup_label` or from a later turn (the per-turn effect dedupe, #1084, covers only a same-turn repeat)
- **History-aware (#1600)**: a delivered message is appended to the recipient's channel session, so the agent's next turn knows what it sent

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
│   │                 │       │                 │       │ (agent key only)│  │
│   └────────┬────────┘       └────────┬────────┘       └────────┬────────┘  │
│            │                         │                         │            │
│            ▼                         ▼                         ▼            │
│   ┌─────────────────┐       ┌─────────────────┐       ┌─────────────────┐  │
│   │ Bot API         │       │ Slack API       │       │ Workspace Main  │  │
│   │ sendMessage     │       │ chat.postMessage│       │ row (proactive) │  │
│   └─────────────────┘       └─────────────────┘       └─────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Frontend Layer

### Access-tab Toggle (#376, relocated #1317 → restored #1577)

The `allow_proactive` flag is managed via a per-operator toggle in the **Access tab**
(`AccessPanel.vue`). *(History: the toggle originally lived in the Channels tab's Team
Sharing section (`SharingPanel.vue`, #377); the #1317 Access-tab redesign replaced that
markup and silently dropped the toggle — a UI-only regression restored in #1577.)*

**Location**: `src/frontend/src/components/AccessPanel.vue` (operator roster rows).

**State source**: `GET /api/agents/{name}/access` already returns `allow_proactive`
per row (`AgentOperatorAccess`, `db/agent_settings/sharing.py:get_agent_operator_access`),
so the panel reads the persisted flag from the roster it already loads — no extra fetch.
Pending (unresolved-email) invites are toggleable too: the flag rides on the
`agent_sharing` row, which exists before the email resolves to an account. The owner is
not in the operator roster and is always allowed (a static note states this).

**UI Structure**:
```vue
<li v-for="op in operators" :key="op.email">
  <!-- … name / status / role … -->
  <div class="flex items-center gap-4">
    <label :title="proactiveTitle(op)">
      <span>Proactive</span>
      <input type="checkbox" :checked="op.allow_proactive"
             :disabled="savingProactive === op.email"
             @change="onToggleProactive(op, $event.target.checked)" />
    </label>
    <button @click="removeOperator(op.email)">Remove</button>
  </div>
</li>
```

**Toggle Handler** (honest status — reflects the server's confirmed value, reverts on
failure; no optimistic-only flip):
```javascript
async function onToggleProactive(op, next) {
  const prev = op.allow_proactive
  try {
    const res = await agentsStore.setProactive(agentName, op.email, next)   // PUT .../shares/proactive
    op.allow_proactive = !!res.allow_proactive
  } catch (e) {
    op.allow_proactive = prev
    message.value = { type: 'error', text: '…' }
  }
}
```

The `setProactive` store action (`stores/agents.js`) issues
`PUT /api/agents/{name}/shares/proactive { email, allow_proactive }`.

**Model** (`src/backend/db_models.py:53-60`):
```python
class AgentShare(BaseModel):
    # ... other fields
    allow_proactive: bool = False  # Can agent send proactive messages to this user
```

## Addressed by role (trinity-enterprise#606)

`SendMessageRequest` takes exactly one of `to` (`primary | approver | viewer`) or the deprecated `recipient_email`. `routers/messages._recipient` resolves the role through `services/role_addressing.resolve` (the rule asks and reports share), and the resolved person then goes through the unchanged authorization, rate-limit and channel steps below. A message has one recipient, so a role nobody fills (`role_unassigned`) or several people fill (`role_resolves_to_several`) is a named 422, never a guess. **One exception (trinity-enterprise#904):** `primary` with no seat written — an entitled assignments provider answering "nobody", the default on a fresh enterprise install — goes to the agent's **owner**, who is always allowed proactive messages. Only here: the resolver is unchanged, so asks still go to the operators and a gate approval by `primary` still refuses (the same resolver decides who may approve a gated skill). An owner with no email is a 422 `role_unassigned` naming the remedy (assign a primary in the agent's Access tab). `operator` is not a message recipient — the operators' door is the Operating Room, reached with an `ask_operator` alert. The MCP `send_message` tool echoes the role, never the resolved email. When a role-addressed send then fails authorization (403 `role_not_opted_in`) or delivery (404 `role_no_channel`), the route's detail names the role only — the service's error text carries the resolved email, so it is passed through only for email-addressed sends, where the caller supplied that email itself. The deprecated email path logs `[role_addressing] … deprecated` only for agent-key callers, as reports do.

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
3. **Web** (trinity-enterprise#1221): the person's **Workspace** conversation with the agent — one assistant row, `source="proactive"`, in their Main thread. Detailed in [Web delivery](#web-delivery-the-workspace-trinity-enterprise1221) below.

**Every refusal is named.** Each channel raises `RecipientNotFoundError` with a stable `reason` (`telegram_no_bot`, `telegram_not_linked`, `slack_not_connected`, `slack_user_not_found`, `workspace_not_shared`, `workspace_blocked`, `workspace_unverifiable`, `workspace_in_turn`, `web_agent_only`, and `<channel>_blocked` for an owner's channel block). `auto` collects them instead of skipping silently; when every channel refuses, the 404 detail carries the map — `{"code": "role_no_channel", "role": …, "message": …, "channels": {channel: code}}` for a role-addressed send, `{"code": "no_channel", …}` for an email-addressed one. Codes only, never an address. The MCP tool reads that body (`ApiError.body`) before any substring check and returns `code`, `error`, `channels`.

**Explicit-only channels** (not in `auto`):
- **WhatsApp** (`channel="whatsapp"`, #467): look up `whatsapp_chat_links` by `verified_email`; send via Twilio REST with `_send_message`, chunked to Twilio's 1600-char body limit. Out-of-24h-window messages fail with Twilio error 63016; the failure surfaces in `DeliveryResult.error`.

### Web delivery: the Workspace (trinity-enterprise#1221)

`channel="web"` means the person's Workspace conversation with this agent — the only web conversation keyed by person (a public-link `public_chat` session is keyed by link and has no agent-initiated surface).

- **Who may receive** — the gate scheduled Workspace delivery already used, extracted in `services/schedule_workspace_delivery.py` as `check_workspace_reachable` (Workspace block list, fail closed → `agent_on_roster(..., include_owned=True)`) and `resolve_main_destination` (+ `ensure_main_session`). `resolve_and_stamp` calls the same functions, behaviour unchanged. Consent is unchanged too: `allow_proactive` (or ownership) still gates the send first.
- **Who may send** — the agent's own key only (`as_agent_key`, set by the route when `current_user.agent_name == agent_name`); any other principal is refused `web_agent_only`, and `auto` skips web for it. Otherwise a person the agent is shared with could place an agent-voiced row in another person's thread.
- **Not from inside the recipient's own Main turn** — `workspace_in_turn`: that turn's reply is the message.
- **The write** — `client_portal.db.add_portal_message_once(…, source=PROACTIVE_SOURCE)` (insert + session touch in one transaction). The row id derives from the effect identity (`agent, execution_id, recipient, "web", dedup_label`), so a retry after a post-commit failure — the effect guard releases its claim — writes nothing new. A keyless send gets a uuid. `_persist_outbound` is skipped (`history_written=True`): the Workspace row is the history, and a `public_chat` twin would be a session nobody reads. An intent-key repeat is suppressed with no "Not sent" note (the web result records no `public_chat` session).
- **Honest status** — the result carries `delivery: "workspace_inbox"`, and the MCP tool adds a note: the person sees it on their next visit; no push, email or WebSocket reaches someone who is away.
- **Next turn** — the row has a non-NULL `source`, so it reaches the agent's next resumed turn through `get_platform_rows_since_last_reply`, and `_context_lines` renders it as `[You messaged them between turns: …]`, not as a reply. The Workspace UI picks it up on its thread poll; `portalUtils.latestTypedReply` treats only `!source` rows as a turn's answer, so a proactive row landing mid-wait is never shown as the reply.
- **Known gap** — a proactive row landing while a typed turn on the same Main is still running is older than that turn's reply, so the resumed session's cursor skips it (the cold replay still shows it). Completion rows share the gap.

**Reachability for the prompt** — `reachable_channels(agent_name, email)` answers which channels could deliver now, sync and local-only (Telegram binding + verified link, Slack = any workspace connected, web = `check_workspace_reachable`), built from the same predicates as the send. Only channels `send_message` accepts are probed: WhatsApp is not one (and `auto` never tries it), so a WhatsApp link alone does not count as reachable. The assignment provider reports it on the prompt seam as `proactive_reachable`; with consent but `False`, the Execution Context says no channel reaches the primary instead of "proactive contact permitted". Slack-connected counts as possibly reachable, so the answer can under-claim "impossible", never over-claim.

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

### 4b. Cross-execution idempotency key (trinity-enterprise#665)

Optional `idempotency_key` + `idempotency_ttl` on the request. After the consent check
and before the rate limit, `idempotency_service.intent_guard` claims
`(agent, recipient, key)`. An earlier run's send inside the TTL → nothing is delivered;
the response carries `sent:false, suppressed_by, first_sent_at, first_execution_id`, an
audit `suppressed` event is written, and a `Trinity`-labelled `system` row is added to the
first send's session. Full contract: [effect-idempotency.md](effect-idempotency.md#cross-execution-intent-keys-trinity-enterprise665).

### 5. Session-History Persistence (#1600)

A delivered message is appended to the recipient's channel session, so the next
inbound turn's `build_public_chat_context` includes it. Without this the agent had
no record of its own outreach — it would repeat itself, contradict the message, or
fail to parse the reply ("yes, sounds good" — to what?).

```python
# _send_message_inner, on confirmed delivery only (never on failure — no phantom turn)
self._persist_outbound(agent_name, recipient_email, text, result)
  -> db.get_or_create_public_chat_session(agent_name, result.session_identifier, channel)
  -> db.add_public_chat_message(session_id, "assistant", text,
                                sender_email=recipient_email,   # DM => single participant (#903)
                                sender_label=agent_name)
```

**The session key must equal the inbound one.** Persisting into *a* session is
useless; it has to be the one the recipient's next message resolves to, or the
write succeeds and the bug survives invisibly. So each `_deliver_*` sets
`DeliveryResult.session_identifier` by driving **its own adapter's**
`get_session_identifier()` with a synthetic DM — never by re-deriving the format
in the service, which would drift from the adapter:

| Channel | Key | Derived from |
|---------|-----|--------------|
| Telegram | `{bot_id}:{user}:{chat}` | binding `bot_id` + chat link's `telegram_user_id` (a DM's chat_id IS the user id) |
| WhatsApp | `{binding_id}:{phone}` | binding id + chat link's `wa_user_phone` |
| Slack | `{team}:{user}:{dm_channel}` | workspace `team_id` + looked-up user + opened DM channel (`is_dm=True` selects the DM branch, not the thread branch) |

**The session is created if absent.** `telegram_chat_links.session_id` and
`whatsapp_chat_links.session_id` look like the natural key but are **never written
by any code path** — vestigial columns, read in three SELECTs and nowhere else. So
"reuse the link's session" isn't available; `get_or_create_public_chat_session` with
the adapter's identifier lands in the same session regardless.

**Attribution (#903)**: role `assistant`, `sender_label` = agent name,
`sender_email` = the recipient. A proactive send targets ONE verified email — a DM,
i.e. a single-participant session — so `_assistant_sender_email`'s rule resolves to
the recipient, keeping sender-filtered MEM-001 summarization in the right user's
memory.

**Fail-soft**: the message is already delivered when this runs, so a persistence
error is logged (loudly) and never surfaces as a delivery failure.

**Scope**: `send_access_grant_notification` shares the `_deliver_*` helpers but does
NOT persist — its history semantics are out of scope (#951). The call sits on the
`send_message` path, which draws that line structurally rather than with a flag.
Proactive **group** sends (`send_group_message`, #349/#350) have the same gap and a
different session model — tracked in #1649.

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

Response 200 (web):
{
  "success": true,
  "channel": "web",
  "message_id": "proactive-…",
  "delivery": "workspace_inbox"   // delivered, but nothing notifies the person
}

Response 404 (every channel refused):
{
  "detail": {
    "code": "no_channel",          // "role_no_channel" (+ "role") for a `to` send
    "message": "No delivery channel available for recipient",
    "channels": {"telegram": "telegram_not_linked", "slack": "slack_not_connected", "web": "workspace_not_shared"}
  }
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

- **Threading**: Support for `reply_to_thread` to continue conversations
- **Batch sends**: Send to multiple recipients in one call
- **Delivery receipts**: Track whether messages were read

## Related Flows

- [agent-sharing.md](agent-sharing.md) — Sharing system where `allow_proactive` lives
- [telegram-integration.md](telegram-integration.md) — Telegram channel delivery
- [slack-channel-routing.md](slack-channel-routing.md) — Slack DM delivery
- [unified-channel-access-control.md](unified-channel-access-control.md) — Verified email as identity
- [effect-idempotency.md](effect-idempotency.md) — `send_message` is wired through `effect_guard` (scope `effect:{execution_id}`, identity = resolved recipient+channel) so a re-delivered turn cannot re-send the same message (#1084)
