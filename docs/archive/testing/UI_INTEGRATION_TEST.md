# UI Integration Test Checklist - Comprehensive

> **Purpose**: Validate Trinity platform ALL components using 8 GitHub-based test agents
> **Duration**: ~45-60 minutes (comprehensive manual), ~10 minutes (automated)
> **Prerequisites**: Local environment running (localhost:3000 + localhost:8000)
> **Critical**: GitHub agent templates MUST work - test fails immediately if GitHub agents fail
> **Last Updated**: 2025-12-09 (comprehensive rewrite - all 8 agents, context tracking, scheduling, workplans)

---

## Test Agents Overview (8 Total - GitHub-Based)

| Agent | Purpose | Key Tests | Status |
|-------|---------|-----------|--------|
| test-echo | Basic chat | Response format, context growth | ✅ Template exists |
| test-counter | State persistence | File I/O, counter.txt tracking | ✅ Template exists |
| test-worker | Workplan system | Task creation, dependencies, completion | 🚧 NEW - needs UI validation |
| test-delegator | Agent-to-agent (Pillar II) | Trinity MCP, delegation, context sharing | ✅ Template exists |
| test-scheduler | Scheduling/autonomy | Cron execution, scheduled runs, logs | 🚧 NEW - needs UI validation |
| test-queue | Execution queue | Concurrency limits, 429 responses, retry | 🚧 NEW - needs UI validation |
| test-files | File browser | File creation, listing, download, permissions | 🚧 NEW - needs UI validation |
| test-error | Error handling | Failure modes, recovery, logging | 🚧 NEW - needs UI validation |

---

## Automated Test Option (Quick Validation)

For quick validation, run the automated test script:

```bash
python3 docs/testing/run_integration_test.py
```

The automated test covers core functionality:
- Agent creation (GitHub templates only)
- Basic chat with test-echo
- State persistence with test-counter
- Agent-to-agent collaboration with test-delegator
- Dashboard APIs (context stats, activity timeline, plans)

**Output**: 26 tests, ~3 minutes runtime, clean summary at end.

**Important**: This does NOT cover workplans, scheduling, or advanced features. Use the manual comprehensive test for full coverage.

---

## Pre-Test Setup (Manual Testing)

### Quick Setup Script (Run Once)
```bash
python3 << 'SETUP'
import requests

BASE = "http://localhost:8000"
TEST_AGENTS = ["test-echo", "test-counter", "test-delegator", "test-worker",
               "test-scheduler", "test-queue", "test-files", "test-error"]

# 1. Verify services
try:
    r = requests.get(f"{BASE}/health", timeout=3)
    print(f"✓ Backend: {r.json()['status']}")
except:
    print("✗ Backend not running"); exit(1)

try:
    r = requests.get("http://localhost:3000", timeout=3)
    print(f"✓ Frontend: responding")
except:
    print("✗ Frontend not running"); exit(1)

# 2. Get token
r = requests.post(f"{BASE}/api/token",
    data={"username": "admin", "password": os.getenv("ADMIN_PASSWORD", "changeme")})
TOKEN = r.json()["access_token"]
headers = {"Authorization": f"Bearer {TOKEN}"}
print(f"✓ Token obtained")

# 3. Clean existing test agents
agents = requests.get(f"{BASE}/api/agents", headers=headers).json()
for a in agents:
    if a["name"] in TEST_AGENTS:
        requests.delete(f"{BASE}/api/agents/{a['name']}", headers=headers)
        print(f"  Deleted: {a['name']}")

# 4. Verify templates available
templates = requests.get(f"{BASE}/api/templates", headers=headers).json()
test_templates = [t for t in templates if "test-agent-" in t.get("id", "")]
print(f"✓ Test templates: {len(test_templates)}/8 available")

# 5. Show current state
agents = requests.get(f"{BASE}/api/agents", headers=headers).json()
print(f"✓ Clean slate: {len(agents)} agents")
print("\n=== Ready for testing ===")
SETUP
```

