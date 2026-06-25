# Approvals

Human-in-the-loop approval gates surfaced through the Operating Room queue. Agents that need authorization for a sensitive action write an approval item; an operator approves or rejects from the UI; the agent reads the decision back and continues.

> 📺 **Watch:** [Trinity Platform Demo — operator queue & approvals](https://youtu.be/ivljtZqsxeo) *(May 2026)* · [all videos](../videos.md)

## Concepts

- **Approval item** — A row in the operator queue with `type=approval`. Created by the agent, consumed by an operator.
- **Options** — JSON array of choices the operator picks from (typically `["approve", "reject"]`, but agents may define richer sets like `["draft", "send", "discard"]`).
- **Priority** — `critical`, `high`, `medium`, `low`. Affects sort order in the queue.
- **Response window** — Optional `expires_at`. After expiry the item moves to `expired`; agents may choose to fail-safe or fail-open.

## How It Works

1. The agent reaches a step that needs operator authorization.
2. The agent writes an `approval` item to `~/.trinity/operator-queue.json` (or calls a helper skill that does so).
3. The Operator Queue Sync Service polls the file every 5 seconds and persists the item to the backend.
4. The item appears on the **Operations** page under the **Needs Response** tab, with title, question, options, and any context the agent attached. Resolved items (acknowledged, cancelled, expired) move to the **Resolved** tab.
5. An operator picks an option and optionally adds a text comment.
6. The decision is written back into `~/.trinity/operator-queue.json` for the agent to read.
7. The agent acknowledges the response and continues.

WebSocket events fired along the way: `operator_queue_new` when the item arrives, `operator_queue_responded` when the operator decides, `operator_queue_acknowledged` when the agent confirms it saw the decision.

### Bulk Operations

The Operations page offers a per-tab **Clear All** that hides resolved items, and pending items can be bulk-cancelled. Because cancellation can race with a response, submitting a decision returns **409** if the item left the `pending` state in the meantime (for example, it was cancelled by a bulk operation) — refresh the queue and re-check before retrying.

## For Agents

Approvals share the operator-queue API surface:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/operator-queue` | GET | List queue items (filter by `type=approval`) |
| `/api/operator-queue/{id}` | GET | Get a single item |
| `/api/operator-queue/{id}/respond` | POST | Submit the operator's decision |
| `/api/operator-queue/{id}/cancel` | POST | Cancel a pending item |
| `/api/operator-queue/agents/{name}` | GET | Items scoped to a specific agent |

See the [Operating Room doc](../operations/operating-room.md) for the full queue model.

### MCP Tools

Agents can inspect the queue programmatically (read-only):

| Tool | Description |
|------|-------------|
| `list_operator_queue` | List queue items, broad or filtered by `agent_name` |
| `get_operator_queue_item` | Fetch a single item by id |

Agent-scoped API keys see only items for the calling agent itself plus agents it has been explicitly permitted to access. Responding and cancelling remain operator actions through the API/UI.

## See Also

- [Operating Room](../operations/operating-room.md) — The Operations page where approvals surface (Needs Response / Notifications / Resolved tabs).
- [Scheduling](scheduling.md) — Automated triggers that often request approval before destructive actions.
