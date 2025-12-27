# Phase 4: State Persistence (test-counter)

> **Purpose**: Validate file I/O, state persistence, AND activity tracking
> **Duration**: ~10 minutes
> **Assumes**: Phase 3 PASSED (context tracking verified)
> **Output**: File operations, persistence, and activity tracking confirmed

---

## Prerequisites

- ✅ Phase 3 PASSED
- ✅ test-counter agent running
- ✅ Context tracking working (or documented as bug)
- ✅ Browser logged in

---

## Activity Tracking Validation

⚠️ **This phase also validates the Activity Panel** since test-counter uses Read/Write tools.

**Expected**: After each command, the Activity Panel should show:
- Tool name (Read, Write)
- Duration (ms)
- Success/failure status
- Tool counts accumulating

**If Activity Panel shows "No activity yet"** after sending commands → **BUG**
(Unlike test-echo which doesn't use tools, test-counter MUST show activity)

---

## Test: Counter State Operations

### Step 1: Navigate to test-counter
**Action**:
- Go to http://localhost:3000/agents
- Click test-counter agent
- Wait for detail page to load

**Expected**:
- [ ] Agent detail page loads
- [ ] Terminal tab active (default)
- [ ] Status: "Running" (green)
- [ ] Context: 0% or previous value
- [ ] Terminal ready for input

---

### Step 2: Send "reset" Command
**Action**:
- In Terminal, type: "reset"
- Press Enter
- Wait 10-15 seconds for response

**Expected Response**:
```
Counter: 0 (previous: N/A - file created)
```

Or if counter.txt already exists:
```
Counter: 0 (previous: X)
```

**Verify**:
- [ ] Response format matches above
- [ ] Counter value is 0
- [ ] "previous" value shown
- [ ] Output appears in terminal

---

### Step 3: Send "increment" Command
**Action**:
- Type: "increment"
- Press Enter
- Wait 10-15 seconds

**Expected Response**:
```
Counter: 1 (previous: 0)
```

**Verify**:
- [ ] Counter incremented from 0 to 1
- [ ] Previous value shown (0)
- [ ] Output in terminal

---

### Step 4: Send "add 10" Command
**Action**:
- Type: "add 10"
- Press Enter
- Wait 10-15 seconds

**Expected Response**:
```
Counter: 11 (previous: 1)
```

**Verify**:
- [ ] Counter calculated: 1 + 10 = 11
- [ ] Previous value shown (1)
- [ ] Arithmetic correct
- [ ] Output in terminal

---

### Step 5: Send "get" Command (Read-Only)
**Action**:
- Type: "get"
- Press Enter
- Wait 5-10 seconds

**Expected Response**:
```
Counter: 11 (previous: 11)
```

**Verify**:
- [ ] No change (read-only operation)
- [ ] Previous = current (both 11)
- [ ] Response time faster (no write)

---

### Step 6: Check Activity Panel (CRITICAL)
**Action**:
- Look at Activity Panel on right side
- Or click "View Timeline" if available

⚠️ **This is the ACTIVITY TRACKING validation** (unlike test-echo which shows "No activity")

**Expected - Tool Calls**:
For each message, should show:
- [ ] "reset" command: Read + Write (2 tools)
- [ ] "increment" command: Read + Write (2 tools)
- [ ] "add 10" command: Read + Write (2 tools)
- [ ] "get" command: Read only (1 tool)

**Tool Count Summary** (after 4 commands):
- Read: 4 calls
- Write: 3 calls
- Total: 7 tool executions

**Verify**:
- [ ] Activity Panel shows tool calls (NOT "No activity yet")
- [ ] Tool calls listed chronologically (newest first)
- [ ] Tool counts displayed (e.g., "Read x4", "Write x3")
- [ ] Timestamps shown
- [ ] Duration shown for each
- [ ] Status shown (success/failure)

**If Activity Panel shows "No activity yet"** → **BUG** in activity tracking
(test-counter MUST use tools, unlike test-echo which doesn't)

---

### Step 7: Verify counter.txt File
**Action**: Click "Files" tab (if available in agent detail)

**Expected**:
- [ ] File list shows workspace files
- [ ] counter.txt visible
- [ ] File size: 2 bytes (for "11")
- [ ] Last modified: recent timestamp
- [ ] Download button available

**Alternative: Docker Verification**
```bash
docker exec agent-test-counter cat /home/developer/workspace/counter.txt
```

**Expected Output**:
```
11
```

**Verify**:
- [ ] File exists and contains: "11"

---

### Step 8: State Persistence Across Commands
**Action**: Review terminal output history

**Expected - Complete Sequence**:
```
$ reset
Counter: 0 (previous: N/A - file created)

$ increment
Counter: 1 (previous: 0)

$ add 10
Counter: 11 (previous: 1)

$ get
Counter: 11 (previous: 11)
```

**Verify**:
- [ ] Previous values match current from previous message
- [ ] State progressed: 0 → 1 → 11 → 11
- [ ] File content on disk (counter.txt) matches current counter (11)

---

## Test: Context Growth During State Operations

### Step 9: Verify Context Increased
**Action**: Look at context indicator

**Expected**:
- [ ] Context % > 0% (increased from Phase 3)
- [ ] 4 messages sent, context should grow
- [ ] Progress bar filled more than before

---

## Critical Validations

### State Correctness
**Validation**: Counter arithmetic

Test each operation:
- [x] reset: 0 ✓
- [x] increment: 0 + 1 = 1 ✓
- [x] add 10: 1 + 10 = 11 ✓
- [x] get: no change ✓

### File Persistence
**Validation**: counter.txt on disk

```bash
docker exec agent-test-counter ls -la /home/developer/workspace/
```

**Expected**:
- [ ] counter.txt exists
- [ ] Modified time: recent
- [ ] Content: "11"

### Activity Tracking
**Validation**: Tool calls logged

Each operation should show:
- [ ] Read tool call (reading counter.txt)
- [ ] Write tool call (writing counter.txt) - except for "get"
- [ ] Timestamps and duration

---

## Success Criteria

Phase 4 is **PASSED** when:
- ✅ reset → counter = 0
- ✅ increment → counter = 1
- ✅ add 10 → counter = 11
- ✅ get → counter = 11 (no change)
- ✅ counter.txt exists on disk with correct value
- ✅ Tool calls properly logged (Read/Write)
- ✅ State persists across multiple messages
- ✅ Context continues to grow

---

## Troubleshooting

**Agent doesn't respond**:
- Wait 10-15 seconds (agent initialization)
- Check: `docker logs agent-test-counter | tail -20`
- Verify agent running: `docker ps | grep test-counter`

**Wrong counter value**:
- Verify counter.txt content: `docker exec agent-test-counter cat /home/developer/workspace/counter.txt`
- Check if multiple counter.txt files exist
- Reset and start fresh if corrupted

**Activity panel empty**:
- Refresh page (F5)
- Check browser console for errors
- Activity may be loading (wait 2-3 seconds)

**File not visible in UI**:
- Files tab may not be implemented yet
- Verify file exists via Docker: `docker exec agent-test-counter ls /home/developer/workspace/counter.txt`
- Use API: `curl http://localhost:8000/api/agents/test-counter/files`

---

## Next Phase

Once Phase 4 is **PASSED**, proceed to:
- **Phase 5**: Agent-to-Agent Collaboration (test-delegator)
- **Phase 7**: Scheduling (test-scheduler)

---

**Status**: 🟢 State persistence validated