### Environment Checklist
- [ ] Backend running at http://localhost:8000
- [ ] Frontend running at http://localhost:3000
- [ ] Docker daemon running
- [ ] No existing test agents (clean slate)

> **Note**: The setup script authenticates via API but does NOT log you into the browser. You must complete the Authentication steps below before testing.

### Authentication
- [ ] Navigate to http://localhost:3000
- [ ] **Refresh the page** (clears any stale cached state from previous sessions)
- [ ] Login with admin/YOUR_PASSWORD (from `.env`)
- [ ] "Connected" indicator shows green in navbar

---

## Test 1: Agent Creation (test-echo)

### Templates Page
- [ ] Navigate to /templates
- [ ] Page loads without errors
- [ ] GitHub templates section visible
- [ ] "Test: Echo" template visible (under GitHub Templates)
- [ ] Template card shows description

### Create Agent Modal
- [ ] Click "Use Template" on test-echo
- [ ] Modal opens
- [ ] **Note**: Due to a known bug, the modal may show "Blank Agent" selected instead of the clicked template. Manually select the correct template from the dropdown if needed.
- [ ] Enter name: "test-echo"
- [ ] Click "Create Agent"
- [ ] Modal closes
- [ ] Success toast/notification appears

> **Alternative (API)**: If template selection is problematic, create via API:
> ```bash
> curl -X POST http://localhost:8000/api/agents \
>   -H "Authorization: Bearer $TOKEN" \
>   -H "Content-Type: application/json" \
>   -d '{"name": "test-echo", "template": "github:abilityai/test-agent-echo"}'
> ```

### Agents Page Validation
- [ ] Navigate to /agents (or redirected automatically)
- [ ] "test-echo" card appears in list
- [ ] Status indicator shows "Starting" (yellow/orange)
- [ ] Status transitions to "Running" (green)
- [ ] Template name displayed on card
- [ ] Context indicator shows 0%

> **IMPORTANT - Wait for Agent Initialization**: After the agent shows "Running", wait **10-15 seconds** before sending chat messages. The agent needs time to initialize its internal server. Sending messages too early results in 503 errors.

### Dashboard Validation
- [ ] Navigate to / (Dashboard)
- [ ] test-echo node appears in network graph
- [ ] Node shows green status indicator
- [ ] Node shows "Idle" state
- [ ] Context bar shows 0%
- [ ] Tasks shows "—"

---

## Test 2: Basic Chat (test-echo)

### Agent Detail Page
- [ ] Click on test-echo (from Dashboard or Agents page)
- [ ] Agent detail page loads
- [ ] Chat tab is active by default
- [ ] Chat input field visible and enabled
- [ ] Agent status shows "Running" / "Idle"

### Send Message
- [ ] Type "Hello World" in chat input
- [ ] Press Enter or click Send
- [ ] Input field clears
- [ ] User message appears in chat (right-aligned)
- [ ] Loading/typing indicator appears
- [ ] Agent response streams in progressively

### Response Validation
- [ ] Response contains "Echo: Hello World"
- [ ] Response contains "Words: 2"
- [ ] Response contains "Characters: 11"
- [ ] Response appears left-aligned (agent message)
- [ ] Timestamp visible on message

### Activity Panel
- [ ] Activity panel visible (right side or tab)
- [ ] Shows execution activity
- [ ] Cost displayed (should be low with Haiku)
- [ ] Duration displayed

### Context Update - CRITICAL VALIDATION
- [ ] Context percentage INCREASED from 0% (not stuck at 0%)
- [ ] Progress bar visually filled based on percentage
- [ ] Progress bar color changes with context load:
  - [ ] Green: 0-50%
  - [ ] Yellow: 50-75%
  - [ ] Orange: 75-90%
  - [ ] Red: >90%
- [ ] Tooltip shows exact tokens: "X / 200K tokens"
- [ ] Context continues to grow as messages accumulate

