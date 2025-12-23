# Feature: System-Wide Trinity Prompt

## Overview
Allows admins to set a custom system prompt (Trinity Prompt) that gets injected into ALL agents' CLAUDE.md files at startup, enabling platform-wide custom instructions for all agents.

## User Story
As an admin, I want to define custom instructions that apply to all agents so that I can enforce platform-wide policies, coding standards, or behavioral guidelines without configuring each agent individually.

## Entry Points
- **UI**: `/Users/eugene/Dropbox/trinity/trinity/src/frontend/src/views/Settings.vue` - Settings page with Trinity Prompt textarea editor
- **API**: `GET/PUT/DELETE /api/settings/{key}` where key is `trinity_prompt`

## Frontend Layer

### Components
- `/Users/eugene/Dropbox/trinity/trinity/src/frontend/src/views/Settings.vue` - Full Settings page with:
  - Textarea for editing Trinity Prompt (lines 39-51)
  - Save/Clear buttons (lines 66-85)
  - Character count display (line 61)
  - Unsaved changes indicator (line 62)
  - Error message display (lines 114-126)
  - Success message display (lines 128-140)

### State Management
- `/Users/eugene/Dropbox/trinity/trinity/src/frontend/src/stores/settings.js`
  - `fetchSettings()` - Fetches all settings from backend (lines 23-43)
  - `updateSetting(key, value)` - Updates a setting via PUT (lines 66-81)
  - `deleteSetting(key)` - Deletes a setting via DELETE (lines 87-102)
  - `trinityPrompt` getter - Returns settings['trinity_prompt'] (lines 13-15)

### API Calls
```javascript
// Fetch all settings
await axios.get('/api/settings')

// Update trinity_prompt
await axios.put('/api/settings/trinity_prompt', { value: promptText })

// Delete/clear trinity_prompt
await axios.delete('/api/settings/trinity_prompt')
```

### Routing
- `/Users/eugene/Dropbox/trinity/trinity/src/frontend/src/router/index.js:48-52`
  - Route: `/settings` -> `Settings.vue`
  - Meta: `requiresAuth: true, requiresAdmin: true`

### Navigation
- `/Users/eugene/Dropbox/trinity/trinity/src/frontend/src/components/NavBar.vue:46-53`
  - Settings link conditionally rendered: `v-if="isAdmin"` (line 47)
  - Admin check via `GET /api/users/me` response role field (lines 218-223)

## Backend Layer

### Endpoints
- `/Users/eugene/Dropbox/trinity/trinity/src/backend/routers/settings.py`
  - `GET /api/settings` (line 24) - List all settings (admin-only)
  - `GET /api/settings/{key}` (line 52) - Get specific setting (admin-only)
  - `PUT /api/settings/{key}` (line 88) - Create/update setting (admin-only)
  - `DELETE /api/settings/{key}` (line 130) - Delete setting (admin-only)

### Business Logic
1. **Admin Check** (lines 18-21):
   ```python
   def require_admin(current_user: User):
       if current_user.role != "admin":
           raise HTTPException(status_code=403, detail="Admin access required")
   ```

2. **Setting Storage**: Uses `db.set_setting(key, value)` for upsert
3. **Audit Logging**: All operations logged via `log_audit_event()`

### Database Operations
- `/Users/eugene/Dropbox/trinity/trinity/src/backend/db/settings.py` - `SettingsOperations` class

**Table**: `system_settings`
```sql
CREATE TABLE IF NOT EXISTS system_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
```

**Setting Key**: `trinity_prompt`

**Operations**:
- `get_setting(key)` - SELECT by key (lines 31-47)
- `get_setting_value(key, default)` - Get just the value (lines 49-58)
- `set_setting(key, value)` - INSERT OR REPLACE (lines 60-83)
- `delete_setting(key)` - DELETE by key (lines 85-98)
- `get_all_settings()` - Get all settings (lines 100-114)

### Models
- `/Users/eugene/Dropbox/trinity/trinity/src/backend/db_models.py:281-290`
  ```python
  class SystemSetting(BaseModel):
      key: str
      value: str
      updated_at: datetime

  class SystemSettingUpdate(BaseModel):
      value: str
  ```

## Agent Injection Flow

### Backend Agent Startup
- `/Users/eugene/Dropbox/trinity/trinity/src/backend/routers/agents.py:50-111`
  - `inject_trinity_meta_prompt(agent_name)` function
  - Called during `start_agent_internal()` (line 147)

