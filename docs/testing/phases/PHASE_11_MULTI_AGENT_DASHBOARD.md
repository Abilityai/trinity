# Phase 11: Multi-Agent Dashboard (All 8 Agents)

> **Purpose**: Validate dashboard, agent coordination, and system-wide monitoring
> **Duration**: ~10 minutes
> **Assumes**: Phase 10 PASSED (all 8 agents created and tested individually)
> **Output**: Multi-agent dashboard and coordination verified

---

## Background

**System-Wide View**:
- Dashboard shows all 8 agents in coordinated view
- Agent relationships and communication visible
- System health and metrics
- Context usage across all agents

---

## Test: Dashboard Overview

### Step 1: Navigate to Dashboard
**Action**:
- Go to http://localhost:3000/ (home/dashboard)
- Wait 3 seconds for page load

**Expected**:
- [ ] Dashboard loads
- [ ] Agent network graph visible
- [ ] 8 agent nodes displayed
- [ ] No loading errors
- [ ] Layout stable (not jumping around)

**Verify**:
- [ ] All 8 agents visible:
  - test-echo
  - test-counter
  - test-delegator
  - test-worker
  - test-scheduler
  - test-queue
  - test-files
  - test-error

---

### Step 2: Verify Agent Status in Dashboard
**Action**:
- Look at each agent node in the graph
- Check status indicator for each

**Expected**:
- [ ] test-echo: Running (green)
- [ ] test-counter: Running (green)
- [ ] test-delegator: Running (green)
- [ ] test-worker: Running (green)
- [ ] test-scheduler: Running (green)
- [ ] test-queue: Running (green)
- [ ] test-files: Running (green)
- [ ] test-error: Running (green)

**Verify**:
- [ ] All agents show green (running)
- [ ] Status icons consistent
- [ ] No agents showing red (stopped) or orange (error)

---

### Step 3: Check Agent Details on Hover
**Action**:
- Hover mouse over test-echo node
- Wait 2 seconds for tooltip

**Expected Tooltip**:
```
test-echo
Status: Running
Context: [X]%
Created: [date]
Template: github:abilityai/test-agent-echo
```

**Verify**:
- [ ] Agent name shown
- [ ] Status shown
- [ ] Context % shown
- [ ] Creation date shown
- [ ] Template shown as github:abilityai/test-agent-echo

---

## Test: Agent Communication Edges

### Step 4: Verify Communication Edges
**Action**:
- Look at lines (edges) connecting agent nodes
- Trace connections from test-delegator to others

**Expected**:
- [ ] Edge from test-delegator → test-echo (from Phase 5)
- [ ] Edge from test-delegator → test-counter (from Phase 5)
- [ ] Label on edge showing message count (e.g., "2x")
- [ ] Visual flow animation if active

**Verify**:
- [ ] Delegation relationships shown
- [ ] Message counts accurate
- [ ] Line thickness may vary by traffic
- [ ] Colors indicate direction

---

### Step 5: Check Agent Context Distribution
**Action**:
- Look at context % displayed for each agent
- Compare values across all 8 agents

**Expected Context Distribution** (approximate):
```
test-echo: 5-15% (from Phase 3 + Phase 5 delegation)
test-counter: 8-18% (from Phase 4 + Phase 5 delegation)
test-delegator: 15-25% (from Phase 5 delegation operations)
test-worker: 10-20% (from Phase 5 delegation target)
test-scheduler: 12-22% (from Phase 7 schedule execution)
test-queue: 15-25% (from Phase 8 queue processing)
test-files: 10-20% (from Phase 9 file operations)
test-error: 10-20% (from Phase 10 error handling)
```

**Verify**:
- [ ] Each agent has context % shown
- [ ] No context % is 0% (all have been used)
- [ ] test-delegator has highest (delegated to others)
- [ ] Reasonable distribution across agents
- [ ] No agent exceeds 100% (>200K tokens)

---

## Test: System Metrics

### Step 6: Check System Metrics Panel
**Action**:
- Look for metrics panel or sidebar
- Check overall system statistics

**Expected Metrics**:
- [ ] Total agents: 8
- [ ] Running agents: 8
- [ ] Total context used: [sum of all %]
- [ ] Average context: [mean]
- [ ] System uptime: [time since start]

**Verify**:
- [ ] Counts accurate (8 agents total)
- [ ] All running (no stopped agents)
- [ ] Context sum makes sense
- [ ] Uptime reasonable

---

## Test: Agent Coordination Scenario

### Step 7: Trigger Multi-Agent Workflow
**Action**:
- Go to dashboard
- Identify test-delegator node
- Trigger a delegation workflow:
  - In test-delegator chat: "coordinate all agents: tell echo to repeat, tell counter to add, tell worker to plan"
  - Press Enter
  - **Wait 45 seconds** (coordination takes time)

