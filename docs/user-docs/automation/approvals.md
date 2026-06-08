# Approvals

Human-in-the-loop approval gates surfaced through the Operating Room queue. Agents that need authorization for a sensitive action write an approval item; an operator approves or rejects from the UI; the agent reads the decision back and continues.

## Concepts

- **Approval item** — A row in the operator queue with `type=approval`. Created by the agent, consumed by an operator.
- **Options** — JSON array of choices the operator picks from (typically `["approve", "reject"]`, but agents may define richer sets like `["draft", "send", "discard"]`).
- **Priority** — `critical`, `high`, `medium`, `low`. Affects sort order in the queue.
- **Response window** — Optional `expires_at`. After expiry the item moves to `expired`; agents may choose to fail-safe or fail-open.

## How It Works

1. The agent reaches a step that needs operator authorization.
2. The agent writes an `approval` item to `~/.trinity/operator-queue.json` (or calls a helper skill that does so).
3. The Operator Queue Sync Service polls the file every 5 seconds and persists the item to the backend.
4. The item appears in **Operating Room → Queue** with title, question, options, and any context the agent attached.
5. An operator picks an option and optionally adds a text comment.
6. The decision is written back into `~/.trinity/operator-queue.json` for the agent to read.
7. The agent acknowledges the response and continues.

WebSocket events fired along the way: `operator_queue_new` when the item arrives, `operator_queue_responded` when the operator decides, `operator_queue_acknowledged` when the agent confirms it saw the decision.

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

## See Also

- [Operating Room](../operations/operating-room.md) — The unified queue surface where approvals live.
- [Scheduling](scheduling.md) — Automated triggers that often request approval before destructive actions.
