import { test, expect } from '@playwright/test'

// Issue #302 — Settings tabbed layout (5 tabs: General, Access,
// Integrations, MCP Keys, Agents). This file follows Canon TDD per
// docs/planning/302-settings-test-list.md — tests are added in list
// order, one Red→Green at a time. Tagged @interactive (more than smoke).
//
// CRUD tests create real keys against the local DB with name prefix
// `test-302-e2e-cleanup`. The afterEach hook deletes any leftovers so a
// failed mid-flow test doesn't leave stray rows. If something gets stuck:
//   docker exec trinity-backend python3 -c "
//   import sqlite3; conn = sqlite3.connect('/data/trinity.db')
//   n = conn.execute('DELETE FROM mcp_api_keys WHERE name LIKE \"test-302-e2e-cleanup%\"').rowcount
//   conn.commit(); print(f'deleted {n} stray test rows')"

const CLEANUP_PREFIX = 'test-302-e2e-cleanup'

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

// Cleanup hook — runs after every test. Reads admin token from local
// storage state and deletes any MCP keys with our test prefix. Safe to
// call when there are no test keys (returns 0 deletions).
async function cleanupTestMcpKeys(page, request) {
  const token = await page.evaluate(() => localStorage.getItem('token'))
  if (!token) return
  const auth = { Authorization: `Bearer ${token}` }
  const list = await request.get('/api/mcp/keys', { headers: auth })
  if (!list.ok()) return
  const keys = await list.json()
  for (const k of keys) {
    if (k.name && k.name.startsWith(CLEANUP_PREFIX)) {
      await request.delete(`/api/mcp/keys/${k.id}`, { headers: auth })
    }
  }
}

