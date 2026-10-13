# Phase 17: Email Authentication

> **Purpose**: Verify the email-code login surface as far as it can be driven without a mailbox: the form, the mode endpoint, the uniform response to an unknown address, and rejection of a bad code.
> **Duration**: ~8 minutes
> **Assumes**: the fixture trio is running; the browser may be logged in or out (Setup signs out)
> **Output**: A pass proves the email login form renders and validates, the request endpoint does not reveal whether an address is registered, and a wrong code is refused with the named error.
> **Last verified**: 2026-10-09 against source (not yet browser-run)

---

## Background

When email login is enabled, `/login` (`views/Login.vue`) leads with an "Email Address"
field and **Send Verification Code**; the second step takes a 6-digit code and
**Verify & Sign In**. Admin password login stays reachable through **🔐 Admin Login**.
Only whitelisted addresses receive a code, but `POST /api/auth/email/request` answers
every address with the same body so membership cannot be probed; the allow-list is
re-checked at verify time, where a non-whitelisted address fails exactly like a wrong
code. Codes expire after 10 minutes and five failed verifications per address in
10 minutes lock that address out.

Replaces the January flow that used "Send Code" / "Verify" labels, expected a
`mode`-style response from `/api/auth/mode`, quoted three attempts, and walked a real
inbox. Everything that needs a delivered code is now listed under Manual-only.

## Prerequisites

- [ ] Frontend at `http://localhost`, backend at `http://localhost:8000`
- [ ] You know `ADMIN_PASSWORD` from `.env` (for the Setup token and to sign back in during Cleanup)
- [ ] `user@example.com` is the only address used. Budget: ONE code request through the UI, ONE through the API, ONE wrong-code submit. Do not repeat them.

## Setup

- Open `http://localhost/`. If you land on the Dashboard, open the user menu (round avatar/initials button, top right) and click **Sign out**. You must be on `/login`.
- Read the mode:
```bash
curl -s http://localhost:8000/api/auth/mode
```
- Confirm `user@example.com` is not whitelisted (`$TOKEN` is an admin Bearer token):
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/settings/email-whitelist \
  | python3 -c "import sys,json; print('user@example.com' in [e['email'] for e in json.load(sys.stdin)['whitelist']])"
# False
```
  If it prints `True`, another run left it there: record that, and still run the phase — every expectation below holds for a whitelisted address too, except that a real code is generated.
- If `email_auth_enabled` is `false`, the email form is not rendered on this instance:
  run Step 1 only, record Steps 2–10 as `SKIPPED (email login disabled on this instance)`, then go to Cleanup.

## Test: Mode and form

### Step 1: Mode endpoint shape
**Action**:
- Inspect the response from Setup

**Expected**:
- [ ] HTTP 200 without any Authorization header
- [ ] The body has exactly the keys `email_auth_enabled` (boolean) and `setup_completed` (boolean)
- [ ] `setup_completed` is `true`

### Step 2: Email form renders
**Action**:
- Look at the sign-in card on `http://localhost/login`

**Expected**:
- [ ] Heading "Trinity" and the line "Sign in to manage your agents"
- [ ] A field labelled "Email Address" with placeholder `you@example.com`
- [ ] A **Send Verification Code** button, disabled while the field is empty
- [ ] Below a divider, a **🔐 Admin Login** button
- [ ] No "Verification Code" field is visible yet
- [ ] If an "Or sign in with" group of extra buttons is shown below, record that it is present and do not click it

### Step 3: Admin Login toggle and back
**Action**:
- Click **🔐 Admin Login**, then click "← Back to email login"

**Expected**:
- [ ] The card switches to the admin form ("Username or email" pre-filled `admin`, "Password", **Sign In as Admin**)
- [ ] "← Back to email login" returns to the "Email Address" form

### Step 4: Malformed address is blocked in the browser
**Action**:
- Type `not-an-email` in "Email Address" and click **Send Verification Code**

**Expected**:
- [ ] The browser's own email-format prompt appears on the field
- [ ] The card does not advance to the code step and no `POST /api/auth/email/request` is sent (check the network log)

## Test: Requesting a code for an unknown address

### Step 5: Request endpoint gives a uniform answer
**Action**:
- Run once:
```bash
curl -s -w '\n%{http_code}\n' -X POST http://localhost:8000/api/auth/email/request \
  -H 'Content-Type: application/json' -d '{"email": "user@example.com"}'
```

**Expected**:
- [ ] HTTP 200
- [ ] Body is exactly `{"success": true, "message": "If your email is registered, you'll receive a code shortly"}`
- [ ] There is no `expires_in_seconds` field, no 403/404/429, and nothing in the body that says whether the address is whitelisted

### Step 6: The UI advances to the code step regardless
**Action**:
- In the browser, clear the field, type `user@example.com`, click **Send Verification Code**

