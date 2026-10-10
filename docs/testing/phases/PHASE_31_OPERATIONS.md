# Phase 31: Operations

> **Purpose**: Check every tab of the Operations page, its deep links and legacy redirects, and the fleet executions list against the API.
> **Duration**: ~15 minutes
> **Assumes**: the fixture trio is running and you are logged in as admin
> **Output**: Each Operations tab loads an honest populated or empty state, `?tab=` survives reload, old URLs land on the right tab, and the seeded executions are listed and open.
> **Last verified**: 2026-10-09 against source (not yet browser-run)

---

## Background

`/operations` (`views/Operations.vue`) is the single fleet-operations page, reached from the "Operations" item in the top nav. Its tabs, selected with `?tab=<id>`:

- "Needs Response" `needs-response` (default) — open operator-queue items, as cards
- "Notifications" `notifications` — agent notifications with filters
- "Health" `health` (admin only) — fleet health summary and per-agent status
- "Executions" `executions` — task runs across all agents
- "Reports" `reports` — reports agents have filed
- "Resolved" `resolved` — answered, cancelled and expired queue items

An unknown `?tab=` value falls back to Needs Response. This phase is **read-only**: it never answers, cancels, acknowledges, dismisses, clears or deletes anything, because the queue and notifications belong to real agents.

## Prerequisites

- [ ] Logged in as `admin` with `ADMIN_PASSWORD` from `.env`
- [ ] The `test-echo` execution id and the `test-counter` task id from fixtures.json are at hand
- [ ] `$TOKEN` holds a Bearer token: `POST http://localhost:8000/api/token` (form-encoded `username=admin` + `password=$ADMIN_PASSWORD`)
- [ ] Browser window at least 1280 px wide to start

## Setup
- Note the theme shown by the theme toggle button in the top nav (it cycles on click) as **ORIGINAL_THEME**.

## Test: Page and tabs

### Step 1: Default tab
**Action**:
- Click "Operations" in the top nav (or open `http://localhost/operations`).

**Expected**:
- [ ] Heading "Operations"
- [ ] Tabs in this order: "Needs Response", "Notifications", "Health", "Executions", "Reports", "Resolved"
- [ ] "Needs Response" is selected
- [ ] A "Refresh" button is at the right of the tab row
- [ ] The subtitle under the heading reads "All clear — your agents are working independently", or states counts such as "2 pending responses, 1 notification". Record it.

### Step 2: Needs Response (observe-only)
**Action**:
- Read the Needs Response tab. Click nothing inside a card.

**Expected**:
- [ ] If the queue is empty: a green check with "All caught up" and "Your agents are working independently. Nice."
- [ ] If items exist: one card per item; the tab shows a numeric badge equal to the number of cards. Record the count.
- [ ] It never shows "All caught up" together with cards
- [ ] If a "Clear All" button is shown, do **not** click it

**Verify** (API, read-only):
```bash
curl -s -H "Authorization: Bearer $TOKEN" "http://localhost:8000/api/operator-queue?limit=200" \
  | python3 -c "import sys,json; i=json.load(sys.stdin)['items']; print(sum(1 for x in i if x['status']=='pending'), 'pending of', len(i))"
```
- [ ] The pending number equals the card count (and the tab badge, when shown)

**Do not** type in a card's answer field, click an option, or click any send/cancel control.

### Step 3: Notifications (observe-only)
**Action**:
- Click "Notifications". Read the filter row, the four stat cards and the list.
- Change the Status filter from "Pending" to "All", then click "Clear filters".

**Expected**:
- [ ] URL carries `?tab=notifications`
- [ ] Filters labelled "Agent", "Type", "Priority", "Status" and a "Show dismissed" checkbox; Status starts on "Pending"
- [ ] Stat cards "Pending", "Acknowledged", "Total", "Agents"
- [ ] A list headed "Notifications" with a "Select all" checkbox
- [ ] If the list is empty with default filters: "No events yet" and "Notifications from your agents will appear here when they send them."
- [ ] With Status "All" and nothing to show: "No matching events" / "Try adjusting your filters"
- [ ] "Clear filters" returns Status to "Pending"

**Verify** (API, read-only):
```bash
curl -s -H "Authorization: Bearer $TOKEN" "http://localhost:8000/api/notifications/count?status=pending"
```
- [ ] Returns `{"status":"pending","count":N}`. Record N. The tab badge is shown only when N > 0.

