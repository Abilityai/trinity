# Phase 20: Live Execution Streaming

> **Purpose**: Validate the Tasks panel's run / Live / Stop controls and the Execution Details page (transcript, status, navigation, not-found).
> **Duration**: ~12 minutes
> **Assumes**: the fixture trio is running and you are logged in as admin
> **Output**: a task can be run inline, its execution opens in Execution Details, a finished transcript renders and survives a refresh, and an unknown execution id fails honestly
> **Last verified**: 2026-10-09 against source (not yet browser-run)

---

## Background

Tasks are run from an inline box at the top of the agent's Tasks tab
(`components/TasksPanel.vue`) — there is no "Create Task" dialog. A running row shows a green
"Live" link and a "Stop execution" button; every server-side row links to
`/agents/:name/executions/:executionId` (`views/ExecutionDetail.vue`).

Execution Details streams a running execution with `fetch` + `ReadableStream` against
`GET /api/agents/{name}/executions/{id}/stream` (not `EventSource` — it needs the Bearer
header). For a finished execution it loads `…/executions/{id}` and `…/executions/{id}/log`.

Replaces the January flow that used `/agents`, a "Create Task" button, an EventSource check and
host `docker logs`. `test-echo` replies in a few seconds, so the Live/Stop steps are written as
"if still live … otherwise SKIPPED"; the transcript checks use the seeded execution.

This phase sends **one** task to `test-echo`.

## Prerequisites

- [ ] Logged in to `http://localhost` as `admin` with `ADMIN_PASSWORD` from `.env`
- [ ] `$TOKEN` holds an admin Bearer token
- [ ] `test-echo` is `running`
- [ ] fixtures.json provides the seeded **task execution id on `test-echo`** (call it `$EXEC`)

## Test: Tasks panel

### Step 1: Open the Tasks tab
**Action**:
- Navigate to `http://localhost/agents`, then click the `test-echo` name link in the list.
- Click the "Tasks" tab.
**Expected**:
- [ ] `/agents` lands on `/?view=list`; the agent page opens at `/agents/test-echo` on the Overview tab
- [ ] The Tasks tab shows the heading "Tasks" with "All executions — chats, tasks, schedules, and agent-to-agent"
- [ ] A trigger filter defaulting to "All triggers" and a queue pill reading "Idle" (or "Busy")
- [ ] A text box with placeholder "Enter task message... (Enter to run, Shift+Enter for newline)" and a "Run" button, disabled while the box is empty
- [ ] The history list contains at least the seeded row (not "No tasks yet")

### Step 2: Queue endpoint
**Action**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-echo/queue \
  | jq '{agent_name, is_busy, queue_length, current: (.current_execution != null)}'
```
**Expected**:
- [ ] `agent_name` is `test-echo`, `queue_length` is `0`
- [ ] `is_busy` matches the pill from Step 1 (`false` ↔ "Idle")

### Step 3: Run one task
**Action**:
- Click into the task box, type `sweep 20 live check`, and press Enter.
- Immediately take a snapshot of the top row of the history list.
**Expected**:
- [ ] The box clears and a new row appears at the top with the message `sweep 20 live check`
- [ ] The row's status badge reads `running` and its trigger badge reads `manual`
- [ ] No error notification

### Step 4: Live link and Stop button (only while running)
**Action**:
- If the top row still reads `running`: look at its action icons. **Do not click Stop.**
- If it already reads `success`: record `SKIPPED (run finished)` for this step.
**Expected** (if still running):
- [ ] A green link with the text "Live" (tooltip "View live execution") once the row has a server execution id
- [ ] A stop button with tooltip "Stop execution" may appear after the panel's next queue poll (0.5 s and 2 s after submit) — record whether it did

### Step 5: Completion
**Action**:
- Wait up to 60 s for the top row's badge to leave `running`.
- Click the row's expand chevron (tooltip "Expand").
**Expected**:
- [ ] Badge reads `success`
- [ ] The expanded response contains `Echo: sweep 20 live check`, `Words: 4` and `Characters: 19`
- [ ] The row no longer shows "Live" or "Stop execution"; it shows a link with tooltip "Open execution details" and a button "View execution log (modal)"
- [ ] The queue pill reads "Idle"

## Test: Execution Details (new run)

### Step 6: Open the run's detail page
**Action**:
- On the row from Step 5, click the link with tooltip "Open execution details".
**Expected**:
- [ ] URL matches `/agents/test-echo/executions/<id>`; record `<id>` as `$NEW`
- [ ] Heading "Execution Details" with a status badge reading `success`
- [ ] Under it: a link to the agent (its name), "/", and the first 8 characters of the id followed by "..."
- [ ] Four cards: "Duration", "Cost", "Context", "Triggered By" (DOM text `manual`, shown capitalised)
- [ ] "Task Input" shows `sweep 20 live check`; "Response Summary" shows the echo reply
- [ ] No "Stop" button and no "Auto-scroll ON/OFF" toggle (both exist only while streaming)

### Step 7: Live streaming view (observation)
**Action**:
- This step applies only if Step 4 found the row still `running` **and** you clicked "Live" instead of waiting. Otherwise record `SKIPPED (run finished)`.
**Expected** (if reached while running):
- [ ] Next to "Execution Transcript": a pulsing dot with the text "Live", and a toggle reading "Auto-scroll ON" (clicking it reads "Auto-scroll OFF")
- [ ] Header button "Stop" with tooltip "Stop execution"; "Completed:" reads "In progress..."
- [ ] Before the first entry arrives: "Waiting for execution output..."
- [ ] When the run ends the "Live" marker disappears without a manual refresh and the badge reads `success`

## Test: Execution Details (seeded run)

### Step 8: Transcript of the seeded execution
**Action**:
- Navigate to `http://localhost/agents/test-echo/executions/$EXEC` (the execution id from fixtures.json).
**Expected**:
- [ ] Heading "Execution Details"; record the status badge text
- [ ] A row with "Started:", "Completed:" (a date, not "In progress...") and "Execution ID:" showing the full `$EXEC`
- [ ] "Task Input" shows the seeded message
- [ ] Under "Execution Transcript": either transcript entries with an "N entries" counter, or the line "No execution transcript available for this task." — record which
- [ ] No "Live" marker
**Verify**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-echo/executions/$EXEC | jq '{status, triggered_by}'
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-echo/executions/$EXEC/log | jq '{has_log}'
```
- [ ] `status` matches the badge; `has_log: true` ↔ entries shown, `has_log: false` ↔ the "No execution transcript…" line

### Step 9: Refresh keeps the page
**Action**:
- Reload the browser on the Step 8 URL.
**Expected**:
- [ ] Same URL, same heading, same status and the same transcript state
- [ ] No redirect to `/login` or `/`; no console errors

### Step 10: Copy id and back navigation
**Action**:
- Click the header button with tooltip "Copy execution ID". Source shows no confirmation for this button — record whether anything visible happens.
- Click the back arrow (tooltip "Back to Tasks").
**Expected**:
- [ ] URL becomes `/agents/test-echo?tab=tasks` and the Tasks tab is selected (not Overview)
- [ ] The history list is populated, including the `sweep 20 live check` row

### Step 11: Unknown execution id
**Action**:
- Navigate to `http://localhost/agents/test-echo/executions/00000000-0000-0000-0000-000000000000`.
**Expected**:
- [ ] Heading "Execution Details" still renders, with no status badge
- [ ] A red card "Failed to load execution" with the text "Execution not found" and a "Try Again" button
- [ ] No endless spinner; clicking "Try Again" returns to the same card
**Verify**:
```bash
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/agents/test-echo/executions/00000000-0000-0000-0000-000000000000   # 404
```

