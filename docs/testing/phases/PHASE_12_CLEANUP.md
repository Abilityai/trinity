# Phase 12: Cleanup (Delete All Agents)

> **Purpose**: Remove all test agents and restore clean state
> **Duration**: ~5 minutes
> **Assumes**: Phase 11 PASSED (all testing complete)
> **Output**: All agents deleted, system ready for next test run

---

## Background

**Cleanup Process**:
- Delete all 8 test agents from system
- Verify Docker containers removed
- Check database cleaned up
- Confirm UI reflects empty state

---

## Test: Delete Agents via UI

### Step 1: Navigate to Agents List
**Action**:
- Go to http://localhost:3000/agents
- Wait 2 seconds for page load

**Expected**:
- [ ] Agents list shows all 8 agents
- [ ] Each agent has delete button (trash icon)
- [ ] All agents status shows "Running"

**Verify**:
- [ ] 8 agents visible
- [ ] Delete controls available

---

### Step 2: Delete test-echo
**Action**:
- Find test-echo in list
- Click trash/delete icon
- Confirm deletion in dialog

**Expected Dialog**:
```
Delete Agent: test-echo
Are you sure? This action cannot be undone.
[Cancel] [Delete]
```

**Expected Result**:
- [ ] Dialog appears
- [ ] Agent removed from list
- [ ] Notification shown: "test-echo deleted successfully"
- [ ] List updates (7 agents remain)

**Verify**:
- [ ] Agent deleted
- [ ] List updated

---

### Step 3: Delete Remaining 7 Agents
**Action**:
- Repeat delete process for each agent:
  - test-counter
  - test-delegator
  - test-worker
  - test-scheduler
  - test-queue
  - test-files
  - test-error

**For Each Agent**:
- Click delete
- Confirm in dialog
- Wait 2 seconds for removal
- **Total time: ~2-3 minutes for all 7**

**Expected**:
- [ ] Each agent deleted successfully
- [ ] List shrinks (8 → 7 → 6 ... → 0)
- [ ] No errors during deletion
- [ ] Notifications appear for each deletion

---

### Step 4: Verify Empty Agents List
**Action**:
- After all deletions, refresh page (F5)
- Wait 2 seconds

**Expected**:
- [ ] Agents list is empty
- [ ] Message: "No agents created yet"
- [ ] Create Agent button visible and ready
- [ ] No agents in the list

**Verify**:
- [ ] Complete list empty
- [ ] UI confirms no agents
- [ ] Dashboard would show empty graph (no nodes)

---

## Test: Docker Container Cleanup

### Step 5: Verify Docker Containers Removed
**Action**:
- Run: `docker ps | grep agent-test`
- Wait for output

**Expected**:
- [ ] No output (no containers found)
- [ ] Command returns empty result

**Verify**:
- [ ] All 8 agent containers deleted
- [ ] No orphaned containers
- [ ] Only Trinity infrastructure containers remain (backend, frontend, mcp-server, etc.)

---

### Step 6: Check Historical Containers
**Action**:
- Run: `docker ps -a | grep agent-test`
- Shows all containers (including stopped)

**Expected**:
- [ ] May see stopped containers from earlier
- [ ] Shows clean history
- [ ] No errors

**Verify** (informational only):
- [ ] Historical record preserved
- [ ] No errors in Docker API

---

## Test: Database Cleanup

### Step 7: Verify Database Cleaned
**Action**:
- Run: `docker logs backend | tail -50 | grep -i delete`
- Shows recent delete operations

**Expected Output**:
- [ ] Delete operations logged for each agent
- [ ] Timestamps match deletion times
- [ ] No errors in deletion process

**Verify** (informational):
- [ ] Backend logged all deletions
- [ ] Database consistency maintained

---

### Step 8: Check API Response
**Action**:
- Run: `curl http://localhost:8000/api/agents`
- Wait 2 seconds

**Expected Response**:
```json
{
  "agents": [],
  "total": 0,
  "status": "success"
}
```

**Verify**:
- [ ] Empty agents array
- [ ] Total count is 0
- [ ] No errors
- [ ] API returns successfully

---

## Test: Verification Commands

### Step 9: Comprehensive Verification
**Action**:
- Run verification commands:

```bash
# Check UI
curl http://localhost:3000 -o /dev/null -s -w "%{http_code}\n"
# Expected: 200

# Check backend
curl http://localhost:8000/api/health -o /dev/null -s -w "%{http_code}\n"
# Expected: 200

# Check agents list
docker ps | grep -c agent-test-
# Expected: 0

# Check database health
docker logs backend | tail -5
# Expected: No errors
```

