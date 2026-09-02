# Feature: System-Wide Trinity Prompt

## Overview
Allows admins to set a custom system prompt (Trinity Prompt) that is injected into every Claude Code invocation via `--append-system-prompt`, enabling platform-wide custom instructions for all agents.

**Architecture change (2026-03-15, Issue #136)**: Replaced file-based injection (`CLAUDE.local.md` written at startup) with runtime injection. The backend now sends platform instructions with every chat/task request. The agent server passes them to Claude Code via `--append-system-prompt`. This eliminates startup coordination issues and ensures instructions cannot be modified by agents.

## User Story
As an admin, I want to define custom instructions that apply to all agents so that I can enforce platform-wide policies, coding standards, or behavioral guidelines without configuring each agent individually.

## Entry Points
- **UI**: `src/frontend/src/views/Settings.vue` - Settings page with Trinity Prompt textarea editor
- **API**: `GET/PUT/DELETE /api/settings/{key}` where key is `trinity_prompt`

## Frontend Layer

### Components
- `src/frontend/src/views/Settings.vue` - Full Settings page with:
  - Textarea for editing Trinity Prompt (lines 240-252)
  - Save/Clear buttons (lines 267-286)
  - Character count display (line 262)
  - Unsaved changes indicator (line 263)
  - Error message display (lines 456-468)
  - Success message display (lines 470-482)
  - Info box explaining how it works (lines 432-452)

### State Management
- `src/frontend/src/stores/settings.js`
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
- `src/frontend/src/router/index.js:138-142`
  - Route: `/settings` -> `Settings.vue`
  - Meta: `requiresAuth: true, requiresAdmin: true`

### Navigation
- `src/frontend/src/components/NavBar.vue:46-53`
  - Settings link conditionally rendered: `v-if="isAdmin"` (line 47)
  - Admin check via `GET /api/users/me` response role field (lines 254-261)

## Backend Layer

### Settings Service
- `src/backend/services/settings_service.py` - Centralized settings retrieval
  - `SettingsService` class (lines 49-101)
  - `get_setting(key, default)` method (lines 59-62)
  - Singleton instance `settings_service` (line 104)

### Endpoints
- `src/backend/routers/settings.py`
  - `GET /api/settings` (line 75) - List all settings (admin-only)
  - `GET /api/settings/{key}` (line 510) - Get specific setting (admin-only)
  - `PUT /api/settings/{key}` (line 537) - Create/update setting (admin-only)
  - `DELETE /api/settings/{key}` (line 559) - Delete setting (admin-only)

### Business Logic
1. **Admin Check** (lines 69-72):
   ```python
   def require_admin(current_user: User):
       if current_user.role != "admin":
           raise HTTPException(status_code=403, detail="Admin access required")
   ```

2. **Setting Storage**: Uses `db.set_setting(key, value)` for upsert

### Database Operations
- `src/backend/db/settings.py` - `SettingsOperations` class

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
- `get_settings_dict()` - Get settings as key-value dict (lines 116-123)

### Models
- `src/backend/db_models.py:285-294`
  ```python
  class SystemSetting(BaseModel):
      key: str
      value: str
      updated_at: datetime

  class SystemSettingUpdate(BaseModel):
      value: str
  ```

## Runtime Injection Flow (Issue #136)

Platform instructions are injected at runtime on every Claude Code invocation — not at startup.

### Platform Prompt Service (Single Source of Truth)
- `src/backend/services/platform_prompt_service.py`
  - `PLATFORM_INSTRUCTIONS` constant — static platform instructions (collaboration tools, operator queue ref, package persistence)
  - `get_platform_system_prompt()` — combines static instructions with `trinity_prompt` DB setting

### Sections, tiers, and the two one-shot-runtime rules (#2454 → #2468)
- Section machinery: every `### `-headed section must be registered in `_KNOWN_SECTION_HEADINGS` (equality-pinned by `tests/unit/test_ent243_prompt_tier.py`); `_MINIMAL_DROP_SECTIONS` names the tool-usage sections lower tiers drop; `_ALWAYS_SECTIONS` is **derived** (`_KNOWN − _MINIMAL_DROP`), never hand-listed. `_adapt_instructions_for_runtime` strips `mcp__trinity__` for Codex (so section text spells `mcp__trinity__set_reminder` and survives as `set_reminder`; `_CODEX_MCP_ORIENTATION` enumerates the bare names incl. `set_reminder`/`run_agent_loop`).
- **"Repeating Work and Deferred Ticks"** (#2454, always-tier): routes repetition to `run_agent_loop`/`set_reminder`; its negative rule names the harness `/loop` skill + `ScheduleWakeup`, and — since #2468 — the whole denied family, phrased as a **capability fact** ("dies with this process") never a policy fact, so the sentence stays true on fleet images the deny has not reached yet (mixed-fleet window: the prompt ships on backend deploy, the deny on the image cut). Forward vocabulary coupling (every denied name appears here) pinned by `test_2454_loop_routing.py`; reverse (every name this paragraph claims removed is actually denied) by `test_2468_headless_tool_audit.py`.
- **"Nothing survives the end of your turn"** (#2468, always-tier): the counterweight for the one promise Trinity cannot deny — Bash's own *"You will be notified when it completes"* (verbatim on 2.1.235; its text also blocks long leading sleeps and routes polling to the denied `Monitor`). Claims are truth-scoped: the kill-claim covers **commands** (subagents are `local_agent` = CLI-waited, said explicitly), no "no second turn" absolutism (false on `--resume` surfaces), and it names the working wait idiom (foreground until-loop under `timeout`) plus the split-persist-`set_reminder` alternative. Cost: ≈ +390 input tokens on every execution, every tier — accepted as the fix's backend-reachable half. Deny mechanism + audit: [agent-guardrails.md](agent-guardrails.md) § Platform tool denials.

### Chat Path (Interactive)
- `src/backend/routers/chat.py`
  - `POST /api/agents/{name}/chat` — calls `get_platform_system_prompt()`, adds `system_prompt` to payload sent to agent `/api/chat`

### Task Path (Headless)
- `src/backend/services/task_execution_service.py`
  - `execute_task()` — calls `get_platform_system_prompt()`, prepends to any caller-provided `system_prompt`
  - All callers (scheduled tasks, MCP delegation, manual tasks) flow through here

### Agent Server — Pass-Through
- `docker/base-image/agent_server/models.py`
  - `ChatRequest` — accepts optional `system_prompt` field
  - `ParallelTaskRequest` — already had `system_prompt` field
- `docker/base-image/agent_server/routers/chat.py`
  - `POST /api/chat` — passes `request.system_prompt` to `runtime.execute()`
  - `POST /api/task` — already passed `request.system_prompt` to `runtime.execute_headless()`
- `docker/base-image/agent_server/services/runtime_adapter.py`
  - `AgentRuntime.execute()` — accepts `system_prompt` parameter
  - `AgentRuntime.execute_headless()` — already accepted `system_prompt`
- `docker/base-image/agent_server/services/claude_code.py`
  - `execute_claude_code()` — adds `--append-system-prompt` when `system_prompt` provided
  - `execute_headless_task()` — already supported `--append-system-prompt`

### Trinity Status Endpoint (Agent-Side)
- `docker/base-image/agent_server/routers/trinity.py`
  - `GET /api/trinity/status` — returns `{injected: true}` (always true, injection is per-request)
  - `POST /api/trinity/inject` and `POST /api/trinity/reset` — **removed**

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

              (On every chat/task request — NOT at startup)

+------------------+      +------------------------+      +-------------------+
| POST /api/agents | ---> | get_platform_system_   | ---> | Agent Server      |
| /{name}/chat     |      | prompt()               |      | POST /api/chat    |
| POST /api/task   |      | (reads trinity_prompt  |      | {system_prompt}   |
+------------------+      |  + static instructions) |      +-------------------+
                          +------------------------+              |
                                                                  v
                                                          +-------------------+
                                                          | Claude Code CLI   |
                                                          | --append-system-  |
                                                          |   prompt "..."    |
                                                          +-------------------+
```

## Key Design Properties

1. **No startup coordination** — instructions are sent with every request, so fleet restart, container recreation, volume wipes all just work
2. **Immediate effect** — changing `trinity_prompt` takes effect on the next message to any agent (no restart needed)
3. **Immutable from agent** — platform instructions are CLI flags, not files the agent can modify
4. **Per-invocation** — `--append-system-prompt` is per-invocation (not accumulated across `--continue` calls), safe on every call

## Side Effects

### No Audit Logging
The current implementation does not log settings CRUD operations via audit logging.

### No WebSocket Broadcasts
This feature does not broadcast changes via WebSocket. Changes take effect immediately on next interaction.

## Error Handling

| Error Case | HTTP Status | Message | Location |
|------------|-------------|---------|----------|
| Not admin | 403 | "Admin access required" | settings.py:72 |
| Setting not found (GET) | 404 | "Setting '{key}' not found" | settings.py:528 |
| Database error | 500 | "Failed to get/update/delete setting: {error}" | settings.py |

## Security Considerations

### Authorization
- All `/api/settings` endpoints require admin role
- `require_admin()` check on every endpoint
- Frontend NavBar hides Settings link for non-admins
- Router meta requires admin: `requiresAdmin: true`

### Input Validation
- Pydantic `SystemSettingUpdate` model validates request body
- No character limit enforced (could store large prompts)
- Markdown content allowed (injected as-is into system prompt)

### Agent Security
- Platform instructions cannot be modified by the agent (CLI flag, not a file)
- `--append-system-prompt` is per-invocation, not persisted in agent state

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

5. **Agent Receives Prompt Immediately**
   - Action: Save a prompt, send a chat message to any running agent
   - Expected: Platform instructions appear in Claude Code's system prompt
   - Verify: Check execution log for `--append-system-prompt` flag

6. **Change Takes Effect Without Restart**
   - Action: Update trinity_prompt, send another message to same agent
   - Expected: Next message uses the updated prompt (no restart needed)

7. **Codebase Cleanup Verification**
   - Action: `grep -r "CLAUDE.local.md\|inject_trinity_meta_prompt\|/api/trinity/inject" --include="*.py" .`
   - Expected: Zero results (no old injection code remains)

### Automated Tests

**Test File**: `tests/test_settings.py`

**Test Classes**:

| Class | Lines | Tests | Coverage |
|-------|-------|-------|----------|
| `TestSettingsEndpointsAuthentication` | 24-51 | 4 | Auth required for all endpoints |
| `TestSettingsEndpointsAdmin` | 54-149 | 6 | Admin CRUD operations |
| `TestTrinityPromptSetting` | 151-238 | 3 | Trinity prompt specific operations |
| `TestTrinityPromptInjection` | 241-415 | 3 | Agent injection verification (may need update for runtime injection) |
| `TestSettingsValidation` | 418-454 | 3 | Input validation |

## Known Issues / Bug Fixes

### Refactor: Runtime injection via --append-system-prompt (2026-03-15, Issue #136)

**Problem**: File-based injection (`CLAUDE.local.md`) required every code path that starts/restarts a container to remember to call `inject_trinity_meta_prompt()`. Multiple paths didn't: `lifespan()`, `ensure_deployed()`, fleet restart, container recreation. Additionally, agents could theoretically modify or delete the file.

**Solution**: Backend sends platform instructions with every chat/task request via `system_prompt` field. Agent server passes them to Claude Code via `--append-system-prompt`. Single source of truth in `platform_prompt_service.py`.

**Removed code**:
- `inject_trinity_meta_prompt()` function and all call sites
- `AgentClient.inject_trinity_prompt()` method
- `POST /api/trinity/inject` and `POST /api/trinity/reset` endpoints
- `TrinityInjectRequest`, `TrinityInjectResponse` models
- File-writing logic in agent-side trinity.py

### Bug Fix: Platform instructions lost during git operations (2026-03-14)

**Issue**: Now moot — runtime injection means no files to lose during git operations.

### Bug Fix: Custom Instructions Not Removed When Cleared (2025-12-14)

**Issue**: Now moot — `get_platform_system_prompt()` reads from DB on every call; if cleared, the section is simply omitted.

## Related Flows
- **Upstream**: Admin authentication (admin-login.md)
- **Downstream**: Agent chat execution (authenticated-chat-tab.md, parallel-headless-execution.md)

---

## Revision History

| Date | Author | Changes |
|------|--------|---------|
| 2025-12-13 | claude | Document created, feature implemented |
| 2025-12-14 | claude | Bug fix: Custom instructions removal when cleared |
| 2025-12-19 | claude | Verified line numbers against current codebase |
| 2026-01-23 | claude | Updated line numbers after refactoring |
| 2026-03-14 | claude | Platform instructions moved from CLAUDE.md to CLAUDE.local.md |
| 2026-03-15 | claude | Major rewrite: Runtime injection via --append-system-prompt (Issue #136). Removed file-based injection entirely. |
| 2026-09-01 | claude | #2454/#2468: section/tier machinery documented; "Repeating Work" family paragraph (capability-first) + always-tier "Nothing survives the end of your turn" counterweight |
