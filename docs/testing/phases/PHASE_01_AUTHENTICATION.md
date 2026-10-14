# Phase 01: Authentication

> **Purpose**: Verify the login gate, admin password login, the top navigation, session persistence, and sign-out.
> **Duration**: ~10 minutes
> **Assumes**: the fixture trio is running; the browser may be logged in or out (Setup signs out)
> **Output**: A pass proves an unauthenticated visitor is held at `/login`, `admin` can sign in and reach every top-nav destination, a lost token ends the session, and "Sign out" revokes it server-side.
> **Last verified**: 2026-10-09 against source (not yet browser-run)

---

## Background

Login lives at `/login` (`views/Login.vue`). The page offers email-code login when the
instance has it enabled, and always offers admin password login. The session is a JWT
kept in `localStorage` under `token` (with the profile under `auth0_user`); the router
guard sends any `requiresAuth` route to `/login` when it is missing. The top bar
(`components/NavBar.vue`) carries Dashboard, Library, Operations, Settings and Workspace
plus a user menu that owns "Sign out".

Replaces the January flow that expected an empty dashboard ("No agents · 0 running"),
a nav with Agents/Templates entries, and a jwt.io decode step — none of which hold now.

## Prerequisites

- [ ] Frontend answers at `http://localhost`, backend at `http://localhost:8000`
- [ ] You know `ADMIN_PASSWORD` from `.env`
- [ ] Viewport starts at desktop width (≥ 1280 px)

## Setup

- Open `http://localhost/`. If you land on the Dashboard (already signed in), open the
  user menu (round avatar/initials button at the far right of the top bar) and click
  **Sign out**. You must be on `/login` before Step 1.

## Test: Login gate

### Step 1: Logged-out deep link is held at the login page
**Action**:
- Navigate to `http://localhost/agents/test-echo`
- Then navigate to `http://localhost/settings`

**Expected**:
- [ ] Both navigations end on `http://localhost/login`
- [ ] The page shows the heading "Trinity" and the line "Sign in to manage your agents"
- [ ] No agent or settings content is visible at any point

### Step 2: Login page offers admin login
**Action**:
- Look at the sign-in card. One of two layouts is shown:
  - **A** — an "Email Address" field with a **Send Verification Code** button and, below a divider, a **🔐 Admin Login** button
  - **B** — the admin form directly (a box reading "Admin Login", fields "Username or email" and "Password")
- If layout A, click **🔐 Admin Login**

**Expected**:
- [ ] Record which layout appeared (A or B)
- [ ] The admin form shows "Username or email" pre-filled with `admin`, a "Password" field with placeholder "Enter admin password", and a **Sign In as Admin** button
- [ ] **Sign In as Admin** is disabled while the password is empty
- [ ] In layout A the admin form also shows "← Back to email login"

**Verify** (API):
```bash
curl -s http://localhost:8000/api/auth/mode
# {"email_auth_enabled": <bool>, "setup_completed": true}
```
- [ ] `setup_completed` is `true`; `email_auth_enabled` is `true` for layout A, `false` for layout B

### Step 3: Wrong password is rejected with a named error
**Action**:
- Leave the username as `admin`, type `not-the-password` in "Password", click **Sign In as Admin**
- Do this ONCE only (repeated failures lock the account for 15 minutes)

**Expected**:
- [ ] The card is replaced by an "Access Denied" panel containing "Incorrect username or password" and a **Try Again** button
- [ ] The URL is still `/login`
- [ ] `localStorage.getItem('token')` is `null`

**Action**:
- Click **Try Again**

**Expected**:
- [ ] The sign-in card returns (layout A or B as before) with the password field empty

### Step 4: Admin login succeeds
**Action**:
- Reach the admin form again (click **🔐 Admin Login** if layout A)
- Enter username `admin` and `ADMIN_PASSWORD` from `.env`, click **Sign In as Admin**

**Expected**:
- [ ] The button briefly reads "Signing in..."
- [ ] The URL becomes `http://localhost/` and the Dashboard renders
- [ ] `localStorage.getItem('token')` is a three-part dot-separated string and `localStorage.getItem('auth0_user')` is a JSON object
- [ ] If a "Two-factor authentication" or "Set up two-factor authentication" step (button "Verify & Sign In" / "Confirm & Sign In") appears instead, this phase cannot continue unattended: record every remaining step as `SKIPPED (second factor required)` and stop. It is not present otherwise.

## Test: Navigation and session

### Step 5: Top navigation items
**Action**:
- Read the links in the top bar, left to right
- Click **Library**, then **Operations**, then **Settings**, then **Dashboard**
- Do not click **Workspace** (it opens a new browser tab); read its link target instead

**Expected**:
- [ ] The links are, in order: Dashboard, Library, Operations, Settings, Workspace (an instance may show one further link after Workspace — record it, do not open it)
- [ ] There is no "Agents", "Templates", "System" or "Schedules" link
- [ ] Each click lands on `/library`, `/operations`, `/settings`, `/` respectively and the clicked link is the highlighted one
- [ ] The Workspace link points at `/workspace` and has `target="_blank"`
- [ ] Operations may carry a numeric badge; record its value if present

### Step 6: Dashboard shows the fixtures
**Action**:
- Navigate to `http://localhost/agents`

**Expected**:
- [ ] The URL is rewritten to the Dashboard (`/`, the `view=list` query may be applied then stripped) and the agent list is shown with a "Search agents..." box
- [ ] `test-echo`, `test-counter` and `test-delegator` are all listed
- [ ] Do NOT expect an empty state — the fixtures always exist

