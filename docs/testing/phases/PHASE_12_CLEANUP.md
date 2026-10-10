# Phase 12: Agent Lifecycle — stop, start, delete (throwaway only)

> **Purpose**: Drive stop, start and delete from the agent header on one throwaway agent and prove the fixtures are untouched.
> **Duration**: ~10 minutes
> **Assumes**: the fixture trio is running and you are logged in as admin
> **Output**: header run-state switch and delete confirmation work end to end; a deleted agent leaves the list and the API; the trio and `trinity-system` are still running
> **Last verified**: 2026-10-09 against source (not yet browser-run)

---

## Background

Lifecycle controls live in the agent header on `/agents/:name` (`components/AgentHeader.vue`):
a run-state switch labelled "Running" / "Stopped", and a trash button titled "Delete agent" that
only renders when the API says `can_delete: true`. The agent list is the Dashboard's list mode
(`/?view=list`; `/agents` redirects there).

Replaces the January flow titled "Delete All Agents", which deleted the fixture agents one by
one. This phase creates ONE throwaway agent and deletes only that.

Delete is a **soft delete**: the container is removed, but the name stays reserved and the
workspace volume stays on disk until the retention sweep. A second run therefore cannot reuse
the same name — Setup handles that.

## Prerequisites

- [ ] Logged in to `http://localhost` as `admin` with `ADMIN_PASSWORD` from `.env`
- [ ] `$TOKEN` holds an admin Bearer token (`POST /api/token`, form-encoded)
- [ ] `test-echo`, `test-counter`, `test-delegator` are `running`

## Setup

### Step 1: Record the baseline and create the throwaway
**Action**:
- Record the status of the four agents that must not change:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents \
  | jq -r '.[] | select(.name|test("^(test-echo|test-counter|test-delegator|trinity-system)$")) | "\(.name) \(.status)"'
```
- Create the throwaway from the echo fixture template. Try the plain name first; if it answers
  `409` (the name is still reserved by an earlier run's soft delete), use a timestamped name:
```bash
TMP=sweep-tmp-12
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost:8000/api/agents \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"name\": \"$TMP\", \"template\": \"local:test-echo\"}")
if [ "$code" = "409" ]; then
  TMP=sweep-tmp-12-$(date +%s)
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost:8000/api/agents \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d "{\"name\": \"$TMP\", \"template\": \"local:test-echo\"}")
fi
echo "$TMP $code"
```
- Poll (every 5 s, at most 60 s) until it is running:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/$TMP | jq -r '.status, .can_delete'
```
**Expected**:
- [ ] Final create status is `200`; **record the value of `$TMP`** — every later step uses it
- [ ] Within 60 s the poll prints `running` and `true`
- [ ] If create fails with anything other than the handled `409`, record the body and stop (run Cleanup)

## Test: Find it in the list

### Step 2: Dashboard list mode
**Action**:
- Navigate to `http://localhost/agents`.
- Type the value of `$TMP` into the "Search agents..." box.
**Expected**:
- [ ] URL becomes `/?view=list`
- [ ] One row whose name link reads the `$TMP` name, with a run-state switch showing "Running"
- [ ] `test-echo`, `test-counter`, `test-delegator` are filtered out while the search text is present

### Step 3: Open the agent
**Action**:
- Click the name link in that row.
**Expected**:
- [ ] URL is `/agents/<$TMP>`; the Overview tab is selected
- [ ] The header shows a switch labelled "Running" (accessible name "Agent is running. Click to stop.")
- [ ] The header shows a trash button with tooltip "Delete agent"

## Test: Stop and start

### Step 4: Stop from the header
**Action**:
- Click the header run-state switch once. Wait up to 30 s.
**Expected**:
- [ ] The switch shows a spinner while the request runs, then the label reads "Stopped"
- [ ] A success notification reads "Agent <$TMP> stopped"
- [ ] The Tasks tab (click "Tasks") shows "Agent must be running to execute tasks" and a disabled "Run" button
**Verify**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/$TMP | jq -r .status
# stopped
```

### Step 5: Stopped state survives a reload
**Action**:
- Reload the page.
**Expected**:
- [ ] Still on `/agents/<$TMP>`; the switch still reads "Stopped"
- [ ] No console errors

### Step 6: Start from the header
**Action**:
- Click the header run-state switch once. Wait up to 60 s.
**Expected**:
- [ ] The label returns to "Running"
- [ ] A success notification reads "Agent <$TMP> started"
- [ ] On the Tasks tab the "Agent must be running to execute tasks" line is gone
**Verify**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/$TMP | jq -r .status   # running
```

