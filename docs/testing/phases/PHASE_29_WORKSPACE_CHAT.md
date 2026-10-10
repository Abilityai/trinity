# Phase 29: Workspace — chats, tabs, drafts

> **Purpose**: Click through the Workspace conversation surface: sidebar, opening an agent, sending a message, chat tabs, drafts, starring, theme, narrow width and deep links.
> **Duration**: ~20 minutes
> **Assumes**: the fixture trio is running and you are logged in as admin
> **Output**: A chat with `test-echo` can be started, switched, starred, archived and reopened by URL without losing typed text or position.
> **Last verified**: 2026-10-09 against source (not yet browser-run)

---

## Background

The Workspace is a standalone page (no top nav bar) at `/workspace`, rendered by `views/Portal.vue` and `components/portal/**`. It has three columns: a left **sidebar** (Inbox row, "New chat", search, Agents, chats), a central **stage** (the conversation), and a right **rail** that starts collapsed.

- A bare `/workspace` always lands on `/workspace/inbox` (Phase 30). To reach a conversation use `/workspace/a/<agent>` (agent page, new chat) or `/workspace/c/<sessionId>` (one chat).
- The top-nav "Workspace" link opens a new browser tab. In this phase, navigate by URL in the current tab instead.
- Closing a chat from its tab **archives** it; nothing is deleted. Unsent text is kept per chat in the browser's local storage.
- `test-echo` replies to any message with a fenced block: `Echo: <message>`, `Words: N`, `Characters: N`.

## Prerequisites

- [ ] Logged in as `admin` with `ADMIN_PASSWORD` from `.env`
- [ ] `test-echo` and `test-counter` are running
- [ ] The workspace thread id from fixtures.json is at hand
- [ ] Browser window at least 1280 px wide to start
- [ ] Open `http://localhost/workspace/a/test-echo`. If the stage shows "Workspace isn't available on this instance", record the whole phase as `SKIPPED (not present)` and stop.
- [ ] If a sign-in card appears instead with a "Continue as …" button naming the admin account, click it once and continue. Do not enter an email.

## Setup

- Open the theme control at the right end of the conversation header (its tooltip starts "Theme:"). Record the selected option (Light / Dark / System) as **ORIGINAL_THEME**. Close the menu with Esc.
- In the sidebar, record whether a "Starred" section exists and how many rows "Archived · N" shows (0 if the line is absent) as **ORIGINAL_ARCHIVED**.

---

## Test: Sidebar

### Step 1: Sidebar contents
**Action**:
- On `/workspace/a/test-echo`, look at the left sidebar from top to bottom.

**Expected**:
- [ ] A row labelled "Inbox" and a button "New chat"
- [ ] A search field with placeholder "Search agents and chats…" showing a small key cap inside it while empty and unfocused
- [ ] A section headed "Agents" listing `test-echo`, `test-counter` and `test-delegator`
- [ ] The `test-echo` row is marked as the current one
- [ ] Footer shows "Signed in to Trinity" and a "Keyboard shortcuts" button (keyboard icon)

### Step 2: Search focus shortcut and shortcut list
**Action**:
- Press the chord printed on the key cap in the search field (⌘/ on macOS, Ctrl+/ elsewhere).
- Type `test-ec`. Note the Agents section. Clear the field.
- Press the same chord again.
- Click the "Keyboard shortcuts" button in the sidebar footer. Read the dialog, then close it with its "Close" button.

**Expected**:
- [ ] First press moves the cursor into the search field
- [ ] While typing, the Agents section narrows to `test-echo` and a "Chats" results section appears below it
- [ ] Second press returns the cursor to the message field
- [ ] The dialog is headed "Keyboard shortcuts" and lists at least "New chat with this agent", "Search agents and chats", "Next agent" and "Previous agent", each with a key cap
- [ ] If a "Keyboard shortcut tips" panel is visible at the bottom of the right rail, record it; do **not** click its "Hide" button

---

## Test: Conversation

### Step 3: Opening an agent starts a new chat
**Action**:
- Click `test-counter` in the Agents list, then click `test-echo`.

**Expected**:
- [ ] URL becomes `/workspace/a/test-counter`, then `/workspace/a/test-echo`
- [ ] The conversation header shows the agent name and, beside it, "New chat"
- [ ] The tab strip above the thread shows a "New chat" tab as the selected one
- [ ] The cursor is in the message field without clicking it; its placeholder starts "Message test-echo…"
- [ ] No message was sent by either click

