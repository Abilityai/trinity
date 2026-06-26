import { test, expect, request } from '@playwright/test'
import fs from 'fs'

/**
 * Cancelled-execution timeline bar e2e (#1332).
 *
 * Verifies the Dashboard ReplayTimeline renders a user-cancelled execution as a
 * DISTINCT neutral (slate) bar — NOT the red of a genuine failure. The bug: a
 * cancelled terminal carries `error="Execution terminated by user"`, which made
 * the bar's `hasError` truthy and rendered it red. The fix detects
 * `status === 'cancelled'`, excludes it from `hasError`, and gives it the slate
 * `#94a3b8` color before the error/in-progress/trigger branches.
 *
 * The timeline is fed by GET /api/activities/timeline → the network store's
 * `historicalCollaborations`. We route-mock THAT response with a single
 * cancelled `schedule_start` activity rather than orchestrating a real
 * terminate (which needs a live long-running turn + race timing) — this spec is
 * about the frontend rendering a cancelled state distinctly, which the mock
 * exercises faithfully. The real backend still serves /api/agents, so a
 * TEST_AGENT must exist; the suite skips cleanly otherwise.
 */

const TEST_AGENT = process.env.TEST_AGENT || 'trinity-system'
const SLATE = '#94a3b8' // getBarColor() cancelled color
const RED = '#ef4444' // getBarColor() error color

function cancelledTimelineResponse() {
  const now = new Date().toISOString()
  return {
    activities: [
      {
        id: 'act-e2e-1332-cancel',
        agent_name: TEST_AGENT,
        activity_type: 'schedule_start',
        activity_state: 'cancelled',
        started_at: now,
        created_at: now,
        related_execution_id: 'exec-e2e-1332-cancel',
        duration_ms: 30000,
        triggered_by: 'schedule',
        // The cancel carries an error string — the regression that used to turn
        // the bar red. The fix must keep it slate despite this.
        error: 'Execution terminated by user',
        details: JSON.stringify({
          schedule_name: 'e2e-cancel',
          message_preview: 'long running task',
        }),
      },
    ],
  }
}

test.describe('cancelled execution timeline bar (#1332)', () => {
  test.beforeEach(async ({ baseURL }) => {
    // The agent-existence probe MUST be authenticated: /api/agents/{name} is
    // behind AuthorizedAgent, and Trinity's JWT lives in localStorage (not a
    // cookie), so a raw request context carries no credential and 401s — which
    // makes test.skip() fire on EVERY run (silent false confidence in CI). Reuse
    // the token the `setup` project persisted into the storageState file.
    let token
    try {
      const state = JSON.parse(fs.readFileSync('e2e/.auth/admin.json', 'utf8'))
      token = state.origins
        ?.flatMap((o) => o.localStorage || [])
        .find((i) => i.name === 'token')?.value
    } catch {
      // No storageState yet (setup didn't run) — leave token undefined; the
      // probe will 401 and the test skips, which is the correct degraded state.
    }
    const api = await request.newContext({
      baseURL,
      extraHTTPHeaders: token ? { Authorization: `Bearer ${token}` } : {},
    })
    const ok = await api
      .get(`/api/agents/${TEST_AGENT}`)
      .then((r) => r.ok())
      .catch(() => false)
    await api.dispose()
    test.skip(!ok, `TEST_AGENT '${TEST_AGENT}' not found on this stack — skipping timeline e2e`)
  })

  test('@smoke cancelled execution renders a slate bar, not red', async ({ page }) => {
    await page.route('**/api/activities/timeline*', (route) =>
      route.fulfill({ json: cancelledTimelineResponse() })
    )

    await page.goto('/')

    // Switch to Timeline mode so ReplayTimeline mounts and renders the bars.
    await page.getByRole('button', { name: 'Timeline' }).click()

    // The cancelled execution renders as a slate bar...
    const cancelledBar = page.locator(`rect[fill="${SLATE}"]`)
    await expect(cancelledBar.first()).toBeVisible({ timeout: 15000 })

    // ...and NOT as a red error bar (the regression this fixes).
    await expect(page.locator(`rect[fill="${RED}"]`)).toHaveCount(0)

    // The bar's tooltip identifies it as cancelled.
    await expect(cancelledBar.first().locator('title')).toContainText(/cancelled/i)
  })
})
