# Feature: Skill Gate — approval before the executor sees the request (trinity-enterprise#751)

## Overview

A person marks a skill on an agent "requires approval by role X" (the per-agent map is trinity-enterprise#753's storage; until it lands, `skill_gate_service.list_skill_gates` returns nothing and the gate is inert). When a request dispatched to that agent invokes the skill, the platform raises the approval **before** the agent sees the request. Approve runs it once, exactly as written; reject, expiry or cancel runs nothing. The requester is answered at once and told the outcome later. Requirement: `requirements/security.md` §26.13 (OPS-001-GATE).

## User Story

As the owner of a finance agent, I want "pay an invoice" to run only after the person responsible approves that exact request, whoever asked — another agent, a schedule, a channel user — so a business action never runs on an agent's judgment alone.

## Entry Points

- **`/chat`** — `routers/chat.py::chat_with_agent` → `dispatch_admission_service.admit_chat_request` (UI chat, MCP `chat_with_agent`, connector `run_playbook` / `ask`).
- **`/task`** — `routers/chat.py::execute_parallel_task` → `chat_execution_service.dispatch_parallel_task` (parallel, self-task, pull-routed).
- **Every other producer** — the backstop at step 1b of `TaskExecutionService.execute_task` (scheduler, loops, fan-out, channels, the Workspace, rooms, sessions, A2A, public links, paid calls, operator resumes, validation, voice post-processing).
- **Voice tool** — `gemini_voice._execute_tool` (reaches the agent without `execute_task`; refuses, never raises an approval).

## Flow

### 1. The check — `services/skill_gate_service.py::enforce`

```
request → entry point
  ├─ triggered_by == "skill_gate"? ── yes → ungated (the gate's own notice)
  ├─ list_skill_gates(agent)       ── error → 503 gate_unavailable
  ├─ find_gated_invocations(request_text, gates)   (utils/skill_invocation.py)
  │      none → ungated → dispatch as today
  ├─ approvers = role_addressing.resolve(agent, role)   none → 422 role_unassigned
  ├─ proven person ∈ approvers?  ── yes → self-approved → dispatch (+ audit)
  ├─ refuse_only (voice)?         ── yes → 403 approval_not_available_here
  ├─ sanitised text > 6000 JSON bytes?  ── yes → 422 request_too_long
  ├─ request_id = gate- + sha256(agent, requester, occurrence)
  │      existing record → still waiting: replay (202) · decided: 409 request_<state>
  ├─ caps: rate 10/min/requester, 10 pending/requester/agent, 50/agent → 429
  ├─ fingerprints (docker exec as root, python3 -I -S) → 409 on unreadable/missing/ambiguous
  ├─ create skill_gate_requests row (pending)  ← BEFORE the ask
  ├─ ask_service.raise_ask(raised_by="gate", type approval, options Approve/Reject, to=role)
  │      the card carries the WHOLE request (what Approve runs) + a 500-char preview
  │      raise failed → record refused, 503 approval_unavailable (no pending record left)
  └─ raise SkillApprovalRequired → 202 {status: pending_approval, request_id, …}
```

- The gate reads everything a requester put in front of the executor and nothing the platform composed around it. Producers that compose a message pass `request_text` explicitly (channels: the sender's text; Workspace: the quoted reply + what the client typed; public links: the visitor's message; rooms: the participants' new messages; operator resumes: the chosen answer + the free text; operator endings: the reason; validation: the operator's custom prompt; `/task`: `message` + `user_message` + `system_prompt`, all caller-supplied; fan-out: each subtask's message + the batch's `system_prompt`). Loops pass none, so the rendered iteration is read — what an approval would run. On `/task`, where `message` is caller-supplied and may carry history, an invocation quoted there is held: the gate cannot tell a quote from a request. Where the platform composes the history itself (the Workspace, channels, rooms, public links) only the new words are read, so a held request replayed in a later cold turn's history reaches the agent as text. That is the prose lane (trinity-enterprise#752), registered as debt: scanning that history would re-hold every later turn of the thread.
- Self-approval needs a proven person: `is_person_principal` at the `/chat` and `/task` seams (a signed-in session or a user-scoped key; never an agent key, a system key, a connector, a portal delegate or the event loopback), or the Workspace caller its route verified (`PortalPrincipal.is_person` → `portal_chat(gate_is_person=…)` → `execute_task(gate_requester=…)`). Otherwise the backstop derives the requester (`requester_for_dispatch`) from the call, filling what the call left off from the pre-created row — the scheduler's `/api/internal/execute-task` sends neither the schedule id nor the person who pressed Run now — so each schedule is its own requester (named on the card) and Run now is asked by that person. A Workspace turn is a person, unproven. The backstop never self-approves. Voice relays a model's paraphrase and stays unproven.
- Nothing is dispatched on a read that failed.

### 2. What the caller gets

| Surface | Pending | Refused |
|---|---|---|
| HTTP routes | 202 `pending_approval` (app handler `error_handlers.skill_gate_error`) | refusal status + body under `detail` |
| Both | `X-Trinity-Error-Code` | `X-Trinity-Error-Code` |
| MCP `chat_with_agent` / connector | structured `pending_approval` result ("do not retry") | structured `refused` (its 429 is never "agent busy") |
| Scheduler | row `skipped`, no retry | row `skipped` |
| Loop | loop `stopped`, `approval_required` | same |
| Fan-out | subtask `pending_approval` + `request_id` (from the gate record) | `failed` |
| Channels / rooms / public / Workspace | the notice text as the reply / a room line / the reply (Workspace) | the reply / a room line / a named portal error (`gated`) |
| A2A | task `input-required`, id = request id | task `rejected` |
| Paid (x402) | 202, payment not settled | refusal, not settled |

### 3. The decision — `ask_service` (`may_end`) and the observer

- Only a person in the ask's `resolved_to` may answer; an admin may cancel, never approve (`answer`, `cancel`, `bulk_cancel` all apply `may_end`; routes map `AskNotAddressee` to 403 `not_addressee`). The Operations card shows that refusal beside its controls until dismissed (`stores/operatorQueue.js::notAddresseeItemId`); hiding Approve/Reject from non-addressees is the approval card's work (trinity-enterprise#755).
- `skill_gate_service.on_ending` (registered in every worker at startup) hops to the loop and calls `resolve(request_id)`; the operator-queue poll cycle runs `sweep()` after expiry for endings no observer consumed.

### 4. Exactly once — `resolve` → `_approve` → `_dispatch_approved`

```
ask ended
  ├─ not approved → record denied | expired | cancelled → notify
  ├─ approved by someone not addressed → denied (approver_not_addressed)
  └─ approved
       ├─ claim: UPDATE … state pending→dispatching, dispatched_execution_id = new id   (CAS)
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

A request from a public-facing surface (`public`, `paid`, channels) runs under the owner's public-channel caller prompt, re-derived at dispatch. The approved run is not gated again: the backstop finds the gate record whose `dispatched_execution_id` is the run's id. `DISPATCH_FIELDS` decides which `execute_task` fields the run re-uses (freeze), gets fresh (reset) or never replays (drop: session, batch, loop, canvas, attachments, seat-memory prompt).

### 5. Telling the requester

- Agent requester → a platform task to it, trigger `skill_gate` (Operator queue analytics bucket; autonomous), naming the request, the outcome, its originating execution and the run's execution id — never which person decided (#715). No slash in the text.
- Person requester (including a Workspace caller) → an Inbox `alert` addressed to them (`gate-note-…` id; never counted against the agent's budget), worded for a person (no `[Trinity]` marker). A person who decided their own request is not told of that decision (approved, rejected, cancelled); an approval that then could not run (`stale`, `not_run`, `unknown`) is news and is sent, once.
- The approved run's own result → back to the Workspace thread or the channel conversation it came from (`channel_completion_report._is_approved_gate_run`: an approved run was never answered inline).
- Schedules, channel users, public visitors, paid callers, connectors → the request id in the immediate answer only.

### 6. Agent deleted or renamed

`cancel_pending_for_agent` (called from the delete and rename routes) cancels each waiting record (a later approval runs nothing), ends its ask as the acting person when they may — only a person: an agent key carries its owner's email, so under one the ask stays open and inert — and notifies the requester.

## Data

`skill_gate_requests` (SQLite `skill_gate_requests_table`, Alembic `0087_skill_gate_requests`): `request_id` PK, `agent_name`, `ask_item_id`, `skills`, `request_text` (sanitised), `fingerprints`, requester (`requester_kind`, `requester_key`, `source_agent`, `requester_email`, `requester_execution_id`, `requester_mcp_key_id`), `origin_execution_id`, `triggered_by`, `dispatch`, `state` (`pending → dispatching → dispatched | stale | not_run | unknown`; `pending → denied | expired | cancelled | refused`), `state_detail`, `dispatched_execution_id` UNIQUE, `created_at`, `decided_at`, `dispatched_at`, `notified_at`. Both agent columns are in `AGENT_REFS` (CASCADE).

## Security

- Gate asks have their own budget; the agent's ask caps and rate buckets never count them (`db/operator_queue._NOT_A_GATE_ROW`).
- Gate rows name people (who asked, who decided), so they are in `operator_queue_service._ABOUT_A_PERSON_ID_PREFIXES` (#715): a machine key's queue reads never return them and `get_my_ask` answers 404 for them.
- The fingerprint script runs as root through the image's interpreter with `-I -S`, so the agent's Python startup files (`sitecustomize`, a `.pth`, `PYTHON*` env) do not run inside it; symlinks and ambiguous names are refused. This guards against accidental interference, not an adversarial executor: the agent user has passwordless sudo and could replace the interpreter itself.
- `X-Trinity-Execution-Id` is kept only when it is one of the requesting agent's own executions; the MCP server forwards the platform header, never the model-typed `execution_id`.
- Stated limits: an agent acting outside the skill with its own credential, or rewriting what the fingerprint read sees (capability confinement is the boundary for both); the dispatch-time fingerprint vs the moment the agent reads the files; a revoked agent permission between request and approval is not re-checked; plugin (`plugin:skill`) names are not matched; prose-only requests (trinity-enterprise#752).

## Testing

- `tests/unit/test_ent751_skill_invocation.py` — the matcher (accept/reject table, mutation-proven).
- `tests/unit/test_ent751_skill_gate_requests_db.py` — the record, lattice, claim, reconcile reads, registered migration, facade parity.
- `tests/unit/test_ent751_gate_budget.py` — gate asks off the agent's budget.
- `tests/unit/test_ent751_skill_gate_enforce.py` — the decision matrix.
- `tests/unit/test_ent751_skill_fingerprint_script.py` — the in-container script, executed.
- `tests/unit/test_ent751_gate_http_mapping.py` — 202 / refusal mapping and its registration.
- `tests/unit/test_ent751_gate_entries.py` — `/chat`, `/task`, the backstop, the loopback principal, the requester execution id.
- `tests/unit/test_ent751_gate_callers.py` — each producer's handling (including the Workspace turn).
- `tests/unit/test_ent329_operator_resume.py`, `tests/unit/test_ent611_ask_endings.py` — what the gate reads on an operator resume and an ending wake.
- `tests/unit/test_ent751_gate_dispatch.py` — endings, exactly-once, stale, every sweep case, notices, `may_end`, the approved dispatch (and its caller prompt), delete/rename.
- `tests/unit/test_ent751_gate_guards.py` — discovery guards and the frozen-dispatch parity.
- `src/mcp-server/src/chat-gate.test.ts` — the MCP results and the forwarded turn.