### Step 4: Send one message (message 1 of 3)
**Action**:
- In the `test-echo` new chat type `phase29 alpha` and press Enter (or click "Send").
- Watch the area under your message until the reply lands (up to 60 s).

**Expected**:
- [ ] Your message appears immediately as a bubble
- [ ] While waiting, a Work card appears next to the agent's avatar. Record its status word and whether an elapsed clock and an activity line are shown.
- [ ] The Send control is replaced by a "Stop this turn" control while the turn runs, and returns afterwards
- [ ] A reply appears containing `Echo: phase29 alpha`, `Words: 2`, `Characters: 13`
- [ ] The Work card is gone once the reply is shown, and no "Steps could not be read" text appears together with a live activity line
- [ ] URL is now `/workspace/c/<id>`. Record the id as **CHAT_A**.
- [ ] The "New chat" tab has become a tab for this chat (record its label)

### Step 5: Code block rendering
**Action**:
- Look at the reply from Step 4.

**Expected**:
- [ ] If the reply is drawn as a code block, it has a header bar with a "Copy" button and the text wraps inside the bubble (no horizontal page scroll). Click "Copy" once and record what the button says next.
- [ ] If the reply is drawn as plain text instead, record `NOT PRODUCED` for this step

### Step 6: Reply to a message
**Action**:
- Hover the agent's reply and click "Reply to this message".
- Do not send. Click "Don't reply to this message" on the chip.

**Expected**:
- [ ] A chip appears above the message field quoting the start of the reply
- [ ] After dismissing, the chip is gone and the message field is still focused or focusable
- [ ] If no "Reply to this message" control appears on hover, record `NOT SHOWN`

### Step 7: Star on first click
**Action**:
- In the conversation header click "Star this chat" once.

**Expected**:
- [ ] The control immediately reads "Unstar this chat" and stays that way after 3 seconds
- [ ] The sidebar gains a "Starred" section containing this chat
- [ ] The chat's tab shows a star mark

---

## Test: Tabs and drafts

### Step 8: Second chat and unread count (message 2 of 3)
**Action**:
- Click "New chat" in the conversation header (not the sidebar button).
- Type `phase29 beta` and press Enter. Immediately click the **CHAT_A** tab.
- Wait up to 60 s, watching the other tab.

**Expected**:
- [ ] A new "New chat" tab appeared and the cursor was in the message field
- [ ] Both tabs have the same width
- [ ] Switching to CHAT_A shows `Echo: phase29 alpha` again; the header band does not flash
- [ ] If the reply to `phase29 beta` lands while CHAT_A is open, its tab shows a numeric badge and its accessible name ends "N unread". If the reply landed before you switched, record `UNREAD NOT PRODUCED`.
- [ ] Click the second tab: the badge clears and the reply `Echo: phase29 beta` is shown. Record its URL id as **CHAT_B**.

### Step 9: Unsent text survives a tab switch
**Action**:
- In CHAT_B type `draft for B` (do not send). Click the CHAT_A tab. Type `draft for A` (do not send).
- Click the CHAT_B tab, then the CHAT_A tab.

**Expected**:
- [ ] CHAT_B's field still holds `draft for B`; CHAT_A's holds `draft for A`
- [ ] Each tab that holds unsent text carries a small draft mark
- [ ] The `test-echo` row in the sidebar carries a draft mark

### Step 10: Unsent text survives an agent switch and a reload
**Action**:
- With `draft for A` still in CHAT_A, click `test-counter` in the sidebar, then open `http://localhost/workspace/c/<CHAT_A>` by URL.
- Reload the page.

**Expected**:
- [ ] `draft for A` is still in the message field after the agent switch
- [ ] After reload the same chat is open (URL unchanged), `Echo: phase29 alpha` is visible and the draft is still there
- [ ] The chat is still starred

### Step 11: Close a chat from its tab
**Action**:
- Clear the message field in CHAT_B (select all, delete), then click the × on the CHAT_B tab (its label is "Archive chat").
- When the toast appears, click "Undo". Then click × on CHAT_B again and let the toast expire.

**Expected**:
- [ ] The tab pinned as "Main", if present, has no ×
- [ ] First close: the tab disappears, the view moves to a neighbouring tab, and a toast "Chat archived" with "Undo" appears at the bottom
- [ ] The sidebar shows "Archived · N" with N one higher than ORIGINAL_ARCHIVED
- [ ] "Undo" brings the CHAT_B tab back and the Archived count returns to its previous value
- [ ] Second close archives it again

---

## Test: Position, theme, width, links

### Step 12: Opens at the newest message
**Action**:
- Open `http://localhost/workspace/c/<workspace thread id from fixtures.json>`.

