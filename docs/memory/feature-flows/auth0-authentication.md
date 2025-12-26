# Feature: Authentication Mode System

## Overview
Dual-mode authentication system with **runtime** mode detection from backend. Supports local username/password login (dev mode) and Auth0 OAuth with Google (production mode). The backend controls which mode is active via `DEV_MODE_ENABLED` environment variable - no frontend rebuild required to switch modes.

## User Story
As a platform operator, I want to control authentication mode via environment variable so that I can switch between dev (local login) and production (Auth0 OAuth) without rebuilding the frontend.

## Entry Points
- **UI**: `src/frontend/src/views/Login.vue` - Dual-mode login page
- **API**: `GET /api/auth/mode` - Mode detection (unauthenticated)
- **API**: `POST /api/token` - Dev mode login (gated)
- **API**: `POST /api/auth/exchange` - Auth0 token exchange

---

## Architecture: Runtime Mode Detection

The authentication system was refactored from build-time (`VITE_DEV_MODE` env var) to runtime mode detection:

```
                    +-----------------------+
                    |   docker-compose.yml  |
                    |   DEV_MODE_ENABLED=   |
                    |   true or false       |
                    +-----------+-----------+
                                |
                    +-----------v-----------+
                    |   Backend config.py   |
                    |   DEV_MODE_ENABLED    |
                    +-----------+-----------+
                                |
    +---------------------------+---------------------------+
    |                                                       |
    v                                                       v
+---+-----+                                         +-------+----+
| GET     |                                         | POST       |
| /api/   | <-- Unauthenticated                     | /api/token |
| auth/   |     frontend calls                      +------+-----+
| mode    |     on page load                               |
+---------+                                                |
    |                                                      v
    v                                            +---------+---------+
+---+------------------------------------------+ | If DEV_MODE=false |
| Response:                                    | | Return 403        |
| {                                            | +-------------------+
|   "dev_mode_enabled": true/false,            |
|   "auth0_configured": true,                  |
|   "allowed_domain": "ability.ai"             |
| }                                            |
+----------------------------------------------+
```

---

## Frontend Layer

### Mode Detection Flow

1. **App Startup** (`src/frontend/src/main.js:23-24`)
   ```javascript
   const authStore = useAuthStore()
   authStore.initializeAuth()  // Calls detectAuthMode()
   ```

2. **Detect Auth Mode** (`src/frontend/src/stores/auth.js:52-69`)
   ```javascript
   async detectAuthMode() {
     const response = await axios.get('/api/auth/mode')
     this.devModeEnabled = response.data.dev_mode_enabled
     this.auth0Configured = response.data.auth0_configured
     this.allowedDomain = response.data.allowed_domain || 'ability.ai'
     this.modeDetected = true
   }
   ```

3. **Login.vue Renders Based on Mode** (`src/frontend/src/views/Login.vue:38-102`)
   - If `authStore.devModeEnabled` is true: Show username/password form
   - If `authStore.devModeEnabled` is false: Show "Sign in with Google" button

### Components

#### Login.vue (`src/frontend/src/views/Login.vue`)
| Line | Element | Purpose |
|------|---------|---------|
| 14-17 | Loading spinner | Shows during mode detection |
| 38-81 | Dev mode form | Username/password inputs |
| 84-102 | Production mode | Google OAuth button |
| 198-209 | `handleDevLogin()` | Calls `authStore.loginWithCredentials()` |
| 212-225 | `handleGoogleLogin()` | Calls `auth0.loginWithRedirect()` |

#### main.js (`src/frontend/src/main.js`)
```javascript
// Line 16-20: Auth0 plugin ALWAYS initialized (needed for prod mode)
const auth0 = createAuth0(auth0Config)
app.use(auth0)

// Line 23-24: Initialize auth state (triggers mode detection)
const authStore = useAuthStore()
authStore.initializeAuth()
```

**Key Change**: Auth0 plugin is always loaded. In dev mode, it is simply not used. This eliminates the need to rebuild frontend when switching modes.

### State Management (`src/frontend/src/stores/auth.js`)