### Step 7: Header at narrow width
**Action**:
- Resize the viewport to 390 px wide, then restore the original size.
**Expected**:
- [ ] The run-state switch and the "Delete agent" button are both still reachable (visible or scrollable into view, not clipped off-screen)
- [ ] No horizontal page scrollbar on the body

## Test: Delete

### Step 8: Cancel the confirmation
**Action**:
- Click the trash button ("Delete agent").
- In the dialog, click "Cancel".
**Expected**:
- [ ] Dialog title "Delete Agent", message "Are you sure you want to delete this agent?", buttons "Delete" and "Cancel"
- [ ] After "Cancel" the dialog closes, the URL is unchanged and the agent is still "Running"

### Step 9: Confirm the delete
**Action**:
- Click the trash button again, then click "Delete".
**Expected**:
- [ ] The browser navigates to `/` (the Dashboard)
- [ ] No error notification

### Step 10: It is gone from the list and the API
**Action**:
- Navigate to `http://localhost/agents` and type the `$TMP` name into "Search agents...".
- Navigate to `http://localhost/agents/<$TMP>`; record what the page renders for an agent that no longer exists.
**Expected**:
- [ ] No row for the `$TMP` name in the list
- [ ] The direct URL does not show a working agent page (record the exact not-found/redirect behaviour)
**Verify**:
```bash
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/$TMP   # 404
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents | jq --arg n "$TMP" '[.[]|select(.name==$n)]|length'   # 0
```

## Test: Fixtures untouched

### Step 11: The system agent has no delete control (observe only)
**Action**:
- Navigate to `http://localhost/agents/trinity-system`. **Do not click the run-state switch.**
**Expected**:
- [ ] The header shows a "SYSTEM" badge with tooltip "System Agent - Platform Orchestrator with full access"
- [ ] There is **no** "Delete agent" trash button in the header
- [ ] Record whether the Access, Sharing, Permissions, Folders, Skills and Settings tabs are absent (source hides them for a system agent)

### Step 12: Trio and system agent still running
**Action**:
- Re-run the baseline command from Step 1.
**Expected**:
- [ ] `test-echo`, `test-counter`, `test-delegator` each print `running`
- [ ] `trinity-system` prints the same status recorded in Step 1

## Cleanup / Restore

- If the phase stopped before Step 9, delete the throwaway by API (only this name, never a fixture):
```bash
curl -s -X DELETE -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/$TMP
```
- **Record the throwaway name in the report.** Its workspace volume `agent-<$TMP>-workspace`
  (for the plain name: `agent-sweep-tmp-12-workspace`) persists after the delete and must be
  removed by whoever maintains the host; this phase has no host access and does not remove it.
- The name stays reserved after the soft delete, which is why Setup falls back to a timestamped name.
- No fixture setting was changed; nothing else to restore.

## Critical Validations

1. The header switch stops and starts the throwaway, and the API status agrees each time.
2. The delete dialog reads "Delete Agent" / "Are you sure you want to delete this agent?" and "Cancel" really cancels.
3. After "Delete", `GET /api/agents/<$TMP>` is `404` and the list has no such row.
4. `test-echo`, `test-counter`, `test-delegator` and `trinity-system` have the same status at the end as at the start.
5. `trinity-system` exposes no delete control.

## Success Criteria

- [ ] All five critical validations hold
- [ ] Only the throwaway agent was created, stopped, started or deleted
- [ ] The throwaway name is recorded in the report for volume removal

## Troubleshooting

- **Create returns `409` twice**: both names are taken or the leftover workspace volume blocks
  the name; record the response `detail` and stop — do not try to free the name.
- **Start takes longer than 60 s**: record it as a finding; the switch stays in its spinner state
  while `POST /api/agents/{name}/start` is pending.
- **No trash button on the throwaway**: `can_delete` came back `false`; record the
  `GET /api/agents/<$TMP>` body.
- **Delete shows an error notification**: the text is the API `detail`; record it and use the
  Cleanup command.
