import { test, expect } from '@playwright/test'

/**
 * Dashboard List view e2e (trinity-enterprise#260).
 *
 * The List is the third dashboard mode (Timeline / Grid / List) — the retired
 * standalone Agents page consolidated into the dashboard chassis. These specs
 * cover the mode toggle + row rendering, mode persistence (AC-4), the
 * /agents → /?view=list redirect (one-shot, NON-persisting, param stripped —
 * AC-2), the local name filter + filtered-empty recovery, and a three-mode
 * round-trip.
 *
 * Runs against a live stack — every install has at least `trinity-system`, so
 * no fixtures are needed. Rows carry `data-agent="<slug>"` (the grid's tile
 * pattern) so locators never text-match display names. Deliberately NO
 * toggle-visibility assertions: CI's only agent is the system agent, whose Run
 * toggle is guarded (grid parity) and whose Autonomy/ReadOnly toggles are
 * `invisible` by design.
 *
 * The auth storageState pre-seeds `trinity_onboarding_dismissed_v1`, so the
 * onboarding wizard never auto-opens under these tests; the explicit
 * `?onboarding=1` param bypasses that key (covered below).
 */

const MODE_KEY = 'trinity-dashboard-view'

async function gotoList(page) {
  await page.goto('/')
  await page.getByRole('button', { name: 'list', exact: true }).click()
  await expect(page.getByPlaceholder('Search agents...')).toBeVisible({ timeout: 15000 })
}

test.describe('dashboard list view (trinity-enterprise#260)', () => {
  test('@smoke mode toggle shows List and renders the system agent row', async ({ page }) => {
    await gotoList(page)

    const sysRow = page.locator('[data-agent="trinity-system"]')
    await expect(sysRow).toBeVisible({ timeout: 15000 })
    await expect(sysRow).toContainText('SYSTEM')
    // Name links to the agent detail page.
    await expect(sysRow.locator('a[href="/agents/trinity-system"]').first()).toBeVisible()

    // List-mode toolbar furniture (name search, status filter, sort).
    await expect(page.getByRole('button', { name: 'Running', exact: true })).toBeVisible()
    await expect(page.locator('select').filter({ hasText: 'Newest First' })).toBeVisible()
  })

  test('mode choice persists across reload (AC-4)', async ({ page }) => {
    await gotoList(page)
    expect(await page.evaluate((k) => localStorage.getItem(k), MODE_KEY)).toBe('list')

    await page.reload()
    // Lands straight back in list mode without touching the toggle.
    await expect(page.getByPlaceholder('Search agents...')).toBeVisible({ timeout: 15000 })
  })

  test('@smoke /agents redirects into List mode, strips the param, does NOT persist (AC-2)', async ({ page }) => {
    await page.goto('/agents')

    // Lands on the bare Dashboard URL — `view` applied then stripped.
    await expect(page).toHaveURL(/^https?:\/\/[^/]+\/$/, { timeout: 10000 })
    await expect(page.locator('[data-agent="trinity-system"]')).toBeVisible({ timeout: 15000 })
    await expect(page.getByPlaceholder('Search agents...')).toBeVisible()

    // The redirect is a one-shot intent, not a preference statement: the saved
    // view selection must be untouched (fresh context → still unset).
    expect(await page.evaluate((k) => localStorage.getItem(k), MODE_KEY)).toBeNull()
  })

  test('/agents?onboarding=1 keeps the onboarding param through the redirect', async ({ page }) => {
    await page.goto('/agents?onboarding=1')

    // `view` is stripped; `onboarding` survives the query-preserving redirect
    // AND the Dashboard's param strip (it spreads the rest of the query).
    await expect(page).toHaveURL(/\?onboarding=1$/, { timeout: 10000 })
    // Explicit ?onboarding=1 bypasses the pre-seeded dismissal key.
    await expect(page.locator('#onboarding-title')).toBeVisible({ timeout: 15000 })
    // Behind the wizard, the List pane mounted.
    await expect(page.getByPlaceholder('Search agents...')).toBeVisible()
  })

  test('name filter narrows rows and filtered-empty recovers via Clear all', async ({ page }) => {
    await gotoList(page)
    await expect(page.locator('[data-agent="trinity-system"]')).toBeVisible({ timeout: 15000 })

    // A query no agent matches → filtered-empty state (panel-local), with the
    // recovery affordance.
    await page.getByPlaceholder('Search agents...').fill('zzz-no-such-agent')
    await expect(page.locator('[data-agent]')).toHaveCount(0)
    await expect(page.getByText('No matching agents')).toBeVisible()

    await page.getByRole('button', { name: 'Clear all filters' }).click()
    await expect(page.locator('[data-agent="trinity-system"]')).toBeVisible()
  })

  test('three-mode round-trip mounts and unmounts panes cleanly', async ({ page }) => {
    await gotoList(page)
    await expect(page.locator('[data-agent="trinity-system"]')).toBeVisible({ timeout: 15000 })

    // List → Grid: the list toolbar tears down, the fleet canvas mounts.
    await page.getByRole('button', { name: 'grid', exact: true }).click()
    await expect(page.getByPlaceholder('Search agents...')).toHaveCount(0)
    await expect(page.locator('.fleet-canvas')).toBeVisible({ timeout: 15000 })

    // Grid → Timeline: the canvas tears down.
    await page.getByRole('button', { name: 'timeline', exact: true }).click()
    await expect(page.locator('.fleet-canvas')).toHaveCount(0)

    // Timeline → List: the list pane remounts.
    await page.getByRole('button', { name: 'list', exact: true }).click()
    await expect(page.getByPlaceholder('Search agents...')).toBeVisible({ timeout: 15000 })
    await expect(page.locator('[data-agent="trinity-system"]')).toBeVisible({ timeout: 15000 })
  })
})
