# Feature: Workspace sidebar IA — agents block, starred chats, unread badges

> **Status**: ✅ Implemented (2026-08-12)
> **Issue**: abilityai/trinity-enterprise#359
> **Requirement**: `docs/memory/requirements/core-agent.md` §5.10
> **Related**: [workspace-absorbs-session.md](workspace-absorbs-session.md) (why the roster's role changed) · [workspace-roster-briefing.md](workspace-roster-briefing.md) (#2163 — the stage and briefing carry the standard scanline motion; the sidebar's own `animate-pulse` roster skeleton stays on #1921's sweep)

## Overview

The sidebar had four things stacked in one column — New chat, Search, an Agents
roster, date-grouped history — all rendered with the same weight. That was
coherent while an agent was a *menu entry you pick to start a chat*. Once the
Workspace absorbed the Session surface (ent#358) and became the only place a
continuing conversation lives, an agent became a **destination** and a chat
became the record of visiting one. Two kinds of thing, one visual treatment.

This change separates them, and adds the two pieces of state that make a list of
conversations navigable rather than merely complete: **which ones you keep coming
back to**, and **which ones are waiting on you**.

## The thing that decided the design

Both new features are *per-viewer*. That single word rules out the obvious
implementation.

A star is one person's bookmark. A room (`shared_sessions`) has several
participants. A `starred` column on the chat row would therefore render one
participant's star in every other participant's sidebar — and incidentally tell
them which conversations their colleagues care about. Rooms also live in the
private enterprise submodule while threads live in OSS, so a per-kind column
would put half of one feature in each repo.

So: one table, keyed by the viewer.

```sql
enterprise_portal_chat_state (
  client_email, chat_kind, chat_id,   -- PK
  starred_at, last_read_at, updated_at
)
```

`client_email` is the primary-key prefix, so the row **is** the tenant scope —
there is no filter to forget on a read path and no way to address another
viewer's state. `chat_kind` (`thread` | `room`) is needed because the two id
spaces are independent: thread `x` and room `x` are different chats.

## Flow

### 0. Loading the list (#2198)

`GET /api/enterprise/client-portal/sessions` returns **every thread the viewer
has, across every agent on their roster, in one call.**

It replaced a literal N+1. The sidebar renders a merged, cross-agent,
recency-sorted list, so `clientPortal.fetchAllSessions()` asked the per-agent
`/agents/{name}/sessions` route once per rostered agent — and it does that from
all **six** `refreshThreads()` call sites, including every thread open and every
completed turn. Each of those calls cost 2–3 DB queries server-side, because
`list_sessions` re-resolves the roster through `agent_on_roster` before it
touches the session table. The batch resolves the roster once and issues one
session query.

Three things about it are load-bearing:

- **The roster set IS the tenant scope.** `agent_name IN (:agents)`, populated
  strictly from `roster_agent_names(email, include_owned)` — the same set
  `agent_on_roster` enforces per agent, extracted so the two cannot drift.
  Filtering on `client_email` alone would re-surface threads for an agent that
  was un-shared, which the per-agent gate hides. `include_owned` is
  `principal.is_platform`: a platform session sees agents it owns (ent#357), an
  external client sees exactly what was shared with them.
- **It must not become a single point of failure.** The fan-out it replaced
  *could not reject* — every per-agent call had its own `catch { return [] }`,
  so "one down agent never blanks the whole list" — and `refreshThreads`
  `Promise.all`s it while only `fetchChatState` was caught. One request inverts
  that: a single 500 would blank a populated sidebar and, worse, reject out of
  `bootstrap()` before `resolveAgentQuery()`, breaking Workspace deep-link
  landing entirely. So the store catches internally and returns its **last good
  list** (never `[]`), raising `sessionsFailed`; `refreshThreads` catches both
  halves as a belt.
- **The rows are filtered client-side to the displayed roster.** The backend
  scope is the access boundary; this narrower filter is a rendering rule, since
  a thread whose agent the sidebar does not show would route nowhere. Today the
  two sets are identical, so it is a no-op — and it keeps the sidebar correct
  whatever #2196 decides about hiding container-less agents.

No cap and no `total`: the per-agent route is unbounded and ran N times, so this
ships the same row volume in one request. A cap would be a *new* behaviour that
collides with §5.10's starred-chat pinning guarantee (a pure recency `LIMIT` can
drop a starred-but-old thread out of the pinned section) — filed separately.

Rate-limited per viewer (`portal_sessions_all:{email}`), because this becomes
the hottest authenticated read in the Workspace and is no longer even
incidentally throttled by the browser's per-host connection cap (and in
production, behind cloudflared/HTTP-2, there is no such cap at all).

**Human-only for platform principals (#2198 E7).** `get_portal_principal`'s
platform branch now runs `reject_agent_principal`: an agent-scoped MCP key
resolves to its owner *carrying the owner's role* (the ent#293/#297 trap), so
before this any agent's injected `TRINITY_MCP_API_KEY` reached every portal
route as the owner — a REST path around the MCP layer's agent-to-agent
permission matrix — and the batch route would have turned "N calls against N
discovered names" into one call returning the owner's whole thread index.
Enforced at the dependency so every current and future portal route inherits
it; user-scoped keys, `scope='system'` and portal session tokens pass exactly
as before.

*Behaviour change worth naming:* one unreadable agent used to degrade alone;
with one query the read is all-or-nothing. Acceptable — a single indexed read,
not N agent round-trips — but it is a real change in failure granularity.

*Transitional:* a **404** falls back to the per-agent fan-out for the
deploy-skew window (a cached bundle against a backend without the route). 404
only — a 5xx already degrades correctly, and fanning out there would turn one
failure into N. Delete one release after this ships.

### 1. Reading state

`GET /chat-state` returns one entry per chat the viewer has state for:
`{kind, id, starred, unread}`. One call rather than a field on each list,
because threads and rooms come from **different endpoints** (and different repos)
but sort into a single list — attaching state per-list would reshuffle the
sidebar as the second response landed.

The shell merges it onto the thread list in `decorate()`; a chat-state failure
costs the stars and badges, never the list.

### 2. What "unread" means

Agent messages newer than that thread's `last_read_at`.

The subtle half is the **absent** cursor. A thread with no `last_read_at`
reports **nothing** unread, rather than its whole history. Unread is defined
relative to a cursor; inventing one at the beginning of time would have lit up
every historical conversation in every install the day this shipped — noise that
teaches people to ignore the badge, which is worse than having no badge.

A cursor is written the first time the viewer opens or sends in a thread
(`openThread`, `openRoom`, `onSessionAdopted`), so any live conversation
acquires one immediately and the first reply the viewer *doesn't* see is the
first thing that badges.

**ent#557 amended the absent-cursor half, and only that half.** The rule above
is right about conversations the VIEWER starts and wrong about the ones an agent
starts. ent#523 made Main the landing place for everything an agent initiates —
agent-initiated messages, asks raised outside a chat, scheduled briefs all
resolve to it — and a freshly minted Main has never been read by anyone. So the
single case an unread badge exists for produced no badge anywhere: the agent
replied, and nothing indicated it.

A thread with no cursor now counts agent messages newer than the viewer's
**account baseline**. Read as: *anything an agent has said to you since the
first time you read anything here, in a chat you have never opened.* A chat
that existed before that instant and was never opened still reports nothing —
the conservative direction, and the property the original rule was written for.

The baseline is a **stored, write-once row**, not a value derived from the
cursors that happen to exist. That is the whole of the design, and it is worth
saying why, because both obvious derivations were tried and both fail the same
test:

| derivation | reads as | why it is wrong |
|---|---|---|
| `MAX(last_read_at)` | "since you were last here" | It advances every time the viewer reads anything, so a reply sitting unread in a chat they have not opened is **silently cleared by reading a different chat**. |
| `MIN(last_read_at)` | "since the first time you read anything" | Looks stable and is not: `mark_chat_read` UPDATES the row it advances, so a viewer with one chat has MIN == MAX and inherits the identical bug. |

Only a value nothing updates is stable, so it is stored: a row in
`enterprise_portal_chat_state` under the reserved kind `account` / id
`baseline`, written on the viewer's first ever `mark_chat_read` and never moved.
Every read of that table excludes it — `get_chat_state` so it never reaches the
sidebar payload as a phantom chat, `count_chat_state_rows` so it cannot spend
one of the viewer's capped rows on bookkeeping they did not ask for.

A viewer with no baseline row — a first-ever sign-in — has no baseline, so the
subquery is NULL, so the comparison is NULL, so nothing counts. ent#359's
property is preserved rather than traded away, and it falls out of SQL's NULL
semantics rather than a second branch.

**The payload is built from two passes, and the second one is the feature.**
`service.get_chat_state` iterates the viewer's state ROWS and reads the unread
map off them — which reaches every chat that has a cursor and, by construction,
none of the chats ent#557 exists for: a never-opened Main has no row. Since the
SQL now LEFT JOINs, `count_unread_by_session` is the WIDER of the two sets, so a
second pass emits its remaining thread ids as `starred: false` entries. Without
it the SQL is correct and nothing on screen changes — no badge, no per-agent
pill, no wordmark total, no tab title. The pass is bounded by the same read
(scoped to the caller's own `enterprise_portal_messages`), de-duplicated against
the ids the first pass already emitted so one chat cannot be counted twice in
the wordmark total, and the account baseline is excluded one layer down so it
can never surface as a phantom chat.

**And only while the badge is CLEARABLE.** `mark_chat_read` silently no-ops when
the row would be a new one and the viewer is at `MAX_CHAT_STATE_ROWS`, on the
stated ground that a read marker is "incidental to what the user asked for". A
cursorless thread is by definition a new row, so ent#557 made that no-op
load-bearing: before it a cursorless thread showed nothing and the no-op was
invisible; after it a capped viewer would get a badge on the wordmark, the agent
pill *and* the browser tab title that opening the chat cannot dismiss. So the
second pass is gated on there being room, and a capped viewer degrades to
ent#359's behaviour — the state they were in before this feature — rather than
to a stuck badge. The gate is read-side only and applies to the cursorless pass
alone: a thread that already has a row can always be marked read, so capping its
badge would hide unread the viewer can perfectly well clear. It fails OPEN (an
unreadable count reports room — the write path is what enforces the cap) and the
COUNT is paid only when there is something to emit, which is never on the
ordinary load.

**Liveness.** `refreshThreads()` is event-driven — a send, a navigation, a turn
finishing — so before ent#557 an agent-initiated reply reached the sidebar on
the viewer's next action and not before, which is the same as never for someone
in another tab. It now also runs on the ent#364 asks poll (20 s,
visibility-aware). Folded into that timer rather than given its own: the
Workspace has no WebSocket a portal client is on (`operator_queue_new` is
broadcast on the platform `/ws`), and a second cadence for one badge is a second
thing to reason about. Since #2198 the thread half is ONE request for every
agent rather than one per agent, which is what makes it cheap enough to ride
there.

**The browser tab.** While anything is unread the tab title carries a compact
`(3) ` prefix; it returns to the plain title when everything is read, and is
cleared when the Workspace unmounts — the count would otherwise outlive the only
surface that can explain it. `utils/tabTitle.js` owns it, and the reason it is a
module rather than two lines is that `document.title` has **two** writers: the
router sets a label on every navigation (#1418) and the count changes on its own
schedule. With both writing directly the last one to fire would erase the
other's half. Neither writes it now; both call in, the module renders the whole
string. The count is a prefix on whatever the router computed — never a
replacement — which is what makes it compose with ent#556's branding work. It
caps at `99+` like the sidebar (a browser truncates a tab to a few characters,
and it truncates from the RIGHT, which is also why the marker leads), and zero
renders nothing rather than a `(0)`.

### 3. Where the counts surface

| Count | Where | Why there |
|---|---|---|
| per chat | the chat row | which conversation to open |
| per agent | the agent row in the agents block | which agent is waiting |
| total | the **wordmark** | the agents block now occupies the top of a *scrolling* region, so a fleet-wide signal parked there scrolls away |

A room credits its unread to **every** agent in it — there is no single agent a
room is "with", so if three agents share a room you are behind on, all three
rows should say so. (Rooms report 0 today; see Known Limitations.)

### 3b. Availability on the agent row (#2196)

Each roster card carries `availability` (`ready`/`stopped`/`unavailable`/
`unknown`). The row renders a chip for the two non-ready states via the pure
`portalUtils.availabilityChip()` — one rule, shared with the agent page and (in
PR 2) the picker and the `@`-typeahead, so four surfaces cannot drift on it.

Three decisions worth keeping:

* **Label, do not disable.** Disabling the row would relocate the dead state
  rather than remove it: a client whose agents are all *stopped* — a routine
  resource-saving posture — would get an entirely inert Workspace. The chip sets
  the expectation and the server's 502 stays the honest refusal.
* **No re-sorting.** Ordering by availability would make rows jump as agents
  start and stop; the design system's layout-stability rule forbids that, and the
  top-5 fold is #2159's design.
* **Reserve the chip's footprint**, so a row does not reflow when an agent's
  state changes between refreshes — but **for the list, not for every row**
  (#2641). `availabilityChip()` answers null for everything except `stopped` and
  `unavailable`, so on a fleet where everything is running — the normal case —
  the 72px strip rendered empty on *every* row. That produced two visible
  defects from one fact: the dates stopped 72px short of the row's right edge,
  and 72px per row came out of the only element that wanted it, so names
  truncated (`Chief ...`) beside a blank strip. `reservesAvailabilitySlot(rows)`
  now decides it once per render, over the rows actually **rendered** — a
  stopped agent hidden by search or the collapse limit must not reserve width on
  a list that shows no chip. The whole `<span>` goes when nothing reserves it,
  not just its width: a zero-width flex child still sits between the date and
  the edge, and the row's `gap-2.5` would keep paying 10px for it.

  Two properties survive the change and one cost is accepted. Uniform down the
  list, so #2580's identical truncation point holds (a per-row reservation would
  give a stopped row a different name width from its neighbours). Nothing moves
  within a populated list — a second agent stopping, or one restarting while
  another is still stopped, changes only that row's chip. The cost is the 0→1
  transition: the first agent in view stopping reflows the list once, where
  before it reflowed nothing. That is the honest price of not charging every row
  for the empty case.

`agentRowTitle()` carries the same reason in the row's `title`, so the state is
reachable without relying on colour.

### 4. Starred chats are lifted, not copied

`partitionStarred()` splits the list before `groupThreadsByDate()` ever sees it,
so a starred chat appears in the Starred section and **nowhere else**. Copying it
above the groups would make the list lie about how many conversations exist,
and both rows would go to the same place — the duplicate carries no information.

### 5. Clicking an agent

With unread, the agent row opens the conversation it is waiting in. With nothing
unread it starts a new chat, exactly as before.

A badge reading "2 replies" next to a control that opens an *empty* chat is a
contradiction: the count is the reason the user clicked. This is **not** a
stand-in for the agent page — that is ent#360, and it is a different thing (a
destination with its own content, not a shortcut to a conversation).

## Files

| Layer | File | Change |
|---|---|---|
| Schema | `db/schema.py`, `db/tables.py`, `db/migrations.py`, `migrations/versions/0038_portal_chat_state.py` | one table, four tracks (Invariant #3) |
| DB | `client_portal/db.py` | state accessors + the unread aggregate; `list_portal_sessions_for_agents` (#2198, chunked at 500 for the SQLite variable ceiling) |
| Service | `client_portal/service.py` | kind/id validation, row cap, star + read + combined read; `roster_agent_names` + `list_all_sessions` (#2198) |
| Router | `client_portal/router.py` | 4 endpoints + `GET /sessions` (#2198) |
| Store | `stores/clientPortal.js` | `fetchChatState`, `setChatStar`, `markChatRead`; `fetchSessionsBatch` + last-good-list resilience (#2198) |
| Shell | `views/Portal.vue` | merge state onto threads, optimistic star, mark-read on open; `refreshThreads` catches both halves (#2198) |
| UI | `components/portal/PortalSidebar.vue` | agents block, starred section, wordmark badge |
| UI | `components/portal/PortalChatRow.vue` | **new** — one row shared by both sections |
| UI | `components/portal/PortalStarButton.vue` | **new** — one star for its three homes |
| UI | `components/portal/PortalConversation.vue`, `PortalRoom.vue` | header star |
| Utils | `components/portal/portalUtils.js` | `partitionStarred`, `unreadByAgent`, `totalUnread`, `rowAgents` |

## Security notes

- **No roster gate on the three chat-state routes.** Every row is keyed by the
  caller's own email; there is no agent to authorize against.
- **No existence check on `chat_id`, on purpose.** A 404 for an unknown chat
  would answer "does chat X exist?" for every id in the install (invariant #8).
  The write lands in the caller's own namespace, so an unknown id gains them
  nothing — per-viewer caps bound the write instead of validation.

  **Two caps, because one number cannot do both jobs.** `MAX_CHAT_STATE_ROWS`
  (1000) bounds abuse: any row, star or read cursor. `MAX_STARRED_CHATS` (200)
  bounds the starred set and is the only one a real user reaches. They have to
  be separate — a read cursor is written for every chat ever opened, so a single
  total cap gets consumed by ordinary use, and then every star returns
  *"unstar some first"* while unstarring frees nothing, forever. Counting only
  starred rows makes that advice true, and unstarring a chat with no read cursor
  deletes its row outright so the table cannot only ever grow.

## Search filters agents too (trinity-enterprise#402)

The search box sits directly above the agents block, and typing **unmounted
that block**: `isSearching` (≥2 chars) swapped the whole steady state for chat
results. So the one control in front of the roster did nothing to it, and on a
fleet larger than the collapsed window there was no way to reach an agent by
name from the surface built to list them.

Now only the CHAT half swaps. The agents section stays mounted in both modes —
which also keeps the sidebar's footprint stable across the first two keystrokes
— and its rows narrow to the matches.

### One matching rule, two rules inside it

`searchAgents(roster, query, { askCounts, expanded })` in `portalUtils.js`
returns `{ items, visible, total, hidden }`. Both of the things that are easy to
get wrong live INSIDE it, so a caller cannot forget either:

- **`requireMentionable: false`.** `filterAgentCandidates` (ent#392) defaults it
  TRUE for the composer, where an un-mentionable pick is a dead end. A sidebar
  row is not a mention — `data.scout` opens perfectly well — so inheriting that
  default would hide a real agent from a search for its own name.
- **The window is `visibleAgentRows`**, the #2424 rule the steady state already
  uses, not a fresh `boundCandidates` slice. A rank-ordered slice can drop an
  agent with an open ask past the window — exactly the failure #2424 fixed for
  the collapsed list, which a second bounding rule would quietly reintroduce for
  search. An ask-bearing match is never collapsed out of its own result.

### One row, one toggle, five preserved identifiers

The agent row is written **once** and mode-switched through `shownAgents`, so
its badges, availability chip, title and `open-agent` emit are inherited by
search rather than copied into it. Extracting a `PortalAgentRow.vue` was
rejected: `PortalChatRow` was extracted (#2149) because the same row rendered
from THREE places, and here it renders from one in both modes — the precedent's
trigger is not met, and the extraction would have broken five live source pins
in `portalRosterRow.spec.js` and `portalAvailabilityChip.spec.js` for no
drift benefit.

The "Show all" control stays the ONE persistent `<button>` (#2159: alternating
two `v-if` buttons drops keyboard focus). Its `v-if` is unchanged; a `v-show`
is what varies, because while searching it is only meaningful when something is
actually hidden — and the label states the list it is actually bounding
(`Show all (12)` vs `Show all (12 matches)`).

### Honest states, per section

`sidebarSearchState` → `roster-loading | searching | both | agents-only |
chats-only | none`, and `searchEmptyLines` renders **per-section lines, never a
combined sentence**:

| State | Agents line | Chats line | Hint |
|---|---|---|---|
| `roster-loading` | — (skeleton stays) | — | — |
| `searching` | `No agents match.` **iff** the filter matched nothing | `Searching chats…` | — |
| `agents-only` | — | `No chats match.` | — |
| `chats-only` | `No agents match.` | — | — |
| `none` | `No agents match.` | `No chats match.` | `Try another word, or clear the search.` |

The `searching` row is why `searchEmptyLines` takes an explicit `agentsEmpty`
rather than reading the agents line off the state alone. `Portal.vue` sets its
`searching` flag on **every keystroke** and clears it only when the request
settles, so that state covers the whole time someone is typing — and the agent
half is a client-side filter that already knows its answer. Deriving the line
from the state alone left the agents section with a header, no rows and no
sentence for that entire window. `chats-only` and `none` already MEAN no agent
matched, so the flag only supplies the arm the state cannot express;
`roster-loading` still outranks it.

Two properties this table exists to guarantee: "nothing matched at all" is
distinguishable from "agents matched, no chats", and neither line ever stands in
for the other. A single combined "Nothing matches" would **over-claim** — the
chat half is a server request, the agent half a filter over a roster already in
hand, and they can fail in different ways at the same time.

`roster-loading` wins outright because loading is not empty: a two-character
query typed while the roster is in flight must not read "No agents match." over
a roster that has not arrived.

With an EMPTY roster the agents line is suppressed and ent#357's next action
("No agents shared with you yet — ask whoever invited you") stands, because that
is the truer sentence; printing both would state one absence twice.

### Known limitation

A **failed** chat search is swallowed into `[]` by `views/Portal.vue`, so it
currently reads as "No chats match." — a wrong answer rather than a missing one.
Fixing it is a change to that view (Lane B territory here) and is tracked
separately; the wording above never claims more than the per-section facts it
can actually see.

## Tests

| Test | Pins |
|---|---|
| `tests/unit/test_ent557_unread_never_opened_chat.py` | ent#557: the agent-started Main case; a first-ever sign-in still counting nothing; a chat predating the baseline not retroactively unread; **reading one chat not clearing another** (the case that rejects both derived baselines); the baseline frozen at the first read; the baseline row invisible to the sidebar and both caps; cross-viewer isolation of the baseline subquery |
| `src/frontend/tests/unit/portalUnreadTabTitle.spec.js` | ent#557: the marker's format executed (prefix, cap, zero, non-numbers, empty base) and the two-writer ordering — a navigation keeps the count, a count change keeps the label |
| `src/frontend/tests/unit/portalUnreadLiveness.spec.js` | ent#557: the poll refreshes threads, stays visibility-aware and adds no second timer; the tab reads the same total the rows do; asks and unread stay separate |
| `tests/unit/test_ent557_unread_never_opened_chat.py` (service section) | ent#557: the db→service boundary — a cursorless thread reaches the API payload, a chat with a row is emitted once, a star survives gaining a count, the baseline is never a chat, and a first-ever viewer still gets nothing |
| `tests/unit/test_ent359_portal_chat_state.py` | cross-viewer isolation; kind/id key separation; email-case normalisation; no-cursor ⇒ nothing unread; only agent messages after the cursor count; re-reading clears; star and read don't overwrite each other; unstar keeps the cursor; validation; unknown ids don't 404; the cap bounds new rows but never freezes owned ones; mark-read at the cap is a no-op |
| `src/frontend/tests/unit/portalSidebarDateFlushRight.spec.js` | #2641: the list-level reservation as a pure table (all-running reserves nothing; one stopped agent reserves for all; derived from `availabilityChip` rather than re-listing the states), that the template removes the ELEMENT rather than its width and computes over the rendered rows, and that #2580's date column is still fixed, right-aligned, `tabular-nums` and unconditional |
| `src/frontend/tests/unit/portalSidebarIA.spec.js` | starred lifted out of every date group and appearing once; per-agent sums; a room crediting every participant; wordmark total; row-avatar cap and overflow |
| `src/frontend/tests/unit/portalUndefinedCalls.spec.js` | (existing guard) the two new SFCs call nothing undefined |

## Known Limitations

| Limitation | Detail |
|---|---|
| **Rooms report `unread: 0`** | A room already has its own seq cursor (`since`), which is a different model from a timestamp cursor. Stars work for rooms; unread does not, so a room never badges. Reconciling the two is follow-up work. |
| ~~**Unread needs one open first**~~ | **Fixed by ent#557.** A never-opened thread now counts agent messages newer than the viewer's stored account baseline. What remains is narrower and deliberate: a viewer who has never read *anything* has no baseline and sees no badge, and a chat that predates their baseline and was never opened still reports nothing. |
| **The agent badge counts replies, not questions** | "Waiting on the user" is read here as "the agent replied and you haven't read it". An agent blocked on an operator-queue approval is a different signal and is not surfaced here — that queue belongs to operators, and a Workspace viewer may be an external client with no standing in it. |
| **Optimistic star, no cross-tab sync** | A star toggled in one tab does not appear in another until its next `refreshThreads`. |
