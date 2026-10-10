# Phase 26: Shared Folders

> **Purpose**: Validate the Folders tab — expose and mount toggles, the restart-required state, and the consumer / available lists — without restarting any fixture.
> **Duration**: ~12 minutes
> **Assumes**: the fixture trio is running and you are logged in as admin
> **Output**: expose on one agent and mount on another are saved, surfaced as pending in both panels and in the API, and restored to their original values
> **Last verified**: 2026-10-09 against source (not yet browser-run)

---

## Background

The Folders tab (`components/FoldersPanel.vue`, tab id `folders`) is shown only to a viewer who
can share the agent (owner or admin) and never on the system agent. It has two switches —
"Expose Shared Folder" and "Mount Shared Folders" — plus "Exposed Folder" (with "Consumers") and
"Mounted Folders" (with "Available Folders") sections.

A toggle only **saves configuration** (`PUT /api/agents/{name}/folders`). It does not restart
the container: the panel shows a "Restart Required" banner and the volume is attached the next
time the agent is restarted. A consumer sees an exposer's folder only if the consumer agent has
permission to call the exposer (Permissions tab / `…/permissions`).

Replaces the January flow that used a `test-worker` agent, a Terminal tab, host `docker exec`
and labels "Expose Folder" / "Consume Folders". Pairing here: **`test-counter` exposes,
`test-delegator` mounts**. This phase sends no messages to agents and restarts nothing.

## Prerequisites

- [ ] Logged in to `http://localhost` as `admin` with `ADMIN_PASSWORD` from `.env`
- [ ] `$TOKEN` holds an admin Bearer token
- [ ] `test-counter` and `test-delegator` are `running`

## Setup

### Step 1: Record the original values
**Action**:
```bash
for a in test-counter test-delegator; do
  curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/$a/folders \
    | jq -c '{agent_name, expose_enabled, consume_enabled, restart_required}'
done
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-delegator/permissions \
  | jq '[.permitted_agents[].name] | index("test-counter") != null'
```
**Expected**:
- [ ] Two JSON lines; **record** `expose_enabled` and `consume_enabled` for both agents as ORIG values (normally all `false`)
- [ ] The last line prints `true` or `false`; **record** it as `ORIG_PERM`
- [ ] If `test-counter.expose_enabled` or `test-delegator.consume_enabled` is already `true`, record `BLOCKED (fixture not in default state)` and stop — do not toggle them off

### Step 2: Ensure the mount permission exists
**Action**:
- Only if `ORIG_PERM` is `false`, grant `test-delegator` permission to call `test-counter`:
```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/agents/test-delegator/permissions/test-counter
```
**Expected**:
- [ ] `{"status": "added", "source_agent": "test-delegator", "target_agent": "test-counter"}` (or nothing run because `ORIG_PERM` was `true`)

## Test: Expose (test-counter)

### Step 3: Open the Folders tab
**Action**:
- Navigate to `http://localhost/agents/test-counter?tab=folders`.
**Expected**:
- [ ] The "Folders" tab is selected
- [ ] Card "Shared Folder Configuration" with two rows: "Expose Shared Folder" (mentions `/home/developer/shared-out`) and "Mount Shared Folders" (mentions `/home/developer/shared-in/{agent}`), each with an unlabelled switch on the right; the expose switch is off (the mount switch matches its ORIG value)
- [ ] Box "How Shared Folders Work" with four numbered lines
- [ ] No "Exposed Folder" section; no "Restart Required" banner if Step 1 reported `restart_required: false`
- [ ] Not the "Owner Access Required" state

### Step 4: Turn expose on
**Action**:
- Click the switch in the "Expose Shared Folder" row **once**. Do not restart the agent.
**Expected**:
- [ ] The switch turns on (stays on after the panel reloads)
- [ ] Banner "Restart Required" — "Configuration has changed. Restart the agent to apply shared folder mounts."
- [ ] New section "Exposed Folder" with "Volume:" (a volume name — record it) and "Path:" `/home/developer/shared-out`
- [ ] Under it: "No agents are configured to consume this folder yet."
- [ ] The agent header still shows "Running" — the toggle did not restart it
**Verify**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-counter/folders \
  | jq -c '{expose_enabled, restart_required, exposed_volume, status}'
# expose_enabled true, restart_required true, status "running"
```

### Step 5: Expose survives a reload
**Action**:
- Reload the page.
**Expected**:
- [ ] Still on the Folders tab (`?tab=folders`), switch still on, "Restart Required" banner still shown
- [ ] No console errors

## Test: Mount (test-delegator)

### Step 6: Consumer panel before mounting
**Action**:
- Navigate to `http://localhost/agents/test-delegator?tab=folders`.
**Expected**:
- [ ] The mount switch is off; no "Mounted Folders" section and no "Available Folders" list yet (they render only while mount is on)

### Step 7: Turn mount on
**Action**:
- Click the switch in the "Mount Shared Folders" row **once**. Do not restart the agent.
**Expected**:
- [ ] The switch turns on
- [ ] Section "Mounted Folders" with a row `test-counter`, a mount path beneath it (record it; it is under `/home/developer/shared-in/`), and a badge "Pending" (dot tooltip "Pending restart")
- [ ] "Available Folders" lists `test-counter` with the line "Folders from agents you have permission to access. These will be mounted on restart."
- [ ] Banner "Restart Required"
- [ ] The agent header still shows "Running"
**Verify**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-delegator/folders \
  | jq -c '{consume_enabled, restart_required, consumed: [.consumed_folders[] | {source_agent, currently_mounted}]}'
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-delegator/folders/available \
  | jq -c '[.available_folders[].source_agent]'