**Do not** click a row's "Acknowledge" or "Dismiss" button, tick any checkbox, or use "Clear All".

### Step 4: Health
**Action**:
- Click "Health".

**Expected**:
- [ ] URL carries `?tab=health`; the subtitle reads "Fleet-wide health status and alerts"
- [ ] A status label "Monitoring Active" or "Monitoring Disabled" (record which), and buttons including "Check All"
- [ ] Five summary cards: "Total Agents", "Healthy", "Degraded", "Unhealthy", "Critical"
- [ ] A section "Agent Health Status" with a status filter starting on "All Statuses"
- [ ] `test-echo`, `test-counter` and `test-delegator` each appear as a row with a "Last check" value, or the section shows "No agents found" / "No agents are being monitored" (record which)
- [ ] Choosing "Critical" in the filter either narrows the rows or shows "No agents match the selected filter"; set it back to "All Statuses"

**Verify** (API, read-only):
```bash
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/monitoring/status
```
- [ ] Prints `200`

**Do not** click "Disable monitoring" / "Enable monitoring", "Check All", or a row's "Trigger health check" button.

## Test: Executions

### Step 5: Executions list and totals
**Action**:
- Click "Executions". In the filter bar, change the time filter from "Last 24h" to "All time".

**Expected**:
- [ ] URL carries `?tab=executions`; the subtitle reads "All task runs across your fleet"
- [ ] A "Live" or "Polling" indicator and a "Refresh" button at the top right
- [ ] Four stat cards: "Total", "Completion", "Failed", "Cost"
- [ ] Filters: "All agents", "All statuses", "All triggers", the time filter, and a search field with placeholder "Search tasks…"
- [ ] Under the filters a line "N shown" and a "Clear filters" link
- [ ] Each row shows a status badge, the agent name, a trigger label, a relative time and the task text
- [ ] The list is bounded: at most 50 rows load at once; if more exist a "Load more" button is at the bottom

**Verify** (API, read-only):
```bash
curl -s -H "Authorization: Bearer $TOKEN" "http://localhost:8000/api/executions/stats?hours=0"
curl -s -H "Authorization: Bearer $TOKEN" "http://localhost:8000/api/executions?hours=0&limit=50" \
  | python3 -c "import sys,json; print(len(json.load(sys.stdin)))"
```
- [ ] `total` in the stats response equals the "Total" card
- [ ] The row count printed equals "N shown" (before any "Load more")

### Step 6: Filter to the seeded executions
**Action**:
- Keep "All time". In the agent filter choose `test-echo`. Find the row whose id matches the execution id from fixtures.json (ids are shown shortened at wide widths).
- Change the agent filter to `test-counter` and find its seeded task.
- Type `zzz-no-such-task` in the search field.
- Click "Clear filters".

**Expected**:
- [ ] With `test-echo` selected every row names `test-echo`, and the seeded execution is among them. Record its status badge text.
- [ ] With `test-counter` selected the seeded task is listed. Record its status.
- [ ] The nonsense search shows "No matching executions" with a "Clear filters" link
- [ ] After "Clear filters" the time filter is back on "Last 24h" and the other filters are back on their "All …" options

### Step 7: Open an execution
**Action**:
- Set the time filter to "All time" and the agent filter to `test-echo`. Click the seeded execution's row (not the agent name).
- Use the browser Back button.

**Expected**:
- [ ] Lands on `/agents/test-echo/executions/<execution id from fixtures.json>`
- [ ] The page is headed "Execution Details" and has a "Task Input" section
- [ ] Back returns to `/operations?tab=executions`

### Step 8: Status colours in both themes
**Action**:
- On the Executions tab with "All time" selected, click the theme toggle in the top nav until the page is dark; look at the status badges and the stat cards. Then switch to light and look again.

**Expected**:
- [ ] In both themes each status badge's text is readable against its background
- [ ] Success-type and failure-type badges are visibly different colours in both themes
- [ ] Stat cards, filter bar and rows have no white-on-white or dark-on-dark text
- [ ] Set the toggle back to **ORIGINAL_THEME**

## Test: Reports and Resolved

### Step 9: Reports
**Action**:
- Click "Reports". Change the window filter from "7d" to "All time".

