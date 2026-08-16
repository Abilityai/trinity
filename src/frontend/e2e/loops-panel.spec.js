import { test, expect, request } from '@playwright/test'
import { agentExists, missingAgentReason } from './helpers/agent-probe.js'

/**
 * Loops tab e2e (#1106 / #740 Phase 2).
 *
 * Drives the new Loops tab on the Agent Detail page against a live stack:
 *   - tab is present (no feature flag — always visible)
 *   - "Run Loop" opens the form with the documented inputs
 *   - starting a 1-run loop lands a row that reaches a terminal status,
 *     and the expanded detail shows the per-run table
 *
 * The start path dispatches a real Claude execution, so the start/run
 * test is marked @interactive (10–60s) — opt-in via
 * `npm run test:e2e -- loops-panel.spec`. The form-render test is also
 * @interactive only because it needs the agent running to enable the
 * "Run Loop" button.
 *
 * Required env: ADMIN_PASSWORD (enforced by auth.setup.js) and
 * LOOPS_TEST_AGENT (defaults to "testfix"). The agent must already exist,
 * and must be on the Claude runtime for the start/run test to be meaningful;
 * beforeAll starts it if it isn't running.
 *
 * A MISSING fixture agent reads as SKIPPED, never broken (#2199) — the probe
 * is authenticated (see e2e/helpers/agent-probe.js for why that matters).
 */

const TEST_AGENT = process.env.LOOPS_TEST_AGENT || 'testfix'

let api
let token
// Set by beforeAll; read by the beforeEach guard. `test.skip()` is called from
// beforeEach (the verified in-repo pattern) — NOT from beforeAll, where it does
// not apply to the tests.
let agentReady = false
let skipReason = ''

test.beforeAll(async ({ baseURL }) => {
  api = await request.newContext({ baseURL })
  const loginResp = await api.post('/api/token', {
    form: { username: 'admin', password: process.env.ADMIN_PASSWORD || '' },
  })
  if (!loginResp.ok()) {
    // A missing/incorrect ADMIN_PASSWORD is an environment gap, not a product
    // failure — skip rather than throw (#2199).
    skipReason = `admin login failed (${loginResp.status()}) — check ADMIN_PASSWORD`
    return
  }
  token = (await loginResp.json()).access_token

  if (!(await agentExists(TEST_AGENT, { baseURL, token }))) {
    skipReason = missingAgentReason(TEST_AGENT, 'LOOPS_TEST_AGENT')
    return
  }
  agentReady = true

  // Ensure the agent is running so the "Run Loop" button is enabled.
  // Best-effort: a 409/already-running response is fine.
  await api.post(`/api/agents/${TEST_AGENT}/start`, {
    headers: { Authorization: `Bearer ${token}` },
  })
})

test.afterAll(async () => {
  if (api) await api.dispose()
})

test.describe('loops tab', () => {
  test.beforeEach(() => {
    test.skip(!agentReady, skipReason)
  })

  test('@interactive tab is visible and Run Loop opens the form', async ({ page }) => {
    await page.goto(`/agents/${TEST_AGENT}`)

    const loopsTab = page.getByRole('button', { name: 'Loops', exact: true })
    await expect(loopsTab).toBeVisible({ timeout: 15000 })
    await loopsTab.click()

    // Empty/intro copy from LoopsPanel header.
    await expect(page.getByRole('heading', { name: 'Loops' })).toBeVisible()

    const runBtn = page.getByRole('button', { name: 'Run Loop' })
    await expect(runBtn).toBeVisible()
    await runBtn.click()

    // Form fields from the documented inputs.
    await expect(page.getByText('Message template')).toBeVisible()
    await expect(page.getByText('Max runs', { exact: false })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Start Loop' })).toBeVisible()
  })

  test('@interactive starting a 1-run loop lands a terminal row with per-run detail', async ({ page }) => {
    await page.goto(`/agents/${TEST_AGENT}`)
    await page.getByRole('button', { name: 'Loops', exact: true }).click()
    await page.getByRole('button', { name: 'Run Loop' }).click()

    // Fill the form: 1 run, trivial message.
    await page.getByPlaceholder(/Process item/).fill('Reply with just the word OK.')
    // Max runs input — set to 1 for a fast single iteration.
    const maxRuns = page.locator('input[type="number"]').first()
    await maxRuns.fill('1')

    await page.getByRole('button', { name: 'Start Loop' }).click()

    // A loop row appears (status badge + "Run x / 1").
    await expect(page.getByText(/Run \d+ \/ 1/)).toBeVisible({ timeout: 15000 })

    // It reaches a terminal status (completed/failed/stopped). Real Claude
    // call — generous timeout. The badge text is one of the terminal states.
    await expect(
      page.getByText(/^(completed|failed|stopped)$/).first()
    ).toBeVisible({ timeout: 90000 })

    // Expand the loop and assert the per-run table renders.
    await page.getByText(/Run \d+ \/ 1/).click()
    await expect(page.getByText('Runs', { exact: true })).toBeVisible({ timeout: 10000 })
  })
})
