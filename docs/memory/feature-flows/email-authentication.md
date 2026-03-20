# Feature: Email-Based Authentication

## Overview
Passwordless email-based authentication with verification codes. Users enter their email, receive a 6-digit code, and login without passwords. Includes admin-managed email whitelist and automatic whitelist addition when agents are shared. This is the **default authentication method** for Trinity (Phase 12.4).

**Implementation Status**: Complete (Backend + Frontend complete as of 2025-12-26)

**Login Page Simplified (2025-12-29)**: Removed Google/Auth0 and Developer Mode options. Login now offers:
1. **Email with code** (primary) - For whitelisted users
2. **Admin login** (secondary) - Fixed username 'admin', just enter password

## Revision History
| Date | Changes |
|------|---------|
| 2026-03-20 | **ROLE-001**: `get_or_create_email_user()` now assigns role `"creator"` (was `"user"`) to new email-authenticated users. Existing users unaffected. See [role-model.md](role-model.md). |
| 2026-02-23 | **Related security fixes**: M-003 removed plaintext password fallback (affects admin login path). M-005 added rate limiting to admin `/token` endpoint (5 attempts per 10 minutes per IP). See [admin-login.md](admin-login.md) for details. Email auth flow unchanged but now has consistent security posture with admin login. |
| 2026-01-23 | Verified line numbers, updated auth.py references (140-269), settings.py (429-503), db/email_auth.py (27-253) |
| 2025-12-29 | Login page simplified to email+admin only |
| 2025-12-26 | Initial implementation complete |

## User Story
As a user, I want to login with my email address and a verification code so that I don't need to manage passwords or OAuth providers.

As an admin, I want to control who can access the platform via an email whitelist so that I can manage access control independently of OAuth providers.

## Entry Points
- **UI**: `src/frontend/src/views/Login.vue:38-123` - Email login form with 2-step verification
- **UI**: `src/frontend/src/views/Settings.vue:291-390` - Email Whitelist management section
- **API**: `POST /api/auth/email/request` - Request verification code (lines 140-198 in auth.py)
- **API**: `POST /api/auth/email/verify` - Verify code and login (lines 201-268 in auth.py)
- **API**: `GET /api/settings/email-whitelist` - List whitelisted emails (lines 429-443 in settings.py)
- **API**: `POST /api/settings/email-whitelist` - Add email to whitelist (lines 446-478 in settings.py)
- **API**: `DELETE /api/settings/email-whitelist/{email}` - Remove from whitelist (lines 481-503 in settings.py)

---

## Architecture: Email Authentication Flow

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Email Auth Flow                              │
└─────────────────────────────────────────────────────────────────────┘

  User enters email                    Backend checks whitelist
┌────────────────────┐              ┌──────────────────────────┐
│  Login.vue         │              │  email_whitelist table   │
│  Email input       │              │  - id                    │
│  "Send Code" btn   │              │  - email                 │
└─────────┬──────────┘              │  - added_by              │
          │                         │  - added_at              │
          │ POST /api/auth/         │  - source (manual|       │
          │      email/request      │    agent_sharing)        │
          │                         └──────────┬───────────────┘
          v                                    │
┌─────────────────────────────────────────┐   │
│  auth.py:request_email_login_code()     │   │
│  1. Check setup_completed               │   │
│  2. Check EMAIL_AUTH_ENABLED            │   │
│  3. Verify email in whitelist  ─────────┼───┘
│  4. Rate limit check (3 per 10 min)     │
│  5. Generate 6-digit code               │
│  6. Store in email_login_codes table    │
│  7. Send email                          │
└─────────┬───────────────────────────────┘
          │
          v
┌─────────────────────────────────────────┐
│  email_service.py                       │
│  - console: Print to console (dev)      │
│  - smtp: Standard SMTP                  │
│  - sendgrid: SendGrid API               │
│  - resend: Resend API                   │
└─────────┬───────────────────────────────┘
          │
          │ User receives email
          v
┌────────────────────┐
│  Email: "Your      │
│  code is: 123456"  │
│  Expires in 10 min │
└─────────┬──────────┘
          │
          │ User enters code
          v
┌────────────────────┐              ┌──────────────────────────┐
│  Login.vue         │              │  email_login_codes       │
│  Code input        │              │  - id                    │
│  "Verify" btn      │              │  - email                 │
└─────────┬──────────┘              │  - code (6 digits)       │
          │                         │  - created_at            │
          │ POST /api/auth/         │  - expires_at            │
          │      email/verify       │  - verified (0|1)        │
          │                         │  - used_at               │
          v                         └──────────┬───────────────┘
┌─────────────────────────────────────────┐   │
│  auth.py:verify_email_login_code()      │   │
│  1. Check setup_completed               │   │
│  2. Check EMAIL_AUTH_ENABLED            │   │
│  3. Verify code and expiry  ────────────┼───┘
│  4. Mark code as verified               │
│  5. Get or create user (email = user)   │
│  6. Generate JWT with mode="email"      │
│  7. Audit log                           │
└─────────┬───────────────────────────────┘
          │
          v
┌────────────────────────────────────────┐
│  JWT Token returned                    │
│  {                                     │
│    "access_token": "eyJ...",           │
│    "token_type": "bearer",             │
│    "user": {                           │
│      "username": "user@example.com",   │
│      "email": "user@example.com",      │
│      "role": "user"                    │
│    }                                   │
│  }                                     │
└────────────────────────────────────────┘
```

---

## Auto-Whitelist on Agent Sharing

When an agent is shared with a new email, that email is automatically added to the whitelist:

```
┌─────────────────────────────────────────────────────────────────────┐
│                     Agent Sharing → Auto-Whitelist                   │
└─────────────────────────────────────────────────────────────────────┘

  Owner shares agent with email
┌────────────────────────────┐
│  AgentDetail.vue           │
│  "Share Agent" dialog      │
│  Enter email, click Share  │
└─────────┬──────────────────┘
          │
          │ POST /api/agents/{agent_name}/share
          │ { "email": "newuser@example.com" }
          │
          v
