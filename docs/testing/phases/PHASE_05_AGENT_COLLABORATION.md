# Phase 05: Agent Collaboration & Permissions (test-delegator)

> **Purpose**: Verify that one agent can call another only when permitted, and that the delegated run is visible on the target agent, in Operations and on the Dashboard timeline with the right trigger type.
> **Duration**: ~15 minutes
> **Assumes**: the fixture trio is running and you are logged in as admin
> **Output**: A permitted delegation from `test-delegator` to `test-echo` runs and is recorded as an `agent`-triggered execution; a denied one is refused; the original permission set is restored
> **Last verified**: 2026-10-09 against source (not yet browser-run)

---

## Background

`test-delegator` understands `list agents`, `delegate to [agent]: [message]` and `ping [agent]`, and reaches other agents through the Trinity MCP tools. Whether a call is allowed is decided by the caller's **Permissions** tab (`components/PermissionsPanel.vue`, owner-only). New agents start with **no** permissions, so nothing is assumed about the starting state — this phase reads it, sets what it needs and puts it back.

A call made by an agent is recorded on the *target* as an execution whose trigger is `agent`. That value is shown as a badge on the target's Tasks tab, as a chip in Operations → Executions, and on the Dashboard timeline as `Agent-Triggered`.

Replaces the January flow that expected eight agents and a default-allow permission model.

## Prerequisites

- [ ] Logged in as `admin` with `ADMIN_PASSWORD` from `.env`
- [ ] `test-delegator` and `test-echo` both show `Running`
- [ ] `$TOKEN` holds an admin Bearer token

This phase sends **3 tasks** to `test-delegator` (each may trigger one further call to `test-echo`).

## Setup

### Step 1: Read and record the original permission set
**Action**:
```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/agents/test-delegator/permissions \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('ORIGINAL:', sorted(a['name'] for a in d['available_agents'] if a['permitted']))"
```

**Expected**:
- [ ] HTTP 200; the response has `source_agent` and `available_agents` (each entry has `name`, `status`, `permitted`)
- [ ] Record the `ORIGINAL` list verbatim — Cleanup restores exactly this

---

## Test: Permissions tab

### Step 2: Open the Permissions tab
**Action**:
- Navigate to `http://localhost/agents/test-delegator?tab=permissions`

**Expected**:
- [ ] Heading `Agent Collaboration Permissions`
- [ ] Text `Control which other agents this agent can communicate with via the Trinity MCP tools.`
- [ ] Links `Allow All` and `Allow None`, and a button `Save Permissions` that is disabled while nothing has changed
- [ ] One checkbox row per other agent, each with a status pill (`running` / `stopped`); rows for `test-echo` and `test-counter` are present; `test-delegator` itself is not listed
- [ ] The checked rows match the `ORIGINAL` list from Step 1

### Step 3: Bulk controls do not save by themselves
**Action**:
- Click `Allow All`, then click `Allow None`
- Do NOT click `Save Permissions`. Reload the page.

**Expected**:
- [ ] After `Allow All`: every checkbox is checked and `Unsaved changes` appears beside the links; `Save Permissions` becomes enabled
- [ ] After `Allow None`: every checkbox is unchecked, `Unsaved changes` still shown
- [ ] After the reload: the checked rows are the `ORIGINAL` list again (nothing was persisted)

### Step 4: Permit test-echo
**Action**:
- If the `test-echo` row is already checked, skip to Expected
- Otherwise tick the `test-echo` checkbox and click `Save Permissions`

**Expected**:
- [ ] If a save was made: the message `Permissions saved (N agents allowed)` appears beside the button for about 3 seconds, and `Unsaved changes` disappears
- [ ] `test-echo` is checked

**Verify** (API):
```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/agents/test-delegator/permissions \
  | python3 -c "import sys,json; print([a['name'] for a in json.load(sys.stdin)['available_agents'] if a['permitted']])"
# Expect the list to include test-echo
```

---

## Test: Permitted delegation

### Step 5: Delegate to test-echo
**Action**:
- Open the `Tasks` tab of `test-delegator`
- In the textarea (placeholder `Enter task message... (Enter to run, Shift+Enter for newline)`) type `delegate to test-echo: sweep05 ping` and click `Run`
- Wait up to 60 s, then expand the new row (chevron titled `Expand`)

