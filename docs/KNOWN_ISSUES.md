# Known Issues & Troubleshooting

## Active Issues

### 🔴 OAuth Redirect May Go to localhost in Production

**Status**: Check your deployment
**Priority**: HIGH
**Affects**: Google OAuth flow when `BACKEND_URL` is misconfigured

**Symptoms:**
- User clicks OAuth button on production credentials page
- After authorizing, redirected to `localhost:8000` instead of production URL

**Solution:**
1. Ensure `BACKEND_URL` is set correctly in your production `.env`:
   ```bash
   BACKEND_URL=https://your-domain.com/api
   ```
2. Add production redirect URI to Google Cloud Console OAuth client
3. Restart backend service
4. Clear browser cache and try again

**Related Files:**
- `src/backend/config.py` - BACKEND_URL config
- `src/backend/routers/credentials.py` - OAuth init endpoint
- `.env.example` - BACKEND_URL documentation

---

### 🟡 Human Approval Timeout Not Enforced

**Status**: KNOWN LIMITATION
**Priority**: MEDIUM
**Affects**: Process Engine - human_approval steps

**Symptoms:**
- Setting `timeout: 5m` on a `human_approval` step does nothing
- Execution stays paused indefinitely until manual approve/reject

**Cause:**
- When execution pauses for approval, no background process monitors the timeout
- Requires a scheduler/cron job to periodically check for timed-out approvals

**Workaround:**
- Manually approve or reject pending approvals
- Don't rely on approval timeouts for critical workflows

**Fix Required:**
- Add background job to check for timed-out approvals
- Could integrate with existing `src/scheduler/` service

---

### 🟡 Agent-to-Agent MCP Calls Timeout at 60 Seconds

**Status**: KNOWN LIMITATION (Claude Code upstream)
**Priority**: HIGH
**Affects**: All agent-to-agent collaboration via `chat_with_agent` MCP tool

**Symptoms:**
- Agent A calls `mcp__trinity__chat_with_agent(agent_name="B", message="...", timeout_seconds=900)`
- After exactly 60 seconds, the call fails with a timeout error
- Agent B may still be processing, but the result is lost
- The `timeout_seconds` parameter has no effect on the 60s limit

**Cause:**
Claude Code has a hardcoded 60-second timeout for all MCP HTTP tool calls. This is an upstream limitation in Claude Code's MCP transport layer, not in Trinity. The `timeout_seconds` parameter controls the backend execution timeout, but Claude Code drops the HTTP connection before the backend timeout is reached.

**Workarounds:**
1. **Design tasks to complete within 60 seconds** — Break complex work into smaller sub-tasks
2. **Use async mode with polling** — Fire-and-forget pattern avoids the timeout:
   ```python
   # Start task (returns immediately)
   result = mcp__trinity__chat_with_agent(
       agent_name="worker",
       message="Do complex analysis",
       parallel=true,
       async=true
   )
   # Returns: { "execution_id": "abc123" }

   # Poll for results using get_execution_result (MCP-007)
   result = mcp__trinity__get_execution_result(
       agent_name="worker",
       execution_id="abc123"
   )
   # Returns: { "status": "running" | "success" | "failed", "response": "...", ... }
   # If still running, sleep 30s and poll again
   ```
3. **Use shared folders** — Write results to `/home/developer/shared-out/` instead of returning them synchronously
4. **Hybrid pattern** — Use async MCP to trigger work, shared folders for results

**Related:**
- GitHub Issue: [#104](https://github.com/abilityai/trinity/issues/104)
- Claude Code Issues: [#16837](https://github.com/anthropics/claude-code/issues/16837), [#424](https://github.com/anthropics/claude-code/issues/424)
- Docs: [Multi-Agent System Guide](MULTI_AGENT_SYSTEM_GUIDE.md#design-limitation-60-second-mcp-call-timeout)

---

## Resolved Issues

_No resolved issues yet_

---

## Workarounds

### For OAuth Issues on Production
**Temporary workaround**: Use local development environment
1. Set up Trinity locally
2. Add OAuth credentials locally
3. Export credentials from Redis
4. Import to production Redis manually

---

## Investigation Tools

### Check Backend Logs
```bash
# Local
docker-compose logs backend --tail=100

# Production (replace with your deployment method)
# Check your container orchestration logs
```

### Check Environment Variables
```bash
# Local
docker-compose exec backend env | grep -E "(BACKEND|GOOGLE)"
```

### Test OAuth Init Endpoint
```bash
# Get auth_url from init endpoint
curl -X POST https://your-domain.com/api/oauth/google/init \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" | jq .

# Check the auth_url in the response - should contain correct redirect_uri
```

---

## How to Report New Issues

When adding a new issue to this document:

1. **Title**: Brief description with emoji (🔴 High, 🟡 Medium, 🟢 Low)
2. **Status**: UNRESOLVED / IN PROGRESS / RESOLVED
3. **Priority**: HIGH / MEDIUM / LOW
4. **Affects**: What feature/environment is broken
5. **Symptoms**: What the user sees
6. **What Was Tried**: Steps already taken to fix it
7. **Possible Causes**: List of theories
8. **Next Steps to Debug**: Concrete actions to investigate
9. **Related Files**: Code locations
10. **Commits**: Git commits related to this issue
11. **Impact**: Who/what is affected
