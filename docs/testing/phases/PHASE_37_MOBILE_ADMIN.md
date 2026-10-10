# Phase 37: Mobile admin (/m)

> **Purpose**: Check the phone-first admin page at `/m` — its own password login, its three bottom tabs and its fit at 390 px and 320 px — without changing any agent, queue item or schedule.
> **Duration**: ~15 minutes
> **Assumes**: the fixture trio is running and you are logged out (Setup makes sure of it)
> **Output**: `/m` signs in inline, shows the fleet, the operator queue and fleet health consistently with the API, never overflows sideways on a small phone, and signs out in place
> **Last verified**: 2026-10-09 against source (not yet browser-run)

---

## Background

`/m` (`views/MobileAdmin.vue`) is a self-contained page with no NavBar. The route is public
(`requiresAuth: false`): logged out, it renders its own "Trinity Mobile" password form instead
of redirecting to `/login`. It signs in as the fixed user `admin` and shares the session with the
desktop app — the same token in `localStorage`, so a browser already signed in on the desktop
skips the form.

It is dark by design: on mount it adds the `dark` class to `<html>` whatever the saved theme is.
Three bottom tabs: **Agents** (fleet list, start/stop, autonomy, chat, logs), **Ops** (sub-tabs
"Queue" and "Alerts") and **System** ("Fleet Health" counters and four fleet-wide "Actions").
The page re-polls the visible tab every 15 seconds.

**This phase is observe-only.** Do not press "Stop" / "Start", the "Mode" button, any queue
option, "Got it", "Send", "Acknowledge", or any of the four "Actions" buttons.

## Prerequisites

- [ ] Viewport 390 × 844 for Steps 1–10, 320 × 568 for Steps 11–12. No other widths
- [ ] `ADMIN_PASSWORD` from `.env` is at hand
- [ ] `$TOKEN` holds an admin session token for the API cross-checks (`POST http://localhost:8000/api/token`, form-encoded). It is a separate session from the browser's
- [ ] A persistent browser profile can autofill the password field and can still hold a desktop session. Clear the stored session first (Setup), and if the field still arrives pre-filled, empty it by hand before Step 2

## Setup

1. On `http://localhost/`, record `localStorage.getItem('trinity-theme')` (`null` = not set).
2. Clear the stored session. If a session exists, sign out: user menu (avatar, top right) → "Sign out". Then confirm `localStorage.getItem('token')` is `null`; if it is not, run `localStorage.removeItem('token'); localStorage.removeItem('user')` and reload. Do **not** call `localStorage.clear()` — it would also wipe the per-browser onboarding and layout keys other phases rely on.
3. If the recorded theme is not `light`, run `localStorage.setItem('trinity-theme','light')` so Step 1 can prove the page forces dark. Cleanup restores the recorded value.
4. Resize to 390 × 844.

## Test: Inline login
### Step 1: Logged-out `/m` shows its own form
**Action**:
- Navigate to `http://localhost/m`

**Expected**:
- [ ] The URL stays `/m` — no redirect to `/login`
- [ ] A centred logo with the heading "Trinity Mobile", one password field with placeholder "Admin password", and a "Sign In" button. There is no username field, no email option and no NavBar
- [ ] The background is dark and `document.documentElement.classList.contains('dark')` is `true`, while `localStorage['trinity-theme']` still reads `light`
- [ ] Browser tab title is `Trinity — Mobile`

### Step 2: Empty field, then sign in
**Action**:
- With the field empty, look at "Sign In"
- Type `ADMIN_PASSWORD` from `.env` and tap "Sign In"

**Expected**:
- [ ] "Sign In" is disabled while the field is empty
- [ ] While submitting the button reads "Signing in..."
- [ ] The form is replaced in place (URL still `/m`) by a header reading "Trinity" with two icon buttons on its right, a content area, and a bottom bar with three tabs: "Agents", "Ops", "System"
- [ ] "Agents" is the active tab

## Test: Agents tab
### Step 3: Fleet list
**Action**:
- Wait for the list (grey placeholder rows first, then cards)

**Expected**:
- [ ] A search field with placeholder "Search agents..."
- [ ] One card each for `test-echo`, `test-counter` and `test-delegator`, each with a status dot, the word `running`, and a "Stop" button on the right
- [ ] `trinity-system` is not listed (system agents are filtered out of this tab)
- [ ] A card may also show an "AUTO" badge and a thin success-rate bar with a percentage — record what each fixture card shows
- [ ] Neither "Couldn't load agents" nor "The fleet list is admin-only on mobile." appears

