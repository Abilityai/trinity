# Phase 21: Chat Session Management

> **Purpose**: Verify the Chat tab's session selector and `New Chat` button — starting a session, switching between sessions, what survives a reload — and the read-only session endpoints.
> **Duration**: ~12 minutes
> **Assumes**: the fixture trio is running and you are logged in as admin
> **Output**: A new chat session is created by the first message after `New Chat`, earlier sessions stay selectable with their history intact, and the API agrees with what the selector shows
> **Last verified**: 2026-10-09 against source (not yet browser-run)

---

## Background

An agent's **Chat** tab (`components/ChatPanel.vue`) keeps conversations as persistent chat sessions. Its header has a session selector (a dropdown listing each session by start time and message count), a model selector, and a `New Chat` button.

Three behaviours worth knowing before running this:
- `New Chat` only clears the panel. A session row is created when the **first message** is sent, not when the button is clicked. There is no `POST …/chat/session/new` endpoint.
- The selected session is **not** remembered. On page load the panel selects the most recently active session that is still `active`.
- The panel has no close or delete control for a session. Closing exists only as an API call, used here in Cleanup on the one session this phase creates.

Continuous, long-running conversation lives in the Workspace; the agent header's `Workspace` button is the door to it.

Replaces the January flow that said sessions had no UI and drove them through a Terminal tab.

## Prerequisites

- [ ] Logged in as `admin` with `ADMIN_PASSWORD` from `.env`
- [ ] `test-echo` shows `Running`
- [ ] `$TOKEN` holds an admin Bearer token

This phase sends **2 messages** to `test-echo`.

## Setup

### Step 1: Record the sessions that already exist
**Action**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/agents/test-echo/chat/sessions \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('S0 =', d['session_count']); [print(s['id'], s['status'], s['message_count'], s['started_at']) for s in d['sessions']]"
```

**Expected**:
- [ ] HTTP 200 with `agent_name`, `session_count`, `sessions`
- [ ] Record `S0` and the list of existing ids as `IDS_BEFORE` (the seed script normally left one)
- [ ] Sessions are ordered newest activity first

---

## Test: Chat header and a fresh session

### Step 2: Open the Chat tab
**Action**:
- Navigate to `http://localhost/agents/test-echo?tab=chat`
- Click the session selector (the button at the top left of the chat panel)

**Expected**:
- [ ] The header shows the session selector, a model selector reading `Default model` (or a model name), and a `New Chat` button
- [ ] The selector's label is a relative time (`Just now`, `12m ago`, `3h ago`, `2d ago`, or a date) if an active session was auto-selected, otherwise `New Conversation` — record it
- [ ] The dropdown lists `S0` entries, each with a relative start time and `N msg` / `N msgs`; the selected one is highlighted. If `S0` is 0 it reads `No previous sessions`
- [ ] If a session was auto-selected, its earlier messages are shown in the panel

### Step 3: New Chat clears the panel
**Action**:
- Click `New Chat`

**Expected**:
- [ ] The selector label becomes `New Conversation`
- [ ] The message area is empty and shows the heading `Start a Conversation` with the line `Pick a quick action below or type your own message. All activity is tracked in the Dashboard timeline.`
- [ ] The input has placeholder `Type your message or / for playbooks…` and is focused

**Verify** (API): the count is still `S0` — the button created nothing.
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-echo/chat/sessions \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['session_count'])"
```

### Step 4: First message creates the session
**Action**:
- Type `sweep21 alpha` and press Enter
- Wait up to 60 s for the reply

**Expected**:
- [ ] The user message appears immediately, followed by a working indicator (`Thinking...` or a tool/status label)
- [ ] The reply contains `Echo: sweep21 alpha`, `Words: 2` and `Characters: 13`
- [ ] After the reply the selector label changes from `New Conversation` to a relative time (`Just now`)

**Verify** (API):
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-echo/chat/sessions \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['session_count']); s=d['sessions'][0]; print('NEW_ID =', s['id'], s['status'], s['message_count'])"
# Expect S0 + 1; the first entry is new (id not in IDS_BEFORE), status active, message_count 2.
# Record its id as NEW_ID.
```

