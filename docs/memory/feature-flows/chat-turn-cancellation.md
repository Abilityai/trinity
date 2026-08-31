# Chat Turn Cancellation — Escape and Stop (ent#155)

> Stop a message that is still processing, and get the words back.

## Overview

A sent chat message used to be unstoppable. The user waited for the turn or for
its timeout, and the text they typed was gone either way — so a message sent to
the wrong agent, or with a typo noticed one second too late, cost a full turn of
spend and had to be retyped from memory.

The cancel machinery already existed end to end and was already in production
use: `POST /api/agents/{name}/executions/{id}/terminate` → the agent-server
process registry → SIGINT → a **CANCELLED** terminal (#679/#1332 — CAS-guarded,
and deliberately not FAILED, so it stays neutral for the dispatch breaker).
`TasksPanel.vue` had been calling it for a long time. What was missing was a
trigger on the surfaces where people actually talk to agents, a way to reach it
from the two credentials that are not a JWT, and a rule for the text.

Two states still had no working cancel after ent#155: an admitted row parked in
the backend's global agent-call queue (the agent has never heard of it, so the
proxy answered 404 and the only "cancel" it ever got was the watchdog's wrong
one), and a turn the agent had accepted but not yet spawned (a cancel landing
there was erased at spawn). #2433 closes both — see
[The terminate sequence](#the-terminate-sequence) and
[Agent side](#agent-side-a-cancel-that-lands-before-the-process-exists-2433).

## The three surfaces, and why the authorization differs on each

The issue's AC named four, including Session mode. ent#358 retired that surface
— `SessionPanel.vue` is deleted and `?tab=session` redirects to `/workspace` —
so its successor takes its place rather than the AC being quietly dropped.

| Surface | Credential | Route |
|---------|-----------|-------|
| Agent Detail → Chat | operator JWT | existing `POST /api/agents/{name}/executions/{id}/terminate` |
| Public link | the link token | new `POST /api/public/executions/{token}/{id}/terminate` |
| Workspace | portal session token (or a platform JWT) | new `POST /api/enterprise/client-portal/agents/{name}/executions/{id}/terminate` |

**The public link scopes per LINK, and per TRIGGER.** Its token is the
credential — the same one that was required to start the turn — and the route
sits beside `status` and `stream`, which are scoped identically. There is
deliberately no per-visitor gate: a public link has no per-visitor identity to
check against (`source_user_email` is populated only when the visitor verified
an email).

The "anyone holding the link can already read the stream, so stopping one is the
same authority" argument was the original reasoning and it does NOT carry
(review finding): reading is passive and cancelling destroys work, and the two
are not the same power just because the same credential opens both. So the route
additionally requires `triggered_by == "public"` — a link-holder can stop turns
that came from a public link, and cannot reach a scheduled run, an operator's
chat, or a Workspace turn on the same agent. Two links on one agent can still
cross-cancel; that residual is named and accepted.

**The Workspace scopes per CALLER**, behind the same three gates as its stream
route and using the *same function*, not a second copy of the predicate:

1. the agent is on the caller's roster,
2. the execution belongs to that agent,
3. **it was started by this caller** (`service.execution_belongs_to_caller`).

The third is the load-bearing one. Executions are agent-scoped, so without it
any client of a shared agent could stop another client's turn by guessing an id
— strictly worse than the read it already prevents. A miss is a uniform 404,
like the roster miss, so the route never confirms that an execution exists to
someone who may not touch it.

## `terminate_execution` became principal-agnostic

`services/chat_execution_service.terminate_execution` used `current_user` for
exactly one thing: `user_id=` on the activity row. A public-link visitor and a
Workspace client are real people with no `users` row, so `current_user` is now
optional and a NULL `user_id` is correct rather than a gap — with an
`actor_kind` (`operator` / `public_link` / `workspace_client`) on the activity
`details` so the blank is legible.

Neither new route re-implements any part of cancellation. Both delegate, so the
CANCELLED-not-FAILED choice, the CAS guard and breaker-neutrality are inherited.
A surface that wrote its own status update would be a second cancel semantics
for one product, and that is what `test_ent155_chat_cancel.py` pins. Delegation
also pays forward: the #2433 parked branch was added to `terminate_execution`
once and reached the operator, public-link and Workspace arms with no
per-surface code.

## The terminate sequence

`terminate_execution` (`services/chat_execution_service.py`) is one function
with four arms, tried in order; every surface reaches all four because every
surface delegates to it.

1. **`_cancel_queued_if_queued`** (BACKLOG-001) — the row is still `queued` in
   the overflow backlog: `db.cancel_queued_execution`, an `EXECUTION_CANCELLED`
   activity, `{"status": "cancelled_while_queued"}`. No container, no slot.
2. **`_cancel_inflight_if_parked`** (#2433) — the row is admitted (`running`,
   slot held, `claude_session_id='dispatched'`) but its dispatcher is still
   waiting on the backend's global agent-call semaphore, so the agent has never
   heard of it and the proxy would answer 404. It asks
   `agent_call_limiter.cancel_inflight(eid)` — flags this worker's in-flight
   entry and returns its phase — and, when this worker does not own it,
   `request_cross_worker_cancel(eid)`: reads the marker
   `execution:inflight:{id}`, sets `execution:cancel:{id}` (TTL = the queue-wait
   bound + 60s) and returns the marker's phase. Only a `parked` phase is
   handled here: `capacity.release_if_matches(name, task_execution_id)`, a CAS
   `update_execution_status(CANCELLED, error="Execution cancelled by user while
   queued in the backend agent-call queue")`,
   `_close_dispatch_activity_cancelled`, and an `EXECUTION_CANCELLED` activity
   carrying `status: "cancelled_while_parked"` and the caller's `actor_kind`;
   the answer is `{"status": "cancelled_while_parked", "execution_id": …}`. A
   `calling` phase (the agent has the turn) and a row that is not in flight
   anywhere fall through unchanged. `eid` is `task_execution_id or
   execution_id`.
3. **Container gate** — no container → 404, not running → 503.
4. **`_proxy_terminate_and_finalize`** — `POST agent/api/executions/{id}/terminate`;
   `terminated` → `release_if_matches`, the CANCELLED CAS, activity close,
   termination activity; `already_finished` passes through and releases
   nothing; an agent 404 is `ChatDispatchError(404)`.

**The dispatcher learns of a parked cancel at grant, never after a POST.**
`acquire_agent_call_slot` flips the entry to `calling` on the semaphore grant
and checks the in-process flag, then — only for a park that outlived the 5s
marker grace (`INFLIGHT_MARKER_GRACE_SECONDS`, the earliest a cross-worker
marker can exist) — the cross-worker cancel key. Either raises
`BackendAgentCallCancelled` before any HTTP. It subclasses
`BackendAgentCallBudgetExhausted` on purpose: `execute_task`'s existing
budget-exhausted branch handles it — and writes **CANCELLED itself**
(`_write_terminal_and_gate(status=CANCELLED)`), never FAILED, because arm 2 may
still be an await away from its own CANCELLED write and the row must read the
same whichever writer wins the CAS; the loser's lost-CAS branch closes the
activity in the standing state (a no-op when arm 2 already closed it). The
`TaskExecutionResult` reports CANCELLED. On the `/chat` arm
`_finalize_budget_exhausted` does the same (CANCELLED row, `cancelled`
activities) and raises **409**, not the retryable 503 a real exhaustion gets.
Nothing was billed: no Claude work started.

**Agent-scoped, both halves.** The operator route proves `name` through
`get_authorized_agent` and takes the execution id from the path verbatim; the
proxy-terminate path LOOKED scoped for free because the agent 404s an id it
does not own — but that 404 scopes only `execution_id`, the id the agent is
asked about, while the CANCELLED CAS and the activity close are keyed on the
caller-supplied `task_execution_id` (a query param on the operator route). So
`terminate_execution` now runs ONE agent-scope gate at its entry, for all three
arms: the row behind `task_execution_id` must belong to `name`, or the call is
refused with the proxy's own uniform 404 (a foreign id must read exactly like
an unknown one); an unreadable row fails CLOSED (503). Arm 2 additionally
scopes its own lookups — `cancel_inflight(eid, agent_name=name)` ignores an
**The remote arm's phase must post-date its own cancel write (#2435 review).**
`entry.phase` flipped to `calling` in memory only, so the marker advertised
`parked` for up to one 15s refresher tick after the POST had begun — and under
`--workers 2` roughly half of all cancels are served by the worker that does
NOT own the coroutine and therefore read it. That answer made this arm finalize
CANCELLED and release the slot while the agent ran the turn to a billed
completion whose SUCCESS then lost the CAS: the #378 symptom in a narrower
window. It is closed by ordering, not by narrowing —

    owner : W(marker=calling) -> R(cancel)     (`_publish_calling_and_check_cancel_sync`)
    remote: W(cancel)         -> R(marker)     (`_set_cancel_then_reread_phase_sync`)

so an observed `parked` gives `W_remote(cancel) < R_remote(marker) <
W_owner(marker) < R_owner(cancel)` and the grant is *guaranteed* to see the key
and refuse to POST. Both sides spend the same one round-trip they already did.
The owner gates that publish on the ENTRY's age, not this attempt's park:
`track_inflight_dispatch` wraps the whole retry loop, so a retry can grant
instantly under a marker a tick left saying `parked`. The remote's scope check
stays on its FIRST read, so no cancel key is ever written for a foreign agent.

`cancel_inflight(eid, agent_name=name)` ignores an
entry registered for another agent and `request_cross_worker_cancel(eid,
agent_name=name)` ignores a marker whose `agent` differs (or is absent) — and
keeps a row belt as a second layer, as does the BACKLOG cancel-if-queued arm
(an adjacent, pre-existing gap of the same class). The proxy arm carried no
check at all until the #2433 security verification traced the exploit: a
caller authorised on agent A could flip agent B's running row to CANCELLED
without B ever being contacted, discarding B's later SUCCESS.

## Agent side: a cancel that lands before the process exists (#2433)

The agent accepts a turn before it spawns it — `/api/task` and `/api/chat`
register the id as **pending** at handler entry (`register_pending`; the #1083
async path inside `try_spawn_async`, before the detached task exists), and the
headless run then waits its turn in the executor pool (see
[parallel-headless-execution.md](parallel-headless-execution.md)). A Stop that
landed in that window used to be erased: `register()` cleared the #679
`_terminated` marker unconditionally (the C10 clear), so a terminate between the
thread-top check and `Popen` vanished and the turn ran to a billed SUCCESS that
overwrote the watchdog's terminal.

- **`terminate()` on a pending id** sets `cancel_requested` on the entry AND
  stamps the `_terminated` marker, keeps the entry (popping it would let the
  next watchdog sweep orphan a row whose thread is about to refuse to spawn),
  and returns `{"success": True, "returncode": None, "reason":
  "cancelled_before_start"}`. The agent router maps any `success` to
  `{"status": "terminated"}`, so the backend runs its ordinary CANCELLED
  finalization; `cancelled_before_start` is a registry-level reason that never
  leaves the container.
- **`register()` consumes the flag.** On promotion it pops the pending entry;
  with `cancel_requested` set it KEEPS the marker and SIGKILLs the process
  group it was just handed (`_signal_process_tree`, outside the registry lock —
  a kill failure is logged, never raised, and the marker still relabels the
  turn). Without the flag the C10 clear runs as before, so a #678 retry that
  reuses the id cannot inherit a stale cancel. This is the authoritative
  consumer, and it is runtime-agnostic — Claude, Codex and (since #2433) Gemini
  all promote through `register()`.
- **The thread-top check is an optimisation.** `_run_headless_subprocess` asks
  `was_terminated(ctx.task_session_id)` before `Popen` and raises
  `HTTPException(409, "Execution cancelled before it started")` — 409 passes
  the inner `except HTTPException: raise` ladder untouched. The `/api/task`
  handler's existing #679 relabel (non-503/429 status + marker set) turns it
  into a `cancelled` 200; on the async path `_run_and_report` builds the
  envelope from the exception and `_cancelled_override` relabels it
  `cancelled`. Both discard the pending entry in their `finally`.

## Escape, and everything Escape must not break

Escape is heavily overloaded here — it closes modals, dismisses the composer
typeahead, leaves the voice loop. So the rule is a conjunction, and it is
deliberately conservative: a missed cancel costs one click on the Stop button,
while a wrong one destroys a turn the user is still waiting for.

`shouldCancelOnEscape` returns true only when the key is Escape, a turn is in
flight, no cancel is already running, the event is not a composed IME candidate
(Escape abandons a candidate — taking it would cancel the turn instead of the
character), no other handler has called `preventDefault`, and none of the
caller's declared overlays is open. Escape with nothing in flight is a no-op
that never touches the input.

**The overlay list is per surface, and it is declared generously.** The rule is
shared; what owns Escape is not. ChatPanel declares `[voice.isActive,
showSessionDropdown]`; the Workspace declares `[typeaheadOpen, pickerOpen,
listening]` — the composer typeahead, the agent picker (which closes on
outside-click only, so Escape is how a user dismisses it) and dictation (the mic
is disabled only while *transcribing*, so it can be live during a turn). The
Workspace list originally carried the typeahead alone, which made pressing
Escape to close the picker or stop the mic cancel the turn instead. The
asymmetry with ChatPanel was the tell. Since the module's stated bias is that a
missed cancel costs one click on Stop while a wrong one destroys work the user
is still waiting for, an overlay is listed whenever it *plausibly* owns Escape
on that surface — the conservative direction is to under-cancel. The voice loop
is deliberately absent from the Workspace list: `voiceMode` is a speak-replies
TTS toggle that does not own Escape, and the ent#440 conversation overlay is not
in `dev` — naming its ref here once threw a `ReferenceError` on every Escape
keydown and made the whole feature dead on that surface, so `turnCancel.spec.js`
pins the identifier's absence.

## The words come back without destroying a draft

The AC asks that a restore "prepends or merges sensibly". Prepend is the
sensible merge: the cancelled text is what the user is returning to edit, so it
belongs where the caret starts, and anything typed while waiting follows it.
Nothing is ever dropped, and the merge is **idempotent** — restoring a message
the composer already starts with is a no-op, so pressing Escape and then Stop
cannot stack two copies.

## Honest status

- A successful cancel renders as **cancelled**, not as an error. The surfaces
  already rendered the `cancelled` terminal from their polls; nothing there
  changed.
- A cancel that lost the race answers `already_terminal` / `already_finished` and says **nothing at
  all** — the client is racing its own poll, the reply is on screen, and there
  is no action to offer. Not a 4xx: the person did not do anything wrong.
- A **refused** terminate leaves the input untouched and says the turn is still
  running. Restoring the text there would imply a stop that did not happen, and
  the turn is still spending. In the Workspace this gets its own dismissible
  line rather than `markFailed`, because the message has not failed.

Every `status` a terminate can answer, and what each one means:

| `status` | Answered by | State it found | Row / slot | Rendered |
|----------|-------------|----------------|------------|----------|
| `cancelled_while_queued` | backend, `_cancel_queued_if_queued` | overflow-queued, never admitted | `cancel_queued_execution`; no slot held | Stopped, words restored |
| `cancelled_while_parked` (#2433) | backend, `_cancel_inflight_if_parked` | admitted, parked in the backend agent-call queue, never POSTed | CANCELLED CAS + `release_if_matches` | Stopped, words restored |
| `terminated` | agent, via `_proxy_terminate_and_finalize` | a running turn (SIGINT → SIGKILL), or — #2433 — a **pending** one: the registry's `cancelled_before_start` reason surfaces as `terminated` and the process is SIGKILLed at spawn | CANCELLED CAS + `release_if_matches` | Stopped, words restored |
| `already_finished` | agent | the process had already exited | untouched; nothing released | silent |
| `already_terminal` | the two ent#155 routes' DB pre-check | the row was already terminal | untouched | silent |
| — (`ChatDispatchError(404)`) | agent 404 | not queued, not parked, not known to the agent | untouched | "Couldn't stop the turn" |

`isNoopCancel` recognises only the two `already_*` spellings, so
`cancelled_while_parked` rendered as a stop on all three surfaces with no
frontend change.

## Where the rules live

`src/frontend/src/utils/turnCancel.js` — `shouldCancelOnEscape`, `restoreDraft`,
`cancelOutcome`, `isTerminalStatus`. Pure, and shared by all three surfaces,
because `vitest.config.js` runs `environment: 'node'` with no mount harness: a
rule decided inside an SFC is a rule no test can reach.

The Stop control itself lives in the shared `components/chat/ChatInput.vue` for
the two chat surfaces — one control, not a second hand-built copy (#2370's
lesson). It **replaces** Send rather than sitting beside it: Send is disabled for
the whole time a turn runs, so a permanent second button would be a dead control
most of the time and a competing target the rest, and the swap puts the
affordance where the user's hand is already going. The Workspace composer does
the same swap inline.

## Files

| File | Role |
|------|------|
| `utils/turnCancel.js` | every decidable rule — Escape, restore/merge, outcome wording |
| `components/chat/ChatInput.vue` | the Send↔Stop swap, shared by both chat surfaces |
| `components/ChatPanel.vue` | operator chat: id + text capture, cancel, Escape listener |
| `views/PublicChat.vue` | public link: the same, over the token route |
| `components/portal/PortalConversation.vue` | Workspace: the same, plus its own refusal line |
| `stores/clientPortal.js` | `cancelPortalTurn` |
| `routers/public.py` | the token-scoped terminate route |
| `client_portal/router.py` + `service.py` | the caller-scoped terminate route + `terminate_portal_turn` |
| `services/chat_execution_service.py` | `terminate_execution` made principal-agnostic; `_cancel_inflight_if_parked` (#2433) |
| `services/agent_call_limiter.py` | `cancel_inflight`, `request_cross_worker_cancel`; `BackendAgentCallCancelled` raised at grant (#2433) |
| `docker/base-image/agent_server/services/process_registry.py` | `terminate()` on a pending id; `register()` consumes the cancel (#2433) |
| `docker/base-image/agent_server/services/headless_executor.py` | the thread-top 409 pre-spawn check (#2433) |
| `tests/unit/test_ent155_chat_cancel.py`, `src/frontend/tests/unit/turnCancel.spec.js` | the rules |
| `tests/unit/test_2433_dispatch_wiring.py`, `test_2433_process_registry_pending.py`, `test_2433_task_handler_pending.py` | the parked and pending arms |

## Testing

- `tests/unit/test_ent155_chat_cancel.py` (14) — the optional principal and the
  `actor_kind` record; that **neither** new route re-implements cancellation (no
  status write, no CAS, no breaker call of its own); the public route's
  link+agent scoping; the Workspace route's caller scoping and its reuse of the
  stream route's own gate function; `already_terminal` / `already_finished` for all four terminal
  statuses and real termination for `running`/`queued`; a missing execution as
  404; and an agent-side failure reworded for a client without naming the agent
  host.
- `src/frontend/tests/unit/turnCancel.spec.js` (40) — every arm of the Escape
  rule including IME and `defaultPrevented`; the restore/merge including
  idempotence and whitespace-only drafts; the three outcome shapes; and, from
  source, that all three surfaces import the shared rules rather than
  re-deciding them, capture and clear the id and the text together, register and
  remove the listener, guard against a double cancel, and do **not** restore the
  words when the cancel was refused. Since the review it also pins the three
  Workspace-specific rules that source alone can answer: that both `deliver()`
  callers settle through one `settleDelivery` (so a cancelled *retry* cannot
  drift back into being marked failed), that `retry()` clears a stale cancel
  refusal like `send()` does, and that the overlay list names the picker and
  dictation alongside the typeahead.
- `tests/unit/test_2433_dispatch_wiring.py` — the parked branch of
  `terminate_execution`: a locally parked row is cancelled without any
  container lookup (`cancelled_while_parked` payload, exactly one CANCELLED
  CAS, `release_if_matches` awaited once); a park owned by the other worker
  (`request_cross_worker_cancel` → `parked`) takes the same branch; not parked
  falls through to the container gate with no status write; a `calling` phase
  goes to the agent. Plus the grant side —
  `test_cancelled_while_parked_propagates_cancelled_exception`: a cancelled
  park raises `BackendAgentCallCancelled` and never POSTs.
- `tests/unit/test_2433_process_registry_pending.py` — `terminate()` on a
  pending id returns `cancelled_before_start`, sets the marker and keeps the
  entry with `cancel_requested`; `register()` consumes it (SIGKILL of the
  handed-over group, marker kept — the race that used to end in a billed
  SUCCESS); without the flag the C10 clear still runs; a kill failure never
  raises; an unknown id is still `not_found`; `unregister` drops a pending
  entry.
- `tests/unit/test_2433_task_handler_pending.py` —
  `test_task_pre_spawn_409_is_relabelled_cancelled_and_discards`: the
  thread-top 409 with the marker set becomes a `cancelled` 200 and the pending
  entry is discarded; plus register-before / discard-after for `/api/task`,
  `/api/chat`, `try_spawn_async` and `_run_and_report`.
- `tests/unit/test_2433_headless_executor.py` —
  `test_pre_spawn_cancel_raises_409_without_spawning` /
  `test_uncancelled_run_reaches_popen`.

## Known gaps, stated

- **A cancel cannot be issued before the id exists.** All three surfaces offer
  Stop only once the dispatch has returned an execution id — roughly the first
  second of a turn. A control offered earlier would be a lie: there is no turn
  to stop yet. The Workspace's synchronous fallback path (`sendPortalChat`, used
  when the streaming dispatch route is unavailable) never learns an id and
  correctly offers nothing. Once the id exists, every state the turn can be in
  is stoppable: overflow-queued, parked in the backend call queue (#2433),
  pending on the agent (#2433), running.
- **A cross-worker park needs Redis, and a marker.** `request_cross_worker_cancel`
  answers None when there is no marker or no Redis, and the terminate then
  falls through to the proxy, where the agent answers 404 — the pre-#2433
  shape, now confined to a park owned by the *other* uvicorn worker while Redis
  is unreadable, or one younger than the 5s marker grace
  (`INFLIGHT_MARKER_GRACE_SECONDS`); the latter is about to be granted, and the
  next Stop reaches it through the agent. Same-worker parks are exact with no
  Redis at all.
- **The `calling` window before the agent registers.** Between the grant and
  the agent's `register_pending` (normally milliseconds; the connect-retry
  sleeps at worst) the proxy still answers 404.
- **Partial cost still counts** for a SIGINT'd running turn, which may report
  the cost it had already incurred; the display follows whatever the terminal
  envelope carries, which is the existing behaviour for every cancelled
  execution. A `cancelled_while_parked` or cancelled-before-start turn costs
  nothing — no process was spawned, or it was killed at spawn.
- **Rooms are out of scope.** `PortalRoom.vue` dispatches to N participants and
  a turn there is not one execution, so "stop this turn" has no single referent.

## Related Flows

- [session-tab.md](session-tab.md) — the `--resume` engine a Workspace turn runs on
- [workspace-absorbs-session.md](workspace-absorbs-session.md) — why Session mode is not a fourth surface
- [status-as-projection.md](status-as-projection.md) — why the CANCELLED write is a CAS
- [cleanup-service.md](cleanup-service.md) — the watchdog whose wrong "cancel" the parked branch replaces (#2433)
- [parallel-headless-execution.md](parallel-headless-execution.md) — the agent's pending state and executor pool
- [task-execution-service.md](task-execution-service.md) — the budget-exhausted branch that turns `BackendAgentCallCancelled` into a CANCELLED result

## Revision History

- 2026-08-25 (ent#155): Escape + Stop on all three conversation surfaces; two
  new credential-scoped terminate routes; `terminate_execution` made
  principal-agnostic
- 2026-08-28 (#2433): a row parked in the backend agent-call queue is
  cancellable (`_cancel_inflight_if_parked` → `cancelled_while_parked`, on all
  three surfaces by delegation; the grant raises `BackendAgentCallCancelled`
  before any POST); a cancel landing before the agent spawns the process is
  consumed at spawn by `register()` (SIGKILL, marker kept), with the
  thread-top 409 pre-spawn check as an optimisation

## Two things the review changed after the first pass

**A cancellation is classified as a cancellation, server-side.** `portal_chat`
answers a `cancelled` status with `409 / category="cancelled"` ahead of the
failure ladder. It used to fall through to the generic `502 / agent_error`, and
`_run` recorded that verdict DURABLY — so a client who stopped their own turn
saw "Something went wrong while the agent was working on this" again the moment
they switched threads or reloaded. The browser kept an in-memory set of
executions it had cancelled, which shielded exactly one tab until its next load.
That set survives, but only for the millisecond window the durable verdict
cannot cover: `terminate_execution` writes the CANCELLED CAS before
`cancelPortalTurn()` resolves, so a poll landing in between reads a cancelled
turn with no recorded outcome yet. In-memory is the right lifetime for that
race and the wrong one for a verdict a reload re-reads.

`retryable` is **False** for this category, keeping the surface's existing rule
that the only retryable verdicts are the ones where nothing reached the agent.
The flag is inert here anyway — the client returns early on the category and
renders neither a failure nor a Retry button.

**Cancelling releases ONE execution's slot, not the agent's.** The terminate
path called `capacity.force_release(name)`, documented in `capacity_manager` as
"Emergency: clear all running slots and the in-memory queue". That was tolerable
while the only caller was an operator on Agent Detail; this feature hands the
same path to a public-link visitor and a Workspace client, so on an agent with
`max_parallel_tasks > 1` one person stopping their own turn dropped slot
accounting for every other in-flight execution and discarded the queued
overflow. It now calls `release_if_matches(name, execution_id)` — per-execution
and TOCTOU-safe — and `already_finished` releases nothing at all, since nothing
was cancelled. `force_release` survives only on the explicit operator
force-release endpoint, which is where an emergency clear belongs.
