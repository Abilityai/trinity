# Frontend E2E Tests

Playwright-based end-to-end tests for the Trinity frontend (#556).

## Run locally

```bash
# 1. Start a Trinity stack (the tests don't spin one up themselves)
./scripts/deploy/start.sh

# 2. Run the tests
cd src/frontend
ADMIN_PASSWORD=<your-admin-password> npm run test:e2e
```

`ADMIN_PASSWORD` is required — `e2e/auth.setup.js` uses it to log in once and
caches the session in `e2e/.auth/admin.json` (gitignored).

## Useful flags

```bash
npm run test:e2e:smoke      # only @smoke-tagged tests (CI parity)
npm run test:e2e:headed     # run with visible browser
npm run test:e2e:ui         # interactive Playwright UI
npm run test:e2e:update     # update visual regression snapshots
```

After a run, the HTML report is at `e2e/playwright-report/index.html`.

## Spec tags

Each test gets a tag in its name to control where it runs:

| Tag | Runs in CI? | Purpose |
|---|---|---|
| `@smoke` | ✅ always | Cross-page health checks. Fast (~5s), zero flakiness. Must always pass. |
| `@visual` | ❌ local only | Visual regression / screenshot baselines. Deferred until cross-platform baseline capture is sorted (issue #596). |
| `@interactive` | ❌ local only | Forms, modals, multi-step flows. Local-only until stabilised. |

CI runs `npm run test:e2e:smoke` (filters by `@smoke`). To promote a spec to CI, simply rename it to include `@smoke` — no workflow changes needed.

```js
// CI + local
test('@smoke dashboard renders', async ({ page }) => { ... })

// Local only (until visual regression infra lands — #596)
test('@visual /monitoring summary cards', async ({ page }) => { ... })

// Local only (interactive flow)
test('@interactive create agent end-to-end', async ({ page }) => { ... })
```

## CI

The workflow lives at `.github/workflows/frontend-e2e.yml` and stands up the
full Trinity stack before running tests (~5 min total). Since #1526 the `ui`
label is **no longer the gate** — the suite runs on:

1. **Nightly** on `dev` (07:00 UTC) — a red night opens/updates a tracking issue.
2. **Any PR touching `src/frontend/**`** — automatically, via the `changes` job.
3. **The `ui` label** — still a manual opt-in on NON-frontend PRs, or to force a run.
4. **`workflow_dispatch`** — on demand.

Two things follow, and both matter when you are judging whether a change is
covered:

- **CI runs `npm run test:e2e:smoke` only.** `@visual` and `@interactive`
  specs never run there, so a green CI says nothing about them.
- **The workflow is advisory, not a required merge gate** (an explicit decision
  in #1526 — promoting it needs a flake budget first, #596). `dev`'s required
  checks are `Analyze (python)`, `Analyze (javascript-typescript)`,
  `schema-parity`, and `verify-non-root`.

So a claim about the **full** suite has to be evidenced by a local run against
a live stack, not by a CI badge.

## Fixture agents

Some specs need a real agent on the stack. Each reads an env var with a
`testfix` default — an agent that does **not** exist on a typical instance:

| Spec | Env var | Needs |
|---|---|---|
| `loops-panel` | `LOOPS_TEST_AGENT` | exists, running, **Claude runtime** |
| `continue-as-chat` | `SESSION_TEST_AGENT` | exists, running, **Claude runtime** |
| `workspace-absorbs-session` (`@interactive` only) | `SESSION_TEST_AGENT` | exists |
| `portal-agent-page-overview` | `PORTAL_TEST_AGENT` | exists, visible to admin |
| `timeline-cancelled-bar`, `honest-failed-states` | `TEST_AGENT` | exists |
| `circuit-breaker-badge` | `TEST_AGENT` | exists |

**The contract (#2199): a missing fixture reads as SKIPPED, never broken —
and the probe must be authenticated.** `GET /api/agents/{name}` is behind
`AuthorizedAgent` and Trinity's JWT lives in localStorage, not a cookie, so an
unauthenticated probe 401s and the test skips on *every* run: silent false
confidence, which is worse than a red test. The inverse holds too: **only a
definitive 404 reads as "fixture absent"** — a 401/403 (setup didn't run, JWT
died with a backend restart), a 5xx, or a transport error means the *probe*
broke, and `agentExists` throws so the test fails with the real diagnosis
instead of skipping under a false "agent not found". Use the shared
`e2e/helpers/agent-probe.js` — do not hand-roll a probe.

**Borrowing an agent's display label (#2358).** `e2e/helpers/agent-label.js`
(`FIXTURE_LABEL` / `pickLabelFixture` / `readLabel` / `writeLabel`) is the
shared borrow-and-restore for specs that must see a labelled agent. It prefers
an agent that is already labelled and falls back to `trinity-system` (CI's
whole fleet). **Read the prior label, restore it in `test.afterEach`, and keep
such tests in a `serial` block**: a `finally` inside a body that times out is
not reliably run, the config is `fullyParallel: true`, and `AgentHeader.vue`
hides the label pencil for system agents — a label stranded on
`trinity-system` can only be cleared through the API. Same loud-never-silent
rule as the probe: every non-2xx throws with the real status.

`serial` orders tests within ONE file, and two specs borrow the same agent
(`dashboard-list-view` and `dashboard-grid-view`), so **run them together with
`--workers=1`** — `workers` is pinned to 1 on CI but left at the default
locally. `FIXTURE_LABEL` is the only label these helpers write and is
deliberately self-identifying; `readLabel` REFUSES to return it, so a second
worker (or a run killed mid-borrow) fails loudly before writing instead of
restoring a test artefact as if it were the operator's own label. If you ever
see it on a live agent: `PUT /api/agents/<name>/label {"label": null}`.

> **Known gap:** `circuit-breaker-badge.spec.js` still carries the legacy
> unauthenticated probe, so both its tests skip on every run. It was left as-is
> deliberately: with an authenticated probe its `@smoke` test **fails** — a
> **test** defect, not a product one (#2210): the spec mocks the standalone
> `/circuit-breaker` endpoint, which the agent page never calls (the badge reads
> the `circuit_breaker` block embedded in the `GET /api/agents/{name}`
> response), so one test fails and the sibling passes vacuously. Un-skipping it
> here would turn the nightly signal red for a cause that belongs in #2210.
> Convert the probes per #2210, then `test.skip(true, 'blocked by #2210')`
> until it lands.

The probe checks **existence only**. If a spec needs the agent running or on a
particular runtime, say so in the spec header: reporting a *present but
unsuitable* fixture as "absent" would be a false diagnosis.

```bash
# point the fixture-dependent specs at a real agent
LOOPS_TEST_AGENT=my-agent SESSION_TEST_AGENT=my-agent PORTAL_TEST_AGENT=my-agent \
  ADMIN_PASSWORD=<pw> npm run test:e2e
```

## Adding tests

- Smoke tests live in `e2e/smoke.spec.js` — the lightweight cross-page checks
- New flows go in their own `*.spec.js` next to the smoke file
- Visual regression: use `await expect(page).toHaveScreenshot()`. Snapshots
  are committed in `e2e/<spec>.spec.js-snapshots/`. Run
  `npm run test:e2e:update` after intentional UI changes, then commit the
  updated PNGs.

## Why this layer exists

The frontend has no other automated test coverage today. E2E tests catch:
- Login regressions
- Top-level routing breakage
- Auth boundary violations exposed via the UI
- Color drift on the design system (with visual regression)

Cheaper layers (Vitest unit tests, type checking) are tracked in #556
Phase 1 / Phase 3 — separate follow-ups.