| Line | Property/Action | Purpose |
|------|-----------------|---------|
| 15-18 | `devModeEnabled`, `auth0Configured`, `modeDetected` | Runtime mode state |
| 52-69 | `detectAuthMode()` | Call `/api/auth/mode` to get backend config |
| 72-114 | `initializeAuth()` | Restore session + validate token mode |
| 117-130 | `parseJwtPayload()` | Decode JWT to check mode claim |
| 199-237 | `loginWithCredentials(username, password)` | Dev mode login |

### Token Mode Validation on Session Restore

When restoring a session from localStorage, the frontend checks if the token mode matches the backend mode (`src/frontend/src/stores/auth.js:86-105`):

```javascript
// Parse JWT to get mode claim
const tokenPayload = this.parseJwtPayload(storedToken)
const tokenMode = tokenPayload?.mode

// If token is dev mode but backend is prod mode, clear credentials
if (tokenMode === 'dev' && !this.devModeEnabled) {
  console.log('Clearing dev mode token (backend is now in production mode)')
  localStorage.removeItem('token')
  localStorage.removeItem('auth0_user')
}
```

This prevents dev mode tokens from being used when the backend switches to production mode.

### Configuration (`src/frontend/src/config/auth0.js`)
```javascript
// Line 4-5: Dev mode is now detected at RUNTIME from backend
// The VITE_DEV_MODE env var is no longer used

export const ALLOWED_DOMAIN = import.meta.env.VITE_AUTH0_ALLOWED_DOMAIN || ''

export const auth0Config = {
  domain: import.meta.env.VITE_AUTH0_DOMAIN || '',
  clientId: import.meta.env.VITE_AUTH0_CLIENT_ID || '',
  authorizationParams: {
    redirect_uri: window.location.origin,
    scope: 'openid profile email',
    connection: 'google-oauth2',
    hd: ALLOWED_DOMAIN || undefined  // Google domain hint
  },
  useRefreshTokens: true,
  cacheLocation: 'localstorage'
}
```

**Key Change**: Removed `export const isDevMode` - mode is now runtime, not build-time.

---

## Backend Layer

### Configuration (`src/backend/config.py:6-9`)
```python
# Development Mode
# Set DEV_MODE_ENABLED=true to enable local username/password login
# When false (default), only Auth0 OAuth is allowed
DEV_MODE_ENABLED = os.getenv("DEV_MODE_ENABLED", "false").lower() == "true"
```

### Endpoints

#### GET /api/auth/mode (`src/backend/routers/auth.py:26-43`)
**Unauthenticated** endpoint for frontend mode detection.

```python
@router.get("/api/auth/mode")
async def get_auth_mode():
    return {
        "dev_mode_enabled": DEV_MODE_ENABLED,
        "auth0_configured": bool(AUTH0_DOMAIN),
        "allowed_domain": AUTH0_ALLOWED_DOMAIN
    }
```

**Response:**
```json
{
  "dev_mode_enabled": true,
  "auth0_configured": true,
  "allowed_domain": "ability.ai"
}
```

#### POST /api/token (`src/backend/routers/auth.py:46-93`)
Dev mode login - **gated by DEV_MODE_ENABLED**.

**Business Logic:**
1. **Check mode** (line 54-58): If `DEV_MODE_ENABLED=false`, return 403
2. **Authenticate** (line 60): `authenticate_user(username, password)`
3. **Create JWT** (line 77-82): Include `mode="dev"` claim
4. **Audit log** (line 84-91): Log `dev_login` action

**Request:**
```
Content-Type: application/x-www-form-urlencoded
username=admin&password=<password>
```

**Response:**
```json
{ "access_token": "<jwt-with-mode=dev>", "token_type": "bearer" }
```

**Error (production mode):**
```json
{ "detail": "Local login is disabled. Use 'Sign in with Google' instead." }
```

#### POST /api/auth/exchange (`src/backend/routers/auth.py:102-208`)
Auth0 token exchange for production mode.

**Business Logic:**
1. Validate Auth0 token via `/userinfo` (line 111-132)
2. Check email domain is allowed (line 143-158)
3. Verify email is verified (line 160-164)
4. Get or create user in database (line 166-173)
5. Create JWT with `mode="prod"` claim (line 175-180)
6. Log audit event (line 182-190)

