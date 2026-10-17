# Phase 04: State Persistence (test-counter)

> **Purpose**: Prove that state an agent writes to its workspace survives independent executions, a page reload and one agent restart.
> **Duration**: ~12 minutes
> **Assumes**: the fixture trio is running and you are logged in as admin
> **Output**: Counter value written by one task is read by the next, is visible in the Files tab, and is still there after a reload and a stop/start
> **Last verified**: 2026-10-09 against source (not yet browser-run)

---

## Background

`test-counter` keeps a number in `/home/developer/counter.txt`. It understands `get`, `increment`, `decrement`, `add N`, `subtract N` and `reset`, and always answers `Counter: [new_value] (previous: [old_value])`.

Commands are sent from the agent's **Tasks** tab, not Chat. A task is a stateless one-shot execution with no conversation history, so the only way `increment` can know the previous value is by reading the file — which is the thing under test. (The Chat tab prepends earlier turns to each message, which would let the model answer from the transcript.)

The state file is inspected through the **Files** tab (`components/FilesPanel.vue`), which lists `/home/developer`.

Replaces the January flow that typed commands into a Terminal tab and watched an Activity panel; neither exists any more.

## Prerequisites

- [ ] Logged in as `admin` with `ADMIN_PASSWORD` from `.env`
- [ ] `test-counter` exists and its header toggle reads `Running`
- [ ] `$TOKEN` holds an admin Bearer token for API checks

This phase sends **4 tasks** to `test-counter` (the limit) and performs **one** stop/start of `test-counter`.

---

## Test: Counter operations through the Tasks tab

### Step 1: Open the Tasks tab
**Action**:
- Navigate to `http://localhost/agents/test-counter?tab=tasks`

**Expected**:
- [ ] Heading `Tasks` with the line `All executions — chats, tasks, schedules, and agent-to-agent`
- [ ] A trigger filter select showing `All triggers`, and a `Refresh` button
- [ ] A textarea with placeholder `Enter task message... (Enter to run, Shift+Enter for newline)` and a `Run` button (disabled while the textarea is empty)
- [ ] Header toggle reads `Running`
- [ ] Record how many rows the task history shows now (the seed script left at least one)

### Step 2: Send `reset`
**Action**:
- Type `reset` in the task textarea and click `Run`
- Wait up to 60 s for the new top row to leave the `running` state

**Expected**:
- [ ] A new row appears at the top immediately with status `running` and trigger badge `manual`
- [ ] The row settles to status `success`
- [ ] Clicking the row's chevron (title `Expand`) shows a response containing `Counter: 0 (previous: ` followed by a number
- [ ] Record the previous value (it is whatever an earlier run left; any number is fine)

### Step 3: Send `increment`
**Action**:
- Type `increment` and click `Run`; wait up to 60 s; expand the new row

**Expected**:
- [ ] Response contains `Counter: 1 (previous: 0)`
- [ ] This execution had no conversation history, so `previous: 0` can only have come from the file written in Step 2

---

## Test: State file in the Files tab

### Step 4: Find the state file
**Action**:
- Click the `Files` tab (it may be inside the `More` overflow menu)
- If `counter.txt` is not visible, click the button titled `Refresh`

**Expected**:
- [ ] The tree lists `counter.txt` at the top level
- [ ] Before a file is clicked the right pane shows `No File Selected` and `Select a file from the tree to preview`
- [ ] The tree footer reads `N files` (optionally followed by `•` and a size)

### Step 5: Preview the state file
**Action**:
- Click `counter.txt`

**Expected**:
- [ ] The preview pane shows the file body as text, and the body is `1` (surrounding whitespace is fine)
- [ ] Below the preview: the name `counter.txt`, its path, a size and a relative time such as `just now`
- [ ] Buttons `Edit`, `Download` and `Delete` are shown. Do not click `Edit` or `Delete`.

**Verify** (API):
```bash
curl -s -H "Authorization: Bearer $TOKEN" --get \
  --data-urlencode "path=counter.txt" \
  http://localhost:8000/api/agents/test-counter/files/preview
# Expect the body: 1
```

---

## Test: Persistence across a page reload

### Step 6: Reload on the Tasks tab
**Action**:
- Navigate to `http://localhost/agents/test-counter?tab=tasks` and reload the page

**Expected**:
- [ ] The page lands on the Tasks tab (the `?tab=` query selects it)
- [ ] The `reset` and `increment` rows are both still listed, each `success` with trigger badge `manual`
- [ ] Summary tiles `Total`, `Success Rate`, `Total Cost`, `Avg Duration` are shown above the task input

**Verify** (API):
```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/agents/test-counter/executions?limit=5" \
  | python3 -c "import sys,json; [print(e['status'], e['triggered_by'], e['message'][:40]) for e in json.load(sys.stdin)]"
# The two newest rows are the increment and reset tasks
```

