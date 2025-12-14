# Phase 13: System Settings & Trinity Prompt

> **Purpose**: Validate admin-only Settings page and Trinity Prompt injection into agents
> **Duration**: ~15 minutes
> **Assumes**: Phase 1 PASSED (authentication working), Phase 2 PASSED (agents can be created)
> **Output**: Settings CRUD verified, Trinity Prompt injected into agent CLAUDE.md

---

## Prerequisites

- ✅ Phase 1 PASSED (logged in as admin)
- ✅ Backend healthy at http://localhost:8000
- ✅ Frontend accessible at http://localhost:3000
- ✅ At least one running agent (or ability to create one)

---

## Test Steps

### Step 1: Verify Admin Access to Settings

**Action**:
- Login as admin user
- Look for "Settings" link in the navigation bar (top right area)
- Click on "Settings" link

**Expected**:
- [ ] Settings link visible in navbar (admin users only)
- [ ] Clicking navigates to `/settings` route
- [ ] Settings page loads with "Trinity Prompt" section
- [ ] Textarea visible for editing prompt
- [ ] "Save Changes" button visible
- [ ] "Clear" button visible

**Verify**:
- [ ] No console errors
- [ ] Page title shows "Settings"
- [ ] Character count displays (e.g., "0 characters")

---

### Step 2: Verify Non-Admin Cannot Access Settings

**Action**:
- If possible, login as a non-admin user
- OR directly navigate to http://localhost:3000/settings

**Expected** (non-admin):
- [ ] Settings link NOT visible in navbar
- [ ] Direct URL access redirects to home or shows access denied

**Verify**:
- [ ] Non-admins cannot see Settings navigation
- [ ] Non-admins cannot access Settings page

*Note: In dev mode with only admin user, this test can be skipped but should be documented.*

---

### Step 3: Create Trinity Prompt

**Action**:
- Navigate to Settings page (as admin)
- Enter test prompt in textarea:
```
## Test Custom Instructions

This is a test prompt from the Settings page.

**Rules**:
1. Always be helpful
2. Use proper Markdown formatting
3. Reference this test ID: TEST-12345
```
- Click "Save Changes"

**Expected**:
- [ ] Success message appears (green toast or inline message)
- [ ] "Unsaved changes" indicator disappears
- [ ] Character count updates to reflect saved content
- [ ] Prompt value persists in textarea

**Verify**:
- [ ] API call succeeded: `PUT /api/settings/trinity_prompt`
- [ ] No console errors
- [ ] Can refresh page and see saved prompt

---

### Step 4: Verify Prompt Persistence via API

**Action**: In browser DevTools Console, run:
```javascript
fetch('/api/settings/trinity_prompt', {
  headers: { 'Authorization': `Bearer ${localStorage.getItem('token')}` }
}).then(r => r.json()).then(console.log)
```

OR via curl:
```bash
TOKEN=$(cat ~/.trinity-test-token)  # or get from browser
curl http://localhost:8000/api/settings/trinity_prompt \
  -H "Authorization: Bearer $TOKEN"
```

**Expected**:
- [ ] Returns JSON with key, value, updated_at
- [ ] Value matches saved prompt text
- [ ] Response status: 200

**Verify**:
```json
{
  "key": "trinity_prompt",
  "value": "## Test Custom Instructions\n\nThis is a test...",
  "updated_at": "2025-12-14T..."
}
```

---

### Step 5: Verify Prompt Injection into New Agent

**Action**:
- Create a new test agent (can use any template)
- Wait for agent to start and reach "running" status
- Wait additional 5 seconds for Trinity injection

**Expected**:
- [ ] Agent created successfully
- [ ] Agent reaches "running" status
- [ ] Trinity injection completes (check agent logs)

**Verify Injection** (via Files tab or API):
```bash
# Get agent's CLAUDE.md content
curl "http://localhost:8000/api/agents/YOUR_AGENT_NAME/files/CLAUDE.md" \
  -H "Authorization: Bearer $TOKEN"
```

**Expected in CLAUDE.md**:
- [ ] Contains "## Custom Instructions" section
- [ ] Contains "TEST-12345" from our test prompt
- [ ] Custom Instructions appears AFTER Trinity Planning System section

---

### Step 6: Verify Prompt Update on Agent Restart

**Action**:
- Update the Trinity Prompt in Settings:
```
## Updated Instructions

This prompt has been UPDATED.
Test ID: UPDATE-67890
```
- Save changes
- Stop the test agent
- Start the test agent again
- Wait for startup and Trinity injection

**Expected**:
- [ ] Agent stops successfully
- [ ] Agent starts successfully
- [ ] Agent logs show "Appended Custom Instructions to CLAUDE.md"

**Verify** (check CLAUDE.md again):
- [ ] Contains "UPDATE-67890" (new content)
- [ ] Does NOT contain "TEST-12345" (old content removed)

---

### Step 7: Verify Prompt Removal When Cleared