**Expected**:
- [ ] The button briefly reads "Sending code..."
- [ ] The card switches to the code step: the line "📧 We sent a 6-digit code to **user@example.com**"
- [ ] A countdown "Code expires in 10:00" (or 9:5x) that ticks down each second
- [ ] A field labelled "Verification Code" with placeholder `000000`
- [ ] A **Verify & Sign In** button and a "← Back to email" button
- [ ] The **🔐 Admin Login** button is no longer shown on this step
- [ ] No error panel — the UI gives no hint that the address is unknown

### Step 7: Code field validation
**Action**:
- Type `123` in "Verification Code"
- Then try to type `1234567890`

**Expected**:
- [ ] With 3 characters **Verify & Sign In** is disabled
- [ ] The field accepts at most 6 characters; with 6 the button is enabled
- [ ] Clear the field afterwards. Do not submit yet

### Step 8: Non-numeric code is blocked in the browser
**Action**:
- Type `abcdef` and click **Verify & Sign In**

**Expected**:
- [ ] The browser's own "match the requested format" prompt appears on the field
- [ ] Record whether a `POST /api/auth/email/verify` request was sent — source predicts none (the field carries the pattern `[0-9]{6}`). If one WAS sent, it counts as the single wrong-code submit: skip the submit in Step 9 and only check its expectations

### Step 9: A wrong code is refused with the named error
**Action**:
- Clear the field, type `000000`, click **Verify & Sign In** — ONCE

**Expected**:
- [ ] The button briefly reads "Verifying..."
- [ ] The card is replaced by an "Access Denied" panel containing "Invalid or expired verification code" and a **Try Again** button
- [ ] The URL is still `/login`; `localStorage.getItem('token')` is `null`
- [ ] The network log shows `POST /api/auth/email/verify` → 401

**Action**:
- Click **Try Again**

**Expected**:
- [ ] The card returns to the first step with an empty "Email Address" field and the **🔐 Admin Login** button

### Step 10: The failed attempt created no session
**Action**:
- Do NOT request another code. Navigate to `http://localhost/agents/test-echo`

**Expected**:
- [ ] The navigation ends on `/login` — the failed attempt created no session

### Step 11: Narrow viewport (390 px) and dark theme
**Action**:
- On `http://localhost/login`, resize to 390 × 844
- Emulate the dark colour scheme (`prefers-color-scheme: dark`) and reload; then return to light and to ≥ 1280 px wide

**Expected**:
- [ ] At 390 px the card fits the width with no horizontal scroll; the field, **Send Verification Code** and **🔐 Admin Login** are fully visible
- [ ] Record whether the page turns dark under the emulated scheme (it does when the saved theme is System); in either theme the labels, placeholder and buttons are readable

## Cleanup / Restore

- The phase leaves the browser signed out. Sign in again: **🔐 Admin Login** → username `admin`, `ADMIN_PASSWORD` from `.env` → **Sign In as Admin**.
- Nothing was written to settings or the whitelist. One failed verification was recorded against `user@example.com` (and against this machine's address); both counters expire on their own within 15 minutes. Do not re-run this phase more than four times in 10 minutes, or `user@example.com` reaches the five-attempt lockout.
- No login code was generated when the address was not whitelisted, so there is nothing to revoke.

## Manual-only (not run unattended)

- Full login with a delivered code: whitelist a real address, request a code, read it from the mailbox, **Verify & Sign In** → Dashboard.
- Code expiry: wait out the 10-minute countdown, then confirm the code is refused.
- Lockout: five wrong codes for one address → "Too many verification attempts. Request a new code or try again in N seconds." (HTTP 429).
- Request throttling: more than three requests for one whitelisted address in 10 minutes are silently dropped (same 200 body — only observable in the mailbox or server logs).
- A whitelisted address signing in for the first time gets an account created — needs a mailbox.
- The two-factor step that can follow a correct code on some instances.

## Critical Validations

1. `/api/auth/mode` returns `{email_auth_enabled, setup_completed}` unauthenticated (Step 1).
2. The email form renders with **Send Verification Code** and **🔐 Admin Login** (Step 2).
3. An unknown address gets HTTP 200 and the generic message — no membership signal (Step 5), and the UI advances to the code step (Step 6).
4. A wrong code shows "Invalid or expired verification code" and stores no token (Step 9).

## Success Criteria

- [ ] Mode endpoint shape confirmed
- [ ] Email form, admin toggle and code step all render with the current labels
- [ ] Browser-side validation blocks malformed addresses and non-numeric codes
- [ ] Unknown address indistinguishable from a known one at request time
- [ ] Wrong code refused; no session created
- [ ] Browser signed back in as admin at the end

## Troubleshooting

- **The card shows the admin form with no email field**: `email_auth_enabled` is `false` — expected on that instance; follow the Setup skip rule.
- **Step 5 or 6 returns 403 "Email authentication is disabled"**: same cause; the UI shows it as an "Access Denied" panel.
- **Step 9 shows "Too many verification attempts…" or "Too many failed attempts for this account…"**: earlier runs used up the attempt budget for `user@example.com`. Record `BLOCKED (attempt limit — retry after the stated seconds)`; do not switch to another address.
- **Every route redirects to `/setup`**: the instance has not completed first-run setup; this phase cannot run.