### Step 5: Second message stays in the same session
**Action**:
- Type `sweep21 beta` and press Enter; wait up to 60 s

**Expected**:
- [ ] A reply beginning `Echo:` and containing `sweep21 beta` — record the full reply (the Chat tab sends earlier turns as context, so the echoed text may be longer than the two words)
- [ ] The panel now shows four messages in order: alpha, its reply, beta, its reply

**Verify** (API):
```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/agents/test-echo/chat/sessions/$NEW_ID \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['message_count'], d['session']['status']); [print(m['role'], '|', m['content'][:40]) for m in d['messages']]"
# Expect 4 messages, roles user/assistant/user/assistant, and the two user rows
# reading exactly "sweep21 alpha" and "sweep21 beta" (no context preamble).
# Session count is still S0 + 1.
```

---

## Test: Switching sessions

### Step 6: New Chat again, without sending
**Action**:
- Click `New Chat`, then open the session selector

**Expected**:
- [ ] The panel is empty again and the label reads `New Conversation`
- [ ] The dropdown lists `S0 + 1` entries; the top one reads `4 msgs`
- [ ] No entry is highlighted

### Step 7: Switch back to the session just created
**Action**:
- In the dropdown click the top entry (`4 msgs`)

**Expected**:
- [ ] The dropdown closes and the four messages from Steps 4–5 are restored in order
- [ ] The label shows that session's relative start time
- [ ] Reopening the dropdown shows that entry highlighted

### Step 8: Switch to an older session and back
**Action**:
- If `S0` is 0, record `SKIPPED (no earlier session)`
- Otherwise click a different entry in the dropdown, observe, then select the `4 msgs` entry again

**Expected**:
- [ ] The panel shows the older session's messages and none of the `sweep21` messages
- [ ] Selecting the `4 msgs` entry brings the four `sweep21` messages back
- [ ] No message is duplicated or lost by switching (the entry still reads `4 msgs`)

### Step 9: Tab switch keeps the chat as it was
**Action**:
- Click the `Tasks` tab, then click `Chat`

**Expected**:
- [ ] The Chat tab shows the same session and the same four messages without reloading them
- [ ] On the Tasks tab, the two `sweep21` messages appear as executions — record the trigger badge they carry

---

## Test: Reload and neighbours

### Step 10: Reload does not remember the selection
**Action**:
- If `S0` > 0: select the older session from Step 8
- Reload `http://localhost/agents/test-echo?tab=chat`

**Expected**:
- [ ] The Chat tab opens directly
- [ ] The panel auto-selects the most recently active session — the `sweep21` one — and shows its four messages, regardless of what was selected before the reload
- [ ] The selector label is that session's relative time, not `New Conversation`

### Step 11: The Workspace door
**Action**:
- In the agent header locate the `Workspace` and `Talk` buttons. Click `Workspace`. Then go back.

**Expected**:
- [ ] `Workspace` has title `Open this agent in the Workspace`; clicking it navigates in the same tab to `/workspace?agent=test-echo`
- [ ] `Talk` is present with a title beginning `Talk to this agent by voice.` — **do not click it** (it starts a voice call)
- [ ] After Back, the agent page is shown again
- [ ] The ChatPanel itself contains no "continue in Workspace" link and no close/delete control for a session — record if you find one

### Step 12: Endpoint guards
**Action**:
```bash
A=http://localhost:8000/api/agents
curl -s -w "\n%{http_code}\n" -H "Authorization: Bearer $TOKEN" $A/test-echo/chat/sessions/zzz-no-such-session
curl -s -w "\n%{http_code}\n" -H "Authorization: Bearer $TOKEN" $A/test-counter/chat/sessions/$NEW_ID
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $TOKEN" "$A/test-echo/chat/sessions?status=active"
curl -s -o /dev/null -w "%{http_code}\n" $A/test-echo/chat/sessions
```

