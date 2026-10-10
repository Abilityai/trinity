# Phase 36: Onboarding checklist and app chrome

> **Purpose**: Check the top navigation bar, theme controls, user menu, per-route browser-tab titles and the catch-all route, plus the two onboarding surfaces: the "Getting started" checklist and the re-openable first-run overlay.
> **Duration**: ~20 minutes
> **Assumes**: the fixture trio is running and you are logged in as admin
> **Output**: the chrome every page shares behaves at wide and narrow widths, theme choice survives a reload, and onboarding state is read, exercised and put back
> **Last verified**: 2026-10-09 against source (not yet browser-run)

---

## Background

- **NavBar** (`components/NavBar.vue`, links from `utils/navLinks.js`): logo, five links (Dashboard, Library, Operations, Settings, Workspace), a connection dot, a version chip, a Documentation link, a theme button and the user menu that owns "Sign out". Links that do not fit collapse into a counted "N more" menu; below 640 px the link row is not rendered at all.
- **Theme** (`stores/theme.js`): `light` / `dark` / `system`, stored per browser in `localStorage['trinity-theme']`.
- **Tab titles** (`router/index.js`): `Trinity — <route title>`; a redirect takes the title of where it lands. Unknown paths redirect to `/`.
- **"Getting started" checklist** (`components/onboarding/ActivationChecklist.vue`): not a header launcher — it is a section at the bottom of the Dashboard's left "Systems" sidebar. Its items and their done-state come from the server; only its expanded/collapsed state is local (`localStorage['trinity-checklist-expanded']`). It only exists on some instances and hides itself when every item is done.
- **First-run overlay** (`components/onboarding/FirstRunOverlay.vue`): opens by itself on a fresh install; any admin can reopen it with `/?onboarding=1` or Settings → General → "Re-run setup". Closing it writes `localStorage['trinity_first_run_closed']`.

## Prerequisites

- [ ] Logged in as `admin` with `ADMIN_PASSWORD` from `.env`; viewport 1440 × 900 unless a step says otherwise
- [ ] `$TOKEN` holds an admin session token (`POST http://localhost:8000/api/token`, form-encoded)
- [ ] Do **not** open `/m` during this phase — that page forces dark mode on the document and would corrupt the theme steps

## Setup

On `http://localhost/`, read and record the four per-browser values this phase touches (`null` means "not set"):

```js
['trinity-theme','trinity-checklist-expanded','trinity-sidebar-collapsed','trinity_first_run_closed']
  .map(k => [k, localStorage.getItem(k)])
```

If a full-screen dialog (the first-run overlay) covers the Dashboard on arrival, record that it auto-opened, then click "Finish later" and confirm with "Finish later" in the "Finish setup later?" dialog. In that case leave `trinity_first_run_closed` at `1` in Cleanup instead of restoring it.

## Test: Navigation bar
### Step 1: Items and order
**Action**:
- Open `http://localhost/` at 1440 × 900

**Expected**:
- [ ] Left: logo plus the word "Trinity", linking to `/`
- [ ] Links in this order: "Dashboard", "Library", "Operations", "Settings", "Workspace". There is no Agents, Templates, Keys or System link
- [ ] "Operations" may carry a numeric badge (pending queue items + notifications, capped at "99+") — record its value or its absence
- [ ] If a sixth link follows "Workspace", record only that it is present; do not open it
- [ ] Right: a small coloured dot, a version chip starting with `v`, a question-mark link titled "Documentation (opens docs.ability.ai)", a theme button, and a round avatar or initials button

### Step 2: Active link per route
**Action**:
- Visit `/`, `/library`, `/operations`, `/settings`, then `/agents/test-echo`
- Inspect the "Workspace" link without clicking it

**Expected**:
- [ ] Exactly one link carries the active underline on each page: Dashboard, Library, Operations, Settings respectively
- [ ] On `/agents/test-echo` the active link is "Dashboard"
- [ ] "Workspace" has `target="_blank"` and an `href` ending `/workspace` (it opens in a new tab by design)

### Step 3: Connection dot and Build Info
**Action**:
- Hover the coloured dot
- Click the version chip; read the dialog; close it with its "Close" button, reopen and close with Esc

**Expected**:
- [ ] Within ~5 s of load the dot is green with tooltip "Connected". An amber pulsing dot with tooltip "Disconnected" is a FAIL on a healthy stack
- [ ] The dialog is titled "Build Info" and lists "Version", "Branch", "Commit", "Commit subject", "Commit timestamp", "Build date"
- [ ] Both ways of closing work and focus returns to the page

