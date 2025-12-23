# Feature: First-Time Setup

## Overview
First-time setup wizard for admin password and API key configuration. On fresh install, users are redirected to `/setup` to set an admin password before accessing the platform. After login, admins can configure the Anthropic API key in Settings.

## User Story
As a platform administrator deploying Trinity for the first time, I want to be guided through initial configuration so that the platform is secured with a proper password and agents have access to the required API key.

## Requirements Reference
- **Requirement 11.4** - First-Time Setup Wizard (Phase 12.3)
- Password: bcrypt-hashed, minimum 8 characters
- API key: Stored in `system_settings` table, validated against Anthropic API

---

## Entry Points

### First Launch Flow
- **UI**: Any route visited on fresh install triggers redirect to `/setup`
- **API**: `GET /api/setup/status` (no auth) - Check if setup completed

### API Key Configuration Flow
- **UI**: `src/frontend/src/views/Settings.vue` - API Keys section
- **API**: `PUT /api/settings/api-keys/anthropic` (admin-only)

---

## Flow 1: First Launch Setup

### Frontend Layer

#### Router Guard
**File**: `/Users/eugene/Dropbox/trinity/trinity/src/frontend/src/router/index.js:88-143`

```javascript
// Cache for setup status check
let setupStatusCache = null
let setupStatusCacheTime = 0
const SETUP_CACHE_DURATION = 5000 // 5 seconds

async function checkSetupStatus() {
  const now = Date.now()
  if (setupStatusCache !== null && (now - setupStatusCacheTime) < SETUP_CACHE_DURATION) {
    return setupStatusCache
  }

  const response = await fetch('/api/setup/status')
  const data = await response.json()
  setupStatusCache = data.setup_completed
  setupStatusCacheTime = now
  return setupStatusCache
}

// Navigation guard
router.beforeEach(async (to, from, next) => {
  if (!to.meta.isSetup) {
    const setupCompleted = await checkSetupStatus()
    if (!setupCompleted) {
      next('/setup')  // Redirect to setup
      return
    }
  }
  // ... rest of guard
})
```

#### Setup Route
**File**: `/Users/eugene/Dropbox/trinity/trinity/src/frontend/src/router/index.js:6-10`

```javascript
{
  path: '/setup',
  name: 'Setup',
  component: () => import('../views/SetupPassword.vue'),
  meta: { requiresAuth: false, isSetup: true }
}
```

#### SetupPassword Component
**File**: `/Users/eugene/Dropbox/trinity/trinity/src/frontend/src/views/SetupPassword.vue`

**Key Features**:
- Password + Confirm Password fields with visibility toggle
- Password strength indicator (Weak/Fair/Good/Strong/Excellent)
- Client-side validation: min 8 chars, passwords must match
- Submits to `/api/setup/admin-password`

```javascript
// Submit handler (line 209-236)
async function handleSubmit() {
  if (!isValid.value) return

  loading.value = true
  try {
    await axios.post('/api/setup/admin-password', {
      password: password.value,
      confirm_password: confirmPassword.value
    })

    // Clear cache so router knows setup is done
    clearSetupCache()

    // Redirect to login
    router.push('/login')
  } catch (e) {
    if (e.response?.status === 403) {
      error.value = 'Setup has already been completed.'
      setTimeout(() => router.push('/login'), 2000)
    }
  }
}
```

### Backend Layer

#### Setup Router
**File**: `/Users/eugene/Dropbox/trinity/trinity/src/backend/routers/setup.py`

**Router Registration** in `main.py:46`:
```python
from routers.setup import router as setup_router
# Line 175 (approx): app.include_router(setup_router)
```

#### GET /api/setup/status (line 23-35)
```python
@router.get("/status")
async def get_setup_status():
    """Check if initial setup is complete. No auth required."""
    setup_completed = db.get_setting_value('setup_completed', 'false') == 'true'
    return {"setup_completed": setup_completed}
```