┌─────────────────────────────────────────┐
│  sharing.py:share_agent_endpoint()      │
│  1. Validate ownership                  │
│  2. Create share record                 │
│  3. Check if EMAIL_AUTH_ENABLED         │
│  4. If enabled: auto-add to whitelist   │
│     - db.add_to_whitelist()             │
│     - source="agent_sharing"            │
│  5. Audit log                           │
│  6. WebSocket broadcast                 │
└─────────┬───────────────────────────────┘
          │
          v
┌─────────────────────────────────────────┐
│  email_whitelist table updated          │
│  - email: newuser@example.com           │
│  - added_by: owner_id                   │
│  - source: "agent_sharing"              │
│  - added_at: 2025-12-26T10:00:00Z       │
└─────────────────────────────────────────┘

Result: New user can now login with email code
        without admin manually adding them
```

---

## Configuration

### Environment Variables (`src/backend/config.py`)

```python
# Lines 6-10: Email Authentication Mode (Phase 12.4)
EMAIL_AUTH_ENABLED = os.getenv("EMAIL_AUTH_ENABLED", "true").lower() == "true"

# Lines 43-51: Email Service Configuration
EMAIL_PROVIDER = os.getenv("EMAIL_PROVIDER", "resend")  # console|smtp|sendgrid|resend
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "noreply@trinity.example.com")
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY", "")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
```

**Setting can also be overridden via system_settings table:**
- Key: `email_auth_enabled`
- Value: `"true"` or `"false"`

---

## Frontend Layer

### State Management

**File**: `src/frontend/src/stores/auth.js`

| Lines | Component | Description |
|-------|-----------|-------------|
| 12 | `emailAuthEnabled` state | Boolean flag for email auth mode |
| 42-56 | `detectAuthMode()` | Sets `emailAuthEnabled` from `/api/auth/mode` |
| 48 | Auth mode logging | Logs EMAIL mode status to console |
| 165-184 | `requestEmailCode()` | Request verification code via email |
| 187-212 | `verifyEmailCode()` | Verify code and authenticate |

**Email Authentication Methods** (from `src/frontend/src/stores/auth.js`):

```javascript
// Lines 165-184: Request a verification code via email
async requestEmailCode(email) {
  if (!this.emailAuthEnabled) {
    this.authError = 'Email authentication is disabled'
    return { success: false, error: 'Email authentication is disabled' }
  }

  try {
    const response = await axios.post('/api/auth/email/request', { email })
    return {
      success: true,
      message: response.data.message,
      expiresInSeconds: response.data.expires_in_seconds
    }
  } catch (error) {
    console.error('Request email code failed:', error)
    const detail = error.response?.data?.detail || 'Failed to send verification code'
    this.authError = detail
    return { success: false, error: detail }
  }
}

// Lines 187-212: Verify email code and login
async verifyEmailCode(email, code) {
  if (!this.emailAuthEnabled) {
    this.authError = 'Email authentication is disabled'
    return false
  }

  try {
    const response = await axios.post('/api/auth/email/verify', { email, code })

    this.token = response.data.access_token
    this.user = response.data.user
    this.isAuthenticated = true

    localStorage.setItem('token', this.token)
    localStorage.setItem('auth0_user', JSON.stringify(this.user))
    this.setupAxiosAuth()

    console.log('📧 Email auth: authenticated as', this.user.email)
    return true
  } catch (error) {
    console.error('Verify email code failed:', error)
    const detail = error.response?.data?.detail || 'Invalid or expired verification code'
    this.authError = detail
    return false
  }
}
```

### Login Component

**File**: `src/frontend/src/views/Login.vue`

**Simplified (2025-12-29)**: Removed Google OAuth and Developer Mode. Two login methods only:

| Lines | Component | Description |
|-------|-----------|-------------|
| 38-123 | Email auth section | Default login method when email auth enabled |
| 40-65 | Step 1: Enter email | Email input with "Send Verification Code" button |
| 68-112 | Step 2: Enter code | 6-digit code input with countdown timer |
| 73-75 | Countdown timer | Displays "Code expires in MM:SS" |
| 115-122 | Admin Login button | Switch to admin password login |
| 126-171 | Admin login section | Fixed username 'admin', password-only form |
| 191-196 | Email state | `emailInput`, `codeInput`, `codeSent`, `countdown` refs |
| 198 | UI state | `showAdminLogin` ref |
| 214-218 | Timer formatting | `formatTime()` converts seconds to MM:SS |
| 221-233 | Countdown logic | `startCountdown()` with setInterval |
| 243-255 | `handleRequestCode()` | Submit email, start countdown |
| 257-267 | `handleVerifyCode()` | Submit code, redirect on success |
| 269-277 | `handleBackToEmail()` | Reset to step 1, clear timer |
| 294-304 | `handleAdminLogin()` | Admin login with fixed username 'admin' |

**2-Step UI Flow**:

```vue
<!-- Step 1: Email Input (Lines 40-65) -->
<div v-if="!codeSent">
  <form @submit.prevent="handleRequestCode" class="space-y-4">
    <div>
      <label for="email">Email Address</label>
      <input
        id="email"
        v-model="emailInput"
        type="email"
        required
        autocomplete="email"
        placeholder="you@example.com"
      />
    </div>
    <button type="submit" :disabled="loginLoading || !emailInput">
      {{ loginLoading ? 'Sending code...' : 'Send Verification Code' }}
    </button>
  </form>
</div>

<!-- Step 2: Code Verification (Lines 67-112) -->
<div v-else>
  <p>📧 We sent a 6-digit code to <strong>{{ emailInput }}</strong></p>
  <p v-if="countdown > 0">Code expires in {{ formatTime(countdown) }}</p>

  <form @submit.prevent="handleVerifyCode" class="space-y-4">
    <div>
      <label for="code">Verification Code</label>
      <input
        id="code"
        v-model="codeInput"
        type="text"
        required
        maxlength="6"
        pattern="[0-9]{6}"
        autocomplete="one-time-code"
        class="text-center text-2xl tracking-widest"
        placeholder="000000"
      />
    </div>
    <button type="submit" :disabled="loginLoading || codeInput.length !== 6">
      {{ loginLoading ? 'Verifying...' : 'Verify & Sign In' }}
    </button>
    <button type="button" @click="handleBackToEmail">
      ← Back to email
    </button>
  </form>