**Expected**:
- [ ] The row settles to `success` with trigger badge `manual`
- [ ] The response mentions `test-echo` and contains `Echo:` together with `sweep05 ping` (the fixture's format is `Delegating to test-echo: "…"` / `Response from test-echo:` followed by the echo; exact wording may vary — record it)

### Step 6: The delegated run on the target's Tasks tab
**Action**:
- Navigate to `http://localhost/agents/test-echo?tab=tasks`
- In the trigger select choose `Agent`

**Expected**:
- [ ] At least one row is listed whose message contains `sweep05 ping`
- [ ] Its trigger badge reads `agent` and its status is `success`
- [ ] Set the select back to `All triggers`

**Verify** (API):
```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/executions?agent=test-echo&triggered_by=agent&hours=1&limit=5" \
  | python3 -c "import sys,json; [print(e['id'], e['status'], e['source_agent_name'], e['message'][:40]) for e in json.load(sys.stdin)]"
# Expect a row with source_agent_name = test-delegator. Record the COUNT of rows as N_AGENT.
```

### Step 7: The delegated run in Operations
**Action**:
- Navigate to `http://localhost/operations?tab=executions`
- In the agent select (first option `All agents`) choose `test-echo`; in the trigger select (first option `All triggers`) choose `agent`

**Expected**:
- [ ] A row is listed with the agent link `test-echo`, a trigger chip reading `agent`, a relative time, and a message containing `sweep05 ping`
- [ ] Summary tiles `Total`, `Completion`, `Failed`, `Cost` are shown above the filters
- [ ] Reset both selects to `All agents` / `All triggers` (or click `Clear filters` if offered)

### Step 8: The delegated run on the Dashboard timeline
**Action**:
- Navigate to `http://localhost/` and click `Timeline` in the mode switcher if it is not already active
- Hover the newest bar in the `test-echo` row, then the newest bar in the `test-delegator` row

**Expected**:
- [ ] The legend at the top right of the timeline lists, in order: `Manual`, `MCP`, `Scheduled`, `Agent-Triggered`, `Paid`, `Public`, `Next Run`
- [ ] The `test-echo` bar's tooltip begins `Agent-Triggered Task`
- [ ] The `test-delegator` bar's tooltip begins `Manual Task`
- [ ] If the bars are not visible, select `1h` in the time-range select and use the button titled `Zoom in`; record the range you used and set it back afterwards

### Step 9: `list agents`
**Action**:
- On `test-delegator` → `Tasks`, send `list agents`; wait up to 60 s; expand the row

**Expected**:
- [ ] The response lists agents (fixture format: `Available agents:` then `- [agent-name]: [status]` lines) and includes `test-echo`
- [ ] Record the full list of names returned — in particular whether `test-counter` appears when it is not in the permitted set

---

## Test: Denied delegation

Run this block only if you will complete Cleanup. If time is short, record Steps 10–11 as `SKIPPED` and go to Cleanup.

### Step 10: Revoke test-echo
**Action**:
- Open `http://localhost/agents/test-delegator?tab=permissions`
- Untick `test-echo` and click `Save Permissions`

**Expected**:
- [ ] `Permissions saved (N agents allowed)` appears, with N one lower than in Step 4
- [ ] After a reload, `test-echo` is unchecked

### Step 11: Delegation is refused
**Action**:
- On `test-delegator` → `Tasks`, send `delegate to test-echo: sweep05 denied`; wait up to 60 s; expand the row

**Expected**:
- [ ] The response does NOT contain `Echo: sweep05 denied`
- [ ] The response reports that the call was refused (the tool's denial reads `Access denied` with the reason `Agent 'test-echo' not found or not accessible`; the agent may paraphrase — record its wording)
- [ ] No new `agent`-triggered execution was created on `test-echo`

**Verify** (API):
```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/executions?agent=test-echo&triggered_by=agent&hours=1&limit=5" \
  | python3 -c "import sys,json; r=json.load(sys.stdin); print(len(r), [e['message'][:30] for e in r])"
# Expect the count to equal N_AGENT from Step 6 and no message containing "sweep05 denied"
```

### Step 12: Permissions panel at 390 px
**Action**:
- With the Permissions tab open, resize to 390 × 844, then restore 1280 × 800

**Expected**:
- [ ] Every agent row, its checkbox and `Save Permissions` remain reachable
- [ ] Record whether the page scrolls horizontally; it should not

---

## Cleanup / Restore

Original value: the `ORIGINAL` list recorded in Step 1. Restore exactly that set, whether or not Steps 10–11 ran:

```bash
# Replace the list with ORIGINAL from Step 1, e.g. [] or ["test-echo"]
curl -s -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"permitted_agents": []}' \
  http://localhost:8000/api/agents/test-delegator/permissions

curl -s -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/agents/test-delegator/permissions \
  | python3 -c "import sys,json; print(sorted(a['name'] for a in json.load(sys.stdin)['available_agents'] if a['permitted']))"
# Must print the ORIGINAL list
```

Also: leave every trigger/agent filter on its "All" option, and the Dashboard time range on the value it had before Step 8.

## Manual-only (not run unattended)

- A user who has access to `test-delegator` but does not own it must not see the Permissions tab and must get `Only the owner can modify agent permissions` (403) from the PUT — needs a second account.
- Delegation to a stopped target (requires stopping a fixture).

## Critical Validations

1. With `test-echo` permitted, the delegation returns the echo and creates an execution on `test-echo` with `triggered_by = agent` and `source_agent_name = test-delegator`.
2. That execution is visible in three places: target Tasks tab (`agent` badge), Operations → Executions (`agent` chip), Dashboard timeline (`Agent-Triggered Task` tooltip).
3. With `test-echo` revoked, the delegation is refused and no execution is created on the target.
4. `Allow All` / `Allow None` change nothing until `Save Permissions` is clicked.
5. The permission set after Cleanup equals the set read in Step 1.

## Success Criteria

- [ ] Original permission set recorded and restored
- [ ] Permitted delegation succeeded and was observed on all three surfaces
- [ ] Legend labels match the seven listed in Step 8
- [ ] Denied delegation produced no target execution (or the block was explicitly skipped)
- [ ] No more than 3 tasks were sent

## Troubleshooting

- **Permissions tab is missing**: it is rendered only for a viewer who can share the agent (owner or admin) and never for the system agent. Check the `More` overflow menu first.
- **`No other agents available`**: the permissions call returned no peers; confirm the other fixtures exist on the Dashboard list.
- **Step 5 response says the agent was not found or not accessible**: the save in Step 4 did not persist — re-run the Verify command before spending another task.
- **PUT returns 400 `Agent '<name>' does not exist or is not accessible`**: a name in the body is wrong; the body replaces the whole set, so send only names that appear in `available_agents`.
- **No bar on the timeline**: the legend is hidden below 640 px width, and bars outside the selected time range are not drawn — widen the window and choose `1h`.
