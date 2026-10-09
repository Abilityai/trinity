# Phase 11: Dashboard: Timeline, Grid, List

> **Purpose**: Verify the Dashboard's three view modes, its header stats and controls, the `?view=` and `/agents` entry points, and the keyboard shortcuts.
> **Duration**: ~15 minutes
> **Assumes**: the fixture trio is running and you are logged in as admin
> **Output**: Timeline, Grid and List each render the fleet (trio + system agent) with working, non-destructive controls, and the user's saved view preference is respected
> **Last verified**: 2026-10-09 against source (not yet browser-run)

---

## Background

The Dashboard (`/`, `views/Dashboard.vue`) is the fleet home and the only place agents are listed. It has three modes, in this order: **Timeline** (default), **Grid**, **List** (`utils/viewModes.js`). There is no graph mode. The old Agents page is the List mode: `/agents` redirects to `/?view=list`.

A click on the mode switcher is saved in the browser (`localStorage` key `trinity-dashboard-view`). `?view=<mode>` is a one-shot instruction: it is applied, then stripped from the URL, and it does **not** overwrite the saved choice.

Replaces the January flow that expected eight agents, a node graph with communication edges and a `Timeline`/`Graph` toggle.

This phase is observe-only. It sends no messages and never clicks a row's `Running`, `AUTO`/`Manual` or `Read-Only`/`Editable` toggle, `Create Agent`, `Tidy up`, `Reset`, or any bulk-tag control.

## Prerequisites

- [ ] Logged in as `admin` with `ADMIN_PASSWORD` from `.env`
- [ ] `test-echo`, `test-counter`, `test-delegator` exist; `trinity-system` exists
- [ ] Viewport 1280 × 800

## Setup

### Step 1: Record the saved preferences
**Action**:
- Navigate to `http://localhost/`
- In the browser console evaluate:
  `[localStorage.getItem('trinity-dashboard-view'), localStorage.getItem('trinity-dashboard-time-range')]`
- Note the title of the theme button in the top bar (`Light mode (click to switch)`, `Dark mode (click to switch)` or `System theme (click to switch)`)

**Expected**:
- [ ] Record the two values as `VIEW_ORIGINAL` and `RANGE_ORIGINAL` (either may be `null`: view then defaults to timeline, range to 24) and the theme title as `THEME_ORIGINAL`

---

## Test: Header

### Step 2: Stats cluster and host telemetry
**Action**:
- Look at the left side of the header strip under the top nav

**Expected**:
- [ ] `R/T agents` where T is the total agent count and R the running count (T ≥ 4)
- [ ] `N working now`
- [ ] `N messages (Hh)` where H is the selected time range in hours
- [ ] Host telemetry: `CPU` with a percentage, `Mem` with used/total, `Disk` with a percentage (it appears a moment after load)

### Step 3: Controls
**Action**:
- Look at the right side of the same strip

**Expected**:
- [ ] A `Create Agent` button (do not click)
- [ ] A button showing `/` with title `Filter agents (press /)`
- [ ] A time-range select with options `1h`, `6h`, `24h`, `3d`, `7d`
- [ ] A small dot titled `Connected` (red and titled `Disconnected` is a failure)
- [ ] A button titled `Refresh`
- [ ] Last in the row, a switcher titled `Switch view (press v to cycle)` with three buttons: `Timeline`, `Grid`, `List`; exactly one is highlighted
- [ ] A `Tags` button and an `All Owners` select appear only when the fleet has tags / more than one owner — record which are present

---

## Test: Timeline mode

### Step 4: Timeline layout
**Action**:
- Click `Timeline`

**Expected**:
- [ ] Left of the timeline bar: buttons titled `Zoom out` and `Zoom in` around a slider titled `Zoom level`, a percentage, and a checkbox `Active only`
- [ ] Right: a legend listing `Manual`, `MCP`, `Scheduled`, `Agent-Triggered`, `Paid`, `Public`, `Next Run`; then `Live` with a pulsing dot; then `N events`
- [ ] One row per agent: `test-echo`, `test-counter`, `test-delegator`, and `trinity-system` carrying a `SYS` badge
- [ ] Each row shows either task count and success percentage, or `No tasks`
- [ ] The system agent's row has no autonomy toggle; the others do

### Step 5: Zoom
**Action**:
- Record the zoom percentage. Click `Zoom in` once, then `Zoom out` once.