### Step 7: Reload on the Files tab
**Action**:
- Navigate to `http://localhost/agents/test-counter?tab=files`
- Click `counter.txt`

**Expected**:
- [ ] The Files tab opens directly and the tree lists `counter.txt`
- [ ] The preview body is still `1`

---

## Test: Persistence across one agent restart

Do this block exactly once. If the agent has not returned to `Running` within 60 s of Step 9, stop the phase, record a failure, and go straight to Cleanup.

### Step 8: Stop the agent
**Action**:
- In the agent header, click the toggle that reads `Running`
- Wait until it reads `Stopped` (up to 30 s). There is no confirmation dialog.

**Expected**:
- [ ] Toggle reads `Stopped`
- [ ] The Files tab shows `Agent must be running to browse files` in place of the tree
- [ ] On the Tasks tab the textarea is disabled and the line `Agent must be running to execute tasks` appears under it
- [ ] The earlier task rows are still listed on the Tasks tab while the agent is stopped

### Step 9: Start the agent
**Action**:
- Click the toggle that reads `Stopped`
- Wait until it reads `Running` (up to 60 s)

**Expected**:
- [ ] Toggle reads `Running`
- [ ] The Tasks textarea is enabled again

### Step 10: State file after the restart
**Action**:
- Open the `Files` tab, click `Refresh` if the tree is empty or shows an error, then click `counter.txt`

**Expected**:
- [ ] `counter.txt` is listed and the preview body is `1` — the value written before the restart

### Step 11: Counter continues from the persisted value
**Action**:
- Open the `Tasks` tab, send `increment`, wait up to 60 s, expand the new row

**Expected**:
- [ ] Response contains `Counter: 2 (previous: 1)`
- [ ] The pre-restart `reset` and `increment` rows are still in the history below it

---

## Test: History filter and narrow layout

### Step 12: Trigger filter
**Action**:
- In the trigger select choose `Manual`, then choose `Schedule`, then choose `All triggers`

**Expected**:
- [ ] `Manual`: this phase's rows remain listed
- [ ] `Schedule`: rows with other triggers disappear. If nothing matches, the list area reads `No tasks yet` — record whether rows or that text appear
- [ ] `All triggers`: the full list returns

### Step 13: 390 px width
**Action**:
- Resize the viewport to 390 × 844 with the Tasks tab open, then restore 1280 × 800

**Expected**:
- [ ] The task textarea and `Run` button are both reachable (scrolling inside the panel is fine)
- [ ] Record whether the page itself scrolls horizontally (`document.documentElement.scrollWidth > window.innerWidth`); it should not

---

## Cleanup / Restore

The counter was read as "whatever an earlier run left" (recorded in Step 2) and is deliberately left at a known value rather than that one:

- On the Tasks tab send `reset` (the 4th and last task). Expected response: `Counter: 0 (previous: 2)`.
- Confirm the header toggle reads `Running`. If it reads `Stopped`, click it once and wait up to 60 s.
- Leave the trigger filter on `All triggers` and the viewport at 1280 × 800.

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-counter \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])"
# Expect: running
```

## Manual-only (not run unattended)

- Persistence across a backend restart or a `docker compose down`/`up` (needs host access).
- Persistence across an agent *recreate* (resource-limit change or base-image update) — a different code path from the plain stop/start above.

## Critical Validations

1. `increment` after `reset` answers `Counter: 1 (previous: 0)` — state crossed two independent executions.
2. `counter.txt` in the Files tab holds the same value the task reported.
3. After stop/start the file still holds `1` and the next `increment` answers `Counter: 2 (previous: 1)`.
4. Task history rows survive both the reload and the restart.
5. The agent is `Running` and the counter is `0` when the phase ends.

## Success Criteria

- [ ] Steps 2, 3 and 11 return the exact `Counter: N (previous: M)` values stated
- [ ] Files tab preview matches the reported value before and after the restart
- [ ] Reloading with `?tab=tasks` and `?tab=files` lands on those tabs with data intact
- [ ] Stopped-agent guard texts appear on Files and Tasks while stopped
- [ ] Cleanup `reset` succeeded and the agent is running

## Troubleshooting

- **Files tree shows `Agent server not ready. The agent may still be starting up.` after Step 9**: the container is up but its internal server is not yet; wait 10 s and click `Refresh`.
- **Response is not in the `Counter: …` format**: the model deviated from the fixture's instructions. Record the full response; judge the step on the value in `counter.txt` and note the format drift.
- **`previous:` in Step 3 is not `0`**: Step 2's write did not land. Check the Step 2 row for an error and whether `counter.txt` exists.
- **A task row stays `running` beyond 60 s**: record it and continue; do not send a replacement task (the 4-task budget is fixed).
- **Tab not visible**: tabs that do not fit are inside the `More` menu at the right end of the tab strip.
