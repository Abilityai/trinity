# Phase 5: Agent-to-Agent Collaboration & Permissions (test-delegator)

> **Purpose**: Validate Trinity MCP agent-to-agent communication (Pillar II) and Agent Permissions System (Req 9.10)
> **Duration**: ~25 minutes
> **Assumes**: Phase 4 PASSED (test-echo and test-counter verified, test-delegator running)
> **Output**: Agent delegation, MCP communication, and permission enforcement confirmed

---

## Prerequisites

- ✅ Phase 4 PASSED
- ✅ test-echo agent running
- ✅ test-counter agent running (with state at counter = 11)
- ✅ test-delegator agent running
- ✅ All agents have Trinity MCP injected

---

## Background

**Pillar II: Hierarchical Delegation**
- test-delegator uses Trinity MCP to call other agents
- MCP tool: `mcp__trinity__chat_with_agent`
- Allows orchestration between agents
- Validates: access control, MCP auth, message propagation

---

## Test: List Available Agents

### Step 1: Navigate to test-delegator
**Action**:
- Go to http://localhost:3000/agents
- Click test-delegator
- Wait for detail page to load

**Expected**:
- [ ] Agent detail page loads
- [ ] Chat tab active
- [ ] Status: "Running" (green)
- [ ] Context: 0% (fresh agent)

---

### Step 2: List Agents Command
**Action**:
- Type: "list agents"
- Press Enter
- Wait 10 seconds

**Expected Response**:
```
Available agents:
- test-echo: running
- test-counter: running
- test-delegator: running
- test-worker: running
- test-scheduler: running
- test-queue: running
- test-files: running
- test-error: running
```

Or similar format showing all 8 agents with "running" status.

**Verify**:
- [ ] All 8 agents listed
- [ ] All show "running" status
- [ ] test-echo present
- [ ] test-counter present

---

## Test: Delegate to test-echo

### Step 3: Delegation Command
**Action**:
- Type: "delegate to test-echo: Hello from delegator"
- Press Enter
- **Wait 15-20 seconds** (delegation takes time: delegator → MCP → target → response)

**Expected Response**:
```
Delegating to test-echo: 'Hello from delegator'
Response from test-echo:
Echo: Hello from delegator
Words: 3
Characters: 23
```

Or similar showing echo response.

**Verify**:
- [ ] Delegation initiated
- [ ] Target agent response received
- [ ] Echo format correct (Echo: ... Words: ... Characters: ...)
- [ ] No error messages

---

### Step 4: Check Activity Panel
**Action**: Look at Activity Panel

**Expected - Tool Call**:
- [ ] `mcp__trinity__chat_with_agent` tool
- [ ] Duration: 15-20 seconds (longer than single-agent)
- [ ] Parameters shown:
  - [ ] target_agent: "test-echo"
  - [ ] message: "Hello from delegator"

**Verify**:
- [ ] MCP tool name correct
- [ ] Target agent identified
- [ ] Execution time reasonable for delegation

---

## Test: Delegate to test-counter

### Step 5: Delegate to Counter
**Action**:
- Type: "delegate to test-counter: add 5"
- Press Enter
- **Wait 15-20 seconds**

**Expected Response**:
```
Delegating to test-counter: 'add 5'
Response from test-counter:
Counter: 16 (previous: 11)
```

Counter should be 11 + 5 = 16 (state persisted from Phase 4)

**Verify**:
- [ ] Delegation executed
- [ ] Counter incremented correctly (11 + 5 = 16)
- [ ] Previous value (11) shown
- [ ] State persisted across agent boundary

---

### Step 6: Verify Counter State Changed
**Action**: Switch to test-counter agent
- Go back to Agents list
- Click test-counter
- Send "get" command

**Expected**:
```
Counter: 16 (previous: 16)
```

Not 11 anymore! Counter updated via delegation from test-delegator.

**Verify**:
- [ ] Counter value is 16 (not 11)
- [ ] Delegation modified target agent state
- [ ] State persisted

---

## Test: Dashboard Collaboration Edge

### Step 7: View Dashboard
**Action**:
- Navigate to http://localhost:3000/ (Dashboard)
- Wait 2 seconds for updates

**Expected**:
- [ ] All 8 agent nodes visible
- [ ] Edges (lines) visible between delegator and others
- [ ] Edge labels showing message counts (e.g., "2x" for test-delegator → test-echo)
- [ ] test-delegator → test-echo edge (from delegation 1)
- [ ] test-delegator → test-counter edge (from delegation 2)

