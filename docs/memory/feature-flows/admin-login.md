# Feature: Admin Login

## Revision History

| Date | Changes |
|------|---------|
| 2026-02-23 | **Security Fixes M-003 and M-005**: (1) Removed plaintext password fallback - all passwords must be bcrypt hashed (dependencies.py:24-34). (2) Added Redis-based rate limiting to `/token` endpoint - 5 attempts per 10 minutes per IP, returns HTTP 429 when exceeded (auth.py:24-95, 127-161). Updated error handling table. |
| 2026-01-23 | Initial documentation |

## Overview

Password-based authentication for the admin user. This is the primary administrative access method where the admin (fixed username "admin") logs in with a password set during first-time setup.

## User Story

As a platform administrator, I want to log in with a password so that I can access the Trinity platform with full admin privileges without requiring email-based authentication.

## Entry Points

- **UI**: `/login` route - Admin Login button at bottom of email login form
- **API**: `POST /api/token` - OAuth2-compatible token endpoint

## Prerequisites

- First-time setup must be completed (`setup_completed=true` in system_settings)
- Admin password must be set via the Setup wizard

## Frontend Layer

### Components

**Login.vue** (`src/frontend/src/views/Login.vue`)

The login page supports two authentication modes:
1. **Email Authentication** (default) - 2-step email verification flow
2. **Admin Login** - Password-based login (this flow)

UI Toggle Logic (lines 38, 114-122, 126):
```javascript
// State to switch between login methods
const showAdminLogin = ref(false)

// Admin Login Option button (visible when email auth is enabled)
<button @click="showAdminLogin = true">
  Admin Login
</button>

// Admin login form shown when:
// - User clicks "Admin Login" button, OR
// - Email auth is disabled entirely
<div v-else-if="showAdminLogin || !authStore.emailAuthEnabled">
```

Admin Login Form (lines 125-171):
- Fixed username display: "admin" (not editable)
- Password input field with validation
- Submit button calls `handleAdminLogin()`

```vue
<!-- Admin Login: Password Only (username is fixed as 'admin') -->
<div v-else-if="showAdminLogin || !authStore.emailAuthEnabled">
  <div class="mb-4 p-3 bg-gray-50">
    <p>Admin Login</p>
  </div>

  <form @submit.prevent="handleAdminLogin">
    <div>
      <label>Username</label>
      <div>admin</div>  <!-- Fixed, not editable -->
    </div>

    <div>
      <label for="password">Password</label>
      <input id="password" v-model="password" type="password" required />
    </div>

    <button type="submit" :disabled="loginLoading || !password">
      Sign In as Admin
    </button>
  </form>
</div>
```

Login Handler (lines 293-304):
```javascript
const handleAdminLogin = async () => {
  loginLoading.value = true
  authStore.clearError()

  const success = await authStore.loginWithCredentials('admin', password.value)
  if (success) {
    router.push('/')
  }

  loginLoading.value = false
}
```

### State Management

**auth.js** (`src/frontend/src/stores/auth.js`)

Key state properties (lines 6-13):
```javascript
state: () => ({
  token: null,
  user: null,
  isAuthenticated: false,
  isLoading: true,
  authError: null,
  emailAuthEnabled: null,
  modeDetected: false
})
```

`loginWithCredentials()` action (lines 125-158):
```javascript
async loginWithCredentials(username, password) {
  try {
    // Create form data for OAuth2 password flow
    const formData = new FormData()
    formData.append('username', username)
    formData.append('password', password)

    // POST to token endpoint
    const response = await axios.post('/api/token', formData)
    this.token = response.data.access_token

    // Create a local user profile (not from Auth0)
    const devUser = {
      sub: `local|${username}`,
      email: `${username}@localhost`,
      name: username,
      picture: 'https://www.gravatar.com/avatar/...',
      email_verified: true
    }

    this.user = devUser
    this.isAuthenticated = true

    // Persist to localStorage
    localStorage.setItem('token', this.token)
    localStorage.setItem('auth0_user', JSON.stringify(devUser))
    this.setupAxiosAuth()

    return true
  } catch (error) {
    const detail = error.response?.data?.detail || 'Invalid username or password'
    this.authError = detail
    return false
  }
}
```

`setupAxiosAuth()` method:
```javascript
setupAxiosAuth() {
  if (this.token) {
    // Set Authorization header for all axios requests
    axios.defaults.headers.common['Authorization'] = `Bearer ${this.token}`
  }
}
```

The token is *not* mirrored into `document.cookie`. An earlier version did
mirror for a hypothetical nginx `auth_request` validation, but no
deployment ever wired up that directive — the cookie was dead code with
real attack surface (no `HttpOnly` possible on JS-set cookies, no
`Secure` flag over HTTP, auto-attached as a CSRF vector). Removed in
#188 / PR #642.

