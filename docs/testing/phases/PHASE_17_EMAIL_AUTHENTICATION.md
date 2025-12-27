# Phase 17: Email-Based Authentication

> **Purpose**: Validate email OTP authentication flow (Req 12.4)
> **Duration**: ~15 minutes
> **Assumes**: Phase 0 PASSED (services running), Email auth mode enabled
> **Output**: Email login flow verified, whitelist enforced

---

## Prerequisites

- ✅ Phase 0 PASSED (services running)
- ✅ Email authentication mode enabled
- ✅ SMTP configured (or backend logs for dev mode)
- ✅ At least one email in whitelist

---

## Background

Email Authentication (Req 12.4) provides passwordless login:

**Flow**:
1. User enters email address
2. System sends 6-digit OTP code
3. User enters code to verify
4. JWT token issued on success

**Security**:
- Only whitelisted emails can login
- Codes expire after 10 minutes
- Maximum 3 verification attempts
- Rate limiting on code requests

---

## Pre-Test Setup

### Check Auth Mode

```bash
curl http://localhost:8000/api/auth/mode
```

**For Email Auth Testing**:
```json
{
  "mode": "email",
  "dev_mode_enabled": false
}
```

**Note**: If mode is "dev", email auth is bypassed. Switch to email mode for this test.

### Verify Whitelist Has Entries

```bash
curl http://localhost:8000/api/settings/email-whitelist \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

**If empty**: Add test email via Settings UI or API first.

---

## Test Steps

### Step 1: Navigate to Login Page

**Action**:
- Open http://localhost:3000 in incognito/private window
- Clear any existing session

**Expected**:
- [ ] Login page displays
- [ ] Email input field visible
- [ ] "Send Code" or "Continue" button visible
- [ ] No username/password fields (email-only mode)

---

### Step 2: Submit Non-Whitelisted Email

**Action**:
- Enter email: `notwhitelisted@example.com`
- Click "Send Code"

**Expected**:
- [ ] Error message: "Email not authorized" or similar
- [ ] No code sent
- [ ] User remains on login page

---

### Step 3: Submit Whitelisted Email

**Action**:
- Enter whitelisted email: `your-email@domain.com`
- Click "Send Code"

**Expected**:
- [ ] Success message: "Code sent to your email"
- [ ] Code input field appears
- [ ] Timer shows (code expiration)
- [ ] "Resend Code" option visible

**Check Backend Logs** (dev mode):
```bash
docker logs trinity-backend 2>&1 | grep -i "verification code"
```
- [ ] Code logged for dev testing

---

### Step 4: Enter Incorrect Code

**Action**:
- Enter wrong code: `000000`
- Click "Verify"

**Expected**:
- [ ] Error message: "Invalid code"
- [ ] Attempts remaining shown (e.g., "2 attempts left")
- [ ] Can retry

---

### Step 5: Enter Correct Code

**Action**:
- Enter correct 6-digit code (from email or logs)
- Click "Verify"

**Expected**:
- [ ] Login successful
- [ ] Redirects to dashboard
- [ ] User profile shows email address
- [ ] JWT token stored in localStorage

---

### Step 6: Verify Session Token

**Action**:
- Open browser DevTools Console
- Run: `localStorage.getItem('token')`

**Expected**:
- [ ] Returns JWT token
- [ ] Token is valid format (3 parts separated by dots)

**Decode Token** (jwt.io):
- [ ] Payload contains email
- [ ] Payload contains expiration
- [ ] Token valid for configured duration

---

### Step 7: Test Code Expiration

**Action**:
- Request new code
- Wait 10+ minutes (or configure shorter expiry for testing)
- Enter code after expiration

**Expected**:
- [ ] Error: "Code expired"
- [ ] Must request new code

**Note**: May skip if time-constrained; document as "not tested"

---

### Step 8: Test Rate Limiting

**Action**:
- Request multiple codes rapidly (5+ times)

**Expected**:
- [ ] Rate limit triggered after X requests
- [ ] Error: "Too many requests" or similar
- [ ] Must wait before retrying

**Note**: Rate limit may vary by configuration

---

### Step 9: Test Max Attempts

**Action**:
- Enter wrong codes 3+ times

**Expected**:
- [ ] After 3 failed attempts: code invalidated
- [ ] Must request new code
- [ ] Error message explains lockout

---

### Step 10: Verify Logout

**Action**:
- Click user profile dropdown
- Click "Sign out"

**Expected**:
- [ ] Session cleared
- [ ] Redirects to login page
- [ ] Token removed from localStorage
- [ ] Cannot access protected pages

---

## Critical Validations

### API Endpoints Work

```bash
# Request code
curl -X POST http://localhost:8000/api/auth/email/request \
  -H "Content-Type: application/json" \
  -d '{"email": "whitelisted@example.com"}'

# Verify code
curl -X POST http://localhost:8000/api/auth/email/verify \
  -H "Content-Type: application/json" \
  -d '{"email": "whitelisted@example.com", "code": "123456"}'
```

**Expected**:
- [ ] Request returns success (or whitelist error)
- [ ] Verify returns JWT token on success

---

### Whitelist Enforcement

**Validation**: Only whitelisted emails can login

1. Add email to whitelist
2. Request code → Should work
3. Remove email from whitelist
4. Request code → Should fail

---

## Success Criteria

Phase 17 is **PASSED** when:
- ✅ Login page shows email auth UI
- ✅ Non-whitelisted emails rejected
- ✅ Code sent to whitelisted email
- ✅ Invalid codes rejected with attempt count
- ✅ Correct code grants access
- ✅ JWT token issued and stored
- ✅ Code expiration enforced
- ✅ Rate limiting works
- ✅ Max attempts enforced
- ✅ Logout clears session

---

## Troubleshooting

**Email not received**:
- Check SMTP configuration
- Check spam folder
- Use backend logs in dev mode

**"Email not authorized"**:
- Verify email in whitelist (Settings → Email Whitelist)
- Check exact email match (case-sensitive)

**Code verification fails**:
- Check code entered correctly (6 digits)
- Verify code not expired
- Check attempts not exceeded

**Login doesn't work**:
- Check auth mode is "email" not "dev"
- Verify backend email auth enabled
- Check frontend route guards

---

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/auth/email/request` | Request OTP code |
| POST | `/api/auth/email/verify` | Verify code, get token |
| GET | `/api/settings/email-whitelist` | List whitelisted emails |
| POST | `/api/settings/email-whitelist` | Add email to whitelist |
| DELETE | `/api/settings/email-whitelist/{email}` | Remove email |

---

## Related Documentation

- Feature Flow: `docs/memory/feature-flows/email-authentication.md`
- Requirements: `requirements.md` section 12.4
- Settings UI: Phase 13 (Email Whitelist section)

---

## Next Phase

After Email Authentication testing, proceed to:
- **Phase 18**: GitHub Repository Initialization

---

**Status**: Ready for testing
**Last Updated**: 2025-12-26