**Verify**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/version
```
- [ ] `git_commit_short` and `git_branch` match the dialog

### Step 4: Browser-tab titles and the catch-all route
**Action**:
- Read `document.title` on `/`, `/library`, `/operations`, `/settings`, `/agents/test-echo`
- Navigate to `/templates`, then `/operating-room`, then `/agents`
- Navigate to `/no-such-page-36`

**Expected**:
- [ ] `Trinity — Dashboard`, `Trinity — Library`, `Trinity — Operations`, `Trinity — Settings`
- [ ] On `/agents/test-echo` the title is `Trinity — ` followed by the agent's name (record the exact string)
- [ ] `/templates` lands on `/library` titled `Trinity — Library`; `/operating-room` lands on `/operations` titled `Trinity — Operations`; `/agents` lands on the Dashboard titled `Trinity — Dashboard`
- [ ] `/no-such-page-36` redirects to `/` and shows the Dashboard — no blank page, no 404 screen, no console error
- [ ] Clicking a nav link changes the title without a page reload

## Test: Theme and user menu
### Step 5: Theme button cycles light → dark → system
**Action**:
- Click the theme button until its tooltip reads "Light mode (click to switch)"
- Click it once, then once more, reading the tooltip and `document.documentElement.classList.contains('dark')` each time

**Expected**:
- [ ] Light: sun icon, `<html>` has no `dark` class, page background is light
- [ ] One click: tooltip "Dark mode (click to switch)", moon icon, `<html>` has `dark`, NavBar and page turn dark with readable text
- [ ] Second click: tooltip "System theme (click to switch)", monitor icon; the page follows the OS preference
- [ ] `localStorage['trinity-theme']` reads `light`, `dark`, `system` in step with the tooltip

### Step 6: User menu and persistence across reload
**Action**:
- Click the avatar button
- In the menu's "Theme" group click "Dark"; click somewhere outside the menu
- Reload the page; open the menu again; click "Light"; reload again

**Expected**:
- [ ] The menu shows the user's name and email, a "Theme" group with "Light" / "Dark" / "System", a "Documentation" link and "Sign out"
- [ ] The chosen option is the highlighted one (`aria-checked="true"`) and the NavBar theme button's icon agrees with it
- [ ] Clicking outside closes the menu
- [ ] After each reload the page comes back in the chosen theme with no flash of the other one, and the menu still marks the same option

**Verify**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/users/me
```
- [ ] The email in the menu matches `email`

## Test: Narrow widths
### Step 7: Links collapse into "N more"
**Action**:
- On `/settings`, shrink the width in steps — 1024, 800, 700 — stopping at the first width where a trigger reading "1 more", "2 more"… appears. Record that width
- Click the trigger; pick "Settings" if it is in the menu, otherwise the last entry
- Press Esc with the menu open on a second try

**Expected**:
- [ ] The inline links plus the count on the trigger always add up to the full set from Step 1
- [ ] No link is cut in half and none overlaps the connection dot
- [ ] When the current page's link is inside the menu, the trigger itself carries the active underline
- [ ] Choosing an entry navigates and closes the menu; Esc closes it and returns focus to the trigger
- [ ] The theme button and the avatar stay fully on screen at every width

### Step 8: 390 px
**Action**:
- Resize to 390 × 844 on `/`
- Open the user menu

**Expected**:
- [ ] The five nav links are not shown at this width and there is no "N more" trigger (the link row only renders from 640 px up). Record this as observed — the logo is the only route control left in the bar
- [ ] Logo, connection dot, Documentation link, theme button and avatar are all visible; the version chip is hidden
- [ ] The user menu opens fully inside the viewport and "Sign out" is reachable
- [ ] No sideways page scroll (`document.documentElement.scrollWidth <= window.innerWidth`)
- [ ] Resize back to 1440 × 900: all five links return inline

## Test: "Getting started" checklist
### Step 9: Find the checklist
**Action**:
- On `/`, look at the left sidebar headed "Systems". If it is a narrow icon rail, click the button titled "Expand sidebar"
- Look below the view list for a row reading "Getting started" with a count such as `2/4`

**Expected**:
- [ ] If the "Getting started" section is not present on this instance, record `SKIPPED (not present)` for Steps 9–11 and continue at Step 12. (It is also absent once every item is done or after it was permanently dismissed.)
- [ ] If present: the count reads `<done>/<total>` and the list beneath is expanded unless Setup read `trinity-checklist-expanded` as `false`

### Step 10: Items reflect real state; one action
**Action**:
- Expand the section if collapsed. Record every item title, in order, and whether it is done
- Click the single action button, note where it lands, then return to `/`

**Expected**:
- [ ] Done items have a filled green check and struck-through text; undone items an empty ring
- [ ] The number of done items equals the first number in the header count
- [ ] Exactly one button is shown, under the first undone item
- [ ] Record whether an item about creating a first agent reads done (the fixture trio exists, so an undone one is worth a note, not a FAIL)
- [ ] The button navigates inside the app to a page that matches the item's wording (record the URL); nothing is created by the click alone

### Step 11: Collapse, reload, reopen
**Action**:
- Click the "Getting started" header row; reload; click it again
- Hover "Don't show this again" — **do not click it**

