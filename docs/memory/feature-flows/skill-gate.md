# Feature: Skill Gate — approval before the executor sees the request (trinity-enterprise#751)

## Overview

A person marks a skill on an agent "requires approval by role X". When a request dispatched to that agent invokes the skill, the platform raises the approval **before** the agent sees the request. **Approve** runs it once, exactly as written; reject, expiry or cancel runs nothing. The requester is answered at once and told the outcome later.

The per-agent gate map is stored by trinity-enterprise#753. Until it lands, `skill_gate_service.list_skill_gates` returns nothing and the gate is inert.

The dispatch-time check reads what a requester typed. A request that names a skill only in prose ("please pay the invoice") reaches the executor, and the agent's own `Skill` call would load the skill. On Claude Code agents, an **in-container hook** (trinity-enterprise#752, §5 below) asks the platform before the skill loads into a run: it is refused unless that run was approved for it, or self-approved by its approver.

Requirement: `requirements/security.md` §26.13 (OPS-001-GATE).

## User Story

As the owner of a finance agent, I want "pay an invoice" to run only after the person responsible approves that exact request, whoever asked (another agent, a schedule, a channel user), so that a business action never runs on an agent's judgment alone.

## Entry Points

- **`/chat`**: `routers/chat.py::chat_with_agent` → `dispatch_admission_service.admit_chat_request` (`src/backend/services/dispatch_admission_service.py:319`). Reached from the UI chat, MCP `chat_with_agent`, and connector `run_playbook` / `ask`.
- **`/task`**: `routers/chat.py::execute_parallel_task` → `chat_execution_service.dispatch_parallel_task` (`src/backend/services/chat_execution_service.py:2125`). Covers parallel tasks, self-tasks and pull-routed tasks.
- **The Workspace (the agent page's main chat)**: `client_portal/router.py::portal_chat` and `portal_chat_stream` → `client_portal/service.py::portal_chat`. The route passes `PortalPrincipal.is_person` through as `gate_is_person`.
- **Every other producer**: the backstop at step 1b of `TaskExecutionService.execute_task` (`src/backend/services/task_execution_service.py:1887`). This covers the scheduler, loops, fan-out, channels, rooms, sessions, A2A, public links, paid calls, operator resumes, validation and voice post-processing.
- **Voice tool**: `gemini_voice._execute_tool` (`src/backend/services/gemini_voice.py:1483`) reaches the agent without `execute_task`, so it refuses a gated skill instead of raising an approval.
- **Inside the agent (trinity-enterprise#752)**: Claude Code's PreToolUse hook `docker/base-image/hooks/skill-gate.py` → `POST /api/skill-gate/check` (`src/backend/routers/skill_gate.py`) → `skill_gate_service.check_invocation`, on every `Skill` call and every `Agent`/`Task` call whose subagent preloads skills.

## Frontend Layer

### Components

- **Operations → Needs Response** (`components/operator/QueueCard.vue`) and the **Workspace Inbox** render the approval as an ordinary ask. The question names who asked and previews the request, and `QueueProposal` shows `kind: gated_skill`, the skills, the exact input, the requester and the fingerprints. A dedicated card is trinity-enterprise#755.
- **A refused decision:** a 403 `not_addressee` (the approval was addressed to someone else) is shown on that card as an `InlineError` beside the controls (`components/operator/QueueCard.vue:134`, `data-testid="queue-not-addressee-notice"`) and stays until dismissed. `/m` does the same (`views/MobileAdmin.vue:1294`).
- **The Workspace thread:** a held turn is answered with the notice as a normal assistant reply, never a Failed turn. An approved run's result arrives later in the same thread ("**Finished**" plus the result).
- **The other run senders (trinity#3274)** read the 202 through `utils/skillGate.js` (`pendingApprovalMessage`, `isGateRefusal`), show the server's own `message`, and poll nothing:
  - **Chat tab** (`ChatPanel.vue`) and **`/m` chat** (`MobileAdmin.vue`): the notice is a platform line (`chat/ChatSystemLine.vue`, the Workspace's centred muted line; `/m` reuses its `.empty-state` tone). The held message and its notice stay on screen but are flagged `held` and left out of the history the next turn sends, which would otherwise ask for the approval again on every later turn;
  - **Public link** (`PublicChat.vue`): its async turn is held in the background, so it never sees a 202. The poll now stops at a `skipped` row, and the status route returns that row's notice (`routers/public.py::_held_notice`, only for a row the link's own chat started);
  - **Tasks tab** (`TasksPanel.vue`): the local row stays, as `pending_approval` with the notice expanded; a refused request stays as `failed` with its reason. Neither creates a server row;
  - **Playbooks** Run and **Dashboard** Update: an `info` toast on the agent page (`AgentDetail.vue` passes `notify`; the toast gained a `status-info` arm, so an info toast no longer renders red). Playbooks no longer navigates to a run that does not exist;
  - a refusal everywhere reads its named message (`apiErrorMessage`), never the `detail` object.

### State Management

`stores/operatorQueue.js:68`: `notAddresseeItemId` names the card whose answer was refused. It is kept out of `error`, because every poll clears `error`.

### API Calls

- `POST /api/operator-queue/{id}/respond`: the decision, which may be refused with 403 `not_addressee`.
- Chat and task callers receive `202 {status: "pending_approval", …, outcome_delivery, message}`; the UI senders render the `message` (above).
- `GET /api/public/executions/{token}/{id}/status` returns `error` for a `skipped` row the public link started: the gate's notice.

## Backend Layer

### 1. The check — `enforce` (`src/backend/services/skill_gate_service.py:150`)

```
request → entry point
  ├─ triggered_by == "skill_gate"? ── yes → ungated (the gate's own notice)
  ├─ list_skill_gates(agent)       ── error → 503 gate_unavailable          (read_gates :281)
  ├─ find_gated_invocations(request_text, gates)      (utils/skill_invocation.py)
  │  + find_gated_invocations(context_text, gates)   scanned apart, never joined (#3274)
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
| Channels | the sender's text, after the quote line in a Telegram group (`[Replying to …: "…"]`, the line the agent reads — #3274) |
| Workspace | the quoted reply + what the client typed |
| Public links | the visitor's message |
| Rooms | the participants' new messages |
| Operator resumes | the chosen answer + the free text |
| Operator endings | the reason |
| Validation | the operator's custom prompt |
| `/task` | `message` + `user_message` + `system_prompt` (all caller-supplied; an identical part is read once) |
| Schedules | the schedule's message (its name is context, below) |
| Fan-out | each subtask's message + the batch's `system_prompt` |
| Loops | no `request_text`, so the rendered iteration is read (what an approval would run) |

**Beside the request (trinity#3274).** The executor's system prompt also carries free text the requester sets: the schedule's name, the MCP key's name and the requester's email (`platform_prompt_service`'s schedule and Triggered-by lines). `requester_context_text` passes each to `enforce` as `context_text`, as given **and** as the prompt renders it, since the renderer collapses `---` and cuts at 80 characters with `…`, which can make a name appear. It is scanned on its own and never joined to the request, which could spell a name neither part holds. When a skill was named only there, the card adds "Also sent to <agent> with this request:" and the labelled lines, counted toward the 6,000-byte bound. It is never stored as the request or replayed. A parity test classifies every `ExecutionContext` string field as scanned or platform-controlled. The schedule-name part is defence in depth today: the scheduler does not send `schedule_name` to `/api/internal/execute-task` (never wired since #171, trinity#3341), so a scheduled run carries no schedule context and the name never reaches the executor. The key name and the email are live.

**The matcher** (`utils/skill_invocation.py`): a `/` preceded by anything but a letter, a dot or another slash starts an invocation, so `-/x`, `_/x_` and `1/x` match while `docs/x`, `https://host/x` and `//x` stay paths. A path whose last segment follows a digit (`api/v1/x`) now matches too. A trailing run of `.`, `_` or `-` ends a name; one that continues into a letter or digit makes it another skill. Unicode's Default_Ignorable code points are matched both removed (a run right before a slash reads as a space) and replaced by a space, and the two results are unioned. So stripping one can never join a letter to the slash, and none hides an invocation inside a name or at its end.

On `/task`, `message` is caller-supplied and may carry history, so an invocation quoted there is held: the gate cannot tell a quote from a request. Where the platform composes the history itself (the Workspace, channels, rooms, public links, a Telegram group's history block), only the new words are read. Sender labels and a group title are not read either. A held request replayed in a later cold turn's history therefore reaches the agent as text; scanning that history would re-hold every later turn of the thread. On Claude Code, if the agent acts on it, the in-container hook (§5) refuses the skill. On Codex and Gemini it is a stated limit.

**Who asked.**
- `/chat` and `/task` build the requester from the authenticated principal (`requester_from_principal`, `:570`).
- The backstop derives it from the call (`requester_for_dispatch`, `:596`), filling in whatever the call left off from the pre-created row (`_with_row_attribution`, `task_execution_service.py:1395`). The scheduler's `/api/internal/execute-task` sends neither the schedule id nor the person who pressed **Run now**, so the row is where they come from. As a result, each schedule is its own requester, named on the card as one bounded line, and **Run now** is asked by the person who pressed it.
- A Workspace turn is a person.

**Self-approval needs a proven person**, from one of two places:
- `is_person_principal` at the `/chat` and `/task` seams: a signed-in session or a user-scoped key, never an agent key, a system key, a connector, a portal delegate or the event loopback;
- the Workspace caller its route verified: `PortalPrincipal.is_person` → `portal_chat(gate_is_person=…)` → `execute_task(gate_requester=…)`.

The backstop never self-approves otherwise. Voice relays a model's paraphrase and stays unproven. Nothing is dispatched on a read that failed.

**A self-approval is recorded where it happens** (trinity-enterprise#752). `record_self_approval` writes a `self_approved` row in `skill_gate_requests`, keyed `gate-self-<sha(agent, run)>`, whose `dispatched_execution_id` is the execution the agent receives, and then #751's audit row. It is the only caller of `audit_self_approved`. The three seams:
- `/task`: after `create_task_execution_and_activities`, on the row it made;
- `/chat`: in `prepare_chat_execution`, on `task_execution_id` — the id `build_chat_payload` sends. The admission only decides; its decision rides `ChatAdmission.gate`, because the admission's own id is the capacity slot's, which no agent sees;
- the backstop: on the row `execute_task` created or was given.

A failed write is logged and the run proceeds; the hook then refuses the skill inside it. A self-approved `/chat` turn also runs in its own session (§5).

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

- **What the approved run sends (trinity#3274).** `/task` and fan-out scan the caller's message and system prompt together, but they freeze the two apart (`frozen_replay` → `replay_message` / `replay_system_prompt` in the record's `dispatch`; fan-out passes it as `execute_task(gate_replay=…)`). The approved run sends the message as the message and the system prompt as the system prompt, each sanitised like the card. A record without them replays `request_text`, as every other producer does.
- A request from a public-facing surface (`public`, `paid`, channels) runs under the owner's public-channel caller prompt, re-derived at dispatch.
- The approved run is not gated again: the backstop finds the gate record whose `dispatched_execution_id` is the run's id.
- `DISPATCH_FIELDS` decides, for each `execute_task` field, whether the approved run re-uses it (freeze), gets it fresh (reset), or never replays it (drop). Dropped fields: session, batch, loop, canvas, attachments, and the seat-memory prompt.

### 4. Deleting or renaming the agent

`cancel_pending_for_agent` (`:1150`) is called from the delete and rename routes:
- it cancels each waiting record, so a later approval runs nothing;
- it ends the record's ask as the acting person, but only when that person may end it, and only a person: an agent key carries its owner's email, so under one the ask stays open and inert;
- it notifies the requester.

### 5. The in-container hook (trinity-enterprise#752, Claude Code only)

```
claude --print (2.1.281)  PreToolUse Skill | Agent | Task
  └─ /usr/bin/env -i /usr/local/bin/python3 -I -S /opt/trinity/hooks/skill-gate.py      (exec form, no shell)
       └─ _skill_gate.main — clears its env; decides in a daemon thread under a 10 s deadline
            ├─ Skill → tool_input.skill (trim, strip one /)       ─┐ names: invoked, its last `:` segment, and every
            ├─ Agent/Task → the subagent definition's `skills:`  ─┘ name each matching skill dir answers to
            │     (no subagent_type = general-purpose; CLAUDE_CONFIG_DIR from the claude env adds its dirs)
            │     no definition (a built-in) / nothing preloaded → exit 0, no call
            │     a name or front matter it cannot read, that may be the one → resolved: false
            ├─ run  = TRINITY_EXECUTION_ID from /proc/<parent>/environ   (the claude process's launch env)
            ├─ key, URL = /proc/1/environ   (the container's; URL defaults to http://backend:8000)
            └─ POST /api/skill-gate/check  {via, invoked, names, resolved, subagent, execution_id, marker}
                  one retry on a refused / dropped connection; no proxy, no redirect
                  200 allowed:true → exit 0 · 200 allowed:false → its message on stderr, exit 2
                  anything else → /opt/trinity/skill-gates-active present ? exit 2 "could not be checked" : exit 0
```

- **The check** (`routers/skill_gate.py` → `skill_gate_service.check_invocation`): `get_self_agent` takes the agent from the key (`trinity-system` for the system key) and refuses every other principal with the uniform 403 `agent_identity_required`, then the access 404. `read_gates` comes first: an unreadable map — or a store that returns `None` — is 503 `gate_unavailable`, the only non-200. With no gates the answer is "allowed". An unresolved call on a gated agent is refused ("could not tell"). Otherwise the gate keys are matched, casefolded, against the names. Each hit must be cleared: the gate record whose `dispatched_execution_id` is the run (`dispatching`, `dispatched` or `self_approved`, on this agent), with the run's row this agent's and live (`running`, `queued`, `pending_retry`). Both reads always run, so an unknown, another agent's and a finished run answer identically. A refusal is returned, never raised, and audited `skill_gate_refused` (at most once per run and skill, and 20 per agent, per 10 minutes — the run id is the caller's), with the reason logged: `no_run`, `run_not_live`, `not_cleared` or `could_not_tell`.
- **The copy** (`hook_refusal_text`) hands the request back (D1). It names the skill and the form to send ("a message that includes /pay-invoice and what they want done"), and no person and no role. A session with no run (web terminal, SSH) gets the "this session is not a Trinity run" variant (D2), and a preload names the subagent.
- **The marker** (`sync_gate_marker`): a root `docker exec` with a constant argv (`marker_command`). On an agent with gates it writes the file through a temp file at 0444; on one without, it removes it; an unreadable map or a failed or timed-out exec changes nothing. `spawn_gate_marker_sync` runs it fire-and-forget at the tail of `start_agent_internal` and of `recreate_container_with_updated_config` (a recreate drops the writable layer), for gated agents only. It also runs from the check when the hook's reported `marker` disagrees with the map (`rate_limiter`, once per agent per 5 minutes). Ordering contract for gate writers (ent#753): sync before an agent's first gate, remove after its last; syncs of one agent are serialised in a worker.
- **A self-approved `/chat` turn runs in its own session.** `prepare_chat_execution` sets `ChatExecutionContext.isolated_session`, `run_chat_turn` passes it through, and `build_chat_payload` puts `isolated_session: true` on the agent's `/api/chat` payload. The agent server (`execute_claude_code`) then runs the turn with no `--resume`, keeps no session id, leaves the shared model and session counters alone, and does no cold retry. The next ordinary turn resumes the shared session, without the skill. Workspace threads and the Chat tab keep their continuity.
- **Registration**: `/etc/claude-code/managed-settings.d/50-skill-gate.json` (root:root 0444 in a 0755 dir), separate from `managed-settings.json` so a rejected entry voids only itself. It pins `LD_PRELOAD` / `LD_LIBRARY_PATH` / `LD_AUDIT` empty. The build smoke is `RUN … skill-gate.py --self-test`. Agent `/health` reports `skill_gate_hook`.

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

`state` moves `pending → dispatching → dispatched | stale | not_run | unknown`, or `pending → denied | expired | cancelled | refused`. A `self_approved` row (trinity-enterprise#752, no migration — a new value in the same column) is inserted in that state by `record_self_approved_run` and never moves; it is never pending work, so no cap, sweep or ask reads it. Both agent columns are in `AGENT_REFS` (CASCADE).

## Side Effects

- **The requester's notice** (`_notify`; text from `outcome_text`). Who is told follows one rule, `outcome_delivery` (`agent_task` / `inbox` / `none`), which the pending answer also carries (#3233):
  - **Agent requester:** a platform task, trigger `skill_gate` (analytics bucket "Operator queue"), naming the request, the outcome, the originating execution and the run's execution id. It never says which person decided (#715), and it contains no slash.
  - **Person requester, including a Workspace caller:** an Inbox `alert` addressed to them, with a `gate-note-…` id that never counts against the agent's ask budget. It is worded for a person, without the `[Trinity]` marker. A person who decided their own request is not told of that decision (approved, rejected, cancelled). An approval that then could not run (`stale`, `not_run`, `unknown`) is news, and is sent once.
  - **The decider is never named by email** (trinity#3274, `_decider_label`). A request a platform session or key made (`source_user_id` frozen — only the `/chat` and `/task` seams and the platform's own producers set it) reads the decider's display name (`users.name`, one line, without `@`). Everyone else reads "the agent's approver": the Workspace, the inline connector, operator resumes, a decider with no usable name, and a failed lookup. Most email-login accounts have no name, so the label is the common case.
  - **Schedules, channel users, public visitors, paid callers, connectors, system keys:** the request id in the immediate answer, whose message now says so: "Nothing will be sent back when it is decided: if it is approved, it runs as a new execution on <agent>; if not, nothing runs."
- **The approved run's result** goes back to the Workspace thread or the channel conversation it came from (`channel_completion_report._is_approved_gate_run`, `src/backend/services/channel_completion_report.py:683`), because an approved run was never answered inline. Only a `dispatching` or `dispatched` record counts: a `self_approved` one names a run that already answered inline.
- **Audit rows:** `skill_gate_outcome` once per decided record (ids only); `skill_gate_self_approved` once per execution the gate let through on the approver's own request; `skill_gate_refused` when the in-container hook is refused (at most once per run and skill, and 20 per agent, per 10 minutes).
- **The marker** in a gated agent's container (`/opt/trinity/skill-gates-active`), written and removed by root `docker exec`.
- **A held run's row** is closed `skipped` with the notice itself as its error, which names the request id and is what a public-link visitor reads (#3274). The scheduler does not retry it, and a loop stops with `approval_required`.

## Error Handling

All responses go through the app handler `error_handlers.skill_gate_error` (`src/backend/error_handlers.py:79`) and carry `X-Trinity-Error-Code`.

| Case | Status / code | What the caller sees |
|---|---|---|
| Approval raised | 202 `approval_pending` | MCP (every tier, the inline connector included since #3274): `pending_approval` ("do not retry"; "the outcome will be sent to you" only when `outcome_delivery` is `agent_task` or `inbox`, otherwise where to look: `list_recent_executions`); fan-out: subtask `pending_approval` + `request_id`; A2A: `input-required` keyed on the request id; paid: 202, not settled; Workspace: the notice as the reply; the UI senders: the notice, no poll |
| …on a paid door | `GATE_HELD` / `GATE_REFUSED` | Both paid doors run `services/paid_turn_service.run_paid_turn`, which returns these outcomes. Nothing ran and nothing is charged; the claim is released with `fail()` (a completed-unsettled claim would make a retry try to settle). The x402 door answers 202 or the refusal; the paid A2A door renders the same `input-required` / `rejected` task, with x402 "verified, nothing charged" metadata (`services/a2a_payment_gate.py::_OUTCOME_RENDER`). |
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

**The in-container hook** (no HTTP caller sees these; the model reads them as a tool result):

| Case | Hook exit | What the model reads |
|---|---|---|
| Not gated, or the run is cleared | 0 | nothing — the skill loads |
| Gated, the run not approved for it | 2 | the hand-back ("… request it through Trinity with a message that includes /pay-invoice …") |
| Gated, no platform run | 2 | "… this session is not a Trinity run, so it cannot carry an approval …" |
| Gated, the call could not be resolved | 2 | "… Trinity could not tell which skill this call loads …" |
| No verdict (unreachable, 404 from an older backend, 5xx, 401/403/422, malformed, deadline, no key) and the marker present | 2 | "Trinity could not be reached to check whether this skill needs approval …" |
| No verdict, no marker (an agent with no gates) | 0 | nothing — the skill loads |
| The hook module cannot import or crashes | 0 / 2 | the bootstrap's marker fallback; never exit 1, which would let the tool run |

## Security Considerations

- **Budget:** gate asks have their own budget. The agent's ask caps and rate buckets never count them (`db/operator_queue._NOT_A_GATE_ROW`).
- **#715:** gate rows name people (who asked, who decided), so they are in `operator_queue_service._ABOUT_A_PERSON_ID_PREFIXES` (`src/backend/services/operator_queue_service.py:302`). A machine key's queue reads never return them (`routers/operator_queue.py:109`), and `get_my_ask` answers 404 for them (`:510`).
- **Fingerprint read:** the script runs as root through the image's interpreter with `-I -S`, so the agent's Python startup files (`sitecustomize`, a `.pth`, `PYTHON*` env) do not run inside it, and symlinks and ambiguous names are refused. This guards against accidental interference, not an adversarial executor: the agent user has passwordless sudo and could replace the interpreter itself.
- **Execution id:** `X-Trinity-Execution-Id` is kept only when it is one of the requesting agent's own executions. The MCP server forwards the platform header, never the model-typed `execution_id`.
- **Schedule names:** a name is free text an agent can set, so it is collapsed to one line of at most 80 characters before it appears on the approver's card. It is scanned whole, and as the prompt renders it (#3274).
- **Stated limits:**
  - an agent acting outside the skill with its own credential, or rewriting what the fingerprint read sees (capability confinement is the boundary for both);
  - the gap between the dispatch-time fingerprint and the moment the agent reads the files;
  - a revoked agent permission between request and approval is not re-checked;
  - plugin (`plugin:skill`) names are not matched;
  - a session minted by a delegate key counts as a person (registered as debt);
  - history is not re-read (#3274, decided): not the Workspace's or a channel thread's, not a room's or a public link's, and not a Telegram group's earlier messages, since one past mention would hold every later turn. Neither are the sender labels or a group title. On Claude Code agents the in-container hook refuses a gated skill that history talks the agent into. Agents on Codex or Gemini have no hook, so on those runtimes nothing stops it;
  - a schedule-name approval runs the schedule's message without the name, which is not replayed.
- **Stated limits of the in-container hook (trinity-enterprise#752):**
  - Claude Code only: Codex and Gemini have no hook (D4). On Codex, which has no `--resume`, a held request sits in replayed Workspace history on every later turn;
  - not a boundary against an adversarial executor: passwordless `sudo` (the registration, the hook and the marker are root-owned, which stops accidents only); reading a SKILL.md and acting on it; calling the skill's own tools, such as an MCP tool, directly; a nested `claude` started from Bash, whose environment — the execution id — is the agent's to set, and in which a prompt that starts with `/<skill>` expands with no tool call at all. The boundary is credential confinement (R23, trinity-enterprise#558);
  - a leading `/skill` in a dispatched message expands without a tool call, so the hook never sees it. That includes an approved or self-approved run's own request, and it is the dispatch-time check's lane. What a clearance lets through is a `Skill` call inside the cleared run: a `/skill` mid-sentence, or the model choosing the skill;
  - plugin and nested names follow #751 (the hook over-matches the last segment);
  - voice-tool calls carry no platform execution and are always refused on a gated agent;
  - it needs a base-image rebuild and an agent recreate, and an older backend answers 404, so nothing is refused;
  - server-managed claude.ai settings outrank the files;
  - a self-approved turn in a Workspace thread or the Chat tab keeps the loaded skill in that conversation;
  - inert until trinity-enterprise#753 supplies the gate map.
- Security audit: `docs/security-reports/cso-diff-2026-10-02-ent751-gated-skills.md`.

## Testing

### Prerequisites

Until trinity-enterprise#753 ships, a gate exists only in a local build where `list_skill_gates` returns a map. Roles other than `primary` resolve only through an assignments provider that implements `people_for`.

### Test Steps (localhost, two accounts)

1. **The approver asks in the Workspace.** Send `/pay-invoice INV-1` → it runs directly, and one `skill_gate_self_approved` audit row is written.
2. **A schedule's Run now asks.** → the run is `skipped` with the notice as its error; a card in Needs Response names the person who pressed Run now and shows the full request.
3. **Approve it.** → a new run executes the request, and the requester gets no Inbox notice because they decided it themselves.
4. **Reject one.** → nothing runs, and there is no notice.
5. **An agent asks** (`chat_with_agent`). → the agent reports pending and doesn't retry. On approve, the agent gets one `skill_gate` task stating the outcome with no email.
6. **A non-approver asks in the Workspace.** →
   - the notice comes back as a normal reply;
   - an admin's Approve is refused on the card (`not_addressee`);
   - when the approver approves, the result lands back in the asker's thread, and the asker's Inbox notice says "approved by the agent's approver" (never an email; #3274).
8. **The UI senders (#3274)**, as a person who is NOT the approver: the Chat tab, the Tasks tab, a playbook's Run, Dashboard → Update Dashboard, `/m` chat, and a public link. Each shows the server's message (a platform line, a kept task row or an info toast) and no "No execution_id" error, failed row or timeout. A second chat message after a held one does not raise a second approval.
7. **The in-container hook** (#752). It needs a base image built from the branch and a recreated agent; the agent's `/health` shows `skill_gate_hook: ok`. Save the running image under a second tag first, and restore by re-tagging it: rebuilding from a local `dev` that is older than the running image downgrades the agents. Claude Code expands a message that *starts* with `/pay-invoice` without any `Skill` call, so the hook is never asked about it; the dispatch-time check above decides those. The hook's allow path is a `Skill` call inside a cleared run, so the approved and self-approved steps put the slash mid-sentence:
   - **prose:** another agent sends "Please pay invoice INV-7 for 100 EUR." with no slash → the model's `Skill` call is refused with the hand-back, and a `skill_gate_refused` audit row is written (`not_cleared`);
   - **approved:** an agent asks "Please run /pay-invoice for INV-8 100 EUR." → card → Approve → inside the approved run the hook logs `skill_gate_allow`, and the skill runs;
   - **self-approved:** the approver sends "Please run /pay-invoice for INV-9 100 EUR." in the Chat tab → a `self_approved` record for that run, and the hook logs `skill_gate_allow`;
   - **isolated `/chat`:** on classic `/chat` (the mobile admin page or the API; the desktop UI no longer sends it), send "Remember the word PELICAN", then `/pay-invoice INV-3 100 EUR`, then ask what came right before → the answer has the word and not the invoice;
   - **preload:** `Task` with a subagent whose definition has `skills: [pay-invoice]` → refused (`via=subagent_preload`);
   - **no run:** in the agent's terminal, "pay invoice INV-6" with no slash → refused, "not a Trinity run" (audit reason `no_run`). A typed `/pay-invoice` there expands and runs, which is a stated limit;
   - **no verdict:** use a local build whose check route answers 503 → on the gated agent a skill is refused ("Trinity could not be reached to check…"); on an agent with no gates it runs. Stopping the backend does not test this, because the chat itself goes through the backend;
   - `/logs/guardrails.jsonl` has a `skill_gate_allow` / `skill_gate_deny` / `skill_gate_unknown` line per call, and never the key.

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
- a forged schedule name;
- a schedule name, a key name or an email that invokes a gated skill, raw and as rendered (#3274);
- `-/x`, `_/x_`, `1/x`, and every Default_Ignorable character before, after or inside the name (#3274).

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
| `src/mcp-server/src/chat-gate.test.ts` | the MCP results and the forwarded turn; the delivery-honest suffix and the inline connector tier (#3274) |
| `tests/unit/test_3274_gate_context.py` | the context scan at the backstop, `/task` and `/chat`; raw vs rendered; scanned apart; the `ExecutionContext` parity |
| `tests/unit/test_3274_gate_replay.py` | `/task` and fan-out replay the message and the system prompt apart; legacy records |
| `tests/unit/test_3274_gate_notice.py` | `outcome_delivery` on the 202 and its retry, `_notify` parity per kind, the decider label, the held row's text |
| `tests/unit/test_3274_gate_wiring.py` | the loopback flag, the observer registration and the sweep call, each through the code that runs it |
| `tests/unit/test_3274_channel_quote.py`, `test_3274_public_skipped_status.py` | the Telegram quote; the public status route for a held turn |
| `src/frontend/tests/unit/skillGateSenders.spec.js`, `mobileAdminSkillGate.spec.js`, `skillGate.spec.js`, `agentDetailGateNotice.spec.js` | the six UI senders, mounted; the reader util; the agent page's toast wiring (source pin) |
| `tests/unit/test_ent752_skill_gate_hook.py` | the hook executed in its own process (`-I -S`, import-level seam): the verdict table, every no-verdict path × marker, the deadline, one retry, identity from `/proc` not its env, crash injection through the bootstrap, FIFO traps, the log |
| `tests/unit/test_ent752_name_resolution.py` | generous name resolution ⊇ #751's `_FINGERPRINT_SCRIPT`; the `skills:` forms; subagent lookup |
| `tests/unit/test_ent752_check_endpoint.py` | the route over the real DB: who may ask, every verdict, uniform answers, 503 / 422, the throttled audit, the marker heal, and the hook's own request replayed against it |
| `tests/unit/test_ent752_self_approval_clearance.py` | the record at `/task`, `/chat` and the backstop on the id the agent receives; the record's lattice; the completion-report filter; the AST guard (`audit_self_approved` only in `record_self_approval`) |
| `tests/unit/test_ent752_marker_sync.py` | the marker's argv executed; failure handling; the spawn; both lifecycle tails driven |
| `tests/unit/test_ent752_isolated_chat_session.py` | the isolated `/chat` turn through the router, the payload, the agent server and the real `execute_claude_code` (#2958 fake CLI) |
| `tests/unit/test_ent752_managed_registration.py`, `test_ent752_cli_pin.py` | the drop-in's exact shape, the matcher through a port of the CLI's own branch, the image wiring, the CLI pin |
| `tests/unit/test_ent752_health_skill_gate.py` | `/health` `skill_gate_hook` |

### Status

✅ Implemented and eyeballed on localhost (2026-10-03). It is inert until the gate map lands. The #3208 validation gaps were closed by trinity#3274 before the map (plus #3233). The in-container hook (trinity-enterprise#752) is implemented and unit-tested, with every call site mutation-checked. It was eyeballed inside real agent containers on localhost (2026-10-05), and every case in step 7 passed.

## Related Flows

- **Upstream:** the gate map (trinity-enterprise#753); the agent Skills tab "Requires approval" row (trinity-enterprise#754).
- **Downstream / shared:**
  - the ask sink and its endings (`operating-room.md`, trinity-enterprise#611);
  - the approval card (trinity-enterprise#755);
  - the completion report back to a channel or thread (ent#457);
  - the connector playbooks (`mcp-connector.md`);
  - the guardrail hooks this hook sits beside (GUARD-002, `requirements/security.md` §28.2) and the chat's own session (#2958).
- **Part of this flow:** the in-container hook (trinity-enterprise#752, §5).
