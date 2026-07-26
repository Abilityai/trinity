# Event Subscriptions

Lightweight pub/sub system for inter-agent event pipelines. Agents emit named events, and subscribing agents receive async tasks with the payload.

## Concepts

- **Event** -- A named occurrence emitted by an agent with a structured JSON payload. Stored in the `agent_events` table.
- **Subscription** -- A rule that says "when agent X emits event type Y, send agent Z an async task with message template M". Stored in the `agent_event_subscriptions` table.
- **Message Template** -- Supports `{{payload.field}}` interpolation. The subscriber's task message is built from the event payload.
- **Permission-Gated** -- Uses existing `agent_permissions`. The subscribing agent must have permission to call the source agent.

## How It Works

1. Agent A emits an event: `emit_event(event_type="report_ready", payload={"url": "...", "summary": "..."})`
2. Trinity checks all subscriptions matching agent A + event type `report_ready`.
3. For each matching subscription, an async task is dispatched to the subscribing agent.
4. The task message is built from the subscription's template with payload fields interpolated.
5. Events are persisted and visible via API.
6. WebSocket broadcast provides real-time event visibility.

## Task-Completion Events (system-emitted)

Trinity deterministically emits `agent.task.completed` and `agent.task.failed` at **every** execution terminal of an agent. These are **system-emitted**: the platform synthesizes them at the execution chokepoint with no agent in the loop. They ride the same subscription machinery as agent-emitted events.

**The win: wake instead of poll.** Subscribe to a worker's `agent.task.completed` (or `agent.task.failed`) and you get an **automatic report-back task** the moment that worker's execution finishes -- no need to hold a call open or poll `get_execution_result`. This is the async-first alternative to `chat_with_agent(async=true)` followed by polling (see [Agent Network](agent-network.md)).

```
subscribe_to_event(
  source_agent="research-worker",
  event_type="agent.task.completed",
  message_template="research-worker finished task {{payload.execution_id}} ({{payload.status}}): {{payload.summary_or_error}}"
)
```

### Reserved namespace

The `agent.task.*` namespace is **reserved for the platform**:

- Agents **cannot** emit into `agent.task.*` themselves (`emit_event` rejects it). Only Trinity produces these events.
- Agents **cannot** self-subscribe to `agent.task.*`. Subscribing is **cross-agent only** -- you subscribe to *another* agent's completions.
- Subscriptions stay **permission-gated**: the subscriber must be permitted to call the source agent (same rule as every other subscription, see [Agent Permissions](agent-permissions.md)).

### Payload fields

Interpolate these into your message template:

| Field | Meaning |
|-------|---------|
| `{{payload.execution_id}}` | The execution that terminated (correlation key) |
| `{{payload.status}}` | `success` or `failed` |
| `{{payload.triggered_by}}` | What triggered the source execution (schedule, chat, event, ...) |
| `{{payload.summary_or_error}}` | The worker's response text on success, or the error on failure (credential-sanitized, truncated) |
| `{{payload.duration_ms}}` | Wall-clock duration of the source execution |
| `{{payload.cost}}` | Cost of the source execution |
| `{{payload.fan_out_id}}` / `{{payload.loop_id}}` | Set when the source execution was part of a fan-out or loop |

### Delivery caveat

Delivery is **best-effort**. The report-back task wakes a subscriber whose container is **running**. If the subscriber agent is **stopped**, the wake is dropped -- the event is still recorded in `agent_events`, but no task is dispatched. This is not a durable queue; do not rely on it for guaranteed hand-off between stopped agents.

### Additive and inert

With **zero matching subscriptions**, nothing happens: no event row is written and no task is dispatched. There is **no new config, endpoint, or flag** -- task-completion events reuse the existing event-subscription tools below (`subscribe_to_event` / `list_event_subscriptions` / `delete_event_subscription`), passing `event_type="agent.task.completed"` or `"agent.task.failed"`.

## For Agents

### MCP Tools

| Tool | Description |
|------|-------------|
| `emit_event(event_type, payload)` | Emit a named event with data |
| `subscribe_to_event(source_agent, event_type, message_template)` | Create a subscription |
| `list_event_subscriptions(agent_name)` | List subscriptions |
| `delete_event_subscription(subscription_id)` | Remove a subscription |

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/event-subscriptions` | POST | Create subscription |
| `/api/agents/{name}/event-subscriptions` | GET | List subscriptions |
| `/api/event-subscriptions/{id}` | GET | Get by ID |
| `/api/event-subscriptions/{id}` | PUT | Update |
| `/api/event-subscriptions/{id}` | DELETE | Delete |
| `/api/events` | POST | Emit event (agent-scoped) |
| `/api/agents/{name}/emit-event` | POST | Emit for specific agent |
| `/api/agents/{name}/events` | GET | Event history |
| `/api/events` | GET | All events |

## See Also

- [Agent Permissions](agent-permissions.md)
- [Agent Network](agent-network.md)