#### POST /api/setup/admin-password (line 38-101)
```python
@router.post("/admin-password")
async def set_admin_password(data: SetAdminPasswordRequest, request: Request):
    # 1. Check setup not already completed
    if db.get_setting_value('setup_completed', 'false') == 'true':
        raise HTTPException(status_code=403, detail="Setup already completed")

    # 2. Validate password
    if len(data.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    if data.password != data.confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match")

    # 3. Hash password with bcrypt
    hashed_password = hash_password(data.password)

    # 4. Update admin user's password
    db.update_user_password('admin', hashed_password)

    # 5. Mark setup as completed
    db.set_setting('setup_completed', 'true')

    # 6. Audit log
    await log_audit_event(event_type="setup", action="admin_password", ...)

    return {"success": True}
```

#### Password Hashing
**File**: `/Users/eugene/Dropbox/trinity/trinity/src/backend/dependencies.py:16-38`

```python
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(password: str) -> str:
    """Hash a password using bcrypt."""
    return pwd_context.hash(password)

def verify_password(plain_password: str, stored_password: str) -> bool:
    """Verify password against stored hash.
    For backward compatibility, also checks plaintext passwords.
    """
    try:
        if pwd_context.verify(plain_password, stored_password):
            return True
    except Exception:
        pass
    # Fall back to plaintext comparison for legacy passwords
    return plain_password == stored_password
```

### Database Layer

#### User Password Update
**File**: `/Users/eugene/Dropbox/trinity/trinity/src/backend/db/users.py:129-146`

```python
def update_user_password(self, username: str, hashed_password: str) -> bool:
    """Update user's password hash."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        now = datetime.utcnow().isoformat()
        cursor.execute("""
            UPDATE users SET password_hash = ?, updated_at = ? WHERE username = ?
        """, (hashed_password, now, username))
        conn.commit()
        return cursor.rowcount > 0
```

#### Settings Storage
**File**: `/Users/eugene/Dropbox/trinity/trinity/src/backend/db/settings.py:60-83`

```python
def set_setting(self, key: str, value: str) -> SystemSetting:
    """Set a system setting value (upsert)."""
    now = datetime.utcnow().isoformat()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO system_settings (key, value, updated_at)
            VALUES (?, ?, ?)
        """, (key, value, now))
        conn.commit()
```

#### Database Table
**File**: `/Users/eugene/Dropbox/trinity/trinity/src/backend/database.py:418-425`

```sql
CREATE TABLE IF NOT EXISTS system_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
```

**Settings Used**:
| Key | Value | Purpose |
|-----|-------|---------|
| `setup_completed` | `"true"` / `"false"` | Gate setup endpoint, redirect logic |
| `anthropic_api_key` | `"sk-ant-..."` | API key for Claude |

### Login Block During Setup

**File**: `/Users/eugene/Dropbox/trinity/trinity/src/backend/routers/auth.py:24-27, 53-66`

```python
def is_setup_completed() -> bool:
    """Check if initial setup is completed."""
    return db.get_setting_value('setup_completed', 'false') == 'true'

@router.post("/token", response_model=Token)
async def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends()):
    # Block login if setup is not completed
    if not is_setup_completed():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="setup_required"
        )
    # ... rest of login logic
```

**Auth mode endpoint also reports setup status**:
```python
@router.get("/api/auth/mode")
async def get_auth_mode():
    return {
        "dev_mode_enabled": DEV_MODE_ENABLED,
        "auth0_configured": bool(AUTH0_DOMAIN),
        "allowed_domain": AUTH0_ALLOWED_DOMAIN,
        "setup_completed": is_setup_completed()
    }
```

---

## Flow 2: API Key Configuration

### Frontend Layer

#### Settings Page
**File**: `/Users/eugene/Dropbox/trinity/trinity/src/frontend/src/views/Settings.vue`

**API Key Section** (lines 23-127):
- Input field with show/hide toggle
- Test button - calls `/api/settings/api-keys/anthropic/test`
- Save button - calls `PUT /api/settings/api-keys/anthropic`
- Status indicator showing: Not configured / Configured (from settings/env)

**Key Methods** (lines 313-374):
```javascript
async function loadApiKeyStatus() {
  const response = await axios.get('/api/settings/api-keys')
  anthropicKeyStatus.value = response.data.anthropic || { configured: false }
}

async function testApiKey() {
  const response = await axios.post('/api/settings/api-keys/anthropic/test', {
    api_key: anthropicKey.value
  })
  apiKeyTestResult.value = response.data.valid
}

async function saveApiKey() {
  const response = await axios.put('/api/settings/api-keys/anthropic', {
    api_key: anthropicKey.value
  })
  anthropicKeyStatus.value = {
    configured: true,
    masked: response.data.masked,
    source: 'settings'
  }
}
```

