# Phase 1: Authentication & UI Readiness

> **Purpose**: Validate authentication flows (Dev Mode + Email Auth) and browser login
> **Duration**: ~5 minutes
> **Assumes**: Phase 0 PASSED (services running, clean slate)
> **Output**: Logged-in browser session, valid JWT token

---

## Prerequisites

- ✅ Phase 0 PASSED
- ✅ Backend healthy
- ✅ Frontend accessible
- ✅ No test agents exist

> **Note**: JWT tokens are invalidated when backend restarts. If you see 401 errors after a deploy/restart, logout and login again to get a fresh token.

---

## Authentication Modes

Trinity supports multiple authentication modes:

| Mode | Description | Login Method |
|------|-------------|--------------|
| **Dev Mode** | Local development | Username/password (admin/PASSWORD) |
| **Email Auth** | Email-based OTP | Email + 6-digit verification code |
| **Auth0** | Production OAuth | Google/social login via Auth0 |

**Check Current Mode**:
```bash
curl http://localhost:8000/api/auth/mode
```

**Expected Response**:
```json
{
  "mode": "dev",          // or "email" or "auth0"
  "dev_mode_enabled": true
}
```

---

## Test Steps

### Step 1: Navigate to Login Page
**Action**:
- Open browser to http://localhost:3000
- Refresh page (Ctrl+F5) to clear any cached state
- If already logged in, logout first

**Expected** (Dev Mode):
- [ ] Page loads
- [ ] Login form visible
- [ ] "Development Mode - Local authentication enabled" message shown
- [ ] Username field visible
- [ ] Password field visible
- [ ] "Sign In" button visible

**Expected** (Email Auth Mode):
- [ ] Page loads
- [ ] Email input field visible
- [ ] "Send Code" or "Continue with Email" button visible
- [ ] No username/password fields (email-only)

**Verify**:
- [ ] No console errors
- [ ] Form is interactive (not disabled)

---

### Step 2: Attempt Invalid Login
**Action**:
- Enter username: `invalid`
- Enter password: `wrong`
- Click "Sign In"

**Expected**:
- [ ] Login fails with error message
- [ ] Stays on login page
- [ ] Error message displays: "Invalid credentials" or similar
- [ ] Form remains interactive

**Verify**:
- [ ] Error message shown
- [ ] Can retry login

---

### Step 3: Successful Login (Dev Mode)
**Action** (Dev Mode):
- Clear form
- Enter username: `admin`
- Enter password: `YOUR_PASSWORD` (from `.env` ADMIN_PASSWORD)
- Click "Sign In"
- Wait for page to load (10 seconds max)

**Expected**:
- [ ] Redirects to http://localhost:3000/ (dashboard)
- [ ] Navigation bar visible
- [ ] "Connected" indicator in navbar
- [ ] Admin profile button shows "admin"
- [ ] Dashboard loads with "No agents" message

**Verify**:
- [ ] No console errors during redirect
- [ ] Page title shows "Trinity"
- [ ] All nav items visible: Dashboard, Agents, Templates, Schedules (or similar)

---

### Step 3-ALT: Successful Login (Email Auth Mode)
**Action** (Email Auth):
- Enter email: `admin@your-domain.com` (whitelisted email)
- Click "Send Code"
- Wait for code email (check backend logs or inbox)
- Enter 6-digit code received
- Click "Verify"
- Wait for page to load

**Expected**:
- [ ] Code sent message appears
- [ ] Code input field shown
- [ ] After verify: Redirects to dashboard
- [ ] Navigation bar visible
- [ ] Profile shows email address

**Note**: Email auth requires:
1. Email in whitelist (Settings → Email Whitelist)
2. SMTP configured (or check backend logs for code in dev mode)

---

### Step 4: Verify Session Token
**Action**: Open browser DevTools Console
```javascript
localStorage.getItem('token')
```

**Expected**:
- [ ] Returns JWT token (starts with `eyJ`)
- [ ] Token contains `.` separators (3 parts)

**Verify**:
- [ ] Token is valid JWT format
- [ ] Token not empty

---

### Step 5: Verify User Profile
**Action**: Click "admin" button in top-right navbar

**Expected**:
- [ ] Dropdown menu appears
- [ ] Shows username: "admin"
- [ ] Shows email: "admin@localhost"
- [ ] "Sign out" button visible

**Verify**:
- [ ] Profile information displayed correctly
- [ ] No console errors

---

### Step 6: Verify Dashboard Initial State
**Action**: Look at main content area

**Expected**:
- [ ] Page title: "Dashboard" or "Agent Network"
- [ ] Message: "No agents · 0 running · 0 messages"
- [ ] "Create Agent" button visible
- [ ] Empty graph area (no agent nodes)
- [ ] No error messages

**Verify**:
- [ ] Clean slate confirmed
- [ ] UI is responsive
- [ ] No pending API errors

---

### Step 7: Logout Test
**Action**:
- Click "admin" dropdown
- Click "Sign out"

**Expected**:
- [ ] Redirects to login page
- [ ] Login form visible
- [ ] Session cleared

**Verify**:
- [ ] Token removed from localStorage
- [ ] Cannot access dashboard directly (redirect to login)

---

### Step 8: Login Again (Session Persistence)
**Action**:
- Login again with admin/YOUR_PASSWORD
- Navigate away (click Agents in navbar)
- Refresh page (F5)

**Expected**:
- [ ] Page loads without redirect to login
- [ ] Session persists
- [ ] Still on Agents page
- [ ] "admin" button shows in navbar

**Verify**:
- [ ] Token still in localStorage
- [ ] Session survived page refresh

---

## Critical Validations

### Dev Mode Detection
**Validation**: Backend correctly reports dev mode

```bash
curl http://localhost:8000/api/auth/mode \
  -H "Authorization: Bearer $TOKEN"
```

**Expected Response**:
```json
{
  "mode": "dev",
  "dev_mode_enabled": true
}
```

**Verify**:
- [ ] Mode = "dev" (not "prod")
- [ ] dev_mode_enabled = true

---

### Token Structure
**Validation**: JWT token has correct claims

Decode token at https://jwt.io:
- [ ] Header: `{"alg":"HS256","typ":"JWT"}`
- [ ] Payload has: `"mode":"dev"` claim
- [ ] Payload has: `"sub"` claim (user ID)
- [ ] Payload has: `"exp"` claim (expiration)

---

## Success Criteria

Phase 1 is **PASSED** when:
- ✅ Dev mode login works (admin/YOUR_PASSWORD)
- ✅ Invalid login fails with error
- ✅ Session token obtained and stored
- ✅ User profile displays correctly
- ✅ Logout clears session
- ✅ Session persists across page refresh
- ✅ Dashboard shows clean state (no agents)

---

## Troubleshooting

**Login fails**:
- Check backend logs: `docker logs trinity-backend | grep -i auth`
- Verify DEV_MODE_ENABLED=true in environment
- Try clearing browser cache

**Token missing**:
- Check localStorage in DevTools
- Verify response from `/api/token` endpoint
- Check CORS settings

**Session doesn't persist**:
- Check cookie settings in browser
- Verify localStorage is enabled
- Check for JavaScript errors in console

---

## Next Phase

Once Phase 1 is **PASSED**, proceed to:
- **Phase 2**: Agent Creation (test-echo - GitHub template)
- **Phase 3**: Basic Chat & Context Validation

---

**Status**: 🟢 Ready for agent creation testing
