# Approvals

Human-in-the-loop approval gates surfaced through the Operations queue. An agent that needs authorization for a sensitive action parks an approval item and ends its turn; a person approves or rejects it — an operator from the Operations page or the mobile admin, or the Workspace user the agent addressed it to — and the agent reads the decision back and continues.

> 📺 **Watch:** [Trinity Platform Demo — operator queue & approvals](https://youtu.be/ivljtZqsxeo) *(May 2026)* · [all videos](../videos.md)

## Concepts

- **Queue item** — A row in the operator queue. Three request types: `approval` (a yes/no or multi-choice decision), `question` (freeform guidance), `alert` (acknowledgement only). Created by the agent, consumed by a person.
- **Options** — JSON array of choices for an `approval` (typically `["approve", "reject"]`, but agents may define richer sets like `["draft", "send", "discard"]`). The decision must be one of them.
- **Decision and note** — The answer has two parts. The **decision** (`response`) is what the agent reads: the chosen option, the typed answer to a question, or `acknowledged`. The **note** (`response_text`) is optional free text riding alongside a decision; it never stands alone.
- **Ask** — A queue item addressed to one Workspace user (`addressed_to_email`). It appears in that person's Workspace, where they answer it, and stays visible to operators in the queue. An item with no addressee is for the operator alone.
- **Priority** — `critical`, `high`, `medium`, `low`. Affects sort order in the queue.
- **Response window** — Optional `expires_at`. After expiry the item moves to `expired`; the agent treats that as "not approved — do not proceed".

## How It Works

1. The agent reaches a step that needs authorization.
2. The agent appends an entry to `~/.trinity/operator-queue.json` (or calls a helper skill that does so) and **ends its turn** — it never waits in-turn for a human.
3. The Operator Queue Sync Service polls the file every 5 seconds and persists the item to the backend.
4. The item appears on the **Operations** page under the **Needs Response** tab with a type pill — **Needs approval**, **Question** or **Heads up** — plus title, question, options, and any context the agent attached. Resolved items (responded, cancelled, expired) move to the **Resolved** tab. An item addressed to a Workspace user also appears in that person's Workspace (below).
5. A person answers. The controls follow the type: for an approval, pick an option, optionally add a note, and click **Send**; for a question, type an answer and click **Send Answer**; for an alert, click **Got it**. Nothing is sent on a single tap of an option.
6. The decision is written back into the agent's `~/.trinity/operator-queue.json` within about 5 seconds (running agents only).
7. The agent acts on it and marks the item acknowledged.

**When the agent acts on it.** By default the answer is read at the start of the agent's *next* turn — its next scheduled run, loop iteration or message. An agent with no next turn would wait indefinitely, so the agent's owner can turn on **Wake this agent when an operator answers**: then answering starts one turn immediately and the agent acts on the decision in it. That turn shows in Executions with trigger `operator_response`. See [Wake on Operator Answer](../agents/agent-configuration.md#wake-on-operator-answer).

**The decision must be one the agent offered.** For an approval that listed options, an answer outside that list is refused with a `422` that names the offered options; matching is exact, so `Approve` and `approve` are different answers. Questions, alerts and approvals that offered no options take free text.

**Platform heads-ups.** Trinity itself files items of other types into the same queue — for example a Workspace client rating a response as not useful, or an agent calling a playbook that does not exist. These carry no decision; they show **Got it** only, and acknowledging them sends nothing back to the agent.

WebSocket events fired along the way: `operator_queue_new` when the item arrives, `operator_queue_responded` when someone decides, `operator_queue_acknowledged` when the agent confirms it saw the decision.

### Asks addressed to a Workspace user

An agent can address an item to one person it is shared with by setting `addressed_to_email` on the request entry. The address is checked at ingestion against the agent's own roster: someone the agent is not shared with, or a malformed value, turns the item into an ordinary operator ask rather than being refused. The `context` block of an ask is never shown to the addressee.

What that person sees in the [Workspace](../sharing-and-access/workspace.md):

- The ask renders above the composer of the chat it belongs to — and, for platform users, again under **Waiting on you** in the rail's **Work** tab. An ask raised outside any chat (by a scheduled run, say) attaches to the person's **Main** chat with that agent; from any other chat the card offers **Open the conversation** instead of controls.
- The sidebar header counts them (*N asks are waiting on your answer*), and the agent's row carries its own badge — distinct from the unread-replies count, and never hidden behind **Show all**.
- The controls are the same three as the Operations page: option → optional note (*Add a note (optional)…*) → **Send**; a typed answer → **Send**; **Got it** for an alert.
- Answering removes the ask everywhere at once and shows a short confirmation: **Sent.**, or **Sent — {agent} is picking this up.** when the owner has turned on wake-on-answer.
- An expired ask still renders, marked as expired, and drops out of the count. It is not silently removed.
- The Workspace refreshes asks every 20 seconds while the tab is visible.

An ask is answered through the Workspace's own route (below), which records the answer with the same write-back, audit fields and wake-on-answer behaviour as an operator's. A client whose share was revoked stops seeing the ask.

### Bulk Operations

The Operations page offers a per-tab **Clear All** that hides resolved items, and pending items can be bulk-cancelled. Because cancellation can race with a response, submitting a decision returns **409** if the item left the `pending` state in the meantime (for example, it was cancelled by a bulk operation) — refresh the queue and re-check before retrying.

### From a phone

The [mobile admin](../sharing-and-access/mobile-admin.md) at `/m` answers the same queue with the same three controls.

## For Agents

Approvals share the operator-queue API surface:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/operator-queue` | GET | List queue items (filter by `type=approval`) |
| `/api/operator-queue/{id}` | GET | Get a single item |
| `/api/operator-queue/{id}/respond` | POST | Submit the decision — `{response, response_text?}`. `422` when an approval's `response` is not an offered option; `409` when the item is no longer pending |
| `/api/operator-queue/{id}/cancel` | POST | Cancel a pending item |
| `/api/operator-queue/agents/{name}` | GET | Items scoped to a specific agent |

Asks addressed to a Workspace user are read and answered as that user, under the Workspace prefix (a Workspace session token or a platform JWT; agent keys are refused):

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/enterprise/client-portal/asks?agent_name=` | GET | Open asks addressed to the caller, optionally narrowed to one agent. Each carries `kind`, `title`, `question`, `options`, `expires_at`, `status` (`pending` or `expired`) and the `chat_id` it belongs to |
| `/api/enterprise/client-portal/asks/{id}/answer` | POST | Answer one — `{response, response_text?}`. Returns the ask with `status: "answered"` and `resume_requested` (whether answering started a turn). `422 empty_answer` / `response_not_an_offered_option`; `409` expired; a missing or not-yours ask is a uniform `404` |

See the [Operating Room doc](../operations/operating-room.md) for the full queue model.

### MCP Tools

Agents can inspect the queue programmatically (read-only):

| Tool | Description |
|------|-------------|
| `list_operator_queue` | List queue items, broad or filtered by `agent_name` |
| `get_operator_queue_item` | Fetch a single item by id |

Agent-scoped API keys see only items for the calling agent itself plus agents it has been explicitly permitted to access. Responding and cancelling remain human actions through the UI or the routes above.

## Limitations

- An answer wakes the agent only when its owner has turned wake-on-answer on; otherwise it waits for the agent's next turn. If the agent has neither a schedule nor a heartbeat, the agent is expected to say in the request how it should be re-triggered.
- Wake-on-answer is a per-agent switch, never per request — an agent cannot decide that answering it costs the answerer a turn.
- A wake that fails after the answer was recorded shows as a failed execution and an audit entry on the operator side; the Workspace confirmation reports the intent to start work, not its outcome.

## See Also

- [Operating Room](../operations/operating-room.md) — The Operations page where approvals surface (Needs Response / Notifications / Resolved tabs).
- [Mobile Admin](../sharing-and-access/mobile-admin.md) — Answering the queue from a phone.
- [Workspace](../sharing-and-access/workspace.md) — Where an addressed ask reaches its person.
- [Agent Configuration](../agents/agent-configuration.md#wake-on-operator-answer) — The per-agent wake-on-answer switch.
- [Scheduling](scheduling.md) — Automated triggers that often request approval before destructive actions.