**Verify**:
- [ ] Collaboration edges displayed
- [ ] Message counts accurate
- [ ] Colors correct (blue flowing animation if active)

---

## Test: Context Growth Across Agents

### Step 8: Verify Context Growth
**Action**: Check context for each agent

test-delegator:
- [ ] Context > 0% (executed 2 delegations)
- [ ] Progress bar filled

test-echo:
- [ ] Context > previous (Phase 3 + delegation)
- [ ] Increased by messages from delegation

test-counter:
- [ ] Context > previous (Phase 4 + delegation)
- [ ] Increased by delegation commands

**Verify**:
- [ ] Each agent tracks own context
- [ ] Delegation increments context on both sides
- [ ] No agent context exceeds 200K limit

---

## Test: Agent Permissions System (Req 9.10)

### Step 9: View Permissions Tab UI
**Action**:
- Navigate to test-delegator agent detail page
- Click "Permissions" tab (visible only for agent owners)

**Expected**:
- [ ] Permissions tab visible
- [ ] Badge shows count of permitted agents (e.g., "7")
- [ ] List of all other agents with checkboxes
- [ ] All same-owner agents pre-checked (default permissions)
- [ ] "Allow All" and "Allow None" buttons visible
- [ ] "Save Permissions" button visible

**Snapshot**: Take snapshot showing Permissions tab

---

### Step 10: Verify Default Permissions via API
**Action**: Check permissions via API

```bash
curl http://localhost:8000/api/agents/test-delegator/permissions \
  -H "Authorization: Bearer $TOKEN" | jq .
```

**Expected Response**:
```json
{
  "source_agent": "test-delegator",
  "permitted_agents": [
    {"name": "test-echo", "status": "running", "permitted": true},
    {"name": "test-counter", "status": "running", "permitted": true},
    {"name": "test-worker", "status": "running", "permitted": true},
    // ... all 7 other agents
  ],
  "available_agents": [...]
}
```

**Verify**:
- [ ] All 7 other agents listed as permitted
- [ ] `permitted: true` for each
- [ ] Bidirectional (test-echo also shows test-delegator as permitted)

---

### Step 11: Revoke Permission (UI)
**Action**: Remove test-echo from test-delegator's permissions
- Uncheck "test-echo" checkbox
- Click "Save Permissions"

**Expected**:
- [ ] Success message: "Permissions saved"
- [ ] Badge updates to show "6" permitted agents
- [ ] test-echo checkbox now unchecked

**Verify via API**:
```bash
curl http://localhost:8000/api/agents/test-delegator/permissions \
  -H "Authorization: Bearer $TOKEN" | jq '.permitted_agents | map(.name)'
```

**Expected**: `["test-counter", "test-worker", ...]` - test-echo NOT in list

---

### Step 12: Test Permission Enforcement via MCP
**Action**: Try delegation to non-permitted agent
- Chat with test-delegator: "delegate to test-echo: Hello again"
- Wait 15 seconds

**Expected Response**:
```
Permission denied: Agent 'test-delegator' is not permitted to communicate with 'test-echo'.
Configure permissions in the Trinity UI.
```

**Verify**:
- [ ] MCP returns permission denied error
- [ ] test-echo does NOT receive the message
- [ ] Access control enforced at MCP layer

**This validates Pillar II security** - agents cannot bypass permission controls.

---

### Step 13: Verify list_agents Filtering
**Action**: Chat with test-delegator: "list agents"

**Expected**: test-echo should NOT appear in the list (filtered out)

```
Available agents:
- test-counter: running
- test-worker: running
- test-scheduler: running
- test-queue: running
- test-files: running
- test-error: running
- test-delegator: running
```

**Verify**:
- [ ] Only 7 agents listed (not 8)
- [ ] test-echo MISSING from list
- [ ] Self (test-delegator) is included
- [ ] Other permitted agents visible

---

### Step 14: Restore Permission
**Action**: Re-enable test-echo permission
- Go to Permissions tab
- Check "test-echo" checkbox
- Click "Save Permissions"

**Expected**:
- [ ] Success message
- [ ] Badge shows "7" again
- [ ] test-echo checkbox checked

**Verify Delegation Works Again**:
- Chat: "delegate to test-echo: Permission restored"