### Session Persistence

On app startup, `initializeAuth()` (lines 59-97) checks for stored credentials:
```javascript
async initializeAuth() {
  await this.detectAuthMode()

  const storedToken = localStorage.getItem('token')
  const storedUser = localStorage.getItem('auth0_user')

  if (storedToken && storedUser) {
    // Parse JWT to get mode claim
    const tokenPayload = this.parseJwtPayload(storedToken)
    const tokenMode = tokenPayload?.mode  // 'admin', 'email', or 'prod'

    if (tokenMode) {
      // Restore session
      this.token = storedToken
      this.user = JSON.parse(storedUser)
      this.isAuthenticated = true
      this.setupAxiosAuth()
    }
  }
}
```

### Router Navigation Guard

**router/index.js** (`src/frontend/src/router/index.js`)

Setup status check (lines 170-188):
```javascript
async function checkSetupStatus() {
  const response = await fetch('/api/setup/status')
  const data = await response.json()
  return data.setup_completed
}
```

Navigation guard (lines 191-239):
```javascript
router.beforeEach(async (to, from, next) => {
  const authStore = useAuthStore()

  // Check if setup is completed
  const setupCompleted = await checkSetupStatus()
  if (!setupCompleted) {
    next('/setup')
    return
  }

  // Check if route requires authentication
  if (to.meta.requiresAuth) {
    if (authStore.isAuthenticated) {
      next()  // Allow access
    } else {
      next('/login')  // Redirect to login
    }
  }
})
```

## Backend Layer

### Endpoints

**routers/auth.py** (`src/backend/routers/auth.py`)

#### Rate Limiting Configuration (M-005)

**Lines 24-95**: Redis-based rate limiting to prevent brute force attacks.

```python
# Rate limiting configuration (lines 24-26)
LOGIN_RATE_LIMIT = 5  # Max attempts
LOGIN_RATE_WINDOW = 600  # 10 minutes in seconds

# Redis client for rate limiting (lines 28-41)
_redis_client = None

def get_redis_client():
    """Get or create Redis client for rate limiting."""
    global _redis_client
    if _redis_client is None:
        try:
            _redis_client = redis.from_url(REDIS_URL, decode_responses=True)
            _redis_client.ping()
        except Exception as e:
            logger.warning(f"Redis unavailable for rate limiting: {e}")
            return None
    return _redis_client


# Check rate limit function (lines 44-71)
def check_login_rate_limit(client_ip: str) -> bool:
    """
    Check if login attempts from IP are within rate limit.
    Returns True if allowed, raises HTTPException if rate limited.
    """
    r = get_redis_client()
    if r is None:
        # Redis unavailable - allow login but log warning
        return True

    key = f"login_attempts:{client_ip}"
    attempts = r.get(key)
    if attempts and int(attempts) >= LOGIN_RATE_LIMIT:
        ttl = r.ttl(key)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many login attempts. Try again in {ttl} seconds."
        )
    return True


# Record login attempt function (lines 74-95)
def record_login_attempt(client_ip: str, success: bool):
    """
    Record a login attempt. Failed attempts increment counter.
    Successful logins clear the counter.
    """
    r = get_redis_client()
    if r is None:
        return

    key = f"login_attempts:{client_ip}"
    if success:
        r.delete(key)  # Clear attempts on successful login
    else:
        pipe = r.pipeline()
        pipe.incr(key)
        pipe.expire(key, LOGIN_RATE_WINDOW)
        pipe.execute()
```

**Rate Limiting Behavior**:
- **Redis key**: `login_attempts:{client_ip}`
- **Threshold**: 5 failed attempts within 10 minutes triggers lockout
- **Reset**: Successful login clears the attempt counter
- **Graceful degradation**: If Redis is unavailable, rate limiting is bypassed with warning logged

#### POST /api/token endpoint (lines 127-172)

```python
@router.post("/token", response_model=Token)
async def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends()):
    """Login with username/password and get JWT token.

    Used for admin login (username 'admin' with password).
    Regular users should use email authentication.

    Rate limited: 5 attempts per 10 minutes per IP (M-005).
    """
    # Get client IP for rate limiting
    client_ip = request.client.host if request.client else "unknown"

    # Check rate limit before processing (M-005)
    check_login_rate_limit(client_ip)

    # Block login if setup is not completed
    if not is_setup_completed():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="setup_required"
        )

    # Authenticate user
    user = authenticate_user(form_data.username, form_data.password)
    if not user:
        # Record failed attempt
        record_login_attempt(client_ip, success=False)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Record successful login (clears attempt counter)
    record_login_attempt(client_ip, success=True)

    # Update last login timestamp
    db.update_last_login(user["username"])

    # Create JWT token with 'admin' mode
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user["username"]},
        expires_delta=access_token_expires,
        mode="admin"  # Mark as admin login token
    )

    return {"access_token": access_token, "token_type": "bearer"}
```

