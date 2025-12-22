# Phase 15: System Agent & Fleet Operations

> **Purpose**: Validate the Internal System Agent (trinity-system), fleet ops API, and admin UI
> **Duration**: ~20 minutes
> **Assumes**: Phase 1 PASSED (admin authentication), Phase 14 PASSED or skipped (OTel optional)
> **Output**: System Agent operational, fleet ops verified, admin UI working

---

## Prerequisites

- Phase 1 PASSED (logged in as admin)
- Backend healthy at http://localhost:8000
- Frontend accessible at http://localhost:3000
- Docker daemon running
- At least 1-2 other agents running (for fleet operations testing)

---

## Test Steps

### Step 1: Verify System Agent Auto-Deployment

**Action**: Check backend logs for system agent deployment on startup

```bash
docker logs trinity-backend 2>&1 | grep -i "system agent"
```

**Expected**:
- [ ] Log shows: "System agent: created - System agent created and started"
- [ ] OR: "System agent: already_running - System agent is already running"

**Verify container exists**:
```bash
docker ps | grep trinity-system
```

**Expected**:
- [ ] Container `agent-trinity-system` is running
- [ ] Status shows "Up" with running state

---

### Step 2: Verify System Agent Status API

**Action**: Call the system agent status endpoint

```bash
curl http://localhost:8000/api/system-agent/status \
  -H "Authorization: Bearer $TOKEN" | jq
```

**Expected Response**:
```json
{
  "exists": true,
  "running": true,
  "status": "running",
  "container_id": "abc123...",
  "health": "healthy"
}
```

**Verify**:
- [ ] `exists: true`
- [ ] `running: true`
- [ ] `status: "running"`
- [ ] `container_id` is present

---

### Step 3: Verify System Agent Cannot Be Deleted

**Action**: Attempt to delete the system agent via API

```bash
curl -X DELETE http://localhost:8000/api/agents/trinity-system \
  -H "Authorization: Bearer $TOKEN" -v
```

**Expected**:
- [ ] HTTP 403 Forbidden
- [ ] Response contains message about system agents being protected
- [ ] Container still running after request

**Verify protection**:
```bash
docker ps | grep trinity-system  # Should still exist
```

---

### Step 4: Verify System Link in NavBar (Admin Only)

**Action**:
- Login as admin user
- Look for "System" link in the navigation bar

**Expected**:
- [ ] "System" link visible in navbar (with CPU icon)
- [ ] Link is styled with purple/system theme
- [ ] Clicking navigates to `/system-agent`

**Non-Admin Test** (if possible):
- Login as non-admin user
- [ ] "System" link should NOT be visible

---

### Step 5: Verify System Agent UI Page

**Action**:
- Navigate to http://localhost:3000/system-agent
- OR click "System" link in navbar

**Expected UI Elements**:
- [ ] Header shows "System Agent" with SYSTEM badge
- [ ] Status indicator (running/stopped)
- [ ] Start/Restart button
- [ ] Fleet Overview cards (Total, Running, Stopped, Issues)
- [ ] Quick Actions section (Emergency Stop, Restart All, etc.)
- [ ] Operations Console (chat interface)
- [ ] Quick command buttons (/ops/status, /ops/health, etc.)

---

### Step 6: Verify Fleet Status API

**Action**: Call the fleet status endpoint

```bash
curl http://localhost:8000/api/ops/fleet/status \
  -H "Authorization: Bearer $TOKEN" | jq
```

**Expected**:
```json
{
  "agents": [
    {
      "name": "trinity-system",
      "status": "running",
      "is_system": true,
      "context_percent": 0,
      "activity_state": "idle"
    },
    {
      "name": "other-agent",
      "status": "running",
      "is_system": false
    }
  ],
  "summary": {
    "total": 3,
    "running": 2,
    "stopped": 1,
    "issues": 0
  }
}
```

**Verify**:
- [ ] Response includes all agents
- [ ] `trinity-system` marked with `is_system: true`
- [ ] Summary counts are accurate
- [ ] UI fleet cards match API counts

---

### Step 7: Verify Fleet Health API

**Action**: Call the fleet health endpoint

```bash
curl http://localhost:8000/api/ops/fleet/health \
  -H "Authorization: Bearer $TOKEN" | jq
```

**Expected**:
```json
{
  "status": "healthy",
  "critical_count": 0,
  "warning_count": 0,
  "healthy_count": 2,
  "issues": [],
  "recommendations": []
}
```

**Verify**:
- [ ] Status reflects fleet health
- [ ] Counts match actual agent states
- [ ] Issues list is accurate (empty if no problems)

---

### Step 8: Test Operations Console - /ops/status

**Action**:
- In System Agent UI, click the `/ops/status` quick command button
- OR type `/ops/status` in the chat input and send

