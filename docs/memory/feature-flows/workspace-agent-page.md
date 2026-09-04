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
| `recent_work`, `stats` | whole ROWS with `triggered_by = "loop"`, for a client only (#2423) | A client cannot open a loop, see what it produced, or start or stop one — the strip is `isPlatformSession`-gated (ent#458) and this page has no Loops tab. So the loop COUNT was client-visible while the loop OUTPUT was operator-only. Same subtractive rule this section states, and the same reason `alert` asks are dropped: operations telemetry, not something the agent is asking a person. **Operators keep every row** — they can click through to Agent Detail → Loops, so hiding it there removes real signal and fixes nothing |

**The loop exclusion is a row filter, so it runs in SQL — before the `LIMIT`.**
`get_agent_executions_summary` takes an `exclude_triggers` set and adds it as a
`WHERE`, so the limit applies to rows that already survived the filter. Filtering
the RESULT instead starves the list: an agent whose newest rows are all loops —
ent#458's normal shape, not an edge — yielded an EMPTY list and the page read
"Nothing yet." while the operator saw twenty. Trading rows a client cannot
explain for a false claim of no activity is a worse bug than the one being fixed.

The first fix over-fetched `MAX_RECENT_WORK * 5 = 100` rows and filtered in
Python. That is the same bug with a constant in front of it, and **the constant
loses**: `models.MAX_RUNS_LIMIT` is 100, so ONE loop at its documented maximum
emits exactly 100 consecutive rows and consumes the entire over-fetch window. No
multiplier survives a product limit — a reviewer picks the multiplier, the
product picks the run length. Moving the filter also deletes the extra read: the
client page now fetches exactly `MAX_RECENT_WORK` rows, like the operator page,
instead of five times as many to discard most of them.

`_last_active` carries the same exclusion for the same reason. Reading the newest
row unconditionally reported a loop run's timestamp to a client for whom that row
does not exist — a header saying "active 2 minutes ago" above a list whose newest
entry is yesterday's, with nothing on the page to reconcile the two. `limit=1`
makes it the extreme case: no over-fetch is even conceivable there.

**`success_rate` and `first_try` are not re-derived — but they are withheld at
zero.** A filtered numerator over an unfiltered denominator is worse than a
figure that is merely broad, so both stay computed over every terminal row. That
argument holds only while there is visible work to be broad *about*: on an agent
whose window is entirely loops, the strip read `0 executions · 89% success ·
33/37 first try` — three numbers describing work the same strip says did not
happen, and a contradiction the client has no way to resolve. At exactly zero
visible executions both rates are therefore **withheld** (`null`, which the UI
already renders as an em-dash), never zeroed: 0% reads as "it fails every time".
One surviving row is enough to keep the broad figures. Operators are never
subject to it — nothing is hidden from them, so their zero is a real zero.

NULL `triggered_by` is explicitly NOT excluded. `triggered_by` is `NOT NULL` in
the schema, but SQL `NOT IN` evaluates to NULL for a NULL left side and the row
would silently vanish — an unclassified row is not a hidden one.
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

**#2196 adds `header.availability` beside it — a second, independent fact, not a
second health source.** It is a *projection of the roster card* (`get_agent_card`
resolves it once, `build_page` copies it), never a second Docker read of its own.
The two are rendered as two labelled facts because their freshness differs by
construction: `health` is the last persisted `agent_health_checks` row and is
stale by design, while `availability` is read at request time. Overloading the
health dot would leave the viewer unable to tell which of the two they were
looking at. `build_page` writes `card.get("availability") or "unknown"` — the
bare `.get` is a 500, because `card` is documented-reachable as `None` (the agent
vanished between the roster read and the page read) and an explicit `None` fails
the `Literal`.

## The two AC #3 metrics

**First-try rate** is real: successes with `retry_count` 0 over the window
(`client_portal/db.py::first_try_stats`). It is deliberately distinct from the
success rate the analytics accessor reports, which counts a retried-then-
succeeded execution as a success — the right answer to "does it get there in the
end", the wrong answer to "does it get there first time". `retry_count` is NULL
on rows predating #678, read as zero, since such a success genuinely had no
retry. No terminal executions ⇒ `rate: null`, not `0.0`: a fresh agent has no
first-try rate, and 0% reads as "it fails every time".

**Rating tally.** It was omitted at first because nothing in Trinity produced
ratings — no table, no column, no endpoint — and a number a user reads as "how
well is this agent doing" has to come from something real. ent#366 then shipped
the data source: a Workspace thumb writes to `agent_evaluations` under
`evaluator = workspace:<email>`, and `_rating_tally` projects the up/down counts
through `platform_db.workspace_rating_tally`. It degrades to no tally rather than
failing the page, and the count is of PEOPLE, not clicks — the ent#366 uniqueness
constraint makes a re-rate a correction. (This paragraph asserted the opposite
for two releases after the feature landed; corrected in #2423 review.)

## The Reports tab (#2162)

The tab shipped `<pre>{{ JSON.stringify(payload, null, 2) }}</pre>`. Read beside
this document's own thesis, that is not a cosmetic gap: the section above refuses
to expose an ask's `context` at all because free-form agent JSON has been a
credential-leak surface (canary G-04) — and `agent_reports.payload` is the *same
category*, filed by the same agents, and was being dumped key-for-key to an
external client. Routing it through the shared `components/reports/` renderer set
**narrows** what crosses, because a typed renderer reads only the keys its hint
declares (`tiles`, `columns`+`rows`, `markdown`, `events`) and never the rest of
the payload.

**Rendering is presentation, and this does not move the exclusion boundary.**
Everything the page must not show is still dropped in `client_portal/agent_page.py`
before the payload exists; nothing is filtered in the Vue component. What changed
is how the payload that legitimately crosses is *presented*. The payload itself
remains agent-authored untrusted content of the same class as `asks.title` and
the schedule name — bounded and escaped, never trusted.

**The fallback is the one place this surface deliberately differs from the
operator ones.** The shared set's fallback is the raw JSON viewer, so reuse alone
could not satisfy "never a raw dump to a client" — and AC #2 asks for a fallback
*"deliberately stricter than the operator side, because the audience is an
external client"*, i.e. it asks for a SPLIT, not for a stricter default
everywhere. So `ReportRenderer` gained a `fallbackComponent` override defaulting
to `ReportJson` — every operator call site passes nothing and renders exactly what
it always did, because a raw payload is the useful answer when you are debugging
an agent's own output — and this page passes `ReportSummary`: a bounded key-value
view (≤40 entries with a counted remainder, ~200-char values, depth 1, so a
nested value is described as "12 items" and never serialised) with
credential-shaped tokens redacted at **value** level, and no raw payload
reachable behind it at all. A key-name allow-list was rejected twice over: the
fallback fires precisely on payloads nobody has seen, so an allow-list blanks
nearly all of them, and an allowed key's value carries the secret anyway
(`{"status": "failed: sk-…"}`).

The override deliberately catches an agent-chosen `display_hint: "json"`
(`src/mcp-server/src/tools/reports.ts`) as well as a shape mismatch — replacing
only the *mismatch* path would leave an agent able to put a raw dump in front of
a client by asking for one.

**Honest residual.** A key-value summary still names every top-level key. It
bounds and humanises; it does not eliminate the class, and a well-shaped
`markdown` or `table` report still renders its values as authored. The general
fix is a G-04-style scrub at the portal read boundary — a security change to a
shipping read path, which deserves its own review rather than riding a UI fix.

**Row windowing without a second route.** AC #3 wants #1537's windowed-rows
pattern, and the operator reader `GET /api/reports/{id}/rows` is
`Depends(get_current_user)` — which a portal principal (a verified email with no
`users` row) structurally cannot satisfy, the same fact #2128 hit with
feature-flags. Rather than clone it onto a client-facing prefix, the **existing**
detail route took two optional params:

```
GET .../agents/{name}/reports/{id}?rows_offset=&rows_limit=
   tabular payload  -> payload {columns, rows: window} + row_meta {total, offset, limit}
   anything else    -> payload whole, no row_meta        (rows_limit ignored)
```

The **server** decides tabularity from the real payload, so the client never
predicts a shape from an agent-authored `display_hint` that can disagree with what
was filed — which deletes the 400-and-recover branch a client-side prediction
would need. `rows_limit` absent is byte-identical to before. No new route means no
second gate and no second copy of the 404-uniformity contract: a foreign report id
and a missing one stay indistinguishable through the windowed path too.

**Read amplification is real and mitigated, not hidden.** The slice happens in
Python after the whole (≤5 MiB) blob is read out of the column, so paging
*multiplies* reads — the route that exists to cut transfer raises them. Acceptable
behind an operator JWT; on a prefix a client can loop it is an amplification
primitive, so the route is rate-limited per (client, agent) after the roster gate.
A report whose total fits one page costs exactly **one** request and shows no
footer, so only genuinely large tables page at all.

**Bounded by rows, not by a nested scroll region.** The page has one scroll axis
(#2101, and the asks list above made the same call); a 100-row window plus
`ReportTable`'s stated total and an explicit "Load more" satisfies "contained with
a stated total" without a second scroll axis.

**The store owns the state, and every await is generation-guarded.** A reset
cannot cancel a promise already in flight, so the `reportsLoaded` flag that
contract #15 requires (an empty state must gate on a *succeeded* fetch, never on
list length) would otherwise have turned a transient wrong-render into a permanent
one: switch agents mid-fetch, the old agent's list lands in the cleared state, and
the new agent is marked loaded-with-the-wrong-data for the life of the mount.
Every report request captures a generation counter before its first await and
discards its result if a reset bumped it. The component additionally gates each
read on the state belonging to the agent on screen — the store is a singleton that
outlives it, and a fresh **mount** for a different agent fires no props watcher.

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
not schedule-backed (chat, reminder) still carry only trigger, duration and
time, so **AC #3 is met for scheduled rows only**. (Loop rows reach an OPERATOR
only since #2423 — a client never sees one, so the question does not arise for
them.)

**It is not "operator-authored", and calling it that was the comfortable
mistake.** `POST /api/agents/{name}/schedules` is `AuthorizedAgent` and the MCP
`create_agent_schedule` tool exists, so an agent-scoped key — hence a
prompt-injected agent — can write the text that renders on a client's page. So it
is treated as untrusted content: capped at `MAX_SCHEDULE_NAME_CHARS` (80) in the
service, escaped by Vue interpolation on render. The same is already true of
`asks` (`title` and `question` are agent-authored), so this is the established
boundary rather than a new one — but it is why the name is bounded rather than
trusted. Gating it on `principal.is_platform` is one line away if an operator
ever objects — and #2423 took exactly that route for loop rows, so the split
this paragraph anticipated now exists on the same payload.

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

**The page renders asks ONCE, from `/asks` (#2449).** It used to render them
twice: `<PortalAsks>` off the `/asks` store, and a second "Waiting on you"
section off a `page.asks` payload built by the page's own `_asks()` reader. Both
guards were true at the same time, so a client saw every ask twice — once with
answer controls, once with a "Reply in chat →" link whose justifying comment
("rather than render a control that 403s for a client") predated ent#428's
client answer route and was simply no longer true.

Deleting one was the fix rather than styling around it, because the two halves
disagreed. `page.asks` capped at `MAX_ASKS = 20` where `/asks` fetches 200, and
it carried no `status` and no `expires_at` — so that section could not show an
expired ask as expired (ent#429's AC) and counted one in the Overview badge
where the sidebar's `askCount`, pending-only by design, did not. The badge now
counts the same store list the section renders, pending-only, so the two agree.

`_asks()` is gone with it, and with it one DB query per page load. Nothing is
lost: `client_portal/asks/service.py::list_asks` covers strictly more —
revoked-share re-check, unreadable-roster fail-closed, expiry, and the
`context`-never-forwarded rule. The one behavioural difference is deliberate and
was already live: `_asks` excluded `alert` items while `/asks` includes them, so
the page already showed them through `<PortalAsks>`; only the duplicate hid
them.

### The Overview row is unconditional, and asks moved below it (#2169)

#2161 put asks and recent work in a grid whose column count was **bound to
`asks.length`** (`:class="{ 'lg:grid-cols-2': asks.length }"`), with the chart
alone above in a `max-w-2xl` section. That made the page's shape a function of
its data: an agent with nothing waiting collapsed to one column, so the layout
changed whenever a transient operator-queue item opened or closed.

The top row is now **unconditional** and its occupants are the chart (left) and
recent work (right); asks sit only when there are any, still with no empty-state
placeholder — an agent with nothing waiting must not advertise the section.

> **#2169's "below the top row" no longer describes the page (#2449), and this
> is worth a decision rather than a silent drift.** ent#428 mounted
> `<PortalAsks>` immediately under the header, ABOVE the stats strip, so the
> page led with asks *and* repeated them lower down. The two ordering tests kept
> passing only because the lower copy existed. #2449 removed the duplicate, so
> the surviving mount is the one near the top and asks now lead the page —
> reversing the instruction #2169 recorded. Deleting a duplicate is not the
> right place to settle where asks belong: if "below the top row" still stands,
> move the surviving mount, do not restore a second one. Nothing has to collapse, because both
row occupants already own an empty state ("No activity in this window." /
"Nothing yet."). The chart's `max-w-2xl` went with the move: inside a half-width
column that cap does not bind below ~1656px.

**The split is `xl`, not `lg`, and that is arithmetic.** The Workspace holds a
288px sidebar plus page padding and the 24px gap, so at 1024px each column is
332px — where the 30-day x-axis (one truncating 9px label per day) reads as
nothing and a nine-bucket legend wraps three to five lines. At 1280px the column
is ~460px. A layout that technically satisfies "two columns" while making the
left one unreadable fails the purpose.

**Known residual, accepted:** the 30-day window's x-axis is ellipsis-clipped in a
half-width column. Measured over a 1280→2000 sweep (Chromium, macOS system
font — glyph widths are platform-dependent, so treat the band, not the counts, as
the finding): all six ticks clipped at 1280–1320, four at 1360–1520, one at
1600–1640, **none from ~1680 up**. It is bounded on the other side too — below
1280 the row stacks and the chart is full width, which is *wider* than the
pre-#2169 `max-w-2xl` cap, so it is clean there as well. So the affected band is
roughly **1280–1680 on the 30-day window only**: 7d (the default) and 14d measure
zero clipped ticks at every width, and the clipping is ellipsis-marked rather
than silently wrong — the bars, the legend totals, and the hover tooltip's full
date are all unaffected. The fix is width-responsive tick density inside
`StackedBarChart.vue::showLabel`, which the operator Overview also consumes —
out of scope here, deliberately.

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
| Store | `stores/clientPortal.js` | `fetchAgentPage`, `fetchAgentReports`, `fetchAgentReport`; the whole Reports orchestration + generation guard (#2162) |
| Service | `client_portal/agent_page.py` | `_window_rows` + `report_detail(rows_offset, rows_limit)` (#2162) |
| Service | `client_portal/agent_page.py` | `_CLIENT_HIDDEN_TRIGGERS` / `_CLIENT_HIDDEN_BUCKETS`; `is_platform` threaded through `_recent_work`, `_stats`, `_last_active` (#2423) |
| DB | `db/schedules/executions.py` | `get_agent_executions_summary(..., exclude_triggers=)` — the `WHERE` that must precede the `LIMIT` (#2423) |
| DB | `database.py` | facade passthrough for `exclude_triggers` (#2423) |
| Router | `client_portal/router.py` | forwards `principal.is_platform` into the page build (#2423) |
| Router | `client_portal/router.py` | `rows_offset`/`rows_limit` on the existing detail route, rate-limited (#2162) |
| UI | `components/reports/ReportSummary.vue` | **new** — the CLIENT-FACING human-readable fallback; no raw escape hatch (#2162) |
| UI | `components/reports/reportSummary.js` | **new** — the bounded, redacting summariser (pure) (#2162) |
| UI | `components/reports/ReportRenderer.vue` | `fallbackComponent` override, default `ReportJson` (operator unchanged); `shapeOk` untouched (#2162) |
| UI | `components/reports/{ReportTable,ReportKpiTiles}.vue` | dark ink pair on meta text — AC #4 (#2162) |
| UI | `components/reports/ReportTimeline.vue` | `bg-blue-500` → `bg-status-info-500` (#2162) |
| UI | `utils/reportPaging.js` | **new** — the one frontend page-size constant, shared with `stores/reports.js` (#2162) |

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

`tests/unit/test_2162_portal_report_window.py` — the row window: `rows_limit`
absent returns today's payload unchanged, a tabular payload windows with a TRUE
total, a non-tabular one comes back whole with no `row_meta` (never a 400), the
offset/limit clamps, and the inherited gate — foreign and missing ids stay
indistinguishable 404s through the windowed path. Plus the route wiring: both
params optional, bounded by the shared `REPORT_ROWS_PAGE_MAX`, rate-limited
*after* the roster gate, and resolvable by FastAPI under postponed annotations.

`src/frontend/tests/unit/reportSummary.spec.js` — the fallback summariser. The
two that define it are negatives: no output path ever serialises the payload
(behaviourally, and by scanning the module source), and a credential-shaped token
is redacted **at value level** — as a whole value, embedded mid-string under an
innocuous key, and past the truncation point. Redaction runs before both
truncation and key humanisation; the humanisation ordering was caught by its own
test, since rewriting `_` to a space destroys the very shape every pattern keys
on.

`src/frontend/tests/unit/portalReportsStore.spec.js` — the store contract against
a mocked axios (`fleetGridFailuresFetch.spec.js` shape). The agent-switch race is
the one that matters: agent A's list, failure, payload and load-more page each
resolve *after* a switch to B and must all be discarded, with B left NOT marked
loaded. Plus the load-more terminal guard, windowed-vs-whole (`row_meta` present
is the only paging signal), and that a failed fetch never lands in the payload map.

`src/frontend/tests/unit/portalReportsRendering.spec.js` — the wiring no unit test
can reach: the tab holds no `<pre>` and no serialiser, mounts the shared
`ReportRenderer`, passes `:fallback-component`, and adds no second scroll axis.
Guards the **mechanism** rather than the spelling — a prop declared and never used
would pass a call-site scan — and re-asserts the five CI-pinned `payload.X` keys
are still inside `ReportRenderer.vue`, the file `test_1535` regexes them out of.

`tests/unit/test_2423_client_loop_visibility.py` — the projection: a client sees
no loop row and no `Loops` chart bucket, an operator sees both, and the CLIENT
view is the default so a caller that forgets to say who is looking leaks least.
Every stub models SQL faithfully (`WHERE` then `LIMIT`) via one shared
`_sql_like` helper — a stub that limits first and filters second is the bug under
test and would pass against the broken implementation.

`tests/unit/test_2423_executions_summary_exclude.py` — the accessor against a
REAL SQLite through the real engine, because the file above can only prove
`_recent_work` *asks* for the exclusion; no Python stub can prove the `WHERE`
actually precedes the `LIMIT`, and that ordering is the whole fix. The
load-bearing case inserts 100 loop rows — not a pathological fixture, exactly one
loop at `MAX_RUNS_LIMIT` — and it fails when the filter is moved back after the
limit (verified by mutation). Plus: the limit still bounds, the NEWEST surviving
rows are returned rather than the oldest, a NULL trigger is not treated as
hidden, multiple excluded triggers work, the agent scope is not widened, and
every existing caller passing nothing sees exactly what it saw.

`src/frontend/tests/unit/workspaceRoomsGate.spec.js` — F24 was **rewritten**, not
deleted. It used to require each exit function to contain `route.params.roomId`;
after #2161 that would mandate the enumeration that *was* the defect, so it now
requires the shared escape and asserts the enumeration is gone.

## Known Limitations

| Limitation | Detail |
|---|---|
| **Asks are read-only** | Answering writes to the operator queue, an operator surface with its own auth. Rather than render a control that 403s for a client, the card offers "Reply in chat →". |
| **Files tab is a list, not the panel** | It lists documents and uploads; the upload flow stays in the existing files panel. |
| **Non-scheduled rows still carry no context** (#2161) | `schedule_name` answers for schedule- and webhook-backed work. A chat, loop or reminder row has no equivalent safe label, and its message is a prompt — so those rows keep trigger, duration and time. AC #3 is met for scheduled rows only. |
| **"What it can do" is the briefing** | A projection of the roster briefing (#138/ent#380). ent#178 (unified exposable-skills config) is the mechanism this becomes a view of; it deliberately does not build a competing one. |