# consume_enabled true; consumed has test-counter with currently_mounted false; available contains "test-counter"
```

### Step 8: The exposer now lists its consumer
**Action**:
- Navigate back to `http://localhost/agents/test-counter?tab=folders`.
**Expected**:
- [ ] In "Exposed Folder": heading "Consumers" with "These agents can mount this folder:" and a row `test-delegator` with a mount path
- [ ] The "No agents are configured to consume this folder yet." line is gone
**Verify**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-counter/folders/consumers \
  | jq -c '{count, names: [.consumers[].agent_name]}'
# names contains "test-delegator"
```

### Step 9: An unrelated agent is not offered the folder by accident
**Action**:
- Run:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/test-echo/folders \
  | jq -c '{expose_enabled, consume_enabled, consumed_folders}'
```
**Expected**:
- [ ] `test-echo` is unchanged: `consume_enabled` is its original value and `consumed_folders` is `[]` unless it was already mounting
- [ ] Record the output; nothing in this phase toggles `test-echo`

### Step 10: Narrow width and dark theme
**Action**:
- On `test-counter`'s Folders tab, resize the viewport to 390 px wide.
- Click the theme button in the top nav (tooltip ends "(click to switch)") until the page is dark; record the starting tooltip first.
**Expected**:
- [ ] At 390 px each configuration row keeps its switch visible and clickable; the volume name wraps or scrolls inside its card without a horizontal page scrollbar
- [ ] In dark theme the "Restart Required" banner text and the code chips (`/home/developer/shared-out`) are readable

## Cleanup / Restore

Restore in this order, then verify. Originals were recorded in Steps 1–2.

### Step 11: Turn both switches back off
**Action**:
- Restore the viewport and click the theme button until its tooltip matches the one recorded in Step 10.
- On `http://localhost/agents/test-delegator?tab=folders`, click the "Mount Shared Folders" switch once (back to off).
- On `http://localhost/agents/test-counter?tab=folders`, click the "Expose Shared Folder" switch once (back to off).
**Expected**:
- [ ] Both switches off; "Mounted Folders" and "Exposed Folder" sections are gone on their agents
- [ ] The "Restart Required" banner is gone on both (configuration matches the running container again)

### Step 12: Restore the permission and verify by API
**Action**:
- Only if Step 2 added the permission (`ORIG_PERM` was `false`):
```bash
curl -s -X DELETE -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/agents/test-delegator/permissions/test-counter
```
- Re-run the three commands from Step 1.
- If a switch could not be restored in the UI, restore it by API:
```bash
curl -s -X PUT -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"expose_enabled": false}' http://localhost:8000/api/agents/test-counter/folders
curl -s -X PUT -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"consume_enabled": false}' http://localhost:8000/api/agents/test-delegator/folders
```
**Expected**:
- [ ] Both agents report the ORIG `expose_enabled` / `consume_enabled` values and `restart_required: false`
- [ ] The permission check prints `ORIG_PERM`
- [ ] Both agents are still `running`; neither was restarted during the phase

## Manual-only (not run unattended)

- **Real mount and file visibility**: restart both agents with the toggles on, confirm the
  consumer row turns "Mounted", write a file under `/home/developer/shared-out` on the exposer
  and read it under the consumer's `shared-in` path, then turn both off and restart again. This
  needs four restarts and container access, so it is not part of the unattended run.
- **"Owner Access Required"** ("Only the agent owner can manage shared folders.") needs a second,
  non-owner account.
- **Permission-gated visibility** (mount on without permission → "No shared folders available.
  Grant permissions to other agents that expose folders.") is safe to try manually by removing
  the permission while mount is on.

## Critical Validations

1. Turning "Expose Shared Folder" on for `test-counter` saves (`expose_enabled: true`) and shows "Restart Required" without restarting the agent.
2. Turning "Mount Shared Folders" on for `test-delegator` lists `test-counter` as "Pending" and under "Available Folders".
3. `test-counter` lists `test-delegator` under "Consumers", and the consumers API agrees.
4. After Cleanup both agents are back to their ORIG values with `restart_required: false`, and the permission is as it was.

## Success Criteria

- [ ] All four critical validations hold
- [ ] Exactly one expose toggle and one mount toggle were turned on, and both were turned off again
- [ ] No fixture agent was stopped, started or restarted

## Troubleshooting

- **No "Folders" tab**: the viewer cannot share the agent (`can_share: false` on
  `GET /api/agents/{name}`), or the agent is a system agent. Record it.
- **Mount on, but "No shared folders available. Grant permissions to other agents that expose
  folders."**: `test-delegator` lacks permission to `test-counter` (Step 2) or expose was not
  saved (Step 4).
- **A switch does not change**: the panel swallows save errors (they are only logged to the
  console) — read the console and the `PUT /api/agents/{name}/folders` response.
- **"Restart Required" still shown after Cleanup**: the container already had the volume
  attached before the phase began; compare with the Step 1 `restart_required` value.