</div>
```

**Key Features**:
- Dark mode support via Tailwind classes
- Email autocomplete (`autocomplete="email"`)
- One-time-code autocomplete (`autocomplete="one-time-code"`)
- Countdown timer with MM:SS format
- 6-digit code input with validation (`maxlength="6"`, `pattern="[0-9]{6}"`)
- Loading states on buttons
- Admin Login option (lines 115-122) - fixed username 'admin', password only

### Settings Component - Email Whitelist

**File**: `src/frontend/src/views/Settings.vue`

| Lines | Component | Description |
|-------|-----------|-------------|
| 291-390 | Email Whitelist section | Complete whitelist management UI |
| 304-324 | Add email form | Input field + Add button |
| 327-383 | Whitelist table | Shows email, source, date, actions |
| 356-380 | Table rows | Iterates over `emailWhitelist` array |
| 361-366 | Source badges | Auto (Agent Sharing) or Manual |
| 372-378 | Remove button | DELETE action per email |
| 385-387 | Help text | "When you share an agent..." tip |
| 505-510 | Whitelist state | `emailWhitelist`, `newEmail`, loading flags |
| 738-802 | Whitelist methods | `loadEmailWhitelist()`, `addEmailToWhitelist()`, `removeEmailFromWhitelist()` |

**Whitelist Table UI**:

```vue
<!-- Lines 327-383 -->
<table class="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
  <thead>
    <tr>
      <th>Email</th>
      <th>Source</th>
      <th>Added</th>
      <th>Actions</th>
    </tr>
  </thead>
  <tbody>
    <tr v-if="loadingWhitelist">
      <td colspan="4">Loading...</td>
    </tr>
    <tr v-else-if="emailWhitelist.length === 0">
      <td colspan="4">No whitelisted emails. Add one above to get started.</td>
    </tr>
    <tr v-else v-for="entry in emailWhitelist" :key="entry.id">
      <td>{{ entry.email }}</td>
      <td>
        <span v-if="entry.source === 'agent_sharing'" class="badge-blue">
          🤝 Auto (Agent Sharing)
        </span>
        <span v-else class="badge-gray">
          ✋ Manual
        </span>
      </td>
      <td>{{ formatDate(entry.added_at) }}</td>
      <td>
        <button @click="removeEmailFromWhitelist(entry.email)">
          {{ removingEmail === entry.email ? 'Removing...' : 'Remove' }}
        </button>
      </td>
    </tr>
  </tbody>
</table>

<p class="text-xs text-gray-500 mt-2">
  💡 Tip: When you share an agent with someone by email, they're automatically added to this whitelist.
</p>
```

**Methods** (defined in script setup section):

```javascript
// Load whitelist from API
async function loadEmailWhitelist() {
  try {
    const response = await axios.get('/api/settings/email-whitelist', {
      headers: authStore.authHeader
    })
    emailWhitelist.value = response.data.whitelist || []
  } catch (e) {
    console.error('Failed to load email whitelist:', e)
  }
}

// Add email to whitelist
async function addEmailToWhitelist() {
  if (!newEmail.value) return

  addingEmail.value = true
  error.value = null

  try {
    await axios.post('/api/settings/email-whitelist', {
      email: newEmail.value,
      source: 'manual'
    }, {
      headers: authStore.authHeader
    })

    newEmail.value = ''
    await loadEmailWhitelist()
    showSuccessMessage()
  } catch (e) {
    error.value = e.response?.data?.detail || 'Failed to add email'
  } finally {
    addingEmail.value = false
  }
}

// Remove email from whitelist
async function removeEmailFromWhitelist(email) {
  if (!confirm(`Remove ${email} from whitelist?`)) return

  removingEmail.value = email
  error.value = null

  try {
    await axios.delete(`/api/settings/email-whitelist/${encodeURIComponent(email)}`, {
      headers: authStore.authHeader
    })

    await loadEmailWhitelist()
    showSuccessMessage()
  } catch (e) {
    error.value = e.response?.data?.detail || 'Failed to remove email'
  } finally {
    removingEmail.value = null
  }
}
```

---

## Backend Layer

### Database Schema (`src/backend/database.py:578-601, 651-653`)

**email_whitelist table:**
```sql
CREATE TABLE IF NOT EXISTS email_whitelist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    added_by TEXT NOT NULL,          -- User ID who added this email
    added_at TEXT NOT NULL,          -- ISO timestamp
    source TEXT NOT NULL,            -- "manual" or "agent_sharing"
    FOREIGN KEY (added_by) REFERENCES users(id)
)
```

**email_login_codes table:**
```sql
CREATE TABLE IF NOT EXISTS email_login_codes (
    id TEXT PRIMARY KEY,             -- Random token
    email TEXT NOT NULL,             -- Email address (lowercase)
    code TEXT NOT NULL,              -- 6-digit verification code
    created_at TEXT NOT NULL,        -- ISO timestamp
    expires_at TEXT NOT NULL,        -- ISO timestamp (created_at + 10 min)
    verified INTEGER DEFAULT 0,      -- 0 = unused, 1 = used
    used_at TEXT                     -- ISO timestamp when verified
)
```

**Indexes:**
```sql
CREATE INDEX idx_email_whitelist_email ON email_whitelist(email)
CREATE INDEX idx_email_login_codes_email ON email_login_codes(email)
CREATE INDEX idx_email_login_codes_code ON email_login_codes(code)
```

### Database Operations (`src/backend/db/email_auth.py`)

**EmailAuthOperations class** (injected with UserOperations dependency):

| Method | Line | Purpose |
|--------|------|---------|
| `is_email_whitelisted(email)` | 27-35 | Check if email is in whitelist |
| `add_to_whitelist(email, added_by, source)` | 37-67 | Add email to whitelist |
| `remove_from_whitelist(email)` | 69-83 | Remove email from whitelist |
| `list_whitelist(limit)` | 85-104 | Get all whitelisted emails with metadata |
| `create_login_code(email, expiry_minutes)` | 110-145 | Generate 6-digit code with 10 min expiry |
| `verify_login_code(email, code)` | 147-190 | Verify code, mark as used, return result |
| `count_recent_code_requests(email, minutes)` | 192-205 | Rate limiting check |
| `cleanup_old_codes(days)` | 207-220 | Cleanup expired codes (housekeeping) |
| `get_or_create_email_user(email)` | 226-253 | Create user account from email |

### Authentication Endpoints (`src/backend/routers/auth.py`)

#### POST /api/auth/email/request (Lines 140-198)

**Request verification code - Unauthenticated endpoint**

```python
@router.post("/api/auth/email/request")
async def request_email_login_code(request: Request):
    """
    Request a login code via email.

    Unauthenticated endpoint. Sends a 6-digit code to the provided email
    if it's in the whitelist.

    Rate limit: 3 requests per 10 minutes per email.
    """