**Expected Response**:
```
Coordinating multi-agent workflow...

Step 1: Delegating to test-echo
  Response: Echo: coordinate all agents...

Step 2: Delegating to test-counter
  Response: Counter: 16 (was 11)

Step 3: Delegating to test-worker
  Response: Task received and acknowledged

All agents coordinated successfully
```

**Verify**:
- [ ] Multiple delegation calls made
- [ ] All agents respond
- [ ] No agents timeout or error
- [ ] Results show in order

---

### Step 8: Watch Dashboard Update
**Action**:
- Keep dashboard visible while coordination happens
- Watch for edge activations and context changes

**Expected**:
- [ ] Edges light up as messages flow
- [ ] Context % for test-delegator increases
- [ ] Context % for target agents increases
- [ ] Animation shows message flow
- [ ] Updates happen in near-real-time

**Verify**:
- [ ] Dashboard updates during activity
- [ ] Refresh rate reasonable (1-2 second updates)
- [ ] No stale data shown

---

## Test: Agent Health Overview

### Step 9: Check Agent Health Indicators
**Action**:
- Look at all agent nodes
- Check for any error indicators or warnings

**Expected**:
- [ ] No red nodes (all running)
- [ ] No warning icons
- [ ] All status indicators green
- [ ] No "unresponsive" labels

**Verify**:
- [ ] System health good across all agents
- [ ] No failures or crashes
- [ ] No communication breakdowns

---

## Test: Historical Data and Trends

### Step 10: Check Historical Metrics (if available)
**Action**:
- Look for historical graph or trend view
- Check context % growth over time

**Expected** (if feature available):
- [ ] Graph shows context growth
- [ ] X-axis: time progression
- [ ] Y-axis: context % per agent
- [ ] Trend upward (context increases with usage)

**Verify** (or skip if not implemented):
- [ ] Historical data collected
- [ ] Trends visible
- [ ] No anomalies

---

## Test: Dynamic Updates

### Step 11: Refresh Dashboard While Agent Active
**Action**:
- Go to test-echo agent detail page
- Type: "do something that takes 30 seconds"
- Press Enter
- Immediately go back to dashboard
- Watch for updates

**Expected**:
- [ ] Dashboard shows test-echo actively running
- [ ] Context % for test-echo increases live
- [ ] Progress visible
- [ ] Updates without manual refresh

**Verify**:
- [ ] Real-time updates working
- [ ] WebSocket or polling active
- [ ] No need to refresh manually

---

## Critical Validations

### GitHub Templates Verified
**Validation**: All 8 agents use correct templates

```bash
# Quick validation script
for agent in test-echo test-counter test-delegator test-worker test-scheduler test-queue test-files test-error; do
  template=$(docker inspect agent-$agent --format='{{index .Config.Labels "trinity.template"}}')
  echo "$agent: $template"
  # All should show: github:abilityai/test-agent-*
done
```

### Context % Tracking
**Validation**: Context increases across all agents

- [ ] No agent shows 0% (unless unused)
- [ ] Delegator has highest context
- [ ] Distribution reasonable
- [ ] No anomalies (e.g., sudden drops)

### System Stability
**Validation**: All agents remain responsive

```bash
# Check all agents running
docker ps | grep agent-test | wc -l
# Should show: 8
```

---

## Success Criteria

Phase 11 is **PASSED** when:
- ✅ Dashboard loads with all 8 agents visible
- ✅ All 8 agents show "Running" status (green)
- ✅ Each agent displays context % (non-zero)
- ✅ Communication edges visible between delegator and others
- ✅ Edge labels show message counts
- ✅ System metrics panel shows 8/8 agents running
- ✅ Multi-agent coordination workflow completes successfully
- ✅ Dashboard updates in real-time during activity
- ✅ All agents remain responsive
- ✅ All agents use GitHub templates (github:abilityai/test-agent-*)
- ✅ No agent shows errors or warnings
- ✅ Context distribution reasonable across all agents

---

## Troubleshooting

**Dashboard doesn't load**:
- Clear browser cache (Ctrl+Shift+Delete)
- Check backend running: `docker ps | grep backend`
- Check frontend running: `docker ps | grep frontend`
- Browser console errors: F12 → Console tab

**Missing agent nodes**:
- Agent may not be running: `docker ps | grep test-XXX`
- Restart missing agent
- Refresh dashboard (F5)

**Context % all zero**:
- This is Phase 3 critical bug
- If stuck across all agents, likely system issue
- Check Phase 3 CONTEXT_VALIDATION.md

**Edges not showing**:
- Dashboard visualization may be broken
- Check browser console for errors
- Try different browser
- Verify agents are communicating: check logs

**Real-time updates not working**:
- WebSocket connection may be broken
- Check backend logs: `docker logs backend`
- Try manual refresh (F5)

---

## Next Phase

Once Phase 11 is **PASSED**, proceed to:
- **Phase 12**: Cleanup (delete all agents)

---

**Status**: 🟢 Multi-agent dashboard & coordination validated
**Last Updated**: 2025-12-09
