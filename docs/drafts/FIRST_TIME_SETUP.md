# First-Time Setup & In-App API Key Configuration

> **Status**: Draft
> **Created**: 2025-12-23
> **Priority**: High
> **Requirement ID**: 12.3

## Overview

Two independent features to improve Trinity's installation experience:

1. **First-time password setup** - Simple screen to set admin password on first launch
2. **In-app Anthropic key** - Configure API key from Settings (no `.env` editing)

---

## Feature 1: First-Time Admin Password Setup

### User Story

As a new Trinity user, I want to set my admin password on first login so that I don't have to edit configuration files.

### Proposed Flow

```
User opens http://localhost:3000
         ↓
Backend checks: Is setup_completed?
         ↓
    ┌────┴────┐
    │   No    │ ──→ Show "Set Admin Password" screen
    └─────────┘              ↓
                    User enters password
                             ↓
                    Backend saves (hashed)
                             ↓
                    Redirect to login
                             ↓
    ┌────┴────┐
    │   Yes   │ ──→ Show normal login
    └─────────┘
```

### UI Design

```
┌─────────────────────────────────────────────────────┐
│                                                     │
│              🔐 Set Admin Password                  │
│                                                     │
│   Create a password for the admin account.          │
│                                                     │
│   Password                                          │
│   ┌─────────────────────────────────────┐          │
│   │ ••••••••••••                    👁️ │          │
│   └─────────────────────────────────────┘          │
│                                                     │
│   Confirm Password                                  │
│   ┌─────────────────────────────────────┐          │
│   │ ••••••••••••                    👁️ │          │
│   └─────────────────────────────────────┘          │
│   ✓ Passwords match                                 │
│                                                     │
│              [Set Password & Continue]              │
│                                                     │
└─────────────────────────────────────────────────────┘
```

### Technical Design

#### Backend Changes

**New endpoint: `GET /api/setup/status`**
```python
@router.get("/api/setup/status")
async def get_setup_status():
    """Check if initial setup is complete. No auth required."""
    setup_completed = get_setting_value('setup_completed', 'false') == 'true'
    return {"setup_completed": setup_completed}
```

**New endpoint: `POST /api/setup/admin-password`**
```python
@router.post("/api/setup/admin-password")
async def set_admin_password(data: SetAdminPasswordRequest):
    """Set admin password on first launch. No auth required, only works once."""
    # Check setup not already completed
    if get_setting_value('setup_completed', 'false') == 'true':
        raise HTTPException(403, "Setup already completed")

    # Validate password
    if len(data.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    if data.password != data.confirm_password:
        raise HTTPException(400, "Passwords do not match")

    # Update admin user with hashed password
    hashed = bcrypt.hashpw(data.password.encode(), bcrypt.gensalt())
    db.update_user_password('admin', hashed.decode())

    # Mark setup as completed
    set_setting('setup_completed', 'true')

    return {"success": True}
```

**Modified: `POST /api/token` (login)**
- If `setup_completed != true`, return 403 `{"detail": "setup_required"}`

**Security: Add bcrypt hashing**
- Add `bcrypt` to `requirements.txt`
- Update `verify_password()` to use bcrypt
- Update `_ensure_admin_user()` to use bcrypt

#### Frontend Changes

**New component: `SetupPassword.vue`**
- Simple form: password + confirm + submit
- Show/hide password toggles
- Basic validation (min 8 chars, match)
- On success: redirect to `/login`

**New route: `/setup`**
- Public (no auth)
- Shows `SetupPassword.vue`
- If setup already complete, redirect to `/login`

**Modified: Router guard**
- On app load, check `GET /api/setup/status`
- If `setup_completed = false`, redirect to `/setup`

### Acceptance Criteria

- [ ] Fresh install shows password setup screen
- [ ] Password must be at least 8 characters
- [ ] Password confirmation must match
- [ ] Password stored with bcrypt hashing
- [ ] After setup, screen never shown again
- [ ] Cannot access `/setup` after completion
- [ ] Login blocked until setup completed

---

## Feature 2: In-App Anthropic API Key Configuration

### User Story

As a Trinity admin, I want to configure the Anthropic API key from the Settings page so that I don't have to edit environment files.

### Current State

- `ANTHROPIC_API_KEY` set in `.env` file
- Passed to backend via docker-compose
- Injected into agent containers on creation
- Requires restart to change

### Proposed Flow

```
Admin goes to Settings
         ↓
Sees "API Keys" section
         ↓
Enters Anthropic key → [Test] → [Save]
         ↓
New agents get key from settings
```

### UI Design (Settings Page Addition)