### Tasks/Workplan - CRITICAL VALIDATION
- [ ] Tasks indicator shows current task (not stuck at "—")
- [ ] Task progress bar visible when tasks active
- [ ] Task name displayed: e.g., "Task 1/5"
- [ ] Task status shows in sidebar or activity panel

---

## Test 3: State Persistence (test-counter)

### Create Agent
- [ ] Create test-counter from template (same flow as test-echo)
- [ ] Agent appears and starts successfully
- [ ] Status shows "Running"

### Counter Operations
- [ ] Send: "reset"
- [ ] Response: "Counter: 0 (previous: N/A - file created)" on first reset, or "Counter: 0 (previous: X)" if counter existed
- [ ] Send: "increment"
- [ ] Response: "Counter: 1 (previous: 0)"
- [ ] Send: "add 10"
- [ ] Response: "Counter: 11 (previous: 1)"

### Activity Panel - Tool Calls
- [ ] Activity shows "Read" tool call (reading counter.txt)
- [ ] Activity shows "Write" tool call (writing counter.txt)
- [ ] Tool calls appear in chronological order

### File Browser (Info Tab)
- [ ] Click "Info" or "Files" tab
- [ ] File browser section visible
- [ ] counter.txt file listed
- [ ] File shows correct size

### Context Growth
- [ ] Context percentage higher than after single message
- [ ] Multiple messages = progressive context growth

---

## Test 4: Agent-to-Agent Collaboration (test-delegator)

### Create Delegator
- [ ] Create test-delegator from template
- [ ] Agent starts successfully
- [ ] Status shows "Running"

### List Agents
- [ ] Send: "list agents"
- [ ] Response lists test-echo, test-counter, test-delegator
- [ ] Shows running status for each

### Delegate Message
- [ ] Send: "delegate to test-echo: ping from delegator"
- [ ] Loading indicator appears (expect **10-15 seconds** for delegation round-trip)
- [ ] Response received from test-echo via delegation
- [ ] Response includes "Echo: ping from delegator"

> **Delegation Timing**: Agent-to-agent communication involves: (1) delegator processing request, (2) MCP tool call, (3) target agent processing, (4) response back. Typical round-trip is 10-15 seconds.

### Activity Panel - MCP Tools
- [ ] Activity shows `mcp__trinity__chat_with_agent` tool call
- [ ] Tool call shows target agent name
- [ ] **Delegation metadata visible in Activity panel**: source agent, target agent, duration, cost, timestamp
- [ ] Response time displayed

### Dashboard Collaboration Edge
- [ ] Navigate to Dashboard (/)
- [ ] Edge visible between test-delegator and test-echo
- [ ] Edge shows message count label (e.g., "2x")
- [ ] Both agent nodes visible with correct status

---

## Test 5: Dashboard Overview

### Agent Network Graph
- [ ] All 3 agents visible as nodes
- [ ] Nodes positioned reasonably (not overlapping)
- [ ] Each node shows:
  - [ ] Agent name
  - [ ] Status (Idle/Running)
  - [ ] Template repository
  - [ ] Context percentage
  - [ ] Tasks indicator

### Collaboration Visualization
- [ ] Edges visible between collaborating agents
- [ ] Edge labels show message counts
- [ ] Mini-map visible in corner
- [ ] Zoom controls work (+/-)

### Stats Bar
- [ ] Shows correct agent count (3)
- [ ] Shows correct running count (3)
- [ ] Shows message count for time period

### Controls
- [ ] Live/Replay toggle visible
- [ ] Time range selector works (1h, 6h, 24h, 3d, 7d)
- [ ] Refresh button works
- [ ] Reset Layout button works

---

---

## CRITICAL VALIDATIONS - Must Pass for All Tests

### Context Progress Bar (DO NOT SKIP)
These validations apply to **EVERY** agent chat test:

**Before Message**:
- [ ] Context shows "0 / 200K (0%)" or recent value
- [ ] Progress bar is empty or at previous %
- [ ] Color: Green (0-50%)

**After Message**:
- [ ] Context % INCREASED (not stuck at 0%)
- [ ] Progress bar VISUALLY FILLED
- [ ] Tooltip shows "X / 200K tokens"
- [ ] If multiple messages, percentage continues to grow

**Color Gradient Validation**:
- [ ] 0-50%: Green ✓
- [ ] 50-75%: Yellow (if reach this level)
- [ ] 75-90%: Orange (if reach this level)
- [ ] >90%: Red (if reach this level)

**If Context Stuck at 0%**: **FAILED TEST**
- This indicates context calculation bug
- Report as critical issue
- Do NOT continue testing

### Tasks/Workplan Progress (DO NOT SKIP)
These validations apply to agents with active workplans:

**Baseline State**:
- [ ] Tasks indicator shows "—" (empty, no active tasks)

**During Task Execution**:
- [ ] Tasks indicator shows actual task name: "Task 1/5", "Parse documents 2/3", etc.
- [ ] Task progress bar visible
- [ ] Task name updates as execution progresses

**If Tasks Stuck at "—"**: **POTENTIAL ISSUE**
- Check if agent actually created workplan
- Check Plans tab for workplan status
- If Plans empty but tasks show "—": FAILED TEST

---

## Test 6: Workplan System (test-worker)

### Create Workplan Agent
- [ ] Create test-worker from GitHub template
- [ ] Agent shows "Running" status
- [ ] Trinity injection confirmed in logs: "Trinity meta-prompt injected"

### Trigger Workplan Creation
- [ ] Send: "Create a workplan to process tasks"
- [ ] Agent creates workplan with multiple tasks
- [ ] Response includes plan ID and task list

### Verify Workplan in UI
- [ ] Navigate to agent detail page
- [ ] Click "Plans" tab (new tab for workplans)
- [ ] Workplan list visible with status badges
- [ ] Status badges show: pending/active/completed/failed/paused
- [ ] Plan summary shows: Tasks count, completion %, time elapsed

### Task Details & Progress
- [ ] Click on workplan to expand details
- [ ] Task list shows with dependencies
- [ ] Task status indicators: ● pending, ◉ active, ✓ completed, ✗ failed
- [ ] Tasks in dependency order (blocked tasks grayed out)
- [ ] Progress bar for overall plan completion

### Context Growth During Workplan
- [ ] Context percentage increases as tasks execute
- [ ] Context bar fills progressively
- [ ] No tasks should be stuck at "—" (must show actual task names)

### Workplan Completion
- [ ] Send follow-up commands to complete remaining tasks
- [ ] Verify tasks transition from pending → active → completed
- [ ] When all tasks done, plan status changes to "completed"
- [ ] Plan moves from "active" to archive view

---

## Test 7: Scheduling & Autonomy (test-scheduler)

### Create Scheduled Agent
- [ ] Create test-scheduler from GitHub template
- [ ] Agent shows "Running" status

### Create Schedule
- [ ] Click "Schedules" tab in agent detail
- [ ] Click "Create Schedule" button
- [ ] Fill form:
  - [ ] Name: "test-schedule"
  - [ ] Cron: "*/5 * * * *" (every 5 minutes)
  - [ ] Message: "Execute scheduled task"
  - [ ] Timezone: UTC
- [ ] Click "Create"
- [ ] Schedule appears in list with status "enabled"

### Verify Schedule Execution
- [ ] Wait 5 minutes (or manually trigger)
- [ ] Click "Executions" or "Execution History"
- [ ] Execution shows:
  - [ ] Timestamp of run
  - [ ] Status: "completed" or "failed"
  - [ ] Duration: execution time
  - [ ] Response: agent output
  - [ ] Cost: API cost for execution

### Manual Trigger Test
- [ ] Click "Run Now" / "Trigger" button on schedule
- [ ] Execution runs immediately
- [ ] New execution appears in history with current timestamp

