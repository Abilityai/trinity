import { test, expect } from '@playwright/test'
import { agentExists, missingAgentReason } from './helpers/agent-probe.js'

/**
 * Session surface retirement (ent#358).
 *
 * Replaces the old Session-tab e2e. The surface this spec used to drive — the
 * Chat tab's "Session mode" toggle rendering SessionPanel — no longer exists:
 * continuous conversation moved to the Workspace, which runs the same `--resume`
 * engine (see services/session_turn_service.py).
 *
 * What still has to hold, and is what this spec pins:
 *   - a `?tab=session` deep link REDIRECTS to the Workspace, carrying the agent
 *   - it does not leave the retired URL in the back stack
 *   - the Chat tab renders the stateless surface with no mode toggle
 *
 * Deliberately no live-chat round-trip here: routing is the contract this
 * change owns, and the conversation itself is covered by the Workspace specs.
 *
 * Tagging splits on ONE question — does the test need the agent to exist? The
 * redirect fires from a guard that runs before the agent is ever fetched, so it
 * holds for any name and carries @smoke. Asserting on the Chat tab needs the
 * agent page to actually render, which needs a real agent, so that one is
 * @interactive like the spec it replaces.
 *
 * Required env: ADMIN_PASSWORD (enforced by auth.setup.js) and
 * SESSION_TEST_AGENT (defaults to "testfix"). The agent must already exist.
 *
 * A MISSING fixture agent reads as SKIPPED, never broken (#2199). The guard is
 * INSIDE the @interactive test on purpose: a describe-level beforeEach would
 * also skip the two @smoke tests above, which are deliberately fixture-free
 * (the redirect fires before the agent is fetched) and are the tier CI runs.
 */

const TEST_AGENT = process.env.SESSION_TEST_AGENT || 'testfix'

test.describe('session surface retired into the Workspace', () => {
  test('@smoke ?tab=session redirects to the Workspace for that agent', async ({ page }) => {
    await page.goto(`/agents/${TEST_AGENT}?tab=session`)

    await expect(page).toHaveURL(new RegExp(`/workspace\\?.*agent=${TEST_AGENT}`))
  })

  test('@smoke the redirect replaces history rather than stacking it', async ({ page }) => {
    await page.goto('/')
    await page.goto(`/agents/${TEST_AGENT}?tab=session`)
    await expect(page).toHaveURL(/\/workspace/)

    // `replace`, so Back lands on the dashboard — NOT on the retired URL,
    // which would immediately bounce forward again and trap the user.
    await page.goBack()
    await expect(page).not.toHaveURL(/tab=session/)
  })

  // @interactive — needs SESSION_TEST_AGENT to exist; the smoke job has no
  // guaranteed agent, and a missing one renders "Agent not found" (no tabs).
  test('@interactive the Chat tab has no session-mode toggle', async ({ page, baseURL }) => {
    test.skip(
      !(await agentExists(TEST_AGENT, { baseURL })),
      missingAgentReason(TEST_AGENT, 'SESSION_TEST_AGENT')
    )

    await page.goto(`/agents/${TEST_AGENT}?tab=chat`)

    await expect(page.getByText('Session mode')).toHaveCount(0)
    await expect(page.getByRole('link', { name: /Continue in Workspace/ })).toBeVisible()
  })
})
