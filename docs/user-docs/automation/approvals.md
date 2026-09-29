# Approvals

Human-in-the-loop approval gates surfaced through the Operations queue. An agent that needs authorization for a sensitive action parks an approval item and ends its turn; a person approves or rejects it — an operator from the Operations page or the mobile admin, or the Workspace user the agent addressed it to — and the agent reads the decision back and continues.

> 📺 **Watch:** [Trinity Platform Demo — operator queue & approvals](https://youtu.be/ivljtZqsxeo) *(May 2026)* · [all videos](../videos.md)

## Concepts

- **Queue item** — A row in the operator queue. Three request types: `approval` (a yes/no or multi-choice decision), `question` (freeform guidance), `alert` (acknowledgement only). Created by the agent, consumed by a person.
- **Options** — JSON array of choices for an `approval` (typically `["approve", "reject"]`, but agents may define richer sets like `["draft", "send", "discard"]`). The decision must be one of them.
- **Decision and note** — The answer has two parts. The **decision** (`response`) is what the agent reads: the chosen option, the typed answer to a question, or `acknowledged`. The **note** (`response_text`) is optional free text riding alongside a decision; it never stands alone.
- **Ask** — A queue item addressed to one Workspace user (`addressed_to_email`). It appears in that person's Workspace, where they answer it, and stays visible to operators in the queue. An item with no addressee is for the operator alone.
- **Priority** — `critical`, `high`, `medium`, `low`. Affects sort order in the queue.
- **Response window** — Optional `expires_at`. After expiry the item moves to `expired`; the agent treats that as "not approved — do not proceed". An answer that arrives after the deadline is refused (`409 expired`), even in the few seconds before the platform sweeps the item.
- **Re-ask** — An ask that raises again one of the agent's asks that expired, once the agent has new information. It names the expired ask (`supersedes_expired`), and the Operations cards show the link both ways: **Re-ask of …** on the new ask, **Re-asked as …** on the expired one. An expiry means *not approved*, so repeating an expired ask's exact action without naming it is refused.
- **How it ended** — Every item ends one of three ways — **answered**, **cancelled** or **expired** — and records who ended it (a person, or the timeout), when, and for a cancellation the optional reason the operator gave. The Resolved tab, the mobile admin and the Workspace all show it.

## How It Works

1. The agent reaches a step that needs authorization.
2. The agent raises the item with the `ask_operator` tool and **ends its turn** — it never waits in-turn for a human. The platform checks the item at once and answers with a receipt: a malformed item is refused with a named reason, and asking again with the same id returns the first receipt instead of a second item.
3. The older route still works for two releases: the agent appends an entry to `~/.trinity/operator-queue.json`, and the Operator Queue Sync Service picks it up within 5 seconds. The platform log names each agent still using the file.
4. The item appears on the **Operations** page under the **Needs Response** tab with a type pill — **Needs approval**, **Question** or **Heads up** — plus title, question, options, and any context the agent attached. Resolved items (responded, cancelled, expired) move to the **Resolved** tab. An item addressed to a Workspace user also appears in that person's Workspace (below).
5. A person answers. Only a person can answer or cancel an item — a browser session or a person's own API key; an agent's key is refused. The controls follow the type: for an approval, pick an option, optionally add a note, and click **Send**; for a question, type an answer and click **Send Answer**; for an alert, click **Got it**. Nothing is sent on a single tap of an option.
6. The agent reads the decision with `get_my_ask`, or in the turn the wake starts (below). For a queue-file item, the decision is also written back into the file within about 5 seconds (running agents only).
7. The agent acts on it. A queue-file item is then marked acknowledged in the file.

**When the agent acts on it.** By default the answer is read at the start of the agent's *next* turn — its next scheduled run, loop iteration or message. An agent with no next turn would wait indefinitely, so the agent's owner can turn on **Wake this agent when an ask it raised ends**: then answering starts one turn immediately and the agent acts on the decision in it (trigger `operator_response` in Executions). A cancellation or an expiry wakes it too, so it stops waiting for an answer that will not come (trigger `operator_ending`; one turn per agent however many of its items one sweep ended; an expiry says *"Denied by timeout; do not re-ask the same action without new information."*). Stopped agents are not woken. See [Wake When an Ask Ends](../agents/agent-configuration.md#wake-when-an-ask-ends).

**The decision must be one the agent offered.** For an approval that listed options, an answer outside that list is refused with a `422` that names the offered options; matching is exact, so `Approve` and `approve` are different answers. Questions, alerts and approvals that offered no options take free text.

**Platform heads-ups.** Trinity itself files items of other types into the same queue — for example a Workspace client rating a response as not useful, or an agent calling a playbook that does not exist. These carry no decision; they show **Got it** only, and acknowledging them sends nothing back to the agent.

WebSocket events fired along the way: `operator_queue_new` when the item arrives, `operator_queue_responded` when someone decides, `operator_queue_cancelled` when someone cancels it, `operator_queue_acknowledged` when the agent confirms it saw the decision.

### Asks addressed to a Workspace user

An item raised with `ask_operator` is addressed by role: `primary` (the default for approvals and questions) goes to the agent's owner, and `operator` (the default for alerts) goes to the operators with no single person. `approver` and `viewer` are refused until someone fills those roles. A queue-file entry can instead address an item to one person the agent is shared with by setting `addressed_to_email`. The address is checked at ingestion against the agent's own roster: someone the agent is not shared with, or a malformed value, turns the item into an ordinary operator ask rather than being refused. The `context` block of an ask is never shown to the addressee.

What that person sees in the [Workspace](../sharing-and-access/workspace.md):

- The ask renders above the composer of the chat it belongs to — and, for platform users, again under **Waiting on you** in the rail's **Work** tab. An ask raised outside any chat (by a scheduled run, say) attaches to the person's **Main** chat with that agent; from any other chat the card offers **Open the conversation** instead of controls.
- The sidebar header counts them (*N asks are waiting on your answer*), and the agent's row carries its own badge — distinct from the unread-replies count, and never hidden behind **Show all**.
- The controls are the same three as the Operations page: option → optional note (*Add a note (optional)…*) → **Send**; a typed answer → **Send**; **Got it** for an alert.
- Answering clears the ask from every count at once, keeps it on the card as **Answered by you**, and shows a short confirmation: **Sent.**, or **Sent — {agent} is picking this up.** when the owner has turned on the wake.
- An ask that ended — answered, cancelled or expired — stays listed for 7 days with how it ended (**by you**, **by the operator**, or expired) and when, without answer controls. It is not silently removed, it drops out of the count, and **Waiting on you** lists only what is still pending. The operator's reason for cancelling is never shown to you.
- The Workspace refreshes asks every 20 seconds while the tab is visible.

An ask is answered through the Workspace's own route (below), which records the answer with the same write-back, audit fields and wake-on-answer behaviour as an operator's. A client whose share was revoked stops seeing the ask.

### Bulk Operations

The Operations page offers a per-tab **Clear All** that hides resolved items, and pending items can be bulk-cancelled. A cancelled item's agent sees it marked cancelled in its queue file on its next turn, and running agents set to wake when their asks end are woken once. Because cancellation can race with a response, submitting a decision — or a cancel — returns **409** if the item left the `pending` state in the meantime (for example, it was cancelled by a bulk operation) — refresh the queue and re-check before retrying.

### From a phone

The [mobile admin](../sharing-and-access/mobile-admin.md) at `/m` answers the same queue with the same three controls.

## For Agents

Approvals share the operator-queue API surface:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/operator-queue` | GET | List queue items (filter by `type=approval`) |
| `/api/operator-queue/{id}` | GET | Get a single item |
| `/api/operator-queue/{id}/respond` | POST | Submit the decision — `{response, response_text?}`. A person only (`403 person_required` for an agent's or the system key). `422` when an approval's `response` is not an offered option; `409` when the item is no longer pending, `409 expired` past its deadline |
| `/api/operator-queue/{id}/cancel` | POST | Cancel a pending item — optional `{reason}` (≤ 500 characters). A person only; `409` when the item is no longer pending |
| `/api/operator-queue/bulk-cancel` | POST | Cancel listed pending items — `{ids, reason?}`; returns `{cancelled, skipped, batch_id}`. A person only |
| `/api/agents/{name}/operator-queue` | POST | Raise an item as the agent itself — `{request_id, title, question?, type?, options?, priority?, context?, proposal?, to?, expires_at?, supersedes_expired?}`, with the agent's own key only (`403 agent_identity_required` otherwise). `201` with a receipt (the role it went to, never a person's email); `200` with the first receipt when the id was already used; a named `422` for a malformed item, `429 rate_limited` or `queue_full` |
| `/api/agents/{name}/operator-queue/{request_id}` | GET | An agent's OWN item by the id it chose — with the agent's own key only (`403 agent_identity_required` otherwise). How it stands and how it ended; no person's email. Still readable after Clear All |
| `/api/operator-queue/agents/{name}` | GET | Items scoped to a specific agent |

Asks addressed to a Workspace user are read and answered as that user, under the Workspace prefix (a Workspace session token or a platform JWT; agent keys are refused):

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/enterprise/client-portal/asks?agent_name=&include_ended=` | GET | Open asks addressed to the caller, optionally narrowed to one agent; `include_ended=true` adds the ones that ended in the last 7 days. Each carries `kind`, `title`, `question`, `options`, `expires_at`, `status` (`pending`, `answered`, `cancelled` or `expired`), `ended_at` / `ended_by` (`you`, `operator` or `timeout`) and the `chat_id` it belongs to |
| `/api/enterprise/client-portal/asks/{id}/answer` | POST | Answer one — `{response, response_text?}`. A person only: the platform's system key gets `403 person_required`. Returns the ask with `status: "answered"` and `resume_requested` (whether answering started a turn). `422 empty_answer` / `response_not_an_offered_option`; `409` expired; a missing or not-yours ask is a uniform `404` |

See the [Operating Room doc](../operations/operating-room.md) for the full queue model.

### MCP Tools

Agents can inspect the queue and resolve a pending item programmatically:

| Tool | Description |
|------|-------------|
| `list_operator_queue` | List queue items, broad or filtered by `agent_name` |
| `get_operator_queue_item` | Fetch a single item by id |
| `respond_to_operator_queue(item_id, response, response_text?)` | Submit the decision for a pending item, with a person's own API key — the same rules as the `respond` route: `response` must be an offered option, an item that is no longer pending (or past its deadline) returns a structured error, and an agent's key is refused |
| `ask_operator(request_id, title, …)` | The calling agent raises an item as itself and gets a receipt: whether it was created or replayed, the role it went to, and `wakes_on_ending` (whether the agent will be woken when the item ends). `expires_at` needs a timezone and must be at least 15 minutes out; a re-ask names the expired item in `supersedes_expired`. A refusal comes back as its named code. Acts as the agent the key belongs to — there is no agent parameter |
| `get_my_ask(request_id)` | The calling agent reads back one of its own items by the id it chose: status, the answer, and how it ended (`disposition`, `disposed_at`, `disposed_by`, the operator's `disposition_reason`). Acts as the agent the key belongs to — there is no agent parameter |

Agent-scoped API keys see only items for the calling agent itself plus agents it has been explicitly permitted to access. Answering and cancelling are a person's act: an agent's own key — or the system key — is refused (`403 person_required`), so an agent can never approve its own request. Every turn's Execution Context also lists the agent's items that ended in the last 24 hours (ids, how and when — no text), so an agent that slept through an ending still learns of it.

## Limitations

- An ending wakes the agent only when its owner has turned the wake on; otherwise it waits for the agent's next turn. A stopped agent is never woken (the skip is recorded in the audit log); it reads the ending when it next runs. If the agent has neither a schedule nor a heartbeat, the agent is expected to say in the request how it should be re-triggered.
- The wake is a per-agent switch, never per request — an agent cannot decide that ending its request costs someone a turn. Only the agent's owner or an admin can change the switch, and an agent's own key is refused.
- A wake that fails after the ending was recorded shows as a failed execution and an audit entry on the operator side; the Workspace confirmation reports the intent to start work, not its outcome. A platform restart in the instant between the two can lose the wake; the agent still reads the ending back.
- Items the platform raises itself (heads-ups, alarms) never wake an agent, and neither does cancelling an item the agent had already closed on its side.
- The `approver` and `viewer` roles are refused until someone fills them; address such an item to `primary` or `operator` for now.
- Ephemeral agents keep raising items through the queue file until they get the native path.

## See Also

- [Operating Room](../operations/operating-room.md) — The Operations page where approvals surface (Needs Response / Notifications / Resolved tabs).
- [Mobile Admin](../sharing-and-access/mobile-admin.md) — Answering the queue from a phone.
- [Workspace](../sharing-and-access/workspace.md) — Where an addressed ask reaches its person.
- [Agent Configuration](../agents/agent-configuration.md#wake-when-an-ask-ends) — The per-agent wake switch.
- [Scheduling](scheduling.md) — Automated triggers that often request approval before destructive actions.