### Disable/Enable Schedule
- [ ] Click disable button next to schedule
- [ ] Schedule status changes to "disabled"
- [ ] Re-enable and verify it runs again

### Multiple Schedules
- [ ] Create 2-3 additional schedules with different cron times
- [ ] Verify all execute independently on their timelines
- [ ] Verify each execution tracked separately

---

## Test 8: Execution Queue & Concurrency (test-queue)

### Create Queue Test Agent
- [ ] Create test-queue from GitHub template
- [ ] Agent shows "Running" status

### Single Execution
- [ ] Send: "execute with 2 second delay"
- [ ] Verify execution completes normally
- [ ] Check response time

### Queue Behavior - Rapid Requests
- [ ] Send message 1: "execute task 1"
- [ ] Immediately send message 2: "execute task 2"
- [ ] Immediately send message 3: "execute task 3"
- [ ] Verify queue handles concurrent requests:
  - [ ] First request executes immediately
  - [ ] Subsequent requests queued
  - [ ] All complete (no 429 errors expected if queue working)
  - [ ] All appear in activity timeline in order

### 429 Response Handling (if queue full)
- [ ] Send 10+ rapid requests to overload queue
- [ ] If agent returns 429 (Too Many Requests):
  - [ ] Verify UI shows error message
  - [ ] Verify retry logic kicks in (should retry automatically)
  - [ ] Verify queue drains and subsequent requests succeed

### Activity Timeline Queue Tracking
- [ ] All queued executions appear in Activity panel
- [ ] Timestamps show queue order
- [ ] Duration shows wait time + execution time

---

## Test 9: File Browser & Persistence (test-files)

### Create Files Agent
- [ ] Create test-files from GitHub template
- [ ] Agent shows "Running" status

### File Creation
- [ ] Send: "create a test.txt file with content"
- [ ] Wait for response

### File Browser Validation
- [ ] Click "Files" tab on agent detail page
- [ ] File list shows recursively from /home/developer/workspace/
- [ ] test.txt visible in list with:
  - [ ] Filename
  - [ ] Size (bytes)
  - [ ] Last modified date
  - [ ] Download button

### File Operations
- [ ] Send: "create documents/report.md with content"
- [ ] Verify directory structure in file browser:
  - [ ] documents/ folder shown
  - [ ] report.md nested under documents
  - [ ] Tree view expandable/collapsible

### Download File
- [ ] Click download button on test.txt
- [ ] File downloads to local machine
- [ ] Verify content matches what agent created

### File Limits
- [ ] Verify file browser skips hidden files (.env, .git, etc.)
- [ ] Verify no files >100MB are downloadable (security check)
- [ ] Verify only workspace files accessible (no /etc, /var, etc.)

### Multiple Files
- [ ] Agent creates 5+ files
- [ ] All appear in file browser
- [ ] Can download each one
- [ ] Organize by directory structure

---

## Test 10: Error Handling & Recovery (test-error)

### Create Error Agent
- [ ] Create test-error from GitHub template
- [ ] Agent shows "Running" status

### Controlled Failures
- [ ] Send: "fail with invalid command"
- [ ] Verify error message displayed in chat
- [ ] Error message includes:
  - [ ] What went wrong
  - [ ] Suggested fix
  - [ ] Error code (if applicable)

### Error Activity Logging
- [ ] Check Activity panel
- [ ] Failed execution shows:
  - [ ] Status: "failed"
  - [ ] Error message
  - [ ] Timestamp
  - [ ] Duration (how long it took to fail)

### Recovery After Error
- [ ] Send: "recover and execute valid task"
- [ ] Verify agent recovers and succeeds
- [ ] Previous error doesn't affect new execution

### Cascading Failures (agent-to-agent)
- [ ] If test-delegator delegates to test-error:
  - [ ] Send error command via delegation
  - [ ] Verify error properly propagated back
  - [ ] Delegator shows: "Target agent failed: [reason]"
  - [ ] Collaboration edge still shows (even with error)

