---
name: test-runner-ui-e2e
description: Run the Playwright frontend e2e suite against a live Trinity stack, analyze failures, and update visual regression snapshots. Use after UI changes, before merging a PR with the `ui` label, or when the `frontend-e2e` CI workflow fails.
allowed-tools: [Bash, Read, Write, Edit, Grep, Glob]
user-invocable: true
argument-hint: "[<filter>] [--update-snapshots] [--headed] [--ui]"
automation: manual
---

# Test Runner — UI E2E

Run the Playwright e2e suite that lives in `src/frontend/e2e/` against a live Trinity stack. Frontend equivalent of `/test-runner` (which covers backend pytest only).

## When to Use

- After any change in `src/frontend/src/` to confirm the UI still works end-to-end
- Before merging a PR carrying the `ui` label (the same PRs that trigger `frontend-e2e` in CI)
- When the `frontend-e2e` CI job fails — to reproduce locally and diagnose
- After a design-system migration to update visual regression snapshots
- When adding new specs to `e2e/` to validate them locally before pushing

## Usage

```
/test-runner-ui-e2e                    # Run the full suite against http://localhost
/test-runner-ui-e2e smoke              # Run specs matching "smoke"
/test-runner-ui-e2e --update-snapshots # Update visual regression baselines
/test-runner-ui-e2e --headed           # Run with a visible browser
/test-runner-ui-e2e --ui               # Open Playwright's interactive UI
```

## Arguments

| Arg | Description |
|---|---|
| `<filter>` | Pattern passed to `playwright test --grep` |
| `--update-snapshots` | Regenerate `e2e/**/*-snapshots/*.png` baselines after intentional UI changes |
| `--headed` | Run with a visible Chromium window (debugging) |
| `--ui` | Open Playwright's interactive test UI (debugging) |

## Process

### Step 1: Prerequisites check

Before running tests, verify the stack is up. The e2e workflow assumes Trinity is reachable at the configured `baseURL` (default `http://localhost`).

```bash
# 1. Trinity backend health
curl -fsS http://localhost:8000/health

# 2. Trinity frontend (Vite dev or nginx) on port 80
curl -sI http://localhost/ | head -1

# 3. Containers
docker ps --filter 'name=trinity' --format 'table {{.Names}}\t{{.Status}}'
```

If backend is down or returning 404, **diagnose and recover** (see "Common failure modes" below) before trying tests. **Do not** skip this step — every failure I've seen has a stack-state precondition.

### Step 2: Resolve admin password

The Playwright auth setup needs `ADMIN_PASSWORD`:

```bash
ADMIN_PASSWORD=$(grep '^ADMIN_PASSWORD=' .env | cut -d= -f2)
test -n "$ADMIN_PASSWORD" || { echo "ADMIN_PASSWORD not set in .env"; exit 1; }
```

Confirm it actually works by hitting the login endpoint directly first:

```bash
curl -s -X POST http://localhost:8000/api/token \
  --data-urlencode "username=admin" --data-urlencode "password=$ADMIN_PASSWORD" \
  | head -c 200
```

If that returns `{"detail":"Not Found"}` the backend is dead. If it returns `Invalid username or password`, the password is wrong. If it returns `{"access_token": "..."}`, you're good to run tests.

### Step 3: Run Playwright

From `src/frontend/`:

```bash
cd src/frontend
rm -rf e2e/.auth/admin.json e2e/test-results 2>/dev/null

# Translate the skill args into npm scripts
SCRIPT="test:e2e"
[[ "$ARGS" == *"--update-snapshots"* ]] && SCRIPT="test:e2e:update"
[[ "$ARGS" == *"--headed"* ]]           && SCRIPT="test:e2e:headed"
[[ "$ARGS" == *"--ui"* ]]               && SCRIPT="test:e2e:ui"

ADMIN_PASSWORD="$ADMIN_PASSWORD" npm run "$SCRIPT" -- ${FILTER:+--grep "$FILTER"}
```