### Backend Layer

#### API Keys Endpoints
**File**: `/Users/eugene/Dropbox/trinity/trinity/src/backend/routers/settings.py:361-588`

**GET /api/settings/api-keys** (line 394-430):
```python
@router.get("/api-keys")
async def get_api_keys_status(current_user: User = Depends(get_current_user)):
    """Get status of configured API keys. Admin-only."""
    require_admin(current_user)

    anthropic_key = get_anthropic_api_key()
    anthropic_configured = bool(anthropic_key)
    key_from_settings = bool(db.get_setting_value('anthropic_api_key', None))

    return {
        "anthropic": {
            "configured": anthropic_configured,
            "masked": mask_api_key(anthropic_key) if anthropic_configured else None,
            "source": "settings" if key_from_settings else ("env" if anthropic_configured else None)
        }
    }
```

**PUT /api/settings/api-keys/anthropic** (line 433-483):
```python
@router.put("/api-keys/anthropic")
async def update_anthropic_key(body: ApiKeyUpdate, current_user: User = Depends(get_current_user)):
    require_admin(current_user)

    key = body.api_key.strip()
    if not key.startswith('sk-ant-'):
        raise HTTPException(status_code=400, detail="Invalid API key format")

    db.set_setting('anthropic_api_key', key)
    return {"success": True, "masked": mask_api_key(key)}
```

**DELETE /api/settings/api-keys/anthropic** (line 486-519):
```python
@router.delete("/api-keys/anthropic")
async def delete_anthropic_key(current_user: User = Depends(get_current_user)):
    require_admin(current_user)

    deleted = db.delete_setting('anthropic_api_key')
    env_key = os.getenv('ANTHROPIC_API_KEY', '')

    return {
        "success": True,
        "deleted": deleted,
        "fallback_configured": bool(env_key)
    }
```

**POST /api/settings/api-keys/anthropic/test** (line 522-587):
```python
@router.post("/api-keys/anthropic/test")
async def test_anthropic_key(body: ApiKeyTest, current_user: User = Depends(get_current_user)):
    require_admin(current_user)

    key = body.api_key.strip()
    if not key.startswith('sk-ant-'):
        return {"valid": False, "error": "Invalid format"}

    # Make lightweight API call to validate
    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://api.anthropic.com/v1/models",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            timeout=10.0
        )

        if response.status_code == 200:
            return {"valid": True}
        elif response.status_code == 401:
            return {"valid": False, "error": "Invalid API key"}
```

#### Key Retrieval Function
**File**: `/Users/eugene/Dropbox/trinity/trinity/src/backend/routers/settings.py:379-384`

```python
def get_anthropic_api_key() -> str:
    """Get Anthropic API key from settings, fallback to env var."""
    key = db.get_setting_value('anthropic_api_key', None)
    if key:
        return key
    return os.getenv('ANTHROPIC_API_KEY', '')
```

---

## Flow 3: Agent Uses Stored API Key

### Agent Creation
**File**: `/Users/eugene/Dropbox/trinity/trinity/src/backend/routers/agents.py:508-512`

```python
env_vars = {
    'AGENT_NAME': config.name,
    'AGENT_TYPE': config.type,
    'ANTHROPIC_API_KEY': get_anthropic_api_key(),  # Uses settings value OR env fallback
    # ...
}
```

### System Agent Service
**File**: `/Users/eugene/Dropbox/trinity/trinity/src/backend/services/system_agent_service.py:24, 180`

```python
from routers.settings import get_anthropic_api_key

# During system agent container creation:
env_vars = {
    # ...
    'ANTHROPIC_API_KEY': get_anthropic_api_key(),
}
```

---

## Side Effects

### Audit Logging

| Event | Type | Action | Details |
|-------|------|--------|---------|
| Password set | `setup` | `admin_password` | `result: success` |
| Setup blocked | `setup` | `admin_password` | `result: blocked, reason: already completed` |
| API key read | `system_settings` | `read_api_keys` | - |
| API key update | `system_settings` | `update_anthropic_key` | `key_masked: ...xxxx` |
| API key delete | `system_settings` | `delete_anthropic_key` | `deleted: true/false` |
| API key test | `system_settings` | `test_anthropic_key` | `valid: true/false` |