```
┌─────────────────────────────────────────────────────┐
│ Settings                                            │
├─────────────────────────────────────────────────────┤
│                                                     │
│ API Keys                                            │
│ ─────────────────────────────────────────────────── │
│                                                     │
│ Anthropic API Key                                   │
│ ┌─────────────────────────────────────┐            │
│ │ sk-ant-••••••••••••••••••••••  👁️ │ [Test] [Save]│
│ └─────────────────────────────────────┘            │
│ ✓ Configured · Last updated 2 hours ago            │
│                                                     │
│ Required for agents to use Claude. Get your key at │
│ console.anthropic.com                               │
│                                                     │
├─────────────────────────────────────────────────────┤
│                                                     │
│ Custom Instructions (Trinity Prompt)                │
│ ─────────────────────────────────────────────────── │
│ ...existing trinity prompt section...               │
│                                                     │
└─────────────────────────────────────────────────────┘
```

### Technical Design

#### Backend Changes

**New helper: `get_anthropic_api_key()`**
```python
def get_anthropic_api_key() -> str:
    """Get Anthropic API key from settings, fallback to env var."""
    key = get_setting_value('anthropic_api_key', None)
    if key:
        return key
    return os.getenv('ANTHROPIC_API_KEY', '')
```

**Modified: Agent creation (`routers/agents.py:~511`)**
```python
# Change from:
'ANTHROPIC_API_KEY': os.getenv('ANTHROPIC_API_KEY', ''),
# To:
'ANTHROPIC_API_KEY': get_anthropic_api_key(),
```

**New endpoint: `GET /api/settings/api-keys`**
- Admin-only
- Returns: `{ "anthropic": { "configured": true, "masked": "sk-ant-...xxxx" } }`

**New endpoint: `PUT /api/settings/api-keys/anthropic`**
- Admin-only
- Accepts: `{ "api_key": "sk-ant-..." }`
- Validates format (starts with `sk-ant-`)
- Stores in `system_settings`
- Returns: `{ "success": true }`

**New endpoint: `POST /api/settings/api-keys/anthropic/test`**
- Admin-only
- Accepts: `{ "api_key": "sk-ant-..." }`
- Makes test call to Anthropic API
- Returns: `{ "valid": true }` or `{ "valid": false, "error": "..." }`

#### Frontend Changes

**Modified: `Settings.vue`**
- Add "API Keys" section at top
- Anthropic key input (password field with show/hide)
- Test button (calls test endpoint)
- Save button
- Status indicator (configured/not configured)

**Modified: `stores/settings.js`**
- Add `fetchApiKeys()` action
- Add `updateAnthropicKey(key)` action
- Add `testAnthropicKey(key)` action

### Acceptance Criteria

- [ ] API key can be set from Settings page
- [ ] Key is masked in UI (only last 4 chars visible)
- [ ] "Test" validates key with Anthropic API
- [ ] New agents use key from settings
- [ ] Fallback to env var if setting not configured
- [ ] Only admin users can access
- [ ] Key not exposed in API responses or logs

---

## Implementation Plan

### Phase 1: Backend - Password Setup (1-2 hours)
1. Add `bcrypt` to requirements.txt
2. Update password hashing in `dependencies.py`
3. Create `/api/setup/status` endpoint
4. Create `/api/setup/admin-password` endpoint
5. Modify login to check setup status

### Phase 2: Frontend - Password Setup (1-2 hours)
1. Create `SetupPassword.vue` component
2. Add `/setup` route
3. Add router guard for setup check

### Phase 3: Backend - API Key (1-2 hours)
1. Create `get_anthropic_api_key()` helper
2. Modify agent creation to use helper
3. Create API key endpoints

### Phase 4: Frontend - API Key (1 hour)
1. Add API Keys section to Settings.vue
2. Implement test/save functionality

### Phase 5: Install Script (30 min)
1. Update success message
2. Remove API key from `.env.example` default

**Total Estimate: 5-7 hours**

---

## Files to Modify

| File | Changes |
|------|---------|
| `src/backend/requirements.txt` | Add bcrypt |
| `src/backend/dependencies.py` | Bcrypt hashing |
| `src/backend/routers/auth.py` | Check setup on login |
| `src/backend/routers/settings.py` | API key endpoints |
| `src/backend/routers/agents.py` | Use `get_anthropic_api_key()` |
| `src/frontend/src/views/SetupPassword.vue` | New file |
| `src/frontend/src/views/Settings.vue` | API key section |
| `src/frontend/src/router/index.js` | Setup route + guard |
| `src/frontend/src/stores/settings.js` | API key methods |
| `install.sh` | Update messages |

---

## Testing

### Password Setup
1. Fresh install → opens password setup screen
2. Set password → redirected to login
3. Login works with new password
4. Refresh → goes to login (not setup)
5. Try `/setup` → redirects to login

### API Key
1. Settings shows API Keys section
2. Enter key → Test → shows valid/invalid
3. Save → key persisted
4. Create agent → works with saved key
5. No env var + no setting → agent creation shows error

---

## Security Notes

1. **Password hashing** - Use bcrypt (currently plaintext!)
2. **Setup endpoint** - Only works before `setup_completed=true`
3. **API key masking** - Never return full key in API responses
4. **Admin-only** - API key endpoints require admin role