---

## Test 10: Multi-Agent Collaboration & Context Sharing

### Dashboard with All 8 Agents
- [ ] Create all 8 agents (may skip test-worker, test-scheduler if time constraints)
- [ ] Navigate to Dashboard
- [ ] All agents visible as nodes
- [ ] No overlapping nodes (good layout)

### Cross-Agent Communication
- [ ] test-delegator delegates to test-counter
- [ ] Verify edge appears with "1x" label
- [ ] test-delegator delegates to test-files
- [ ] Verify new edge appears with "1x" label
- [ ] Multiple edges visible simultaneously

### Shared Context Tracking
- [ ] Send messages to test-echo (context grows)
- [ ] Send messages to test-counter (context grows separately)
- [ ] Dashboard shows different context % for each agent
- [ ] Context independent per agent (not summed)

### Agent Network Statistics
- [ ] Stats bar shows correct agent count (8 if all created)
- [ ] Stats bar shows correct running count
- [ ] Message count aggregates all agent chats
- [ ] Collaboration count shows number of inter-agent calls

---

## Test 11: Agent Lifecycle (Comprehensive)

### Stop Agent
- [ ] Navigate to test-echo detail page
- [ ] Click Stop button
- [ ] Button shows "Stopping..." (disabled state)
- [ ] Agent status changes to "stopped"
- [ ] Chat tab shows message: "Agent is not running. Start the agent to chat."
- [ ] Context bar remains visible but grayed out
- [ ] Telemetry section replaced with "Created just now"

### Start Agent
- [ ] Click Start button
- [ ] Button shows "Starting..." (disabled state)
- [ ] Agent status changes to "running"
- [ ] Trinity injection triggered (logs show injection)
- [ ] Toast notification: "Agent test-echo started"
- [ ] Telemetry restored (CPU, MEM, NET, UP stats)
- [ ] Chat interface restored with previous chat history
- [ ] Chat input enabled
- [ ] Context bar restored to previous value (history preserved)

### Delete Agent
- [ ] Click Delete button
- [ ] **ConfirmDialog modal** appears (not browser confirm() - testable via `data-testid="confirm-dialog"`)
- [ ] Dialog shows: Title "Delete Agent", Message "Are you sure you want to delete this agent?"
- [ ] Click "Delete" button in dialog (`data-testid="confirm-dialog-confirm"`)
- [ ] Agent removed from list
- [ ] Agent removed from Dashboard
- [ ] Docker container removed
- [ ] Redirected to Agents page

> **Note for UI Testing**: All confirmation dialogs use the in-app `ConfirmDialog.vue` component, not browser `confirm()`. This enables automated testing via Chrome DevTools MCP. Key test IDs:
> - `data-testid="confirm-dialog"` - Dialog container
> - `data-testid="confirm-dialog-title"` - Title element
> - `data-testid="confirm-dialog-message"` - Message element
> - `data-testid="confirm-dialog-confirm"` - Confirm button
> - `data-testid="confirm-dialog-cancel"` - Cancel button

---

## Post-Test Cleanup

### Remove All Test Agents
- [ ] Delete test-echo
- [ ] Delete test-counter
- [ ] Delete test-delegator
- [ ] Delete test-worker (if created)
- [ ] Delete test-scheduler (if created)
- [ ] Delete test-queue (if created)
- [ ] Delete test-files (if created)
- [ ] Delete test-error (if created)
- [ ] Verify Dashboard empty

### Verify Clean State
- [ ] Agents page shows: "No agents - Get started by creating a new agent"
- [ ] Dashboard shows: "0 agents · 0 running · 0 messages (24h)"
- [ ] All Docker containers removed

---

## Test Results Summary

