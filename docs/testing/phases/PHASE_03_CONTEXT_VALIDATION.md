# Phase 3: Context Progress Bar Validation (CRITICAL)

> **Purpose**: Validate context % increases as agents consume tokens (CRITICAL BUG)
> **Duration**: ~10 minutes
> **Assumes**: Phase 2 PASSED (8 agents created, all running)
> **Output**: Context tracking verified or bugs documented

---

## Prerequisites

- ✅ Phase 2 PASSED
- ✅ 8 agents running with GitHub templates
- ✅ All agents show status "running" (green)
- ✅ All agents show Context 0%

---

## Important: Activity Panel Behavior

⚠️ **The Activity Panel shows TOOL EXECUTIONS only** (Read, Write, MCP calls).

| Agent | Uses Tools? | Activity Panel |
|-------|-------------|----------------|
| test-echo | **NO** | "No activity yet" (EXPECTED) |
| test-counter | **YES** (Read/Write) | Shows tool calls ✅ |
| test-delegator | **YES** (MCP) | Shows MCP calls ✅ |

**If test-echo shows "No activity yet"** → This is CORRECT behavior, not a bug.
The echo agent returns text without using tools. To test activity tracking, use test-counter in Phase 4.

---

## Critical Context Validation

### Background

**KNOWN ISSUE (2025-12-09)**: Context progress bar stuck at 0%
- Multiple messages sent but context % doesn't increase
- Progress bar not visually filling
- This is a **CRITICAL BUG** preventing observable token tracking

**Goal of This Phase**: Determine if bug still exists or has been fixed

---

## Test: test-echo Context Growth

### Step 1: Open Agent Detail Page
**Action**:
- Navigate to http://localhost:3000/agents
- Click on test-echo agent card
- Wait for detail page to load

**Expected**:
- [ ] Agent detail page loads
- [ ] Chat tab is active (default)
- [ ] Context shows: "0 / 200K (0%)"
- [ ] Progress bar is **empty** (no fill)
- [ ] Progress bar is **green** (0-50% color)

---

### Step 2: Send First Message
**Action**:
- Type: "Hello World"
- Press Enter or click Send
- Wait 5-10 seconds for response

**Expected**:
- [ ] User message appears (right-aligned)
- [ ] Loading indicator appears
- [ ] Agent response streams in (left-aligned)
- [ ] Response: "Echo: Hello World\nWords: 2\nCharacters: 11"

---

### Step 3: Check Context AFTER First Message
**CRITICAL VALIDATION**: Context must have increased

**Action**: Look at context indicator

**Current State (BEFORE FIX)**:
- Context shows: "0 / 200K (0%)"  ❌ STUCK
- Progress bar: Empty ❌ NO FILL
- This is the bug

**Expected (AFTER FIX)**:
- Context shows: "X / 200K (Y%)" where X > 0, Y > 0  ✅
- Progress bar: **Visually filled** to Y% ✅
- Color: Green (since <50%) ✅

**Verify One of Two Outcomes**:

**Outcome A: BUG FIXED** ✅
- [ ] Context % increased from 0%
- [ ] Progress bar shows visual fill
- [ ] Tooltip shows: "X / 200K tokens"
- **Continue to Step 4**

**Outcome B: BUG STILL EXISTS** ❌
- [ ] Context % stuck at 0%
- [ ] Progress bar empty
- [ ] Clicking doesn't show tooltip
- **Document as FAILED, skip to Issue Documentation**

---

### Step 4: Send Multiple Messages (If Bug Fixed)
**Action**: Send 5+ messages to accumulate context

Messages to send:
1. "What is 2+2?"
2. "List the first 10 prime numbers"
3. "Explain machine learning in one paragraph"
4. "What are the benefits of Python?"
5. "Give an example of recursion"

**After Each Message**:
- [ ] Context % increases
- [ ] Progress bar fill increases
- [ ] Tooltip updates with new token count

**Expected Pattern**:
```
Message 1: 0% → X%
Message 2: X% → Y%
Message 3: Y% → Z% (should keep growing)
Message 4: Z% → A%
Message 5: A% → B%
```

