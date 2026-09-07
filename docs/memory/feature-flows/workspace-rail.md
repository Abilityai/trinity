# Workspace conversation rail — the shell (trinity-enterprise#474)

> Slice 1 of #472: the collapsible third column beside the conversation, the
> tab contract every later capability docks into, the collapsed-state activity
> signal, room grouping, persistence, and the mobile forms. The first docked tab
> is **Work**, docked empty by the operator's split — its content is #457's.
> Folded in: #2540, the loading rule this shell was built under (skeletons on
> pages and threads; the scanline is for charts).

## The shape, and why it needs no backend

The rail is a frame. It reads two things the shell already knows — who is in
the conversation and whether this is a platform session — and renders the tabs
that pass their door. Nothing here fetches: the Work tab is empty until #457
docks its executions into the `#tab-work` slot, and every later tab brings its
own store (loops already has one, `stores/portalLoops.js`). No new route, no
new model, no migration, no MCP tool.

```
Portal.vue (shell)
├─ railState        ref(loadRailState(localStorage))  ← setup ref, ONE key, before first paint
├─ railParticipants railParticipantsFor({agentPage, roomId, roomParticipants, activeAgent})
├─ railTabs         visibleTabs(RAIL_TABS, {isPlatform: store.isPlatformSession, participants})   ← THE gate
├─ railVisible      railVisibleFor({agentPage, stageState: stage.state, roomId, roomsAvailable, …})
├─ railSignals      { work: workSignal }   ← reset on [convKey, roomId] change
│
├─ <main>  ── PortalRoom          @participants-changed ─► roomParticipants
│                                 @work-state           ─► workSignal (server `working` list)
│                                 #rail-strip ─► <PortalRailStrip sm:hidden>
│          ── PortalConversation  @work-state           ─► workSignal (in-flight `sending`)
│                                 #rail-strip ─► <PortalRailStrip sm:hidden>
├─ <PortalRail hidden sm:flex>    collapsed (w-12) | open (w-96)     ← sibling of <main>
└─ <PortalRail sheet sm:hidden>   the bottom sheet, when the strip is tapped
```

## Design decisions

### `visibleTabs` is the one gate — for render AND for mount

#472 rule 2 and the design pass say a failed door means the tab is **not
fetched**, not merely hidden. The shell computes `railTabs` once and hands that
list to the column, the strip and the sheet; `PortalRail` never reads the
registry, mounts a body for exactly the active tab, and renders no chrome at
all for an empty list. So a tab's body — the thing that would own a fetch —
cannot exist for a session that fails its door. With Work the only registered
tab, an external client sees no rail today; with the design's four-tab set they
see Canvas · Files (artboard 6). An unknown door fails closed.

### The rail is a sibling of `<main>`, outside every remount

`PortalConversation` is keyed by `convKey` and `PortalRoom` by its id; both
remount on a chat switch. The rail's state is a setup ref of `Portal.vue`, and
the rail element sits beside `<main>` with no key, so open/collapsed and the
active tab survive a switch by construction (AC), and a live update patches the
rail body in place without touching the conversation's scroll or composer
(principle 5). Visibility is keyed on the ROUTE and the stage VERDICT — a room's
participants arrive with its own fetch, and hiding on an empty list would
flicker the rail in and out.

### The signal is derived, never latched

`workSignalFrom({ sending, agent })` runs on every change of the conversation's
in-flight flag — the same flag its `finally` clears on a failed turn — and the
room's version reads the server's `working` list, which survives a reload and
follows the room's 3s poll. Two belts on top: the conversation emits an empty
signal on unmount, and the shell resets the signal on every chat switch. A
stuck "running" would have to survive all three.

### One storage key, as approved

`trinity-workspace-rail` (design pass, "State & honesty"), JSON `{open, tab}`,
normalized on read: an unknown tab falls back to Work, a non-boolean `open`
reads as collapsed. Per-viewer namespacing was considered and not adopted — the
key holds a layout preference, not data, and the approved name is the house
style (`trinity-dashboard-view`).

### Two signal shapes, one primitive change

Live (8px dot, 3px ring at 28%, `motion-safe:animate-pulse`) and updated (6px
plain dot) — one hue, two shapes (principle 24). The open rail shows the same
dot after the tab label, which `OverflowTabs` could not carry: its mirror row
measures `{id, label, badge}` only. It gained an optional `signal` per tab,
drawn in the visible row, the overflow menu AND the mirror row, so the measured
width includes it. Additive; no existing consumer changes.

### Widths are the rail's own until #492