`GET /api/auth/mode` endpoint (lines 27-46):
```python
@router.get("/api/auth/mode")
async def get_auth_mode():
    """Get authentication mode configuration (no auth required)."""
    email_auth_enabled = db.get_setting_value("email_auth_enabled", "true").lower() == "true"
    return {
        "email_auth_enabled": email_auth_enabled,
        "setup_completed": is_setup_completed()
    }
```

### Authentication Logic

**dependencies.py** (`src/backend/dependencies.py`)

Password verification (lines 24-34) - **Updated M-003 (2026-02-23)**:
```python
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

**Security Note (M-003)**: The plaintext password fallback has been removed. All passwords must be stored as bcrypt hashes. If a stored password has an invalid hash format, authentication is rejected. This ensures:
- No legacy plaintext passwords can be used
- Invalid hash formats are treated as authentication failure
- Timing attacks are mitigated by consistent bcrypt verification

User authentication (lines 40-47):
```python
def authenticate_user(username: str, password: str):
    """Authenticate a user by username and password."""
    user = db.get_user_by_username(username)
    if not user:
        return False
    if not verify_password(password, user["password"]):
        return False
    return user
```

JWT token creation (lines 50-68):
```python
def create_access_token(data: dict, expires_delta: Optional[timedelta] = None, mode: str = "prod") -> str:
    """Create a JWT access token."""
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=15))
    to_encode.update({
        "exp": expire,
        "mode": mode  # 'admin', 'email', or 'prod'
    })
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
```

### Database Operations

**db/users.py** (`src/backend/db/users.py`)

User lookup (lines 49-51):
```python
def get_user_by_username(self, username: str) -> Optional[Dict]:
    """Get user by username."""
    return self._get_user_by_field("username", username)
```

Row to user dict mapping (lines 22-36):
```python
def _row_to_user_dict(row) -> Dict:
    """Convert a user row to a dictionary."""
    return {
        "id": row["id"],
        "username": row["username"],
        "password": row["password_hash"],  # Note: key is "password" for backward compat
        "role": row["role"],
        "email": row["email"],
        # ... other fields
    }