Where X < Y < Z < A < B (always increasing)

---

### Step 5: Verify Color Progression
**Action**: Send messages until context reaches different thresholds

**Expected Color Changes**:
- [ ] 0-50%: Green ✓ (should see in first 5 messages)
- [ ] 50-75%: Yellow (may not reach in 5 messages, that's OK)
- [ ] 75-90%: Orange (may not reach)
- [ ] >90%: Red (unlikely without 20+ messages)

**Minimum Validation**: Should at least see GREEN color, not stuck.

---

### Step 6: Verify Context Tooltip
**Action**: Hover over progress bar

**Expected**:
- [ ] Tooltip appears
- [ ] Shows: "X tokens / 200K tokens"
- [ ] Shows: "Context: Y%"

**Example Tooltip**:
```
2,847 tokens / 200,000 tokens
Context: 1.4%
```

---

## Test: test-counter Context Growth

### Step 7: Switch to test-counter
**Action**:
- Navigate to Agents list
- Click on test-counter
- Verify context shows 0%

---

### Step 8: Send State Commands
**Action**: Send 3 commands to test-counter

Commands:
1. "reset"
2. "increment"
3. "add 10"

**After Each Command**:
- Verify context increases
- Verify different from test-echo context
- Each agent should have **independent** context

**Verify**:
- [ ] test-counter context grows
- [ ] test-echo context remains unchanged (different agent)
- [ ] Each agent tracks context separately

---

## Critical Bug Assessment

### If Context Stuck at 0% (Bug Confirmed)

**Documentation**:
```
BUG: Context Progress Bar Stuck at 0%
Status: CONFIRMED
Date: 2025-12-09
Agents Tested: test-echo, test-counter
Behavior:
  - Multiple messages sent
  - Context always shows "0 / 200K (0%)"
  - Progress bar never fills
  - Tooltip not available
Impact: CRITICAL - Users cannot see token usage
Files to Investigate:
  - src/frontend/src/views/AgentDetail.vue (context display)
  - src/frontend/src/stores/agents.js (context state)
  - Backend context calculation in chat endpoint
```

**Do Not Continue to Other Phases**: This bug blocks testing

---

### If Context Tracking Works (Bug Fixed)

**Validation Summary**:
```
✅ Context Increases: Yes
✅ Progress Bar Fills: Yes
✅ Color Progression: Correct
✅ Tooltip Shows Data: Yes
✅ Independent per Agent: Yes
```

**Continue to Next Phases**

---

## Success Criteria

Phase 3 is **PASSED** when **ONE** of these is true:

**Scenario 1: Bug Fixed** ✅
- [ ] Context % increases with messages (not 0%)
- [ ] Progress bar visually fills
- [ ] Tooltip shows token count
- [ ] Colors change (0-50% green, 50%+ yellow, etc.)
- [ ] Multiple agents show independent context
- **Move to Phase 4**

**Scenario 2: Bug Confirmed** ⚠️
- [ ] Context stuck at 0% for all agents
- [ ] Progress bar never fills
- [ ] Bug documented with details
- **STOP: Requires investigation and fix before proceeding**

---

## Troubleshooting

**Context not visible**:
- Try refreshing page (F5)
- Check browser console for errors
- Verify agent is running: `docker ps | grep test-echo`

**Tooltip doesn't show**:
- Hover directly on progress bar (not text)
- May require hovering for 1-2 seconds
- Check if browser tooltips enabled

**Agent not responding to messages**:
- Wait 10-15 seconds after agent started
- Agent internal server needs initialization time
- Check logs: `docker logs agent-test-echo | tail -20`

---

## Next Phases (If Bug Fixed)

- **Phase 4**: State Persistence (test-counter)
- **Phase 5**: Agent-to-Agent Collaboration (test-delegator)
- **Phase 7**: Scheduling (test-scheduler)
- **Phase 8**: Execution Queue (test-queue)
- **Phase 9**: File Browser (test-files)
- **Phase 10**: Error Handling (test-error)
- **Phase 11**: Multi-Agent Dashboard

---

**Status**: 🟡 CRITICAL - Context tracking validation in progress