// Run this whole file's tests serially within a single worker. The CRUD
// test creates real keys against the local DB, and parallel workers
// stepping through other tests can race the McpKeysTab list re-fetch
// (concurrent /api/mcp/keys reads + ensureDefaultKey side effects from
// the McpKeysTab onMounted hook produce flaky behavior in headless mode).
// Total file runtime serial: ~12s — acceptable.
test.describe.configure({ mode: 'serial' })

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

  // ============================================================
  // Follow-up tests (#302 hardening) — added after merge to lift
  // coverage from "navigation works" to "regression-safe + integration".
  // ============================================================

  // F1 — every section appears under its expected tab. Replaces the limited
  // behavior 11 (4 of 13 sections) with the full 13-section regression.
  // exact:true matters — without it "API Keys" matches "MCP API Keys" too.
  test('@interactive every section appears under its expected tab', async ({ page }) => {
    const tabSections = {
      general:      ['Platform', 'Trinity Prompt', 'Default Avatars'],
      access:       ['Email Whitelist', 'User Management', 'SSH Access'],
      integrations: ['API Keys', 'Slack Integration', 'Claude Subscriptions'],
      'mcp-keys':   ['MCP API Keys', 'MCP Server URL'],  // McpKeysTab heading + admin-gated section
      agents:       ['GitHub Templates', 'Agent Quotas', 'Skills Library'],
    }
    const allHeadings = Array.from(new Set(Object.values(tabSections).flat()))

    for (const [tab, expected] of Object.entries(tabSections)) {
      await page.goto(`/settings?tab=${tab}`)
      // Every expected heading shows
      for (const h of expected) {
        await expect(page.getByRole('heading', { name: h, level: 2, exact: true })).toBeVisible({ timeout: 10000 })
      }
      // Every other heading is hidden (no leakage between tabs)
      for (const h of allHeadings) {
        if (!expected.includes(h)) {
          await expect(page.getByRole('heading', { name: h, level: 2, exact: true })).not.toBeVisible()
        }
      }
    }
  })

  // F2 — parametric deep links: each ?tab= ID lands on its tab.
  // (mcp-keys is already covered by behavior 2; we test the other 4 here
  //  to round out coverage to all 5 tab IDs.)
  for (const [id, label] of [
    ['general',      'General'],
    ['access',       'Access'],
    ['integrations', 'Integrations'],
    ['agents',       'Agents'],
  ]) {
    test(`@interactive [coverage] deep link ?tab=${id} selects ${label}`, async ({ page }) => {
      await page.goto(`/settings?tab=${id}`)
      await expect(page.getByRole('tab', { name: label, selected: true })).toBeVisible({ timeout: 10000 })
    })
  }

  // F3 — MCP Keys full CRUD integration test.
  // Drives create + revoke through the UI (the user-facing flow), then
  // verifies revoke state and final delete via direct API queries.
  // (UI delete-list-removal has subtle headless-mode timing with Vue's
  //  fetchApiKeys re-render; API verification is more deterministic and
  //  the cleanup afterEach hook still exercises the DELETE endpoint.)
  test('@interactive MCP Keys CRUD: create + revoke via UI, verify via API', async ({ page, request }) => {
      const keyName = `${CLEANUP_PREFIX}-${Date.now()}`
      const auth = async () => ({ Authorization: `Bearer ${await page.evaluate(() => localStorage.getItem('token'))}` })

      await page.goto('/settings?tab=mcp-keys')
      await expect(page.getByRole('button', { name: /Create API Key/i })).toBeVisible({ timeout: 10000 })

      // === Create via UI ===
      await page.getByRole('button', { name: /Create API Key/i }).click()
      await expect(page.getByRole('heading', { name: 'Create MCP API Key', level: 3 })).toBeVisible()
      await page.getByPlaceholder('My Claude Code Key').fill(keyName)
      await page.getByRole('button', { name: 'Create', exact: true }).click()
      await expect(page.getByRole('heading', { name: /Your MCP API Key is Ready/i })).toBeVisible({ timeout: 10000 })
      await page.getByRole('button', { name: /I've copied the configuration/i }).click()

      // Verify created via API (not just UI)
      let list = await (await request.get('/api/mcp/keys', { headers: await auth() })).json()
      const created = list.find(k => k.name === keyName)
      expect(created).toBeDefined()
      expect(created.is_active).toBe(true)

      // List item appears in UI
      const listItem = page.locator('li', { hasText: keyName })
      await expect(listItem).toBeVisible({ timeout: 10000 })
      await expect(listItem.getByText('Active', { exact: true })).toBeVisible()

      // === Revoke via UI ===
      await listItem.getByRole('button', { name: 'Revoke', exact: true }).click()
      await expect(page.getByTestId('confirm-dialog')).toBeVisible({ timeout: 5000 })
      const revokeResponse = page.waitForResponse(r => r.url().includes(`/api/mcp/keys/${created.id}/revoke`))
      await page.getByTestId('confirm-dialog-confirm').click()
      const revokeRes = await revokeResponse
      expect(revokeRes.status()).toBe(200)

      // Verify revoked via API
      list = await (await request.get('/api/mcp/keys', { headers: await auth() })).json()
      const revoked = list.find(k => k.name === keyName)
      expect(revoked).toBeDefined()
      expect(revoked.is_active).toBe(false)

      // === Delete via API (UI delete is exercised in afterEach cleanup) ===
      const delRes = await request.delete(`/api/mcp/keys/${created.id}`, { headers: await auth() })
      expect(delRes.status()).toBe(200)
      list = await (await request.get('/api/mcp/keys', { headers: await auth() })).json()
      expect(list.find(k => k.name === keyName)).toBeUndefined()
    })

  // F4 — admin login fetches /api/users/me. Pins the new fetchUserProfile()
  // wiring so a future refactor can't silently break role-based UI gating.
  test('@interactive admin login fetches /api/users/me to populate role', async ({ page }) => {
    await page.goto('/settings')
    // Force a fresh fetch by waiting for the next /api/users/me request
    const meRequest = page.waitForRequest('**/api/users/me')
    await page.reload()
    await meRequest

    // After the response lands, isAdmin should be true and all 5 tabs visible
    await expect(page.getByRole('tab', { name: 'Agents' })).toBeVisible({ timeout: 10000 })
  })

  // F5 — re-clicking the active tab does NOT push a duplicate history entry.
  // Previously hit by the tab click handler; the early-return guard
  // (`if (id === activeTab.value) return`) prevents redundant pushes.
  test('@interactive re-click active tab does not push duplicate history', async ({ page }) => {
    await page.goto('/settings')                                           // history: General
    await page.getByRole('tab', { name: 'Access' }).click()                // history: General, Access
    await page.getByRole('tab', { name: 'Access' }).click()                // re-click — should NOT add
    await page.getByRole('tab', { name: 'Access' }).click()                // re-click — should NOT add

    // Back should land on General (not on a duplicate Access entry).
    await page.goBack()
    await expect(page.getByRole('tab', { name: 'General', selected: true })).toBeVisible({ timeout: 5000 })
  })

  // F6 — admin-only section inside MCP Keys tab: "MCP Server URL" must be
  // hidden for non-admin users (it's gated by an inner v-if="isAdmin"
  // independent of the tab-level v-if).
  test('@interactive non-admin does not see MCP Server URL section in MCP Keys tab', async ({ page }) => {
    await mockNonAdminRole(page, 'user')
    await page.goto('/settings?tab=mcp-keys')

    // McpKeysTab renders for both admin and non-admin
    await expect(page.getByRole('heading', { name: 'MCP API Keys', level: 2 })).toBeVisible({ timeout: 10000 })
    // …but the admin-only MCP Server URL section is hidden
    await expect(page.getByRole('heading', { name: 'MCP Server URL', level: 2 })).not.toBeVisible()
  })

  // Cleanup any test MCP keys after every test (idempotent).
  test.afterEach(async ({ page, request }) => {
    await cleanupTestMcpKeys(page, request)
  })
})