---

## Error Handling

| Error Case | HTTP Status | Message | Handling |
|------------|-------------|---------|----------|
| Setup already completed | 403 | "Setup already completed" | Frontend redirects to /login after 2s |
| Password too short | 400 | "Password must be at least 8 characters" | Form validation |
| Passwords don't match | 400 | "Passwords do not match" | Form validation |
| Invalid API key format | 400 | "Invalid API key format. Keys start with 'sk-ant-'" | Inline error |
| API key invalid | N/A | `{valid: false, error: "..."}` | Test result display |
| Not admin | 403 | "Admin access required" | Redirect to dashboard |
| Login blocked (no setup) | 403 | "setup_required" | Frontend checks and redirects |

---

## Security Considerations

1. **Password Security**:
   - Bcrypt hashing with auto-configured work factor
   - Backward compatibility with plaintext (legacy migration)
   - Minimum 8 character requirement
   - Setup endpoint only works ONCE

2. **API Key Security**:
   - Never exposed in full (masked in responses)
   - Format validation (`sk-ant-` prefix)
   - Admin-only access to all API key endpoints
   - Fallback to environment variable if not in settings

3. **Setup Endpoint Protection**:
   - No auth required (must work on fresh install)
   - Self-disabling after first use via `setup_completed` flag
   - Audit logged even on blocked attempts

4. **Login Block**:
   - Login endpoint returns 403 with `setup_required` until admin password set
   - Prevents access with default password

---

## Testing

### Prerequisites
- Fresh database (delete `~/trinity-data/trinity.db`) or reset `setup_completed` setting
- Backend and frontend running

### Test Steps

**Flow 1: First-Time Setup**

1. **Delete existing setup flag**
   ```sql
   DELETE FROM system_settings WHERE key = 'setup_completed';
   ```

2. **Visit any page** (e.g., `http://localhost:3000/`)
   - **Expected**: Redirect to `/setup`
   - **Verify**: URL shows `/setup`, setup form displayed

3. **Try weak password** (less than 8 chars)
   - **Expected**: Submit button disabled
   - **Verify**: Strength indicator shows "Weak"

4. **Enter mismatched passwords**
   - **Expected**: "Passwords do not match" indicator
   - **Verify**: Submit button disabled

5. **Enter valid password** (8+ chars, matching)
   - **Expected**: Submit enabled, strength indicator updates
   - **Verify**: Click "Set Password & Continue"

6. **After successful setup**
   - **Expected**: Redirect to `/login`
   - **Verify**: Can log in with `admin` / new password

7. **Try accessing /setup again**
   - **Expected**: Redirect to `/login` (setup already done)

**Flow 2: API Key Configuration**

1. **Login as admin**, navigate to Settings

2. **Check initial status**
   - **Expected**: "Not configured" warning if no env var

3. **Enter invalid key format** (e.g., "test123")
   - Click Test
   - **Expected**: "Invalid format" error

4. **Enter valid format but invalid key** (e.g., "sk-ant-fake123")
   - Click Test
   - **Expected**: "Invalid API key" error

5. **Enter valid API key**
   - Click Test
   - **Expected**: "API key is valid!" success

6. **Save the key**
   - Click Save
   - **Expected**: Status changes to "Configured (from settings)"

7. **Create an agent**
   - **Verify**: Agent can use Claude (API key injected)

### Edge Cases

- **Multiple setup attempts**: Second POST to `/api/setup/admin-password` returns 403
- **Env fallback**: Delete key from settings, env var should be used
- **Non-admin access**: Settings page returns 403 for non-admin users

### Cleanup

```sql
-- Reset to fresh state
DELETE FROM system_settings WHERE key IN ('setup_completed', 'anthropic_api_key');
UPDATE users SET password_hash = 'changeme' WHERE username = 'admin';
```

### Status
- First-Time Setup: **Not Tested** (newly implemented)
- API Key Configuration: **Not Tested** (newly implemented)

---

## Related Flows

### Upstream
- None (this is the entry point for fresh installations)

### Downstream
- **Agent Lifecycle**: Uses stored API key via `get_anthropic_api_key()`
- **System Agent**: Uses stored API key for trinity-system operations
- **Authentication**: Login blocked until setup completed