**Injection Logic** (lines 72-80):
```python
# Fetch system-wide custom prompt setting
custom_prompt = db.get_setting_value("trinity_prompt", default=None)

# Build request payload
payload = {"force": False}
if custom_prompt:
    payload["custom_prompt"] = custom_prompt
```

### Agent-Server Injection
- `/Users/eugene/Dropbox/trinity/trinity/docker/base-image/agent_server/routers/trinity.py:86-304`
  - `POST /api/trinity/inject` endpoint (line 86)
  - Handles `TrinityInjectRequest.custom_prompt` field

**Custom Instructions Injection** (lines 239-248):
```python
# Build the custom instructions section if provided
custom_section = ""
if request.custom_prompt and request.custom_prompt.strip():
    custom_section = f"""

## Custom Instructions

{request.custom_prompt.strip()}
"""
```

**CLAUDE.md Update** (lines 250-289):
- If CLAUDE.md exists and has Trinity section: Append/update Custom Instructions
- If CLAUDE.md exists without Trinity: Add both sections
- If CLAUDE.md doesn't exist: Create with Trinity + Custom sections
- Removes existing "## Custom Instructions" before updating (lines 254-263)
- Tracks `had_custom_instructions` flag to ensure removal when prompt is cleared (lines 255-262)

### Models
- `/Users/eugene/Dropbox/trinity/trinity/docker/base-image/agent_server/models.py:177-180`
  ```python
  class TrinityInjectRequest(BaseModel):
      force: bool = False
      custom_prompt: Optional[str] = None  # System-wide custom prompt
  ```

## Data Flow Diagram

```
+-------------+      +----------------+      +------------------+
| Settings.vue| ---> | settings.js    | ---> | PUT /api/settings|
| (Admin UI)  |      | (Pinia Store)  |      | /trinity_prompt  |
+-------------+      +----------------+      +------------------+
                                                    |
                                                    v
                                            +------------------+
                                            | system_settings  |
                                            | (SQLite table)   |
                                            +------------------+

                            (Later, when agent starts)

+------------------+      +------------------------+      +-------------------+
| POST /api/agents | ---> | inject_trinity_meta_   | ---> | agent-server      |
| /{name}/start    |      | prompt()               |      | POST /api/trinity |
+------------------+      | (reads trinity_prompt) |      | /inject           |
                          +------------------------+      +-------------------+
                                                                   |
                                                                   v
                                                          +-------------------+
                                                          | CLAUDE.md         |
                                                          | ## Custom         |
                                                          | Instructions      |
                                                          +-------------------+
```

## Side Effects

### Audit Logging
- `/Users/eugene/Dropbox/trinity/trinity/src/backend/routers/settings.py`
  - All CRUD operations logged via `log_audit_event()`
  - Events: `system_settings.list`, `system_settings.read`, `system_settings.update`, `system_settings.delete`
  - Details include: key name, value length (not value itself), success/failure

Example audit entry:
```python
await log_audit_event(
    event_type="system_settings",
    action="update",
    user_id=current_user.username,
    resource=f"setting:trinity_prompt",
    result="success",
    details={"key": "trinity_prompt", "value_length": 150}
)
```

### No WebSocket Broadcasts
This feature does not broadcast changes via WebSocket. Agents only receive the prompt when they start/restart.

## Error Handling

| Error Case | HTTP Status | Message | Location |
|------------|-------------|---------|----------|
| Not admin | 403 | "Admin access required" | settings.py:21 |
| Setting not found (GET) | 404 | "Setting '{key}' not found" | settings.py:70 |
| Database error | 500 | "Failed to get/update/delete setting: {error}" | settings.py |
| Agent not reachable | N/A | Returns error in injection result | agents.py:105 |

## Security Considerations

### Authorization
- All `/api/settings` endpoints require admin role
- `require_admin()` check on every endpoint (lines 34, 64, 100, 141 in settings.py)
- Frontend NavBar hides Settings link for non-admins (NavBar.vue line 47)
- Router meta requires admin: `requiresAdmin: true` (router/index.js line 51)

### Input Validation
- Pydantic `SystemSettingUpdate` model validates request body
- No character limit enforced (could store large prompts)
- Markdown content allowed (injected as-is into CLAUDE.md)

### Audit Trail
- All changes logged with user, timestamp, resource, and result
- Value contents NOT logged (only length) - safe for sensitive instructions

## Testing

### Prerequisites
- Trinity platform running (`./scripts/deploy/start.sh`)
- Admin user logged in

