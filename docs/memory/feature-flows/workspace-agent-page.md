# Feature: the Workspace agent page

> **Status**: ✅ Implemented (2026-08-13)
> **Issue**: abilityai/trinity-enterprise#360
> **Requirement**: `docs/memory/requirements/core-agent.md` §5.11
> **Related**: [workspace-sidebar-ia.md](workspace-sidebar-ia.md) (the roster row that opens it), [workspace-absorbs-session.md](workspace-absorbs-session.md)

## Overview

An agent had no home. A roster row emitted `new-chat-with-agent`, so there was
nowhere to see what an agent had been doing, nowhere for it to ask you something
while no chat was open, and nowhere to show what it can do. Clicking an agent now
opens its page; starting a chat is an explicit button there.

## The two constraints, and why both are subtractive

**It reports; it does not configure.** No schedules, no skill editing, no logs,
no costs. Model and plan are not shown at all — the AC permits them
"informational and visibility-gated", and the cheapest way to satisfy a gate is
to not open the door. Building agents stays operator-side.

**The viewer may be an external client.** The same page serves a portal-token
client and a platform user. That is what makes this a security surface rather
than a layout exercise, and it decides *where* the exclusions live.

## Exclusion by projection, not by template

Every field the page must not show is dropped in `client_portal/agent_page.py`,
before the payload exists. Nothing is filtered in the Vue component.

The reason is failure mode, not taste: a template filter is correct until
somebody adds a column to a list view, and nothing fails when they do. A field
that never leaves the service cannot be surfaced by a later edit.

