# Feature: Workspace roster load — briefing hydration off the critical path

> **Status**: ✅ Implemented (2026-09-03)
> **Issue**: abilityai/trinity#2163
> **Requirement**: `docs/memory/requirements/core-agent.md` §5.16
> **Related**: [workspace-sidebar-ia.md](workspace-sidebar-ia.md) · [workspace-agent-page.md](workspace-agent-page.md) · [workspace-composer-typeahead.md](workspace-composer-typeahead.md)

## Overview

The Workspace's first paint was bounded by the **slowest agent in the fleet**.

`get_roster` built each card, then fanned `_agent_briefing` across all of them
and `await asyncio.gather(...)` — which waits for ALL. Each briefing is up to
two agent HTTP calls under an httpx `timeout=5.0`. So one wedged or slow-booting
agent made `GET /my-agents` take five seconds *for every user, on every sign-in*,
however small or large the fleet. Measured: 65–274 ms healthy, ≥5 s with one
unresponsive agent among twelve.

The enrichment being "best-effort and parallel" bounded the **blast radius** (a
failing agent left `(None, [])` on its own card) but never the **latency**.

Two changes, and the second is a belt for what remains:

1. **Defer.** The roster awaits no agent HTTP at all. Every card ships
   `briefing_state: "pending"`, and a new viewer-scoped read hydrates the
   briefings off the critical path.
2. **Bound.** Every briefing that still runs — the hydration batch and the agent
   page's single one — runs under one wall clock, and a trip is reported rather
   than silently indistinguishable from an agent that has no hints.

## Flow

```
Portal.vue::bootstrap()
  └─ store.fetchRoster() ──► GET /my-agents ──► service.get_roster
                                                 ├─ _roster_rows ............ SQL union (shared ∪ owned)
                                                 ├─ _availability_map ....... ONE Docker list (#2196)
                                                 └─ cards[briefing_state="pending"]   ← NO agent HTTP
       │
       ├─ (store, roster-success branch, NOT awaited)
       │    briefingHydrationPlan(agents).batch ──► store.hydrateBriefings()
       │        └─ GET /briefings ─────────────► service.get_briefings(requested=None)
       │                                           ├─ roster names (None ⇒ all)
       │                                           ├─ NO Docker read (availability="unknown")
       │                                           └─ Semaphore(16) ⊃ _bounded_briefing
       │                                                └─ wait_for(_agent_briefing[gather ×2 GET], 3.0s)
       │
       └─ Portal.vue watch(activeAgent.name) ──► store.ensureBriefing(name)
                                                  └─ GET /briefings?agents=<active>   (never coalesced)

  agent page: GET /agents/{name}/page ──► get_agent_card ──► _bounded_briefing (one, bounded)

Zones:  <main> ScanlineReveal(stageZone ← viewState)
          ⊃ PortalConversation ScanlineReveal(!historyLoaded)
              ⊃ PortalBriefing ScanlineReveal(briefingZone)
```

## Design decisions

### One route with an optional filter, not two routes and not per-agent

`GET /api/enterprise/client-portal/briefings[?agents=a,b]`.

A per-agent `GET /agents/{name}/briefing` would have been the smaller route and
the wrong one: the agent picker renders every agent's `description`, so
completing the picker would cost N calls per sign-in — exactly the N+1 shape
#2198 removed. An unfiltered batch alone would have moved the floor rather than
removing it: the active agent's hints would wait for the batch's slowest member
(≤ the bound) on every new-chat screen. The filter makes the active agent arrive
at its own speed while the background batch fills the picker and the composer's
`/` typeahead.

### Scope is the roster, and the roster's own strings are what we iterate

`selected = roster_names if requested is None else [n for n in roster_names if n in set(requested)]`.