**Expected**:
- [ ] The seeded chat opens directly, with `test-echo` in the header and no "This chat isn't available" message
- [ ] The newest message is in view without scrolling
- [ ] If the transcript is long enough to scroll: scroll to the top, confirm the view stays where you put it for 10 s, then scroll back to the bottom. Otherwise record `TOO SHORT TO SCROLL`.

### Step 13: Theme switch
**Action**:
- Click the theme control at the right end of the conversation header. Choose "Dark", reopen it and choose "Light", then choose "System".

**Expected**:
- [ ] The menu is headed "Theme" and offers exactly "Light", "Dark", "System"
- [ ] Dark: sidebar, header, bubbles, message field and tab strip all turn dark; all text stays readable
- [ ] Light: the same areas turn light
- [ ] System: the control's label reads "System · light" or "System · dark"
- [ ] The header row does not change height or reflow when the menu opens

### Step 14: Compact header at 390 px
**Action**:
- Resize the window to 390 × 844 with a chat open.
- Click the "Menu" button at the left of the header, then press Esc or click outside the drawer.

**Expected**:
- [ ] The sidebar is hidden and a "Menu" button appears in the header
- [ ] The theme control shows an icon only; the "New chat" header button shows an icon only
- [ ] The message field and Send control are fully visible; the page does not scroll sideways
- [ ] "Menu" opens the sidebar as a drawer over the chat; it closes again
- [ ] Resize back to 1280 px wide before continuing

### Step 15: Legacy links
**Action**:
- Open each URL in turn and record where it lands:
  1. `http://localhost/portal`
  2. `http://localhost/sessions`
  3. `http://localhost/portal?agent=test-echo`
  4. `http://localhost/agents/test-echo/workspace`
  5. `http://localhost/portal/c/<workspace thread id from fixtures.json>`

**Expected**:
- [ ] 1 and 2 land on `/workspace/inbox`
- [ ] 3 and 4 land inside `/workspace…` on a `test-echo` conversation (the `agent` selection survived the redirect), not on the Inbox and not on a "You don't have access to" message
- [ ] 5 lands on `/workspace/c/<that id>` showing the seeded chat
- [ ] None of them shows the platform top nav or a 404 page

---

## Cleanup / Restore

- Open `/workspace/c/<CHAT_A>`. Clear the message field (the draft `draft for A`). Click "Unstar this chat" (it was unstarred before this phase).
- Click × on the CHAT_A tab to archive it. CHAT_B was archived in Step 11.
- Open `/workspace/a/test-echo` and `/workspace/a/test-counter`; if either message field holds text typed by this phase, clear it. No draft mark should remain on any agent row.
- Set the theme control back to **ORIGINAL_THEME**.
- Left behind on purpose: two archived `test-echo` chats (CHAT_A, CHAT_B). There is no delete control; record both ids in the report. "Archived · N" should read ORIGINAL_ARCHIVED + 2.
- Messages sent: 2 of the 3 allowed.

## Critical Validations

1. Clicking an agent in the sidebar opens a new chat with the cursor already in the message field.
2. A sent message gets an `Echo:` reply and the URL becomes `/workspace/c/<id>`; reloading that URL reopens the same chat.
3. Text typed and not sent is still there after switching tabs, switching agents and reloading.
4. The star toggles on the first click and stays toggled.
5. Closing a tab archives the chat, moves to a neighbour, and "Undo" restores it.

## Success Criteria

- [ ] Sidebar, search shortcut and shortcut list behave as described
- [ ] Two chats were created with one message each; replies matched the echo format
- [ ] Tabs switch without losing drafts; draft marks appear
- [ ] Theme switch applies all three options to the whole page
- [ ] No sideways scroll at 390 px
- [ ] All five legacy links land in the Workspace
- [ ] Cleanup done; no star, draft or theme change left behind

## Troubleshooting

- **Lands on the Inbox instead of a chat**: a bare `/workspace` always redirects to `/workspace/inbox`. Use `/workspace/a/test-echo`.
- **"This chat isn't available"**: the id in the URL is not one of the signed-in user's chats. Re-check the id against fixtures.json.
- **"You don't have access to test-echo"**: the agent is not on this user's Workspace list. Confirm `test-echo` is running and listed under Agents.
- **× on a tab is greyed out**: a reply is still in flight in that chat; its tooltip reads "Wait for the reply to finish, then archive this chat".
- **Shortcut does nothing**: the chord differs by platform; use the one printed on the key cap in the search field.