#### GET /api/auth/validate (`src/backend/routers/auth.py:211-257`)
Validates JWT for nginx auth_request (unchanged).

### JWT Token Structure

```python
# src/backend/dependencies.py:35-53
def create_access_token(data: dict, expires_delta: Optional[timedelta] = None, mode: str = "prod") -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=15))
    to_encode.update({
        "exp": expire,
        "mode": mode  # Track auth mode to prevent dev/prod token mixing
    })
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
```

**JWT Payload:**
```json
{
  "sub": "admin",
  "exp": 1733400000,
  "mode": "dev"  // or "prod" for Auth0 tokens
}
```

---

## Docker Configuration

### Development (`docker-compose.yml:11`)
```yaml
environment:
  - DEV_MODE_ENABLED=${DEV_MODE_ENABLED:-true}  # Enable local login for development
```

### Production (`docker-compose.prod.yml:20`)
```yaml
environment:
  - DEV_MODE_ENABLED=${DEV_MODE_ENABLED:-false}
```

### Switching Modes
```bash
# Local development (default): dev mode enabled
docker-compose up

# Production-like locally:
DEV_MODE_ENABLED=false docker-compose up

# Production (uses docker-compose.prod.yml):
# DEV_MODE_ENABLED defaults to false
```

---

## Sequence Diagrams

### Dev Mode Login Flow
```
User -> Login.vue: Load page
Login.vue -> auth.js: initializeAuth()
auth.js -> Backend: GET /api/auth/mode
Backend -> auth.js: {dev_mode_enabled: true, ...}
auth.js -> Login.vue: devModeEnabled = true
Login.vue -> User: Show username/password form

User -> Login.vue: Enter credentials, click Sign In
Login.vue -> auth.js: loginWithCredentials(admin, password)
auth.js -> Backend: POST /api/token (FormData)
Backend -> Backend: Validate credentials
Backend -> auth.js: {access_token: "jwt-mode-dev", token_type: "bearer"}
auth.js -> localStorage: Store token + user
auth.js -> axios: Set Authorization header
Login.vue -> Router: Redirect to /
```

### Production Mode Login Flow
```
User -> Login.vue: Load page
Login.vue -> auth.js: initializeAuth()
auth.js -> Backend: GET /api/auth/mode
Backend -> auth.js: {dev_mode_enabled: false, ...}
auth.js -> Login.vue: devModeEnabled = false
Login.vue -> User: Show "Sign in with Google" button

User -> Login.vue: Click "Sign in with Google"
Login.vue -> Auth0: loginWithRedirect()
Auth0 -> Google: OAuth redirect
Google -> Auth0: Authorization code
Auth0 -> Login.vue: Redirect with tokens
Login.vue -> auth.js: handleAuth0Callback()
auth.js -> Auth0: getAccessTokenSilently()
Auth0 -> auth.js: access_token
auth.js -> Backend: POST /api/auth/exchange {auth0_token}
Backend -> Auth0: GET /userinfo
Auth0 -> Backend: User profile (email, name, etc.)
Backend -> Backend: Validate allowed domain
Backend -> SQLite: get_or_create_auth0_user()
Backend -> auth.js: {access_token: "jwt-mode-prod", token_type: "bearer"}
auth.js -> localStorage: Store token + user
auth.js -> axios: Set Authorization header
Login.vue -> Router: Redirect to /
```

### Session Restore with Mode Validation
```
User -> App: Load application
App -> auth.js: initializeAuth()
auth.js -> Backend: GET /api/auth/mode
Backend -> auth.js: {dev_mode_enabled: false}
auth.js -> localStorage: Read stored token
auth.js -> auth.js: parseJwtPayload(token)

alt Token mode is "dev" but backend is "prod"
    auth.js -> localStorage: Clear token + user
    auth.js -> User: Redirect to login
else Token mode matches backend
    auth.js -> axios: Set Authorization header
    auth.js -> App: Session restored
end
```

---

## Session Persistence

### localStorage Keys
| Key | Value | Purpose |
|-----|-------|---------|
| `token` | JWT string (includes mode claim) | Backend authentication |
| `auth0_user` | JSON object | User profile data |

### Cookie (for nginx auth_request)
```javascript
// auth.js:193
document.cookie = `token=${this.token}; path=/; max-age=1800; SameSite=Strict`
```