The caller's names are only ever tested for **set membership**; the string that
reaches `agent-{name}:8000` is always a DB row value, so a crafted name cannot
steer the HTTP target. An unknown or off-roster name is dropped silently rather
than answered — no existence oracle, and the caller already knows its own roster
(Invariant #8). `include_owned = principal.is_platform`, threaded exactly as
`/sessions` does (ent#357/#2198: what a caller may DO equals what they may SEE).

The `requested is None` arm is explicit and tested: a literal `n in None` would
be a 500 on every background hydration.

### No Docker read on the hydration path

`_agent_briefing` **attempts** `unknown` by design — it reaches the agent by DNS
over the agent network, so a backend Docker-socket fault says nothing about
whether the agent answers HTTP. A stopped or absent container therefore fails
its connect immediately under the bound and lands as `unavailable`: the same
verdict a skip would produce, one fleet-wide Docker call cheaper, moments after
the roster made one. `get_agent_card` keeps its single tri-state read, because
the page renders the availability chip.

### The bound: two numbers, both constants

| Constant | Value | What it bounds |
|---|---|---|
| `_BRIEFING_HTTP_TIMEOUT_SECONDS` | 2.0 | httpx, **per phase** (connect/read/write/pool) |
| `_BRIEFING_BUDGET_SECONDS` | 3.0 | wall clock for one agent's whole briefing |
| `_BRIEFING_CONCURRENCY` | 16 | in-flight briefings per REQUEST |

The literal `5.0` this replaces was never a ceiling: `_agent_briefing` makes two
sequential GETs, each of which may spend a full timeout in each phase, so the
number every caller reasoned about was a floor (the `a2a_client` tarpit shape —
a per-read timeout resets forever). `_bounded_briefing` adds the wall clock and
never raises: a trip, a raise, or an agent that was never attempted all yield
`(AgentBriefing(), False)`.

Why 2.0/3.0 and not the issue's "say 1.5 s": with the briefing off the critical
path the bound no longer protects the roster, and `GET /api/skills` on the agent
is a **synchronous directory scan on the agent-server's own event loop**, so a
healthy agent mid-turn can legitimately exceed a second. A tighter value only
trips on working agents.

Constants, not settings and not env vars — an engineering bound no operator
tunes at runtime (`SAMPLE_INTERVAL_SECONDS` precedent, #1644), and an env read
that no compose file forwards is inert while reading as configurable (#1039).

`_agent_briefing`'s two GETs now run under `asyncio.gather`: sequential, a
wall-clock cancel landing in the second call threw away the description the
first had already returned, and the healthy latency was double what it needed to
be.

### The semaphore is per request, and acquired OUTSIDE the wall clock

Per request because an `asyncio.Semaphore` binds to the first loop that creates
a waiter on it — a module-level one raises `RuntimeError: … bound to a different
event loop` the second time anything calls this under `asyncio.run`. Outside the
wall clock because an agent queued behind the permits would otherwise burn its
whole budget waiting for a slot: on a 100-agent roster, rounds 2+ would all come
back `unavailable` while every agent was healthy.

### `briefing_state` is a server-owned tri-state

`pending | ready | unavailable`, default `"ready"`.

All three values are the server's. Letting the server say `"ready"` for a bound
trip would make a wedged agent byte-identical to one that genuinely has nothing
to offer — the "looks complete" class `playbooks_total` already guards one tier
over — and would force every headless ent#83 client to reinvent the third value
out of empty fields. `"ready"` means **reached a verdict inside its budget**, not
"returned data": an agent exposing nothing legitimately briefs empty.

The default is the non-privileged direction: the field grants nothing and gates
no affordance, it only says whether a fetch is owed, so an absent field (an
older payload) must read as resolved-inline rather than leaving a client waiting
forever. It is a **data-state marker, not a capability** — #2128's rule (the
roster payload is *the* portal capability channel) is untouched.

The client stamps `unavailable` too, for a failed or 429'd hydration call, using
the same word rather than inventing a fourth.

## The three loading zones (AC4)

All decidable rules are pure, in `components/portal/portalBriefingState.js`
(`vitest.config.js` pins `environment: 'node'` with no mount harness — a rule
inside an SFC is a rule no test can reach).

| Zone | File | Key | Snaps (no reveal) when |
|---|---|---|---|
| Stage | `views/Portal.vue` | `stageZone(...)` over `viewState` | failed / empty roster, unreachable deep-link agent |
| Conversation body | `components/portal/PortalConversation.vue` | `!historyLoaded` | no messages |
| Briefing hints | `components/portal/PortalBriefing.vue` | `briefingZone(card)` | `unavailable`, or `ready` with nothing in it |

* **The stage waits for `bootstrapResolved`, not just the roster.**
  `activeAgentName` / `pendingSession` are assigned only after
  `refreshThreads()`, so a `/workspace/c/:sid` deep link (or `?agent=X`) would
  otherwise reveal the stage for `agents[0]`, flash that agent's briefing, fire
  a wasted hydration, and then remount for the real target. AC4 says "while the
  roster **and a thread's history** hydrate".
* **The history zone keys on a VERDICT, never `loadingHistory`.** On the first
  turn of a new chat, `session-adopted` sets `pendingSession` with no remount by
  design, and the resulting watcher re-runs `loadThread` — which sets
  `loadingHistory = true` and clears `messages`. An in-flight key would sweep
  the beam over the transcript the user just watched arrive and then "reveal"
  it: precisely the p13 lie AC4 exists to remove. `historyLoaded` is true at
  mount for a brand-new chat, set in `loadThread`'s `finally` (a verdict either
  way), and never goes false again on that instance; `convKey` remounts
  re-derive it.
* **No zone passes `announce`.** The primitive puts `role="status"` — an
  implicit `aria-live="polite" aria-atomic="true"` region — on the zone ROOT. The
  stage zone wraps the whole conversation including the composer and the history
  zone wraps the transcript, so every appended or streamed message would be
  re-announced in full. `aria-busy` still rides each root while loading. Moving
  the live region onto the primitive's `sr-only` span is a one-line follow-up
  recorded for #1921/ent#245.
* **`ScanlineReveal` gained `content-class`.** `.scan-content` is the
  primitive's own wrapper and `:deep()` is forbidden by its contract, so a zone
  whose loaded content must FILL a flex column had no hook. Two lines, default
  `''`, no existing consumer changes.
* **A background refetch is invisible.** `mergeRosterBriefings` carries hydrated
  cards across a roster refetch (no rising loading edge), and `fetchRoster`
  re-enters loading only when there is no data on screen — so a "Try again"
  after a failed first load shows the scanline instead of flashing "No agents
  shared with you yet" (a pre-existing p15 lie the AC4 line sat next to).

Deliberately NOT swept here: `PortalFilesPanel.vue`'s `animate-spin`,
`PortalSidebar.vue`'s `animate-pulse` roster skeleton (#2159's), and
`PortalAgentPage.vue`'s own loading. Those stay on #1921 per the operator's
ruling.

## Client hydration rules

| Rule | Why |
|---|---|
| The batch fires from the store's **roster-success branch**, not `bootstrap()` | Both "Try again" buttons call `store.fetchRoster()` directly; a failed-then-retried first load would strand every non-active card `pending` for the session (contract principle 21: loading behaviour lives in stores) |
| It fires at **≥ 1** pending card | A deep link into an EXISTING thread never mounts `PortalBriefing`, so a one-agent roster would never hydrate at all |
| The active agent's single is driven by `Portal.vue`'s `activeAgent` watcher | `PortalBriefing` renders only in the conversation's `#empty` slot; and the agent's `/` typeahead reads the same `playbooks`, so it would otherwise wait for the batch's slowest member. The component stays presentational |
| A single is **never coalesced** into an in-flight batch | Coalescing hands the active agent exactly the floor this issue removes |
| `unavailable` → `pending` on an explicit refetch; otherwise retried **once per session** | A refetch is a user act, so one earlier blip must not read "couldn't load" until reload; without the cap a wedged agent costs a call per chat open |
| `hydrateBriefings` never throws | A degraded briefing is not a failed Workspace; `store.error` belongs to the roster |

Cost, stated honestly: the active agent is briefed twice per sign-in (its single
plus the unfiltered batch) — N+1 bounded agent calls, not "the same as today".
Accepted: the alternative (a batch that excludes the active agent) cannot know
which agent that is at roster-success time and would carry N names in a URL.
Lazy-on-picker-open was considered and rejected as stated — the picker row is
`<span v-if="a.description">`, so descriptions arriving after it opens grow rows
under the cursor; the path if fleet cost ever matters is to reserve that line's
footprint first.

## Tests

**Backend** — `tests/unit/test_2163_roster_latency_floor.py` (29). What is pinned
is the COUNT and the BOUND, never a duration on a tiny fixture: a timing
assertion on a 12-agent fixture passes against the old code too, which is how
#2160 shipped.

| Test | Property |
|---|---|
| `test_roster_awaits_no_briefing_even_when_every_agent_hangs` | **crux** — a stub that can never resolve; the roster returns inside `wait_for(…, 1.0)` and the stub is never called |
| `test_the_roster_source_no_longer_fans_out` | source pin: no `gather(` / `_agent_briefing(` in `get_roster` (docstring included) |
| `test_one_hung_agent_does_not_delay_the_other_briefings` | the bound on the batch, budget patched to 0.05 s |
| `test_a_bound_trip_is_reported_unavailable_not_hintless` | a trip is `unavailable`, on both paths |
| `test_an_empty_briefing_that_COMPLETED_is_still_ready` | `ok` = verdict, not data |
| `test_briefings_are_roster_scoped_and_iterate_the_roster` | `AGENT-1` / `not-mine` dropped; the DB string is what is briefed |
| `test_briefings_with_no_filter_cover_the_whole_roster` | the `requested is None` arm |
| `test_briefings_make_no_docker_read` | both Docker seams stay at 0; every attempt carries `"unknown"` |
| `test_semaphore_is_per_request_and_acquired_outside_the_bound` | 40 agents / 16 permits / 0.05 s budget all `ready`, twice through `asyncio.run` |
| `test_the_two_briefing_gets_run_concurrently` | < 0.09 s against 2 × 0.05 s sequential |
| route tests | split/dedupe, named 422 over 200 names raised BEFORE the limiter, `include_owned` threading, the two limiter keys, the tighter unfiltered budget, FastAPI include-time resolution, Invariant #4 ordering |

Updated: `test_2160`'s "the roster still briefs everyone" is **inverted**;
`test_ent79`'s two briefing assertions move to `get_briefings` and the roster
gains a "ships no briefing at all" guard; `test_163` adds the route to its
fenced-route parametrize (the `portal_delegate` fence is a single-route
allowlist, so it is fenced by construction — this is the belt).

**Frontend** — `src/frontend/tests/unit/portalBriefingState.spec.js` (30) over
the pure module; `workspaceRoomsGate.spec.js` F23 follows its roster-verdict arm
up a level to the stage zone.

## Measurement (AC3)

**Method: an offline service-level timing harness, not a live fleet.** The live
dev stack carries exactly one agent container — `agent-trinity-system` — so the
plan's step-3 recipe (`kill -STOP` on a throwaway agent's `agent-server.py`) had
no eligible subject: the only candidate is the orchestrator, which is off limits.
The harness therefore drives the real `get_roster` / `get_agent_card` /
`get_briefings` in both trees (`origin/dev` @ `a4eebdbe` vs this branch) over a
12-agent roster, stubbing only the two edges the measurement is not about — the
SQL roster read and the Docker availability read, a constant addend on *both*
sides. The agent HTTP edge is stubbed at `agent_httpx_client`, so the real
`timeout=` plumbing and the real sequential-vs-concurrent GET structure run.