**Verify**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/ops/fleet/status \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['summary']); [print(a['name'], a['status'], a['is_system']) for a in d['agents']]"
```
- [ ] Every agent with `is_system` false has a card, with the same status

### Step 4: Search
**Action**:
- Type `echo` into the search field, then `zzz-none`, then clear it

**Expected**:
- [ ] `echo` leaves only `test-echo`
- [ ] `zzz-none` shows "No agents found"
- [ ] Clearing the field restores the full list

### Step 5: Expand a card (read-only parts)
**Action**:
- Tap the `test-echo` card body (not the "Stop" button)
- Tap "Load" in the "Logs" row
- Tap the card body again

**Expected**:
- [ ] The card expands to three rows: "Mode" (a button reading "AUTO" or "Manual" — do not tap), "Chat" (button "Open Chat") and "Logs" (link "Load")
- [ ] "Load" shows a block of recent log text inside the card (capped in height) and the link changes to "Refresh"
- [ ] The second tap collapses the card. The agent is still `running`

### Step 6: Chat overlay opens and closes without sending
**Action**:
- Expand `test-echo` again, tap "Open Chat"
- Tap "Sessions (N)", then tap it again (it now reads "Hide (N)")
- Tap the back arrow at the top left of the overlay. **Send nothing.**

**Expected**:
- [ ] A full-screen overlay headed `test-echo` with the status "Ready" and a "New" button
- [ ] A message box with placeholder "Message..." and a send button that is disabled while the box is empty
- [ ] The sessions list shows earlier sessions or "No previous sessions"
- [ ] The back arrow returns to the Agents tab with the list intact

## Test: Ops tab (observe only)
### Step 7: Queue
**Action**:
- Tap "Ops" in the bottom bar. **Do not tap anything inside a queue card.**

**Expected**:
- [ ] Two sub-tabs: "Queue" (active) and "Alerts", each with a count badge only when its count is above 0
- [ ] Either "No pending items", or one card per item showing the agent name, a priority label, a type line and the item's text. Record the number of cards
- [ ] Each card ends in one of three control sets — option buttons, a single "Got it" button, or a field with placeholder "Type response..." and a "Send" button. Record which, per card; touch none
- [ ] A "Recently ended" section may follow — record whether it is present
- [ ] "Couldn't load the queue" does not appear

**Verify**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" "http://localhost:8000/api/operator-queue?limit=100" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); i=d.get('items',d) if isinstance(d,dict) else d; print(sum(1 for x in i if x.get('status')=='pending'), 'pending of', len(i))"
```
- [ ] The pending count equals the "Queue" badge (no badge means 0)

### Step 8: Alerts and the Ops badge
**Action**:
- Tap "Alerts". **Do not tap "Acknowledge".**
- Look at the "Ops" item in the bottom bar

**Expected**:
- [ ] Either "No notifications", or cards with agent name, priority, text, a relative time and an "Acknowledge" button
- [ ] The badge on "Ops" in the bottom bar equals the "Queue" count plus the "Alerts" count (shown as "99+" above 99; no badge when both are 0)

**Verify**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" "http://localhost:8000/api/notifications?status=pending&limit=100"
```
- [ ] The number of pending notifications equals the "Alerts" badge

## Test: System tab (observe only)
### Step 9: Fleet Health and the Actions that must not be pressed
**Action**:
- Tap "System". **Press none of the four action buttons.**

**Expected**:
- [ ] Section "Fleet Health" with four counters labelled "Total", "Running", "Stopped", "High Ctx"
- [ ] The four numbers equal `summary` from the Step 3 Verify call (re-run it if more than a minute has passed). "Total" counts every agent including system agents, so it can exceed the number of cards on the Agents tab
- [ ] Section "Actions" with exactly four buttons: "Emergency Stop", "Fleet Restart", "Pause Schedules", "Resume Schedules" — all present, all enabled, none pressed
- [ ] "Fleet health and fleet actions are admin-only." does not appear

### Step 10: Tabs, deep link, reload, refresh
**Action**:
- Navigate to `http://localhost/m?tab=system`, then `http://localhost/m?tab=ops`, then `http://localhost/m?tab=nope`
- On `/m?tab=ops`, reload the page
- Navigate to `http://localhost/m` (no query), tap "System", reload
- Tap the first of the two icon buttons in the header (refresh)

**Expected**:
- [ ] `?tab=system` and `?tab=ops` open on that tab; `?tab=nope` opens on "Agents"
- [ ] Every reload keeps the session — the password form does not come back
- [ ] Tapping a tab does not change the URL, so the reload of plain `/m` returns to "Agents"; the reload of `/m?tab=ops` stays on "Ops"
- [ ] The refresh button spins briefly and the current tab's data is still shown afterwards
- [ ] Record whether a floating "Open help chat" button is drawn over the page and whether it covers the bottom bar or a "Stop" button

