# Workspace — agents at the centre (trinity-enterprise#523, trinity-enterprise#524)

**Requirements**: `docs/memory/requirements/core-agent.md` §5.23
**Design**: board **A3** (A1–A6), artifact `6192c90d-eb4a-49f8-90aa-0ff5e30ba8e5`, approved 2026-09-06
**Related flows**: [workspace-rail.md](workspace-rail.md) (ent#474) ·
[workspace-chat-tabs-and-titles.md](workspace-chat-tabs-and-titles.md) (ent#451/#473) ·
[workspace-work.md](workspace-work.md) (ent#525) · [workspace-agent-page.md](workspace-agent-page.md) (the page this dismantles)

---

## The problem

The Workspace was built agent-page-first. Clicking an agent opened a *report*
about it and the conversation sat one click further behind a **Start a chat**
button. ent#360 had good reasons — an agent had no home, nowhere to see what it
had done, nowhere for it to ask you something — and none of those are reverted
here. What changes is that the page stops being a **stop** on the way to the
conversation.

Two things were also missing outright: a durable place for the agent to reach
you when no conversation named itself, and any way to drop a file on the one
surface people actually work in.

## The model

- **Agents are the central entity.** A sidebar row is the agent; clicking it
  opens the chat you were last in.
- **Main** is the pinned chat every `(user, agent)` pair has. It is where an
  agent-initiated message, an ask raised outside a chat and a scheduled brief
  land unless the row names another session.
- **Reset** archives Main and starts the agent cold. Nothing is lost, so there
  is no confirmation.
- **One page type**: conversation in the centre, the agent's numbers in a band
  above it, the agent's context on demand in the rail's place.

---

## Backend

### Schema

`enterprise_portal_sessions` gains two columns and one index (both migration
tracks, Invariant #3 — SQLite `portal_session_main_chat`, Alembic `0053`):

```sql
is_main     INTEGER NOT NULL DEFAULT 0   -- the pinned chat
archived_at TEXT                         -- the one Reset retired; NULL = live

CREATE UNIQUE INDEX idx_portal_sessions_main
  ON enterprise_portal_sessions(agent_name, client_email) WHERE is_main = 1;
```

The index is **the invariant, not an optimisation**. `ensure_main_session` is
reachable from two request paths and runs in every uvicorn worker, so a
check-then-insert races two Mains into existence for one pair — after which
"the pinned first tab" has no single answer. The loser of that race catches
`IntegrityError` and re-reads the winner's row.

`WHERE is_main = 1` is equally load-bearing: an archived row keeps its
`(agent_name, client_email)` pair forever, so an unconditional unique index
would refuse the **second** Reset.

**No backfill.** Backfilling would have to anoint one existing thread per pair,
and "whichever was most recent when we migrated" is not a fact anyone asked
for. Existing rows read `is_main = 0` and gain a Main on the next visit — the
ent#473 `title_source` precedent one revision back.

### Where Main is minted

| Call site | Why |
|---|---|
| `service.list_sessions(agent, email)` | opening an agent is when the pinned tab has to exist, and this is the one per-agent read on that path |
| `service._resolve_session_id(agent, email, None)` | a turn or an ask with no named thread |
| ~~`list_all_sessions`~~ | **deliberately not**: the cross-agent batch (#2198) runs on every sidebar refresh and would write a row per agent the person has never opened |

Ensuring inside `list_sessions` is best-effort — a failure to mint Main must not
blank the chat list the caller asked for.

### Reset

`POST /api/enterprise/client-portal/agents/{agent}/sessions/main/reset`

1. roster gate → uniform 404 (Invariant #8)
2. resolve the live Main
3. **refused while a turn is in flight** → named 409 `turn_in_flight`. Retiring
   the thread mid-turn lands the reply somewhere only a search would find, and
   bills it either way.
4. **an untouched Main is a no-op** → `archived_session_id: null`. Archiving
   anyway mints an empty thread per click and files it under a name nobody
   chose.
5. one transaction: clear `is_main` + stamp `archived_at` (+ name the archive if
   it has no title), then insert the fresh Main. The order is not stylistic —
   the index refuses the insert until the old row's flag is cleared. A
   concurrent Reset that won first yields a named 409 `reset_raced`.
6. one `role = 'system'` message in the **new** Main naming the archive.

**There is no second reset primitive.** A fresh row carries no
`cached_claude_session_id` and `session_turn_service` resumes only on a cached
id, so *cold* is a property of the new row. `routers/sessions.py::
reset_session_memory` stays untouched: clearing a cache and keeping the thread
is a different verb from retiring the thread. The archive **keeps** its cached
id, so it stays resumable as an ordinary past chat and
`session_cleanup_service`'s keep-set (which unions that column) keeps its JSONL.
Per-user memory (MEM-001) is not touched.

An untitled archive is named **and dated** — these accumulate in one list, and
three rows reading "Previous conversation" say nothing about which is which.

### The landing rule

```
_resolve_session_id(agent, email, None)  ->  ensure_main_session(agent, email)
```

One edit, because every homeless turn already funnels through this function:
`ensure_thread_for_ask` (ent#364/#429), a scheduled brief (ent#498), a headless
API turn. "Most recent" was a reasonable guess when there was nowhere
designated; now there is, and a guess would scatter the agent's own messages
across whichever chat the person happened to open last. An explicit
`session_id` still wins.

---

## Frontend

### The dismantle — where each piece went

| `PortalAgentPage.vue` (deleted) | New home |
|---|---|
| stats strip + window selector | `PortalAgentBand.vue` — always visible under the header |
| Activity chart (`StackedBarChart`) | `PortalAgentBand.vue` — the **only** scanline on this page (#2540) |
| "Your chats with X" | `PortalAgentDetails.vue` → Chats (the full list) |
| Reports | `PortalAgentDetails.vue` → Reports (same shared dispatch, same `ReportSummary` client fallback, #2162) |
| "What it can do" | `PortalAgentDetails.vue` → What it can do (pre-fills the composer, never auto-sends — ent#138) |
| Canvas · Files | already rail tabs (ent#475) — removed here, **not lost** |
| Recent work · Activity list | already the rail's Work tab (ent#525) |
| `PortalAsks` | the conversation already mounted the surviving copy; the page's was #2449's second one |
| health + availability | `PortalAgentDetails.vue` header, still two separate facts (#2196) |

Both surfaces read one payload through `composables/usePortalAgentPage.js` —
they are on screen at different times, and each issuing its own fetch would
double every agent page load.

### Agent details is a sibling of the rail

Ruled 2026-09-05. `Portal.vue` renders `PortalAgentDetails` **or** `PortalRail`
in the third column. The rail's state is a setup ref of the view, so the swap
does not touch it and closing details returns the rail on the tab it was
showing. `portalRail.js`'s five-tab set is unchanged — no new tab, no new door.

### Pure rules (`components/portal/portalUtils.js`)

`vitest` runs `environment: 'node'` with no mount harness, so every decidable
rule is a plain function (the ent#392 precedent):

| Function | Rule |
|---|---|
| `agentChatTabs` | Main first, then recency; archived chats are not tabs; Main is labelled by its **role** |
| `landingThread` | most recently active, **Main as the floor**; never an archived chat |
| `resolveAgentLanding` | the `?agent=` deep link, delegating to `landingThread` so there is one answer |
| `orderRosterAgents` | most recent collaboration, then name; `primaryName` is ent#491's seam |
| `agentPreview` | the newest chat's **title** — the sidebar list carries no message content (#2198), so a body preview would reinstate the N+1 |
| `composerAvailabilityNotice` | what the composer says for a stopped/unavailable agent |

### Things that look like details and are not

- **Main is not renameable.** It is the same thread for the life of the pair;
  a derived or typed title would make the pinned tab and the header disagree
  about which chat you are in.
- **An unused Main is filtered from the sidebar only.** It exists for every pair
  the moment an agent is opened; the tab strip must still show it from the first
  visit, so this is a projection for one consumer (`sidebarThreads`), not a
  filter on `threads`.
- **The roster is ordered before the collapse.** Bounding first would sort a
  slice chosen by the old order.
- **The composer labels, never disables.** A client whose agents are all stopped
  (a routine resource-saving posture) would otherwise get an inert Workspace.

---

## Files onto the conversation (ent#524)

`composables/usePortalFileDrop.js` is the ONE implementation — the issue forbids
a second, and the reason is in the defect it fixes: the gesture already existed
on the Files panel, and the `files?.[0]` bug existed there too, because each
surface had written its own.

| Consumer | Destination |
|---|---|
| `PortalConversation.vue` | the agent's inbox |
| `PortalRoom.vue` | **every participating agent's** inbox; the chip names the recipients (operator decision 13) |
| `PortalRailFiles.vue` | the target agent's inbox |

- the whole conversation is the target, with an affordance naming what will
  happen; `isFileDrag` keeps a dragged link or text selection from lighting it
- the overlay is `pointer-events-none` so it cannot swallow the drop it announces
- `dragenter`/`dragleave` are **depth-counted** — they fire per child element, so
  a boolean toggled on leave flickers the affordance off mid-gesture
- one chip per file, each with its own progress and outcome; a refused file names
  itself and the limit; a 429 batch says which files landed and when to retry
- uploads run **sequentially**: twenty parallel requests is the surest way to
  trip the per-email limiter (ent#287) on a gesture that would have succeeded
  spread over a second
- a chip renders from one derived `attachmentState` rather than a bare
  `v-if="uploading"`, which the #1927 ratchet counts and cannot tell apart from
  the fetch-in-flight gate it exists to stop

**Deferred by design**: the working-folder destination (ent#484/#486). This
ships against the existing upload path, and when they land the gesture does not
change — only the caller's `upload`.

---

## Tests

- `tests/unit/test_ent523_main_chat.py` — both migration tracks and the index
  predicate; `ensure_main_session` idempotency and the lost-race path; the
  landing rule through `_resolve_session_id` **and** `ensure_thread_for_ask`;
  Reset's archive/mint/system-line, its cold-by-construction property, the
  in-flight 409, the untouched-Main no-op, repeatability, and the uniform 404
- `src/frontend/tests/unit/portalAgentsAtCentre.spec.js` — every pure rule
  above, the ent#524 drop/batch rules, and the source guards that no unit test
  can reach (which surface mounts what, no `[0]` left anywhere)
- Re-pointed rather than deleted when their subject moved: `portalRatings`,
  `portalReportsRendering`, `portalAvailabilityChip`, `portalAskSingleSource`,
  `portalAgentPageUx`, `portalRail`, `portalLoadingTreatment`,
  `workspaceRoomsGate` F23, `portalRosterRow`, `portalSidebarSearch`

## Not included

Stated so the narrowing is never inferred later from the fact that it merged
(the ent#474 convention):

- the **State** tab (ent#439)
- the working-folder destination for dropped files (ent#484/#486)
- ent#498's brief delivery, which consumes this landing rule but is its own issue
- ent#492's resize handles; the tab strip repacks without them because
  `OverflowTabs` re-measures on `ResizeObserver`
- ent#491's roster ordering (incubating) — a deterministic order ships and
  leaves the seam