**Verify**:
- [ ] UI responds (200)
- [ ] Backend responds (200)
- [ ] No agent containers
- [ ] No database errors

---

## Test: Ready for Next Test Run

### Step 10: Confirm Clean Slate
**Action**:
- Go to http://localhost:3000 (dashboard)
- Wait 3 seconds

**Expected Dashboard State**:
- [ ] Dashboard loads successfully
- [ ] No agent nodes shown
- [ ] Message: "No agents to display"
- [ ] System ready for fresh test run
- [ ] No artifacts from previous test

**Verify**:
- [ ] Truly clean state
- [ ] No leftover data
- [ ] Ready for Phase 0 of next test run

---

## Success Criteria

Phase 12 is **PASSED** when:
- ✅ All 8 agents deleted via UI
- ✅ Agents list empty (shows 0 agents)
- ✅ Page refreshes show empty state persists
- ✅ Docker: No agent-test-* containers running
- ✅ API: GET /api/agents returns empty array
- ✅ Dashboard: Shows "No agents" or empty graph
- ✅ No orphaned containers or data
- ✅ Backend and frontend still responsive
- ✅ System ready for fresh test run

---

## Troubleshooting

**Agent deletion fails**:
- Try API deletion: `curl -X DELETE http://localhost:8000/api/agents/test-echo`
- Check backend logs: `docker logs backend`
- May require manual Docker cleanup

**Agent still appears after deletion**:
- Refresh page (F5, Ctrl+Shift+R for hard refresh)
- Check browser cache
- API may have cached response

**Docker container won't delete**:
- Stop manually: `docker stop agent-test-echo`
- Remove manually: `docker rm agent-test-echo`
- Check for container locks

**Database shows agents after deletion**:
- Backend database inconsistency
- Check backend logs: `docker logs backend`
- May require database repair

---

## Test Run Complete

🎉 **All 12 phases completed!**

### Test Summary
- **Total Duration**: ~2 hours 45 minutes
- **Phases Executed**: 0-12 (13 total)
- **Agents Tested**: 8
- **GitHub Templates**: Validated all use correct templates
- **Critical Bugs Found**: Context stuck at 0% (if applicable), Task indicator at "—" (if applicable)

### Results Template

For the complete test run report:

```markdown
# Complete Test Run Results

**Date**: 2025-12-10
**Duration**: 2:45
**Environment**: Local (localhost:3000 + localhost:8000)
**Tester**: [Name or Agent ID]

## Phase Summary
- Phase 0: ✅ PASSED (Setup)
- Phase 1: ✅ PASSED (Authentication)
- Phase 2: ✅ PASSED (Agent Creation - GitHub templates)
- Phase 3: [✅ PASSED / ⚠️ BUG FOUND] (Context validation)
- Phase 4: ✅ PASSED (State Persistence)
- Phase 5: ✅ PASSED (Agent Collaboration)
- Phase 7: ✅ PASSED (Scheduling)
- Phase 8: ✅ PASSED (Execution Queue)
- Phase 9: ✅ PASSED (File Browser)
- Phase 10: ✅ PASSED (Error Handling)
- Phase 11: ✅ PASSED (Multi-Agent Dashboard)
- Phase 12: ✅ PASSED (Cleanup)
- Phase 13: ✅ PASSED (System Settings)
- Phase 14: ✅ PASSED (OpenTelemetry)
- Phase 15: ✅ PASSED (System Agent)
- Phase 16: ✅ PASSED (Web Terminal)
- Phase 17: ✅ PASSED (Email Auth - if enabled)
- Phase 18: ✅ PASSED (GitHub Initialization)

## Critical Findings
[List any bugs or issues found]

## Recommendations
[List any fixes or improvements needed]
```

---

## Next Steps

After cleanup is complete:

1. **If All Phases PASSED**:
   - System is production-ready
   - No critical issues found
   - Document in git/tracking system

2. **If Bugs Found**:
   - Create tickets for issues
   - Prioritize by severity
   - Re-run Phase 3 after context fix
   - Continue monitoring system

3. **For Next Test Run**:
   - Clean state verified
   - Can run from Phase 0 again
   - Takes ~2h 45min for full suite

---

**Status**: 🟢 Complete test run successful
**Last Updated**: 2025-12-09