**Action**:
- Navigate to Settings page
- Click "Clear" button to empty the prompt
- Confirm action if prompted
- Stop the test agent
- Start the test agent again
- Wait for Trinity injection

**Expected**:
- [ ] Settings shows empty textarea
- [ ] API returns 404 for `GET /api/settings/trinity_prompt`
- [ ] Agent logs show "Removed Custom Instructions from CLAUDE.md" (or just Trinity section without custom)

**Verify** (check CLAUDE.md):
- [ ] "## Custom Instructions" section is GONE
- [ ] "UPDATE-67890" text is NOT present
- [ ] Trinity Planning System section still exists

---

### Step 8: Test Markdown Content Support

**Action**:
- Save a prompt with various Markdown elements:
```markdown
## Code Guidelines

Always use:
- **Bold** for emphasis
- `code blocks` for technical terms
- Lists for multiple items

### Example
\`\`\`python
def hello():
    print("Hello, World!")
\`\`\`

> This is a blockquote

| Column A | Column B |
|----------|----------|
| Value 1  | Value 2  |
```

**Expected**:
- [ ] Prompt saves successfully
- [ ] All Markdown preserved when retrieved
- [ ] Agent CLAUDE.md contains exact Markdown

**Verify**:
- [ ] No escaping or corruption of special characters
- [ ] Backticks preserved
- [ ] Table syntax preserved

---

## Critical Validations

### Admin-Only Access Control

**Validation**: Settings endpoints require admin role

```bash
# This should work (admin user)
curl http://localhost:8000/api/settings \
  -H "Authorization: Bearer $ADMIN_TOKEN"
# Expected: 200, array of settings

# Without auth should fail
curl http://localhost:8000/api/settings
# Expected: 401 Unauthorized
```

**Verify**:
- [ ] Authenticated admin: 200
- [ ] Unauthenticated: 401
- [ ] Non-admin (if testable): 403

---

### Audit Logging

**Validation**: Settings operations are logged

Check audit logs after CRUD operations:
```bash
curl http://localhost:8001/api/logs?action=system_settings \
  -H "Authorization: Bearer $TOKEN"
```

**Expected**:
- [ ] Events for: list, read, update, delete
- [ ] User attribution correct
- [ ] Resource shows "setting:trinity_prompt"

---

## Success Criteria

Phase 13 is **PASSED** when:
- ✅ Settings page accessible to admin users only
- ✅ Trinity Prompt can be created, read, updated, deleted
- ✅ New agents receive Trinity Prompt in CLAUDE.md
- ✅ Agent restart updates CLAUDE.md with new prompt
- ✅ Clearing prompt removes Custom Instructions from CLAUDE.md
- ✅ Markdown content preserved correctly
- ✅ All operations logged for audit

---

## Troubleshooting

**Settings link not visible**:
- Check if user role is "admin"
- Check NavBar.vue conditional rendering: `v-if="isAdmin"`
- Verify `/api/users/me` returns `role: "admin"`

**Prompt not saving**:
- Check browser console for API errors
- Verify backend logs: `docker logs trinity-backend | grep settings`
- Ensure SQLite database is writable

**Injection not working**:
- Check agent logs for injection activity
- Verify agent-server is receiving `custom_prompt` in request
- Check `/api/trinity/status` on agent for injection state

**Custom Instructions not removed**:
- This was a known bug fixed 2025-12-14
- Check agent-server routers/trinity.py for `had_custom_instructions` flag
- Restart agent with empty prompt should trigger removal

---

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/settings` | List all settings (admin only) |
| GET | `/api/settings/{key}` | Get specific setting |
| PUT | `/api/settings/{key}` | Create/update setting |
| DELETE | `/api/settings/{key}` | Delete setting |

**Request Body** (PUT):
```json
{
  "value": "Your prompt text here"
}
```

**Response** (GET/PUT):
```json
{
  "key": "trinity_prompt",
  "value": "Your prompt text here",
  "updated_at": "2025-12-14T12:00:00Z"
}
```

---

## Cleanup

After testing:
- [ ] Delete test agent if created
- [ ] Clear Trinity Prompt setting (or leave configured if intentional)
- [ ] No orphaned settings left

```bash
# Clear trinity_prompt
curl -X DELETE http://localhost:8000/api/settings/trinity_prompt \
  -H "Authorization: Bearer $TOKEN"
```

---

## Related Documentation

- Feature Flow: `.claude/memory/feature-flows/system-wide-trinity-prompt.md`
- API Tests: `tests/test_settings.py`
- Requirements: `requirements.md` section 10.6
- NavBar Component: `src/frontend/src/components/NavBar.vue`
- Settings Page: `src/frontend/src/views/Settings.vue`
- Settings Store: `src/frontend/src/stores/settings.js`

---

## Next Phase

Phase 13 completion marks the end of the core feature test suite.
Future phases may include:
- Phase 14: Vector Memory (Chroma)
- Phase 15: Shared Folders
- Phase 16: Advanced Agent Metrics

---

**Status**: 🟢 Ready for testing
**Last Updated**: 2025-12-14