```

## First-Time Setup Relationship

The admin password is set during first-time setup. Until setup is complete, login is blocked.

**routers/setup.py** (`src/backend/routers/setup.py`)

Set admin password endpoint (lines 37-81):
```python
@router.post("/admin-password")
async def set_admin_password(data: SetAdminPasswordRequest):
    """Set admin password on first launch (only works once)."""

    # Block if already completed
    if db.get_setting_value('setup_completed', 'false') == 'true':
        raise HTTPException(status_code=403, detail="Setup already completed")

    # Validate password
    if len(data.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    # Hash password with bcrypt
    hashed_password = hash_password(data.password)

    # Update or create admin user
    db.update_user_password('admin', hashed_password)

    # Mark setup as completed
    db.set_setting('setup_completed', 'true')

    return {"success": True}
```

**database.py** (`src/backend/database.py`)

Admin user initialization on database startup (lines 661-718):
```python
def _ensure_admin_user(cursor, conn):
    """Ensure the admin user exists with properly hashed password."""
    admin_password = os.getenv("ADMIN_PASSWORD", "")
    admin_username = os.getenv("ADMIN_USERNAME", "admin")

    cursor.execute("SELECT id, password_hash FROM users WHERE username = ?", (admin_username,))
    existing = cursor.fetchone()

    if existing is None:
        # Create admin user if ADMIN_PASSWORD env var is set
        if admin_password:
            hashed = pwd_context.hash(admin_password)
            cursor.execute("""
                INSERT INTO users (username, password_hash, role, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
            """, (admin_username, hashed, "admin", now, now))
```

## Configuration

**config.py** (`src/backend/config.py`)

JWT settings (lines 12-26):
```python
SECRET_KEY = os.getenv("SECRET_KEY", "") or secrets.token_hex(32)
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 10080  # 7 days
```

## Data Flow Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            ADMIN LOGIN FLOW                                  │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  Login.vue  │────>│  auth.js    │────>│ POST /token │────>│ database.py │
│  UI Form    │     │  store      │     │  FastAPI    │     │  SQLite     │
└─────────────┘     └─────────────┘     └─────────────┘     └─────────────┘
      │                   │                   │                   │
      │ password          │ FormData         │ authenticate      │ SELECT
      │ entered           │ POST             │ _user()           │ FROM users
      │                   │                   │                   │
      v                   v                   v                   v
┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│ handleAdmin │────>│ loginWith   │────>│ verify_     │────>│ bcrypt      │
│ Login()     │     │ Credentials │     │ password()  │     │ verify      │
└─────────────┘     └─────────────┘     └─────────────┘     └─────────────┘
                          │                   │
                          │ success           │ create_access
                          │                   │ _token()
                          v                   v
                    ┌─────────────┐     ┌─────────────┐
                    │ localStorage│<────│ JWT Token   │
                    │ + axios     │     │ mode=admin  │
                    │ headers     │     │ exp=7 days  │
                    └─────────────┘     └─────────────┘
                          │
                          │ router.push('/')
                          v
                    ┌─────────────┐
                    │ Dashboard   │
                    │ (protected) │
                    └─────────────┘
```

## Token Structure

JWT token payload for admin login:
```json
{
  "sub": "admin",
  "exp": 1735689600,
  "mode": "admin"
}
```

- `sub`: Username (always "admin" for admin login)
- `exp`: Expiration timestamp (7 days from creation)
- `mode`: Authentication mode ("admin", "email", or "prod")

## Storage Locations

| Data | Storage | Key | Lifespan |
|------|---------|-----|----------|
| JWT Token | localStorage | `token` | Until logout/expiry |
| User Profile | localStorage | `auth0_user` | Until logout |
| Admin Password | SQLite | `users.password_hash` | Permanent |
| Setup Status | SQLite | `system_settings.setup_completed` | Permanent |

## Error Handling

| Error Case | HTTP Status | Error Detail | UI Message |
|------------|-------------|--------------|------------|
| Setup not completed | 403 | `setup_required` | Redirected to /setup |
| Rate limited (M-005) | 429 | `Too many login attempts. Try again in {ttl} seconds.` | "Too many login attempts" |
| Invalid credentials | 401 | `Incorrect username or password` | "Invalid username or password" |
| Invalid hash format (M-003) | 401 | `Incorrect username or password` | "Invalid username or password" |
| Missing password | N/A | N/A | Button disabled |
| Network error | N/A | Connection error | "Failed to login" |

## Security Considerations

1. **Password Hashing (M-003)**: Admin password stored as bcrypt hash. Plaintext fallback removed - all passwords must be bcrypt hashed. Invalid hash formats are rejected.
2. **Rate Limiting (M-005)**: 5 login attempts per 10 minutes per IP address. Redis-based tracking with graceful degradation if Redis unavailable.
3. **JWT Secret**: `SECRET_KEY` should be set via environment variable
4. **Token Expiry**: 7-day token lifetime (configurable)
5. **HTTPS**: Required in production for secure credential transmission
6. **Cookie Security**: Token cookie has `SameSite=Strict` attribute
7. **Brute Force Protection**: Rate limiting prevents credential stuffing attacks. Successful login clears the attempt counter.

## Related Flows

- **Upstream**: [first-time-setup.md](first-time-setup.md) - Admin password creation
- **Alternative**: [email-authentication.md](email-authentication.md) - Email-based login
- **Downstream**: All authenticated endpoints use the JWT token

## Testing

### Prerequisites
- Fresh Trinity installation OR existing installation with setup completed
- Access to login page at http://localhost/login

### Test Steps

1. **First-time setup prerequisite**
   - If fresh install, navigate to `/setup` and set admin password
   - Remember the password for login testing

2. **Access admin login form**
   - Navigate to `/login`
   - Click "Admin Login" button at bottom of email form
   - **Expected**: Admin login form appears with fixed "admin" username

3. **Test invalid password**
   - Enter incorrect password
   - Click "Sign In as Admin"
   - **Expected**: Error message "Invalid username or password"

4. **Test valid login**
   - Enter correct admin password
   - Click "Sign In as Admin"
   - **Expected**: Redirected to Dashboard (`/`)

5. **Verify session persistence**
   - Refresh the page
   - **Expected**: Still authenticated, Dashboard loads

6. **Test token in localStorage**
   - Open browser DevTools > Application > Local Storage
   - **Expected**: `token` and `auth0_user` keys present

7. **Test logout**
   - Click user menu and select Logout
   - **Expected**: Redirected to `/login`, localStorage cleared

### Edge Cases

- Login before setup completed: Should redirect to `/setup`
- Empty password: Submit button should be disabled
- Token expiry: After 7 days, should redirect to `/login`
- Rate limited (M-005): After 5 failed attempts, returns 429 with TTL countdown
- Redis unavailable (M-005): Rate limiting bypassed with warning logged
- Invalid hash format (M-003): Returns 401 same as wrong password (no information leakage)

### Status

| Test Case | Status |
|-----------|--------|
| Admin login form access | Working |
| Invalid password rejection | Working |
| Valid password acceptance | Working |
| Session persistence | Working |
| Logout clears session | Working |