```

**Request Body:**
```json
{
  "email": "user@example.com"
}
```

**Business Logic:**
1. Check if setup is completed (lines 153-158)
2. Check if email auth is enabled (lines 160-166)
3. Parse and lowercase email (lines 168-171)
4. Check if email is whitelisted (lines 173-177)
   - If not whitelisted: Return generic success message (prevent enumeration)
5. Check rate limit: 3 requests per 10 minutes (lines 179-185)
   - If exceeded: Return 429
6. Generate 6-digit code (line 188)
   - `db.create_login_code(email, expiry_minutes=10)`
7. Send email via EmailService (lines 190-192)
8. Return success response (lines 194-198)

**Response:**
```json
{
  "success": true,
  "message": "Verification code sent to your email",
  "expires_in_seconds": 600
}
```

**Security:** Returns generic success message even if email is not whitelisted (prevents email enumeration).

#### POST /api/auth/email/verify (Lines 201-268)

**Verify code and get JWT - Unauthenticated endpoint**

```python
@router.post("/api/auth/email/verify")
async def verify_email_login_code(request: Request):
    """
    Verify email login code and get JWT token.

    Unauthenticated endpoint. Verifies the code and creates/returns user session.
    """
```

**Request Body:**
```json
{
  "email": "user@example.com",
  "code": "123456"
}
```

**Business Logic:**
1. Check if setup is completed (lines 210-215)
2. Check if email auth is enabled (lines 217-223)
3. Parse request (lines 225-229)
4. Verify code (line 232)
   - `db.verify_login_code(email, code)`
   - Checks: code exists, not already used, not expired
   - Marks code as verified with `used_at` timestamp
5. If invalid: return 401 (lines 233-237)
6. Get or create user (lines 239-245)
   - `db.get_or_create_email_user(email)`
   - Username = email (lowercase)
   - Role = "user"
   - No password set (email auth only)
7. Update last login (line 248)
8. Create JWT token (lines 250-256)
   - 7-day expiry (ACCESS_TOKEN_EXPIRE_MINUTES = 10080)
   - Include `mode="email"` claim
9. Return token and user profile (lines 258-268)

**Response:**
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer",
  "user": {
    "username": "user@example.com",
    "email": "user@example.com",
    "role": "user",
    "name": null,
    "picture": null
  }
}
```

### Whitelist Management Endpoints (`src/backend/routers/settings.py`)

All whitelist endpoints are **admin-only** (require `role="admin"`).

#### GET /api/settings/email-whitelist (Lines 429-443)

List all whitelisted emails.

**Response:**
```json
{
  "whitelist": [
    {
      "id": 1,
      "email": "admin@example.com",
      "added_by": "1",
      "added_by_username": "admin",
      "added_at": "2025-12-26T10:00:00Z",
      "source": "manual"
    },
    {
      "id": 2,
      "email": "user@example.com",
      "added_by": "1",
      "added_by_username": "admin",
      "added_at": "2025-12-26T11:00:00Z",
      "source": "agent_sharing"
    }
  ]
}
```

#### POST /api/settings/email-whitelist (Lines 446-478)

Add email to whitelist.

**Request Body:**
```json
{
  "email": "newuser@example.com",
  "source": "manual"
}
```

**Business Logic:**
1. Parse request and lowercase email (lines 460-463)
2. Add to whitelist (lines 466-467)
   - `db.add_to_whitelist(email, current_user.username, source)`
   - Returns `False` if already exists
3. If duplicate: return 409 Conflict (lines 469-472)
4. Return success (line 475)

**Response:**
```json
{
  "success": true,
  "email": "newuser@example.com"
}
```

#### DELETE /api/settings/email-whitelist/{email} (Lines 481-503)

Remove email from whitelist.

**Response:**
```json
{
  "success": true,
  "email": "user@example.com"
}
```

### Agent Sharing Auto-Whitelist (`src/backend/routers/sharing.py:44-56`)

When an agent is shared, automatically add recipient to whitelist:

```python
# Lines 44-56: Auto-add email to whitelist if email auth is enabled
from config import EMAIL_AUTH_ENABLED
email_auth_setting = db.get_setting_value("email_auth_enabled", str(EMAIL_AUTH_ENABLED).lower())
if email_auth_setting.lower() == "true":
    try:
        db.add_to_whitelist(
            share_request.email,
            current_user.username,
            source="agent_sharing"
        )
    except Exception:
        # Already whitelisted or error - continue anyway
        pass
```

**Key behavior:**
- Runs after successful share creation
- Silent failure if email already whitelisted
- Source is marked as `"agent_sharing"` for audit trail
- Does NOT block agent sharing if whitelist add fails

---

## Email Service (`src/backend/services/email_service.py`)

**EmailService class** supports 4 providers:

| Provider | Config | Use Case |
|----------|--------|----------|
| `console` | Default | Development - prints to console/logs |
| `smtp` | SMTP_HOST, SMTP_USER, SMTP_PASSWORD | Standard email server |
| `sendgrid` | SENDGRID_API_KEY | SendGrid API |
| `resend` | RESEND_API_KEY | Resend API (recommended) |

### Key Methods

| Method | Line | Purpose |
|--------|------|---------|
| `send_verification_code(to_email, code)` | 42-56 | Send 6-digit code email |
| `send_email(to_email, subject, body, html_body)` | 58-92 | Generic email sender |
| `_send_console(...)` | 103-114 | Print to console (dev mode) |
| `_send_smtp(...)` | 116-153 | Send via SMTP |
| `_send_sendgrid(...)` | 155-203 | Send via SendGrid API |
| `_send_resend(...)` | 205-253 | Send via Resend API |

### Email Template

```text
Subject: Your verification code

Your verification code is: 123456

This code expires in 10 minutes.

If you didn't request this code, you can safely ignore this email.
```

**Note:** HTML template can be added as optional `html_body` parameter.

---

## Data Models (`src/backend/db_models.py`)

### EmailWhitelistEntry (Lines 380-387)
```python
class EmailWhitelistEntry(BaseModel):
    id: int
    email: str
    added_by: str                    # User ID
    added_by_username: Optional[str] = None
    added_at: datetime
    source: str                      # "manual", "agent_sharing"
```

### EmailWhitelistAdd (Lines 390-393)
```python
class EmailWhitelistAdd(BaseModel):
    email: str
    source: str = "manual"
```

### EmailLoginRequest (Lines 396-398)
```python
class EmailLoginRequest(BaseModel):
    email: str
```

### EmailLoginVerify (Lines 401-404)
```python
class EmailLoginVerify(BaseModel):
    email: str
    code: str  # 6-digit code
```

### EmailLoginResponse (Lines 407-411)
```python
class EmailLoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict  # User profile
```

---

## Security Considerations

### Email Enumeration Prevention

The system prevents attackers from discovering which emails are registered:

1. **Generic success message**: `/api/auth/email/request` returns success even for non-whitelisted emails
2. **Audit logging**: Failed attempts are logged with `result="denied"`, `reason="not_whitelisted"`
3. **Rate limiting**: Prevents brute-force enumeration attempts

**Example - Non-whitelisted email:**
```python
# Lines 173-177 in auth.py
if not db.is_email_whitelisted(email):
    # Return success to prevent email enumeration
    return {"success": True, "message": "If your email is registered, you'll receive a code shortly"}
```

### Rate Limiting

**3 code requests per 10 minutes per email:**

```python
# Lines 179-185 in auth.py
recent_requests = db.count_recent_code_requests(email, minutes=10)
if recent_requests >= 3:
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Too many requests. Please try again in 10 minutes"
    )
```

### Code Expiration

- Codes expire 10 minutes after creation
- Expired codes are rejected during verification
- Old codes are cleaned up by `cleanup_old_codes()` (can be run as scheduled task)

### Single-Use Codes

- Codes marked as `verified=1` after successful use
- `used_at` timestamp recorded
- Subsequent attempts with same code fail

### JWT Token Security

- 7-day expiration (configurable via `ACCESS_TOKEN_EXPIRE_MINUTES`)
- Tokens include `mode="email"` claim for tracking
- SECRET_KEY must be set in production (auto-generated warning on startup)

### Audit Logging

All operations are logged to audit service:

| Event Type | Action | Details |
|------------|--------|---------|
| `authentication` | `email_login_request` | Code request (success/denied) |
| `authentication` | `email_login_code_sent` | Email sent status |
| `authentication` | `email_login_verify` | Code verification (success/failed) |
| `authentication` | `email_login` | Successful login |
| `email_whitelist` | `list` | List whitelist |
| `email_whitelist` | `add` | Add email to whitelist |
| `email_whitelist` | `remove` | Remove email from whitelist |
| `agent_sharing` | `share` | Agent shared (triggers auto-whitelist) |

---

## Error Handling

| Error Case | HTTP Status | Message | Handling |
|------------|-------------|---------|----------|
| Setup not completed | 403 | `setup_required` | Block login until first-time setup done |
| Email auth disabled | 403 | `Email authentication is disabled` | Check `EMAIL_AUTH_ENABLED` setting |
| Email not whitelisted | 200 | Generic success (prevents enumeration) | Logs audit event with `result="denied"` |
| Rate limit exceeded | 429 | `Too many requests. Please try again in 10 minutes` | 3 requests per 10 min per email |
| Invalid code | 401 | `Invalid or expired verification code` | Code doesn't exist, expired, or already used |
| Code expired | 401 | `Invalid or expired verification code` | Code older than 10 minutes |
| User creation failed | 500 | `Failed to create user account` | Database error |
| Email already whitelisted | 409 | `Email {email} is already whitelisted` | Duplicate email in POST whitelist |
| Email not in whitelist | 404 | `Email {email} not found in whitelist` | DELETE non-existent email |
| User not found (whitelist add) | 400 | `User not found: {username}` | Invalid `added_by` user |

---

## Configuration Examples

### Development Setup (Console Email)

```yaml
# docker-compose.yml
backend:
  environment:
    - EMAIL_AUTH_ENABLED=true
    - EMAIL_PROVIDER=console  # Prints codes to logs
```

**Code appears in console:**
```
[EMAIL to user@example.com] Subject: Your verification code
Body: Your verification code is: 123456

This code expires in 10 minutes.
```

### Production Setup (Resend)

```yaml
# docker-compose.yml
backend:
  environment:
    - EMAIL_AUTH_ENABLED=true
    - EMAIL_PROVIDER=resend
    - RESEND_API_KEY=re_xxxxxxxxxxxxx
    - SMTP_FROM=noreply@yourdomain.com
```

### Production Setup (SMTP)

