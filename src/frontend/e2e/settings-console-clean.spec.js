import { test, expect } from '@playwright/test'

/**
 * Settings loads clean, and the MCP Keys list is bounded (#2202).
 *
 * Two papercuts found together on a 31-page sweep, where Settings was the ONLY
 * page emitting a page-level console error — and it did so on every tab:
 *
 *   1. `GET /api/settings/public_chat_url` → 404 on any instance that never
 *      configured one. The store already treated 404 as "unset", so nothing was
 *      broken; what was lost was the signal, since a real failure on that call
 *      looked exactly like the ordinary case.
 *   2. Every key ever minted rendered at once — measured 306 keys, 294 revoked
 *      (96%), ~71KB of DOM text, no filter and no bound. Agent keys accumulate
 *      structurally (one per agent, one per #1854 rotation, one per ghost), so
 *      the page grows for the life of the instance.
 */
const TABS = ['general', 'access', 'integrations', 'mcp-keys', 'agents', 'retention']

test('@interactive every Settings tab loads with no console error', async ({ page }) => {
  // WebSocket transport noise is excluded, and the exclusion is narrow on
  // purpose. The subject here is Settings' OWN errors — the sweep's finding was
  // that Settings was the only page logging one — whereas the socket is a
  // global concern present on every page and absent from any harness that
  // serves the bundle without a WS proxy. What could actually regress from
  // Settings, the ticket mint, is asserted below as an HTTP call instead, so
  // dropping the handshake message costs no coverage this spec ever had.
  const isSocketNoise = (t) => /WebSocket/i.test(t)
  const errors = []
  page.on('console', (m) => {
    if (m.type() === 'error' && !isSocketNoise(m.text())) errors.push(m.text())
  })
  page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))
  const failed = []
  page.on('requestfailed', (r) => failed.push(`${r.method()} ${r.url()}`))
  // A 404 is not a `requestfailed`, so watch responses too — the old bug was a
  // *successful* request with a 404 status.
  const notFound = []
  const badTicket = []
  page.on('response', (r) => {
    if (r.status() === 404) notFound.push(`${r.status()} ${r.url()}`)
    if (r.url().includes('/api/ws/ticket') && !r.ok()) badTicket.push(`${r.status()} ${r.url()}`)
  })

  for (const tab of TABS) {
    await page.goto(`/settings?tab=${tab}`)
    await page.waitForLoadState('networkidle')
    await page.waitForTimeout(600)
  }

  expect(notFound, 'a Settings tab requested something that does not exist').toEqual([])
  expect(badTicket, 'the WebSocket ticket mint failed').toEqual([])
  expect(errors, 'Settings logged a console error').toEqual([])
  expect(failed, 'a Settings request failed outright').toEqual([])
})

test('@interactive MCP Keys shows active keys only, bounded, with the revoked count visible', async ({ page }) => {
  await page.goto('/settings?tab=mcp-keys')
  await page.waitForLoadState('networkidle')
  await page.waitForTimeout(1000)

  const keys = await page.evaluate(async () => {
    const res = await fetch('/api/mcp/keys', {
      headers: { Authorization: `Bearer ${localStorage.getItem('token')}` },
    })
    const rows = await res.json()
    return {
      total: rows.length,
      active: rows.filter((k) => k.is_active).length,
      revoked: rows.filter((k) => !k.is_active).length,
    }
  })
  test.skip(keys.total === 0, 'this instance has no MCP keys to bound')

  const rendered = await page.locator('ul > li').count()
  // The bound is the point: the DOM must not scale with instance age.
  expect(rendered, `rendered ${rendered} rows for ${keys.total} keys`).toBeLessThanOrEqual(25)

  if (keys.revoked > 0) {
    // Revoked keys are hidden by default and their count is stated, so the
    // operator can tell "no keys" from "none active".
    await expect(page.getByText(/Show revoked \(/)).toBeVisible()
    // Scoped to the LIST: the toggle's own label contains the word, so an
    // unscoped text match asserts against the control instead of the rows.
    await expect(page.locator('ul > li').getByText('Revoked', { exact: true })).toHaveCount(0)
  }
  if (keys.active > 0) {
    await expect(page.getByText(`${keys.active.toLocaleString()} active`)).toBeVisible()
  }
})

test('@interactive revealing revoked keys is one explicit click, and search narrows', async ({ page }) => {
  await page.goto('/settings?tab=mcp-keys')
  await page.waitForLoadState('networkidle')
  await page.waitForTimeout(1000)

  const toggle = page.getByText(/Show revoked \(/)
  test.skip(!(await toggle.isVisible().catch(() => false)), 'no revoked keys on this instance')

  const before = await page.locator('ul > li').count()
  await toggle.click()
  await page.waitForTimeout(300)
  await expect(page.locator('ul > li').getByText('Revoked', { exact: true }).first()).toBeVisible()
  // Still bounded after revealing — that is what stops 294 rows landing at once.
  expect(await page.locator('ul > li').count()).toBeLessThanOrEqual(25)

  await page.getByPlaceholder('Search by name, prefix or agent').fill('zzz-no-such-key')
  await page.waitForTimeout(300)
  await expect(page.getByText(/No keys match/)).toBeVisible()
  // The empty state offers the way back rather than dead-ending.
  await page.getByRole('button', { name: 'Clear search' }).click()
  await page.waitForTimeout(300)
  expect(await page.locator('ul > li').count()).toBeGreaterThanOrEqual(Math.min(before, 1))
})