**Expected**:
- [ ] Unknown id: `404` with detail `Chat session not found`
- [ ] A `test-echo` session requested through `test-counter`: `403` with detail `Session does not belong to this agent`
- [ ] `?status=active`: `200`
- [ ] No token: `401`

### Step 13: 390 px
**Action**:
- On the Chat tab resize to 390 × 844, open the session selector, close it, then restore 1280 × 800

**Expected**:
- [ ] The selector, the model selector and `New Chat` are all reachable; record whether the header row wraps or clips
- [ ] The dropdown opens fully on screen and its entries are readable
- [ ] The message input stays visible at the bottom of the panel

---

## Cleanup / Restore

Original state: `S0` sessions with ids `IDS_BEFORE`; this phase added exactly one (`NEW_ID`). The UI offers no way to remove it, so it is closed by API — history is kept, but it stops being the session the Chat tab auto-selects, which returns the tab to what it showed in Step 2.

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/agents/test-echo/chat/sessions/$NEW_ID/close
# Expect: {"status":"closed","session_id":"<NEW_ID>"}

curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-echo/chat/sessions \
  | python3 -c "import sys,json; [print(s['id'], s['status']) for s in json.load(sys.stdin)['sessions']]"
# NEW_ID is closed; every id in IDS_BEFORE has the status it had in Step 1.
```

- Close only `NEW_ID`. Never close an id from `IDS_BEFORE`.
- Reload `http://localhost/agents/test-echo?tab=chat` and confirm the selector label matches what Step 2 recorded (same session, or `New Conversation` if `S0` had no active session). The closed session is still listed in the dropdown.

## Manual-only (not run unattended)

- **Per-user isolation**: a non-admin sees only their own sessions in the list and gets 403 `You don't have access to this session` on another user's — needs a second account.
- **History across an agent restart**: sessions are stored by the platform, not in the container, so they should survive; needs a fixture stop/start (Phase 04 owns the one permitted restart).
- **Stopped agent**: the Chat tab shows `Agent Not Running` / `Start the agent to begin chatting.` and does not load sessions.

## Critical Validations

1. `New Chat` alone creates no session; the first message does (`session_count` goes from `S0` to `S0 + 1` only at Step 4).
2. The second message lands in the same session (`message_count` 4), stored as the bare user text.
3. Switching sessions swaps the history shown and loses nothing.
4. After a reload the panel selects the most recently active session — the previous selection is not remembered.
5. Only the phase's own session is closed in Cleanup.

## Success Criteria

- [ ] Selector, model selector and `New Chat` present; empty state text matches
- [ ] Session count and message counts match the API at Steps 3, 4 and 5
- [ ] Switching and reload behave as stated
- [ ] The four endpoint guards return 404 / 403 / 200 / 401
- [ ] `NEW_ID` closed; earlier sessions untouched; no more than 2 messages sent

## Troubleshooting

- **Label stays `New Conversation` after the reply**: the session list refresh found no `active` session. Run the Step 4 Verify command; if `session_count` did not rise, the message was not saved to a session — report it.
- **Red banner `Failed to load conversation history`**: the session detail call failed; repeat it with the Step 5 command and record the status code.
- **Red banner `Request timed out. Please try again.` or no reply within 60 s**: record it and do not resend (2-message budget). The message may still complete; check the Tasks tab.
- **Dropdown reads `Loading sessions...` indefinitely**: the list call is hanging; check `GET /api/agents/test-echo/chat/sessions` directly.
- **Reply is not in `Echo: … / Words: … / Characters: …` form**: the model deviated from the fixture's instructions; record the text and judge the step on the session and message counts.
