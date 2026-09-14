# Audit Trail Manual Test Plan (SEC-001 / Issue #20)

Covers Phases 2b, 3, and 4. Assumes services running via `./scripts/deploy/start.sh`.

---

## Prerequisites

```bash
# Get admin token (used in all curl commands below)
TOKEN=$(curl -s -X POST http://localhost:8000/api/token \
  -d "username=admin&password=${ADMIN_PASSWORD}" | jq -r '.access_token // empty')

# Verify token works
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/users/me | jq .
```

---

## Phase 2b — Backend Write Integrations

### 1. Request-ID Middleware

Every response should have `X-Request-ID` header:

```bash
curl -sI -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents | grep -i x-request-id
# Expected: X-Request-ID: <uuid>
```

Pass your own:

```bash
curl -sI -H "Authorization: Bearer $TOKEN" \
  -H "X-Request-ID: my-custom-id-123" \
  http://localhost:8000/api/agents | grep -i x-request-id
# Expected: X-Request-ID: my-custom-id-123
```

### 2. Authentication Audit

**Admin login success:**

```bash
curl -s -X POST http://localhost:8000/api/token \
  -d "username=admin&password=${ADMIN_PASSWORD}" | jq .

# Check audit log
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/audit-log?event_type=authentication&limit=5" | jq '.entries[] | {event_action, actor_ip, details}'
# Expected: entry with event_action="login_success", details.method="admin"
```

**Admin login failure:**

```bash
curl -s -X POST http://localhost:8000/api/token \
  -d "username=admin&password=wrong_password" 2>/dev/null

# Check audit log
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/audit-log?event_type=authentication&limit=5" | jq '.entries[] | select(.event_action=="login_failed")'
# Expected: entry with event_action="login_failed", details.method="admin"
```

**Email login (if email auth enabled):**

```bash
# Request code
curl -s -X POST http://localhost:8000/api/auth/email/request \
  -H "Content-Type: application/json" \
  -d '{"email":"test@example.com"}'

# Verify with wrong code
curl -s -X POST http://localhost:8000/api/auth/email/verify \
  -H "Content-Type: application/json" \
  -d '{"email":"test@example.com","code":"000000"}'

# Check for login_failed with method="email"
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/audit-log?event_type=authentication&limit=5" | jq '.entries[0]'
```

### 3. Sharing Audit

**Share an agent:**

```bash
AGENT_NAME="<your-agent>"

curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  "http://localhost:8000/api/agents/${AGENT_NAME}/share" \
  -d '{"email":"testuser@example.com"}' | jq .

# Check audit log
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/audit-log?event_type=authorization&limit=5" | jq '.entries[] | {event_action, target_id, details}'
# Expected: event_action="share", details.shared_with="testuser@example.com"
```

**Unshare:**

```bash
curl -s -X DELETE -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/agents/${AGENT_NAME}/share/testuser@example.com" | jq .

# Check audit log
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/audit-log?event_type=authorization&limit=5" | jq '.entries[] | select(.event_action=="unshare")'
# Expected: event_action="unshare", details.removed_email="testuser@example.com"
```

### 4. Credentials Audit

Requires a running agent:

```bash
AGENT_NAME="<running-agent>"

# Inject credentials
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  "http://localhost:8000/api/agents/${AGENT_NAME}/credentials/inject" \
  -d '{"files":{".env":"TEST_KEY=test_value"}}' | jq .

# Export credentials
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/agents/${AGENT_NAME}/credentials/export" | jq .

# Import credentials
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/agents/${AGENT_NAME}/credentials/import" | jq .

# Check audit log
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/audit-log?event_type=credentials&limit=10" | jq '.entries[] | {event_action, target_id, details}'
# Expected: inject, export, import entries with file lists (never values)
```

### 5. Settings Audit

```bash
# Update a setting
curl -s -X PUT -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  "http://localhost:8000/api/settings/test_audit_setting" \
  -d '{"value":"hello"}' | jq .

# Delete it
curl -s -X DELETE -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/settings/test_audit_setting" | jq .

# Check audit log
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/audit-log?event_type=configuration&limit=5" | jq '.entries[] | {event_action, details}'
# Expected: two entries with details.setting="test_audit_setting", actions "update" and "delete"
```

### 6. Agent Rename Audit

```bash
# Create a temp agent, rename it, check audit
AGENT_NAME="<agent-to-rename>"

curl -s -X PUT -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  "http://localhost:8000/api/agents/${AGENT_NAME}/rename" \
  -d '{"new_name":"renamed-agent"}' | jq .

# Check audit log
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/audit-log?event_type=agent_lifecycle&limit=5" | jq '.entries[] | select(.event_action=="rename")'
# Expected: event_action="rename", details.old_name, details.new_name
```

---

## Phase 3 — MCP Tool Call Audit

### 7. MCP Tool Calls via Claude Code

Use any MCP tool through Claude Code or direct MCP call:

```bash
# If you have an MCP API key:
MCP_KEY="trinity_mcp_..."

curl -s -X POST http://localhost:8080/mcp \
  -H "Authorization: Bearer $MCP_KEY" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"list_agents","arguments":{}}}' | jq .

# Wait a moment for fire-and-forget audit POST, then check
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/audit-log?event_type=mcp_operation&limit=10" | jq '.entries[] | {event_action, mcp_key_name, mcp_scope, details}'
# Expected: event_action="tool_call", details.tool="list_agents", details.success=true
```

### 8. MCP Audit — Agent Scope

If an agent calls another agent via MCP, the audit entry should show:
- `mcp_scope="agent"`
- `actor_type="agent"`
- `actor_id=<calling-agent-name>`

Check after any agent-to-agent collaboration:

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/audit-log?source=mcp&limit=20" | jq '.entries[] | select(.mcp_scope=="agent") | {mcp_key_name, actor_id, details}'
```

### 9. MCP Audit — Failure Tracking

If an MCP tool fails, audit should show `success=false` and `error`:

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/audit-log?event_type=mcp_operation&limit=50" | jq '.entries[] | select(.details.success==false)'
```

---

## Phase 4 — Hash Chain + Export

### 10. Enable Hash Chain

```bash
# Enable
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/audit-log/hash-chain/enable?enabled=true" | jq .
# Expected: {"hash_chain_enabled": true}
```

### 11. Generate Some Entries With Hashes

After enabling, do a few auditable actions (login, setting change, etc.):

```bash
# Change a setting to generate an audit entry with hash
curl -s -X PUT -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  "http://localhost:8000/api/settings/hash_test" \
  -d '{"value":"chain_test"}' | jq .

curl -s -X DELETE -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/settings/hash_test" | jq .

# Check that new entries have entry_hash and previous_hash
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/audit-log?limit=5" | jq '.entries[] | {event_id, entry_hash, previous_hash}'
# Expected: recent entries should have non-null entry_hash and previous_hash
```

### 12. Verify Hash Chain

```bash
# Get ID range from recent entries
START_ID=$(curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/audit-log?limit=100" | jq '.entries[-1].id')
END_ID=$(curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/audit-log?limit=1" | jq '.entries[0].id')

# Verify chain
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/audit-log/verify?start_id=${START_ID}&end_id=${END_ID}" | jq .
# Expected: {"valid": true, "checked": N, "first_invalid_id": null}
```

### 13. Disable Hash Chain

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/audit-log/hash-chain/enable?enabled=false" | jq .
# Expected: {"hash_chain_enabled": false}
```

### 14. Export — JSON

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/audit-log/export?start_time=2020-01-01T00:00:00Z&end_time=2030-01-01T00:00:00Z&format=json" | jq '.count'
# Expected: number > 0
```

### 15. Export — CSV

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/audit-log/export?start_time=2020-01-01T00:00:00Z&end_time=2030-01-01T00:00:00Z&format=csv" -o audit_export.csv

head -5 audit_export.csv
# Expected: CSV with headers (event_id, event_type, event_action, ...)

# Clean up
rm audit_export.csv
```

---

## Cross-Cutting Checks

### 16. Stats Endpoint

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/audit-log/stats" | jq .
# Expected: total > 0, by_event_type and by_actor_type breakdowns
```

### 17. Filter Combinations

```bash
# By actor
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/audit-log?actor_type=user&limit=5" | jq '.total'

# By source
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/audit-log?source=mcp&limit=5" | jq '.total'

# By target
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/audit-log?target_type=agent&limit=5" | jq '.total'
```

### 18. Non-Admin Access Denied

```bash
# If you have a non-admin user token:
NON_ADMIN_TOKEN="..."

curl -s -H "Authorization: Bearer $NON_ADMIN_TOKEN" \
  "http://localhost:8000/api/audit-log" | jq .
# Expected: 403 Forbidden
```

### 19. Audit Never Breaks Caller

Verify that even if audit logging fails (e.g. DB issue), the primary
operation still succeeds. This is tested by unit tests but can be observed
by checking that all normal operations (create agent, login, etc.) work
normally regardless of audit state.

---

## Checklist

| # | Test | Pass? |
|---|------|-------|
| 1 | Request-ID header present + passthrough | |
| 2 | Admin login success/failure audited | |
| 3 | Email login success/failure audited | |
| 4 | Share/unshare audited | |
| 5 | Credential inject/export/import audited | |
| 6 | Setting update/delete audited | |
| 7 | Agent rename audited | |
| 8 | MCP tool call audited (user scope) | |
| 9 | MCP tool call audited (agent scope) | |
| 10 | MCP tool failure includes error | |
| 11 | Hash chain enable/disable works | |
| 12 | Entries get hashes when chain enabled | |
| 13 | Verify returns valid=true for intact chain | |
| 14 | JSON export works | |
| 15 | CSV export downloads file | |
| 16 | Stats endpoint returns breakdowns | |
| 17 | Filter combinations work | |
| 18 | Non-admin gets 403 | |
| 19 | Audit failure never breaks caller | |