### Step 4: Parse results

A passing run looks like:

```
Running 5 tests using 4 workers
  ✓  1 [setup] › e2e/auth.setup.js:8:6 › authenticate as admin (2.5s)
  ✓  2 [chromium] › e2e/smoke.spec.js:25:7 › smoke › templates page loads (842ms)
  ...
  5 passed (5.1s)
```

Failing runs include attachments (screenshot + video + trace) under `e2e/test-results/<spec-name>/`. The most useful artifact is `error-context.md` which has the page snapshot at the moment of failure.

### Step 5: Generate report

```
## E2E Test Results

**Status**: PASSED / FAILED
**Duration**: Xs
**Specs**: X passed, Y failed
**Browser**: chromium

### Failures (if any)
- spec name: <one-line cause from the page snapshot>
  → File: e2e/<spec>:<line>
  → Page state: <e.g. "stuck on /login", "got 404", "form button disabled">

### HTML report
e2e/playwright-report/index.html

### Recommendations
- ...
```

If snapshots updated: list the new/changed `*.png` paths so the user knows to commit them.

### Step 6: Reminder about the `ui` label

If the run was triggered for a PR-bound change, remind the user:

> Add the `ui` label to your PR so CI runs the same suite (`frontend-e2e.yml`).

## Common failure modes (learned 2026-04-29)

These patterns recur. Diagnose by checking the page snapshot in `e2e/test-results/<spec>/error-context.md`.

### A. Stack is down before tests run

**Symptom**: `auth.setup` fails with `net::ERR_CONNECTION_REFUSED at http://localhost/`.

**Cause**: `trinity-frontend` or `trinity-backend` container exited.

**Diagnose**:
```bash
docker ps --filter 'name=trinity' --format 'table {{.Names}}\t{{.Status}}'
docker logs trinity-backend --tail 20
```

**Recover**:
```bash
docker rm -f trinity-backend 2>/dev/null
docker compose up -d backend
```

If port 8000 is "already allocated" by `com.docker.backend` after `docker rm -f`, you have the **Docker Desktop port-zombie bug**. The only reliable fix is restarting Docker Desktop:
```bash
osascript -e 'quit app "Docker Desktop"'
# wait for daemon stop, then reopen
open -a "Docker Desktop"
```

### B. Stuck on /setup wizard (CI fresh-DB scenario)

**Symptom**: Page snapshot shows `<button disabled type="submit" class="...bg-indigo-600...">` rather than the login form.

**Cause**: Fresh Trinity DB. Migration #19 (`setup_completed_backfill`) runs *before* `_ensure_admin_user`, finds no admin, skips backfill. Admin is then created from `ADMIN_PASSWORD` env var, but `setup_completed` stays `false` — the frontend redirects all routes to `/setup`.

**Recover**:
```bash
docker exec trinity-backend python3 -c "from database import db; db.set_setting('setup_completed', 'true')"
```

This is exactly what the CI workflow's "Skip first-time setup wizard" step does.

### C. "Invalid username or password"

**Symptom**: Page snapshot shows the "Access Denied" panel with that exact text.

**Causes** (in order of likelihood):
1. `ADMIN_PASSWORD` env in your shell ≠ `ADMIN_PASSWORD` in the running backend container (backend was started before `.env` was edited)
2. Username changed (default is `admin`, but `ADMIN_USERNAME` env var can override)

**Diagnose**:
```bash
docker exec trinity-backend printenv ADMIN_USERNAME
docker exec trinity-backend printenv ADMIN_PASSWORD | head -c 5  # first 5 chars to compare
diff <(echo "$ADMIN_PASSWORD") <(docker exec trinity-backend printenv ADMIN_PASSWORD)
```

**Recover**: restart backend so it picks up `.env`:
```bash
docker compose restart backend
```

### D. Submit button found but disabled

**Symptom**: `locator.click: ... element is not enabled`. Click retries 50+ times.