**Expected**:
- [ ] Message appears in console as "user" message
- [ ] System agent processes the command
- [ ] Response shows fleet status report with:
  - Agent count (total, running, stopped)
  - Context usage per agent
  - Activity states

**Note**: First command may take 10-20 seconds as agent warms up.

---

### Step 9: Test Operations Console - /ops/health

**Action**:
- Click the `/ops/health` quick command button
- OR type `/ops/health` in chat

**Expected**:
- [ ] System agent returns health report
- [ ] Report includes:
  - Healthy/warning/critical agent counts
  - Issues found (if any)
  - Recommendations

---

### Step 10: Test Operations Console - /ops/costs (OTel Integration)

**Action**:
- Click the `/ops/costs` quick command button
- OR type `/ops/costs` in chat

**Expected** (when OTel enabled):
- [ ] System agent returns cost report with:
  - Total cost breakdown
  - Token usage summary
  - Daily limit status (if configured)
  - Productivity metrics

**Expected** (when OTel disabled):
- [ ] System agent reports OTel is not enabled
- [ ] Suggests enabling with `OTEL_ENABLED=1`

---

### Step 11: Verify Quick Actions - Refresh Status

**Action**:
- Click "Refresh Status" button in Quick Actions

**Expected**:
- [ ] Fleet cards update with current counts
- [ ] Button shows loading state briefly
- [ ] Success notification appears

---

### Step 12: Test Pause/Resume Schedules

**Action**:
- Click "Pause Schedules" button
- Confirm action if prompted

**Expected**:
- [ ] Success message: "X schedules paused"
- [ ] API call to `/api/ops/schedules/pause` succeeds

**Verification**:
```bash
# Check schedules are paused (enabled=0)
curl http://localhost:8000/api/agents/ANY_AGENT/schedules \
  -H "Authorization: Bearer $TOKEN"
```

**Resume**:
- Click "Resume Schedules" button
- [ ] Success message: "X schedules resumed"
- [ ] Schedules re-enabled

---

### Step 13: Test Restart All Agents

**Action**:
- Click "Restart All" button
- Confirm action

**Expected**:
- [ ] Confirmation dialog appears
- [ ] After confirm, agents restart sequentially
- [ ] Success message shows count restarted
- [ ] System agent (trinity-system) is NOT restarted
- [ ] Fleet cards update to show running agents

**Verify**:
```bash
docker ps --filter "label=trinity.platform=agent" --format "{{.Names}} {{.Status}}"
```

---

### Step 14: Test Emergency Stop

**Action**:
- Click "Emergency Stop" button
- Confirm action (should have strong warning)

**Expected**:
- [ ] Confirmation dialog with warning message
- [ ] After confirm:
  - All non-system agents stopped
  - All schedules paused
  - Success message with counts
- [ ] System agent remains running
- [ ] Fleet cards show all stopped (except system)

**Verify**:
```bash
docker ps --filter "label=trinity.platform=agent" --format "{{.Names}} {{.Status}}"
# Only agent-trinity-system should be running
```

**IMPORTANT**: Re-start agents after this test if needed for other testing.

---

### Step 15: Test System Agent Restart

**Action**:
- Click "Restart" button on System Agent (in header)

**Expected**:
- [ ] Button shows loading state
- [ ] System agent stops and restarts
- [ ] Status returns to "running"
- [ ] Trinity injection occurs on restart
- [ ] No errors in console

---

### Step 16: Verify System Agent Reinitialize (Admin Only)

**Action**: Call reinitialize API (admin-only, resets to clean state)

```bash
curl -X POST http://localhost:8000/api/system-agent/reinitialize \
  -H "Authorization: Bearer $TOKEN" | jq
```

**Expected Response**:
```json
{
  "success": true,
  "steps_completed": [
    "STOPPED",
    "WORKSPACE_CLEARED",
    "STARTED",
    "TEMPLATE_COPIED",
    "TRINITY_INJECTED"
  ],
  "message": "System agent reinitialized successfully"
}
```

**Verify**:
- [ ] All 5 steps completed
- [ ] System agent is running after reinitialize
- [ ] Slash commands available (`/ops/status` works)

---

### Step 17: Verify System MCP Scope (Permission Bypass)

**Action**: Verify system agent can call any agent without permissions

Via chat in System Agent UI:
```
Use the Trinity MCP to list all agents
```

**Expected**:
- [ ] System agent can list ALL agents (not filtered)
- [ ] Can communicate with any agent (if others exist)
- [ ] No permission denied errors

**Verify MCP key scope**:
```bash
# Check the system agent's MCP key scope in database
sqlite3 ~/trinity-data/trinity.db \
  "SELECT scope, agent_name FROM mcp_api_keys WHERE agent_name='trinity-system'"
# Should show scope="system"
```