**Expected**:
- [ ] `Zoom in` raises the percentage by 50 (e.g. `1200%` → `1250%`); `Zoom out` returns it to the recorded value
- [ ] Agent labels stay pinned at the left while the timeline is scrolled horizontally

### Step 6: Time range
**Action**:
- Select `1h`, then `7d`, then set the select back to the value it had

**Expected**:
- [ ] The header stat reads `messages (1h)`, then `messages (168h)`
- [ ] The `N events` count and the bars change with the range without a page reload
- [ ] Hovering a bar in the `test-echo` row shows a tooltip beginning with one of `Manual Task`, `MCP Task`, `Agent-Triggered Task`, `Public Task`, `Scheduled`, `Task`, `Execution` — record which

### Step 7: Active only
**Action**:
- Record the `Active only` checkbox state, toggle it, observe, toggle it back

**Expected**:
- [ ] Ticked: rows with no activity in the range are hidden; unticked: all four agents are listed again
- [ ] The checkbox ends in the recorded state

---

## Test: Grid mode

### Step 8: Grid layout
**Action**:
- Click `Grid`

**Expected**:
- [ ] `Tidy up` and `Reset` buttons appear immediately left of the switcher (do not click them); the switcher itself does not move
- [ ] A tile exists for each of `test-echo`, `test-counter`, `test-delegator` and `trinity-system`; the system tile carries a `SYSTEM` badge
- [ ] Each tile shows the agent name, a state word, and a `Details` button
- [ ] Canvas controls titled `Zoom in`, `Zoom out`, `Fit view` with a percentage, and buttons `Zones` and `Lines`
- [ ] If tiles show only a name (zoomed far out), record it, click the canvas `Zoom in` until `Details` is visible, and click `Zoom out` the same number of times before leaving Grid

### Step 9: Click-through from a tile
**Action**:
- Click `Details` on the `test-echo` tile; go back; click `Details` on the `trinity-system` tile; go back

**Expected**:
- [ ] First click lands on `/agents/test-echo` with the Overview tab active
- [ ] Second click lands on `/agents/trinity-system`
- [ ] After each Back, the Dashboard is in Grid mode with the tiles where they were

---

## Test: List mode

### Step 10: List layout
**Action**:
- Click `List`

**Expected**:
- [ ] Toolbar: input with placeholder `Search agents...`; a three-way filter `All` / `Running` / `Stopped`; a sort select with `Newest First`, `Oldest First`, `Name (A-Z)`, `Name (Z-A)`, `Running First`, `Success Rate`
- [ ] Column header `Name`, `Status`, `Controls`, `Success`, `Exec / Sched` — shown only when the list area itself is at least ~1088 px wide. At 1280 px with the left views sidebar open it is not shown; collapse the sidebar (button titled `Collapse sidebar`) or widen the window to 1600 px to see it, then put the sidebar/window back. Record which layout you saw.
- [ ] One row per agent with a checkbox, a status dot and the name as a link; the system agent's row carries `SYSTEM`
- [ ] `Tidy up` and `Reset` are gone

### Step 11: List search, status filter and sort
**Action**:
- Type `test-` in `Search agents...`
- Replace it with `zzz-no-such-agent`
- Click `Clear all filters`
- Click `Running`, then `All`
- Choose `Name (A-Z)` in the sort select, then choose `Newest First`

**Expected**:
- [ ] `test-`: only rows whose name contains `test-` remain; a counter `X/T` and a `Clear` button appear in the toolbar
- [ ] `zzz-no-such-agent`: a card reads `No matching agents` / `Try adjusting your filters.` with a `Clear all filters` button
- [ ] After clearing: all rows return and the counter disappears
- [ ] `Running`: only running agents are listed
- [ ] `Name (A-Z)`: rows are in ascending name order
- [ ] Clicking the `test-echo` name link opens `/agents/test-echo` (go back afterwards)

---

## Test: Shortcuts and entry points

### Step 12: `/` type-to-filter
**Action**:
- Click an empty part of the page so no input has focus, then press `/`
- Type `echo`; then replace it with `zzz-no-such-agent`; then press `Esc`
- Click the `/` button in the header twice

**Expected**:
- [ ] A floating search pill opens with placeholder `Filter agents…`, focused
- [ ] `echo`: only `test-echo` remains in the current mode
- [ ] `zzz-no-such-agent`: an overlay reads `No agents match "zzz-no-such-agent" — Esc to clear` with a `Clear filter` button
- [ ] `Esc`: the pill closes and every agent is listed again
- [ ] The header `/` button opens the pill on the first click and closes it on the second
- [ ] Pressing `/` while the cursor is inside `Search agents...` types a slash instead of opening the pill