### Test Steps

1. **Access Settings Page**
   - Action: Navigate to `/settings` as admin
   - Expected: Settings page loads with Trinity Prompt section
   - Verify: Textarea visible, Save/Clear buttons present

2. **Non-Admin Access Denied**
   - Action: Try to access `/settings` as non-admin user
   - Expected: Redirected to `/` or 403 error on API call
   - Verify: Settings link not visible in NavBar

3. **Save Trinity Prompt**
   - Action: Enter text in textarea, click "Save Changes"
   - Expected: Success message appears, "Unsaved changes" clears
   - Verify: `GET /api/settings/trinity_prompt` returns saved value

4. **Clear Trinity Prompt**
   - Action: Click "Clear" button
   - Expected: Textarea emptied, setting deleted
   - Verify: `GET /api/settings/trinity_prompt` returns 404

5. **Agent Receives Prompt**
   - Action: Save a prompt, then start a new agent
   - Expected: Agent's CLAUDE.md contains "## Custom Instructions" section
   - Verify: SSH into agent, check CLAUDE.md content

6. **Restart Agent Updates Prompt**
   - Action: Update trinity_prompt, restart existing agent
   - Expected: Agent's CLAUDE.md updated with new content
   - Verify: Check CLAUDE.md after restart

### Automated Tests

**Test File**: `/Users/eugene/Dropbox/trinity/trinity/tests/test_settings.py`

**Test Classes** (19 tests total):

| Class | Tests | Coverage |
|-------|-------|----------|
| `TestSettingsEndpointsAuthentication` (lines 24-51) | 4 | Auth required for all endpoints |
| `TestSettingsEndpointsAdmin` (lines 54-148) | 6 | Admin CRUD operations |
| `TestTrinityPromptSetting` (lines 151-238) | 3 | Trinity prompt specific operations |
| `TestTrinityPromptInjection` (lines 241-415) | 3 | Agent injection verification (slow) |
| `TestSettingsValidation` (lines 418-453) | 3 | Input validation |

**Key Test Cases**:
- `test_list_settings_requires_auth` (line 29) - Verifies 401 without token
- `test_create_and_get_setting` (line 75) - Full CRUD cycle
- `test_trinity_prompt_crud` (line 156) - Specific trinity_prompt handling
- `test_agent_receives_prompt_on_creation` (line 249) - Verifies CLAUDE.md injection
- `test_prompt_removed_when_cleared` (lines 355-415) - Verifies bug fix

### Status
**Last Tested**: 2025-12-14
**Tested By**: claude
**Status**: All tests passed (19/19)

**Test Results**:
- Settings API (CRUD operations): Working
- Trinity Prompt injection on agent creation: Working
- Trinity Prompt update on agent restart: Working
- Trinity Prompt removal when cleared: Working (bug fixed)

## Known Issues / Bug Fixes

### Bug Fix: Custom Instructions Not Removed When Cleared (2025-12-14)

**Issue**: When clearing the `trinity_prompt` setting, the "## Custom Instructions" section was not being removed from CLAUDE.md on agent restart.

**Root Cause**: The condition at line 271 only checked for `custom_section` being non-empty, so when the prompt was cleared (empty `custom_section`), the code would not rewrite CLAUDE.md even though it had stripped out the old Custom Instructions section.

**Fix Location**: `/Users/eugene/Dropbox/trinity/trinity/docker/base-image/agent_server/routers/trinity.py`

**Changes**:
1. Added `had_custom_instructions` flag tracking at lines 255-262:
   ```python
   had_custom_instructions = False
   if "## Custom Instructions" in content:
       # ... strip existing section ...
       had_custom_instructions = True
   ```

2. Modified condition at line 271 from:
   ```python
   elif custom_section:
   ```
   to:
   ```python
   elif custom_section or had_custom_instructions:
   ```

3. Added log message at line 280:
   ```python
   logger.info("Removed Custom Instructions from CLAUDE.md")
   ```

**Verification**: Test `test_prompt_removed_when_cleared` in `/Users/eugene/Dropbox/trinity/trinity/tests/test_settings.py` validates the fix.

## Related Flows
- **Upstream**: Admin authentication (auth0-authentication.md)
- **Downstream**: Agent Lifecycle (agent-lifecycle.md) - Trinity injection at startup

---

*Document created: 2025-12-13*
*Feature implemented: 2025-12-13*
*Last updated: 2025-12-19* (verified line numbers against modular codebase structure)
