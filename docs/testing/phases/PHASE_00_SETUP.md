# Phase 0: Preflight (non-destructive)

> **Purpose**: Confirm the instance, the fixture agents and the login page are in the state every other phase assumes.
> **Duration**: ~5 minutes
> **Assumes**: the local stack is up; nothing about browser login state
> **Output**: backend healthy, admin token mintable, setup completed, fixture trio running, fixture templates resolvable, login page renders cleanly
> **Last verified**: 2026-10-09 against source (not yet browser-run)

---

## Background

Phase 0 is a read-only gate. **This phase deletes nothing, creates nothing and changes nothing.**
It only reads: `GET /health`, `POST /api/token` (mints a token, no state change),
`GET /api/setup/status`, `GET /api/agents`, `GET /api/templates/...`, and the login page at
`http://localhost/login`.

Replaces the January flow that deleted eight agents by name (including the live fixtures) and
expected eight GitHub test templates. The fixtures are now three long-lived local agents —
`test-echo`, `test-counter`, `test-delegator` — and a missing or stopped fixture is a **FAIL to
report**, never something this phase repairs.

## Prerequisites

- [ ] Stack reachable at `http://localhost` (UI) and `http://localhost:8000` (API)
- [ ] `ADMIN_PASSWORD` is readable from the repo's `.env` (the value lives there; compose only passes it through)
- [ ] `curl` and `jq` available in the shell

## Test: Backend

### Step 1: Health endpoint
**Action**:
- Run:
```bash
curl -s -w '\nHTTP %{http_code}\n' http://localhost:8000/health
```
**Expected**:
- [ ] Last line is `HTTP 200`
- [ ] Body has `"status": "healthy"` and a `timestamp`
- [ ] If the status is `503`, the body has `"status": "unhealthy"` and a `migrations` object (`applied`, `expected`, `first_pending`) — record it verbatim and FAIL the phase

### Step 2: Mint an admin token
**Action**:
- Log in as `admin` with `ADMIN_PASSWORD` from `.env`. The endpoint is form-encoded, not JSON:
```bash
ADMIN_PASSWORD=$(grep -E '^ADMIN_PASSWORD=' .env | cut -d= -f2-)
TOKEN=$(curl -s -X POST http://localhost:8000/api/token \
  --data-urlencode 'username=admin' \
  --data-urlencode "password=${ADMIN_PASSWORD}" | jq -r '.access_token // empty')
test -n "$TOKEN" && echo "token ok" || echo "NO TOKEN"
```
**Expected**:
- [ ] Prints `token ok`
- [ ] The token starts with `eyJ` (do not print or record the full token or the password)

### Step 3: Unauthenticated calls are refused
**Action**:
- Run:
```bash
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/api/agents
```
**Expected**:
- [ ] HTTP status `401` (a JSON refusal, not a connection error)

### Step 4: Setup is completed
**Action**:
- Run (no auth needed):
```bash
curl -s http://localhost:8000/api/setup/status | jq
```
**Expected**:
- [ ] `setup_completed` is `true`
- [ ] `setup_available` is `true`
- [ ] A `claim_required` key is present (its value is `null` on most instances — record it)

## Test: Fixtures

### Step 5: The fixture trio is running
**Action**:
- Run:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents \
  | jq -r '.[] | select(.name=="test-echo" or .name=="test-counter" or .name=="test-delegator") | "\(.name) \(.status)"'
```
**Expected**:
- [ ] Exactly three lines: `test-echo running`, `test-counter running`, `test-delegator running`
- [ ] If any of the three is missing or not `running`: record which one and its status, mark the phase **FAIL**, and stop. Do **not** create, start, restart or delete anything to fix it.

### Step 6: `trinity-system` is present (observe only)
**Action**:
- Run:
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/trinity-system \
  | jq '{name, status, is_system, can_delete}'
```
**Expected**:
- [ ] `name` is `trinity-system`; record `status`
- [ ] `can_delete` is `false`
- [ ] Nothing was changed on it

### Step 7: The three fixture templates resolve by id
**Action**:
- The fixture templates are marked `hidden: true`, so they are deliberately **absent** from the
  catalog list and must be fetched by id:
```bash
for t in test-echo test-counter test-delegator; do
  curl -s -o /dev/null -w "local:$t %{http_code}\n" \
    -H "Authorization: Bearer $TOKEN" "http://localhost:8000/api/templates/local:$t"
done
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/templates \
  | jq '[.[] | select(.id | startswith("local:test-"))] | length'
```
**Expected**:
- [ ] `local:test-echo 200`, `local:test-counter 200`, `local:test-delegator 200`
- [ ] The catalog count of `local:test-*` ids is `0` (hidden fixtures are not listed)
- [ ] `GET /api/templates` itself returned a JSON array (HTTP 200)

## Test: Frontend

### Step 8: Login page renders
**Action**:
- Open `http://localhost/login` in the browser.
- If the browser already holds a session you are redirected to `/` (the Dashboard). In that case
  record `already signed in`, check the top nav shows Dashboard, Library, Operations, Settings,
  Workspace, and skip the remaining bullets of this step.
**Expected** (signed-out browser):
- [ ] URL stays `/login`
- [ ] Heading "Trinity" with the line "Sign in to manage your agents"
- [ ] Either a "Send Verification Code" button with a "🔐 Admin Login" button below it, or the admin form directly with a "Sign In as Admin" button — record which
- [ ] The page is not blank and shows no error card

### Step 9: Signed-out routing
**Action**:
- Only if Step 8 found a signed-out browser: navigate to `http://localhost/` and then to `http://localhost/setup`.
**Expected**:
- [ ] `/` redirects to `/login`
- [ ] `/setup` redirects to `/login` (setup is completed, so the setup form must not render)

### Step 10: Clean console and narrow width
**Action**:
- Read the browser console messages for the page loads in Steps 8–9.
- Resize the viewport to 390 px wide on `/login` (or on `/` if already signed in), then restore the original size.
**Expected**:
- [ ] No console errors (warnings are acceptable; record any error text verbatim)
- [ ] No failed (4xx/5xx) network requests other than an expected `401` for a signed-out probe
- [ ] At 390 px the sign-in card (or the Dashboard) fits without a horizontal page scrollbar

## Cleanup / Restore

Nothing to restore. **This phase deletes nothing** and writes nothing: the only non-GET request
is the token mint in Step 2, which changes no state.

## Critical Validations

1. `GET /health` → `200` / `"status": "healthy"`.
2. `POST /api/token` returns an access token for `admin`.
3. `GET /api/setup/status` → `setup_completed: true`.
4. `test-echo`, `test-counter`, `test-delegator` all present with status `running`.
5. `/login` renders with no console errors.

## Success Criteria

- [ ] All five critical validations hold
- [ ] The three `local:test-*` templates resolve by id
- [ ] No agent, template, setting or file was created, modified or deleted

## Troubleshooting

- **`NO TOKEN` in Step 2**: `ADMIN_PASSWORD` in `.env` is empty or differs from the running
  backend's value (the backend reads it at start). Record the HTTP body of the token call; do not
  attempt other passwords — repeated failures are rate-limited.
- **`503` from `/health`**: a SQLite migration has not been applied; `first_pending` names it.
- **A fixture is missing or stopped**: this is the phase's FAIL condition. The fixtures are
  provisioned outside the test run; report it rather than creating one.
- **`setup_completed: false`**: the instance has never been set up; every UI route will redirect
  to `/setup`. Report and stop — Phase 19 describes that flow as manual-only.