`docker pause` stays wrong for the same reason the plan gives: a non-`running`
container reads `availability="stopped"`, which the briefing skips before any
HTTP, so the "before" would be fast and prove nothing.

Three unreachable shapes are distinguished, because they exit the briefing by
three different doors and only one of them trips the wall clock:

| Shape | What the agent does | Which bound catches it |
|---|---|---|
| **wedged** (`kill -STOP`) | connect accepted, read never answered | httpx per-phase `ReadTimeout` at 2.0 s |
| **tarpit** | bytes trickle, no per-phase timer ever fires | the 3.0 s wall clock |
| **refused** | container gone, connect refused | neither — fails immediately |

Roster of 12, one agent unreachable, healthy agents answering each GET in 50 ms.
Five runs per cell; `min / median / max` seconds.

| Endpoint | Before (`origin/dev` @ `a4eebdbe`) | After (this branch) |
|---|---|---|
| `GET /my-agents` (12 agents, 1 **wedged**) | 10.003 / 10.004 / 10.086 | **0.000 / 0.000 / 0.000** |
| `GET /my-agents` (12 agents, all healthy) | 0.103 / 0.105 / 0.167 | **0.000 / 0.000 / 0.000** |
| `GET /briefings?agents=<healthy>` | n/a | 0.051 / 0.053 / 0.054 |
| `GET /briefings` (whole roster, 1 **wedged**) | n/a | 2.002 / 2.002 / 2.003 |
| `GET /briefings` (whole roster, 1 **tarpit**) | n/a | 3.001 / 3.003 / 3.003 |
| `GET /agents/X/page` (X **wedged**) | 10.002 / 10.003 / 10.005 | 2.002 / 2.002 / 2.085 |
| `GET /agents/X/page` (X **tarpit**) | (never returns under the old code) | 3.002 / 3.002 / 3.002 |
| `GET /agents/Y/page` (Y healthy) | 0.101 / 0.103 / 0.106 | 0.051 / 0.051 / 0.053 |

