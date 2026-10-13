# Phase 19: First-Time Setup

> **Purpose**: Prove that a set-up instance keeps the setup page and the setup endpoint closed, and document the fresh-install walkthrough for manual runs.
> **Duration**: ~8 minutes (Part A only)
> **Assumes**: the fixture trio is running and you are logged in as admin
> **Output**: `/setup` is unreachable once setup is completed; the status endpoint reports completed; the provisioning endpoint refuses a second setup without side effects
> **Last verified**: 2026-10-09 against source (not yet browser-run)

---

## Background

First-time setup is a dedicated route, `/setup`, rendering `views/SetupPassword.vue` — a dark
"Welcome to Trinity" page with a "Create your admin account" card. Its state comes from
`GET /api/setup/status` → `{setup_completed, setup_available, claim_required}` and it submits to
`POST /api/setup/admin-password` (`routers/setup.py`). Both are unauthenticated.

The router guard (`router/index.js`) sends `/setup` to `/login` once `setup_completed` is true,
and sends every other route to `/setup` while it is false. The endpoint refuses with `403` as
its very first check whenever a usable admin account already exists, and again whenever
`setup_completed` is true — both before the password is validated or hashed — so one negative
call against a set-up instance changes nothing. Step 1 gates on that flag before Step 5 runs.

Replaces the January flow that expected a "Setup Required" message on the login page and
`setup_required` from `GET /api/auth/mode`, and that wiped the database to get there.

**Part A** below runs unattended on any set-up instance. **Part B** (the real walkthrough) is
manual-only.

## Prerequisites

- [ ] Logged in to `http://localhost` as `admin` with `ADMIN_PASSWORD` from `.env`
- [ ] `curl`, `jq` and `openssl` available in the shell

## Test: Part A — status on a set-up instance

### Step 1: Status endpoint reports completed
**Action**:
- Run (no auth header):
```bash
curl -s -w '\nHTTP %{http_code}\n' http://localhost:8000/api/setup/status
```
**Expected**:
- [ ] `HTTP 200`
- [ ] `setup_completed` is `true`
- [ ] `setup_available` is `true`
- [ ] `claim_required` is present; record its value (`null` or `"instance-id"`). The response never contains a secret.
- [ ] If `setup_completed` is `false`, stop: record `BLOCKED (instance not set up)` and do **not** continue — Part B is manual-only

### Step 2: `/setup` redirects while signed in
**Action**:
- In the logged-in browser, navigate to `http://localhost/setup`.
**Expected**:
- [ ] The setup form does **not** render (no "Create your admin account" heading)
- [ ] The final URL is `/` (the guard sends `/setup` to `/login`, and a signed-in session is sent on from `/login` to the Dashboard)
- [ ] The top nav (Dashboard, Library, Operations, Settings, Workspace) is visible

### Step 3: `/setup` redirects for a signed-out visitor
**Action**:
- Open a fresh browser context with no stored session (a new incognito context/tab group; do not sign out of the main session) and navigate to `http://localhost/setup`.
- If a clean context cannot be opened, record `SKIPPED (no clean context)` and continue.
**Expected**:
- [ ] The final URL is `/login`
- [ ] The page shows the "Trinity" heading and "Sign in to manage your agents" — not the setup card
- [ ] No "Welcome to Trinity" hero and no "Create admin account & continue →" button

### Step 4: The guard makes exactly the status call
**Action**:
- In the context from Step 3 (or the main one), inspect network requests made while loading `/setup`.
**Expected**:
- [ ] A `GET /api/setup/status` request with status `200`
- [ ] **No** `POST /api/setup/admin-password` request was made by the page
- [ ] No console errors

## Test: Part A — the endpoint refuses a second setup

### Step 5: One negative provisioning call
**Action**:
- Send a single well-formed request with a throwaway random password (any 12+ character
  string; it is generated here and never stored or printed). The endpoint must refuse before
  using it. Send this **once** — do not loop.
```bash
PW="$(openssl rand -hex 12)Aa!"
curl -s -w '\nHTTP %{http_code}\n' -X POST http://localhost:8000/api/setup/admin-password \
  -H 'Content-Type: application/json' \
  -d "{\"email\": \"user@example.com\", \"password\": \"$PW\", \"confirm_password\": \"$PW\"}"
unset PW
```
**Expected**:
- [ ] `HTTP 403`
- [ ] `detail` begins "This instance already has an administrator account." (if it instead reads "Setup already completed. Password cannot be changed through this endpoint.", record that — it is the second refusal in the same handler and also a pass)
- [ ] The body has no `success` key

### Step 6: A malformed body is rejected at validation
**Action**:
- Run:
```bash
curl -s -w '\nHTTP %{http_code}\n' -X POST http://localhost:8000/api/setup/admin-password \
  -H 'Content-Type: application/json' -d '{}'
```
**Expected**:
- [ ] `HTTP 422` (missing required fields `password`, `confirm_password`, `email`)

