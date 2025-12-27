# Phase 7: Scheduling & Autonomy (test-scheduler)

> **Purpose**: Validate schedule creation, execution, and autonomous agent behavior
> **Duration**: ~15 minutes
> **Assumes**: Phase 5 PASSED (test-scheduler running)
> **Output**: Scheduling system verified with execution history

---

## Background

**Autonomous Execution**:
- Agents execute scheduled tasks without user interaction
- Execution history tracked with timestamps
- Failed schedules can be retried automatically
- Context grows with each autonomous execution

---

## Test: Create Schedule

### Step 1: Navigate to test-scheduler
**Action**:
- Go to http://localhost:3000/agents
- Click test-scheduler
- Wait for detail page to load

**Expected**:
- [ ] Agent detail page loads
- [ ] Terminal tab active (default)
- [ ] Status: "Running" (green)
- [ ] Context: 0% (fresh agent)

---

### Step 2: Create Scheduled Task
**Action**:
- In Terminal, type: "schedule daily at 2:00 PM: check system health, generate report, send alerts"
- Press Enter
- **Wait 15 seconds**

**Expected Response**:
```
Schedule created successfully
Schedule ID: sched-[uuid]
Trigger: Daily at 2:00 PM
Task: Check system health, generate report, send alerts
Status: active
Next execution: [tomorrow at 2:00 PM]
```

**Verify**:
- [ ] Schedule created with ID
- [ ] Trigger time correctly parsed
- [ ] Task description stored
- [ ] Status: active (not pending)
- [ ] Next execution time shown

---

### Step 3: Create One-Time Schedule
**Action**:
- Type: "schedule for 5 minutes from now: run test suite"
- Press Enter
- **Wait 10 seconds**

**Expected Response**:
```
Schedule created successfully
Schedule ID: sched-[uuid]
Trigger: One-time at [time in 5 minutes]
Task: run test suite
Status: pending
Next execution: [in 5 minutes]
```

**Verify**:
- [ ] One-time schedule created
- [ ] Time correctly calculated
- [ ] Status: pending (hasn't executed yet)

---

## Test: Execution History

### Step 4: Wait for Schedule Execution
**Action**:
- Wait 5-6 minutes for one-time schedule to execute
- Monitor chat for execution message

**Expected Response**:
```
Schedule triggered: run test suite
Execution ID: exec-[uuid]
Status: running
Started: [timestamp]
```

**Verify**:
- [ ] Schedule executed at correct time
- [ ] Execution ID generated
- [ ] Status shows "running"

---

### Step 5: Check Execution History
**Action**:
- Type: "show execution history"
- Press Enter
- Wait 5 seconds

**Expected Response**:
```
Execution History:
- Execution 1: run test suite
  Status: completed
  Started: [timestamp]
  Duration: 1:23
  Output: All tests passed

- Execution 2: (pending, will run at [time])
```

**Verify**:
- [ ] Execution history shows past runs
- [ ] Status transitions: pending → running → completed
- [ ] Duration tracked
- [ ] Output/results logged
- [ ] Upcoming executions shown

---

### Step 6: Check Schedule Status
**Action**:
- Type: "list schedules"
- Press Enter
- Wait 5 seconds

**Expected Response**:
```
Active Schedules:
1. Daily check at 2:00 PM (sched-xxx)
   - Last execution: never
   - Next execution: [tomorrow at 2:00 PM]

2. One-time test suite (sched-yyy)
   - Last execution: [just now at HH:MM]
   - Next execution: none (completed)
```

**Verify**:
- [ ] All schedules listed
- [ ] Last execution time shown
- [ ] Next execution time shown
- [ ] Completed one-time schedules marked correctly

---

## Test: Activity Panel Tracking

### Step 7: Verify Activity Panel
**Action**:
- Scroll to Activity section
- Look for schedule-related tool calls

**Expected**:
- [ ] `create_schedule` tool call
- [ ] `execute_schedule` tool call
- [ ] `get_execution_history` tool call
- [ ] Each with duration and parameters

**Verify**:
- [ ] Tool calls recorded
- [ ] Parameters show schedule details
- [ ] Execution timestamps match chat responses

---

## Test: Context Growth with Execution

### Step 8: Check Context After Execution
**Action**:
- Look at context % in agent detail page header
- Compare to Phase 6 context level

**Expected**:
- [ ] Context % increased from Phase 6
- [ ] Significant increase due to autonomous execution
- [ ] Progress bar filled accordingly
- [ ] No agent exceeds 200K limit

**Verify**:
- [ ] Context grows with execution
- [ ] Each schedule execution adds context
- [ ] Color progression visible (green → yellow → orange)

---

## Critical Validations

### Schedule Execution Timing
**Validation**: Schedules execute at correct times

```bash
# Check scheduler logs
docker logs agent-test-scheduler | grep -i "schedule\|trigger\|execution"
```

Expected: Execution timestamps match scheduled times (within 1 minute tolerance)

### Execution History Persistence
**Validation**: History survives page refresh

- [ ] Navigate away from agent
- [ ] Return to test-scheduler
- [ ] Wait 2 seconds for load
- [ ] Execution history still present
- [ ] Same execution IDs and timestamps

### Failed Schedule Retry
**Validation**: Failed schedules can be retried

**Action**: Type "retry last failed schedule"
- [ ] System identifies last failed execution
- [ ] Retry creates new execution record
- [ ] New execution ID generated
- [ ] History shows both original and retry

---

## Success Criteria

Phase 7 is **PASSED** when:
- ✅ Daily schedule created successfully
- ✅ One-time schedule created and executed
- ✅ Execution history shows all runs with status
- ✅ Schedule list shows next execution times
- ✅ Tool calls logged in activity panel
- ✅ Context % increased due to autonomous execution
- ✅ Execution timestamps accurate (within 1 minute)
- ✅ Execution history persisted across refresh
- ✅ Failed schedule retry works

---

## Troubleshooting

**Schedule not executing**:
- Wait longer (5-10 minutes for one-time)
- Check system time is correct: `date`
- Verify test-scheduler running: `docker ps | grep test-scheduler`
- Check logs: `docker logs agent-test-scheduler`

**Execution history empty**:
- Schedule may not have executed yet
- Wait for scheduled time to pass
- Check if schedule was actually created: "list schedules"

**Context not growing**:
- Verify Phase 3 context tracking working
- If stuck at 0%, that's known bug (Phase 3)
- Proceed to next phase anyway

**Schedule lost after page refresh**:
- Database persistence may be broken
- Check backend logs: `docker logs backend`
- Schedule may be stored but not loaded

---

## Next Phase

Once Phase 7 is **PASSED**, proceed to:
- **Phase 8**: Execution Queue (test-queue)

---

**Status**: 🟢 Scheduling & autonomy validated
**Last Updated**: 2025-12-09