**AC3 is met.** The roster's floor is no longer its slowest agent: with one
wedged agent `/my-agents` goes from 10.0 s to 0.000 s, and the active agent's own
hints (`/briefings?agents=<healthy>`) arrive in 0.053 s regardless of what the
rest of the fleet is doing — which is what the `?agents=` filter exists for.

Three things the numbers say that the plan predicted but had not yet shown:

- **"5 s was a floor, not a ceiling."** The old literal is httpx's *per-phase*
  timeout and the two GETs ran sequentially, so one wedged agent cost **10 s**,
  not 5. The `gather` then made that the whole roster's cost.
- **The wall clock is not redundant with the httpx value.** It never fires in the
  wedged shape (2.0 s catches it first) and is the *only* thing that returns at
  all in the tarpit shape.
- **The concurrent-GET change halves the healthy agent page**, 0.103 s → 0.051 s
  — a second, unadvertised win from the same edit.

Re-run at 250 ms per healthy GET to confirm nothing above is an artifact of the
50 ms figure: before `/my-agents` 10.005 s (wedged) / 0.506 s (healthy), after
0.000 s in both; healthy agent page 0.503 s → 0.252 s. The shape holds.

**What this method does NOT prove.** It does not exercise the real network, the
real agent-server, TLS/DNS, or the backend's own request path — it times the
service functions. It models the two GETs as fully concurrent, whereas a real
agent server is one uvicorn worker whose event loop serialises them, so the
measured "after" healthy figure is the optimistic end. And the Docker read is
stubbed to zero on both sides, so every number above excludes it (it is a shared
addend, and a named residual below).

