# Phase 30: Workspace — Inbox, asks, rooms, projects

> **Purpose**: Check the Workspace Inbox (tabs, list, reading pane, empty states), the seeded room, and the Projects list and detail pages.
> **Duration**: ~15 minutes
> **Assumes**: the fixture trio is running and you are logged in as admin
> **Output**: The Inbox, a room and Projects each render an honest populated or empty state, and deep links to them survive a reload.
> **Last verified**: 2026-10-09 against source (not yet browser-run)

---

## Background

All four routes render inside the same Workspace shell (`views/Portal.vue`), with no top nav bar:

- `/workspace/inbox` — the Workspace landing page. Tabs "Action" (asks waiting on you), "Unread" (chats with new arrivals), "All" (every chat plus recent asks). A row opens in a reading pane beside the list; the URL gains `?tab=…&item=…`.
- `/workspace/r/:roomId` — a chat with more than one agent.
- `/workspace/projects` and `/workspace/projects/:projectId` — the Projects list and one project.

An admin is not the addressee of their own agents' asks, so the Action tab is normally empty for this user; operator asks live in Operations (Phase 31). This phase is **observe-only**: it sends no messages, answers no asks and creates no projects (a project can be archived but not deleted).

## Prerequisites

- [ ] Logged in as `admin` with `ADMIN_PASSWORD` from `.env`
- [ ] The workspace thread id and the room id from fixtures.json are at hand
- [ ] Browser window at least 1280 px wide to start
- [ ] Open `http://localhost/workspace`. If the stage shows "Workspace isn't available on this instance", record the whole phase as `SKIPPED (not present)` and stop.
- [ ] If a sign-in card appears instead with a "Continue as …" button naming the admin account, click it once and continue. Do not enter an email.

---

## Test: Inbox

### Step 1: Landing and header
**Action**:
- Open `http://localhost/workspace`.

**Expected**:
- [ ] URL becomes `/workspace/inbox` without a conversation flashing first
- [ ] Heading "Inbox" with the line "What needs you, and what came back, across your agents."
- [ ] Three tabs: "Action", "Unread", "All"
- [ ] The sidebar's "Inbox" row is shown as current
- [ ] A theme control is present at the right of the Inbox header

### Step 2: Action tab
**Action**:
- Click "Action".

**Expected**:
- [ ] URL carries `?tab=action`
- [ ] If no asks are waiting: the list shows "Nothing needs your answer", the line "Questions and approvals your agents address to you land here. Asks for the operator live in Operations." and a link "Open Operations". Do not click it.
- [ ] If asks are listed: record how many and from which agents, and go to Step 5. Do not click any answer button.

### Step 3: Unread tab
**Action**:
- Click "Unread".

**Expected**:
- [ ] URL carries `?tab=unread`
- [ ] If nothing is unread: "You're all caught up" with "New replies and deliverables from your agents land here."
- [ ] If rows are listed: each shows an agent name, a chat title and a badge "N new". Record the count. Do **not** click the "Mark … read" button in the header.

### Step 4: All tab and the reading pane
**Action**:
- Click "All". Find the row for the seeded `test-echo` chat and click it.
- Then reload the page.

**Expected**:
- [ ] The list has at least one row (the seeded chat); a line above the list states the totals (for example "1 chat")
- [ ] The list footer says "Answered, expired and cancelled asks drop off after 7 days."
- [ ] Before a row is chosen the pane reads "Pick something on the left to read it here."
- [ ] After clicking: the pane shows a heading for that chat and an "Open in chat" button; URL carries `?tab=all&item=thread:<id>`
- [ ] The pane body shows recent messages, or the line "Nothing new in this chat. Open it to see the whole conversation."
- [ ] After reload the same tab and the same row are still open
- [ ] The list scrolls inside its own column; the page itself does not grow

### Step 5: What an ask shows (observe-only)
**Action**:
- Only if Step 2 listed at least one ask: click one ask row and read the pane. Otherwise record `NO ASKS — NOT RUN`.

**Expected**:
- [ ] The pane shows the ask's title, the agent it came from, and its answer controls
- [ ] Record which of these are present: option buttons, a "Something else" button, a "Got it" button, a free-text field with "Send", "Discuss" (or "Continue discussion"), "Dismiss"
- [ ] Record any priority or expiry badge

**Do not** click an option, "Send", "Got it", "Discuss" or "Dismiss". These asks belong to real agents; answering wakes the agent and dismissing ends the ask.

### Step 6: "Open in chat" from the pane
**Action**:
- On the All tab, open the seeded chat's row again and click "Open in chat".
- Use the browser Back button.

**Expected**:
- [ ] Lands on `/workspace/c/<workspace thread id from fixtures.json>` with the conversation shown
- [ ] Back returns to the Inbox with the same row still selected

### Step 7: Asks tab in the right rail
**Action**:
- With the seeded chat open (`/workspace/c/<id>`), expand the right rail ("Open the conversation rail"). Record whether it was collapsed first as **ORIGINAL_RAIL**.
- Read the rail's tab names.

**Expected**:
- [ ] The rail lists tabs (for example "Work", "Files", "Info"); record the full list
- [ ] An "Asks" tab appears only while this agent has an ask waiting on you. If absent, record `NOT SHOWN (no asks)`. If present, open it and confirm it lists the same asks the Inbox Action tab showed; click nothing inside it.
- [ ] Collapse the rail again if ORIGINAL_RAIL was collapsed

