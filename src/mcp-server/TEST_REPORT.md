# Trinity MCP Server - Test Report

**Date:** 2025-11-26
**Tester:** Claude Code
**Server Version:** 1.0.0
**Test Method:** MCP Inspector CLI (`npx @modelcontextprotocol/inspector`)

> **HISTORICAL** — captured 2025-11-26. The "changeme" default password
> referenced in the credential and env-var tables below was removed by
> issue #692 (2026-05). The MCP server now throws on startup if
> `MCP_REQUIRE_API_KEY=false` and no `TRINITY_API_TOKEN`/`TRINITY_PASSWORD`
> is set. See `src/mcp-server/src/server.ts` for current behavior.

## Summary

All **10 MCP tools** have been tested and verified working correctly.

| Tool | Status | Response Time |
|------|--------|---------------|
| `list_agents` | ✅ Pass | < 1s |
| `get_agent` | ✅ Pass | < 1s |
| `create_agent` | ✅ Pass | ~30s (container startup) |
| `delete_agent` | ✅ Pass | ~5s |
| `start_agent` | ✅ Pass | ~5s |
| `stop_agent` | ✅ Pass | ~5s |
| `list_templates` | ✅ Pass | < 1s |
| `chat_with_agent` | ✅ Pass | ~10-15s (Claude API) |
| `get_chat_history` | ✅ Pass | < 1s |
| `get_agent_logs` | ✅ Pass | < 1s |

## Test Details

### 1. list_agents
```bash
npx @modelcontextprotocol/inspector http://localhost:8080/mcp --cli --method tools/call --tool-name list_agents
```
**Result:** Returns JSON array of all agents with name, type, status, ports, resources, container_id.

### 2. get_agent
```bash
npx @modelcontextprotocol/inspector http://localhost:8080/mcp --cli --method tools/call --tool-name get_agent --tool-arg name=test-ruby-agent
```
**Result:** Returns detailed agent info including UI port, SSH port, creation time.

### 3. create_agent
```bash
npx @modelcontextprotocol/inspector http://localhost:8080/mcp --cli --method tools/call --tool-name create_agent --tool-arg name=mcp-test-agent --tool-arg type=business-assistant
```
**Result:** Creates new agent container, returns status "created" with assigned ports.

### 4. delete_agent
```bash
npx @modelcontextprotocol/inspector http://localhost:8080/mcp --cli --method tools/call --tool-name delete_agent --tool-arg name=mcp-test-agent
```
**Result:** "Agent mcp-test-agent deleted"

### 5. stop_agent
```bash
npx @modelcontextprotocol/inspector http://localhost:8080/mcp --cli --method tools/call --tool-name stop_agent --tool-arg name=integration-test-agent
```
**Result:** "Agent integration-test-agent stopped"

### 6. start_agent
```bash
npx @modelcontextprotocol/inspector http://localhost:8080/mcp --cli --method tools/call --tool-name start_agent --tool-arg name=integration-test-agent
```
**Result:** "Agent integration-test-agent started"

### 7. list_templates
```bash
npx @modelcontextprotocol/inspector http://localhost:8080/mcp --cli --method tools/call --tool-name list_templates
```
**Result:** Returns array of templates including "ruby-social-media-agent".

### 8. chat_with_agent
```bash
npx @modelcontextprotocol/inspector http://localhost:8080/mcp --cli --method tools/call --tool-name chat_with_agent --tool-arg agent_name=test-ruby-agent --tool-arg 'message=Say hello in one word'
```
**Result:**
```json
{
  "response": "Hello!",
  "timestamp": "2025-11-26T13:29:40.967070"
}
```

### 9. get_chat_history
```bash
npx @modelcontextprotocol/inspector http://localhost:8080/mcp --cli --method tools/call --tool-name get_chat_history --tool-arg agent_name=test-ruby-agent
```
**Result:** Returns full conversation history array with role, content, timestamp.

### 10. get_agent_logs
```bash
npx @modelcontextprotocol/inspector http://localhost:8080/mcp --cli --method tools/call --tool-name get_agent_logs --tool-arg agent_name=test-ruby-agent --tool-arg lines=10
```
**Result:** Returns last 10 lines of container logs showing HTTP requests and Claude API responses.

## Bugs Fixed During Testing

### 1. Token Expiration (401 Unauthorized)

**Issue:** After 30 minutes, the JWT token expired causing all API calls to fail with 401.

**Fix:** Added automatic re-authentication in `client.ts`:
- Store username/password during initial authentication
- On 401 response, attempt re-authentication
- Retry the original request with new token

**Code Location:** `src/client.ts:27-69, 102-110`

### 2. Chat History Response Format Mismatch

**Issue:** `get_chat_history` returned empty array because client expected `{ history: [...] }` but backend returns `[...]` directly.

**Fix:** Updated client to expect array directly:
```typescript
// Before (incorrect)
const response = await this.request<{ history: ChatMessage[] }>(...);
return response.history || [];

// After (correct)
return this.request<ChatMessage[]>(...);
```

**Code Location:** `src/client.ts:193-198`

## Server Configuration

- **Port:** 8080
- **Transport:** Streamable HTTP (httpStream)
- **Endpoint:** `http://localhost:8080/mcp`
- **Authentication:** Username/password to Trinity Backend API
- **Default Credentials:** admin / changeme (set via ADMIN_PASSWORD env var)

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_PORT` | 8080 | MCP server port |
| `MCP_TRANSPORT` | httpStream | Transport type |
| `TRINITY_API_URL` | http://localhost:8000 | Backend URL |
| `TRINITY_API_TOKEN` | - | Pre-configured token |
| `TRINITY_USERNAME` | admin | Auth username |
| `TRINITY_PASSWORD` | changeme | Auth password |

## Recommendations

1. **Session Persistence:** When MCP server restarts, clients need to reconnect. Consider adding session recovery or notifying clients of server restart.

2. **Token Refresh Timing:** Currently refreshes on 401. Could proactively refresh before expiration (token expires in 30 min).

3. **Error Handling:** Add more specific error messages for common failures (agent not found, container not running, etc.).

4. **Rate Limiting:** Consider adding rate limiting for chat_with_agent to prevent excessive Claude API costs.

## Conclusion

The Trinity MCP Server is fully functional and ready for production use. All 10 tools work correctly with the Trinity Backend API. The server properly handles authentication, token refresh, and provides a complete interface for agent orchestration via MCP.