**Expected**:
- [ ] The header's `aria-expanded` flips to `false`, the list and the "Don't show this again" link disappear, and the count stays visible
- [ ] After the reload it is still collapsed (`localStorage['trinity-checklist-expanded']` is `false`)
- [ ] The second click expands it again and the value becomes `true`
- [ ] The link's tooltip reads "Hides these steps for good — they do not come back"

## Test: First-run overlay (re-opened on purpose)
### Step 12: Open from Settings and read it
**Action**:
- Go to `/settings?tab=general`, find the card "First-run setup" and click "Re-run setup"

**Expected**:
- [ ] URL becomes `/?onboarding=1` and a dialog covers the Dashboard; the page behind it does not move
- [ ] The left rail shows a progress line "`N` of `M` done", a step list and "Finish later". The list is a subset, in this order, of: "Secure this instance", "Sign-in email", "Connect Claude", "Other keys", "Your first agent", "Usage sharing" — record which appear and which read "Done"
- [ ] The footer's main button reads "Get started"; there is no "← Back" on this first screen and no X to close
- [ ] Clicking the dimmed backdrop does not close it

### Step 13: Leave without changing anything
**Action**:
- Press Esc; in the dialog "Finish setup later?" click "Keep setting up"
- Click "Finish later" in the rail, then "Finish later" in the dialog
- Reload the page

**Expected**:
- [ ] "Keep setting up" returns to the overlay, still on the welcome screen
- [ ] "Finish later" closes the overlay and removes `onboarding=1` from the URL
- [ ] The reload does not reopen it; `localStorage['trinity_first_run_closed']` is `1`
- [ ] No step was entered, so no key, email or sharing choice was touched

## Test: Sign out
### Step 14: Sign out and the guarded routes
**Action**:
- Open the user menu and click "Sign out"
- Navigate to `/settings`, then to `/no-such-page-36`

**Expected**:
- [ ] Lands on `/login`, titled `Trinity — Login`; no NavBar
- [ ] `/settings` and the unknown path both end on `/login`
- [ ] The theme chosen before signing out is still applied on the login page

## Cleanup / Restore

1. Log in again as `admin` with `ADMIN_PASSWORD` from `.env` ("Admin Login" → "Sign In as Admin").
2. Put the four per-browser values back to what Setup recorded, then reload:
```js
// for each key: value recorded in Setup, or null if it was not set
const restore = { 'trinity-theme': null, 'trinity-checklist-expanded': null,
                  'trinity-sidebar-collapsed': null, 'trinity_first_run_closed': null }
for (const [k, v] of Object.entries(restore)) v === null ? localStorage.removeItem(k) : localStorage.setItem(k, v)
```
   - Exception: if Setup found the overlay auto-opening, keep `trinity_first_run_closed` at `1`.
3. Viewport back to 1440 × 900.
4. Not restorable, and expected: finishing Step 13 records one `setup_dismissed` product event on the server. No checklist state was changed — "Don't show this again" was never clicked.

## Manual-only (not run unattended)

- **First-run overlay opening by itself**: needs a fresh install (no Claude credential, or no agent of the user's own). Walking its steps saves real keys and an email address.
- **"Don't show this again"** on the checklist: permanent for the account, stored on the server, and the app offers no way to undo it.
- **"Disconnected" state** of the connection dot: needs the backend stopped.
- **Checklist item completion changing live** (e.g. after creating a first schedule or channel).

## Critical Validations

1. Nav links, their order and the active link are right on every top-level route, and `/agents/:name` lights "Dashboard".
2. Theme choice persists across reload and both controls (bar button, menu group) agree.
3. Tab titles follow the route, including through redirects; unknown paths fall back to the Dashboard.
4. Below the fit width links move into "N more" and stay reachable; the right-hand controls never leave the screen.
5. All four per-browser values are back to their Setup readings and the session is logged in again.

## Success Criteria

- [ ] NavBar items, active state, connection dot and Build Info verified
- [ ] Theme cycles and persists in light and dark
- [ ] Titles and catch-all redirect verified
- [ ] Overflow verified at an intermediate width and at 390 px
- [ ] Checklist exercised or recorded `SKIPPED (not present)`
- [ ] Overlay opened and left with "Finish later"; state restored

## Troubleshooting

- **No version chip**: it is hidden below 1280 px, and absent when `GET /api/version` failed.
- **Theme button seems to do nothing on the third click**: `system` follows the OS; if the OS is dark it looks the same as `dark`. Trust the tooltip and `localStorage['trinity-theme']`.
- **No "Getting started" section**: not present on this instance, already complete, dismissed, or the "Systems" sidebar is collapsed.
- **Overlay does not open from "Re-run setup"**: it waits for the profile and settings reads to finish; allow ~5 s on `/?onboarding=1`.
- **Dark stuck on after visiting `/m`**: that page adds the `dark` class and does not remove it; reload any other page.
