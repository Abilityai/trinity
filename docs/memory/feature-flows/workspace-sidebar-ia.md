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
  state changes between refreshes.

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

## Tests

| Test | Pins |
|---|---|
| `tests/unit/test_ent359_portal_chat_state.py` | cross-viewer isolation; kind/id key separation; email-case normalisation; no-cursor ⇒ nothing unread; only agent messages after the cursor count; re-reading clears; star and read don't overwrite each other; unstar keeps the cursor; validation; unknown ids don't 404; the cap bounds new rows but never freezes owned ones; mark-read at the cap is a no-op |
| `src/frontend/tests/unit/portalSidebarIA.spec.js` | starred lifted out of every date group and appearing once; per-agent sums; a room crediting every participant; wordmark total; row-avatar cap and overflow |
| `src/frontend/tests/unit/portalUndefinedCalls.spec.js` | (existing guard) the two new SFCs call nothing undefined |

## Known Limitations

| Limitation | Detail |
|---|---|
| **Rooms report `unread: 0`** | A room already has its own seq cursor (`since`), which is a different model from a timestamp cursor. Stars work for rooms; unread does not, so a room never badges. Reconciling the two is follow-up work. |
| **Unread needs one open first** | A thread the viewer has never opened *since this shipped* has no cursor and so never badges, by design (see above). It acquires one the first time they open or send. |
| **The agent badge counts replies, not questions** | "Waiting on the user" is read here as "the agent replied and you haven't read it". An agent blocked on an operator-queue approval is a different signal and is not surfaced here — that queue belongs to operators, and a Workspace viewer may be an external client with no standing in it. |
| **Optimistic star, no cross-tab sync** | A star toggled in one tab does not appear in another until its next `refreshThreads`. |