### Step 13: `v` cycles the mode
**Action**:
- Click `Timeline`. With no input focused press `v` three times.

**Expected**:
- [ ] The mode goes Timeline → Grid → List → Timeline, and the highlighted switcher button follows

### Step 14: `?view=` and `/agents`
**Action**:
- Click `Timeline` (this saves timeline as the preference)
- Navigate to `http://localhost/?view=grid`, wait for the page, then reload
- Navigate to `http://localhost/agents`
- Navigate to `http://localhost/?view=bogus`

**Expected**:
- [ ] `/?view=grid`: Grid is shown and the address bar becomes `http://localhost/` (the parameter is stripped)
- [ ] After the reload: **Timeline** is shown — the query did not overwrite the saved choice
- [ ] `/agents`: the address bar ends at `http://localhost/` and **List** is shown
- [ ] `/?view=bogus`: record which mode is shown (an unknown mode degrades to Timeline) and whether anything in the left views sidebar changed

---

## Test: Narrow width and themes

### Step 15: 390 px
**Action**:
- Resize to 390 × 844. Visit Timeline, Grid and List in turn. Restore 1280 × 800.

**Expected**:
- [ ] The `R/T agents` stat stays visible; `working now`, `messages` and the host meters may drop away one by one as space runs out, but nothing is half-clipped
- [ ] The controls wrap onto further lines instead of running off the right edge; `Create Agent` shrinks to an icon (its `aria-label` is still `Create Agent`)
- [ ] The mode switcher is still fully visible and clickable
- [ ] Timeline: the legend is hidden at this width; `Live` and `N events` remain
- [ ] List: rows switch to a stacked layout and the column header row is not shown
- [ ] `document.documentElement.scrollWidth <= window.innerWidth` in all three modes — record any mode where it is not

### Step 16: Light and dark
**Action**:
- Click the theme button until dark is active; visit Timeline, Grid and List; click until light is active and repeat; then click until the title equals `THEME_ORIGINAL`

**Expected**:
- [ ] In both themes the highlighted switcher button, the legend labels, timeline bars, the `SYS` / `SYSTEM` badges and List row text are legible

---

## Cleanup / Restore

Original values: `VIEW_ORIGINAL`, `RANGE_ORIGINAL`, `THEME_ORIGINAL` from Step 1.

- View: click the switcher button matching `VIEW_ORIGINAL` (`Timeline` if it was `null`).
- Time range: set the select to `RANGE_ORIGINAL` hours (`24h` if `null`; 72 = `3d`, 168 = `7d`).
- Theme: the button title equals `THEME_ORIGINAL`.
- List sort is `Newest First`, status filter `All`, search box empty, type-to-filter pill closed, `Active only` as found.

## Manual-only (not run unattended)

- `Tidy up` / `Reset`, dragging tiles, `Zones`, `Lines` and department assignment — these rewrite the saved grid layout.
- Bulk tag add/remove from List selection — changes agent tags.
- Row toggles in List (`Running`, `AUTO`/`Manual`, `Read-Only`/`Editable`) — change agent state.
- The red `Disconnected` indicator and the `Couldn't load timeline data.` / `Couldn't load agents` + `Retry` states — need the backend to be down.

## Critical Validations

1. The switcher offers exactly Timeline, Grid, List, in that order, and each renders the trio plus the system agent.
2. `/agents` lands on the Dashboard in List mode.
3. `?view=` applies once, is stripped from the URL, and does not change the saved preference.
4. The timeline legend lists the seven labels in Step 4.
5. `/` and `v` work when no input is focused and are ignored inside inputs.
6. No horizontal page scroll at 390 px in any mode.

## Success Criteria

- [ ] Tile and list-row click-through reach `/agents/<name>`
- [ ] Search, status filter, sort and the two empty states in List behave as stated
- [ ] Shortcuts and entry points behave as stated
- [ ] Saved view, time range and theme are restored

## Troubleshooting

- **A fixture row is missing in Timeline**: `Active only` is ticked, or an owner/tag filter or the `/` filter is applied — clear them.
- **`v` or `/` does nothing**: focus is inside an input, select or textarea, or a modal is open. Click an empty area first.
- **Zoom percentage differs from the example**: the starting zoom is derived from the time range (half the range in hours, as a multiplier), so only the ±50 step is asserted.
