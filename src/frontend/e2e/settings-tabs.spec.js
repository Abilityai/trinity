import { test, expect } from '@playwright/test'

// Issue #302 — Settings tabbed layout (5 tabs: General, Access,
// Integrations, MCP Keys, Agents). This file follows Canon TDD per
// docs/planning/302-settings-test-list.md — tests are added in list
// order, one Red→Green at a time. Tagged @interactive (more than smoke).

// Mock /api/users/me to return a non-admin role. The auth fixture logs
// in as admin (storage state has admin token), but we override the role
// at the API level to test how the UI gates on `user.role`.
async function mockNonAdminRole(page, role = 'user') {
  await page.route('**/api/users/me', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        username: 'admin',
        email: 'test@example.com',
        name: 'Test User',
        picture: null,
        role,
      }),
    })
  })
}

test.describe('Settings tabbed layout (#302)', () => {
  // Behavior 1 — admin lands on General tab by default at /settings
  test('@interactive admin lands on General tab by default', async ({ page }) => {
    await page.goto('/settings')

    // Tab strip is rendered with the 5 tabs
    await expect(page.getByRole('tab', { name: 'General' })).toBeVisible({ timeout: 10000 })

    // General is the active/selected tab on first load (no ?tab= in URL)
    await expect(page.getByRole('tab', { name: 'General', selected: true })).toBeVisible()
  })

  // Behavior 2 — /settings?tab=mcp-keys deep-links into MCP Keys tab
  test('@interactive deep link ?tab=mcp-keys selects MCP Keys', async ({ page }) => {
    await page.goto('/settings?tab=mcp-keys')

    await expect(page.getByRole('tab', { name: 'MCP Keys', selected: true })).toBeVisible({ timeout: 10000 })
  })

  // Behavior 3 — unknown ?tab= falls back to default without crash
  test('@interactive unknown ?tab=foobar falls back to default', async ({ page }) => {
    await page.goto('/settings?tab=foobar')

    // Page renders, default tab (General) is selected, no error
    await expect(page.getByRole('tab', { name: 'General', selected: true })).toBeVisible({ timeout: 10000 })
  })

  // Behavior 4 — admin sees all 5 tabs in the strip
  test('@interactive admin sees all 5 tabs', async ({ page }) => {
    await page.goto('/settings')

    await expect(page.getByRole('tab', { name: 'General' })).toBeVisible({ timeout: 10000 })
    await expect(page.getByRole('tab', { name: 'Access' })).toBeVisible()
    await expect(page.getByRole('tab', { name: 'Integrations' })).toBeVisible()
    await expect(page.getByRole('tab', { name: 'MCP Keys' })).toBeVisible()
    await expect(page.getByRole('tab', { name: 'Agents' })).toBeVisible()
  })

  // Behavior 5 — non-admin sees ONLY the MCP Keys tab
  test('@interactive non-admin sees only MCP Keys tab', async ({ page }) => {
    await mockNonAdminRole(page, 'user')
    await page.goto('/settings')

    // MCP Keys visible
    await expect(page.getByRole('tab', { name: 'MCP Keys' })).toBeVisible({ timeout: 10000 })
    // Other tabs hidden
    await expect(page.getByRole('tab', { name: 'General' })).not.toBeVisible()
    await expect(page.getByRole('tab', { name: 'Access' })).not.toBeVisible()
    await expect(page.getByRole('tab', { name: 'Integrations' })).not.toBeVisible()
    await expect(page.getByRole('tab', { name: 'Agents' })).not.toBeVisible()
  })

  // Behavior 6 — non-admin loading /settings (no ?tab=) lands on MCP Keys
  test('@interactive non-admin defaults to MCP Keys tab', async ({ page }) => {
    await mockNonAdminRole(page, 'user')
    await page.goto('/settings')

    await expect(page.getByRole('tab', { name: 'MCP Keys', selected: true })).toBeVisible({ timeout: 10000 })
  })

  // Behavior 6.1 — non-admin must NOT be redirected away from /settings.
  // Regression: an earlier draft of #302 left the admin-only data fetches
  // running on mount; their 403 triggered router.push('/'), bouncing
  // non-admin users before they could reach the MCP Keys tab. This test
  // pins the gated-load behavior so that regression cannot return.
  test('@interactive non-admin stays on /settings (no admin-403 bounce)', async ({ page }) => {
    await mockNonAdminRole(page, 'user')
    // Block admin-only Settings endpoints with 403 so we'd reproduce the bug
    // if the page tried to load them. They should never be called.
    const adminEndpoints = [
      '**/api/settings/api-keys',
      '**/api/settings/slack',
      '**/api/settings/slack/status',
      '**/api/settings/email-whitelist*',
      '**/api/users',
      '**/api/settings/github-templates',
      '**/api/ops/**',
      '**/api/settings/agent-quotas',
      '**/api/settings/skills_library_url',
      '**/api/subscriptions',
    ]
    for (const ep of adminEndpoints) {
      await page.route(ep, route => route.fulfill({ status: 403, contentType: 'application/json', body: '{"detail":"Admin only"}' }))
    }

    await page.goto('/settings')

    await expect(page.getByRole('tab', { name: 'MCP Keys', selected: true })).toBeVisible({ timeout: 10000 })
    // We must still be on /settings (not bounced to /).
    await expect(page).toHaveURL(/\/settings(\?|$)/)
  })

  // Behavior 7 — /api-keys redirects to /settings?tab=mcp-keys (backward compat)
  test('@interactive /api-keys redirects to settings MCP Keys tab', async ({ page }) => {
    await page.goto('/api-keys')

    // After SPA navigation settles, we should be on /settings?tab=mcp-keys
    await expect(page).toHaveURL(/\/settings\?.*tab=mcp-keys/, { timeout: 10000 })
    await expect(page.getByRole('tab', { name: 'MCP Keys', selected: true })).toBeVisible()
  })

  // Behavior 8 — NavBar no longer shows the top-level "Keys" link
  test('@interactive NavBar has no top-level Keys link', async ({ page }) => {
    await page.goto('/')

    // Wait for NavBar to render — Settings link is a sibling that proves NavBar is up
    await expect(page.getByRole('link', { name: 'Settings', exact: true })).toBeVisible({ timeout: 10000 })

    // The "Keys" top-level link is gone (it lived in NavBar with name="Keys").
    await expect(page.getByRole('link', { name: 'Keys', exact: true })).toHaveCount(0)
  })

  // Behavior 9 — clicking a tab updates URL ?tab= without a full page reload
  test('@interactive clicking a tab updates URL without full reload', async ({ page }) => {
    await page.goto('/settings')

    // Mark window so we can detect a full reload (set on DOMContentLoaded above)
    await page.evaluate(() => { window.__noReload = true })

    await page.getByRole('tab', { name: 'Access' }).click()

    await expect(page).toHaveURL(/\/settings\?.*tab=access/, { timeout: 5000 })
    await expect(page.getByRole('tab', { name: 'Access', selected: true })).toBeVisible()

    // Marker survived → no full reload
    const stillSet = await page.evaluate(() => window.__noReload === true)
    expect(stillSet).toBe(true)
  })

  // Behavior 10 — browser back/forward navigates between tab states
  test('@interactive back/forward navigates tab history', async ({ page }) => {
    await page.goto('/settings')
    await expect(page.getByRole('tab', { name: 'General', selected: true })).toBeVisible({ timeout: 10000 })

    await page.getByRole('tab', { name: 'Access' }).click()
    await expect(page.getByRole('tab', { name: 'Access', selected: true })).toBeVisible()

    await page.getByRole('tab', { name: 'MCP Keys' }).click()
    await expect(page.getByRole('tab', { name: 'MCP Keys', selected: true })).toBeVisible()

    // Back: should land on Access
    await page.goBack()
    await expect(page.getByRole('tab', { name: 'Access', selected: true })).toBeVisible()

    // Forward: back to MCP Keys
    await page.goForward()
    await expect(page.getByRole('tab', { name: 'MCP Keys', selected: true })).toBeVisible()
  })

  // Behavior 11 — sections gate by tab (regression: pre-existing UI works in new tabs)
  test('@interactive sections gate by tab', async ({ page }) => {
    await page.goto('/settings?tab=general')
    // General tab shows the Platform section header
    await expect(page.getByRole('heading', { name: 'Platform', level: 2 })).toBeVisible({ timeout: 10000 })
    // …and hides Slack Integration (which lives in Integrations)
    await expect(page.getByRole('heading', { name: 'Slack Integration', level: 2 })).not.toBeVisible()

    await page.getByRole('tab', { name: 'Integrations' }).click()
    await expect(page.getByRole('heading', { name: 'Slack Integration', level: 2 })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Platform', level: 2 })).not.toBeVisible()

    await page.getByRole('tab', { name: 'Access' }).click()
    await expect(page.getByRole('heading', { name: 'Email Whitelist', level: 2 })).toBeVisible()

    await page.getByRole('tab', { name: 'Agents' }).click()
    await expect(page.getByRole('heading', { name: 'Agent Quotas', level: 2 })).toBeVisible()
  })

  // Behavior 12 — MCP Keys tab content: the API key list and "Generate Key"
  // affordance from the former /api-keys page are present.
  test('@interactive MCP Keys tab renders key management UI', async ({ page }) => {
    await page.goto('/settings?tab=mcp-keys')
    // The new tab content should expose key management — at minimum a
    // "Generate" / "Create" key button. The label was "Generate API Key"
    // on the old page; we assert by partial match for resilience.
    await expect(page.getByRole('button', { name: /Generate.*Key|Create.*Key/i })).toBeVisible({ timeout: 10000 })
  })
})
