# Phase 16: Web Terminal Testing

> **Purpose**: Validate the browser-based Web Terminal for all agents (Req 11.5)
> **Duration**: ~15 minutes
> **Assumes**: Phase 1 PASSED (authenticated), Phase 2 PASSED (agents running)
> **Output**: Web Terminal functional for all agent types

---

## Prerequisites

- ✅ Phase 1 PASSED (logged in as admin)
- ✅ At least 2 agents running (any type)
- ✅ Backend healthy at http://localhost:8000
- ✅ Frontend accessible at http://localhost:3000

---

## Background

The Web Terminal (Req 11.5) provides a browser-based CLI interface to agent containers:

**Features**:
- Full shell access to agent containers
- Runs Claude Code commands
- WebSocket-based real-time communication
- History preserved during session
- Per-agent API key toggle (Req 11.7)

**Replaces**: Legacy Chat tab - all agent interaction now via Terminal

---

## Test Steps

### Step 1: Navigate to Agent Detail

**Action**:
- Navigate to http://localhost:3000/agents
- Click on any running agent
- Wait for detail page to load

**Expected**:
- [ ] Agent detail page loads
- [ ] Terminal tab is visible and active (default)
- [ ] No Chat tab present (deprecated)
- [ ] Terminal section shows loading or ready state

---

### Step 2: Verify Terminal Loads

**Action**:
- Wait for terminal to initialize
- Observe terminal prompt

**Expected**:
- [ ] Terminal container displays
- [ ] Prompt appears (e.g., `developer@agent:~$`)
- [ ] Cursor is active
- [ ] No connection errors in console

**If Terminal Fails to Load**:
- Check WebSocket connection
- Verify agent container is running: `docker ps | grep agent-NAME`
- Check backend logs for terminal errors

---

### Step 3: Test Basic Shell Commands

**Action**:
- In terminal, type: `ls -la`
- Press Enter
- Wait for output

**Expected**:
- [ ] Command executes
- [ ] File listing appears
- [ ] Shows agent workspace files
- [ ] No errors

**Additional Commands**:
```bash
pwd              # Should show /home/developer
whoami           # Should show developer
cat CLAUDE.md    # Should show agent instructions
```

---

### Step 4: Test Terminal Input/Output

**Action**:
- Type: `echo "Hello from Terminal"`
- Press Enter

**Expected**:
- [ ] Command executes
- [ ] Output displays: "Hello from Terminal"
- [ ] Command history updated

---

### Step 5: Test Keyboard Navigation

**Action**:
- Press Up Arrow (to recall previous command)
- Press Down Arrow
- Press Tab (for autocomplete if supported)
- Press Ctrl+C (to interrupt)

**Expected**:
- [ ] Up arrow recalls previous command
- [ ] Down arrow moves forward in history
- [ ] Tab completion works (may vary)
- [ ] Ctrl+C sends interrupt signal

---

### Step 6: Test Claude Code Commands

**Action**:
- Type: `claude --version` (or equivalent)
- OR start Claude Code session

**Expected**:
- [ ] Claude Code is available in container
- [ ] Can start interactive session
- [ ] Can run AI-assisted commands

**Note**: Claude Code requires ANTHROPIC_API_KEY in container (see API Key toggle test)

---

### Step 7: Test Long-Running Commands

**Action**:
- Run: `sleep 5 && echo "Done"`
- Wait for output

**Expected**:
- [ ] Command runs for 5 seconds
- [ ] "Done" appears after delay
- [ ] Terminal remains responsive during execution

---

### Step 8: Test Multi-Agent Terminal Sessions

**Action**:
- Open second agent in new browser tab
- Open terminal for second agent
- Run commands in both terminals

**Expected**:
- [ ] Each terminal connects to correct agent
- [ ] Commands isolated to each agent
- [ ] No cross-contamination of sessions

---

### Step 9: Verify Terminal API Endpoint

**Action**: Test terminal WebSocket endpoint via API

```bash
# Check terminal endpoint exists
curl http://localhost:8000/api/agents/AGENT_NAME/terminal \
  -H "Authorization: Bearer $TOKEN" \
  -H "Upgrade: websocket"
```

**Expected**:
- [ ] Endpoint returns 101 Switching Protocols (WebSocket)
- [ ] OR proper upgrade response

---

### Step 10: Test Terminal Reconnection

**Action**:
- With terminal open, refresh page (F5)
- Wait for terminal to reconnect

**Expected**:
- [ ] Terminal reconnects after refresh
- [ ] Can run new commands
- [ ] Previous history may be lost (expected)

---

## Critical Validations

### WebSocket Connection Health

**Validation**: Terminal maintains connection

```bash
# Check backend logs for WebSocket activity
docker logs trinity-backend 2>&1 | grep -i "websocket\|terminal"
```

**Expected**:
- [ ] WebSocket connections logged
- [ ] No disconnect errors
- [ ] Heartbeat messages (if implemented)

---

### Container Exec Permission

**Validation**: Backend can exec into containers

```bash
# Direct container test
docker exec -it agent-AGENT_NAME bash -c "echo test"
```

**Expected**:
- [ ] Command executes successfully
- [ ] Output: "test"

---

## Success Criteria

Phase 16 is **PASSED** when:
- ✅ Terminal loads for any agent
- ✅ Basic shell commands work (ls, pwd, cat)
- ✅ Input/output displays correctly
- ✅ Keyboard navigation functional
- ✅ Claude Code commands available (with API key)
- ✅ Long-running commands work
- ✅ Multi-agent terminals are isolated
- ✅ Terminal reconnects after page refresh

---

## Troubleshooting

**Terminal doesn't load**:
- Check WebSocket support in browser
- Verify agent container is running
- Check backend terminal endpoint logs

**Commands don't execute**:
- Verify container exec permissions
- Check Docker socket access
- Ensure agent user has shell access

**Disconnects frequently**:
- Check network stability
- Look for proxy/timeout issues
- Verify WebSocket keepalive

**No Claude Code**:
- Verify Claude Code installed in base image
- Check ANTHROPIC_API_KEY env var
- See Per-Agent API Key toggle

---

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| WebSocket | `/api/agents/{name}/terminal` | Terminal WebSocket connection |
| GET | `/api/agents/{name}/api-key-setting` | Check API key toggle |
| PUT | `/api/agents/{name}/api-key-setting` | Set API key toggle |

---

## Related Documentation

- Feature Flow: `docs/memory/feature-flows/agent-terminal.md`
- Requirements: `requirements.md` section 11.5
- API Key Control: Section 11.7

---

## Next Phase

After Terminal testing, proceed to:
- **Phase 17**: Email Authentication (if enabled)
- **Phase 18**: GitHub Repository Initialization

---

**Status**: Ready for testing
**Last Updated**: 2025-12-26
