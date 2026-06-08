import { test, expect, request } from '@playwright/test'

/**
 * Dispatch circuit-breaker "circuit open" badge e2e (#526, RELIABILITY-007).
 *
 * Verifies the AgentHeader badge renders DISTINCTLY (danger/red styling, not
 * the grey stopped/capacity styling) when the agent's dispatch breaker is open.
 *
 * The badge is driven by `agent.circuit_breaker_state`, which AgentDetail's
 * store sets from a secondary GET /api/agents/{name}/circuit-breaker call. We
 * route-mock THAT response rather than forcing a real breaker open (which would
 * need Redis seeding + the global flag) — this spec is about the frontend
 * rendering the state distinctly, which the mock exercises faithfully.
 *
 * The real backend still serves GET /api/agents/{name}, so a TEST_AGENT must
 * exist; the suite skips cleanly if the detail page can't load.
 */

const TEST_AGENT = process.env.TEST_AGENT || 'trinity-system'

function cbResponse(state) {
  return {
    agent_name: TEST_AGENT,
    dispatch: { state, failure_count: state === 'open' ? 3 : 0, retry_after_seconds: state === 'open' ? 30 : 0 },
    transport: { state: 'closed', failure_count: 0, cooldown_remaining: 0 },
    open: state === 'open',
    config: { enabled: true, global_enabled: true },
  }
}

test.describe('dispatch circuit-breaker badge (#526)', () => {
  test.beforeEach(async ({ page, baseURL }) => {
    // Skip if the agent detail page doesn't render (no such agent on this stack).
    const api = await request.newContext({ baseURL })
    const ok = await api
      .get(`/api/agents/${TEST_AGENT}`)
      .then((r) => r.ok())
      .catch(() => false)
    await api.dispose()
    test.skip(!ok, `TEST_AGENT '${TEST_AGENT}' not found on this stack — skipping badge e2e`)
  })

  test('@smoke renders distinct danger badge when dispatch breaker is open', async ({ page }) => {
    await page.route('**/api/agents/*/circuit-breaker', (route) =>
      route.fulfill({ json: cbResponse('open') })
    )
    await page.goto(`/agents/${TEST_AGENT}`)

    const badge = page.getByTestId('circuit-open-badge')
    await expect(badge).toBeVisible({ timeout: 15000 })
    await expect(badge).toContainText(/circuit open/i)

    // Distinct danger styling — the class carries the status-danger (red) token,
    // NOT the grey stopped/capacity styling.
    const cls = await badge.getAttribute('class')
    expect(cls).toMatch(/status-danger/)
    expect(cls).not.toMatch(/bg-gray-/)
  })

  test('badge is absent when the breaker is closed', async ({ page }) => {
    await page.route('**/api/agents/*/circuit-breaker', (route) =>
      route.fulfill({ json: cbResponse('closed') })
    )
    await page.goto(`/agents/${TEST_AGENT}`)
    // Header status badge confirms the page rendered; the circuit badge must not.
    await expect(page.getByTestId('circuit-open-badge')).toHaveCount(0, { timeout: 15000 })
  })
})
