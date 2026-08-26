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
for one product, and that is what `test_ent155_chat_cancel.py` pins.

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
| `services/chat_execution_service.py` | `terminate_execution` made principal-agnostic |
| `tests/unit/test_ent155_chat_cancel.py`, `src/frontend/tests/unit/turnCancel.spec.js` | the rules |

## Testing

- `tests/unit/test_ent155_chat_cancel.py` (14) — the optional principal and the
  `actor_kind` record; that **neither** new route re-implements cancellation (no
  status write, no CAS, no breaker call of its own); the public route's
  link+agent scoping; the Workspace route's caller scoping and its reuse of the
  stream route's own gate function; `already_terminal` / `already_finished` for all four terminal
  statuses and real termination for `running`/`queued`; a missing execution as
  404; and an agent-side failure reworded for a client without naming the agent
  host.
- `src/frontend/tests/unit/turnCancel.spec.js` (30) — every arm of the Escape
  rule including IME and `defaultPrevented`; the restore/merge including
  idempotence and whitespace-only drafts; the three outcome shapes; and, from
  source, that all three surfaces import the shared rules rather than
  re-deciding them, capture and clear the id and the text together, register and
  remove the listener, guard against a double cancel, and do **not** restore the
  words when the cancel was refused.

## Known gaps, stated

- **A cancel cannot be issued before the id exists.** All three surfaces offer
  Stop only once the dispatch has returned an execution id — roughly the first
  second of a turn. A control offered earlier would be a lie: there is no turn
  to stop yet. The Workspace's synchronous fallback path (`sendPortalChat`, used
  when the streaming dispatch route is unavailable) never learns an id and
  correctly offers nothing.
- **Partial cost still counts.** A SIGINT'd turn may report the cost it had
  already incurred; the display follows whatever the terminal envelope carries,
  which is the existing behaviour for every cancelled execution.
- **Rooms are out of scope.** `PortalRoom.vue` dispatches to N participants and
  a turn there is not one execution, so "stop this turn" has no single referent.

## Related Flows

- [session-tab.md](session-tab.md) — the `--resume` engine a Workspace turn runs on
- [workspace-absorbs-session.md](workspace-absorbs-session.md) — why Session mode is not a fourth surface
- [status-as-projection.md](status-as-projection.md) — why the CANCELLED write is a CAS

## Revision History

- 2026-08-25 (ent#155): Escape + Stop on all three conversation surfaces; two
  new credential-scoped terminate routes; `terminate_execution` made
  principal-agnostic

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
