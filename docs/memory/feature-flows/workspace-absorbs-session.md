# Feature: the Workspace absorbs the Session surface

> **Status**: ✅ Implemented (2026-08-12)
> **Issues**: abilityai/trinity-enterprise#358 (absorb) · abilityai/trinity-enterprise#286 (streaming)
> **Requirement**: `docs/memory/requirements/core-agent.md` §5.9
> **Related**: [session-tab.md](session-tab.md) (the engine, still accurate), [architecture.md → Resumable Turns](../architecture/execution.md#resumable-turns)

## Overview

Trinity had two surfaces for one job. Agent Detail's Session mode and the
Workspace both existed to hold a continuing conversation with an agent, and
maintaining both meant every conversation feature had to be built twice or
silently exist in only one place. The Workspace becomes the one.

The interesting part is not the removal. It is that the surface being removed
was the **more capable** one, so removing it naively would have been a
downgrade nobody would notice until they needed the thing that disappeared.

## What each surface did before

| | Agent Detail Session mode | Workspace chat |
|---|---|---|
| engine | `claude --print --resume <uuid>` | stateless `execute_task` |
| continuity | real session: tool results, mid-skill state, reasoning state | last N messages replayed as a **text prompt prefix** |
| identity | platform user (`agent_sessions`) | verified client email (`enterprise_portal_sessions`) |
| streaming | **no** — synchronous POST + reattach poller (#1376/#759) | no |

Replay recovers what was *said*. It does not recover what the agent *did*: a
half-finished skill, a tool result it read three turns ago, the reasoning it
already paid for. Moving users from the first column to the second without
closing that gap would have looked fine in every demo and failed exactly when a
conversation got long enough to matter.

## The prerequisite that wasn't (and what we did with it)

The issue named ent#286 (Workspace streaming) as a hard prerequisite, on the
reasoning that "the session tab streams; workspace chat does not."

It doesn't. `SessionPanel.vue` had no `EventSource`, no `WebSocket`, no SSE;
`stores/sessions.js` sent a plain awaited POST; `routers/sessions.py` exposed no
stream endpoint. The reply arrived in one piece, with a reattach poller covering
long turns. Absorbing a non-streaming surface into a non-streaming surface is
not a regression, so ent#286 was decoupled — it is a Workspace improvement, not
a gate on this one. It then shipped in the same PR anyway, by choice rather than
by dependency; see [Streaming](#streaming-ent286-same-pr).

The **real** prerequisite was continuity parity, which is most of this change.

## Flow

### 1. Shared engine — `services/session_turn_service.py`

Everything that made a Session turn resumable moved out of the router (it was
business logic in a router, Invariant #1) into a service both surfaces call:

```
run_resumable_turn(agent_name, session_key, message, cached_uuid, triggered_by,
                   cold_message=…, on_resume_failure=…, **execute_kwargs)
  ├─ runtime gate ......... drop cached_uuid when the runtime has no --resume (Codex)
  ├─ ResumeLock ........... per-(agent, uuid); cold turns key on session_key (#779)
  ├─ execute_task ......... persist_session=True ALWAYS (a cold turn must write the
  │                          JSONL, or turn 2 has nothing to resume)
  └─ on "no conversation found" → on_resume_failure() → ONE cold retry
                              (sends `cold_message` if the caller supplied one)
```

Callers keep their own persistence. `ResumeLockBusy` subclasses
`HTTPException(429)` so the Session router's behaviour is byte-identical, while
the Workspace catches the precise type and translates it into its own
`ClientPortalError(429)`.

### 2. Workspace threads became resumable

`enterprise_portal_sessions` gained the same three columns `agent_sessions`
carries — `cached_claude_session_id`, `last_resume_at`,
`consecutive_resume_failures` — across all four tracks (`db/schema.py`,
`db/tables.py`, SQLite `portal_session_resume`, Alembic
`0037_portal_session_resume`; Invariant #3).

`client_portal/service.py::portal_chat` now reads the cached id, runs the turn
through the shared engine, and caches the id the turn actually ran under.
Existing threads start with a NULL cache: their next turn runs cold, writes a
JSONL, and is resumable from then on. No stored history changes.

### 3. History replay became cold-turn-only

This looks cosmetic and is the subtlest part of the change. `portal_chat`
composes **two** messages:

- **turn message** — omits the history block when resuming. The session already
  holds that context, so replaying it re-pays for it *and* places a summary of
  the conversation beside the conversation, inviting the model to treat the
  summary as the record.
- **`cold_message`** — always keeps the history block. It is what the engine
  sends on a cold retry, and it is the only continuity a first turn or a
  Codex agent has.

Same block, opposite correctness depending on the turn. Both directions are
pinned in tests.

### 4. The reaper had to learn about Workspace threads

`session_cleanup_service` built its keep set from `agent_sessions` alone. With
Workspace threads now resuming, that omission would delete a live Workspace
JSONL one hour after it was written (the age guard) and every thread on that
agent would go cold — no error, no log, no failed request. Just an agent that
forgot.

The keep set is now the union of both tables, and a failure reading **either**
half aborts the sweep rather than reaping against a partial set: skipping a
cycle costs disk, reaping blind costs conversations.

### 5. The surface came out

- `SessionPanel.vue` — deleted (nothing rendered it).
- The Chat tab's Session-mode toggle, `localStorage['trinity.chatMode']`, and
  the transient `routeForcedMode` — deleted with the fork they selected. The
  Chat tab is now the stateless surface plus a "Continue in Workspace" link.
- `?tab=session` — **redirects** to `/workspace?agent=<name>`, query-preserving,
  via `router.replace` so the retired URL doesn't sit in the back stack. The
  guard runs FIRST in both `onMounted` and `onActivated`: AgentDetail is
  `<KeepAlive>`-cached, and handling a landing in only one of those hooks is the
  exact #1672 bug class.
- `/workspace?agent=<name>` — new landing, resolved by the pure
  `resolveAgentLanding()` in `portalUtils.js`: most-recent thread with that
  agent, or a fresh one; `?new=1` forces fresh; an agent not on the caller's
  roster is ignored rather than surfaced as an error.

  **ent#451 — the landing is only half the answer.** Deciding *which thread to
  show* and deciding *what the first send asks for* are two questions, and an
  absent `session_id` could not distinguish "unresolved" from "deliberately
  fresh". `resolveAgentQuery` reads `route.query.new` ONCE into a local that
  feeds both `resolveAgentLanding` and `startingNewChat`, which rides to
  `PortalConversation` as `:new-chat` and becomes `new_thread` on the turn.
  Without the second half `?new=1` rendered an empty conversation and then
  resumed the old thread on the first turn — the landing honoured the deep link
  and the send did not. Every site that nulls `pendingSession` also settles the
  intent, so the two bits cannot desync.

`agent_sessions` rows, the six endpoints, and `stores/sessions.js` are all
untouched — AC #3. Only the entry point went away.

## Files

| Layer | File | Change |
|---|---|---|
| Engine | `src/backend/services/session_turn_service.py` | **new** — lock, sentinel, runtime gate, resume+fallback turn |
| Router | `src/backend/routers/sessions.py` | delegates to the engine; keeps private aliases so its tests still pin the same objects |
| Workspace | `src/backend/client_portal/service.py` | resumable turn + two-message composition |
| Workspace | `src/backend/client_portal/db.py` | resume-state accessors + `list_active_claude_session_ids` |
| Schema | `db/schema.py`, `db/tables.py`, `db/migrations.py`, `migrations/versions/0037_portal_session_resume.py` | three columns, four tracks |
| Reaper | `src/backend/services/session_cleanup_service.py` | keep-set union, fail-closed |
| UI | `src/frontend/src/views/AgentDetail.vue` | toggle + panel removed, redirect added |
| UI | `src/frontend/src/views/Portal.vue`, `components/portal/portalUtils.js` | `?agent=` landing |

## Tests

| Test | Pins |
|---|---|
| `tests/unit/test_ent358_workspace_absorbs_session.py` | resume happens; cold turns still persist; the id is cached; history replay is cold-turn-only in **both** directions; missing-JSONL falls back once with history restored; a real agent error is not retried; Codex runs stateless with replay; a busy thread answers 429; the reaper keeps Workspace JSONLs and fails closed |
| `tests/unit/test_session_*` | the extraction is behaviour-preserving for the legacy surface |
| `src/frontend/tests/unit/workspaceAgentLanding.spec.js` | the `?agent=` landing, including the roster miss and `?new=1` |
| `src/frontend/e2e/workspace-absorbs-session.spec.js` | the redirect, that it replaces history, and that no mode toggle remains |
| `tests/unit/test_2133_bounded_reply_poll.py` | the turn-bound chain (#2214 successors of the #2133 pins): derived arithmetic over the whole TIMEOUT-001 range against the real retry constant; resolver clamp + fail-open-to-default; marker TTL == 202 budget == dispatched timeout proven on one observed dispatch; reattach budget on the history response (remaining TTL / -2 / -1 / exception); the honest 504 |
| `src/frontend/tests/unit/portalSidebarIA.spec.js` | `resolveWaitBudgetMs` (positive budget wins; unusable → fallback) and that the fallback stays frozen at the pre-#2214 server bound |
| `tests/unit/test_2320_portal_failed_turn_visibility.py` | the failure taxonomy as two parametrised tables (a new unclassified branch is a missing row); outcome written BEFORE the marker clears; cleared at dispatch and on success; raw exception text never leaves the server; `AUTH`/`BILLING` no longer falls through; the substring fallback still classifies a `None` code; `last_turn_outcome` declared on `PortalHistory` (the `response_model` strip trap); the `@dataclass` enum cannot be compared with `==` |
| `src/frontend/tests/unit/portalFailedTurn.spec.js` | the first spec to exercise `PortalConversation.vue` — the retryable rule `res?.retryable ?? !res?.lost`, the execution-id match before a verdict is believed, and `markLastUserTurnFailed` marking only the unanswered tail. Extracts the shipped expressions and runs them (there is no component-mount harness in this project) |

## Streaming (ent#286, same PR)

Landed alongside, because the two rewrite the same function and doing them
separately would have meant writing the resume semantics and then immediately
rewriting where they execute.

The blocker was never SSE — the agent has streamed its log since long before
this, and `routers/public.py` already proxies it for public links. It was that
`portal_chat` only returns when the turn is over, so the client never learned an
execution id to subscribe with.

`start_portal_turn` creates the execution row first, returns
`{execution_id, session_id}` as a 202, and runs the *same* `portal_chat`
coroutine as an in-process background task. That choice is the whole design:

| If it rode #1083 fire-and-forget | In-process |
|---|---|
| resume lock becomes a lease across a callback | stays an `async with` |
| cold retry splits across the terminal | stays synchronous |
| history/title/UUID-cache writes all move | none move |
| `DISPATCH_ASYNC`-gated, Claude-only | no flag, every runtime |

`POST .../chat` stays synchronous (ent#83's documented headless surface);
streaming is the additive `POST .../chat/stream`. The frontend falls back to the
synchronous send on any streaming failure — an older backend, a buffering proxy,
a stopped agent — because streaming is an improvement to how a turn is *watched*,
never a new way for one to fail.

**That fallback is why `portal_chat`'s missing liveness gate mattered (#2196).**
`start_portal_turn` refuses a turn the agent cannot run before creating anything;
`portal_chat` had no such gate at all, and its 502 sits at the far end — *after*
`_persist_user_turn`, which ent#286 deliberately moved earlier so a refresh
mid-turn shows what was sent. Against a containerless agent the two decisions
combined to leave a durable user message with no reply in the client's thread,
plus an execution row. Since the browser reaches this path on any streaming
failure, it was not a headless-only concern. `portal_chat` now gates on the same
resolved availability, placed after the roster check (so a state-dependent
refusal cannot become an existence oracle) and before `_resolve_session_id` (so a
refused turn does not even open a thread). `start_portal_turn` passes the state
it already resolved, so a streamed turn still costs one Docker read.

**`_resolve_session_id` has three states, not two (ent#451).** An explicit
`session_id` must belong to (agent, client) — a miss is 404. With none given it
resumes the client's latest, which is right for a deep link, a refresh and a
headless caller that never held an id. `new_thread=True` is the third: open one
even though a latest exists. An explicit id WINS over the flag — a caller
sending both contradicts itself, and the id is a fact where the flag is an
intent — and the ownership check runs first either way, so the flag is never a
route past it. `ensure_thread_for_ask` deliberately does NOT pass it: ent#429's
landing rule reuses the latest thread so asks do not accumulate beside the
conversation, which matters more once several chats can exist, not less.

**Reading a stream requires all three:** the agent on the caller's roster, the
execution belonging to that agent, and the execution having been started by that
caller (`source_user_email`). The third is the one that matters — executions are
agent-scoped, so two clients of a shared agent can reach each other's ids.

## Turn bound & wait budgets (#2133 → #2214)

A Workspace turn is bounded by the agent's own `execution_timeout_seconds`
(TIMEOUT-001; default 3600, operator range 60–7200) — not, as it originally
shipped, by a flat `PORTAL_TURN_TIMEOUT_SECONDS = 300` that silently overrode
the per-agent knob on exactly the surface clients use. The engine owns the read:
`session_turn_service.resolve_turn_timeout(agent_name)`, beside
`resolve_lock_ttl`, read-side clamped to TIMEOUT-001's own range and fail-open
to the platform **default** (deliberately not the lock's fallback-to-**cap** —
an over-TTL lock is a harmless auto-expiring key, an over-long turn is billable
work).

```
resolve_turn_timeout(agent)  ..............  clamp[60, 7200], fail-open 3600
  │  resolved ONCE per turn in start_portal_turn, threaded everywhere
  ├─ dispatch ............. run_resumable_turn(timeout_seconds=t)
  ├─ attempt ceiling ...... portal_attempt_ceiling_seconds(t) = t + 10 + retry cap
  │                          (+10 = execute_task HTTP slack; retry cap =
  │                          _AUTO_RETRY_MAX_TIMEOUT_S, IMPORTED never copied —
  │                          the #678 auto-retry is a second full HTTP call)
  ├─ marker TTL / budget .. portal_max_turn_seconds(t) = 2 × ceiling + 60
  │                          (the cold retry re-runs the WHOLE turn)
  ├─ 202 ................... wait_budget_seconds = that same number
  └─ reattach .............. GET .../history returns
                             in_flight_wait_budget_seconds = the marker's
                             REMAINING Redis TTL (GET then TTL; -2 → treated as
                             nothing running, -1 → fail-open to the full
                             per-agent budget, exception → None so the client
                             falls back)
```

The derived bounds are **pure functions**; the old module constants are deleted,
not aliased, so a missed def-time consumer fails loudly at import.
`mark_turn_inflight`'s TTL default became a None-sentinel resolved at call time
for the same reason. One resolution per turn means marker, budget and dispatch
cannot disagree, and the mid-turn `PUT /timeout` race between independent reads
is gone (the resume **lock** still resolves its own TTL at acquire —
pre-existing, engine-owned).

Client side: `awaitPersistedReply` picks its ceiling via
`portalUtils.resolveWaitBudgetMs` — a positive server budget wins;
`REPLY_MAX_WAIT_MS_FALLBACK` is **frozen at the pre-#2214 server bound**
(`2×(300+10+300)+60`s), because its only remaining audience is backends that
predate the per-agent bound, whose real ceiling WAS that number. On reattach,
`loadThread` stamps `budgetReadAt` beside the history fetch — the remaining-TTL
budget is only honest from its own read time.

Decisions worth remembering: **no Workspace clamp below the agent cap** (a
clamp under 7200 re-introduces the silent override for the upper half of the
range TIMEOUT-001 sells); the accepted cost is a bigger orphaned-marker window
— hard-kill only (graceful shutdown clears the marker in `finally`), absolute
worst `portal_max_turn_seconds(7200)` = 15,080s, precedent the Session
surface's own ≤7230s sentinel; operator escape `DEL portal_inflight:{session}`.
A turn that hits the bound 504s naming the agent's limit (seconds below 120,
else rounded minutes). Long-timeout **headless** integrators should prefer the
streaming route — the synchronous `POST .../chat` holds a byte-silent response
for the whole turn, which is proxy read-timeout territory at hour scale.

## Failed turns are visible, and Retry follows the billing evidence (#2320)

A turn that fails before or at start persists **no assistant message** and its
`finally` clears the in-flight marker on every exit path. The client learns an
outcome exactly two ways — a new assistant row, or the marker still being set —
so a fast failure produced neither, and after `REPLY_IDLE_GIVE_UP_MS` the client
rendered the #2133 *"we've lost track of this turn — it may still finish"* copy
for a turn the backend had diagnosed precisely and written to
`schedule_executions.error`. Every clause of that message was false, and Retry
was suppressed on the one path where re-sending is safe.

Three parts:

**The two bits live on the exception, decided at the raise site.**
`ClientPortalError(status, detail, *, category, retryable)`. Not inferred
downstream — `_fail_unstarted_execution` is reached from the pre-start branch
**and** from the generic `except Exception`, which can fire after `execute_task`
already returned, so "was this billed" is not a property of the row being
written. `retryable` defaults **False**, so a raise site that forgets it gets the
unprivileged answer.

| Raise site | category | retryable |
|---|---|---|
| roster miss / stopped / containerless | `agent_unavailable` | ✗ — unbilled, but ent#286 settled that retrying cannot work |
| `ResumeLockBusy` | `busy` | **✓** never reached the agent |
| `CAPACITY` | `capacity` | **✓** admission refused; the queue drains |
| `AUTH` / `BILLING` | `auth` | ✗ retry re-fails |
| `TIMEOUT` | `timeout` | ✗ ran to the bound |
| generic turn failure | `agent_error` | ✗ ran |
| uncaught crash | `internal` | ✗ fixed sentence; raw text stays operator-only |

Classification now reads `TaskExecutionResult.error_code` (via
`_error_code_name`, which takes `.name` — the enum is `@dataclass`-decorated so
`AUTH == TIMEOUT` is **True**, the #1085 footgun) and keeps the old substring
tests as the `None`-code fallback, so it is additive. `AUTH`/`BILLING` had no
branch at all before and fell through to the generic 502 — that is the
subscription-limit case #2320 was reported from.

**The record rides Redis beside the marker it is the terminal half of.**
`portal_turn_outcome:{session_id}`, TTL 900s, written in `_run`'s except
branches — **before** the `finally` that clears the marker, which is the whole
ordering contract: the client's give-up timer starts when the marker vanishes,
so an outcome written after it races a 6s window. Cleared at dispatch (so turn
N+1 never inherits turn N's verdict) and on success. Redis down ⇒ no outcome ⇒
the pre-#2320 message, never worse. Surfaced as `PortalHistory.last_turn_outcome`
— **declared** on the model, because the route's `response_model` strips
undeclared keys, so a service-layer-only change is a no-op.

Deliberately **not** a message row in `enterprise_portal_messages`: `role` is
bare TEXT with no enum, but `_format_history_context` replays any non-`user` role
to the agent **as its own words**, and `_persist_user_turn`'s dedupe reads
`recent[-1].role == "user"`, so an error row would make Retry duplicate the user
message — breaking a #2120 pin that does have a test. No schema change, no
migration.

**The client believes a verdict only for the turn it is waiting on**
(`outcome.execution_id === executionId`), reports it instead of the lost-track
copy, and offers Retry iff the verdict says nothing reached the agent. The two
give-ups are now worded distinctly. `reattach()` and `loadThread()` render
failures too — that surface previously checked only for a reply and rendered
**nothing at all** on a failed or lost turn, so refreshing mid-turn showed less
than staying put. `markLastUserTurnFailed` marks only the thread's **unanswered
tail**, never "the last user row": two raise sites record a verdict without
persisting a user row of their own, and a backwards walk would pin the failure
onto an earlier, answered turn.

Residual: the synchronous `POST .../chat` path records no outcome — it raises
into a live request, where the client already gets a real HTTP error.

## Known Limitations

| Limitation | Detail |
|---|---|
| **The reply is read back, not streamed** | The agent's stream ends when its execution ends, but the reply is persisted a moment later, so the client polls history briefly (bounded) for the new assistant message. Token-by-token rendering of the reply itself is a further step. |
| **A backend restart mid-turn loses the terminal** | The background task dies with the process. No worse than the synchronous path (whose request died too), but unlike #1083 there is no callback to recover it. |
| **A pre-existing thread's next turn is cold** | Existing Workspace threads have no cached id, so the first turn after deploy replays history and starts a fresh session. Self-healing, one turn. |
| **Codex Workspace threads keep the old behaviour** | No `--resume`, so they stay on history replay. Correct, but it means continuity quality now differs by runtime within the same surface. |
| **`stores/sessions.js` keeps actions nothing calls** | Its turn/session actions map 1:1 to endpoints that are still live and still serve `agent_sessions` data (AC #3). Pruning them is cleanup for whoever retires those endpoints. |