### Healthy-busy tail (step 5b) — component measured, tail NOT measured

The question the constants turn on is whether a *healthy but busy* agent can
exceed 2.0 s on a briefing GET. It has two parts and only one was reachable here.

**Measured — the scan's own cost.** `GET /api/skills` is served by an `async def`
that calls the synchronous `scan_skills_directory` inline, with no `to_thread` /
`run_in_executor`, so it occupies the agent server's event loop for its whole
duration (verified in `docker/base-image/agent_server/routers/skills.py`). Timed
directly against synthesised skill trees, 20 runs each:

| Skills | p50 | p95 | max |
|---|---|---|---|
| 10 | 1.9 ms | 2.5 ms | 2.6 ms |
| 50 | 9.5 ms | 11.7 ms | 12.0 ms |
| 200 | 37.2 ms | 38.2 ms | 44.4 ms |

≈0.19 ms per skill — **three orders of magnitude under the 2.0 s bound**. The
scan itself cannot plausibly trip it.

**Not measured — event-loop contention.** That leaves exactly one path to a
false trip: the agent server's single event loop being occupied for >2.0 s while
a turn runs. Reproducing it needs a real agent mid-turn, and no eligible agent
exists on this host (see the method note above); starting a turn would also mean
spending on the live fleet. **Named human step before the PR leaves draft:** on an
instance with a real non-system agent Y, start a turn that runs several tool
calls and time `GET /briefings?agents=Y` ×10 during it; if p95 exceeds
`_BRIEFING_HTTP_TIMEOUT_SECONDS`, raise the constants (still well under 5 s).