### Step 8: Inbox at 390 px
**Action**:
- Open `http://localhost/workspace/inbox?tab=all` and resize to 390 × 844.
- Tap the first row. Then tap "Back" in the pane.

**Expected**:
- [ ] Only the list is shown at first; a "Menu" button is at the left of the Inbox header
- [ ] Tapping a row replaces the list with the pane (the list is not visible beside it)
- [ ] The pane has a "Back" button and, for a chat, "Open in chat"
- [ ] "Back" returns to the list, still on `/workspace/inbox`
- [ ] No sideways scroll in either state
- [ ] Resize back to 1280 px wide

---

## Test: Rooms

### Step 9: Open the seeded room
**Action**:
- Open `http://localhost/workspace/r/<room id from fixtures.json>`.

**Expected**:
- [ ] If the stage reads "This conversation isn't available on this instance", record `SKIPPED (not present)` for Steps 9–10 and continue at Step 11
- [ ] Otherwise the header shows the room's name and, under it, small avatars plus a comma-separated list of the agents in the room (or "no agents yet"). Record the list.
- [ ] A "+ Add agent" button is in the header. Do not click it.
- [ ] The transcript area shows the room's messages, or is empty if the seed posted none. Record which.
- [ ] The message field's placeholder starts "Message" and names the participants
- [ ] A theme control is at the right of the header

**Verify** (API, read-only — skip if the room was recorded as not present):
```bash
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/rooms/<room id from fixtures.json>
```
- [ ] Prints `200`

### Step 10: Room scroll, reload and legacy link
**Action**:
- Reload the page.
- Open `http://localhost/sessions/<room id from fixtures.json>`.

**Expected**:
- [ ] After reload the same room is shown at the same URL
- [ ] The transcript scrolls inside its own area; the header and message field stay fixed and the page itself does not scroll
- [ ] The newest message (if any) is in view on open
- [ ] The `/sessions/…` link lands on `/workspace/r/<same id>` showing the same room

**Do not** type in or send from the room's message field: a room message wakes its agents, and unsent text is saved as a draft.

---

## Test: Projects

### Step 11: Projects list
**Action**:
- Open `http://localhost/workspace/projects`.

**Expected**:
- [ ] If the stage reads "Projects aren't available here", record `SKIPPED (not present)` for Steps 11–12, confirm the "Back to chats" button leads to `/workspace…`, and go to Cleanup
- [ ] Otherwise: heading "Projects", buttons "Import" and "New project", a "Show archived" toggle and a search field with placeholder "Search by name"
- [ ] The sidebar shows a Projects link, marked current
- [ ] If there are no projects: "No projects yet" with the explanatory paragraph and a second "New project" button
- [ ] If projects are listed: each row shows a name, "N of your chats" and "Updated …". Record the count.

**Do not** click "New project" or "Import". A project cannot be deleted, so this phase creates none.

### Step 12: Project detail
**Action**:
- Open `http://localhost/workspace/projects/sweep-does-not-exist`.
- If Step 11 listed at least one project, go back and click the first row; then reload.

**Expected**:
- [ ] The unknown id shows "This project isn't available to you" and a "Back to Projects" button, which returns to `/workspace/projects`
- [ ] For a real project: URL is `/workspace/projects/<id>`; the page shows the project name, a "Projects" breadcrumb link, and tabs "Overview", "Tasks", "Log", "Files & reports"
- [ ] The Overview tab shows sections "Members" and "Agents"
- [ ] After reload the same project is shown
- [ ] If no project exists, record `NO PROJECTS — detail not run`

**Do not** change the Status selector, click "Edit", "Members & access" or "Archive".

---

## Cleanup / Restore

- Nothing was created, sent, answered, dismissed or marked read.
- Right rail: return it to **ORIGINAL_RAIL** (collapsed or open) if Step 7 changed it.
- Window width back to 1280 px or wider.
- If a row was opened in the Inbox Unread tab, note that opening a chat can clear its unread count; record which chat, if any.

## Critical Validations

1. Bare `/workspace` lands on `/workspace/inbox`, and each of the three tabs shows either rows or its own empty-state text — never a blank list.
2. An opened Inbox row is restored from the URL (`?tab=…&item=…`) after reload.
3. At 390 px the Inbox shows list **or** pane, never both, and "Back" returns to the list.
4. The seeded room opens by URL with its participants named, or reports clearly that rooms are not present.
5. An unknown project id shows "This project isn't available to you" rather than an empty or broken page.

## Success Criteria

- [ ] Inbox header, tabs and empty states match the quoted text
- [ ] Reading pane opens, deep-links and survives reload
- [ ] Asks were observed only; no answer, discuss or dismiss control was clicked
- [ ] Room renders (or is recorded as not present); legacy `/sessions/<id>` link resolves to it
- [ ] Projects list and unknown-project state render (or are recorded as not present)
- [ ] 390 px check passed with no sideways scroll

## Troubleshooting

- **Inbox list shows "Couldn't load your chats" / "Couldn't load your asks"**: the list request failed; use the retry control once and record the detail text.
- **Seeded chat missing from the All tab**: the All tab lists chats, not rooms — its footer says "Rooms aren't in the Inbox yet. Open them from the sidebar." when rooms exist. Check you are looking for the thread id, not the room id.
- **Room URL shows "Start a new chat" button**: rooms are not present on this instance; record `SKIPPED (not present)`.
- **No Asks tab in the rail**: expected whenever the open agent has no ask waiting on the signed-in user.
