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

`tests/unit/portalRail.spec.js` — the pure contract (registry shape, doors,
ordering, state normalization and persistence, signal precedence and leakage,
room grouping, empty copy, placement) plus source guards (sibling of `<main>`
with no key; `store.isPlatformSession` never a literal; the reset watch; the
sheet; `PortalRail` renders nothing for an empty list and never reads the
registry; `motion-safe:` only; the two emitters; the `OverflowTabs` mirror).
`tests/unit/portalLoadingTreatment.spec.js` — the #2540 half. Verified live on
the Docker frontend: collapsed → open → persisted across reload; light and dark;
the mobile strip → sheet → Escape; the stage skeleton under a slowed roster; no
console errors.

## Residuals (stated)

- The Work signal rides component emits until #457 gives executions a store;
  a store-derived signal is the shape every later tab uses (loops already can).
- "See what you can ask" opens the agent page when the briefing is not on
  screen (a thread with messages) — the nearest home for "what it can do".
- The Reset-on-Main, sidebar, thread tab strip, top band, Agent-details panel
  and drop target of the approved conversation page are later steps of the
  same build; the rail's `w-96` and the sidebar's `w-72` are fixed until #492.