**The constants are UNCHANGED at 2.0 s / 3.0 s** — nothing measured contradicts
them, and they are not moved on a guess. But note what the tightening from 5.0 s
costs if the tail does turn out to be long: per the finding below, a healthy-but-
slow agent does not render as `unavailable`, it renders as an agent with **no
hints**, and the client never retries it.

### Finding at verification — an unreachable agent does not always read `unavailable`

Measured, not inferred. `briefing_state` for the SAME unreachable agent depends
on which door the failure exits by:

| Shape | `/briefings` entry | `get_agent_card` | Latency |
|---|---|---|---|
| **tarpit** | `unavailable` ✅ | `unavailable` ✅ | 3.0 s (wall clock) |
| **wedged** (`kill -STOP`) | `ready` ⚠️ | `ready` ⚠️ | 2.0 s (httpx) |
| **refused** (container gone) | `ready` ⚠️ | `ready` ⚠️ | ~0 s |

The cause is structural, not a slip: `ok=False` is reachable only from the
`availability` early return, a non-tuple return, or the wall clock tripping.
Every HTTP failure is swallowed one level down — `_agent_briefing` has a
`try/except` per GET leg *and* an outer `except Exception` — so an httpx
`ReadTimeout` or `ConnectError` returns a normal, empty `AgentBriefing()` well
inside the budget, and `_bounded_briefing` correctly reports that it "reached a
verdict", which is what its docstring defines `ok` to mean.

The consequence is the one D4 was written to prevent: the two commonest
unreachable shapes render as a genuinely hint-less agent ("Start a conversation
below."), and because `shouldRequestBriefing` retries only `unavailable`, the
client never tries again for the rest of the session. It also makes
`get_briefings`' own docstring wrong where it says a stopped or absent container
"lands as `unavailable`" — measured, it lands as `ready`. Note `/briefings`
passes `availability="unknown"` for every name by design (no Docker read), so
the `availability` door — the one that *does* yield `unavailable` — is never
taken on that route at all.

**Not changed here** (a verification stage measures; it does not redesign). The
honest fix is for `_agent_briefing` to report *reachability* separately from
*content* — one flag, set where the excepts already are — so `ok` can mean "the
agent answered". Recorded for the lane owner's decision before the PR leaves
draft: either the behaviour changes, or the two docstrings and D4's rationale
are corrected to say that only the wall-clock shape is distinguishable.

**AC3 is unaffected** — it is a latency criterion, and the latency numbers above
meet it in every shape.

## Residuals

- **The roster's Docker read is the remaining synchronous dependency.**
  `docker.from_env()` sets no `timeout=`, so the SDK default applies and a hung
  daemon can still hold the roster for up to 60 s. Named, not fixed here — a
  `timeout=` on the shared client is a fleet-wide change with its own issue.
- **Socket fan-out across concurrent sign-ins**: the semaphore is per request,
  so ten concurrent 100-agent sign-ins can hold 160 backend→agent sockets
  (today: 1000, on the critical path). Bounded per request and off the critical
  path; a global governor is out of scope.
- **Rate limits**: a client visiting more than ~58 unhydrated agents in a
  minute, or reloading the roster more than 10 times a minute, gets
  `unavailable` on the tail. One retry per agent per session, no loop.
- **Briefing-zone footprint**: `min-h` reserves the header plus one hint row; a
  taller grid grows DOWNWARD into the empty pane, and the composer is pinned to
  the bottom of the flex column, so nothing visible moves. Reserving the full
  collapsed fold was rejected — it would leave a ~17–30 rem hole under "Start a
  conversation below." for every hint-less agent, which is the everyday case.
- **Live-region gap**: with no `announce`, screen readers get `aria-busy` only.
  The correct fix is the primitive's, recorded for #1921/ent#245.
- **Documented headless surface change (ent#83)**: `/my-agents` cards now carry
  empty briefing fields plus `briefing_state: "pending"`; API-only clients that
  want the briefing read `/briefings`. Mitigated by the default-`"ready"` field
  (older payloads still validate) and by the state being self-describing.
