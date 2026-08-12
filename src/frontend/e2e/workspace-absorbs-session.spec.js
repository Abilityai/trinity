import { test, expect } from '@playwright/test'

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
 * That also keeps this spec fast and Claude-free, so it can carry @smoke.
 *
 * Required env: ADMIN_PASSWORD (enforced by auth.setup.js) and
 * SESSION_TEST_AGENT (defaults to "testfix"). The agent must already exist.
 */

const TEST_AGENT = process.env.SESSION_TEST_AGENT || 'testfix'

test.describe('@smoke session surface retired into the Workspace', () => {
  test('?tab=session redirects to the Workspace for that agent', async ({ page }) => {
    await page.goto(`/agents/${TEST_AGENT}?tab=session`)

    await expect(page).toHaveURL(new RegExp(`/workspace\\?.*agent=${TEST_AGENT}`))
  })

  test('the redirect replaces history rather than stacking it', async ({ page }) => {
    await page.goto('/')
    await page.goto(`/agents/${TEST_AGENT}?tab=session`)
    await expect(page).toHaveURL(/\/workspace/)

    // `replace`, so Back lands on the dashboard — NOT on the retired URL,
    // which would immediately bounce forward again and trap the user.
    await page.goBack()
    await expect(page).not.toHaveURL(/tab=session/)
  })

  test('the Chat tab has no session-mode toggle', async ({ page }) => {
    await page.goto(`/agents/${TEST_AGENT}?tab=chat`)

    await expect(page.getByText('Session mode')).toHaveCount(0)
    await expect(page.getByRole('link', { name: /Continue in Workspace/ })).toBeVisible()
  })
})