```yaml
backend:
  environment:
    - EMAIL_AUTH_ENABLED=true
    - EMAIL_PROVIDER=smtp
    - SMTP_HOST=smtp.gmail.com
    - SMTP_PORT=587
    - SMTP_USER=your-email@gmail.com
    - SMTP_PASSWORD=your-app-password
    - SMTP_FROM=noreply@yourdomain.com
```

### Disabling Email Auth

To disable email authentication and use only admin login:

**Via environment variable:**
```yaml
backend:
  environment:
    - EMAIL_AUTH_ENABLED=false
```

**Via system settings (runtime toggle):**
```bash
# Via API or database
curl -X PUT /api/settings/email_auth_enabled \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"value": "false"}'
```

---

## Related Flows

### Upstream Dependencies
- **First-Time Setup** ([first-time-setup.md](first-time-setup.md)) - Must complete setup before login

### Downstream Flows
- **Agent Sharing** ([agent-sharing.md](agent-sharing.md)) - Auto-adds emails to whitelist
- **User Management** - Email becomes username for email auth users

### Interaction with Other Auth Methods

**Simplified Login (2025-12-29)**: The login page now offers only two options:

| Mode | Priority | Use Case |
|------|----------|----------|
| Email Auth | Default (Primary) | Passwordless login for whitelisted users |
| Admin Login | Secondary | Admin password login (username fixed as 'admin') |

**Note**: Google OAuth and Developer Mode options were removed from the UI. The backend still supports these modes for API access, but they are no longer exposed in the login page.

**Detection endpoint** (`GET /api/auth/mode`) returns:
```json
{
  "email_auth_enabled": true,
  "setup_completed": true
}
```

---

## Testing

### Prerequisites
- Backend running with `EMAIL_AUTH_ENABLED=true`
- Email service configured (use `EMAIL_PROVIDER=console` for dev)
- At least one admin user exists (via first-time setup)

### Manual Testing Steps

#### 1. Frontend: Email Login Flow

**Action**:
1. Open browser to `http://localhost:3000/login`
2. Verify email authentication form is displayed (default method)
3. Enter email: `testuser@example.com`
4. Click "Send Verification Code"

**Expected**:
- Button changes to "Sending code..."
- After 1-2 seconds, UI switches to Step 2 (code input)
- Email sent message appears: "📧 We sent a 6-digit code to testuser@example.com"
- Countdown timer starts: "Code expires in 10:00"
- Backend logs show code (if EMAIL_PROVIDER=console)

**Verify**:
- Check browser console for `[GitPanel]` style logs
- Check backend logs for email service output
- Countdown should decrease every second

**Action (continued)**:
5. Enter 6-digit code from email/logs
6. Click "Verify & Sign In"

**Expected**:
- Button changes to "Verifying..."
- After 1-2 seconds, redirect to dashboard (`/`)
- Console logs: `📧 Email auth: authenticated as testuser@example.com`
- User menu shows email address

**Verify**:
```bash
# Check localStorage
localStorage.getItem('token')  # Should have JWT
localStorage.getItem('auth0_user')  # Should have user object with email
```

#### 2. Frontend: Countdown Timer Test

**Action**:
1. Login page, enter email, send code
2. Wait and watch countdown timer

**Expected**:
- Timer starts at "10:00" (600 seconds)
- Decrements every second: "9:59", "9:58", etc.
- Format is always MM:SS (zero-padded seconds)
- When timer reaches 0:00, stops (code expired)

**Verify**: Code should actually be expired after 10 minutes (backend validation)

#### 3. Frontend: Back to Email Button

**Action**:
1. Request code (Step 2 displayed)
2. Click "← Back to email" button

**Expected**:
- UI returns to Step 1 (email input)
- Code input cleared
- Countdown timer stopped and reset
- Email field retains previous value

#### 4. Frontend: Validation Tests

**Action**:
1. Try to submit email with empty field
2. Try to submit code with < 6 digits
3. Try to enter letters in code field

**Expected**:
- "Send Verification Code" button disabled when email empty
- "Verify & Sign In" button disabled when code length !== 6
- Code input accepts only numbers (pattern="[0-9]{6}")
- Browser validation enforces email format

#### 5. Frontend: Admin Login

**Action**:
1. On email login page, click "🔐 Admin Login" button
2. Verify username field shows "admin" (fixed, non-editable)
3. Enter admin password
4. Click "Sign In as Admin"

**Expected**:
- Admin login form shows fixed username "admin"
- Password field accepts input
- "← Back to email login" link returns to email form
- Successful login redirects to dashboard

#### 6. Frontend: Settings - Email Whitelist Management

**Action**:
1. Login as admin
2. Navigate to Settings page (`/settings`)
3. Scroll to "Email Whitelist" section

**Expected**:
- Section title: "Email Whitelist"
- Description: "Manage whitelisted emails for email-based authentication..."
- Input field + "Add Email" button
- Table with columns: Email, Source, Added, Actions
- Empty state message if no emails

**Action (continued)**:
4. Enter email: `newuser@example.com`
5. Click "Add Email"

**Expected**:
- Button shows "Adding..." with spinner
- After 1-2 seconds, success message appears
- Email appears in table
- Source badge shows "✋ Manual"
- Date shows current time
- Input field cleared

**Action (continued)**:
6. Click "Remove" button on an email

**Expected**:
- Browser confirm dialog: "Remove {email} from whitelist?"
- If confirmed, button shows "Removing..."
- After 1-2 seconds, email disappears from table
- Success message appears

**Verify**:
```bash
# Check database
sqlite3 ~/trinity-data/trinity.db "SELECT * FROM email_whitelist"
```

#### 7. Backend: Admin Adds Email to Whitelist (API)

**Action:**
```bash
# Login as admin first
curl -X POST http://localhost:8000/api/token \
  -d "username=admin&password=your-password"

export TOKEN="<jwt-token>"

# Add email to whitelist
curl -X POST http://localhost:8000/api/settings/email-whitelist \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"email": "testuser@example.com", "source": "manual"}'
```

**Expected:**
```json
{
  "success": true,
  "email": "testuser@example.com"
}
```

