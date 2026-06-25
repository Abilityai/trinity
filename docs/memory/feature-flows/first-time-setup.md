# Feature: First-Time Setup

## Overview
First-time setup wizard for admin password and API key configuration. On fresh install, users are redirected to `/setup` to set an admin password before accessing the platform. After login, admins can configure the Anthropic API key in Settings.

## User Story
As a platform administrator deploying Trinity for the first time, I want to be guided through initial configuration so that the platform is secured with a proper password and agents have access to the required API key.

## Requirements Reference
- **Requirement 11.4** - First-Time Setup Wizard (Phase 12.3)
- **#189** - Password complexity requirements (OWASP ASVS 2.1)
- Password: bcrypt-hashed, OWASP ASVS 2.1 complexity (12+ chars, uppercase, lowercase, digit, special, not common)
- Validation: `src/backend/utils/password_validation.py` — reusable module with `validate_password_strength()`
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
**File**: `src/frontend/src/router/index.js:165-220`

```javascript
// Cache for setup status check (avoid repeated API calls)
let setupStatusCache = null
let setupStatusCacheTime = 0
const SETUP_CACHE_DURATION = 5000 // 5 seconds

async function checkSetupStatus() {
  const now = Date.now()
  // Use cached value if recent
  if (setupStatusCache !== null && (now - setupStatusCacheTime) < SETUP_CACHE_DURATION) {
    return setupStatusCache
  }

  try {
    const response = await fetch('/api/setup/status')
    const data = await response.json()
    setupStatusCache = data.setup_completed
    setupStatusCacheTime = now
    return setupStatusCache
  } catch (e) {
    console.error('Failed to check setup status:', e)
    // Assume setup is completed if check fails (don't block access)
    return true
  }
}

// Navigation guard
router.beforeEach(async (to, from, next) => {
  // ... auth initialization check

  // Check setup status for login and protected routes
  if (!to.meta.isSetup) {
    const setupCompleted = await checkSetupStatus()

    // If setup not completed, redirect to setup page
    if (!setupCompleted) {
      // Allow access to public routes that don't need setup
      if (to.path === '/chat' || to.path.startsWith('/chat/')) {
        next()
        return
      }
      next('/setup')
      return
    }

    // If setup completed and trying to access setup page, redirect to login
    if (to.path === '/setup') {
      next('/login')
      return
    }
  }
  // ... rest of guard
})
```

#### Setup Route
**File**: `src/frontend/src/router/index.js:6-10`

```javascript
{
  path: '/setup',
  name: 'Setup',
  component: () => import('../views/SetupPassword.vue'),
  meta: { requiresAuth: false, isSetup: true }
}
```

#### Clear Setup Cache Export
**File**: `src/frontend/src/router/index.js:242-245`

```javascript
// Clear setup cache on successful setup
export function clearSetupCache() {
  setupStatusCache = null
  setupStatusCacheTime = 0
}
```

#### SetupPassword Component
**File**: `src/frontend/src/views/SetupPassword.vue`