**Cause**: Form has a `disabled` attribute on submit because a precondition isn't met (empty input, validation failure, loading state).

**Diagnose**: read the page snapshot in `error-context.md` — the button's class string usually reveals which form (`bg-blue-600` = login, `bg-indigo-600` = setup wizard, etc.).

**Fix**: usually a selector or precondition issue in the spec, not a stack issue.

### E. Auth setup passes but smoke specs land on /login

**Symptom**: `setup` ✓ but downstream specs fail asserting on dashboard nav links.

**Cause**: `e2e/.auth/admin.json` (storageState) is empty (36 bytes). The auth setup saved state before `localStorage.setItem('token', ...)` completed.

**Diagnose**:
```bash
cat src/frontend/e2e/.auth/admin.json | python3 -c "
import json, sys
d = json.load(sys.stdin)
print('cookies:', len(d.get('cookies',[])))
for o in d.get('origins',[]):
    print(' localStorage entries:', len(o.get('localStorage',[])))
"
```

**Fix**: `auth.setup.js` must `expect.poll(() => page.evaluate(() => localStorage.getItem('token'))).not.toBeNull()` before `storageState({ path: ... })`. If you're on a branch where this isn't yet fixed, cherry-pick from PR #579.

### F. Visual regression baseline mismatch

**Symptom**: `expected ... received ...` with a screenshot diff under `e2e/test-results/<spec>/<name>-diff.png`.

**Causes**:
1. Intentional UI change — run `--update-snapshots` and commit the new PNGs
2. Unintentional regression — review the diff image; fix the code

**Recover (intentional)**:
```bash
ADMIN_PASSWORD=$ADMIN_PASSWORD npm run test:e2e:update
git add src/frontend/e2e/**/*-snapshots/
```

## Coverage today

What the suite currently exercises:
- `auth.setup.js` — admin login flow, JWT persistence
- `smoke.spec.js` — top-nav rendering on `/` and that `/agents`, `/operating-room`, `/templates` load

What's **not** covered (use this to scope new specs):
- Settings, Health (`/monitoring`), Keys (`/api-keys`)
- Any agent detail page (`/agents/:name`)
- Form submission, modals, terminal, file manager
- WebSocket events / real-time updates
- Visual regression on the design system
- Mobile / responsive viewports
- Cross-browser (Chromium only by default)

When adding a spec, prefer **visual regression** for any PR that touches design-system tokens — that's where the harness pays the highest dividend.

## Output

```
## E2E Test Results

**Status**: PASSED
**Duration**: 5.1s
**Specs**: 5 passed
**Browser**: chromium

### Report
HTML: src/frontend/e2e/playwright-report/index.html
Trace (on failure only): e2e/test-results/<spec>/trace.zip
```

Or on failure:

```
## E2E Test Results

**Status**: FAILED
**Specs**: 1 failed, 4 did not run
**Failure category**: B (stuck on /setup wizard)

### Failed
- [setup] authenticate as admin (auth.setup.js:8)
  → Page is /setup wizard, not /login
  → Run: docker exec trinity-backend python3 -c "from database import db; db.set_setting('setup_completed','true')"

### Recovery
Re-run after applying the fix above.
```

## Implementation notes

- This skill does NOT spawn a sub-agent — it's a thin orchestration over `npm run test:e2e`. The diagnostic logic is small enough to live in the skill itself.
- For the failure-classification logic, prefer reading `error-context.md` (Playwright auto-generates it on failure) over parsing CLI output. The page snapshot YAML is the most reliable signal for distinguishing failure modes.
- This skill is the local complement to the `frontend-e2e.yml` CI workflow. CI runs the same `npm run test:e2e` against a fresh dockerized stack on a GitHub-hosted runner — see `docs/memory/feature-flows/...` for the deploy-to-dev story.
- The CI workflow is path-gated by the `ui` label (not by file paths) — adding `ui` to a PR is the trigger. Mention this to users who run this skill locally before pushing.