### Step 7: Nothing changed
**Action**:
- Re-run the Step 1 command.
- Mint a fresh admin token with the unchanged `ADMIN_PASSWORD` from `.env`:
```bash
ADMIN_PASSWORD=$(grep -E '^ADMIN_PASSWORD=' .env | cut -d= -f2-)
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8000/api/token \
  --data-urlencode 'username=admin' --data-urlencode "password=${ADMIN_PASSWORD}"
```
- Reload the Dashboard in the main (logged-in) browser.
**Expected**:
- [ ] `setup_completed` is still `true`
- [ ] The token call returns `200` — the admin password was not altered by Step 5
- [ ] The Dashboard still loads signed in; no redirect to `/setup` or `/login`

### Step 8: Protected routes do not fall back to setup
**Action**:
- In the main browser, navigate to `http://localhost/settings` and then `http://localhost/agents`.
**Expected**:
- [ ] `/settings` renders the Settings page (admin)
- [ ] `/agents` lands on `/?view=list`
- [ ] Neither navigation passes through `/setup`

## Cleanup / Restore

Nothing to restore. Part A sends two refused requests (Steps 5–6) that write nothing: the `403`
is raised before any password or email handling, and the `422` is raised before the handler runs.
Close the extra browser context opened in Step 3.

## Manual-only (not run unattended)

> **WARNING — destructive.** Part B needs a **throwaway stack with an empty database and a
> blank `ADMIN_PASSWORD`**. It creates the admin account and sets its password, and it triggers
> first-run seeding (default agents are created in the background). Never run it against the
> shared local instance or anything with data you want to keep. Use a password you generate for
> the occasion and do not write it into any report or file.

On a stack where `GET /api/setup/status` returns `setup_completed: false`:

1. **Forced redirect** — opening `/`, `/login` or `/settings` lands on `/setup`. Public chat
   links (`/chat/:token`) are exempt.
2. **Page content** — left: eyebrow "Trinity · First-time setup", heading "Welcome to Trinity",
   chips "Governed", "Auditable", "Your infrastructure". Right: card "Create your admin account"
   with the line "This sets up the owner of this Trinity instance."
3. **Fields** — "Admin email *" (placeholder `you@company.com`, hint "You'll sign in with this
   email and your password."); "Password *" (placeholder "Enter password (12+ characters)") with
   an eye toggle; "Confirm password *" (placeholder "Confirm your password"); "Company /
   organization (optional)" (placeholder "Acme Inc."); a checkbox "Occasionally email me
   important security & product updates."
4. **Instance-claim variant** — only when `claim_required` is `"instance-id"`: an extra first
   field "EC2 instance ID *" (placeholder `i-0abc123def4567890`, inline "Format: i-0abc123…"
   when malformed); the email label becomes "Admin email (optional)" with the hint "Leave blank
   to sign in as admin."; the updates checkbox is disabled while the email is empty. A wrong ID
   shows "That instance ID does not match this server." and stays on the page.
5. **Password rules** — five live indicators: "12+ characters", "Uppercase", "Lowercase",
   "Number", "Symbol". A strength word appears beside the label (Very Weak → Weak → Fair → Good
   → Strong → Excellent) with a meter. With both password fields filled, the confirm row shows
   "Passwords match" or "Passwords do not match".
6. **Submit gating** — the button "Create admin account & continue →" stays disabled until the
   email is valid, all five rules are met and the passwords match. Below it: "Available only
   until your admin account is created." and "Prefer the classic login? Sign in as `admin` with
   this password anytime."
7. **Submit** — the button reads "Creating account…", then the page signs in automatically with
   the email + password just chosen and navigates to `/`. If the automatic sign-in fails it
   lands on `/login` instead.
8. **First-run overlay** — on the Dashboard a setup overlay opens with a step rail. Record the
   steps shown; "Connect Claude" is tagged "Required". "Finish later" (or Esc) opens a dialog
   "Finish setup later?" with buttons "Finish later" and "Keep setting up".
9. **Self-disabling** — after success `GET /api/setup/status` returns `setup_completed: true`,
   `/setup` redirects to `/login`, and repeating the POST returns `403`. Submitting a stale open
   setup tab shows "Setup has already been completed." and moves to `/login` after ~2 s.
10. **Sign-in afterwards** — on `/login`, the admin form accepts either `admin` or the email
    entered in step 3 with the new password.

## Critical Validations

1. `GET /api/setup/status` → `setup_completed: true`.
2. `/setup` never renders the setup card on a set-up instance (signed in → `/`, signed out → `/login`).
3. `POST /api/setup/admin-password` → `403` with the "already has an administrator account" detail.
4. After the negative call the admin can still mint a token with the unchanged password.

## Success Criteria

- [ ] All four critical validations hold
- [ ] No request in Part A returned `200` from `POST /api/setup/admin-password`
- [ ] No literal password appears in the run's report or logs

## Troubleshooting

- **`/setup` renders the form**: `setup_completed` is false on this instance, or the status
  call failed — the guard assumes "completed" only when the call errors. Check Step 1 first.
- **Step 5 returns `400`/`200` instead of `403`**: the instance has no usable admin account.
  This is a real finding on a shared instance — stop immediately and report; do not retry.
- **Step 7 token call returns `401`**: either `.env` does not match the running backend (check
  whether it also failed before Step 5), or Step 5 changed the password — report as Critical.
- **Status result looks stale in the browser**: the guard caches the status for 5 seconds.