**Verify:**
```bash
curl -X GET http://localhost:8000/api/settings/email-whitelist \
  -H "Authorization: Bearer $TOKEN"
```

#### 8. Backend: User Requests Login Code (API)

**Action:**
```bash
curl -X POST http://localhost:8000/api/auth/email/request \
  -H "Content-Type: application/json" \
  -d '{"email": "testuser@example.com"}'
```

**Expected:**
```json
{
  "success": true,
  "message": "Verification code sent to your email",
  "expires_in_seconds": 600
}
```

**Verify:**
- Check backend logs (if EMAIL_PROVIDER=console)
- Check email inbox (if using real email provider)
- Code should be 6 digits: `123456`

#### 9. Backend: User Verifies Code (API)

**Action:**
```bash
curl -X POST http://localhost:8000/api/auth/email/verify \
  -H "Content-Type: application/json" \
  -d '{"email": "testuser@example.com", "code": "123456"}'
```

**Expected:**
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer",
  "user": {
    "username": "testuser@example.com",
    "email": "testuser@example.com",
    "role": "user",
    "name": null,
    "picture": null
  }
}
```

**Verify:**
```bash
# Use token to access authenticated endpoint
curl -X GET http://localhost:8000/api/agents \
  -H "Authorization: Bearer <access-token>"
```

#### 10. Backend: Rate Limiting Test

**Action:**
```bash
# Send 4 code requests within 10 minutes
for i in {1..4}; do
  curl -X POST http://localhost:8000/api/auth/email/request \
    -H "Content-Type: application/json" \
    -d '{"email": "testuser@example.com"}'
  echo ""
done
```

**Expected:**
- First 3 requests: Success
- 4th request: `429 Too Many Requests`

**Verify:**
```json
{
  "detail": "Too many requests. Please try again in 10 minutes"
}
```

#### 11. Backend: Email Enumeration Prevention Test

**Action:**
```bash
# Try to request code for non-whitelisted email
curl -X POST http://localhost:8000/api/auth/email/request \
  -H "Content-Type: application/json" \
  -d '{"email": "notwhitelisted@example.com"}'
```

**Expected:**
```json
{
  "success": true,
  "message": "If your email is registered, you'll receive a code shortly"
}
```

**Verify:**
- Response looks the same as whitelisted email (no enumeration)
- Check audit logs for `result="denied"`, `reason="not_whitelisted"`

#### 12. Frontend: Auto-Whitelist on Agent Share

**Action:**
```bash
# Share agent with new email
curl -X POST http://localhost:8000/api/agents/test-agent/share \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"email": "newuser@example.com"}'
```

**Expected:**
```json
{
  "id": 2,
  "agent_name": "test-agent",
  "shared_with_email": "newuser@example.com",
  "shared_by_id": 1,
  "shared_by_email": "admin@example.com",
  "created_at": "2025-12-26T12:00:00Z"
}
```

**Verify:**
```bash
# Check whitelist - newuser@example.com should be added
curl -X GET http://localhost:8000/api/settings/email-whitelist \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

**Expected whitelist entry:**
```json
{
  "id": 3,
  "email": "newuser@example.com",
  "added_by": "1",
  "added_by_username": "admin",
  "added_at": "2025-12-26T12:00:00Z",
  "source": "agent_sharing"
}
```

#### 13. Backend: Code Expiration Test

**Action:**
```bash
# Request code
curl -X POST http://localhost:8000/api/auth/email/request \
  -H "Content-Type: application/json" \
  -d '{"email": "testuser@example.com"}'

# Wait 11 minutes (or modify expires_at in database)

# Try to verify expired code
curl -X POST http://localhost:8000/api/auth/email/verify \
  -H "Content-Type: application/json" \
  -d '{"email": "testuser@example.com", "code": "123456"}'
```

**Expected:**
```json
{
  "detail": "Invalid or expired verification code"
}
```

#### 14. Backend: Single-Use Code Test

**Action:**
```bash
# Request and verify code successfully
CODE=$(curl -X POST http://localhost:8000/api/auth/email/request \
  -H "Content-Type: application/json" \
  -d '{"email": "testuser@example.com"}' | grep code)

# Verify once (success)
curl -X POST http://localhost:8000/api/auth/email/verify \
  -H "Content-Type: application/json" \
  -d '{"email": "testuser@example.com", "code": "'$CODE'"}'

# Try to verify same code again (fail)
curl -X POST http://localhost:8000/api/auth/email/verify \
  -H "Content-Type: application/json" \
  -d '{"email": "testuser@example.com", "code": "'$CODE'"}'
```

**Expected on 2nd attempt:**
```json
{
  "detail": "Invalid or expired verification code"
}
```

### Audit Log Verification

Check audit logger for complete event trail:

```bash
# View audit logs
curl -X GET http://localhost:8001/api/audit/events?limit=50

# Filter for email auth events
curl -X GET "http://localhost:8001/api/audit/events?event_type=authentication&action=email_login"
```

**Expected events:**
1. `email_login_request` - Code requested
2. `email_login_code_sent` - Email sent
3. `email_login_verify` - Code verification attempt
4. `email_login` - Successful login
5. `email_whitelist/add` - Email added to whitelist
6. `agent_sharing/share` - Agent shared (if testing auto-whitelist)

### Edge Cases

| Scenario | Expected Behavior | UI/API |
|----------|------------------|--------|
| Email auth disabled | 403 "Email authentication is disabled" | Error message in Login.vue |
| Setup not completed | 403 "setup_required" | Blocked by first-time setup check |
| Invalid email format | Browser validation error | HTML5 `type="email"` validation |
| Code with wrong email | 401 "Invalid or expired verification code" | Red error message below code input |
| Database error during user creation | 500 "Failed to create user account" | Error message in Login.vue |
| Email service down | Code created but email fails (audit log shows `email_sent=false`) | User sees success but doesn't receive email |
| Countdown reaches zero | Code still works until backend expiry (10 min) | Timer shows "0:00", no UI enforcement |
| Network interruption during verify | Axios error | Red error message, user can retry |
| Duplicate email in whitelist | 409 "Email already whitelisted" | Error message in Settings page |
| Non-admin tries to manage whitelist | 403 or redirect | Settings page requires admin role |