---

## Side Effects

### Audit Logging
| Event | Action | Logged When |
|-------|--------|-------------|
| authentication | dev_login | Local admin login (dev mode) |
| authentication | auth0_exchange | Auth0 token exchange (prod mode) |
| authentication | token_validation | Any protected endpoint access |

### WebSocket Connection
On successful login, App.vue connects to WebSocket for real-time updates.

---

## Error Handling

| Error Case | HTTP Status | Message |
|------------|-------------|---------|
| Dev login in prod mode | 403 | "Local login is disabled. Use 'Sign in with Google' instead." |
| Invalid credentials | 401 | "Incorrect username or password" |
| Invalid Auth0 token | 401 | "Invalid Auth0 token" |
| No email in profile | 401 | "No email found in Auth0 profile" |
| Wrong domain | 403 | "Access restricted to @{domain} domain users only" |
| Email not verified | 403 | "Email not verified" |
| Invalid/expired JWT | 401 | "Could not validate credentials" |
| Auth0 API failure | 500 | "Auth0 token exchange failed: {error}" |

### Automatic Logout on Token Expiration

**Implementation** (`src/frontend/src/main.js:27-49`)

Axios response interceptor automatically handles expired tokens:

```javascript
axios.interceptors.response.use(
  response => response,
  error => {
    if (error.response?.status === 401) {
      const currentPath = router.currentRoute.value.path
      if (currentPath !== '/login' && currentPath !== '/setup') {
        console.log('🔐 Session expired - redirecting to login')
        authStore.logout()
        router.push('/login')
      }
    }
    return Promise.reject(error)
  }
)
```

**Behavior**:
- Any 401 response triggers automatic logout
- User is redirected to login page
- Auth state is cleared from localStorage
- Prevents confusing empty interface when token expires

---

## Security Considerations

1. **No Hardcoded Credentials**: Dev mode credentials are NOT in source code - user must provide them
2. **Mode Gating**: `/api/token` returns 403 when `DEV_MODE_ENABLED=false`
3. **Token Mode Claim**: JWTs include `mode` claim to distinguish dev/prod tokens
4. **Session Invalidation**: Dev tokens are cleared if backend switches to prod mode
5. **Domain Restriction**: Server-side validation ensures only allowed domain emails accepted
6. **Token Expiry**: JWT expires in 7 days (configurable via `ACCESS_TOKEN_EXPIRE_MINUTES` in `config.py`)
7. **Cookie Security**: SameSite=Strict prevents CSRF
8. **Audit Trail**: All authentication events logged

---

## Related Flows

- **Downstream**: All protected API endpoints via `Depends(get_current_user)`
- **Related**: Agent Sharing (uses user identity for ownership)
- **Related**: MCP API Keys (tied to user accounts)

---

## Testing

### Prerequisites
- [ ] Backend running at http://localhost:8000
- [ ] Frontend running at http://localhost:3000
- [ ] Auth0 tenant configured (for production mode testing)
- [ ] Allowed domain Google account (for production mode testing)

### Test Steps

#### 1. Mode Detection
**Action**: Open browser dev tools, navigate to http://localhost:3000/login

**Expected**:
- Network tab shows `GET /api/auth/mode`
- Response shows `dev_mode_enabled: true` (local) or `false` (prod)
- UI shows appropriate login form

**Verify**:
- [ ] `/api/auth/mode` returns expected mode
- [ ] Login UI matches the mode

#### 2. Dev Mode Login
**Action** (requires `DEV_MODE_ENABLED=true`):
- Navigate to http://localhost:3000/login
- Enter username: `admin`
- Enter password: (configured password)
- Click "Sign In"

**Expected**:
- POST to `/api/token` with FormData
- JWT returned with `mode: "dev"`
- Redirect to dashboard

**Verify**:
- [ ] Yellow "Development Mode" banner visible
- [ ] Login successful with correct credentials
- [ ] localStorage has `token` and `auth0_user`
- [ ] JWT payload contains `"mode": "dev"`