**Expected**:
- [ ] URL carries `?tab=reports`
- [ ] Three tiles: "Total reports", "Report types", "Agents reporting"
- [ ] Filters "All agents", "All types", the window filter, and a search field with placeholder "Search title / type / agent"
- [ ] If no reports exist: "No reports match these filters."
- [ ] If reports are listed: click one row to expand it; its content renders and "Export .xlsx" is offered. Click the row again to collapse it.

**Verify** (API, read-only):
```bash
curl -s -H "Authorization: Bearer $TOKEN" "http://localhost:8000/api/reports?hours=0&limit=100" \
  | python3 -c "import sys,json; print(len(json.load(sys.stdin)))"
```
- [ ] 0 when the empty state is shown; otherwise equal to the number of rows listed

**Do not** click "Delete" on a report or either export button.

### Step 10: Resolved (observe-only)
**Action**:
- Click "Resolved".

**Expected**:
- [ ] URL carries `?tab=resolved`
- [ ] Either "No resolved items yet", or a list of resolved cards. Record the count.
- [ ] If a "Clear All" button is shown, do **not** click it

## Test: Links and width

### Step 11: Deep link, reload and invalid tab
**Action**:
- Open `http://localhost/operations?tab=reports`, then reload.
- Open `http://localhost/operations?tab=not-a-tab`.

**Expected**:
- [ ] "Reports" is selected on first load and still selected after reload
- [ ] The invalid value shows "Needs Response" selected with its normal content, not a blank page

### Step 12: Legacy redirects
**Action**:
- Open each URL and record the final URL and selected tab:
  1. `http://localhost/executions`
  2. `http://localhost/events`
  3. `http://localhost/monitoring`
  4. `http://localhost/operating-room`
  5. `http://localhost/operating-room?tab=resolved`

**Expected**:
- [ ] 1 → `/operations?tab=executions`, "Executions" selected
- [ ] 2 → `/operations?tab=notifications`, "Notifications" selected
- [ ] 3 → `/operations?tab=health`, "Health" selected
- [ ] 4 → `/operations`, "Needs Response" selected
- [ ] 5 → `/operations?tab=resolved`, "Resolved" selected (the incoming `?tab=` is kept)

### Step 13: Tab bar at 390 px
**Action**:
- Open `http://localhost/operations?tab=executions` and resize to 390 × 844.
- If a "More" control is at the end of the tab row, open it and choose a tab from the menu.

**Expected**:
- [ ] Tabs that do not fit are collected under a "More" control; no tab is cut off mid-label and the tab row does not need sideways scrolling
- [ ] The selected tab is still identifiable (in the row, or inside the "More" menu)
- [ ] Choosing a tab from the menu switches content and updates `?tab=`
- [ ] Executions rows wrap inside the screen; the stat cards form two columns; the page does not scroll sideways
- [ ] Resize back to 1280 px wide

## Cleanup / Restore

- Nothing was answered, cancelled, acknowledged, dismissed, cleared, exported or deleted.
- Theme: confirm the top-nav toggle is back on **ORIGINAL_THEME** (Step 8).
- Filters on Notifications, Executions and Reports are held in page memory only; a reload resets them. Reload once to confirm Executions is back on "Last 24h".

## Critical Validations

1. All six tabs load for admin, and each shows either real rows or its own empty-state text.
2. The seeded `test-echo` execution is listed under Executions with "All time" selected and opens at `/agents/test-echo/executions/<id>`.
3. The "Total" card and the "N shown" line agree with `/api/executions/stats` and `/api/executions`.
4. `?tab=` survives a reload, and an invalid value falls back to Needs Response.
5. The four legacy URLs land on the stated tabs.

## Success Criteria

- [ ] Needs Response, Notifications and Resolved were observed without acting on any item
- [ ] Health tab shows summary cards and the agent list
- [ ] Executions filters, empty-filter state and click-through work
- [ ] Redirects and deep links behave as listed
- [ ] No sideways scroll at 390 px; badges readable in both themes

## Troubleshooting

- **Seeded execution not listed**: the Executions tab defaults to "Last 24h". Select "All time".
- **"Couldn't load the queue"**: the operator-queue request failed before the first load; use its retry once and record the detail text. This is a failure of the page, not an empty queue.
- **No "Health" tab**: the tab is shown only to the admin role. Confirm you are logged in as `admin`.