| Surface | Underlying accessor also returns | Why it must not ship |
|---|---|---|
| `recent_work` | `message`, `cost`, `model_used`, `source_user_email` | `message` is another user's prompt; `cost` and `model_used` are excluded by AC #7 |
| `recent_work` | — *except* `schedule_name` (#2161) | The one deliberate crossing. See [What crosses, and why it is the name and not the message](#what-crosses-and-why-it-is-the-name-and-not-the-message) |
| `asks` | `context`, and `alert`-type items | `context` is free-form agent JSON and a known credential-leak surface (canary G-04 exists because secrets turn up there). `alert` items are platform-generated ops telemetry — sync-failing, git-bloat, breaker dormancy — not an agent asking a person anything |
| report detail | any report in the install | Report ids are global. The roster gate proves only that the caller may reach **this** agent, so without an ownership check the page becomes a reader for every report on the instance. A foreign id returns the same 404 as a missing one, so it is not an existence oracle either (invariant #8) |

## Flow

```
GET /api/enterprise/client-portal/agents/{name}/page?window=7d
  ├─ _require_roster ............ uniform 404 for an agent off the roster
  ├─ get_roster ................. identity + "what it can do" (briefing, #138/ent#380)
  └─ agent_page.build_page
       ├─ _health ............... last persisted health check
       ├─ _stats ................ db.get_agent_analytics (#1107) + first_try_stats
       ├─ _asks ................. operator queue, filtered + projected
       └─ _recent_work .......... executions, projected to shape
            └─ _schedule_names .. ONE query, id → name only (#2161)
```

One call rather than five, because the page is one screen: fetching header,
stats, asks and work separately renders it in pieces, and a stats call that
outruns the header shows numbers above a nameless card. Reports and Files are
separate — most visits never open those tabs, so they fetch on first open.

`GET /api/agents/{name}/analytics` is reused as the Technical Notes ask, but
through the **DB accessor**, not over HTTP: the platform endpoint is JWT-gated
and a portal-token client cannot call it.

## Degradation — AC #6

Everything is DB-sourced, so a stopped agent renders degraded rather than empty,
and a failing data source degrades **only its own section** (a page that 500s
because the operator queue is unhappy is worse than one without its asks).

Health reports `unknown`, never `unhealthy`, when nothing has ever checked the
agent. Monitoring is default-OFF (#1121), so on many installs that is every
agent, and "unhealthy" there would be a lie about the whole fleet.

## The two AC #3 metrics

**First-try rate** is real: successes with `retry_count` 0 over the window
(`client_portal/db.py::first_try_stats`). It is deliberately distinct from the
success rate the analytics accessor reports, which counts a retried-then-
succeeded execution as a success — the right answer to "does it get there in the
end", the wrong answer to "does it get there first time". `retry_count` is NULL
on rows predating #678, read as zero, since such a success genuinely had no
retry. No terminal executions ⇒ `rate: null`, not `0.0`: a fresh agent has no
first-try rate, and 0% reads as "it fails every time".

**Rating tally is not shipped.** There is no rating, thumbs or feedback
mechanism anywhere in Trinity — no table, no column, no endpoint. It has no data
source, so it was omitted rather than invented. A number a user reads as "how
well is this agent doing" has to come from something real.

## The UX repairs (#2161)

The page shipped with four defects, and fixing them forced two of ent#360's own
acceptance criteria to be revisited rather than implemented. Both overrides are
recorded here because a later contributor reading only the issue would "fix" them
back.

### What crosses, and why it is the name and not the message

ent#360 projected `recent_work` down to shape, which was right about `message`
and left every scheduled row rendering the same three words — "Scheduled run",
eight times. The issue's AC #3 asked for a **message summary**; the answer is a
**schedule name**, and the distinction is the whole decision:

* the schedule's `message` is a **prompt**, written to instruct an agent, and is
  precisely what a page serving external clients must not show;
* its `name` is a short label an operator wrote to tell schedules apart, which is
  the same job the row needs done.

A per-viewer variant was considered — showing a viewer their *own* prompts, since
`source_user_email` identifies them and that leaks nothing cross-user — and
rejected: it doubles the row's shape by audience, and the product decision was no
prompt text on this surface at all. The cost is honest and bounded: rows that are
not schedule-backed (chat, loop, reminder) still carry only trigger, duration and
time, so **AC #3 is met for scheduled rows only**.

**It is not "operator-authored", and calling it that was the comfortable
mistake.** `POST /api/agents/{name}/schedules` is `AuthorizedAgent` and the MCP
`create_agent_schedule` tool exists, so an agent-scoped key — hence a
prompt-injected agent — can write the text that renders on a client's page. So it
is treated as untrusted content: capped at `MAX_SCHEDULE_NAME_CHARS` (80) in the
service, escaped by Vue interpolation on render. The same is already true of
`asks` (`title` and `question` are agent-authored), so this is the established
boundary rather than a new one — but it is why the name is bounded rather than
trusted. Gating it on `principal.is_platform` is one line away if an operator
ever objects.

Four properties of the lookup, each load-bearing:

* **The prompt is never loaded at all.** `db.get_agent_schedule_names` is a
  projected `SELECT id, name`, not a `.name` read off `list_agent_schedules`,
  which returns whole `Schedule` models carrying `message` and
  `validation_prompt`. Loading those and declining to return them would make
  this module's own principle — *a field that never leaves the service cannot be
  leaked by a later edit* — a review invariant; not loading them makes it
  structural. One `{s.id: s}` refactor is all it would otherwise take, so a test
  pins that the prompt-carrying accessor is not called.
* **One query per page**, never `get_schedule` per row — that is an N+1 whose
  only symptom is latency, so the test asserts the *call count*, not the output.
* **Agent scoping is free.** Because the map holds only this agent's schedules, a
  foreign or stale `schedule_id` simply misses. There is no ownership check to
  forget.
* **It fails soft.** A schedules read that raises costs the labels, never the
  rows — the section is the executions; the names are a garnish on them.

Misses are ordinary and are shown as a plain trigger label: the `__manual__`
sentinel (chat turns, reminders), and soft-deleted schedules, whose executions
outlive their name by up to 30 days (#834).

The name attaches to **any** row whose id resolves, not only
`triggered_by == "schedule"`: a webhook that fires a schedule is running that
schedule, and naming it is the point.

### Asks stay on the Overview — AC #4 overridden

AC #4 asked for a dedicated tab with a count badge. The product decision was to
keep asks where they are: the page's own reason to exist is that an agent can
reach you when no chat is open, and a tab is a place you have to *go*. The defect
was that they were unbounded, not that they were present. So they are contained
instead — compact cards, the question clamped, the first five with a counted
"Show all" toggle.

Two constraints on that layout:

* ~~**Asks are first in DOM order**, so the mobile stack keeps the priority the
  page was built around.~~ **Superseded by #2169** — see below. Asks now sit
  below the top row on every breakpoint, which on a narrow viewport puts them
  third.
* **No nested scroll region.** #2101 settled this on this surface: the page has
  one scroll axis, and a pane that scrolls inside a page that scrolls traps the
  gesture on touch. Containment is first-N-plus-toggle, not `overflow-y-auto`.
  **Still true after #2169** — the section moved, its containment did not change.

The badge reads `20+` at the cap, because `MAX_ASKS` truncates server-side — a
bare "20" against 50 pending is a wrong number, not a rounded one.

### The Overview row is unconditional, and asks moved below it (#2169)

#2161 put asks and recent work in a grid whose column count was **bound to
`asks.length`** (`:class="{ 'lg:grid-cols-2': asks.length }"`), with the chart
alone above in a `max-w-2xl` section. That made the page's shape a function of
its data: an agent with nothing waiting collapsed to one column, so the layout
changed whenever a transient operator-queue item opened or closed.

The top row is now **unconditional** and its occupants are the chart (left) and
recent work (right); asks sit **below it at full width**, still only when there
are any and still with no empty-state placeholder — an agent with nothing
waiting must not advertise the section. Nothing has to collapse, because both
row occupants already own an empty state ("No activity in this window." /
"Nothing yet."). The chart's `max-w-2xl` went with the move: inside a half-width
column that cap does not bind below ~1656px.

**The split is `xl`, not `lg`, and that is arithmetic.** The Workspace holds a
288px sidebar plus page padding and the 24px gap, so at 1024px each column is
332px — where the 30-day x-axis (one truncating 9px label per day) reads as
nothing and a nine-bucket legend wraps three to five lines. At 1280px the column
is ~460px. A layout that technically satisfies "two columns" while making the
left one unreadable fails the purpose. **Known residual:** the 30-day x-axis
still truncates in a half-width column (measured 5/6 shown ticks at 1280, 3/6 at
1600); 7d and 14d are clean. The fix is width-responsive tick density inside
`StackedBarChart.vue`, which the operator Overview also consumes — out of scope
here, deliberately.

**Moving asks below reverses #2161's DOM-order decision**, on instruction. The
residual is bounded rather than a priority inversion: the header carrying the
Overview tab's ask-count badge is `shrink-0` and sits **outside** the page
scroller, so a narrow viewport shows the count at every scroll position; only the
ask text moves below the fold. If it needs reversing, the costed alternative is
~3 lines — keep the asks section first in DOM inside the same grid with
`xl:col-span-2 xl:order-last` — which reads identically on desktop and keeps asks
first on mobile, at the price of a DOM/visual order split.

The **loading skeleton** gained the same `xl` split. It was one column in front
of what is now a two-column row, so every load ended in a reflow — and the
no-asks agent this issue is about is exactly the case where skeleton and loaded
state used to agree. Measured after the change: skeleton blocks and loaded
sections land on identical boxes (x 312/876, y 198, w 540).

### The avatar has an edge (#2169)

`PortalAvatar` was a bare `rounded-full` span, so an image avatar with light
edges bled into the sidebar and chat surfaces. It now carries
`border border-gray-300 dark:border-gray-700` — the contract's `border-strong`
pair, one line in the one shared component, reaching all fourteen call sites.

Three things settled by measurement rather than preference:

* **`border`, not `ring-inset`.** An inset ring paints below child content and
  the `<img>` is exactly the padding box, so it is invisible on precisely the
  image avatars this fixes — the edge would show on initials avatars only.
* **`border`, not an outer ring.** `PortalChatRow` already passes
  `ring-2 ring-gray-50 dark:ring-gray-950` into this component as the
  stacked-avatar separator, and all Tailwind rings share `--tw-ring-*` and one
  `box-shadow`. Border and ring are independent properties and compose; verified
  at 5× magnification that the separator gap survives.
* **`border-strong`, not `border`.** The reported case is the light theme:
  `gray-200` against the sidebar's `gray-50` ground measures 1.19:1, an arc too
  faint to see at 26px. `gray-300` is 1.41:1.

`box-sizing: border-box` means the outer footprint is unchanged — measured exact
at all nine sizes in use (16, 18, 20, 22, 26, 28, 30, 52, 64) — and the image
insets 1px per side rather than clipping. The `+N` overflow chip in
`PortalChatRow` is hand-rolled rather than a `PortalAvatar`, so it carries the
same recipe explicitly; without it a four-agent row draws three hairlined circles
and one bare blob.

Geometry is the one thing this project's unit tests structurally cannot see (no
layout engine, no mount harness — the ent#245 class), so it is pinned by
`e2e/portal-agent-page-overview.spec.js` (`@interactive`) and verified in the
browser in both themes.

### The chart is the operator surface's chart

The header's bespoke full-bleed CSS bar strip is gone; the Overview opens with a
bounded card rendering `StackedBarChart.vue`, the same component Agent Detail
uses (#1107). The payload already carried everything it needs, so this is a
deletion plus a mount. (#2169 moved that card into the left half of the top row
and dropped its width cap; the component and its props are unchanged.)

Two things moved to make that honest rather than duplicated:

* `BUCKET_COLORS` now lives in `utils/executionBuckets.js`. It had been inline in
  `OverviewPanel.vue`; a second copy is exactly the shape that drifts, and the
  *order* is a contract with the backend's `_BUCKET_ORDER`.
* `_stats` **forwards** the analytics accessor's own `buckets` list rather than
  letting the portal re-derive one from `by_type`. The two are equivalent today,
  which is the reason to pick one — not the reason to keep both.

The legend needed client-facing wording ("Tool call", not "MCP") to match the
`triggerLabel` translation the page already does everywhere else. That is an
**optional `labels` prop** on the chart, never a rename of the `buckets` array:
those entries are the keys the chart indexes `by_type` with, so translating them
in place makes every lookup miss and renders an **empty chart** — a silent
failure that reads as "this agent did nothing this week".

Empty and unavailable are different sentences ("No activity in this window" vs
"Stats are unavailable right now"), and the window selector is hidden on the tabs
it does not drive.

### "Start a chat" did nothing — the third time this shape broke

The button was wired correctly. `newChatWithAgent` prepared the chat and then
failed to leave the route, so `PortalAgentPage` — which renders **first** in the
stage chain and is keyed on `route.params.agentName` — kept the stage and the new
chat was set up invisibly behind it.

The escape test read `route.params.sessionId || route.params.roomId`. That list
is written once and goes stale every time a stage route is added: #2128 found
`roomId` missing from guards written when `sessionId` was the only stage route,
and ent#360 then added `/workspace/a/:agentName` without revisiting them.

So the question is inverted rather than extended. `shouldEscapeStage(path,
query)` in `portalUtils.js` asks about route **shape** — anything that is not the
bare workspace root is a stage that must be left — and therefore **fails
closed**: a fourth stage route needs no edit here and cannot silently re-break
the button.

The **query** is half of that shape, and it is the half that leaks across
sessions. `?agent=` is the ent#358 landing spot and is re-read by `bootstrap()`,
which runs again after a sign-in — so signing out at `/workspace?agent=X` and
handing the browser on makes the *next* person's first screen "You don't have
access to X". That predates #2161 (the param enumeration missed it too), but it
is the same class the guard exists to close, so it lives in the same predicate
rather than a second one somebody has to remember.

Two live call sites, both fixed: `newChatWithAgent` and `onSignOut` (which
carried a room id, and then an agent name, into the next session's address bar).
A third, `startBlankChat`, was **deleted** — it had no callers at all, which
makes it the plausible-looking fix that would have changed nothing.

## What this supersedes

ent#359 made a roster row with unread open the *unread chat*, because a badge
reading "2 replies" beside a control that opened a **blank** chat was a
contradiction. The page resolves that properly: the row opens the page, the
badge still shows on the row, and the page's Overview lists the chats this agent
belongs to with their unread counts. Nothing is lost, and "an agent is a
destination" is finally true.

## Files

| Layer | File | Change |
|---|---|---|
| Service | `client_portal/agent_page.py` | **new** — assembly + every projection; `_schedule_names` + forwarded `buckets` (#2161) |
| DB | `client_portal/db.py` | `first_try_stats` |
| Router | `client_portal/router.py` | 3 endpoints, roster-gated |
| Models | `client_portal/models.py` | page / ask / work / stats / report models; `schedule_name`, `buckets` (#2161) |
| UI | `components/portal/PortalAgentPage.vue` | **new** — header, stats strip, 5 tabs; chart card + contained asks (#2161); unconditional `xl` 50/50 top row, asks below it, two-column skeleton (#2169) |
| UI | `components/portal/PortalAvatar.vue` | 1px `border-strong` edge, both themes (#2169) |
| UI | `components/portal/PortalChatRow.vue` | same edge on the hand-rolled `+N` overflow chip (#2169) |
| UI | `components/portal/PortalSidebar.vue` | roster row emits `open-agent` |
| UI | `components/portal/portalUtils.js` | `shouldEscapeStage`, `PORTAL_BUCKET_LABELS` (#2161) |
| UI | `components/StackedBarChart.vue` | optional `labels` prop (#2161) |
| UI | `utils/executionBuckets.js` | **new** — `BUCKET_COLORS` + chart helpers, shared with `OverviewPanel.vue` (#2161) |
| UI | `views/Portal.vue`, `router/index.js` | `/workspace/a/:agentName`; shared stage escape (#2161) |
| Store | `stores/clientPortal.js` | `fetchAgentPage`, `fetchAgentReports`, `fetchAgentReport` |

## Tests

`tests/unit/test_ent360_workspace_agent_page.py` — the projections (no
message/cost/model; alerts excluded; `context` never present, asserted against
the rendered repr too), cross-agent report isolation and its 404 uniformity,
degradation (health `unknown`, per-section failure, capabilities from the
briefing), and the first-try arithmetic including the NULL-`retry_count` and
no-terminal-rows cases.

Verified live against the running instance: 37 executions, 89% completed, 33/37
first try, and `recent_work` carrying exactly the six safe keys (seven since
#2161 — the exact-dict assertion is what forced that addition to be argued rather
than absorbed).

`tests/unit/test_2161_agent_page_ux.py` — the schedule-name seam: it resolves for
scheduled *and* webhook-fired rows, never reads the schedule's `message`, misses
harmlessly on the `__manual__` sentinel / soft-deleted / foreign ids, costs
exactly one query for the whole page, and degrades to unlabelled rows rather than
no rows. Plus the forwarded `buckets` on both the healthy and unavailable stats
envelopes.

`src/frontend/tests/unit/portalAgentPageUx.spec.js` — the pure halves and the
source-structure guards, since this project has no component-mount harness. The
load-bearing case is `shouldEscapeStage('/workspace/x/whatever')`: it asserts a
route **nobody has written yet** still escapes, which is the property a
param-enumerating guard cannot have and the reason this bug shipped twice. Also
pins that the chart is stacked by untranslated buckets while the labels ride the
separate prop — the mistake that renders a blank chart.

`src/frontend/tests/unit/workspaceRoomsGate.spec.js` — F24 was **rewritten**, not
deleted. It used to require each exit function to contain `route.params.roomId`;
after #2161 that would mandate the enumeration that *was* the defect, so it now
requires the shared escape and asserts the enumeration is gone.

## Known Limitations

| Limitation | Detail |
|---|---|
| **Asks are read-only** | Answering writes to the operator queue, an operator surface with its own auth. Rather than render a control that 403s for a client, the card offers "Reply in chat →". |
| **No rating tally** | See above — no data source exists. |
| **Files tab is a list, not the panel** | It lists documents and uploads; the upload flow stays in the existing files panel. |
| **Non-scheduled rows still carry no context** (#2161) | `schedule_name` answers for schedule- and webhook-backed work. A chat, loop or reminder row has no equivalent safe label, and its message is a prompt — so those rows keep trigger, duration and time. AC #3 is met for scheduled rows only. |
| **Ask count saturates at 20** | `MAX_ASKS` truncates server-side, so the badge reads `20+` rather than a true count. |
| **"What it can do" is the briefing** | A projection of the roster briefing (#138/ent#380). ent#178 (unified exposable-skills config) is the mechanism this becomes a view of; it deliberately does not build a competing one. |