## Test: Small phone (320 px)
### Step 11: No sideways overflow on any tab
**Action**:
- Resize to 320 × 568
- Visit "Agents", "Ops" → "Queue", "Ops" → "Alerts" and "System"; on each, evaluate `document.documentElement.scrollWidth <= window.innerWidth`

**Expected**:
- [ ] `true` on all four views; the page never scrolls sideways
- [ ] Agent names that are too long are truncated, not pushed under the "Stop" button
- [ ] The four "Fleet Health" counters and the four "Actions" buttons are fully inside the viewport with their labels on one or two lines, none cut off
- [ ] Queue and alert cards wrap their text inside the card

### Step 12: Tap targets are whole
**Action**:
- On each tab, inspect the bottom bar, the two header buttons, and every "Stop" button
- On "Agents", expand `test-echo` once and collapse it

**Expected**:
- [ ] All three bottom-bar items show icon and label in full and share the width equally; the bar stays pinned to the bottom while the content scrolls
- [ ] Both header buttons are fully visible and neither overlaps the "Trinity" title
- [ ] Every "Stop" button is fully visible, at least 60 px wide, and not overlapped by its card's text
- [ ] The expanded rows ("Mode", "Chat", "Logs") fit the width; label left, control right

## Test: Sign out
### Step 13: Sign out in place
**Action**:
- Resize to 390 × 844
- Tap the second (red) icon button in the header
- Reload the page, then navigate to `http://localhost/`

**Expected**:
- [ ] The "Trinity Mobile" password form returns immediately, URL still `/m`, with no confirmation prompt
- [ ] After the reload the form is still shown — the session is gone
- [ ] `http://localhost/` redirects to `/login`: signing out on `/m` ended the desktop session too

## Cleanup / Restore

1. Restore the theme recorded in Setup: `localStorage.setItem('trinity-theme', <recorded value>)`, or `localStorage.removeItem('trinity-theme')` if it was `null`. Reload — `/m` leaves the `dark` class on `<html>` until a reload.
2. Viewport back to 1440 × 900.
3. The phase ends **logged out**. Nothing else was changed: no agent was started or stopped, no autonomy mode toggled, no message sent, no queue item answered, no notification acknowledged, no fleet action run.
4. Confirm the fleet is as it was:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/ops/fleet/status \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['summary'])"
```
   - [ ] Same `running` and `stopped` counts as in Step 3

## Manual-only (not run unattended)

- **The four "Actions" buttons.** Each opens an in-page confirmation ("Emergency Stop" → "This will pause ALL schedules and stop ALL running agents. Are you sure?"; "Fleet Restart" → confirm button "Restart All"; "Pause Schedules" → "Pause All"; "Resume Schedules" → "Resume All") and then acts on the whole fleet. Only on a disposable instance.
- **"Stop" / "Start" and the "Mode" (AUTO / Manual) button** on an agent card.
- **Answering a queue item** (option → restated consequence → "Send: …", "Got it", or a typed answer) and **"Acknowledge"** on an alert — these reach a real agent.
- **Sending a chat message** from the overlay.
- **Wrong password** (the form shows an inline error under the button) — kept out of unattended runs to stay clear of login rate limits.
- **Install to home screen**, pull-to-refresh and on-screen-keyboard behaviour — need a real phone.

## Critical Validations

1. Logged-out `/m` renders its own form and never redirects to `/login`.
2. The Agents cards and the Fleet Health counters agree with `GET /api/ops/fleet/status`.
3. Queue and Alerts counts agree with the API, and no item was answered or acknowledged.
4. No sideways overflow at 320 px on any tab; bottom bar and "Stop" buttons intact.
5. The fleet summary is unchanged at the end.

## Success Criteria

- [ ] Inline login and in-place sign-out work; reload keeps the session
- [ ] Agents, Ops (Queue, Alerts) and System render and match the API
- [ ] All four fleet "Actions" are present and were not pressed
- [ ] Forced dark confirmed without the saved theme being rewritten
- [ ] 390 px and 320 px layouts are clean

## Troubleshooting

- **`/m` opens straight on the Agents tab in Step 1**: a session token is still in `localStorage` — redo Setup.
- **Password field is pre-filled**: browser autofill from a persistent profile. Empty it before the disabled-button check.
- **"The fleet list is admin-only on mobile."**: the session is not an admin one; `GET /api/ops/fleet/status` returned 403.
- **A banner starting "Couldn't refresh …" above a list**: a background refresh failed while data was on screen; use its retry control and record the detail text.
- **Desktop pages look dark after this phase**: `/m` adds the `dark` class and does not remove it on leaving; reload.
