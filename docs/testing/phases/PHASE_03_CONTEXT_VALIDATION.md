# Phase 03: Tasks & Chat on One Agent

> **Purpose**: Run one task and one chat message against `test-echo` through the agent page, and check the execution record and the context figures that go with them.
> **Duration**: ~10 minutes
> **Assumes**: the fixture trio is running and you are logged in as admin
> **Output**: A pass proves the Tasks composer and the Chat tab both reach the agent, each produces an execution row, the execution detail page renders, and context usage is reported where the UI shows it.
> **Last verified**: 2026-10-09 against source (not yet browser-run)

---

## Background

An agent's page (`/agents/:name`) has a **Tasks** tab (`components/TasksPanel.vue`) — a
composer plus the list of every execution (chats, tasks, schedules, agent-to-agent) —
and a **Chat** tab (`components/ChatPanel.vue`) for stateless per-turn chat. Each
execution opens at `/agents/:name/executions/:executionId` ("Execution Details"). Tabs
deep-link with `?tab=<id>`.

Replaces the January flow, which claimed chat was not exposed in the UI and looked for
a context percentage in the agent header. There is no header context bar now. Context
is shown in three places: a small bar + percentage on a task row (only when the
execution recorded it), a "Context" card on the execution detail page, and the fleet
endpoint `GET /api/agents/context-stats` that the Dashboard polls.

`test-echo` replies to any message in this exact shape:
```
Echo: <the message>
Words: <n>
Characters: <n>
```

## Prerequisites

- [ ] Logged in as `admin` (password is `ADMIN_PASSWORD` from `.env`)
- [ ] `test-echo` is running
- [ ] `fixtures.json` is available (for the seeded `test-echo` execution id)
- [ ] A Bearer token for API checks, referred to as `$TOKEN`
- [ ] Message budget for this phase: 2 (one task, one chat). Do not use the re-run button.

## Test: Tasks tab

### Step 1: Tasks tab renders with history
**Action**:
- Navigate to `http://localhost/agents/test-echo?tab=tasks`

**Expected**:
- [ ] The Tasks tab is active; heading "Tasks" with the line "All executions — chats, tasks, schedules, and agent-to-agent"
- [ ] A trigger filter defaulting to "All triggers", an "Idle" or "Busy" pill, and a **Refresh** button
- [ ] Four summary tiles: "Total", "Success Rate", "Total Cost", "Avg Duration"
- [ ] The composer: a "Model" selector, a "Timeout" select (default shown; options 5 min … 2 hours), a textarea with placeholder "Enter task message... (Enter to run, Shift+Enter for newline)" and a **Run** button
- [ ] **Run** is disabled while the textarea is empty
- [ ] The list below has at least one row (the seeded execution) — "No tasks yet" must NOT be shown

### Step 2: Run one task
**Action**:
- Type `phase three task` in the textarea
- Click **Run**

**Expected**:
- [ ] A new row appears at the top of the list at once, with the message `phase three task`, a trigger badge `manual` and a status badge `running` (or `queued`)
- [ ] The textarea is cleared
- [ ] While it runs, the row offers a green "Live" link (title "View live execution")
- [ ] Within 60 seconds the status badge becomes `success`

### Step 3: Read the result inline
**Action**:
- Click the message text of the new row (or the chevron button titled "Expand")

**Expected**:
- [ ] The row expands and shows the response containing `Echo: phase three task`, `Words: 3` and `Characters: 16`
- [ ] The row's stats line shows a duration; record whether a model name, a cost and a context bar with a percentage are shown (each is rendered only when the execution recorded that value)
- [ ] Clicking again (button title "Collapse") collapses the row

### Step 4: Row actions are present
**Action**:
- Hover the action buttons at the right of the completed row and read their titles. Do not click "Re-run this task" or "Create schedule from this task".

**Expected**:
- [ ] Buttons titled "Open execution details", "View execution log (modal)", "Copy task input", "Re-run this task", "Create schedule from this task" and "Expand"
- [ ] There is no "Stop execution" button on a finished row

### Step 5: Open the execution detail page
**Action**:
- Click the button titled "Open execution details" on the new row

**Expected**:
- [ ] The URL is `http://localhost/agents/test-echo/executions/<id>`
- [ ] Heading "Execution Details" with a status pill `success`; beneath it a link to the agent and the first 8 characters of the id
- [ ] Cards labelled "Duration", "Cost", "Context" and "Triggered By"; the Context card reads `<used> / <max>`
- [ ] A "Task Input" section containing `phase three task`
- [ ] A "Response Summary" section containing `Echo: phase three task`
- [ ] An "Execution Transcript" section with at least one entry
- [ ] Record whether a **Continue as Chat** button is shown — do not click it

**Verify** (API):
```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/agents/test-echo/executions/<id>" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status'), d.get('triggered_by'), d.get('context_used'), d.get('context_max'))"
# success manual <n> <n>
```

### Step 6: Back link returns to the Tasks tab
**Action**:
- Click the arrow button titled "Back to Tasks"

**Expected**:
- [ ] The URL is `http://localhost/agents/test-echo?tab=tasks` and the Tasks tab is active with the new row still at the top

### Step 7: A seeded execution opens by direct link
**Action**:
- Navigate to `http://localhost/agents/test-echo/executions/<test-echo task execution id from fixtures.json>`

**Expected**:
- [ ] "Execution Details" renders with a status pill, the four cards and a "Task Input" section — not "Failed to load execution"

