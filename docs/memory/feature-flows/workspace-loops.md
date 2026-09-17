# Workspace Loops — run and watch from chat (ent#458)

> A workspace user starts, watches and stops loops in the conversation they
> belong to. Folded in: ent#338, the per-run timeout that was never bounded by
> the agent's own ceiling.

## The shape, and why it needs no backend

ent#458 scopes this surface to the **platform-authenticated door** (ent#78's
auth-path invariant). A signed-in platform session in the Workspace already
carries the operator's JWT — it is the same axios default header the rest of the
app uses — and `routers/loops.py` gates on `get_current_user` /
`get_authorized_agent`. So the panel calls the **existing operator endpoints**:

```
GET  /api/agents/{name}/loops        # bare List[LoopStatusResponse] (verified)
POST /api/agents/{name}/loops        # 202 {loop_id}
POST /api/loops/{loop_id}/stop       # {status: "stopping" | "already_done"}
```

No new route, no new model, no migration. An external client holding a portal
token never mounts the panel, and could not reach these routes if it did.

## Live without a new transport

Loop events (`loop_run_completed`, `loop_completed`) are already broadcast
fleet-wide and unfiltered (#1106), and `/workspace` renders inside the same app
shell whose root connects the WebSocket. The events were already arriving — they
were simply routed to the operator store, which filters to the agent shown on
Agent Detail.

So `utils/websocket.js` now routes each event to **two** stores:

| Store | Filters to | Mounted by |
|---|---|---|
| `stores/loops.js` | the agent on Agent Detail | operator `LoopsPanel` |
| `stores/portalLoops.js` | the chat's participants | Workspace `PortalLoops` |

Two consumers of one event, each a no-op when its surface is not mounted — the
`reportsStore` + `fleetReportsStore` shape (#918). They are separate stores
because `stores/loops.js` is agent-at-a-time by construction (`setAgent(name)`
replaces the list) while a room has several participants at once; one singleton
would have each surface clearing the other's list on navigation (the
`skillsLibrary` vs `skills` split, ent#263).

A 12s backstop poll runs **only while something is active** (AC #4: "live push
degrades to poll, never a stuck running"), so an idle Workspace tab issues no
traffic. An unknown status is treated as NOT active, so a value this bundle has
never heard of cannot leave the panel claiming work is in flight forever.

## What the panel refuses to flatten

`stop_reason` carries six different situations with six different next actions.
`portalLoopUtils.loopStatusLabel` is the one place that refuses to collapse them:

| status / stop_reason | reads as |
|---|---|
| `completed`, or `stopped` + `max_runs_reached` | **Done** |
| `stopped` + `budget_exhausted` | Stopped — cost budget reached |
| `stopped` + `deadline_exceeded` | Stopped — time limit reached |
| `stopped` + `no_progress` | Stopped — it stopped making progress |
| `stopped` + `stop_signal_matched` | Stopped — it reported it was done |
| `stopped` + `user_stopped` | Stopped by you |
| `completed_with_errors` | Done, with errors |
| `failed` + `max_consecutive_failures` | Failed — too many errors in a row |

`max_runs_reached` arriving on a *stopped* row is the one that matters: calling
it "Stopped" reads as a fault when the loop simply finished.

**Headroom reports `null` for a guardrail that was never set** — not 0%, not
100%. "No budget" and "budget untouched" are different facts and a bar drawn at
either extreme asserts the wrong one. An overshoot clamps to the end of the
track, because the runtime lets the current run finish and cost can legitimately
exceed its budget (#1155).

## ent#338 — the ceiling that was not applied

`agent_ownership.execution_timeout_seconds` is the per-agent ceiling, and
nothing downstream re-applied it: `task_execution_service` reads the cap only
when the caller passed no `timeout_seconds`, so an explicit `timeout_per_run`
was handed straight to dispatch. A loop could run iterations **longer than its
owner's ceiling** — a bypass, not a display bug, multiplied by up to 100 runs.

`_reject_timeout_above_cap` refuses with 400 and a structured detail carrying
`agent_cap_seconds`, mirroring #929 for schedules. **Refuse, not clamp**: this
feature puts the guardrails on screen before Start, so a silent clamp would begin
a loop whose bounds differ from the ones the user was shown. It runs *before*
#1156's deadline comparison, so that comparison can never quote a per-run timeout
the caller may not have. It **fails open** on an unreadable cap — a resource
ceiling, not a security gate, and the prior behaviour was no check at all.

## Files

| File | Role |
|------|------|
| `routers/loops.py` | `_reject_timeout_above_cap` (ent#338) |
| `components/portal/portalLoopUtils.js` | every decidable rule — labels, tones, headroom, strip text, form pre-flight, payload |
| `components/portal/PortalLoops.vue` | the rail's Loops tab body (ent#475): the rows, the start form, Stop, the teaching empty state |
| `stores/portalLoops.js` | participant-scoped state, poll backstop, partial-failure tolerance |
| `composables/usePortalRailFeeds.js` | the ONE owner of the store's participants (ent#475) — fed off the rail's door gate |
| `utils/websocket.js` | the second route for the existing loop broadcast |
| `views/Portal.vue` | mount point: the rail's `#tab-loops` slot (ent#475; the strip above the composer is gone) |
| `tests/unit/test_ent338_loop_timeout_cap.py`, `tests/unit/portalLoops.spec.js` | the rules |

## Testing

`vitest.config.js` is `environment: 'node'` with no mount harness, so every
rule that could be wrong lives in the pure module and is tested there; the SFC
is a dispatcher over it.

- `tests/unit/test_ent338_loop_timeout_cap.py` (5) — the per-run ceiling:
  a `timeout_per_run` above the owner's `execution_timeout_seconds` is refused
  with a structured `agent_cap_seconds`, a value AT the cap is allowed, an
  absent one is untouched, an unreadable cap **fails open** (a resource ceiling
  must not become an outage), and the guard is pinned to run BEFORE the #1156
  deadline comparison — otherwise the deadline is validated against a bound the
  request is not entitled to.
- `src/frontend/tests/unit/portalLoops.spec.js` (39) — active detection;
  the terminal vocabulary, including `max_runs_reached` reading as **Done**
  rather than "Stopped" and every `stop_reason` keeping its own word; guardrail
  headroom; strip text and grouping; start-form pre-flight and the request body;
  refusal messages; which defaults mirror the server and which are the panel's
  own; the panel being the platform-authenticated door only (an external client
  never mounts it); the auth-confirm and shared-store races found in review; and
  the poll backstop that covers a dropped broadcast.

Live pass on a running instance (recorded in the PR): start from a chat, watch
runs arrive over the existing broadcast, Stop mid-run, and a `timeout_per_run`
above the agent cap refused with the bound named — with the bounds visible in
the form before Start, which is why the ceiling refuses rather than clamps.

## Re-homed into the rail (trinity-enterprise#475)

The collapsed strip above the composer is gone. `PortalLoops` is the body of
the rail's **Loops** tab (`docs/memory/feature-flows/workspace-rail.md`, slice
2); the collapsed rail carries the "1 running" signal (`loopsSignalFrom` over
the store's active loops), and a room groups by participant through the rail's
one grouping rule. Ownership of `stores/portalLoops.js` moved to the shell
(`composables/usePortalRailFeeds.js`) so the signal is live with no body
mounted — the `ownedKey` / incoming-vs-outgoing races are gone rather than
guarded, because there is one mount point.

## Known gaps, stated

- **AC #3 (history)** landed with ent#525: loop runs are one execution kind in the
  rail's Work tab (`workspace-work.md`); no parallel surface.
- **One request per participant** on refresh. The loops API is agent-scoped and
  this is a chat's participants (typically 1–3), not the roster — the ent#2198
  N+1 was per-agent across the *whole* roster on every thread refresh. A batched
  route would be a new backend surface for a fleet this small.
- The list response carries each loop's full `runs` array; prior art (#1106) has
  the same shape. Not a problem at `limit=20`, worth knowing.