48px collapsed, 384px open — the design's "360 clips" note is why `w-96` and not
`w-80`. #492 lands the shell's grid variables and the two resize handles; the
rail then follows `--ws-rail` and needs nothing else from here.

## Loading treatment (#2540)

Built under the amended principle 12: the stage, the thread and the briefing
render `PortalSkeleton` (stage / thread / briefing) while their verdict says "no
data yet" — `stage.state === 'loading'`, `!historyLoaded`, `zone.state ===
'pending'` — never a bare `<x>.loading` path (the #1927 ratchet counts that
spelling), inside a wrapper that owns the footprint for both faces. The
`ScanlineReveal` importer set is pinned as an allowlist in
`tests/unit/portalLoadingTreatment.spec.js`; the two pre-ruling non-chart
holdovers are on #1921.

## Tests

`tests/unit/portalRail.spec.js` — the pure contract (+ ent#475: the four-tab registry, `feedsFor`, timestamps as instants, `updatedSignal`, seen markers, `feedView`, `railOpenPlan`, the removed placements, the owner's wiring); `tests/unit/portalRailFeeds.spec.js` — the feed store and the owner composable under Pinia in node (door-gated fetching, partial failure, the stale-response token, uploads on request, debounced push, the room's first beat, seen-on-open) (registry shape, doors,
ordering, state normalization and persistence, signal precedence and leakage,
room grouping, empty copy, placement) plus source guards (sibling of `<main>`
with no key; `store.isPlatformSession` never a literal; the reset watch; the
sheet; `PortalRail` renders nothing for an empty list and never reads the
registry; `motion-safe:` only; the two emitters; the `OverflowTabs` mirror).
`tests/unit/portalRailFiles.spec.js` and `tests/unit/portalFiles.spec.js` —
Slice 3's upload signal, the flat projection, the preview rules and the delete
matrix (see that section). `tests/unit/portalLoadingTreatment.spec.js` — the #2540 half. Verified live on
the Docker frontend: collapsed → open → persisted across reload; light and dark;
the mobile strip → sheet → Escape; the stage skeleton under a slowed roster; no
console errors.

## Slice 2 — Loops, Canvas and Files re-homed (trinity-enterprise#475)

The three placements #472 found — the loops strip above the composer (ent#458),
the Files slide-over, the canvas on the agent page only (ent#438) — dock into
the frame. The old placements are **removed, not duplicated**: `PortalLoops` is
no longer mounted by `PortalConversation` / `PortalRoom`, and
`PortalFilesPanel.vue` is deleted (its sheet chrome already lived in
`PortalRail`).

```
Portal.vue (shell)
├─ rail = usePortalRailFeeds({ visible: railVisible, tabs: railTabs, participants, activeTab, open, sheetOpen, storage })
│    ├─ wants = feedsFor(railTabs)                    ← THE door gate, extended from "mount" to "fetch"
│    ├─ watch([visible, participantsKey, wantsKey])   hidden → reset() ; empty participants → return (no blank) ;
│    │      loops ∈ wants → portalLoops.setParticipants + fetchLoops ; feeds.setFeeds + refresh()
│    ├─ watch(shown)      files opened → refresh({uploads:true}) ; canvas opened → refresh()
│    ├─ seen  = loadSeen(localStorage['trinity-workspace-rail-seen'])   re-marked on [shown, feeds.version]
│    └─ signals = { loops: loopsSignalFrom(loops.active), canvas|files: updatedSignal(newest server ts vs seen) }
├─ railSignals = { work, ...rail.signals }
├─ onWorkState: live → 0  ─► rail.refresh()          (a turn ended: 1:1 `sending`, room `working`)
├─ watch([convKey, roomId]) ─► rail.reset()
├─ @open-files ─► openRailOn('files')  = railOpenPlan({ wide: isWideViewport(window) })   column ≥ sm, sheet < sm
├─ @ask-canvas ─► usePlaybook(askCanvasPrefill(participants))   a PREFILL (conversation AND room), never a send
└─ <PortalRail> #tab-loops → PortalLoops · #tab-canvas → PortalRailCanvas → CanvasPanel × participant · #tab-files → PortalRailFiles
utils/websocket.js: loop_* and agent_activity (terminal) → portalRailFeeds.handleWebSocketEvent (debounced 2s, participant-filtered)
```

### One owner, so the collapsed rail can signal

The strip owned `stores/portalLoops.js` from inside two mount points and
guarded three races (`ownedKey`, the late `isPlatformSession`, the re-fire
resetting the chosen agent). The rail needs the loops signal with **no body
mounted**, so ownership moves to the shell — `composables/usePortalRailFeeds.js`
— which is one mount, keyed on the joined participant key and the feed set
(so a late auth confirmation that makes `loops` appear re-fires it). The
bodies only read. `stores/portalRailFeeds.js` is the same shape for canvases
and documents: `allSettled` per participant, partial failure keeps rows, a
fetch token drops a response that lands after a chat switch, **no timer while
idle**. `uploads` (the viewer's own inbox) is a container read on the backend,
so it is fetched only while Files is the open active tab, after an upload from
**any** surface, and after a delete. It **does** signal since #2582 — see
Slice 3 — through `filesSignalItems`, which projects uploads onto the
`created_at` key the Files dot already reads; the two collections stay
separate and only the signal merges them.

### Fetching follows the door, and the stage verdict

`feedsFor(visibleTabs)` decides what exists: an external client (Canvas ·
Files) never causes a loops request; a session with no participant fetches
nothing; `visible` false (agent page, a loading / failed / empty stage, a deep
link to an unreachable agent) clears both stores — so URL text never drives a
request. A room's first beat, with participants not yet landed, returns
without clearing, so the rail does not blank and refill.

### "Updated since last view"

No backend event exists for a canvas write or a shared file (registered as
debt). The dot derives from data on every render: the newest **server**
timestamp per participant (`updated_at` / `created_at`, compared as epoch ms
through `parseUTC` — never lexicographically, Invariant #16) is newer than the
agent's seen marker, or nothing was ever seen and the feed is non-empty. The
marker is the newest server stamp observed, so browser clock skew cannot keep
a dot lit. Markers persist under a **second** key
(`trinity-workspace-rail-seen`), so the approved `{open, tab}` key is
untouched. Refresh triggers: participants change, a turn ending (both chats,
through the Work signal's live → 0 edge), `loop_*` and terminal
`agent_activity` events for a participant (platform sessions), the tab being
opened, a successful upload **from any surface** (#2582 — the conversation
composer, a room's fan-out, or the tab's own drop zone), and a delete. An
external client with a scheduled canvas rewrite and no chat turn sees the dot
at its next turn or tab open — stated.

### One rendering layer for the canvas

`PortalRailCanvas` renders `CanvasPanel` per participant with
`store.fetchAgentCanvas` injected — the Workspace agent page's exact pair.
The audience is the ent#438 ruling, unchanged: `audience='roster'` for every
Workspace principal. `CanvasPanel` gained one watch: when the selected
canvas's `updated_at` moves on a list refresh, it re-reads the blocks, so a
lit dot never opens onto stale content.

### Loading and failure, per AC 6 as amended

Every body renders `PortalSkeleton variant="rail"` keyed on the feed's
verdict (`feedView` → `viewState`; `loading` while a room's participants have
not landed), `LoadFailed` + Retry on a failed first fetch, and keeps its rows
under an `InlineError` on a failed refresh. The Files drawer's `animate-spin`
is gone with the drawer.

## Slice 3 — Files: uploads at once, save, preview, delete (trinity#2582 + ent#548)

An operator tested the tab on `dev` (2026-09-07). Three defects and two asks,
shipped as one change set because both halves edit `PortalRailFiles.vue`.

### The upload had nowhere to announce itself

The composer uploaded straight to the agent's per-client inbox and told the
rail's feed store nothing, so "Files you sent" was stale until the tab was
opened or a turn ended. The fix is **not** in `PortalConversation.vue`: the
store funnel `clientPortal.js::uploadDocument` has exactly three callers —
`portalRailFeeds.js`, `PortalConversation.vue`, `PortalRoom.vue` — so notifying
from there catches the conversation, the room and the rail at once, and leaves
the file the whole delivery sequence is serialized to protect untouched.

The signal is a **pending-agent SET** drained by the owner composable, and that
shape is load-bearing. A scalar (`lastUpload` + a watcher joining the in-flight
promise) re-breaks the defect it fixes, twice:

* **Trailing loss.** `usePortalFileDrop` uploads a batch sequentially and does
  not await the feed re-read. File 1 resolves and starts a ~200–800 ms
  `container_exec_run`; file 2 resolves at +300 ms and the watcher joins
  promise #1, which returns a listing snapshotted *before* file 2 landed.
* **Fan-out loss.** `PortalRoom.vue`'s drop is
  `for (const name of names) await store.uploadDocument(name, file)` — three
  calls for three *different* agents. Vue coalesces mutations in one flush
  window into a single watcher invocation carrying only the last value.

So: a set, a **trailing re-fire** (a dirty flag that re-runs once after the
in-flight read settles), and the drain guarded by the feed store's existing
`_fetchToken` — shared with `refresh()`, or a `refresh({uploads:true})` issued
before the upload and resolving after it clobbers the fresh listing with the
pre-upload one.

**Stated reachability limit.** `feeds.uploads` is populated only on tab-open,
turn-end-while-open, or a `noteUpload`. On a fresh page load with Files closed,
`filesSignalItems` sees `{}`, so the dot **cannot** light for an upload made on
another device or in a previous session. That is defensible — and it is also
what avoids a one-time false-dot burst on deploy — but it is a real limit, not
an oversight.

### Download had to actually save

`GET /api/files/{id}` serves ent#461's inline allowlist, so an image opened in
a tab instead of saving. The route (and its `HEAD`, which must agree or a
player mis-plans) gains a **one-way** `?download=1`: it may only force
`attachment`, never `inline`. The asymmetry is the whole design — a requester
choosing to be *more* restricted about their own download grants nothing, while
the reverse is the XSS the allowlist exists to prevent. `sig` is a stored bearer
token compared with `compare_digest`, not an HMAC over the URL, so appending the
flag cannot invalidate it. Only the Files tab's URL carries it; the agent's chat
link does not.

Own uploads had no control at all, because a client upload has no DB row — it is
a file in a container directory. Reading one back is
`GET …/uploads/{filename}` through `extract_from_agent`, deleting one is
`rm -f --`, and the MIME has to be guessed (`mimetypes.guess_type` in
`_read_inbox`, which also fixes a live bug: `PortalRailFiles.vue` has always
rendered `<FileIcon :mime="u.mime_type">` against a field the response model
stripped, so the icon was unconditionally generic).

### Preview

`components/portal/PortalFilePreview.vue`, `v-if`-mounted — never `v-show`,
because the desktop column and the mobile sheet are siblings and a phone with
the sheet open mounts the tab body twice, where a teleported overlay would
ignore the hidden ancestor. Bytes come from the existing routes, not a new
preview route: `/files/preview` is platform-JWT-gated, reads only the agent
container's `/home/developer`, and serves `inline` unconditionally, so it can
serve neither an external client nor agent-shared bytes (which live at
`/data/agent-files/{id}` on the backend host).

* Images render **only** via `<img :src="objectUrl">` — never inline `<svg>`,
  never `v-html`. An uploaded SVG is a script host; `<img>` never executes it.
* Markdown goes through `PortalMarkdown` (the one sanitiser policy); other text
  renders in a `<pre>`, escaped by interpolation.
* Text is capped at **256 KB** and fetched **whole, with no `Range` header**,
  then sliced client-side. `main.py`'s CORS `allow_headers` does not list
  `Range`, so a ranged preview dies silently wherever the portal base URL is
  genuinely cross-origin — and slicing keeps preview off the download-counter
  path entirely. The cap is stated in the UI, not only in code.
* Non-previewable types, and a **failed byte fetch**, both land on the same
  name/size/type + Download card. "Never a blank modal" has to cover a failure,
  not only an unknown type.
* Next/previous walks the *previewable* subset of the flat list, so a `.zip`
  every third row is skipped rather than opening blank, and it stops at the ends.

Escape and the arrows are registered with **`{ capture: true }`** and call
`preventDefault()`. Capture is required: the conversation's turn-cancel listener
is on `document` in the bubble phase, so a bubble listener would let Escape
cancel an in-flight turn before `shouldCancelOnEscape` sees `defaultPrevented`.
**Known residual, filed as a follow-up:** `PortalConversation.vue` handles
Escape for an active voice call in a branch *above* that rule, so a preview
opened during a voice call also ends the call.

### Delete, and who may do what

| Case | Affordance | Mechanism |
|---|---|---|
| My own upload | **Delete** (real) | `rm -f --` in the container inbox |
| Agent-shared, I am a viewer | **Remove from my list** | a `portal_file_dismissals` row; the share is untouched |
| Agent-shared, I am the owner **in a platform session** | both, "Delete for everyone" offered | `db.revoke_agent_shared_file` (soft; the sweeper reclaims bytes) |

**The matrix is session-type dependent, and the copy says so.**
`PortalPrincipal` is `(email, is_platform)` and carries no role, so
`include_owned` is `principal.is_platform` everywhere (ent#358). A **non-owner
admin is a viewer** in the Workspace — stricter than the platform surface, and
correct — and an **owner on a magic-link portal token gets the viewer
affordance too**. `portal_owns_agent` is the *same* membership the roster card
renders, so the UI and the enforcement cannot disagree; the affordance is not
offered rather than offered-and-refused.

"Unshare" needed new storage: `agent_shared_files` has no audience column
(`portal_documents` lists every active share of the agent, for every rostered
client), and the one generic per-user preference store is FK'd to `users.id`,
which a portal principal has no row in. `portal_file_dismissals` is that
storage — server-side, auditable, and it survives a device change, which a
`localStorage` "hidden on this device" would not. It carries `agent_name` so it
follows the agent's lifecycle (and so it does not sidestep the cleanup parity
guard), and it does **not** validate the `file_id`: a 404 for an unknown id is
an existence oracle over every share in the install, exactly the fork
`set_chat_star` already resolved by capping rows instead.

`portal_revoke_shared_file` is **access-first** — roster (uniform 404) →
ownership (403) → row lookup (404) — never existence-then-access, which is the
shape Invariant #8 forbids. It returns **404** for an unknown id where the
sibling operator route `DELETE /api/agents/{name}/shared-files/{id}` is
documented idempotent-**204**; the divergence is enumeration-uniformity on an
external surface, and it is recorded here so the next reader does not "align" it.

### Costs, stated

The download route is the first time a rostered client can reach
`extract_from_agent`, which iterates the docker-py generator **synchronously
inside `async def`** on the global 4-worker executor shared with agent
start/stop/reload, and holds ~3× the file size in memory transiently. There is
no streaming primitive and building one was out of scope. The bound is a
**two-tier limiter** (`PORTAL_FILE_BURST_LIMIT` 20/60s,
`PORTAL_FILE_HOURLY_LIMIT` 100/1h, per email, env-tunable, delete on its own
looser counter) — the shape `router.py`'s own comment already required, and the
reason the burst tier is 20 and not 60.

Two smaller residuals: `uploaded_at` is a **container** mtime while `created_at`
is a backend `utc_now_iso()`, and `markSeen` stores one marker across both, so a
skewed agent clock can park the marker ahead and swallow a later real share. And
`_safe_filename` mangles non-Latin filenames on the way in, so a preview title
shows the mangled name — pre-existing and cosmetic, but it is the first thing a
reviewer clicks.

### Testing

* `tests/unit/test_2582_download_flag.py` — the one-way flag on GET and HEAD,
  `?download=` / `?download=x` not 422-ing, other headers preserved, the ranged
  206 path, and that a ranged prefix read is audited `ranged_prefix: true`
  without bumping `download_count`.
* `tests/unit/test_2582_portal_uploads.py` — gate order, the uniform 404 on a
  traversal attempt asserted **through the mounted route** as well as at the
  handler (uvicorn normalises `../` before Starlette matches, so a handler-only
  test cannot tell "gated" from "unroutable"), attachment headers, the `rm -f --`
  quoting, the ent#308 email pair landing in different inboxes, and 429 at both
  tiers.
* `tests/unit/test_ent548_portal_share_delete.py` — the permission matrix,
  access-first ordering, the dismissal's non-validation and row cap, both purge
  paths, the `AgentRef` registration, and both migration tracks.
* `src/frontend/tests/unit/portalRailFiles.spec.js` — an upload lights the dot
  with Files closed, a two-file batch does not lose the second, a room fan-out
  notes all three, and a `refresh` resolving after `noteUpload` does not clobber
  it.
* `src/frontend/tests/unit/portalFiles.spec.js` — `previewKind`, `neighbour`,
  `flattenFiles` order equalling render order, the `fileActions` matrix, and the
  component's own source guards (an `<img>`, no `v-html`, capture + preventDefault,
  `revokeObjectURL`, no `Range`, zero raw palette classes).

## Residuals (stated)

- The Work signal is store-derived since ent#525 (`workspace-work.md`): the owner merges
  the conversation's emit into the feed's live rows BY EXECUTION ID;
- On a phone the hidden column (`hidden sm:flex`) and the open sheet both mount
  the active tab's body; bodies never fetch, so the cost is a second form
  instance, not a second request.
- "See what you can ask" opens the agent page when the briefing is not on
  screen (a thread with messages) — the nearest home for "what it can do".
- The Reset-on-Main, sidebar, thread tab strip, top band, Agent-details panel
  and drop target of the approved conversation page are later steps of the
  same build; the rail's `w-96` and the sidebar's `w-72` are fixed until #492.