### Step 8: Trigger filter
**Action**:
- Back on `http://localhost/agents/test-echo?tab=tasks`, set the trigger filter to "Manual", then to "Schedule", then back to "All triggers"

**Expected**:
- [ ] With "Manual", every listed row carries the `manual` badge and the `phase three task` row is present
- [ ] With "Schedule", the `phase three task` row is absent (the list may be empty — "No tasks yet" is acceptable here)
- [ ] "All triggers" restores the full list

## Test: Chat tab

### Step 9: Chat tab renders
**Action**:
- Navigate to `http://localhost/agents/test-echo?tab=chat`
- If the conversation area already shows messages, click **New Chat**

**Expected**:
- [ ] The Chat tab is active; the bar above the conversation shows a session picker (reading "New Conversation" for a fresh chat), a model selector and a **New Chat** button
- [ ] The empty conversation shows "Start a Conversation" and the line beginning "Pick a quick action below or type your own message."
- [ ] The input has the placeholder "Type your message or / for playbooks…" and the send button (paper-plane icon) is disabled while the input is empty
- [ ] "Agent Not Running" is NOT shown

### Step 10: Send one message and see the echo
**Action**:
- Type `phase three chat` and press Enter

**Expected**:
- [ ] Your message appears as a user bubble at once and a working/loading indicator shows while the agent replies
- [ ] Within 60 seconds an agent reply appears containing `Echo: phase three chat`, `Words: 3` and `Characters: 16`
- [ ] The input is empty and enabled again; no error banner is shown

### Step 11: The chat turn is recorded as an execution
**Action**:
- Navigate to `http://localhost/agents/test-echo?tab=tasks`

**Expected**:
- [ ] A row with the message `phase three chat` is at or near the top with status `success`
- [ ] Record its trigger badge. The Chat tab submits through the same task endpoint as the composer, so `manual` is the value source predicts; `chat` is also acceptable. Any other value is a finding
- [ ] Both the `phase three task` and the `phase three chat` rows are present

## Test: Context figures and layout

### Step 12: Fleet context endpoint
**Action**:
- Run the check below

**Verify** (API):
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/context-stats \
  | python3 -c "import sys,json; [print(a['name'], a.get('activityState'), a.get('contextPercent'), a.get('contextUsed'), a.get('contextMax')) for a in json.load(sys.stdin)['agents'] if a['name'].startswith('test-')]"
```

**Expected**:
- [ ] The response is an object with an `agents` array containing an entry for each of `test-echo`, `test-counter`, `test-delegator`
- [ ] Each entry has `name`, `activityState`, `contextPercent`, `contextUsed`, `contextMax`
- [ ] `contextPercent` is a number between 0 and 100. Record the `test-echo` values; do not assert a specific percentage (task and chat turns here are stateless)

### Step 13: Narrow viewport (390 px) and dark theme
**Action**:
- On `http://localhost/agents/test-echo?tab=tasks`, resize to 390 × 844
- Open the user menu (avatar button, top right), choose **Dark** under "Theme", look at the Tasks tab, then choose the theme that was active before
- Resize back to ≥ 1280 px

**Expected**:
- [ ] At 390 px the page does not scroll horizontally; the textarea and **Run** are both visible and usable; row action buttons are reachable
- [ ] In dark theme the status and trigger badges, the summary tiles and the composer all have readable contrast (no dark-on-dark or white panels)

## Cleanup / Restore

- Two executions were added to `test-echo` (`phase three task`, `phase three chat`). They are history and are left in place.
- If the theme was changed in Step 13, set it back (record the original value; default is System).
- No setting, schedule or file was changed. Nothing else to restore.

## Manual-only (not run unattended)

- Stopping a long-running task with "Stop execution" (needs a task that runs long enough to catch).
- The "compacted" badge on a row (needs a conversation large enough to trigger history compaction).
- The "Agent must be running to execute tasks" / "Agent Not Running" states (would require stopping a fixture).

## Critical Validations

1. A task typed into the Tasks composer appears in the list and reaches `success` within 60 s (Step 2).
2. Its response contains `Echo: phase three task` (Step 3).
3. The execution detail page renders heading, cards, input and response for that execution (Step 5).
4. One chat message gets an `Echo:` reply in the Chat tab (Step 10) and shows up as an execution row (Step 11).
5. `GET /api/agents/context-stats` returns an entry per fixture with the five context fields (Step 12).

## Success Criteria

- [ ] Tasks tab renders with composer, tiles and history
- [ ] One task run end to end, result readable inline and on the detail page
- [ ] Chat tab sends one message and shows the echo
- [ ] Both turns appear as execution rows; trigger badges recorded
- [ ] Context figures present on the detail page and in the fleet endpoint
- [ ] Exactly two messages were sent to `test-echo`

## Troubleshooting

- **"Agent must be running to execute tasks" under the composer / "Agent Not Running" in Chat**: `test-echo` is not running — record `BLOCKED (fixture not running)`; do not start it from this phase.
- **Row stays `running` past 60 s**: open its "Live" link and read the transcript; record the last entry. Do not send a second task.
- **Reply is not in the `Echo:` shape**: the fixture's instructions are in `config/agent-templates/test-echo/CLAUDE.md`; record the actual reply — a different shape is a fixture defect, not a UI one.
- **"Couldn't load task history"**: the executions fetch failed; use its retry control once and record the detail text.
- **Row marked `failed` with an authentication message**: the agent has no working model credential; record `BLOCKED (agent auth)` with the message.