---

### Step 18: Verify SYSTEM Badge in Dashboard

**Action**:
- Navigate to main Dashboard (http://localhost:3000)
- Look at the agent network graph

**Expected**:
- [ ] `trinity-system` node has distinct purple styling
- [ ] Shows "System Dashboard" link (not "View Details" button)
- [ ] Clicking link goes to `/system-agent` page
- [ ] System agent NOT shown in Agents page list (only in Dashboard)

---

## Critical Validations

### System Agent Must Auto-Deploy on Backend Restart

**Validation**: Restart backend and verify system agent deploys

```bash
docker-compose restart backend
sleep 10
docker logs trinity-backend 2>&1 | grep -i "system agent"
docker ps | grep trinity-system
```

**Expected**:
- [ ] System agent deploys (created or already_running)
- [ ] No errors in backend logs

---

### Deletion Protection Must Be Enforced

**Validation**: API, UI, and MCP all block deletion

```bash
# API
curl -X DELETE http://localhost:8000/api/agents/trinity-system \
  -H "Authorization: Bearer $TOKEN"
# Expected: 403

# MCP (if testing via Claude Code)
# mcp__trinity__delete_agent(name="trinity-system")
# Expected: Error about system agents
```

---

### Ops Settings Defaults

**Validation**: Ops settings have correct defaults

```bash
curl http://localhost:8000/api/settings/ops/config \
  -H "Authorization: Bearer $TOKEN" | jq
```

**Expected defaults**:
```json
{
  "ops_context_warning_threshold": "75",
  "ops_context_critical_threshold": "90",
  "ops_idle_timeout_minutes": "30",
  "ops_cost_limit_daily_usd": "50.0",
  "ops_max_execution_minutes": "10"
}
```

---

## Success Criteria

Phase 15 is **PASSED** when:
- System agent auto-deploys on backend startup
- System agent status API returns correct state
- System agent cannot be deleted via API
- NavBar shows "System" link for admin users only
- System Agent UI page loads with all components
- Fleet status/health APIs return accurate data
- Operations Console executes /ops/* commands
- Quick Actions (Refresh, Pause/Resume, Restart All, Emergency Stop) work
- System agent restart works
- System agent reinitialize completes all 5 steps
- System MCP scope bypasses permissions
- SYSTEM badge visible in Dashboard

---

## Troubleshooting

**System agent not deploying**:
- Check admin user exists in database
- Check template at `config/agent-templates/trinity-system/`
- Check backend logs for errors

**System agent can't run ops commands**:
- Reinitialize to restore slash commands
- Check `/trinity-meta-prompt` mount exists
- Verify `$TRINITY_MCP_API_KEY` env var is set

**Fleet status returns empty**:
- Check docker socket is accessible
- Verify agent containers have correct labels

**Emergency stop doesn't work**:
- Check user is admin
- Check backend can access Docker
- Look at ops.py logs

**System link not visible**:
- Verify user role is "admin"
- Check NavBar.vue `isAdmin` conditional
- Verify `/api/users/me` returns admin role

---

## API Reference

### System Agent

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/system-agent/status` | Get system agent status |
| POST | `/api/system-agent/restart` | Restart system agent (admin) |
| POST | `/api/system-agent/reinitialize` | Reset to clean state (admin) |

### Fleet Operations

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/ops/fleet/status` | All agents with status/context |
| GET | `/api/ops/fleet/health` | Health summary with issues |
| POST | `/api/ops/fleet/restart` | Restart all/filtered agents |
| POST | `/api/ops/fleet/stop` | Stop all/filtered agents |
| POST | `/api/ops/schedules/pause` | Pause all schedules |
| POST | `/api/ops/schedules/resume` | Resume all schedules |
| POST | `/api/ops/emergency-stop` | Halt all executions |
| GET | `/api/ops/costs` | Cost report from OTel |

---

## Cleanup

After testing:
- [ ] Resume paused schedules: Click "Resume Schedules"
- [ ] Restart any stopped agents if needed
- [ ] System agent should remain running (do not delete)

---

## Related Documentation

- Feature Flow: `docs/memory/feature-flows/internal-system-agent.md`
- Feature Flow: `docs/memory/feature-flows/system-agent-ui.md`
- Requirements: `requirements.md` sections 11.1 and 11.2
- Template: `config/agent-templates/trinity-system/`
- System Agent Service: `src/backend/services/system_agent_service.py`
- Ops Router: `src/backend/routers/ops.py`
- System Agent Router: `src/backend/routers/system_agent.py`

---

## Next Phase

Phase 15 completion marks the end of the extended feature test suite.
Return to **Phase 12 (Cleanup)** if needed to clean up test agents.

---

**Status**: Ready for testing
**Last Updated**: 2025-12-21
