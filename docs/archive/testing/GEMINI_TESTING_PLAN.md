# Gemini Runtime Testing Plan

**Purpose**: Validate Gemini CLI integration before merging to main.
**Duration**: ~30-45 minutes
**Prerequisites**:
- Trinity running locally (`docker compose up -d`)
- Google API key from [AI Studio](https://makersuite.google.com/app/apikey)

---

## Phase 1: Setup (5 min)

### 1.1 Add Google API Key

```bash
# Add to your .env file
echo "GOOGLE_API_KEY=your-google-api-key-here" >> .env

# Verify it's set
grep GOOGLE_API_KEY .env
```

### 1.2 Rebuild Base Image

```bash
# This installs Gemini CLI in the base image
./scripts/deploy/build-base-image.sh

# Verify version tag
docker images | grep trinity-agent-base
# Should show: trinity-agent-base:0.9.0 and :latest
```

### 1.3 Restart Backend

```bash
docker compose restart backend

# Verify backend picked up GOOGLE_API_KEY
docker compose logs backend | grep -i "google\|gemini" | head -5
```

### 1.4 Verify Version Endpoint

```bash
curl -s http://localhost:8000/api/version | jq
# Should show version 0.9.0 and runtimes: ["claude-code", "gemini-cli"]
```

---

## Phase 2: UI Testing (10 min)

### 2.1 Create Claude Agent (Control)

1. Open http://localhost:3000
2. Login with your credentials
3. Click **"+ Create Agent"**
4. Configure:
   - **Name**: `test-claude`
   - **Template**: `local:test-echo` (or any simple template)
5. Click **Create**
6. Wait for agent to start (green status)

**Expected**: Agent created successfully, shows "claude-code" or no runtime indicator (default)

### 2.2 Create Gemini Agent

1. Click **"+ Create Agent"** again
2. Configure:
   - **Name**: `test-gemini`
   - **Template**: `local:test-gemini`
3. Click **Create**
4. Wait for agent to start

**Expected**: Agent created successfully

### 2.3 Test Chat - Claude Agent

1. Click on `test-claude`
2. Send message: `Hello, what runtime are you using?`
3. Observe:
   - [ ] Response received
   - [ ] Cost tracking shows ($ amount)
   - [ ] Context window shows 200K

### 2.4 Test Chat - Gemini Agent

1. Click on `test-gemini`
2. Send message: `Hello, what runtime are you using? What's your context window?`
3. Observe:
   - [ ] Response received
   - [ ] Cost tracking (may show $0 for free tier)
   - [ ] Context window shows 1M (1,000,000)

### 2.5 Test Model Selection (Gemini)

1. In `test-gemini` chat, click model selector (if available)
2. Try changing to `gemini-2.5-flash`
3. Send another message
4. Observe model is respected

---

## Phase 3: API Testing (10 min)

### 3.1 Get Auth Token

```bash
# Login and get token
TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"your-password"}' | jq -r '.access_token // empty')

echo "Token: ${TOKEN:0:20}..."
```

### 3.2 Check Agent Health - Claude

```bash
curl -s http://localhost:8000/api/agents/test-claude/health \
  -H "Authorization: Bearer $TOKEN" | jq
```

**Expected**:
```json
{
  "status": "healthy",
  "runtime": "claude-code",
  "runtime_available": true
}
```

### 3.3 Check Agent Health - Gemini

```bash
curl -s http://localhost:8000/api/agents/test-gemini/health \
  -H "Authorization: Bearer $TOKEN" | jq
```

**Expected**:
```json
{
  "status": "healthy",
  "runtime": "gemini-cli",
  "runtime_available": true
}
```

### 3.4 Test Chat API - Gemini

```bash
curl -s -X POST http://localhost:8000/api/agents/test-gemini/chat \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": "What is 2+2? Reply with just the number."}' | jq
```

**Expected**: Response with `"response"`, `"metadata"` including cost/tokens

### 3.5 Test Model API - Gemini

```bash
# Get current model
curl -s http://localhost:8000/api/agents/test-gemini/proxy/api/model \
  -H "Authorization: Bearer $TOKEN" | jq

# Should show available_models: ["gemini-2.5-pro", "gemini-2.5-flash", ...]
```

### 3.6 Test Session Info

```bash
curl -s http://localhost:8000/api/agents/test-gemini/proxy/api/chat/session \
  -H "Authorization: Bearer $TOKEN" | jq
```

**Expected**: Shows `context_window: 1000000` for Gemini

---

## Phase 4: Agent-to-Agent Communication (15 min)

This tests if a Gemini agent can delegate tasks to a Claude agent (and vice versa).

### 4.1 Create Orchestrator Agent (Gemini)

```bash
# Create orchestrator that uses Gemini
curl -s -X POST http://localhost:8000/api/agents \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "orchestrator-gemini",
    "type": "orchestrator",
    "runtime": "gemini-cli",
    "runtime_model": "gemini-2.5-pro",
    "resources": {"cpu": "2", "memory": "4g"}
  }' | jq
```

### 4.2 Create Worker Agent (Claude)

```bash
# Create worker that uses Claude
curl -s -X POST http://localhost:8000/api/agents \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "worker-claude",
    "type": "worker",
    "runtime": "claude-code",
    "resources": {"cpu": "2", "memory": "4g"}
  }' | jq
```

### 4.3 Grant Permissions

```bash
# Grant orchestrator permission to call worker
curl -s -X POST http://localhost:8000/api/agents/orchestrator-gemini/permissions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"target_agent": "worker-claude", "permission": "delegate"}' | jq
```

### 4.4 Test Cross-Runtime Delegation

In the UI or via API, send to `orchestrator-gemini`:

```
Use the Trinity MCP tools to send a task to worker-claude asking it to
calculate the factorial of 5. Report back what it says.
```

**Expected**:
- Gemini orchestrator calls Trinity MCP
- Worker (Claude) receives task
- Worker responds with "120"
- Orchestrator reports result

### 4.5 Verify in Logs

```bash
# Check orchestrator logs
docker logs agent-orchestrator-gemini 2>&1 | tail -20

# Check worker logs
docker logs agent-worker-claude 2>&1 | tail -20
```

---

## Phase 5: Edge Cases & Error Handling (5 min)

### 5.1 Test Without Google API Key

```bash
# Create agent without GOOGLE_API_KEY in container
# (This should fail gracefully)
docker exec agent-test-gemini env | grep -i google
# Should show GOOGLE_API_KEY
```

### 5.2 Test Invalid Model

```bash
curl -s -X PUT http://localhost:8000/api/agents/test-gemini/proxy/api/model \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model": "invalid-model-name"}' | jq
```

**Expected**: Error message about invalid model

### 5.3 Test Rate Limiting (Gemini Free Tier)

Send multiple rapid requests to test free tier limits:

```bash
for i in {1..5}; do
  curl -s -X POST http://localhost:8000/api/agents/test-gemini/chat \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"message\": \"Count to $i\"}" &
done
wait
```

**Expected**: All should succeed (free tier is 60/min)

### 5.4 Test Parallel Task (Headless)

```bash
curl -s -X POST http://localhost:8000/api/agents/test-gemini/proxy/api/task \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "message": "What is the capital of France? Reply in one word.",
    "timeout_seconds": 30
  }' | jq
```

**Expected**: Returns response with `session_id`

---

## Phase 6: Cleanup

```bash
# Delete test agents
for agent in test-claude test-gemini orchestrator-gemini worker-claude; do
  curl -s -X DELETE "http://localhost:8000/api/agents/$agent" \
    -H "Authorization: Bearer $TOKEN"
  echo "Deleted: $agent"
done
```

---

## Test Results Checklist

### Core Functionality
- [ ] Claude agent creates and chats successfully
- [ ] Gemini agent creates and chats successfully
- [ ] Context window shows correctly (200K vs 1M)
- [ ] Cost tracking works for both runtimes
- [ ] Model selection works for Gemini

### API Endpoints
- [ ] `/api/version` returns runtime info
- [ ] `/health` shows runtime status
- [ ] Chat API works for both runtimes
- [ ] Model API validates correctly per runtime
- [ ] Session API shows correct context window

### Agent Communication
- [ ] Gemini → Claude delegation works
- [ ] Claude → Gemini delegation works (optional test)
- [ ] MCP tools accessible from both runtimes

### Error Handling
- [ ] Invalid model rejected with clear error
- [ ] Missing API key shows helpful message
- [ ] Rate limits handled gracefully

---

## Issues Found

Document any issues here during testing:

| Issue | Severity | Notes |
|-------|----------|-------|
| | | |
| | | |
| | | |

---

## Next Steps

After testing:
1. Fix any issues found
2. Update changelog if needed
3. Create PR for review
4. Tag release: `git tag v0.9.0`