#### 3. Dev Mode Login Blocked in Production
**Action** (requires `DEV_MODE_ENABLED=false`):
```bash
# Stop current containers
docker-compose down

# Start with prod mode
DEV_MODE_ENABLED=false docker-compose up
```
- Try to POST to `/api/token` directly via curl:
```bash
curl -X POST http://localhost:8000/api/token \
  -d "username=admin&password=yourpassword"
```

**Expected**:
- 403 Forbidden
- Message: "Local login is disabled. Use 'Sign in with Google' instead."

**Verify**:
- [ ] `/api/token` returns 403
- [ ] Login page shows Google OAuth button instead of form

#### 4. Production Mode Login (Auth0)
**Action** (requires `DEV_MODE_ENABLED=false`):
- Navigate to http://localhost:3000/login
- Click "Sign in with Google"
- Complete OAuth flow with allowed domain account

**Expected**:
- Redirect to Auth0
- Google OAuth consent
- Redirect back with tokens
- JWT returned with `mode: "prod"`

**Verify**:
- [ ] Auth0 redirect works
- [ ] Backend exchanges token successfully
- [ ] JWT payload contains `"mode": "prod"`
- [ ] Audit log shows `auth0_exchange` event

#### 5. Token Mode Mismatch Handling
**Action**:
1. Login in dev mode, get dev token stored
2. Stop containers
3. Restart with `DEV_MODE_ENABLED=false`
4. Refresh the page

**Expected**:
- Frontend detects mode mismatch
- Dev token is cleared
- User redirected to login

**Verify**:
- [ ] Console shows "Clearing dev mode token (backend is now in production mode)"
- [ ] localStorage token is removed
- [ ] User must re-authenticate

#### 6. Domain Restriction (Production)
**Action**: Login with non-allowed domain email (requires test setup)

**Expected**:
- 403 Forbidden
- Message: "Access restricted to @{domain} domain users only"

**Verify**:
- [ ] Backend rejects token exchange
- [ ] Audit log shows `denied` result

### Cleanup
```bash
# Reset to default dev mode
docker-compose down
docker-compose up
```

---

**Last Updated**: 2025-12-23
**Status**: Working (dev mode tested, production mode tested)
**Changes**:
- 2025-12-23: Documented bcrypt password hashing (OWASP Phase 12.3), SECRET_KEY handling
- 2025-12-19: Updated line numbers and verified against current codebase

---

## Security Hardening (OWASP Phase 12.3)

### Password Hashing

Passwords are now hashed using bcrypt (passlib) instead of plaintext storage:

**File**: `src/backend/dependencies.py:16-38`

```python
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(password: str) -> str:
    """Hash a password using bcrypt."""
    return pwd_context.hash(password)

def verify_password(plain_password: str, stored_password: str) -> bool:
    """Verify password against stored hash.
    Supports both bcrypt hashes and legacy plaintext for migration.
    """
    try:
        if pwd_context.verify(plain_password, stored_password):
            return True
    except Exception:
        pass
    # Fall back to plaintext comparison for legacy passwords
    return plain_password == stored_password
```

### SECRET_KEY Handling

**File**: `src/backend/config.py:12-23`

```python
# SECURITY: SECRET_KEY must be set via environment variable in production
_secret_key = os.getenv("SECRET_KEY", "")
if not _secret_key:
    _secret_key = secrets.token_hex(32)
    print("WARNING: SECRET_KEY not set - generated random key for this session")
elif _secret_key == "your-secret-key-change-in-production":
    print("CRITICAL: Default SECRET_KEY detected - change immediately!")

SECRET_KEY = _secret_key
```

**Best Practices**:
- Always set `SECRET_KEY` via environment variable in production
- Use `secrets.token_hex(32)` to generate secure keys
- Never use the default placeholder value

### Database Migration

Existing plaintext passwords are automatically migrated to bcrypt on first login:

**File**: `src/backend/database.py:555-569`

```python
# Check if existing password needs migration from plaintext to bcrypt
if existing_hash and not existing_hash.startswith('$2'):
    # Password is likely plaintext (bcrypt hashes start with $2)
    if admin_password and existing_hash == admin_password:
        pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
        hashed = pwd_context.hash(admin_password)
        cursor.execute("UPDATE users SET password_hash = ? WHERE username = 'admin'", (hashed,))
        print("Migrated admin password from plaintext to bcrypt")
```