| Test | Status | Notes |
|------|--------|-------|
| Agent Creation (GitHub) | [ ] Pass / [ ] Fail | **CRITICAL**: Must use GitHub templates |
| Basic Chat & Context | [ ] Pass / [ ] Fail | **CRITICAL**: Context % must increase |
| State Persistence | [ ] Pass / [ ] Fail | |
| Agent Collaboration | [ ] Pass / [ ] Fail | |
| Workplan System | [ ] Pass / [ ] Fail | **NEW**: Tasks must appear (not "—") |
| Scheduling | [ ] Pass / [ ] Fail | **NEW**: Executions must run on schedule |
| Execution Queue | [ ] Pass / [ ] Fail | **NEW**: Concurrency handling |
| File Browser | [ ] Pass / [ ] Fail | **NEW**: File listing, download, security |
| Error Handling | [ ] Pass / [ ] Fail | **NEW**: Failure recovery, logging |
| Dashboard Multi-Agent | [ ] Pass / [ ] Fail | **NEW**: 8 agents, all features |

**Overall Result**: [ ] Pass / [ ] Fail
**GitHub Templates**: [ ] All agents created from github:abilityai/* (CRITICAL)

**Tester**: _______________
**Date**: _______________
**Environment**: Local / Staging / Production
**Notes**:

---

## Known Issues & Expected Bugs (2025-12-09)

### CRITICAL ISSUES (Must Be Fixed Before Production)

1. **Context Progress Bar Stuck at 0%**
   - **Status**: 🐛 BUG CONFIRMED (2025-12-09)
   - **Description**: Context percentage remains at 0% despite multiple messages
   - **Expected**: Should increase with each message
   - **Impact**: Critical - Users cannot see token usage
   - **Workaround**: None
   - **Files**: Check `src/frontend/src/views/AgentDetail.vue` context display logic

2. **Task Indicator Stuck at "—"**
   - **Status**: 🐛 Needs Investigation (2025-12-09)
   - **Description**: Tasks column shows "—" even when agent is executing tasks
   - **Expected**: Should show "Task 1/5" or similar during execution
   - **Impact**: High - Users cannot see workplan progress
   - **Workaround**: Check Plans tab instead
   - **Files**: Check `src/frontend/src/views/AgentDetail.vue` task display logic

3. **UI Tab Switching / Page Auto-Refresh**
   - **Status**: 🐛 BUG CONFIRMED (2025-12-09)
   - **Description**: Tab buttons don't work, page constantly refreshing
   - **Impact**: Cannot navigate between tabs reliably
   - **Workaround**: Use API directly for testing

### Known Platform Bugs (Documented)

1. **Template Pre-selection Bug**: Clicking "Use Template" opens modal with "Blank Agent" selected instead of clicked template. Users must manually select from dropdown or use API.

2. **Accessibility Tree Mismatch**: Dashboard accessibility tree describes collaboration edges incorrectly. Affects screen readers and automated testing.

3. **503 Errors During Initialization**: Sending chat messages immediately after agent starts returns 503. Wait ~10 seconds for agent internal server initialization. Automated test handles this with retry logic.

4. **Agents List Empty State**: After certain navigation sequences, agents list page shows "No agents" even though agents exist in backend. Workaround: Reload page.

---

## Key Learnings for Reliable Testing

Based on automated test development (2025-12-08):

1. **Agent Initialization Time**: Wait 10-15 seconds after agent shows "running" before sending chat messages.

2. **MCP Server Initialization**: For agents with MCP servers (like test-delegator), wait an additional 5 seconds for MCP server initialization.

3. **Retry Logic for 503**: If you get a 503 error, wait 5 seconds and retry. The automated test uses 5 retries with 5-second waits.

4. **Delegation Round-Trip**: Agent-to-agent communication takes 10-15 seconds. Don't timeout too early.

5. **Clean Slate**: Always delete existing test agents before starting a test run to ensure consistent results.

6. **ConfirmDialog for All Confirmations**: All confirmation dialogs use the in-app `ConfirmDialog.vue` component, NOT browser `confirm()`. This enables automated UI testing via Chrome DevTools MCP. Locations using ConfirmDialog:
   - **AgentDetail.vue**: Delete agent, New session
   - **Credentials.vue**: Delete credential
   - **ApiKeys.vue**: Revoke key, Delete key
   - **SchedulesPanel.vue**: Delete schedule
   - **WorkplanPanel.vue**: Delete plan

---

## GitHub Agent Testing (CRITICAL)

### Why GitHub Templates Matter

Trinity's test agents are **repository-based** (`github:abilityai/test-agent-*`), not static templates. This means:
- Agents can be updated via Git pull in the agent's workspace
- Template source is the GitHub repository
- Future test improvements update the agent code automatically
- **Testing validates the entire CI/CD pipeline**

### GitHub Template Creation Flow

```
1. User creates agent: POST /api/agents
   - name: "test-echo"
   - template: "github:abilityai/test-agent-echo"

2. Backend fetches from GitHub:
   - Clone repo to agent workspace
   - Create .mcp.json from .mcp.json.template
   - Inject credentials

3. Agent container starts
   - Git working directory available
   - Can execute `git status`, `git log`, `git pull`
   - Workspace synced with GitHub

4. Test validates:
   - Agent created from GitHub template (not local)
   - Agent workspace has .git directory
   - Credentials injected correctly
   - Trinity meta-prompt injected
```

### Validation Checklist: GitHub Agents

For EACH agent created, verify:
- [ ] Agent template shows `github:abilityai/test-agent-{name}`
- [ ] Agent workspace contains `.git/` directory
- [ ] Docker inspect shows: `"trinity.template": "github:abilityai/test-agent-*"`
- [ ] Agent can execute: "git log --oneline" (show recent commits)
- [ ] Agent can execute: "git status" (show working directory state)

### If GitHub Agent Creation Fails

**Failure Modes**:
1. **404 Not Found** - Template doesn't exist in GitHub
   - Check: `https://github.com/abilityai/test-agent-{name}`
   - May not be public
   - May not be created yet

2. **Authentication Error** - No GitHub PAT available
   - Check: `GITHUB_CREDENTIAL_ID` in config.py points to valid credential
   - Check: Backend can access GitHub API

3. **Timeout During Clone** - Large repository or network issue
   - Agent creation takes 30-60 seconds for large repos
   - May timeout if backend not responsive
   - Check: Backend logs for clone progress

4. **Wrong Template Source** - Agent created from local template instead of GitHub
   - API shows: `"template": "local:test-echo"` (WRONG)
   - Should show: `"template": "github:abilityai/test-agent-echo"` (CORRECT)
   - **Test automatically fails if local template used**

### Testing GitHub Sync Capability

Once GitHub agents are created, optionally test sync:

1. Open agent detail page
2. Click "Git" tab (if available)
3. Should show:
   - [ ] Repository: `abilityai/test-agent-{name}`
   - [ ] Branch: `trinity/test-{name}/{instance-id}`
   - [ ] Last sync: timestamp
   - [ ] Commit history

4. Make change to agent workspace (via chat)
5. Click "Sync to GitHub" button (if available)
6. Verify:
   - [ ] Branch updated on GitHub
   - [ ] New commit appears in history
   - [ ] Timestamp updated

---

## Revision History

| Date | Changes |
|------|---------|
| 2025-12-08 | Initial checklist created |
| 2025-12-08 | Refined based on test run: fixed template name, added API workaround, clarified expected responses, updated lifecycle state descriptions, documented known bugs |
| 2025-12-08 | Added automated test script (`run_integration_test.py`), documented key learnings about timing and retries, verified 26/26 tests passing |
| 2025-12-09 | **COMPREHENSIVE REWRITE**: Added all 8 test agents, context/task validation, scheduling, workplans, queue, file browser, error handling. Marked critical bugs. Required GitHub templates. |
