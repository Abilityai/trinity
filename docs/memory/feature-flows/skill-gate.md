# Feature: Skill Gate — approval before the executor sees the request (trinity-enterprise#751)

## Overview

A person marks a skill on an agent "requires approval by role X". When a request dispatched to that agent invokes the skill, the platform raises the approval **before** the agent sees the request. **Approve** runs it once, exactly as written; reject, expiry or cancel runs nothing. The requester is answered at once and told the outcome later.

The per-agent gate map is stored by trinity-enterprise#753. Until it lands, `skill_gate_service.list_skill_gates` returns nothing and the gate is inert.

Requirement: `requirements/security.md` §26.13 (OPS-001-GATE).

## User Story

As the owner of a finance agent, I want "pay an invoice" to run only after the person responsible approves that exact request, whoever asked (another agent, a schedule, a channel user), so that a business action never runs on an agent's judgment alone.

## Entry Points

- **`/chat`**: `routers/chat.py::chat_with_agent` → `dispatch_admission_service.admit_chat_request` (`src/backend/services/dispatch_admission_service.py:319`). Reached from the UI chat, MCP `chat_with_agent`, and connector `run_playbook` / `ask`.
- **`/task`**: `routers/chat.py::execute_parallel_task` → `chat_execution_service.dispatch_parallel_task` (`src/backend/services/chat_execution_service.py:2125`). Covers parallel tasks, self-tasks and pull-routed tasks.
- **The Workspace (the agent page's main chat)**: `client_portal/router.py::portal_chat` and `portal_chat_stream` → `client_portal/service.py::portal_chat`. The route passes `PortalPrincipal.is_person` through as `gate_is_person`.
- **Every other producer**: the backstop at step 1b of `TaskExecutionService.execute_task` (`src/backend/services/task_execution_service.py:1887`). This covers the scheduler, loops, fan-out, channels, rooms, sessions, A2A, public links, paid calls, operator resumes, validation and voice post-processing.
- **Voice tool**: `gemini_voice._execute_tool` (`src/backend/services/gemini_voice.py:1483`) reaches the agent without `execute_task`, so it refuses a gated skill instead of raising an approval.

## Frontend Layer

### Components

- **Operations → Needs Response** (`components/operator/QueueCard.vue`) and the **Workspace Inbox** render the approval as an ordinary ask. The question names who asked and previews the request, and `QueueProposal` shows `kind: gated_skill`, the skills, the exact input, the requester and the fingerprints. A dedicated card is trinity-enterprise#755.
- **A refused decision:** a 403 `not_addressee` (the approval was addressed to someone else) is shown on that card as an `InlineError` beside the controls (`components/operator/QueueCard.vue:134`, `data-testid="queue-not-addressee-notice"`) and stays until dismissed. `/m` does the same (`views/MobileAdmin.vue:1294`).
- **The Workspace thread:** a held turn is answered with the notice as a normal assistant reply, never a Failed turn. An approved run's result arrives later in the same thread ("**Finished**" plus the result).

### State Management

`stores/operatorQueue.js:68`: `notAddresseeItemId` names the card whose answer was refused. It is kept out of `error`, because every poll clears `error`.

### API Calls

- `POST /api/operator-queue/{id}/respond`: the decision, which may be refused with 403 `not_addressee`.
- Chat and task callers receive `202 {status: "pending_approval", …}`. The UI chat and task panels do not render that 202 yet; that work must land with trinity-enterprise#753.

## Backend Layer

### 1. The check — `enforce` (`src/backend/services/skill_gate_service.py:150`)

```
request → entry point
  ├─ triggered_by == "skill_gate"? ── yes → ungated (the gate's own notice)
  ├─ list_skill_gates(agent)       ── error → 503 gate_unavailable          (read_gates :281)
  ├─ find_gated_invocations(request_text, gates)      (utils/skill_invocation.py:39)
  │      none → ungated → dispatch as today
  ├─ approvers = role_addressing.resolve(agent, role)   none → 422 role_unassigned
  ├─ proven person ∈ approvers?  ── yes → self-approved → dispatch (+ audit, :641)
  ├─ refuse_only (voice)?         ── yes → 403 approval_not_available_here
  ├─ sanitised text > 6000 JSON bytes?  ── yes → 422 request_too_long
  ├─ request_id = gate- + sha256(agent, requester, occurrence)
  │      existing record → still waiting: replay (202) · decided: 409 request_<state>
  ├─ caps (:313): rate 10/min/requester, 10 pending/requester/agent, 50/agent → 429
  ├─ fingerprints (:521; docker exec as root, python3 -I -S) → 409 unreadable/missing/ambiguous
  ├─ create skill_gate_requests row (pending)  ← BEFORE the ask   (db/skill_gate_requests.py:84)
  ├─ ask_service.raise_ask(raised_by="gate", approval, Approve/Reject, to=role)   (_ask_body :362)
  │      the card carries the WHOLE request (what Approve runs) + a 500-char preview
  │      raise failed → record refused, 503 approval_unavailable (no pending record left)
  └─ raise SkillApprovalRequired → 202 {status: pending_approval, request_id, …}
```

**What is read.** The gate reads everything a requester put in front of the executor, and nothing the platform composed around it. Producers that compose a message pass `request_text` explicitly:

| Producer | What the gate reads |
|---|---|
| Channels | the sender's text |
| Workspace | the quoted reply + what the client typed |
| Public links | the visitor's message |
| Rooms | the participants' new messages |
| Operator resumes | the chosen answer + the free text |
| Operator endings | the reason |
| Validation | the operator's custom prompt |
| `/task` | `message` + `user_message` + `system_prompt` (all caller-supplied) |
| Fan-out | each subtask's message + the batch's `system_prompt` |
| Loops | no `request_text`, so the rendered iteration is read (what an approval would run) |

On `/task`, `message` is caller-supplied and may carry history, so an invocation quoted there is held: the gate cannot tell a quote from a request. Where the platform composes the history itself (the Workspace, channels, rooms, public links), only the new words are read. A held request replayed in a later cold turn's history therefore reaches the agent as text. That is the prose lane (trinity-enterprise#752) and is a stated limit, because scanning that history would re-hold every later turn of the thread.

**Who asked.**
- `/chat` and `/task` build the requester from the authenticated principal (`requester_from_principal`, `:570`).
- The backstop derives it from the call (`requester_for_dispatch`, `:596`), filling in whatever the call left off from the pre-created row (`_with_row_attribution`, `task_execution_service.py:1395`). The scheduler's `/api/internal/execute-task` sends neither the schedule id nor the person who pressed **Run now**, so the row is where they come from. As a result, each schedule is its own requester, named on the card as one bounded line, and **Run now** is asked by the person who pressed it.
- A Workspace turn is a person.

**Self-approval needs a proven person**, from one of two places:
- `is_person_principal` at the `/chat` and `/task` seams: a signed-in session or a user-scoped key, never an agent key, a system key, a connector, a portal delegate or the event loopback;
- the Workspace caller its route verified: `PortalPrincipal.is_person` → `portal_chat(gate_is_person=…)` → `execute_task(gate_requester=…)`.

The backstop never self-approves otherwise. Voice relays a model's paraphrase and stays unproven. Nothing is dispatched on a read that failed.

### 2. The decision — `may_end` and the observer

- Only a person in the ask's `resolved_to` may answer; an admin may cancel but never approve. `answer`, `cancel` and `bulk_cancel` all apply `may_end` (`src/backend/services/ask_service.py:156`), and the routes map `AskNotAddressee` to 403 `not_addressee`.
- `on_ending` (`skill_gate_service.py:754`) is registered in every worker at startup. It hops to the event loop and calls `resolve(request_id)` (`:814`). The operator-queue poll cycle runs `sweep()` (`:769`) after expiry, for endings no observer consumed.

### 3. Exactly once — `resolve` → `_approve` (`:858`) → `_dispatch_approved` (`:900`)

```
ask ended
  ├─ not approved → record denied | expired | cancelled → notify
  ├─ approved by someone not addressed → denied (approver_not_addressed)
  └─ approved
       ├─ claim: UPDATE … state pending→dispatching, dispatched_execution_id = new id
       │     (CAS, db/skill_gate_requests.py:154)
       ├─ agent gone / requesting agent gone / unreachable → not_run → notify
       ├─ fingerprint changed → stale → notify
       ├─ create row under the claimed id; capacity.acquire(queue_persistent)
       │     admitted → run_async_task(…) in the background; queued → the backlog drain runs it
       └─ dispatched → notify (with the run's execution id)
sweep (each a compare-and-set, after 5 min):
  dispatching, no row      → unknown → notify requester + approver (never re-run)
  dispatching, row exists  → dispatched → the notice the requester never got
  pending, ask not attached → attach (the next pass consumes its ending)
  pending, ask gone        → cancelled (approval_ask_missing) → notify; frees the cap slot
```

- A request from a public-facing surface (`public`, `paid`, channels) runs under the owner's public-channel caller prompt, re-derived at dispatch.
- The approved run is not gated again: the backstop finds the gate record whose `dispatched_execution_id` is the run's id.
- `DISPATCH_FIELDS` decides, for each `execute_task` field, whether the approved run re-uses it (freeze), gets it fresh (reset), or never replays it (drop). Dropped fields: session, batch, loop, canvas, attachments, and the seat-memory prompt.

### 4. Deleting or renaming the agent

`cancel_pending_for_agent` (`:1150`) is called from the delete and rename routes:
- it cancels each waiting record, so a later approval runs nothing;
- it ends the record's ask as the acting person, but only when that person may end it, and only a person: an agent key carries its owner's email, so under one the ask stays open and inert;
- it notifies the requester.

### Database Operations

`skill_gate_requests` exists on both tracks: SQLite `skill_gate_requests_table`, and Alembic `0088_skill_gate_requests` on top of `0087_pull_sync`. Columns:

| Group | Columns |
|---|---|
| Identity | `request_id` PK, `agent_name`, `ask_item_id` |
| The request | `skills`, `request_text` (sanitised), `fingerprints` |
| The requester | `requester_kind`, `requester_key`, `source_agent`, `requester_email`, `requester_execution_id`, `requester_mcp_key_id` |
| Dispatch | `origin_execution_id`, `triggered_by`, `dispatch`, `dispatched_execution_id` UNIQUE |
| State | `state`, `state_detail` |
| Timestamps | `created_at`, `decided_at`, `dispatched_at`, `notified_at` |

`state` moves `pending → dispatching → dispatched | stale | not_run | unknown`, or `pending → denied | expired | cancelled | refused`. Both agent columns are in `AGENT_REFS` (CASCADE).

## Side Effects

- **The requester's notice** (`_notify`, `skill_gate_service.py:1054`; text from `outcome_text`, `:1030`):
  - **Agent requester:** a platform task, trigger `skill_gate` (analytics bucket "Operator queue"), naming the request, the outcome, the originating execution and the run's execution id. It never says which person decided (#715), and it contains no slash.
  - **Person requester, including a Workspace caller:** an Inbox `alert` addressed to them, with a `gate-note-…` id that never counts against the agent's ask budget. It is worded for a person, without the `[Trinity]` marker. A person who decided their own request is not told of that decision (approved, rejected, cancelled). An approval that then could not run (`stale`, `not_run`, `unknown`) is news, and is sent once.
  - **Schedules, channel users, public visitors, paid callers, connectors:** the request id in the immediate answer only.
- **The approved run's result** goes back to the Workspace thread or the channel conversation it came from (`channel_completion_report._is_approved_gate_run`, `src/backend/services/channel_completion_report.py:683`), because an approved run was never answered inline.
- **Audit rows:** `skill_gate_outcome` once per decided record (ids only); `skill_gate_self_approved` once per execution the gate let through on the approver's own request.
- **A held run's row** is closed `skipped` with "Awaiting approval (gate-…)". The scheduler does not retry it, and a loop stops with `approval_required`.

## Error Handling

All responses go through the app handler `error_handlers.skill_gate_error` (`src/backend/error_handlers.py:79`) and carry `X-Trinity-Error-Code`.

| Case | Status / code | What the caller sees |
|---|---|---|
| Approval raised | 202 `approval_pending` | MCP: `pending_approval` ("do not retry"); fan-out: subtask `pending_approval` + `request_id`; A2A: `input-required` keyed on the request id; paid: 202, not settled; Workspace: the notice as the reply |
| Gate map unreadable | 503 `gate_unavailable` | nothing dispatched |
| Approval could not be raised | 503 `approval_unavailable` | no pending record left behind |
| Nobody fills the role | 422 `role_unassigned` | nothing raised |
| Skills with different approvers | 422 `mixed_approvers` | ask for them separately |
| Request too long for the card | 422 `request_too_long` | shorten it |
| Retry after the decision | 409 `request_<state>` | the decision, never "pending" |
| Skill missing / ambiguous / unreadable | 409 `gated_skill_not_installed` / `_ambiguous` / `_unreadable` | nothing raised |
| Too many waiting / too fast | 429 `approval_queue_full` / `approval_rate_limited` | MCP shows `refused`, never "agent busy" |
| Voice tool | 403 `approval_not_available_here` | "ask in chat instead" |
| Decided by a non-addressee | 403 `not_addressee` | the card says so; nothing is recorded |

On a refusal: the scheduler leaves the row `skipped`; channels, rooms and public links reply with the refusal text; the Workspace raises a named `gated` portal error; A2A returns `rejected`.

## Security Considerations

- **Budget:** gate asks have their own budget. The agent's ask caps and rate buckets never count them (`db/operator_queue._NOT_A_GATE_ROW`).
- **#715:** gate rows name people (who asked, who decided), so they are in `operator_queue_service._ABOUT_A_PERSON_ID_PREFIXES` (`src/backend/services/operator_queue_service.py:302`). A machine key's queue reads never return them (`routers/operator_queue.py:109`), and `get_my_ask` answers 404 for them (`:510`).
- **Fingerprint read:** the script runs as root through the image's interpreter with `-I -S`, so the agent's Python startup files (`sitecustomize`, a `.pth`, `PYTHON*` env) do not run inside it, and symlinks and ambiguous names are refused. This guards against accidental interference, not an adversarial executor: the agent user has passwordless sudo and could replace the interpreter itself.
- **Execution id:** `X-Trinity-Execution-Id` is kept only when it is one of the requesting agent's own executions. The MCP server forwards the platform header, never the model-typed `execution_id`.
- **Schedule names:** a name is free text an agent can set, so it is collapsed to one line of at most 80 characters before it appears on the approver's card.
- **Stated limits:**
  - an agent acting outside the skill with its own credential, or rewriting what the fingerprint read sees (capability confinement is the boundary for both);
  - the gap between the dispatch-time fingerprint and the moment the agent reads the files;
  - a revoked agent permission between request and approval is not re-checked;
  - plugin (`plugin:skill`) names are not matched;
  - prose-only requests and history replays (trinity-enterprise#752);
  - a session minted by a delegate key counts as a person (registered as debt).
- Security audit: `docs/security-reports/cso-diff-2026-10-02-ent751-gated-skills.md`.

## Testing

### Prerequisites

Until trinity-enterprise#753 ships, a gate exists only in a local build where `list_skill_gates` returns a map. Roles other than `primary` resolve only through an assignments provider that implements `people_for`.

### Test Steps (localhost, two accounts)

1. **The approver asks in the Workspace.** Send `/pay-invoice INV-1` → it runs directly, and one `skill_gate_self_approved` audit row is written.
2. **A schedule's Run now asks.** → the run is `skipped` ("Awaiting approval"); a card in Needs Response names the person who pressed Run now and shows the full request.
3. **Approve it.** → a new run executes the request, and the requester gets no Inbox notice because they decided it themselves.
4. **Reject one.** → nothing runs, and there is no notice.
5. **An agent asks** (`chat_with_agent`). → the agent reports pending and doesn't retry. On approve, the agent gets one `skill_gate` task stating the outcome with no email.
6. **A non-approver asks in the Workspace.** →
   - the notice comes back as a normal reply;
   - an admin's Approve is refused on the card (`not_addressee`);
   - when the approver approves, the result lands back in the asker's thread, and the asker's Inbox notice names the decider.

### Edge Cases

These are covered by unit tests:
- invisible or bidi characters in the token;
- a trailing dot, versus `/pay-invoice.v2`;
- the same Idempotency-Key from two requesters;
- a retry after the decision;
- a raise failure;
- the cap at 10 pending;
- two resolvers racing;
- each sweep case;
- a changed fingerprint;
- a deleted agent;
- the loopback token;
- an invocation hidden in `system_prompt`;
- a forged schedule name.

### Test Files

| File | What it covers |
|---|---|
| `tests/unit/test_ent751_skill_invocation.py` | the matcher (accept/reject table) |
| `tests/unit/test_ent751_skill_gate_requests_db.py` | the record, the state lattice, the claim, the reconcile reads, the registered migration, facade parity |
| `tests/unit/test_ent751_gate_budget.py` | gate asks kept off the agent's budget |
| `tests/unit/test_ent751_skill_gate_enforce.py` | the decision matrix |
| `tests/unit/test_ent751_skill_fingerprint_script.py` | the in-container script, executed |
| `tests/unit/test_ent751_gate_http_mapping.py` | 202 / refusal mapping and its registration |
| `tests/unit/test_ent751_gate_entries.py` | `/chat`, `/task`, the backstop, the Workspace requester, row attribution, the loopback principal |
| `tests/unit/test_ent751_gate_callers.py` | each producer's handling, including the Workspace turn and the approved-run report |
| `tests/unit/test_ent751_gate_dispatch.py` | endings, exactly-once, every sweep case, notices, `may_end`, #715 machine reads, delete/rename |
| `tests/unit/test_ent751_gate_guards.py` | discovery guards and the frozen-dispatch parity |
| `tests/unit/test_ent329_operator_resume.py`, `tests/unit/test_ent611_ask_endings.py` | what the gate reads on a resume and an ending |
| `src/frontend/tests/unit/queueCardNotAddressee.spec.js`, `mobileAdminNotAddressee.spec.js` | the refusal shown on the card |
| `src/mcp-server/src/chat-gate.test.ts` | the MCP results and the forwarded turn |

### Status

✅ Implemented and eyeballed on localhost (2026-10-03). It is inert until the gate map lands.

## Related Flows

- **Upstream:** the gate map (trinity-enterprise#753); the agent Skills tab "Requires approval" row (trinity-enterprise#754).
- **Downstream / shared:**
  - the ask sink and its endings (`operating-room.md`, trinity-enterprise#611);
  - the approval card (trinity-enterprise#755);
  - the completion report back to a channel or thread (ent#457);
  - the connector playbooks (`mcp-connector.md`).
- **Sibling:** the in-container hook for skills named only in prose (trinity-enterprise#752).
