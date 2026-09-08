# Feature: OverflowTabs Component (Agent Detail tab overflow)

> **Created**: 2026-06-10 (#1114) — reusable "priority+" tab strip that replaces the Agent Detail tab bar's horizontal scroll with a "More ▾" overflow dropdown.

## Overview

`OverflowTabs.vue` is a reusable Vue 3 component that renders a horizontal tab
strip and, when the tabs don't fit the container, collapses the trailing
overflow into a right-aligned **"More ▾"** disclosure menu instead of an
`overflow-x-auto` horizontal scrollbar. The split re-measures on container
resize (`ResizeObserver`) and after web-font load.

It replaces the Agent Detail (`views/AgentDetail.vue`) tab nav, which had grown
to ~16 tabs (Overview, Tasks, Chat, Session, Dashboard, Schedules, Loops,
Playbooks, Credentials, Payments, Sharing, Permissions, Git, Files, Folders,
Guardrails, Info) and relied on easy-to-miss horizontal scrolling on narrow
viewports.

## User Story

As a platform user on a narrow viewport, I want overflowing Agent Detail tabs
to collapse into a discoverable "More" menu instead of hiding behind a
horizontal scrollbar, so every tab stays reachable without scrolling.

## Component Location

**File**: `src/frontend/src/components/OverflowTabs.vue`

**Used in**: `src/frontend/src/views/AgentDetail.vue` — the tab nav over
`visibleTabs` (replaces the former `<div class="…overflow-x-auto…"><nav>` block):

```vue
<OverflowTabs :tabs="visibleTabs" v-model="activeTab" />
```

`visibleTabs` is the existing computed array of `{ id, label, badge? }`;
`activeTab` is a local string ref. The component is intentionally generic so
the `Operations.vue` (`?tab=`-driven) tab strip can adopt it next.

---

## Component API

### Props

| Prop | Type | Default | Description |
|------|------|---------|-------------|
| `tabs` | Array | **required** | `[{ id, label, badge?, signal?: 'live'\|'updated', pinned? }]` — tab definitions, in display order. `pinned` (ent#523) draws a bookmark before the label; `signal` (ent#474) draws an activity dot after it. Both are drawn in the MIRROR row too — a glyph the visible row renders and the mirror does not is a tab measured narrower than it draws |
| `modelValue` | String \| null | **required** | active tab id (v-model) |
| `moreLabel` | Function | `() => 'More'` | the overflow trigger's label, given the hidden count (ent#451 — the contract's counted "N more"). The mirror measures the WIDEST label the strip can need, so a growing count never reflows the fit |
| `dense` | Boolean | `false` | compact pad and type for a strip above a thread (ent#451) |
| `fixedWidth` | Boolean | `false` | every tab one width (`FIXED_TAB_WIDTH`, exported), label clamped, full text on `title=` — the opt-in for a strip of **unbounded** labels (#2579). See below |

*(The middle three rows were added by #2579 — the props themselves shipped with
ent#451/#474/#523 and the table had drifted behind them.)*

### The fixed-width variant (#2579)

Added for the Workspace chat strip, whose labels are user and model text: one
long title stretched its tab and pushed every sibling under "N more". Under
`fixedWidth` each tab is `FIXED_TAB_WIDTH` (`'w-40'` — a named export from a
companion `<script>` block, since `<script setup>` cannot carry one), the label
clamps in a `min-w-0 truncate` span, and the full text rides `title=` on the
button **and** on the overflow-menu row.

Default **false**, and every width-related class — the native tooltip included —
is gated on it, so Agent Detail, Library and the portal rail are unchanged by
construction; a tooltip on "Overview" would be noise and a 160px "Files" tab
waste. Two of the gated classes are load-bearing rather than cosmetic:
`inlineCount` starts at `+Infinity`, so on first paint every tab renders inline,
and a truncating label drops the button's min-content to padding — without
`shrink-0` on the visible button (and `overflow-hidden` on the visible nav) flex
squeezes the row to ~50px per tab for a frame while the `max-content` mirror
still reports 160. The mirror takes the width class and **nothing else**: it
never shrinks, and `getBoundingClientRect` returns the border box.

This is a recorded amendment to the design contract's "never wrap or truncate",
which governs the **set** — the strip still overflows into a counted menu.

### Events

| Event | Payload | Description |
|-------|---------|-------------|
| `update:modelValue` | String | emitted on tab/menu-item selection (v-model) |

The component holds **no** routing or selection state of its own — the parent
owns `activeTab`, so `?tab=` deep-linking and programmatic tab switches are
unaffected (the component simply reflects whatever `modelValue` is, inline or
in overflow).

---

## Measurement mechanism ("priority+" pattern)

The split between inline and overflowed tabs is computed deterministically:

1. **Hidden mirror row** — a second `aria-hidden` `<nav>` renders ALL tabs (incl.
   badges) plus a worst-case "More" button. It lives in a `position:absolute;
   width:0; height:0; overflow:hidden; visibility:hidden` wrapper, so its boxes
   stay measurable (`getBoundingClientRect().width`) while contributing zero
   layout and never inducing page scroll. (`visibility:hidden`, NOT
   `display:none`, which would report `0`.)
2. **`recompute()`** — if `sum(tabWidths) <= containerWidth`, render all inline
   (no "More" button). Otherwise reserve the measured `moreWidth` and pack tabs
   from the left until the next would exceed `containerWidth - moreWidth`; that
   index is the inline/overflow boundary.
3. **Refinements**:
   - Single-overflow optimization: if exactly one tab overflows and it fits
     without reserving the "More" trigger, keep it inline.
   - Always keep ≥1 tab inline when the first one fits.
   - 1px epsilon on the fit comparison (sub-pixel rounding).

### Re-measure / reflow triggers

| Trigger | Mechanism |
|---------|-----------|
| Container resize | `ResizeObserver` on the **outer** wrapper (not the content-sized nav, which would feedback-loop), rAF-debounced, with a width-diff early-return so height-only jitter (badge pill, font swap) doesn't recompute |
| Web-font load | `document.fonts.ready` re-measure (font swap changes intrinsic text width but does NOT resize the container, so RO never fires) |
| `tabs` content change | `watch` on a derived `id:label:badge` signature, `flush:'post'` (in-place badge/label mutation must re-measure) |

**First-paint**: defaults to all-inline before the first measurement, so the
common fits-everything case is correct on first paint with no collapse/snap.

### Active-tab-in-overflow

When the active tab lands in the overflow set, the "More" trigger reflects it
(active underline `border-action-primary-500` + a small dot) rather than
reshuffling tab order. The trigger label stays the fixed "More ▾" so its width
matches the reserved `moreWidth`.

---

## Accessibility

Implemented as a **plain disclosure** (NOT `role="menu"`), consistent with the
page's plain-button tabs — advertising full ARIA menu semantics without
arrow-key roving would be a worse experience than an honest disclosure:

- Trigger is a `<button>` with `:aria-expanded` + `aria-controls="overflow-tabs-menu"`.
- Dropdown panel is a `<div id="overflow-tabs-menu">` of plain `<button>` items
  (Tab traverses them).
- Opens on click; **Escape** closes and returns focus to the trigger;
  outside **`pointerdown`** closes (touch-safe).
- Focus moves to the first item on open.
- Dark-mode aware (`dark:` classes on panel + items).

---

## Data Flow

```
Container resizes / fonts load / tabs change
    |
    v
measure() reads widths from the hidden mirror row
    |
    v
recompute() → inlineCount → inlineTabs / overflowTabs
    |
    +-- visible nav renders inlineTabs (+ "More ▾" when overflowTabs.length > 0)
    +-- dropdown renders overflowTabs (when open)
    |
User clicks a tab (inline or in dropdown)
    |
    v
emit('update:modelValue', id)  → parent sets activeTab → tab content swaps
```

No backend, API, or database involvement — pure client-side render logic.

---

## Testing

**E2E**: `src/frontend/e2e/agent-detail-tabs-overflow.spec.js` (Playwright,
`@interactive`; `beforeAll` resolves a target agent, `TABS_TEST_AGENT` override).
Stable selectors are component contracts: `[data-overflow-trigger]`,
`[data-overflow-menu]`, `[data-menu-item]`, `[data-measure-tab]`.

| # | Behavior |
|---|----------|
| B1 | Narrow viewport collapses overflow into a "More" dropdown; visible nav does not horizontally scroll |
| B2 | Selecting an overflow item activates that tab, closes the menu, and the trigger reflects the active state |
| B3 | Reflow on resize — a wider container fits more tabs inline (overflow set shrinks) |
| B4 | Active tab reflected whether inline OR overflow, tracked across a live reflow |
| B5 | Escape closes the dropdown and returns focus to the trigger |
| B6 | Outside click closes the dropdown |

Verified manually in light + dark mode; no-overflow (all-inline, no "More")
confirmed on an agent whose tabs all fit (`trinity-system`).

### Status
- Working (2026-06-10) — 7/7 e2e pass.

### Known limitation (pre-existing, out of scope)
`AgentDetail`'s fresh-load `?tab=` deep-link does not reliably apply the
requested tab on a hard page load (confirmed present on the original code too —
not a regression from this change). `OverflowTabs` faithfully reflects whatever
`activeTab` the parent sets; the deep-link plumbing in `AgentDetail.onMounted`
is a separate concern.

---

## Related Flows

- **[Agent Overview Dashboard](agent-overview-dashboard.md)** — the Agent Detail Overview tab + tab IA (#1107)
- **[Operating Room](operating-room.md)** — Operations `?tab=` strip, candidate adopter of this component
- **[Workspace chat tabs and titles](workspace-chat-tabs-and-titles.md)** — the `dense` + `fixedWidth` consumer, and the only one that opts into either

---

## Revision History

| Date | Change |
|------|--------|
| 2026-06-10 | Initial — `OverflowTabs.vue` introduced (#1114), wired into AgentDetail tab nav |
| 2026-09-07 | `fixedWidth` opt-in for strips of unbounded labels (#2579, the Workspace chat tabs); the props table brought current with `dense` / `moreLabel` / `pinned` / `signal`. Geometry pinned in `src/frontend/e2e/workspace-chat-tabs.spec.js` |
