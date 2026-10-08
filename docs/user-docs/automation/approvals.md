# Approvals

Human-in-the-loop approval gates surfaced through the Operations queue. An agent that needs authorization for a sensitive action parks an approval item and ends its turn; a person approves or rejects it — an operator from the Operations page or the mobile admin, or the Workspace user the agent addressed it to — and the agent reads the decision back and continues.

> 📺 **Watch:** [Trinity Platform Demo — operator queue & approvals](https://youtu.be/ivljtZqsxeo) *(May 2026)* · [all videos](../videos.md)

## Concepts

- **Queue item** — A row in the operator queue. Three request types: `approval` (a yes/no or multi-choice decision), `question` (freeform guidance), `alert` (acknowledgement only). Created by the agent, consumed by a person.
- **Options** — JSON array of choices for an `approval` (typically `["approve", "reject"]`, but agents may define richer sets like `["draft", "send", "discard"]`). The decision must be one of them, or the platform's reserved **Something else** (`"(something else)"`), which every approval offers and an agent may not list itself.
- **Atomic ask** — One decision per ask, with a short title and a few short options. The platform enforces the shape on items an agent raises: at most 5 options (Something else not counted), each at most 60 characters, and a title of at most 120 characters. The reasoning belongs in the question and what each option will do belongs in the proposal. See [Authoring limits](#authoring-limits).
- **Decision and note** — The answer has two parts. The **decision** (`response`) is what the agent reads: the chosen option, the typed answer to a question, or `acknowledged`. The **note** (`response_text`) is optional free text riding alongside a decision; it never stands alone. With **Something else** it is not optional: it is the instruction the agent re-plans from.
- **Ask** — A queue item addressed to one Workspace user (`addressed_to_email`). It appears in that person's Workspace, where they answer it, and stays visible to operators in the queue. An item with no addressee is for the operator alone.
- **Priority** — `critical`, `high`, `medium`, `low`. Affects sort order in the queue.
- **Response window** — Optional `expires_at`. After expiry the item moves to `expired`; the agent treats that as "not approved — do not proceed". An answer that arrives after the deadline is refused (`409 expired`), even in the few seconds before the platform sweeps the item.
- **Re-ask** — An ask that raises again one of the agent's asks that expired, once the agent has new information. It names the expired ask (`supersedes_expired`), and the Operations cards show the link both ways: **Re-ask of …** on the new ask, **Re-asked as …** on the expired one. An expiry means *not approved*, so repeating an expired ask's exact action without naming it is refused.
- **Proposal** — Optional structured description of the exact action an approval would allow. It is shown read-only, as text, on every card that offers the decision (the Operations card, the mobile admin and the Workspace ask) and stays on the resolved card, so the record says what was decided.
- **Aging** — A pending item that waits longer than the aging bound (default 24 hours) is marked **Waiting** on its card, and a queue-file item gets a `platform.aging_since` timestamp written into the agent's entry. An admin sets the bound in **Settings → Access → Operator queue aging bound (hours)**; `0` turns it off.
- **How it ended** — Every item ends one of four ways — **answered**, **cancelled**, **dismissed** (the addressee chose not to answer, in the Workspace) or **expired** — and records who ended it (a person, the timeout, or for a platform heads-up the platform itself), when, and for a cancellation the optional reason the operator gave. The Resolved tab, the mobile admin and the Workspace all show it.

## How It Works

1. The agent reaches a step that needs authorization.
2. The agent raises the item with the `ask_operator` tool and **ends its turn** — it never waits in-turn for a human. The platform checks the item at once and answers with a receipt: a malformed item is refused with a named reason, and asking again with the same id returns the first receipt instead of a second item.
3. The older route still works for two releases: the agent appends an entry to `~/.trinity/operator-queue.json`, and the Operator Queue Sync Service picks it up within 5 seconds. The platform log names each agent still using the file.
4. The item appears on the **Operations** page under the **Needs Response** tab with a type pill — **Needs approval**, **Question** or **Heads up** — plus title, question, options, and any context the agent attached. The question renders as markdown; the title, the option labels and the given answer render inline formatting only (bold, code, links). The same rendering applies on the Operations page, the mobile admin and the Workspace. Resolved items (responded, cancelled, expired) move to the **Resolved** tab. An item addressed to a Workspace user also appears in that person's Workspace (below).
5. A person answers. Only a person can answer or cancel an item — a browser session or a person's own API key; an agent's key is refused. The controls follow the type: for an approval, pick an option, optionally add a note, and click **Send**; for a question, type an answer and click **Send Answer**; for an alert, click **Got it**. Nothing is sent on a single tap of an option.
6. The agent reads the decision with `get_my_ask`, or in the turn the wake starts (below). For a queue-file item, the decision is also written back into the file within about 5 seconds (running agents only).
7. The agent acts on it. A queue-file item is then marked acknowledged in the file.

**When the agent acts on it.** By default the answer is read at the start of the agent's *next* turn — its next scheduled run, loop iteration or message. An agent with no next turn would wait indefinitely, so the agent's owner can turn on **Wake this agent when an ask it raised ends**: then answering starts one turn immediately and the agent acts on the decision in it (trigger `operator_response` in Executions). A cancellation, a dismissal or an expiry wakes it too, so it stops waiting for an answer that will not come (trigger `operator_ending`; one turn per agent however many of its items one sweep ended; an expiry says *"Denied by timeout; do not re-ask the same action without new information."*). Stopped agents are not woken. See [Wake When an Ask Ends](../agents/agent-configuration.md#wake-when-an-ask-ends).

**The decision must be one the agent offered — or Something else.** For an approval that listed options, an answer outside that list is refused with a `422` that names the offered options; matching is exact, so `Approve` and `approve` are different answers. The one exception is the reserved `(something else)`: it means none of the options is approved, the agent carries out none of them, and your instruction (required, up to 4000 characters) travels in `response_text`. It is refused on a question or alert (`reserved_value`), without an instruction (`instruction_required`) and on an approval the platform raised itself, such as a skill gate (`not_off_menu`) — those show no **Something else** button. Questions, alerts and approvals that offered no options take free text.

**When the agent's copy and the queue disagree.** For a queue-file item, the platform compares every entry in the agent's file with the queue on each cycle and shows a badge when something is off. A healthy card shows no badge.

| Badge | Meaning |
|-------|---------|
| **Changed by the agent** | The agent rewrote the item after it was ingested. You are reading the original. |
| **Closed by the agent** | The agent closed the item on its side; it still waits for you here. |
| **Gone from the agent** | The agent's file no longer carries the item. |
| **Re-used id** | The agent used the id again after the item closed; the new entry was not admitted. |
| **Unconfirmed** | The platform could not check the agent's copy — the agent is stopped or unreachable, or its file is unreadable. Hover for the reason and when it was last confirmed. |
| **Answer not delivered** | Your answer or cancellation did not reach the agent's file. Hover for why; most causes retry on their own, and an answer to a stopped agent lands when it starts. |
| **Waiting** | The item is past the aging bound. |

Answering an item marked **Changed by the agent** or **Closed by the agent** is refused once (`409 item_diverged`): the card refreshes and asks you to review it, and the next **Send** answers anyway. The Operations header counts items whose answer did not reach the agent and items the agent closed, and offers one-click cancel of the closed ones. The platform never writes over an agent's queue file it cannot parse, and delivers an answer only into an entry that still matches what you answered.

**Platform heads-ups.** Trinity itself files items of other types into the same queue — for example a Workspace client rating a response as not useful, or an agent calling a skill that does not exist. These carry no decision; they show **Got it** only, and acknowledging them sends nothing back to the agent.

Some heads-ups describe a *condition* rather than an event — for example a subscription running low on headroom, a skills-library URL Trinity refused to adopt, a stale system-agent base image, or an agent's dispatch breaker gone dormant. For those, Trinity keeps **one pending row per condition**:

- A new reading of the same condition updates the existing row instead of filing another. A card seen more than once reads *seen N times · last seen <when>*.
- When the condition clears, the platform ends the row itself. The card reads **Ended by the platform — the condition cleared**.
- A row that nobody acts on expires 14 days after its last reading (`OPERATOR_PLATFORM_ALERT_LIFETIME_DAYS`). An expired heads-up reads **Expired — nobody acted on it**.
- After a person ends one, the same reading files nothing for 7 days (`OPERATOR_PLATFORM_ALERT_SNOOZE_DAYS`), unless its priority rises (for example a headroom warning turning critical).

Other platform heads-ups still file one row per event; they move onto the same model in a later release.

WebSocket events fired along the way: `operator_queue_new` when the item arrives, `operator_queue_responded` when someone decides, `operator_queue_cancelled` when someone cancels it, `operator_queue_acknowledged` when the agent confirms it saw the decision.

### Asks addressed to a Workspace user

An item raised with `ask_operator` is addressed by role: `primary` (the default for approvals and questions) goes to the agent's owner, and `operator` (the default for alerts) goes to the operators with no single person. `approver` and `viewer` are refused until someone fills those roles. A queue-file entry can instead address an item to one person the agent is shared with by setting `addressed_to_email`. The address is checked at ingestion against the agent's own roster: someone the agent is not shared with, or a malformed value, turns the item into an ordinary operator ask rather than being refused. The `context` block of an ask is never shown to the addressee.

What that person sees in the [Workspace](../sharing-and-access/workspace.md):

- Every ask waiting on the person is in their Workspace **Inbox** (the **Action** tab, which can be narrowed to one agent). An ask raised during a chat turn appears as a tile inside that chat's conversation, placed where it was asked, and you answer it right there; once it has ended it stays in that chat's history as one muted line (kind · title · how it ended · when). An ask raised outside any chat — by a scheduled run, a loop or a gate — appears in no chat, only in the **Inbox**; your reply to it lands in your **Main** chat with that agent. The agent's **Work** tab (platform users) and **Info** tab (everyone) carry one line, *N asks waiting on you · Open in Inbox*.
- The sidebar header counts them (*N asks are waiting on your answer*), and the agent's row carries its own badge — distinct from the unread-replies count, and never hidden behind **Show all**.
- The controls are the same three as the Operations page: option → optional note (*Add a note (optional)…*) → **Send**, or **Something else** (or start typing with no option picked) → instruction → **Send**; a typed answer → **Send**; **Got it** for an alert.
- **Discuss** (on an approval or a question) opens a chat with the agent about that ask, titled after it, so you can ask "why?" before you decide. The ask stays pending and is still answered on its own card, which heads that chat. One chat per ask: clicking again shows **Continue discussion** and reopens the same chat. If you answer after discussing, and the owner has turned on the wake, the agent's result is posted into that discussion chat.
- **Dismiss** (on an approval or a question) ends the ask without answering it — for an ask that needs no decision. There is no confirmation: the card shows *Dismissed — the agent hears nothing until this closes.* with an **Undo** for 5 seconds, then the dismissal is sent. The agent reads it as `dismissed` with an empty answer and is told not to raise the same ask again straight away; an agent set to wake when its asks end is woken once.
- Answering clears the ask from every count at once, keeps it on the card as **Answered by you**, and shows a short confirmation: **Sent.**, or **Sent — {agent} is picking this up.** when the owner has turned on the wake.
- An ask that ended — answered, dismissed, cancelled or expired — stays listed in the Inbox for 7 days with how it ended (**by you**, **by the operator**, **by the platform**, or expired) and when, without answer controls; in the chat that raised it, it stays as a muted line for as long as the queue keeps it. It is not silently removed, and it drops out of the count. The operator's reason for cancelling is never shown to you.
- The Workspace refreshes asks every 20 seconds while the tab is visible.

An ask is answered through the Workspace's own route (below), which records the answer with the same write-back, audit fields and wake-on-answer behaviour as an operator's. A client whose share was revoked stops seeing the ask.

### Authoring limits

Trinity holds items an agent raises to a few hard limits, so that each one is a single decision a person can take at a glance:

| Limit | Default | Setting | Refusal |
|-------|---------|---------|---------|
| Options per approval (Something else not counted) | 5 | `OPERATOR_QUEUE_MAX_OPTIONS` | `too_many_options` |
| Characters per option | 60 | `OPERATOR_QUEUE_OPTION_MAX_CHARS` | `option_too_long` |
| Characters in the title | 120 | `OPERATOR_QUEUE_ASK_TITLE_MAX_CHARS` | `title_too_long` |
| An option that imitates **Something else** | — | — | `invalid_options` |

`ask_operator` refuses an item over a limit with a `422` that names the code and the limit in force, and nothing is filed. A queue-file entry over a limit is **held**: it stays `pending` in the file, no queue row is created, and the file's `platform.ingestion` block lists its id under `invalid_options` or `invalid_title` beside `max_options`, `max_option_chars` and `max_title_chars`. Fix the entry and the next sync admits it. An admin can raise each limit in `.env`; each has a floor (2 options, 16 characters, a 40-character title).

**Volume limits.** Each agent may have at most 25 pending items of its own (`OPERATOR_QUEUE_MAX_PENDING_PER_AGENT`), and may create at most 60 per minute (300 per minute across the fleet). Over a limit, `ask_operator` answers `429 queue_full` or `429 rate_limited`; a queue-file entry is held rather than dropped and admitted on a later cycle, and the file's `platform.ingestion` block says why. Trinity files one **queue flood** heads-up per episode — a second needs the condition to clear first. Platform heads-ups never count toward an agent's own 25.

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
| `/api/operator-queue/{id}/respond` | POST | Submit the decision — `{response, response_text?, acknowledge_divergence?}`. A person only (`403 person_required` for an agent's or the system key). `422` when an approval's `response` is not an offered option (`response_not_an_offered_option`), or is `(something else)` without an instruction (`instruction_required`), on a non-approval (`reserved_value`) or on a gate approval (`not_off_menu`); `409` when the item is no longer pending, `409 expired` past its deadline, `409 item_diverged` when the agent changed or closed the item (send again with `acknowledge_divergence: true` to answer anyway) |
| `/api/operator-queue/{id}/cancel` | POST | Cancel a pending item — optional `{reason}` (≤ 500 characters). A person only; `409` when the item is no longer pending |
| `/api/operator-queue/bulk-cancel` | POST | Cancel listed pending items — `{ids, reason?}`; returns `{cancelled, skipped, batch_id}`. A person only |
| `/api/agents/{name}/operator-queue` | POST | Raise an item as the agent itself — `{request_id, title, question?, type?, options?, priority?, context?, proposal?, to?, expires_at?, supersedes_expired?}`, with the agent's own key only (`403 agent_identity_required` otherwise). `201` with a receipt (the role it went to, never a person's email); `200` with the first receipt when the id was already used; a named `422` for a malformed item or one over the [authoring limits](#authoring-limits) (`too_many_options`, `option_too_long`, `invalid_options`, `title_too_long`), `429 rate_limited` or `queue_full`. An item raised during a Workspace chat turn attaches to that chat |
| `/api/agents/{name}/operator-queue/{request_id}` | GET | An agent's OWN item by the id it chose — with the agent's own key only (`403 agent_identity_required` otherwise). How it stands and how it ended; no person's email. Still readable after Clear All |
| `/api/operator-queue/agents/{name}` | GET | Items scoped to a specific agent |
| `/api/operator-queue/stats` | GET | Counts by status, type, priority and agent, over the agents you can access |
| `/api/operator-queue/clear-resolved` | POST | Hide acknowledged, cancelled and expired items — optional `{agent_name}`; returns `{cleared}`. Answered items still waiting for the agent to acknowledge them stay listed |

Asks addressed to a Workspace user are read and answered as that user, under the Workspace prefix (a Workspace session token or a platform JWT; agent keys are refused):

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/enterprise/client-portal/asks?agent_name=&include_ended=&chat_id=` | GET | Open asks addressed to the caller, optionally narrowed to one agent; `include_ended=true` adds the ones that ended in the last 7 days; `chat_id` returns the asks raised during a turn of that chat, plus the ask that chat discusses, ended ones included with no 7-day limit. Each carries `kind`, `title`, `question`, `options`, `proposal`, `expires_at`, `status` (`pending`, `answered`, `dismissed`, `cancelled` or `expired`), `ended_at` / `ended_by` (`you`, `operator`, `platform` or `timeout`), the `chat_id` it belongs to, `raised_in_turn` (whether a chat turn raised it) and `discussion_chat_id` (the chat that discusses it, once opened) |
| `/api/enterprise/client-portal/asks/{id}/answer` | POST | Answer one — `{response, response_text?}`. A person only: the platform's system key gets `403 person_required`. Returns the ask with `status: "answered"` and `resume_requested` (whether answering started a turn). `422 empty_answer` / `response_not_an_offered_option` / `instruction_required` / `reserved_value` / `not_off_menu`; `409` expired; a missing or not-yours ask is a uniform `404` |
| `/api/enterprise/client-portal/asks/{id}/dismiss` | POST | End an ask without answering it. A person only; 60 per minute. Returns the ask with `status: "dismissed"`; an ask that already ended comes back as it stands with a `200` |
| `/api/enterprise/client-portal/asks/{id}/discuss` | POST | Open, or continue, the chat that discusses this ask — `{chat_id, agent_name, title, created, ask}` (`created: false` when it continued an earlier one). A person only; 30 per minute. `422 not_discussable` for an alert; `409 already_resolved` when the ask ended before a chat was opened |
| `/api/enterprise/client-portal/asks/{id}/context` | GET | Where the ask came from (its chat, and the three messages before it when the chat is yours), the run that raised it (schedule, loop, turn, …), and your recent answers to this agent. No cost and no execution id. 120 per minute |

See the [Operating Room doc](../operations/operating-room.md) for the full queue model.

### MCP Tools

Agents can inspect the queue and resolve a pending item programmatically:

| Tool | Description |
|------|-------------|
| `list_operator_queue` | List queue items, broad or filtered by `agent_name`. Returns `count`, `total`, `has_more`, `next_cursor` and `next_offset`; page with `cursor="start"`, then `cursor=next_cursor` until `has_more` is `false`. `null` means not verified |
| `get_operator_queue_item` | Fetch a single item by id |
| `respond_to_operator_queue(item_id, response, response_text?, acknowledge_divergence?)` | Submit the decision for a pending item, with a person's own API key — the same rules as the `respond` route: `response` must be an offered option or `(something else)` with the instruction in `response_text`, an item that is no longer pending (or past its deadline, or changed by the agent without `acknowledge_divergence`) returns a structured error, and an agent's key is refused |
| `ask_operator(request_id, title, …)` | The calling agent raises an item as itself and gets a receipt: whether it was created or replayed, the role it went to, and `wakes_on_ending` (whether the agent will be woken when the item ends). `expires_at` needs a timezone and must be at least 15 minutes out; a re-ask names the expired item in `supersedes_expired`. The tool description carries five authoring rules — one decision per ask, a one-line title, options that name the choice only, few options, and context written for people — and the [authoring limits](#authoring-limits) are enforced. A refusal comes back as `{success: false, status, code, message}`. Acts as the agent the key belongs to — there is no agent parameter |
| `get_my_ask(request_id)` | The calling agent reads back one of its own items by the id it chose: status, the answer, and how it ended (`disposition` — `answered`, `cancelled`, `dismissed` or `expired` — `disposed_at`, `disposed_by`, the operator's `disposition_reason`). Acts as the agent the key belongs to — there is no agent parameter |

Agent-scoped API keys see only items for the calling agent itself plus agents it has been explicitly permitted to access. Answering and cancelling are a person's act: an agent's own key — or the system key — is refused (`403 person_required`), so an agent can never approve its own request. Only a person (a browser session or the person's own API key) reads a whole queue row. An agent's key, the system key and other machine keys get the row without any person's identity — no answerer's email, no addressee, no name of who ended it — and never see the platform's heads-ups about a person (such as a Workspace client's complaint). The answer written back into a queue file carries `response`, `response_text` and `responded_at`, not who answered. Every turn's Execution Context also lists the agent's items that ended in the last 24 hours (ids, how and when — no text), so an agent that slept through an ending still learns of it.

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
