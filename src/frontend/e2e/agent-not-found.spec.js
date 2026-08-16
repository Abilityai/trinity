import { test, expect } from '@playwright/test'

/**
 * Agent Detail not-found state (#1914).
 *
 * Regression guard for a BLANK PAGE: navigating to /agents/<unknown> rendered
 * nothing at all. `stores/agents.js::fetchAgent` caught the 404, logged it to
 * the console, and returned `undefined`, so `AgentDetail.vue::loadAgent`'s catch
 * never ran — `error` stayed '' and `agent` stayed null, making BOTH the error
 * banner (`v-if="error && !agent"`) and the agent body (`v-if="agent"`) false.
 * The only evidence was a console line the user never sees.
 *
 * @smoke, deliberately: the whole point is that the agent does NOT exist, so
 * unlike the other AgentDetail specs this needs no fixture agent, no running
 * container, and no Claude dispatch. It is fast and deterministic.
 *
 * The name below must never resolve to a real agent. `sanitize_agent_name`
 * strips a leading underscore (see the `_retention-guard` sentinel in
 * architecture.md), so a name in this shape cannot be created through the API.
 */

const MISSING_AGENT = '_e2e-agent-1914-does-not-exist'

test.describe('agent detail — not found', () => {
  test('@smoke unknown agent renders a not-found state, not a blank page', async ({ page }) => {
    await page.goto(`/agents/${MISSING_AGENT}`)

    const panel = page.getByTestId('agent-not-found')
    await expect(panel).toBeVisible({ timeout: 10000 })
    await expect(panel).toContainText(/agent not found/i)

    // No dead empty state — there is a way out (Product Quality Bar). The exit
    // is the Dashboard, not the Agents list: the agent you asked for isn't
    // there either, so the list is a second dead end for the same question.
    const back = panel.getByRole('link', { name: /back to dashboard/i })
    await expect(back).toBeVisible()
    await back.click()
    await expect(page).toHaveURL(/\/$/)
  })

  test('@smoke not-found copy does not disclose whether the agent exists', async ({ page }) => {
    // The backend returns a UNIFORM 404 for "no such agent" and "you can't see
    // this agent" on purpose — the differential is an enumeration oracle
    // (Invariant #8 / #186). The UI must not reintroduce it by claiming which
    // case this is. This asserts the copy covers BOTH, and pins the absence of
    // an existence claim so a future copy edit can't quietly leak it.
    await page.goto(`/agents/${MISSING_AGENT}`)

    const panel = page.getByTestId('agent-not-found')
    await expect(panel).toBeVisible({ timeout: 10000 })
    await expect(panel).toContainText(/doesn't exist, or you don't have access/i)
    await expect(panel).not.toContainText(/no such agent|does not exist on this instance|was deleted/i)
  })

  test('@smoke the missing agent is fetched ONCE, not twice (#2198 AC 3)', async ({ page }) => {
    // AgentDetail is KeepAlive'd (App.vue), so Vue fires onMounted AND
    // onActivated on the first mount and BOTH awaited `loadAgent()` — two
    // identical 404s for one navigation. The store now joins the in-flight
    // request, and the first-activation sentinel stops the second call being
    // made at all.
    //
    // The URL shape test is the same one the 500 case below uses: it isolates
    // the single-agent GET from `/api/agents`, `/api/agents/sync-health`, etc.
    let singleAgentGets = 0
    await page.route('**/api/agents/*', (route) => {
      if (/\/api\/agents\/[^/?]+(\?|$)/.test(route.request().url())) singleAgentGets++
      return route.continue()
    })

    await page.goto(`/agents/${MISSING_AGENT}`)
    await expect(page.getByTestId('agent-not-found')).toBeVisible({ timeout: 10000 })
    // Settle past the point where a second hook, a route watcher or a status
    // watcher would have fired.
    await page.waitForTimeout(3000)

    expect(singleAgentGets).toBe(1)
  })

  test('@smoke a non-404 failure shows a retryable load error, not the not-found state', async ({ page }) => {
    // Retrying a 404 is pointless; retrying a 500/network blip is not. The two
    // failures must stay visually and behaviourally distinct.
    //
    // #2198 note: this interceptor now fires ONCE instead of twice, because the
    // store joins the two concurrent callers. The assertion still holds — the
    // rejection propagates to every joiner, so `loadAgent`'s catch runs exactly
    // as before — but the change in observed traffic is deliberate, not a
    // symptom.
    await page.route('**/api/agents/*', (route) => {
      const url = route.request().url()
      // Only the single-agent GET — leave /api/agents, /sync-health, etc. alone.
      if (/\/api\/agents\/[^/?]+(\?|$)/.test(url)) {
        return route.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"boom"}' })
      }
      return route.continue()
    })

    await page.goto('/agents/anything')

    await expect(page.getByTestId('agent-load-error')).toBeVisible({ timeout: 10000 })
    await expect(page.getByTestId('agent-not-found')).toHaveCount(0)
    await expect(page.getByTestId('agent-load-retry')).toBeVisible()
  })
})