**Expected**:
```
Response from test-echo:
Echo: Permission restored
Words: 2
Characters: 20
```

- [ ] Delegation succeeds
- [ ] Echo response received
- [ ] Permission system works bidirectionally

---

### Step 15: Test Bulk Actions
**Action**: Test "Allow None" bulk action
- Click "Allow None"
- Verify all checkboxes unchecked
- Click "Save Permissions"

**Expected**:
- [ ] All agents unchecked
- [ ] Badge shows "0"
- [ ] Saved successfully

**Action**: Test "Allow All" bulk action
- Click "Allow All"
- Verify all checkboxes checked
- Click "Save Permissions"

**Expected**:
- [ ] All agents checked
- [ ] Badge shows "7"
- [ ] Saved successfully

---

## Critical Validations

### Permissions System (Req 9.10)
**Validation**: Permission enforcement works end-to-end

| Test | Expected | Status |
|------|----------|--------|
| Default permissions granted on creation | All same-owner agents permitted | ✅/❌ |
| Permissions tab visible to owners | Tab displays with checkbox list | ✅/❌ |
| Revoke permission via UI | Checkbox saves, API reflects change | ✅/❌ |
| MCP blocks non-permitted delegation | "Permission denied" error | ✅/❌ |
| list_agents filters non-permitted | Only permitted agents visible | ✅/❌ |
| Restore permission works | Can delegate again | ✅/❌ |
| Bulk actions (Allow All/None) | All checkboxes toggle | ✅/❌ |

---

### MCP Tool Execution
**Validation**: Tool calls properly recorded

```bash
# Check agent logs for MCP call
docker logs agent-test-delegator | grep -i "mcp\|trinity\|chat_with_agent" | tail -10
```

**Expected**: Log entries showing MCP tool execution

### Access Control
**Validation**: Delegation between same-owner agents works

Since all agents owned by "admin":
- [ ] Delegation succeeds (when permitted)
- [ ] "Permission denied" when not permitted
- [ ] MCP auth validated

### Message Propagation
**Validation**: Messages reach target agent

test-counter saw:
- [ ] Message: "add 5"
- [ ] Executed operation
- [ ] Returned response

**Verify**: Full round-trip worked

---

## Success Criteria

Phase 5 is **PASSED** when:
- ✅ List agents command shows all 8 agents (when all permitted)
- ✅ Delegation to test-echo works (receives echo response)
- ✅ Delegation to test-counter works (counter incremented to 16)
- ✅ test-counter state persisted (verified by direct chat)
- ✅ MCP tool calls logged in activity
- ✅ Execution time 15-20 seconds (normal for delegation)
- ✅ Dashboard shows collaboration edges
- ✅ Context grows on all agents involved
- ✅ Access control works (allowed same-owner agents)
- ✅ **Permissions tab visible** (Req 9.10) - Checkbox list with all agents
- ✅ **Default permissions granted** - All same-owner agents pre-permitted
- ✅ **Revoke permission works** - Uncheck + save blocks delegation
- ✅ **Permission enforcement** - MCP returns "Permission denied" for non-permitted agents
- ✅ **list_agents filtering** - Non-permitted agents hidden from agent's view
- ✅ **Restore permission works** - Re-enable allows delegation again
- ✅ **Bulk actions work** - Allow All / Allow None toggle all checkboxes

---

## Troubleshooting

**Delegation times out (>30 seconds)**:
- Wait longer (may be slow)
- Check agent logs: `docker logs agent-test-delegator`
- Verify target agent running: `docker ps | grep test-echo`

**"Agent not found" error**:
- Verify agent exists: `curl http://localhost:8000/api/agents`
- Agent may not be running yet
- Check Docker: `docker ps | grep test-echo`

**MCP tool not found**:
- Verify Trinity MCP injected: `docker exec agent-test-delegator ls -la /home/developer/.mcp.json`
- Check MCP server running: `docker logs trinity-mcp-server`
- May need to reload agent credentials

**Access denied**:
- All agents owned by admin, should allow
- Check ownership: `curl http://localhost:8000/api/agents/test-echo`
- Verify sharing rules

**No activity logged**:
- Refresh page
- Check browser console for errors
- Activity may take 2-3 seconds to appear

---

## Next Phase

Once Phase 5 is **PASSED**, proceed to:
- **Phase 7**: Scheduling (test-scheduler)

---

**Status**: 🟢 Agent-to-agent collaboration validated (Pillar II)
