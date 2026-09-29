import { test, expect } from '@playwright/test'

/**
 * The Workspace Inbox — the landing (trinity-enterprise#610).
 *
 * What must hold, measured on a live stack:
 *   1. signing in on bare `/workspace` lands on `/workspace/inbox`, once
 *   2. the sidebar carries the pinned Inbox row
 *   3. an explicit target still wins: `?agent=X` opens X's conversation, and
 *      `/workspace?new=1` stays the new-chat stage (D9 — bare `/workspace` keeps
 *      its meaning; only the bootstrap lands elsewhere)
 *   4. the Inbox list does not shift as the data arrives — sampled
 *      CONTAINER-relative (the #2711 method), never against an absolute pixel
 *      floor, which pins the host's scrollbar rather than the layout
 *
 * Arms 1 and 2 need only a signed-in session; arms 3 and 4 need an agent on
 * the roster and skip (never fail) without one (#2199).
 */

async function firstRosterAgent(page) {
  return page.evaluate(async () => {
    const res = await fetch('/api/enterprise/client-portal/my-agents', {
      headers: { Authorization: `Bearer ${localStorage.getItem('token')}` },
    })
    if (!res.ok) return null
    const body = await res.json()
    const rows = body.agents || (Array.isArray(body) ? body : [])
    return rows[0]?.name || null
  })
}

test.describe('Workspace Inbox', () => {
  test('@smoke bare /workspace lands on the Inbox, with the pinned row in the sidebar', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    await page.goto('/workspace')
    await page.waitForURL('**/workspace/inbox**', { timeout: 20000 })
    const row = page.getByTestId('sidebar-inbox')
    await expect(row).toBeVisible({ timeout: 20000 })
    await expect(row).toHaveAttribute('aria-current', 'page')
  })

  test('@smoke an explicit target is never redirected to the Inbox', async ({ page }) => {
    await page.goto('/workspace/inbox')
    const agent = await firstRosterAgent(page)
    test.skip(!agent, 'no agent on the roster to open')

    await page.goto(`/workspace?agent=${encodeURIComponent(agent)}`)
    await page.waitForLoadState('networkidle')
    expect(new URL(page.url()).pathname).not.toBe('/workspace/inbox')
    await expect(page.getByTestId('inbox')).toHaveCount(0)

    await page.goto('/workspace?new=1')
    await page.waitForLoadState('networkidle')
    expect(new URL(page.url()).pathname).not.toBe('/workspace/inbox')
  })

  test('@smoke the Inbox list does not shift as its data arrives', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    await page.goto('/workspace/inbox')
    // With no roster the Inbox never renders — the stage falls through to the
    // roster copy (D9) — so there is no list to measure. Skip, never fail (#2199).
    test.skip(!await firstRosterAgent(page), 'no agent on the roster, so no Inbox list to measure')
    const inbox = page.getByTestId('inbox')
    await inbox.waitFor({ timeout: 20000 })
    // The list's top edge RELATIVE to the Inbox root, from the first frame the
    // Inbox exists through the asks and thread reads settling.
    const tops = []
    for (let i = 0; i < 20; i++) {
      const t = await page.evaluate(() => {
        const root = document.querySelector('[data-testid="inbox"]')
        const list = root?.querySelector('[data-testid="inbox-list"], [data-testid="inbox-list-loading"], [data-testid="inbox-empty"], [data-testid="inbox-list-failed"]')
        if (!root || !list) return null
        return Math.round(list.getBoundingClientRect().top - root.getBoundingClientRect().top)
      })
      if (t !== null) tops.push(t)
      await page.waitForTimeout(100)
    }
    expect(tops.length, 'never measured the list').toBeGreaterThan(5)
    const spread = Math.max(...tops) - Math.min(...tops)
    expect(spread, `the list moved ${spread}px while loading — ${tops.join(',')}`).toBeLessThanOrEqual(1)
  })

  // §3g A4: at 768px (sidebar beside it) and at 640×400 (a 1280 window at 200%)
  // the Inbox's container is under 720px, so it STACKS: an opened row's pane
  // takes the whole Inbox instead of the ~150px (768) / 64px (zoomed) the
  // viewport rule left it. Relative geometry only — the pane against the Inbox
  // root, never an absolute pixel floor.
  for (const vp of [{ width: 768, height: 900 }, { width: 640, height: 400 }]) {
    test(`an opened row's pane takes the Inbox's width at ${vp.width}×${vp.height}`, async ({ page }) => {
      await page.setViewportSize(vp)
      await page.goto('/workspace/inbox?tab=all')
      test.skip(!await firstRosterAgent(page), 'no agent on the roster, so no Inbox')
      const inbox = page.getByTestId('inbox')
      await inbox.waitFor({ timeout: 20000 })
      const row = page.locator('[data-inbox-row]').first()
      test.skip(!await row.count(), 'nothing in All to open')
      await expect(inbox).toHaveAttribute('data-layout', 'stacked')
      await row.click()
      const pane = page.getByTestId('inbox-pane')
      await expect(pane).toBeVisible()
      await expect(page.getByTestId('inbox-list-column')).toBeHidden()
      const ratio = await page.evaluate(() => {
        const root = document.querySelector('[data-testid="inbox"]').getBoundingClientRect()
        const p = document.querySelector('[data-testid="inbox-pane"]').getBoundingClientRect()
        return p.width / root.width
      })
      expect(ratio, `the pane is ${Math.round(ratio * 100)}% of the Inbox`).toBeGreaterThan(0.95)
    })
  }

  // §3g A6: on a phone, opening a row PUSHES, so the browser's Back returns to
  // the list — in the Inbox, focus on the row — instead of leaving the Workspace.
  test('a phone Back after opening a row stays in the Inbox', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    await page.goto('/workspace/inbox?tab=all')
    test.skip(!await firstRosterAgent(page), 'no agent on the roster, so no Inbox')
    await page.getByTestId('inbox').waitFor({ timeout: 20000 })
    const row = page.locator('[data-inbox-row]').first()
    test.skip(!await row.count(), 'nothing in All to open')
    const key = await row.getAttribute('data-inbox-row')
    await row.click()
    await expect(page.getByTestId('inbox-pane')).toBeVisible()
    await page.goBack()
    await expect(page).toHaveURL(/\/workspace\/inbox/)
    await expect(page.getByTestId('inbox-pane')).toHaveCount(0)
    await expect(page.locator(`[data-inbox-row="${key}"]`)).toBeFocused()
    // F6: the touch targets that remain are at least 44px tall.
    const menu = await page.getByTestId('inbox-menu').boundingBox()
    expect(menu.height).toBeGreaterThanOrEqual(44)
  })
})