### Step 12: Execution id under the wrong agent
**Action**:
- Navigate to `http://localhost/agents/test-counter/executions/$EXEC` (a `test-echo` execution under `test-counter`).
**Expected**:
- [ ] The same "Failed to load execution" / "Execution not found" card — the `test-echo` transcript is not shown under another agent's URL

### Step 13: Narrow width and dark theme
**Action**:
- Back on `http://localhost/agents/test-echo/executions/$EXEC`, resize the viewport to 390 px wide.
- Click the theme button in the top nav (tooltip "Light mode (click to switch)" / "Dark mode (click to switch)" / "System theme (click to switch)") until the page is dark; record the starting tooltip first.
**Expected**:
- [ ] At 390 px the four cards stack in one column and the transcript stays inside the viewport (no horizontal page scrollbar)
- [ ] In dark theme the "Task Input" block and transcript text remain readable (no dark-on-dark or white panels)

## Cleanup / Restore

- Restore the viewport size and click the theme button until its tooltip matches the one recorded in Step 13.
- The one execution created in Step 3 stays in `test-echo`'s history; that is expected and nothing depends on it. Nothing else was changed.

## Manual-only (not run unattended)

- **Stopping a live run and the Live/Auto-scroll view** need a task that runs long enough to
  catch; the fixtures answer too quickly to make this deterministic. Manually: run a long task on
  an agent of your own, click "Live", confirm entries stream in, toggle "Auto-scroll OFF/ON",
  click "Stop", and confirm the row ends as `cancelled`.
- **Termination signal flow** (container logs) needs host `docker` access.

## Critical Validations

1. A task typed into the inline box and submitted with Enter completes as `success` with the exact echo reply.
2. The row's detail link opens `/agents/test-echo/executions/<id>` with status, cards and task input.
3. The seeded execution's page matches the API (`status`, `has_log`) and survives a reload.
4. An unknown id, and a valid id under the wrong agent, both show "Failed to load execution" / "Execution not found".

## Success Criteria

- [ ] All four critical validations hold
- [ ] "Back to Tasks" returns to the Tasks tab via `?tab=tasks`
- [ ] Exactly one task was sent to a fixture agent
- [ ] Live/Stop steps are either verified or recorded as `SKIPPED (run finished)`

## Troubleshooting

- **"Run" stays disabled / "Agent must be running to execute tasks"**: `test-echo` is not
  running. Report it; do not start it from this phase.
- **Row ends `failed`**: expand it — the error text is the API `detail` from `POST /api/agents/test-echo/task`. Record it verbatim.
- **Top row has no detail link right after submit**: a just-submitted row is local until the
  server row replaces it when the request returns; wait for completion (Step 5).
- **Banner "Unable to connect to live stream. The execution is still running — refresh the page
  to check for updates." with a "Retry" button**: the stream could not be opened. Record it.