### Step 7: Authenticated visit to /login bounces to the Dashboard
**Action**:
- Navigate to `http://localhost/login`

**Expected**:
- [ ] The URL ends at `http://localhost/`; the sign-in card is not shown

### Step 8: Unknown and legacy routes
**Action**:
- Navigate to `http://localhost/this-route-does-not-exist`
- Navigate to `http://localhost/templates`
- Navigate to `http://localhost/api-keys`

**Expected**:
- [ ] The unknown path ends at `http://localhost/` (Dashboard), not a blank page or 404 screen
- [ ] `/templates` ends at `/library`
- [ ] `/api-keys` ends at `/settings?tab=mcp-keys`

### Step 9: Session survives a reload
**Action**:
- On `http://localhost/library`, reload the page

**Expected**:
- [ ] You are still on `/library`, signed in, with no flash of the login card
- [ ] `localStorage.getItem('token')` is unchanged

### Step 10: User menu and theme
**Action**:
- Click the round avatar/initials button at the far right of the top bar
- In the menu, under "Theme", choose **Dark**, then **Light**

**Expected**:
- [ ] The menu shows a name line, an email line, a "Theme" group with Light / Dark / System, a "Documentation" link and a **Sign out** button
- [ ] Choosing Dark adds the class `dark` to `<html>` and the bar and page turn dark with readable text; choosing Light removes it
- [ ] The separate theme button in the bar has a title ending "(click to switch)"

### Step 11: Narrow viewport (390 px)
**Action**:
- Resize the viewport to 390 × 844 on `http://localhost/`

**Expected**:
- [ ] The page does not scroll horizontally
- [ ] Links that no longer fit collapse into a disclosure labelled "N more" (N = number hidden); opening it lists the hidden links and they navigate
- [ ] The user-menu button is still visible and opens the same menu, including **Sign out**

**Action**:
- Resize back to ≥ 1280 px wide

## Test: Losing and ending the session

### Step 12: A token the server rejects ends the session
**Action**:
- On `http://localhost/`, run in the page:
  `const t = localStorage.getItem('token'); window.__saved = t; localStorage.setItem('token', t.slice(0, -4) + 'AAAA')`
- Reload the page and wait up to 10 seconds

**Expected**:
- [ ] You end on `http://localhost/login`
- [ ] `localStorage.getItem('token')` is `null`

### Step 13: A cleared token redirects on the next navigation
**Action**:
- Sign in again as `admin` (Step 4)
- Run `localStorage.removeItem('token'); localStorage.removeItem('auth0_user')`, then reload

**Expected**:
- [ ] The reload ends on `http://localhost/login`

### Step 14: Sign out revokes the token
**Action**:
- Sign in again as `admin` and note the new token as `$OLD`
- Open the user menu and click **Sign out**

**Expected**:
- [ ] The URL becomes `http://localhost/login` and the sign-in card is shown
- [ ] `localStorage.getItem('token')` and `localStorage.getItem('auth0_user')` are both `null`
- [ ] Navigating to `http://localhost/` returns to `/login`

**Verify** (API):
```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer $OLD" http://localhost:8000/api/agents
# 401 — the signed-out token no longer works
```

## Cleanup / Restore

- The phase ends signed out. Sign in again as `admin` with `ADMIN_PASSWORD` from `.env`
  so the browser is left with a working session.
- Set the theme back to the value it had before Step 10 (record what it was; default is System).
- Nothing else was changed. The single failed login from Step 3 is cleared by the successful login in Step 4.

## Manual-only (not run unattended)

- Email-code login end to end (needs a mailbox to read the code) — see Phase 17.
- Completing a second-factor challenge or enrolment (needs an authenticator).
- Account lockout after repeated bad passwords (5 failures per account / 15 minutes) — deliberately not exercised.
- First-run `/setup` wizard (needs an uninitialised database).

## Critical Validations

1. Logged-out visits to `/agents/test-echo` and `/settings` end on `/login` (Step 1).
2. A wrong password shows "Incorrect username or password" and stores no token (Step 3).
3. Correct admin credentials land on `/` with a token in `localStorage` (Step 4).
4. A rejected or missing token ends on `/login` (Steps 12–13).
5. After **Sign out** the old token gets HTTP 401 from the API (Step 14).

## Success Criteria

- [ ] Login gate holds for deep links
- [ ] Admin login works; wrong password is refused with the named error
- [ ] Nav shows Dashboard, Library, Operations, Settings, Workspace and each routes correctly
- [ ] Unknown route falls back to `/`; legacy `/templates` and `/api-keys` redirect
- [ ] Session persists across reload and is ended by a bad/missing token
- [ ] Sign out clears local state and revokes the token
- [ ] Nav and user menu remain usable at 390 px and in both themes

## Troubleshooting

- **Every route redirects to `/setup`**: `GET /api/setup/status` reports `setup_completed: false` — the instance has no admin password yet; this phase cannot run.
- **"Too many failed attempts for this account. Try again in N seconds."**: the per-account limit (5 failures / 15 min) was hit by earlier runs; wait it out, do not retry in a loop.
- **Login card stuck on a pulsing placeholder with "Checking authentication..."**: `GET /api/auth/mode` is not answering — the backend is down.
- **Step 12 stays on the Dashboard**: no authenticated request has failed yet; navigate to `/operations` to force one, then re-check.