### Status

- **Backend Implementation**: Complete (Phase 12.4, 2025-12-26)
- **Frontend Implementation**: Complete (2025-12-26)
  - `src/frontend/src/stores/auth.js` - Email auth methods (lines 165-212)
  - `src/frontend/src/views/Login.vue` - 2-step email login UI (lines 38-123)
  - `src/frontend/src/views/Settings.vue` - Email Whitelist management (lines 291-390)
- **Email Service**: Complete (console, SMTP, SendGrid, Resend)
- **Auto-Whitelist**: Complete (agent sharing integration)
- **Security**: Complete (rate limiting, enumeration prevention, code expiration)
- **UI/UX Features**: Complete (countdown timer, dark mode, autocomplete, loading states)

---

## Implementation Checklist

### Backend (Completed 2025-12-26)
- [x] Database schema (email_whitelist, email_login_codes)
- [x] EmailAuthOperations class
- [x] POST /api/auth/email/request endpoint
- [x] POST /api/auth/email/verify endpoint
- [x] GET /api/settings/email-whitelist endpoint
- [x] POST /api/settings/email-whitelist endpoint
- [x] DELETE /api/settings/email-whitelist/{email} endpoint
- [x] Email service (console, SMTP, SendGrid, Resend)
- [x] Rate limiting (3 per 10 min)
- [x] Email enumeration prevention
- [x] Code expiration (10 minutes)
- [x] Single-use codes
- [x] Audit logging
- [x] Auto-whitelist on agent sharing
- [x] Runtime enable/disable via settings
- [x] Route ordering fix (whitelist routes before catch-all)

### Frontend (Completed 2025-12-26)
- [x] Add `emailAuthEnabled` to auth store state (line 12)
- [x] Add `requestEmailCode()` method to auth store (lines 165-184)
- [x] Add `verifyEmailCode()` method to auth store (lines 187-212)
- [x] Update Login.vue with email auth form (lines 38-123)
  - [x] Step 1: Email input + "Send code" button
  - [x] Step 2: Code input (6 digits) + "Verify" button
  - [x] Countdown timer with MM:SS display
  - [x] Error handling and loading states
  - [x] Dark mode support
  - [x] Autocomplete attributes (email, one-time-code)
- [x] Add Email Whitelist section to Settings.vue (lines 291-390)
  - [x] List whitelist with source badges (Auto/Manual)
  - [x] Add email form with inline input
  - [x] Remove email confirmation
  - [x] Admin-only access (Settings page requires admin)
  - [x] Loading states and error handling
- [x] GET /api/auth/mode returns `email_auth_enabled` (line 45 in auth.js)
- [x] Complete flow tested end-to-end

### Documentation (Completed 2025-12-26)
- [x] Feature flow document (complete with frontend implementation)
- [x] Security considerations
- [x] Testing instructions (backend + frontend)
- [x] Configuration examples

---

## Notes

### Frontend Architecture

**Login Flow State Machine**:
1. **Mode Detection**: `detectAuthMode()` calls `/api/auth/mode` to determine available auth methods
2. **Default Display**: Email auth shown by default if `emailAuthEnabled === true`
3. **Alternative Methods**: Admin login via button below email form
4. **State Persistence**: JWT and user stored in localStorage, restored on page load

**Countdown Timer Implementation**:
- Uses `setInterval()` with 1-second tick (lines 221-233 in Login.vue)
- Cleanup on component unmount to prevent memory leaks (lines 236-240)
- Timer is UI-only - backend expiry is authoritative (10 minutes)

**Dark Mode Support**:
- All components use Tailwind dark mode classes (`dark:bg-gray-800`, etc.)
- Consistent with existing Trinity UI design system

### Why Email = Username?

Email authentication users have their email as their username for simplicity:
- Username = email (lowercase)
- No password field (email auth only)
- Role defaults to "user"
- Can still be looked up by email or username

This is implemented in `get_or_create_email_user()`:

```python
# Lines 243-248 in db/email_auth.py
username = email.lower()

cursor.execute("""
    INSERT INTO users (username, email, role, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?)
""", (username, email.lower(), "user", now, now))
```

### JWT Mode Claim

All email auth JWTs include `mode="email"` for tracking:

```python
# Lines 252-256 in routers/auth.py
access_token = create_access_token(
    data={"sub": user["username"]},
    expires_delta=access_token_expires,
    mode="email"  # Mark as email auth token
)
```

This allows distinguishing between:
- `mode="admin"` - Admin login (username/password)
- `mode="email"` - Email verification code (default)

### Source Tracking

The whitelist tracks how emails were added (visible in Settings UI):
- `source="manual"` - ✋ Manual badge - Added by admin via Settings page
- `source="agent_sharing"` - 🤝 Auto badge - Auto-added when agent was shared

This provides an audit trail and helps admins understand whitelist growth.

### Cleanup Strategy

Old verification codes should be cleaned up periodically:

```python
# Can be run as a scheduled task (e.g., daily cron)
db.cleanup_old_codes(days=1)  # Delete codes older than 1 day
```

This is not currently scheduled but can be added to the System Agent or a separate cleanup task.

### UI/UX Decisions

**Autocomplete Attributes**:
- `autocomplete="email"` on email input - helps password managers recognize field
- `autocomplete="one-time-code"` on code input - iOS/Android can auto-fill from SMS

**Code Input Styling**:
- Large text (text-2xl) for easy reading
- Monospace with letter-spacing (tracking-widest) for 6-digit appearance
- Centered text for visual appeal
- Pattern validation prevents non-numeric input

**Loading States**:
- All async actions show loading spinners
- Buttons disable during loading to prevent double-submit
- Loading text changes button label ("Sending code...", "Verifying...")

**Error Handling**:
- Errors displayed inline below form elements
- Red color scheme for errors
- authStore.clearError() called before new operations
- Retry button clears all state and returns to Step 1
