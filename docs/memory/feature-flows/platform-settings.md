# Feature: Platform Settings Management

## Overview

Admin-only page for managing system-wide configuration including API keys (Anthropic, GitHub), Trinity Prompt, email whitelist, SSH access toggle, ops configuration settings, GitHub template configuration (TMPL-001), MCP Server URL (#76), and default avatar generation (AVATAR-003).

## User Stories

| ID | Story | Status |
|----|-------|--------|
| SET-004 | As an admin, I want to set a GitHub PAT for GitHub templates so that I can create agents from private repositories | Implemented |
| SET-005 | As an admin, I want to test GitHub PAT permissions so that I can verify the token has required scopes | Implemented |
| SET-010 | As an admin, I want to view ops configuration so that I can see current thresholds and limits | Implemented |
| SET-011 | As an admin, I want to update ops settings so that I can tune context warnings, cost limits, and other thresholds | Implemented |
| SET-012 | As an admin, I want to reset ops settings to defaults so that I can restore standard configuration | Implemented |
| AVATAR-003 | As an admin, I want to generate default avatars for all agents so that agents without custom avatars get AI-generated ones | Implemented |
| MCP-URL-001 | As an admin, I want to configure the external MCP server URL so that API Keys page shows the correct URL for production deployments | Implemented |

## Entry Points

- **UI**: `src/frontend/src/views/Settings.vue` - Settings page accessible via navigation
- **Route**: `/settings`
- **API Endpoints**:
  - `GET /api/settings` - Get all settings
  - `GET /api/settings/api-keys` - Get API key status
  - `PUT /api/settings/api-keys/github` - Save GitHub PAT
  - `POST /api/settings/api-keys/github/test` - Test GitHub PAT
  - `GET /api/settings/ops/config` - Get ops configuration
  - `PUT /api/settings/ops/config` - Update ops settings
  - `POST /api/settings/ops/reset` - Reset ops to defaults
  - `GET /api/settings/github-templates` - Get GitHub templates config (TMPL-001)
  - `PUT /api/settings/github-templates` - Set GitHub templates (TMPL-001)
  - `DELETE /api/settings/github-templates` - Reset templates to defaults (TMPL-001)
  - `GET /api/settings/mcp-url` - Get MCP URL config (any auth user) (#76)
  - `PUT /api/settings/mcp-url` - Set custom MCP URL (admin-only) (#76)
  - `DELETE /api/settings/mcp-url` - Reset MCP URL to auto-detect (admin-only) (#76)
  - `POST /api/agents/avatars/generate-defaults` - Generate avatars for agents without one (AVATAR-003, see [agent-avatars.md](agent-avatars.md))

---

## Frontend Layer

### Components

**Settings.vue** (`src/frontend/src/views/Settings.vue`)

| Section | Lines | Description |
|---------|-------|-------------|
| API Keys Section | 23-221 | Anthropic API key and GitHub PAT configuration |
| GitHub PAT Input | 126-218 | Password input with show/hide toggle, Test and Save buttons |
| Trinity Prompt | 224-289 | Textarea for custom agent instructions |
| Email Whitelist | 291-390 | Table of whitelisted emails with add/remove |
| SSH Access Toggle | 392-430 | Toggle switch for enabling SSH access |
| MCP Server URL | 1089-1149 | Configure external MCP URL with custom/auto-detect badge (#76) |
| Default Avatars | 1054-1092 | Generate AI avatars for agents without custom ones (AVATAR-003) |

**Key Reactive State**

```javascript
// Settings.vue lines 529-539
const githubPat = ref('')
const showGithubPat = ref(false)
const testingGithubPat = ref(false)
const savingGithubPat = ref(false)
const githubPatTestResult = ref(null)
const githubPatTestMessage = ref('')
const githubPatStatus = ref({
  configured: false,
  masked: null,
  source: null
})

// SSH Access state (lines 542-543)
const sshAccessEnabled = ref(false)
const savingSshAccess = ref(false)

// Default Avatars state (lines 1248-1250)
const generatingDefaultAvatars = ref(false)
const defaultAvatarResult = ref(null)
```

### State Management

**settings.js** (`src/frontend/src/stores/settings.js`)

| Action | Lines | Description |
|--------|-------|-------------|
| `fetchSettings()` | 23-43 | Load all settings from API |
| `getSetting(key)` | 48-60 | Get single setting by key |
| `updateSetting(key, value)` | 66-81 | Update a setting value |
| `deleteSetting(key)` | 87-102 | Delete a setting |

### API Calls

**GitHub PAT Test** (Settings.vue lines 636-675)
```javascript
async function testGithubPat() {
  const response = await axios.post('/api/settings/api-keys/github/test', {
    api_key: githubPat.value
  })
  githubPatTestResult.value = response.data.valid
  githubPatTestMessage.value = response.data.valid
    ? `Valid! GitHub user: ${response.data.username}`
    : response.data.error
}
```

**GitHub PAT Save** (Settings.vue lines 677-707)
```javascript
async function saveGithubPat() {
  const response = await axios.put('/api/settings/api-keys/github', {
    api_key: githubPat.value
  })
  githubPatStatus.value = {
    configured: true,
    masked: response.data.masked,
    source: 'settings'
  }
  // #211: backend auto-propagates the new PAT to running agents.
  // response.data.propagation = {total_running, updated, skipped, failed}
  githubPatPropagation.value = response.data.propagation || null
}
```

**Auto-Propagation on PAT Update (#211)** — When the global PAT is saved, the
backend reuses the agent-side `/api/credentials/read` + `/api/credentials/inject`
endpoints (via `services/github_pat_propagation_service.py`) to merge the new
`GITHUB_PAT` into each running agent's `.env` without a restart. Agents with a
per-agent PAT (#347) are skipped; agents whose `.env` never had `GITHUB_PAT`
are skipped. Delete PAT does NOT propagate. Partial failures are reported per
agent and never block the PAT save.

**Ops Settings Load** (Settings.vue lines 820-829)
```javascript
async function loadOpsSettings() {
  const response = await axios.get('/api/settings/ops/config', {
    headers: authStore.authHeader
  })
  sshAccessEnabled.value = response.data.ssh_access_enabled === 'true'
}
```

**SSH Access Toggle** (Settings.vue lines 831-855)
```javascript
async function toggleSshAccess() {
  const newValue = !sshAccessEnabled.value
  await axios.put('/api/settings/ops/config', {
    settings: {
      ssh_access_enabled: newValue ? 'true' : 'false'
    }
  }, { headers: authStore.authHeader })
  sshAccessEnabled.value = newValue
}
```

**Generate Default Avatars** (Settings.vue lines 1812-1828)
```javascript
async function generateDefaultAvatars() {
  generatingDefaultAvatars.value = true
  defaultAvatarResult.value = null
  const response = await axios.post('/api/agents/avatars/generate-defaults', {}, {
    headers: authStore.authHeader,
    timeout: 300000 // 5 min timeout for sequential generation
  })
  defaultAvatarResult.value = response.data
}
```

Response data shape: `{ message, generated, failed, skipped, agents: [name, ...], errors: [{agent, error}, ...] }`

The UI displays a colored result card: green (all succeeded), yellow (some failed), gray (all skipped). Lists generated agent names and any error details.

Backend endpoint details are documented in [agent-avatars.md](agent-avatars.md) under the "Generate Default Avatars" section.

---

## Backend Layer

### Router: settings.py

`src/backend/routers/settings.py`

| Endpoint | Lines | Handler | Description |
|----------|-------|---------|-------------|
| `GET /api/settings/api-keys` | 100-138 | `get_api_keys_status()` | Get masked status of all API keys |
| `PUT /api/settings/api-keys/github` | 263-295 | `update_github_pat()` | Store GitHub PAT |
| `DELETE /api/settings/api-keys/github` | 298-322 | `delete_github_pat()` | Remove GitHub PAT |
| `POST /api/settings/api-keys/github/test` | 325-422 | `test_github_pat()` | Validate PAT against GitHub API |
| `GET /api/settings/ops/config` | 584-616 | `get_ops_settings()` | Get all ops settings with defaults |
| `PUT /api/settings/ops/config` | 619-650 | `update_ops_settings()` | Update multiple ops settings |
| `POST /api/settings/ops/reset` | 653-678 | `reset_ops_settings()` | Delete all ops settings (revert to defaults) |
| `GET /api/settings` | 75-92 | `get_all_settings()` | List all system settings |
| `PUT /api/settings/{key}` | 537-556 | `update_setting()` | Update single setting by key |

### Authorization

All settings endpoints require admin role:

```python
# settings.py lines 69-72
def require_admin(current_user: User):
    """Verify user is an admin."""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
```

### Business Logic

**GitHub PAT Validation** (settings.py lines 325-422)

1. Validate format (must start with `ghp_` or `github_pat_`)
2. Call GitHub API `/user` endpoint to verify token
3. For fine-grained PATs: Test `/user/repos` for repository access
4. For classic PATs: Check `X-OAuth-Scopes` header for `repo` scope
5. Return username, token type, and permission status

```python
# Determine token type and check permissions
is_fine_grained = key.startswith('github_pat_')
if is_fine_grained:
    # Fine-grained PATs: Test actual permissions
    repos_response = await client.get(
        "https://api.github.com/user/repos",
        headers={"Authorization": f"Bearer {key}"},
        params={"per_page": 1}
    )
    has_repo_access = repos_response.status_code == 200
else:
    # Classic PAT: Check X-OAuth-Scopes header
    scope_header = response.headers.get("X-OAuth-Scopes", "")
    scopes = [s.strip() for s in scope_header.split(",")]
    has_repo_access = "repo" in scopes or "public_repo" in scopes
```

### Ops Settings Defaults

Defined in `src/backend/services/settings_service.py` (lines 23-33):

| Setting Key | Default | Description |
|-------------|---------|-------------|
| `ops_context_warning_threshold` | `"75"` | Context % to trigger warning |
| `ops_context_critical_threshold` | `"90"` | Context % to trigger reset |
| `ops_idle_timeout_minutes` | `"30"` | Minutes before stuck detection |
| `ops_cost_limit_daily_usd` | `"50.0"` | Daily cost limit (0 = unlimited) |
| `ops_max_execution_minutes` | `"10"` | Max chat execution time |
| `ops_alert_suppression_minutes` | `"15"` | Suppress duplicate alerts |
| `ops_log_retention_days` | `"7"` | Days to keep container logs |
| `ops_health_check_interval` | `"60"` | Seconds between health checks |
| `ssh_access_enabled` | `"false"` | Enable SSH access via MCP tool |

---

## Service Layer

### SettingsService

`src/backend/services/settings_service.py`

| Method | Lines | Description |
|--------|-------|-------------|
| `get_setting(key, default)` | 59-62 | Get setting from DB with fallback |
| `get_github_pat()` | 71-76 | Get GitHub PAT (DB or env var) |
| `get_ops_setting(key, as_type)` | 85-100 | Get ops setting with type conversion |

**Hierarchy for API Keys** (ent#435):
1. Encrypted database setting `<key>_encrypted` (AES-256-GCM envelope)
2. Legacy cleartext row `<key>` — **encrypted and deleted on sight**, then returned
3. Environment variable fallback
4. Empty string (not configured)

```python
# settings_service.py — every credential getter is a thin call on this
def get_github_pat(self) -> str:
    return self._resolve_secret_setting('github_pat', 'GITHUB_PAT')
```

Step 2 is not dead code that the one-shot migration made unreachable. A restored
pre-fix backup, a rollback-then-roll-forward, or a direct DB write can all put a
cleartext row back, and the read path is the only thing that would notice — so it
re-encrypts and DELETEs on sight, which makes cleartext *transient by
construction* rather than merely absent at this instant. Steady state pays
nothing: while the encrypted row exists the legacy key is never read.

An unreadable envelope (rotated key, corrupt row) degrades to the **env var**,
not to a legacy cleartext row — falling through to a stale plaintext value would
resurrect a credential the operator had replaced. Reads never raise (a 500 here
would break agent start); writes fail closed (no encryption key ⇒ refuse, never
silently store cleartext).

---

## Data Layer

### Database Schema

**system_settings Table** (`src/backend/database.py` lines 524-528)

```sql
CREATE TABLE IF NOT EXISTS system_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
```

### Database Operations

`src/backend/db/settings.py`

| Method | Lines | Description |
|--------|-------|-------------|
| `get_setting(key)` | 31-47 | Get setting by key |
| `get_setting_value(key, default)` | 49-58 | Get just the value |
| `set_setting(key, value)` | 60-83 | Upsert setting — **refuses a cleartext credential write** (ent#435): raises `SecretSettingWriteError` for a registered secret key or any credential-*shaped* key (`*_api_key`/`*_token`/`*_secret`/`*_pat`/`*_password`/`*_credentials`). The guard sits here, not only on the routes, because the generic `PUT /api/settings/{key}` catch-all can address any key and because `system_settings` has more than one writer (`client_portal/db.py` and two enterprise modules each carry their own upsert) |
| `delete_setting(key)` | 85-98 | Delete setting |
| `get_all_settings()` | 100-114 | List all settings |
| `get_settings_dict()` | 116-123 | Get as key-value dict |

**Upsert Pattern**:
```sql
INSERT OR REPLACE INTO system_settings (key, value, updated_at)
VALUES (?, ?, ?)
```

---

## Error Handling

| Error Case | HTTP Status | Message |
|------------|-------------|---------|
| Not admin | 403 | "Admin access required" |
| Invalid PAT format | 400 | "Invalid token format. GitHub PATs start with 'ghp_' or 'github_pat_'" |
| PAT authentication failed | 200 | `{"valid": false, "error": "Invalid Personal Access Token"}` |
| GitHub API error | 200 | `{"valid": false, "error": "GitHub API returned status {code}"}` |
| Request timeout | 200 | `{"valid": false, "error": "Request timed out. Please try again."}` |
| Invalid ops setting key | Ignored | Key ignored in update, returned in `ignored` array |
| Cleartext write to a credential key | 422 | "'{key}' holds a live credential and may not be stored in cleartext (ent#435, CWE-312)…" — names the `<key>_encrypted` destination and the route that writes it |
| Raw write to a `*_encrypted` key | 422 | The value must be an envelope this platform produced; a hand-pasted string would land as a row every reader then fails to decrypt (the #736 A2A-endpoints rationale) |

---

## Security Considerations

1. **Admin-Only Access**: All settings endpoints check `current_user.role == "admin"`
2. **API Key Masking**: Stored keys displayed as `...{last4chars}` via `mask_api_key()`
3. **Key Validation**: PAT format validated before storage (prefix check)
4. **No Key Logging**: API key values never appear in logs
5. **Encrypted at Rest** (ent#435): credential-bearing settings are persisted as
   AES-256-GCM envelopes under `<key>_encrypted` via
   `services/credential_encryption.py`, and the cleartext row is deleted —
   `anthropic_api_key`, `github_pat`, `google_api_key`, `slack_app_token`,
   `slack_client_secret`, `slack_signing_secret`.

   **This bullet used to read "stored in SQLite, not Redis (persistent, encrypted
   at rest if filesystem supports it)", which was the defect stated as a
   feature.** Filesystem-level encryption protects a powered-off disk and nothing
   else: every DB dump, backup, replica and snapshot still carried live
   third-party tokens in the clear — and backups are exactly the artifact most
   likely to travel — while any read path to the database (psql access, a future
   SQLi, a misconfigured BI or monitoring hook) yielded working credentials
   **without** needing `CREDENTIAL_ENCRYPTION_KEY` (CWE-312, ent#435).
   Application-level envelopes are what make the claim true.

   `slack_client_id` is deliberately excluded: an OAuth client_id is a public
   identifier that `slack_service.get_oauth_url` puts verbatim into the
   browser-visible authorize URL. It is a *reviewed* exemption recorded in
   `secret_settings.PUBLIC_CREDENTIAL_SHAPED_KEYS`, not an oversight.

   Encryption protects the database **going forward only** — historical backups
   still hold the plaintext, so upgrading installs must **rotate** the affected
   tokens: [`docs/migrations/SECRET_SETTINGS_ENCRYPTION_2026-08.md`](../../migrations/SECRET_SETTINGS_ENCRYPTION_2026-08.md).
6. **Cleartext writes are refused at the sink** (ent#435), so a new credential
   cannot re-enter through the generic `PUT /api/settings/{key}` catch-all — the
   same door #506, #1609, ent#12, #1644, ent#14 and ent#346 each found open. The
   heuristic half also catches the *next* credential-shaped key nobody has
   registered yet. Guarded against recurrence by
   `tests/unit/test_ent435_settings_sink_guard.py` (AST: every `system_settings`
   writer is gated or listed with the reason it cannot carry a credential).
7. **Environment Fallback**: Can use env vars instead of DB storage for sensitive deployments

---

## GitHub Templates Configuration (TMPL-001)

**Status**: Implemented (2026-03-04)

Admins can configure which GitHub repositories appear as agent templates via the Settings page, replacing the hardcoded `config.py` list.

**The `config.py` default list is EMPTY since #1931.** It shipped a pre-2026 repo set that no install had ever overridden, so every operator browsed the same dead catalog; curation is now an explicit operator act. Consequences, all deliberate:

- The `None` (no DB row) and `[]` (explicit empty) branches now produce the same **catalog**. They still differ in the `source: defaults | settings` badge — which the panel renders as *"No defaults configured"* rather than *"Using defaults"*, because badging an empty set as defaults is a lie (#1931).
- **Reset to Defaults** now reverts to an empty list. The button is already `:disabled` when `source === 'defaults'`, so the empty-state row no longer offers it as an action (it named one the user could not take — the dead end #1931 fixed one click away from the Library's own new empty state).
- Emptying the list removes a **browse** surface, never a **create** capability: `template: github:owner/repo` still resolves through `template_service.get_github_template`'s dynamic branch whether or not the repo is configured. Guarded by `tests/unit/test_1931_empty_github_defaults.py`.
- Side-effect: with no default repos, `GET /api/templates` makes **zero** outbound GitHub calls on a cold metadata cache (`_fetch_all_metadata([])` skips the `if to_fetch:` block entirely, so no ThreadPoolExecutor, no HTTP, no PAT read), where it previously blocked on up to six 10s-timeout requests.

`trinity-enterprise#14` (remote template registry) **has now repointed this seam** — see [template-registry.md](template-registry.md). The constant is kept, not deleted: it is the fail-open FLOOR under the registry, and it still backs the `None`-vs-`[]` fallback above.

### Data Flow

```
Settings.vue (GitHub Templates section)
    |
    GET /api/settings/github-templates
    |
    ├─ DB has config → {source: "settings", templates: [...]}
    └─ No DB config  → {source: "defaults", templates: [] since #1931}
    |
    PUT /api/settings/github-templates
    + Body: {templates: [{github_repo, display_name, description}, ...]}
    |
    └─ SettingsService.set_github_templates() → JSON in system_settings
    |
    DELETE /api/settings/github-templates
    └─ Revert to config.py defaults
```

### Backend Endpoints

| Endpoint | File:Line | Description |
|----------|-----------|-------------|
| `GET /api/settings/github-templates` | `settings.py:657` | Get configured templates (admin) |
| `PUT /api/settings/github-templates` | `settings.py:693` | Set templates list (admin) |
| `DELETE /api/settings/github-templates` | `settings.py:724` | Reset to defaults (admin) |

### Template List Resolution

```python
# routers/templates.py:17-26
@router.get("")
async def list_templates():
    db_templates = get_github_templates_from_db()
    templates = db_templates if db_templates is not None else list(ALL_GITHUB_TEMPLATES)
    templates.sort(...)
    return templates
```

**Priority**: DB-configured list takes full precedence. When set, only DB entries are shown. When deleted, falls back to `config.py` `ALL_GITHUB_TEMPLATES`.

### Service Layer

| Method | File:Line | Description |
|--------|-----------|-------------|
| `get_github_templates()` | `settings_service.py:118` | Get from DB (returns None if not configured) |
| `set_github_templates()` | `settings_service.py:136` | Save as JSON to system_settings |
| `delete_github_templates()` | `settings_service.py:139` | Delete DB config (revert to defaults) |
| `expand_github_template()` | `template_service.py:14` | Convert minimal entry to full template dict |
| `get_github_templates_from_db()` | `template_service.py:35` | Get expanded templates from DB |

### Frontend (Settings.vue)

GitHub Templates section (lines 793-918) with:
- Template list with edit/delete per entry
- Add form (owner/repo input + optional display name)
- "Using defaults" / "Custom config" badge
- "Reset to Defaults" button

### Storage

Templates stored as JSON in `system_settings` table under key `github_templates`:
```json
[
  {"github_repo": "owner/repo", "display_name": "Name", "description": "Desc"},
  ...
]
```

---

## Template Registry (TMPL-002, trinity-enterprise#14)

**Status**: Implemented (2026-08-04)

The runtime SOURCE for the GitHub half of the catalog — a `registry.yaml` fetched
over HTTPS, so curating an install's starter templates is a vendor file edit
rather than a Trinity release. Precedence: **admin DB override (TMPL-001 above)
→ remote registry → bundled `DEFAULT_GITHUB_TEMPLATE_REPOS`**, so a curated
install is unaffected and never even issues the fetch.

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/settings/template-registry` | `assert_admin`. Config + a live `status` block |
| `PUT` | `/api/settings/template-registry` | `assert_admin` **+ `reject_agent_principal`**. Partial `{url?, enabled?}`; SSRF-validated; audit-logged |
| `DELETE` | `/api/settings/template-registry` | `assert_admin` **+ `reject_agent_principal`**. Reverts to the config default; audit-logged |

Registered **before** the `/{key}` catch-all (Invariant #4); all four registry
keys 422 on the generic `PUT` **and** `DELETE /{key}`. UI:
`components/settings/TemplateRegistryPanel.vue` in the **Agents** tab.

Three things a reader of this file needs, with the full argument in the flow doc:

- **The human gate is load-bearing.** `assert_admin` answers *what role*, never
  *is this a human* — an agent-scoped MCP key resolves to its owner carrying the
  owner's role (trinity-ops-agent#232), and here that would let an agent repoint
  the platform's template registry at a URL it controls.
- **The status block is part of the contract.** Fail-open makes a broken registry
  look exactly like a working empty one from the catalog, so this panel is the
  only place an operator can see it.
- **Two off-switches, asymmetric.** `TEMPLATE_REGISTRY_ENABLED` (env) is a hard
  kill switch no DB row can override; `template_registry_enabled` is the admin
  toggle, default-on. Deliberately not `_resolve_bool_flag` — its env leg is
  opt-in only, so `default=True` would have swallowed the `false` and shipped an
  inert switch (#1039 class).

**Full flow**: [template-registry.md](template-registry.md) — the document schema,
the tolerant reader, the fail-open matrix, the cache semantics, the SSRF gate, and
the `fork_to_own` fail-closed fix.

---

## Related Flows

- **Downstream**: [template-registry.md](template-registry.md) - Remote registry for the GitHub half of the catalog (TMPL-002)
- **Upstream**: [first-time-setup.md](first-time-setup.md) - Initial admin password and API key configuration
- **Downstream**: [template-processing.md](template-processing.md) - Uses GitHub PAT for private repo cloning
- **Downstream**: [library-page.md](library-page.md) - Library page (formerly Templates, ent#263) uses configured list
- **Related**: [internal-system-agent.md](internal-system-agent.md) - Ops settings affect fleet health checks
- **Related**: [ssh-access.md](ssh-access.md) - `ssh_access_enabled` setting controls MCP tool availability
- **Related**: [agent-avatars.md](agent-avatars.md) - Default avatar generation endpoint and image generation pipeline (AVATAR-003)

---

## Testing

### Prerequisites
- Trinity platform running locally
- Admin user account

### Test Steps

**SET-004: Set GitHub PAT**

| Step | Action | Expected | Verify |
|------|--------|----------|--------|
| 1 | Navigate to Settings page | Page loads with API Keys section | Check URL is `/settings` |
| 2 | Enter valid GitHub PAT in input | Input accepts value | Value appears (masked by default) |
| 3 | Click "Save" button | Save spinner, then success message | Green "Settings saved" toast |
| 4 | Refresh page | Status shows "Configured (from settings)" | Green checkmark visible |

**SET-005: Test GitHub PAT Permissions**

| Step | Action | Expected | Verify |
|------|--------|----------|--------|
| 1 | Enter GitHub PAT | Input accepts value | Value visible if show toggle on |
| 2 | Click "Test" button | Spinner while testing | API call to GitHub visible in Network tab |
| 3a | Valid PAT with repo scope | Green checkmark, username displayed | Message shows "Valid! GitHub user: {username}. Has repo scope" |
| 3b | Valid PAT without repo scope | Yellow warning | Message shows "Missing repo scope" |
| 3c | Invalid PAT | Red X | Message shows "Invalid Personal Access Token" |

**SET-010: View Ops Configuration**

| Step | Action | Expected | Verify |
|------|--------|----------|--------|
| 1 | Navigate to Settings | SSH Access toggle visible | Toggle shows current state |
| 2 | Check API response | `GET /api/settings/ops/config` returns all ops settings | Response includes `ssh_access_enabled` |

**SET-011: Update Ops Settings**

| Step | Action | Expected | Verify |
|------|--------|----------|--------|
| 1 | Toggle SSH Access switch | Toggle changes state | Success message appears |
| 2 | Verify API call | `PUT /api/settings/ops/config` sent | Body contains `{"settings": {"ssh_access_enabled": "true"}}` |
| 3 | Refresh page | Toggle retains new state | State persisted |

**SET-012: Reset Ops to Defaults**

| Step | Action | Expected | Verify |
|------|--------|----------|--------|
| 1 | Call reset API directly | `POST /api/settings/ops/reset` | Response shows `{"success": true, "reset": [...]}` |
| 2 | Verify ops settings | `GET /api/settings/ops/config` | All values show `is_default: true` |

**AVATAR-003: Generate Default Avatars**

| Step | Action | Expected | Verify |
|------|--------|----------|--------|
| 1 | Navigate to Settings page | "Default Avatars" card visible after Skills Library | Card has "Generate Default Avatars" button |
| 2 | Click "Generate Default Avatars" | Button shows spinner and "Generating..." text | Button is disabled while generating |
| 3a | Agents without avatars exist | Green result card showing generated count and agent names | `generated > 0` in response |
| 3b | All agents already have avatars | Gray result card showing "all agents already have avatars" | `skipped > 0`, `generated === 0` |
| 4 | Non-admin user | Endpoint returns 403 | "Admin access required" or "Not enough permissions" |

### Edge Cases

1. **Empty PAT**: Save button should be disabled
2. **Network Error During Test**: Error message displayed, no crash
3. **Concurrent Admin Updates**: Last write wins (no locking)
4. **Invalid Setting Key in Update**: Key ignored, warning returned

### Status

| Test Case | Status |
|-----------|--------|
| SET-004 | Implemented |
| SET-005 | Implemented |
| SET-010 | Implemented |
| SET-011 | Implemented |
| SET-012 | Implemented |
| AVATAR-003 | Implemented |

---

## Revision History

| Date | Change |
|------|--------|
| 2026-08-04 | **TMPL-002 Remote Template Registry (trinity-enterprise#14)**: new section + endpoint table for `GET/PUT/DELETE /api/settings/template-registry`; the TMPL-001 section's *"ent#14 will repoint this seam"* sentence is now past tense and names the constant as the fail-open floor. Full vertical in the new [template-registry.md](template-registry.md). |
| 2026-03-08 | **AVATAR-003 Default Avatars**: Added Default Avatars card documentation. New UI section (lines 1054-1092), state refs, generateDefaultAvatars() method, test case. Backend endpoint documented in agent-avatars.md. |
| 2026-03-04 | **TMPL-001 GitHub Templates Configuration**: Added admin UI and API endpoints for configuring which GitHub repos appear as agent templates. New section with data flow, endpoints, service layer, and storage details. Updated overview, entry points, related flows. |
| 2026-01-13 | Initial documentation for Platform Settings feature flow |