A welcoming, **single-screen first-run page** (trinity-enterprise#49): a dark,
branded hero with an animated **orbiting fleet constellation** (the Trinity mark
at the core, agent nodes orbiting on three rings) on the left, and the setup form
on the right (stacks to one column on mobile; the constellation freezes to a
static composition under `prefers-reduced-motion`).

**Key Features**:
- **No setup-token field** — removed in #49 (the old log-copied token is gone).
- **Admin email — required** (becomes the sign-in identity), validated client-side
  against the same shape regex as the backend.
- Password + Confirm Password fields with visibility toggle.
- Password strength indicator + per-requirement checklist + match indicator.
- Optional **Company / organization** field and **updates opt-in** checkbox.
- Field order: **email → password (+confirm) → company → updates opt-in**.
- Submits `{email, password, confirm_password, company, consent_updates}` to
  `POST /api/setup/admin-password` (no `setup_token`).

```javascript
// Submit handler
async function handleSubmit() {
  if (!isValid.value) return

  loading.value = true
  error.value = null

  try {
    await axios.post('/api/setup/admin-password', {
      email: email.value.trim(),
      password: password.value,
      confirm_password: confirmPassword.value,
      company: company.value.trim() || null,
      consent_updates: consentUpdates.value,
    })

    clearSetupCache()
    router.push('/login')
  } catch (e) {
    if (e.response?.status === 403) {
      // Setup already completed — endpoint self-disabled; go to login.
      error.value = 'Setup has already been completed.'
      setTimeout(() => router.push('/login'), 2000)
    } else {
      error.value = e.response?.data?.detail || 'Failed to create the admin account. Please try again.'
    }
  } finally {
    loading.value = false
  }
}
```

**Validation Logic** (email required; full OWASP password requirements, not just length):
```javascript
const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s.]+$/
const isValid = computed(() =>
  EMAIL_RE.test(email.value.trim()) &&
  passwordRequirements.value.every(r => r.met) &&
  passwordsMatch.value
)
```

### Backend Layer

#### Setup Router
**File**: `src/backend/routers/setup.py`

**Router Registration** in `main.py:46, 294`:
```python
from routers.setup import router as setup_router
# ...
app.include_router(setup_router)
```

**Setup token — REMOVED (trinity-enterprise#49):** the earlier flow required a
one-time token (shared across uvicorn workers via Redis, #1165) copied from the
server logs into the wizard, to guard the first-run window against admin hijack
(SEC #177). It has been removed to streamline the common self-hosted, single-
operator bring-up. Consequences:
- No `setup_token` field on the request, no `ensure_setup_token`/`clear_setup_token`
  machinery, and **no Redis dependency for setup** (the admin write goes straight
  to SQLite). The `_get_redis`/`_candidate_token`/`_SETUP_TOKEN_KEY` code is gone.
- The startup token-emission block in `main.py` is replaced by a plain
  "first-time setup required" log line (still `logger.warning`, preserving the
  #858 flush guarantee — see below).
- The tradeoff is an explicit **operator responsibility**: an internet-reachable
  instance must be kept behind a tunnel/VPN until setup completes
  (`docs/DEPLOYMENT.md` → Security Recommendations). The endpoint still
  self-disables after the first success, so the exposure is the pre-setup window
  only.

**Startup first-run signal** (in `main.py` lifespan handler, immediately after
`setup_logging()`):
```python
if _db.get_setting_value('setup_completed', 'false') != 'true':
    logger.warning(
        "TRINITY FIRST-TIME SETUP REQUIRED — open the Trinity UI to create the "
        "admin account. On an internet-reachable instance, keep it behind a "
        "tunnel/VPN until setup completes (docs/DEPLOYMENT.md → Security)."
    )
```

> **Why `logger.warning`, not `print` (#858):** the lifespan runs under uvicorn with
> stdout connected to a Docker log pipe (not a TTY). Without `ENV PYTHONUNBUFFERED=1`
> in `docker/backend/Dockerfile`, CPython block-buffers `print()` (~8KB) and the line
> never reaches `docker logs`. The logging `StreamHandler` flushes after every record,
> so the signal is delivered regardless and flows through the structured JSON logger / Vector.

**Request Model**:
```python
class SetAdminPasswordRequest(BaseModel):
    """Request body for creating the admin account at first-time setup."""
    password: str = Field(..., max_length=128)
    confirm_password: str = Field(..., max_length=128)
    email: str = Field(..., max_length=254)        # REQUIRED — admin sign-in identity (#49)
    company: Optional[str] = Field(None, max_length=200)   # optional operator profile
    name: Optional[str] = Field(None, max_length=200)
    role: Optional[str] = Field(None, max_length=200)
    use_case: Optional[str] = Field(None, max_length=500)
    consent_updates: bool = False                  # opt-in to hosted intake
```

#### GET /api/setup/status
```python
@router.get("/status")
async def get_setup_status():
    """
    Check if initial setup is complete. No auth required.

    Returns:
        - setup_completed: Whether the admin account has been created
        - setup_available: Always true now the Redis-backed setup token is gone
          (#49); kept for backward compatibility with older frontends.
    """
    setup_completed = db.get_setting_value('setup_completed', 'false') == 'true'
    return {"setup_completed": setup_completed, "setup_available": True}
```

#### POST /api/setup/admin-password
```python
@router.post("/admin-password")
async def set_admin_password(data, request, background_tasks):
    """
    Create the admin account on first launch. No auth required, only works once.
    No setup token (#49) — on an internet-reachable instance the operator restricts
    network access (tunnel/VPN) until setup completes (docs/DEPLOYMENT.md → Security).
    """
    # 1. Check setup not already completed
    if db.get_setting_value('setup_completed', 'false') == 'true':
        raise HTTPException(status_code=403, detail="Setup already completed...")

    # 2. Validate password complexity (OWASP ASVS 2.1) — generic message on this
    #    unauthenticated endpoint (don't reveal which rule failed)
    if validate_password_strength(data.password):
        raise HTTPException(status_code=400, detail=PASSWORD_REQUIREMENTS_MESSAGE)

    # 3. Validate passwords match
    if data.password != data.confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match")

    # 4. Email is REQUIRED (#49) — validate shape before any write (missing → 422
    #    at the model layer; blank/typo → 400 here, so setup never half-completes)
    normalized_email = (data.email or "").strip().lower()
    if not normalized_email or not _EMAIL_RE.match(normalized_email):
        raise HTTPException(status_code=400, detail="A valid admin email is required")

    # 5. Hash + upsert admin password; bind the email as the sign-in identity
    db.update_user_password('admin', hash_password(data.password))
    try:
        db.update_user('admin', {"email": normalized_email})
        email_registered = True
    except Exception:           # never block setup on a profile write
        email_registered = False

    # 6. Mark setup completed
    db.set_setting('setup_completed', 'true')

    # 7. Operator intake (trinity-enterprise#38): only on affirmative consent,
    #    scheduled as a BackgroundTask so it runs after the response
    if data.consent_updates:
        background_tasks.add_task(submit_operator_intake, email=normalized_email, ...)

    return {"success": True, "email_registered": email_registered}
```

#### Password Hashing
**File**: `src/backend/dependencies.py:15-34`

```python
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")


def hash_password(password: str) -> str:
    """Hash a password using bcrypt."""
    return pwd_context.hash(password)


def verify_password(plain_password: str, stored_password: str) -> bool:
    """Verify password against stored bcrypt hash.

    Security: Plaintext fallback removed (M-003, 2026-02-23).
    All passwords must be bcrypt hashed.
    """
    try:
        return pwd_context.verify(plain_password, stored_password)
    except Exception:
        # Invalid hash format - reject
        return False
```

**Security Note (M-003)**: The plaintext password fallback was removed on 2026-02-23. All passwords must be stored as bcrypt hashes. Invalid hash formats are rejected, returning `False` for authentication.

### Database Layer

#### User Password Update (Upsert Pattern)
**File**: `src/backend/db/users.py:129-162`

```python
def update_user_password(self, username: str, hashed_password: str) -> bool:
    """Update user's password hash, creating the user if it doesn't exist.

    For the admin user during first-time setup, this will create the user
    if it doesn't exist yet.

    Args:
        username: The username to update
        hashed_password: The bcrypt-hashed password

    Returns:
        True if the user was updated or created successfully
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        now = datetime.utcnow().isoformat()

        # Try to update existing user
        cursor.execute("""
            UPDATE users SET password_hash = ?, updated_at = ? WHERE username = ?
        """, (hashed_password, now, username))
        conn.commit()

        if cursor.rowcount > 0:
            return True

        # User doesn't exist - create it (for admin user during first-time setup)
        cursor.execute("""
            INSERT INTO users (username, password_hash, role, email, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (username, hashed_password, 'admin', username, now, now))
        conn.commit()
        return cursor.rowcount > 0
```

**Upsert Logic**:
1. First attempts UPDATE on existing user
2. If UPDATE affects 0 rows (user doesn't exist), performs INSERT
3. New admin users are created with role='admin' and username as email
4. This pattern ensures first-time setup works even on fresh deployments with no existing admin user

#### Settings Storage
**File**: `src/backend/db/settings.py:60-83`

```python
def set_setting(self, key: str, value: str) -> SystemSetting:
    """
    Set a system setting value (upsert).

    Creates the setting if it doesn't exist, updates if it does.
    Returns the updated setting.
    """
    now = datetime.utcnow().isoformat()

    with get_db_connection() as conn:
        cursor = conn.cursor()

        # Use INSERT OR REPLACE for upsert
        cursor.execute("""
            INSERT OR REPLACE INTO system_settings (key, value, updated_at)
            VALUES (?, ?, ?)
        """, (key, value, now))
        conn.commit()

        return SystemSetting(
            key=key,
            value=value,
            updated_at=datetime.fromisoformat(now)
        )
```

**Get Setting Value** (lines 49-58):
```python
def get_setting_value(self, key: str, default: str = None) -> Optional[str]:
    """
    Get just the value of a setting.

    Returns the default if the setting doesn't exist.
    """
    setting = self.get_setting(key)
    if setting:
        return setting.value
    return default
```

#### Database Table
**File**: `src/backend/database.py:522-527`

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

**File**: `src/backend/routers/auth.py`

**Setup Check Function** (lines 20-22):
```python
def is_setup_completed() -> bool:
    """Check if initial setup is completed."""
    return db.get_setting_value('setup_completed', 'false') == 'true'
```

**Admin Login Block** (lines 49-78):
```python
@router.post("/token", response_model=Token)
async def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends()):
    """Login with username/password and get JWT token.

    Used for admin login (username 'admin' with password).
    Regular users should use email authentication.
    """
    # Block login if setup is not completed
    if not is_setup_completed():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="setup_required"
        )
    # ... rest of login logic
```

**Email Login Request Block** (lines 153-158):
```python
# Block if setup is not completed
if not is_setup_completed():
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="setup_required"
    )
```

**Email Login Verify Block** (lines 210-215):
```python
# Block if setup is not completed
if not is_setup_completed():
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="setup_required"
    )
```

**Auth Mode Endpoint Reports Setup Status** (lines 27-46):
```python
@router.get("/api/auth/mode")
async def get_auth_mode():
    """
    Get authentication mode configuration.

    This endpoint requires NO authentication - it's called before login
    to determine which login options to show.

    Returns:
        - email_auth_enabled: Whether email-based login is enabled
        - setup_completed: Whether first-time setup is complete
    """
    email_auth_setting = db.get_setting_value("email_auth_enabled", str(EMAIL_AUTH_ENABLED).lower())
    email_auth_enabled = email_auth_setting.lower() == "true"

    return {
        "email_auth_enabled": email_auth_enabled,
        "setup_completed": is_setup_completed()
    }
```

---

## Flow 2: API Key Configuration

### Frontend Layer

#### Settings Page
**File**: `src/frontend/src/views/Settings.vue`

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
**File**: `src/backend/routers/settings.py:361-588`

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
**File**: `src/backend/routers/settings.py:379-384`

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
**File**: `src/backend/routers/agents.py:508-512`

```python
env_vars = {
    'AGENT_NAME': config.name,
    'AGENT_TYPE': config.type,
    'ANTHROPIC_API_KEY': get_anthropic_api_key(),  # Uses settings value OR env fallback
    # ...
}
```

### System Agent Service
**File**: `src/backend/services/system_agent_service.py:24, 180`

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
| Missing required field (email/password) | 422 | Pydantic validation error | Form prevents submission (email + password required) |
| Email missing/blank/invalid shape | 400 | "A valid admin email is required" | Form validation (email required, #49) |
| Password fails complexity | 400 | "Password does not meet requirements: ..." | Form validation (full OWASP ASVS 2.1 checklist) |
| Passwords don't match | 400 | "Passwords do not match" | Form validation |
| Invalid API key format | 400 | "Invalid API key format. Keys start with 'sk-ant-'" | Inline error |
| API key invalid | N/A | `{valid: false, error: "..."}` | Test result display |
| Not admin | 403 | "Admin access required" | Redirect to dashboard |
| Login blocked (no setup) | 403 | "setup_required" | Frontend checks and redirects |

---

## Security Considerations

1. **Password Security**:
   - Bcrypt hashing with auto-configured work factor
   - **No plaintext fallback (M-003)**: Plaintext password comparison removed as of 2026-02-23. All passwords must be bcrypt hashed.
   - Invalid hash formats are rejected (returns authentication failure)
   - Full OWASP ASVS 2.1 complexity (12+ chars, upper/lower/digit/special, not common) via `validate_password_strength()`
   - Setup endpoint only works ONCE

2. **API Key Security**:
   - Never exposed in full (masked in responses)
   - Format validation (`sk-ant-` prefix)
   - Admin-only access to all API key endpoints
   - Fallback to environment variable if not in settings

3. **Setup Endpoint Protection** (SEC #177 → revised by trinity-enterprise#49):
   - No network auth required (must work on fresh install).
   - **Setup token removed (#49)**: the earlier single-use token (Redis-shared, #1165;
     constant-time compared) that guarded the first-run window against admin hijack is
     **gone**, to streamline self-hosted bring-up. **Accepted tradeoff**: the unauthenticated
     `/api/setup/admin-password` has no proof-of-control, so anyone reaching the URL before
     setup completes can claim the admin account.
   - **Operator responsibility**: deploy an internet-reachable instance behind a tunnel/VPN
     (or otherwise restrict network access) until setup completes — documented in
     `docs/DEPLOYMENT.md` → Security Recommendations. On localhost / trusted LAN this is a non-issue.
   - **Self-disabling** after first use via the `setup_completed` flag — the exposure is the
     pre-setup window only; after that, all auth is enforced.

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

2. **Restart the backend** and check logs for the first-run signal:
   ```bash
   docker compose logs backend | grep "FIRST-TIME SETUP"
   ```
   - **Expected**: a structured JSON log line `TRINITY FIRST-TIME SETUP REQUIRED — open
     the Trinity UI to create the admin account...` (emitted at `WARNING` level for the
     #858 flush guarantee). **No setup token** is printed (#49).

3. **Visit any page** (e.g., `http://localhost/`)
   - **Expected**: Redirect to `/setup`
   - **Verify**: the welcome page shows the orbiting fleet constellation + a form with
     **Admin email (required)**, password, confirm, company (optional), updates opt-in —
     and **no setup-token field**.

4. **Leave email blank** (or enter a malformed email), valid matching password
   - **Expected**: Submit disabled (client-side); the API rejects with 422 (missing) /
     400 ("A valid admin email is required") if forced.

5. **Valid email, weak password** (fails the 12-char / complexity checklist)
   - **Expected**: Submit button disabled (the requirements checklist shows unmet items).

6. **Valid email, mismatched passwords**
   - **Expected**: "Passwords do not match" indicator, submit disabled.

7. **Valid email, strong matching password**
   - **Expected**: Submit enabled — click "Create admin account & continue".

8. **After successful setup**
   - **Expected**: Redirect to `/login`
   - **Verify**: Can log in with the **email + password** (or `admin` + password).

9. **Try accessing /setup again**
   - **Expected**: Redirect to `/login` (setup already done; the endpoint 403s).

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
- First-Time Setup: **Working** (Implemented 2025-12-23)
- API Key Configuration: **Working** (Implemented 2025-12-23)

---

## Flow 3: Operator Profile — Intake + Admin Email Login (trinity-enterprise#38, #82)

First-run setup additionally captures an **optional operator profile** that does
two jobs at once from a single email field: it becomes the admin's **sign-in
identity**, and (on explicit consent) it's submitted to a **hosted intake**.

### Why no verification email at setup

A fresh install has **no email provider** configured (no Resend key), so a
verification code cannot be delivered during setup. The design works *around*
that rather than blocking on it:

- The email is **bound** to the admin account, not verified by a code. The
  operator can immediately sign in with **email + password** (or still with
  `admin` + password).
- The hosted intake is a plain HTTPS POST to a Cloudflare Worker — it does **not**
  use the email provider, so it works offline-of-email too.
- The code-based second factor (email OTP after password) is **Phase 2**, gated
  on a configured provider and the existing `mfa_gate` seam (#5/#388) — not built
  here.

### Frontend

- **`views/SetupPassword.vue`** — a **required Admin email** (sign-in identity)
  plus an optional **Company** field and an **unchecked-by-default** consent
  checkbox: *"Occasionally email me important security & product updates."* Since
  email is always present, the consent box is freely toggleable. Submits
  `{email, password, confirm_password, company, consent_updates}` to
  `POST /api/setup/admin-password` (no `setup_token` since #49).
- **`views/Login.vue`** — the admin form's fixed `admin` label becomes an
  editable **"Username or email"** field (default `admin`), routed through the
  unchanged `authStore.loginWithCredentials(identifier, password)`.
- **`views/Settings.vue`** (General tab, admin) — **Admin sign-in email** card:
  existing-admin transition. `PUT /api/users/me/email` → refresh profile.

### Backend

- **`POST /api/setup/admin-password`** (`routers/setup.py`) — validates the email
  shape *before any write* (typo → clean 400, setup not half-completed); binds it
  via `db.update_user('admin', {'email': …})`; returns `{success, email_registered}`.
  On `consent_updates && email`, schedules `submit_operator_intake(...)` as a
  **`BackgroundTasks`** job so it runs *after* the response — never delaying or
  breaking setup.
- **`services/operator_intake_service.py`** — fire-and-forget `httpx` POST (5s).
  Guards: disabled via `OPERATOR_INTAKE_ENABLED=false` / `DO_NOT_TRACK`; **at-most-
  once** via the `operator_intake_submitted` marker claimed *before* the POST;
  no-op without an email. Owns the stable `installation_id` (random UUID in
  `system_settings`, the #758 telemetry seed). Never raises; never logs the email.
- **`dependencies.authenticate_user`** — resolves the identifier by username, then
  by email when it looks like one and no username matched. The password check
  still runs, so only an account *with* a password hash (the admin) authenticates
  — email-code-only users (no password) never can.
- **`PUT /api/users/me/email`** (`routers/users.py`) — own-account scoped; 409 if
  the email belongs to another account; no verification email.

### Hosted intake endpoint (out-of-repo Cloudflare Worker)

Reuses #1116's intake app on the stable `https://intake.abilityai.dev` domain —
**one added endpoint**, `POST /v1/operator-intake` (sibling to `/v1/report-bug`).
The Worker lives outside this repo (`trinity-ops-agent/`, alongside the bug
intake). `intake.abilityai.dev` is already CSP-allowed (`connect-src`, #1116), so
no CSP change is needed. Versioned contract:

```
POST https://intake.abilityai.dev/v1/operator-intake
{ installation_id, email, company?, name?, role?, use_case?,
  consent: "security_and_product_updates", trinity_version, submitted_at }
→ Worker: rate-limit + dedupe by installation_id → add to the security/product
  updates list (server-held creds) → 2xx. Backend treats any response as
  best-effort and never retries (at-most-once).
```

### Config

| Env | Default | Purpose |
|-----|---------|---------|
| `OPERATOR_INTAKE_ENABLED` | `true` | `false` (or `DO_NOT_TRACK=1`) disables the outbound submission entirely (box still shows, nothing leaves) |
| `OPERATOR_INTAKE_URL` | `https://intake.abilityai.dev/v1/operator-intake` | Repoint to self-host the intake |

### Status
- Operator Intake + Admin Email Login (Phase 1): **Working** (Implemented 2026-06-22)

---

## Related Flows

### Upstream
- None (this is the entry point for fresh installations)

### Downstream
- **Agent Lifecycle**: Uses stored API key via `get_anthropic_api_key()`
- **System Agent**: Uses stored API key for trinity-system operations
- **Authentication**: Login blocked until setup completed

---

## Revision History

| Date | Change | Details |
|------|--------|---------|
| 2025-12-23 | Initial documentation | First-time setup and API key configuration flows |
| 2026-01-14 | Bug fix: Admin user upsert | Fixed `update_user_password()` to create admin user if it doesn't exist. Previously, on fresh deployment with empty ADMIN_PASSWORD env var, the UPDATE query affected 0 rows but setup_completed was still set to true, leaving users unable to login. The method now uses an upsert pattern: UPDATE first, then INSERT if no rows affected. See `src/backend/db/users.py:129-162`. |
| 2026-01-23 | Line number verification | Updated all line numbers to match current codebase. Verified: setup.py endpoints (22-34, 37-81), auth.py login blocks (20-22, 49-78, 153-158, 210-215), dependencies.py password hashing (15-37), db/users.py upsert (129-162), db/settings.py (49-83), router/index.js guards (165-220, 242-245), SetupPassword.vue (166-176, 205-207, 209-236). Added documentation for email auth login blocking and password strength validation. |
| 2026-02-23 | Security Fix M-003 | Removed plaintext password fallback from `verify_password()` in dependencies.py:24-34. All passwords must now be bcrypt hashed. Invalid hash formats are rejected. Updated Password Hashing section and Security Considerations. |
| 2026-03-26 | Security Fix SEC #177 | Added single-use setup token to prevent installation hijack. Token generated via `secrets.token_urlsafe(24)` at startup, printed to server logs, required in `POST /api/setup/admin-password`. Frontend adds setup token field with instructions to check `docker compose logs backend`. Constant-time comparison guards against timing attacks. |
| 2026-06-22 | Operator profile — intake + admin email login (trinity-enterprise#38, #82) | Added Flow 3. Optional email/company + unchecked-by-default consent at setup; email binds as admin sign-in identity (login with email + password — **no verification email**, fresh installs have no Resend key); opt-in once-per-install fire-and-forget POST to a new `/v1/operator-intake` endpoint on #1116's Cloudflare intake app. New `services/operator_intake_service.py` (+ `installation_id`), `authenticate_user` email resolution, `PUT /api/users/me/email` + Settings card for existing-admin transition, `OPERATOR_INTAKE_ENABLED`/`OPERATOR_INTAKE_URL`. |
| 2026-06-23 | Streamlined setup wizard (trinity-enterprise#49) | **Removed the setup token** (and all `ensure_setup_token`/`clear_setup_token`/Redis machinery + the `main.py` startup emission — setup no longer depends on Redis), **made admin email required** (sign-in identity; missing → 422, blank/invalid → 400 before any write), reordered to email → password (+confirm) → company → updates opt-in, and rebuilt `SetupPassword.vue` as a welcoming single-screen first-run page with an animated **orbiting fleet constellation** (dark hero, `prefers-reduced-motion` aware). Security tradeoff of token removal accepted + documented as operator responsibility (deploy behind a tunnel/VPN until setup completes) in `docs/DEPLOYMENT.md` → Security Recommendations. Removed `tests/unit/test_1165_setup_token_shared.py`; updated `tests/test_setup.py` + `tests/unit/test_setup_operator_profile.py`. |
