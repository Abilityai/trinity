# Phase 0: Setup & Prerequisites

> **Purpose**: Verify all services running and environment ready for testing
> **Duration**: ~5 minutes
> **Assumes**: Nothing (first phase)
> **Output**: Services healthy, clean slate, authentication token ready

---

## Prerequisites Check

### Step 1: Verify Backend Health
**Action**: Query backend health endpoint
```bash
curl -s http://localhost:8000/health | jq
```

**Expected**:
```json
{"status": "healthy"}
```

**Verify**:
- [ ] Status code: 200
- [ ] Response contains `"status": "healthy"`

**If Failed**: Backend not running. Start with `docker-compose up -d backend`

---

### Step 2: Verify Frontend Accessible
**Action**: Navigate to http://localhost

**Expected**:
- Page loads without errors
- Login page or Dashboard visible

**Verify**:
- [ ] HTTP 200 response
- [ ] Page renders (not blank or error)

**If Failed**: Frontend not running. Start with `docker-compose up -d frontend`

---

### Step 3: Verify Redis Running
**Action**: Check Redis container
```bash
docker ps --filter "name=trinity-redis" --format "{{.Status}}"
```

**Expected**: "Up X minutes/hours/days"

**Verify**:
- [ ] Container running
- [ ] Status shows "Up"

**If Failed**: Redis not running. Start with `docker-compose up -d redis`

---

### Step 4: Verify Docker Socket Access
**Action**: Backend can access Docker
```bash
curl -s http://localhost:8000/api/agents -H "Authorization: Bearer test" 2>&1 | head -1
```

**Expected**: JSON response (even if 401 unauthorized)

**Verify**:
- [ ] Response is JSON (not connection error)
- [ ] Backend can communicate

---

### Step 5: Get Authentication Token
**Action**: Authenticate with dev mode credentials
```bash
curl -s -X POST http://localhost:8000/api/token \
  -d "username=admin&password=YOUR_PASSWORD" \
  -H "Content-Type: application/x-www-form-urlencoded" | jq -r '.access_token // empty'
```

**Expected**: JWT token string (starts with "eyJ...")

**Verify**:
- [ ] Token returned (not error)
- [ ] Token is valid JWT format

**Store**: Save token for subsequent API calls

---

### Step 6: Clean Existing Test Agents
**Action**: Delete any existing test agents
```bash
TOKEN="<token from step 5>"
for agent in test-echo test-counter test-worker test-delegator test-scheduler test-queue test-files test-error; do
  curl -s -X DELETE "http://localhost:8000/api/agents/$agent" \
    -H "Authorization: Bearer $TOKEN"
done
```

**Expected**: 200 OK or 404 (agent doesn't exist)

**Verify**:
- [ ] No test agents remain
- [ ] Clean slate for testing

---

### Step 7: Verify GitHub Templates Available
**Action**: Check templates API
```bash
curl -s http://localhost:8000/api/templates \
  -H "Authorization: Bearer $TOKEN" | jq '[.[] | select(.id | contains("test-agent"))] | length'
```

**Expected**: 8 (all test templates available)

**Verify**:
- [ ] 8 test templates visible
- [ ] Templates include: test-agent-echo, test-agent-counter, test-agent-worker, test-agent-delegator, test-agent-scheduler, test-agent-queue, test-agent-files, test-agent-error

**If Less Than 8**: Check config.py TEST_AGENT_TEMPLATES

---

### Step 8: Verify Docker Agent Network
**Action**: Check agent network exists
```bash
docker network ls --filter "name=trinity-agent-network" --format "{{.Name}}"
```

**Expected**: `trinity-agent-network`

**Verify**:
- [ ] Network exists
- [ ] Agents can communicate

---

## Phase 0 Completion Checklist

| Check | Status |
|-------|--------|
| Backend healthy | [ ] |
| Frontend accessible | [ ] |
| Redis running | [ ] |
| Docker socket accessible | [ ] |
| Auth token obtained | [ ] |
| Test agents cleaned up | [ ] |
| 8 GitHub templates available | [ ] |
| Agent network exists | [ ] |

---

## Success Criteria

Phase 0 is **PASSED** when:
- All 8 checks above are verified
- Authentication token saved for subsequent phases
- No test agents exist (clean slate)

## Next Phase

Proceed to **Phase 1: Authentication** (`PHASE_01_AUTHENTICATION.md`)

---

## Troubleshooting

### Backend Not Starting
```bash
docker-compose logs backend --tail 50
```

### Frontend Not Loading
```bash
docker-compose logs frontend --tail 50
```

### Redis Connection Issues
```bash
docker-compose restart redis
```

### Token Invalid
- Check ADMIN_PASSWORD is set in docker-compose.yml
- Restart backend after changing env vars

---

**Phase Status**: Setup Complete
**Artifacts**: Authentication token stored
**Next**: Phase 1 - Authentication
