import { test, expect } from '@playwright/test'

/**
 * The Dashboard's Systems rail opens by default (vybe's call), at 224px. On a
 * 375px phone that left the fleet a ~150px column with agent names cut off.
 * Below `sm` the rail now STARTS collapsed; a saved preference still wins, and
 * from `sm` up nothing changes. Measured in a real browser.
 */

const KEY = 'trinity-sidebar-collapsed'

async function railAndPane(page) {
  const rail = page.locator('main > div').first()
  const pane = page.locator('main > div').nth(1)
  await expect(rail).toBeVisible({ timeout: 15000 })
  return {
    rail: (await rail.boundingBox()).width,
    pane: (await pane.boundingBox()).width,
  }
}

async function freshDashboard(page, viewport, saved = null) {
  await page.setViewportSize(viewport)
  await page.addInitScript(([key, value]) => {
    if (value === null) localStorage.removeItem(key)
    else localStorage.setItem(key, value)
  }, [KEY, saved])
  await page.goto('/')
}

test.describe('Systems rail at phone width', () => {
  for (const width of [320, 375, 430]) {
    test(`@smoke starts collapsed at ${width}px, leaving the fleet the screen`, async ({ page }) => {
      await freshDashboard(page, { width, height: 800 })
      const { rail, pane } = await railAndPane(page)
      expect(rail).toBeLessThanOrEqual(48)
      // Measured against <main>, not the viewport: desktop Chromium draws a
      // classic scrollbar on the app shell that phones do not, and those
      // pixels are not the rail's to give back.
      const inner = (await page.locator('main').boundingBox()).width
      expect(pane).toBeGreaterThanOrEqual(inner - 48 - 1)
      // The page itself never scrolls sideways (contract, principle 7).
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width)
    })
  }

  test('one tap opens it, and the choice is saved as before', async ({ page }) => {
    await freshDashboard(page, { width: 390, height: 844 })
    await page.getByTitle('Expand sidebar').click()
    await expect(page.getByText('Systems', { exact: true })).toBeVisible()
    expect(await page.evaluate((k) => localStorage.getItem(k), KEY)).toBe('false')
  })

  test('a saved "open" preference still wins on a phone', async ({ page }) => {
    await freshDashboard(page, { width: 390, height: 844 }, 'false')
    expect((await railAndPane(page)).rail).toBeGreaterThan(200)
  })

  test('from 640px up the rail still opens by default', async ({ page }) => {
    for (const width of [640, 1280]) {
      await freshDashboard(page, { width, height: 900 })
      expect((await railAndPane(page)).rail, `rail at ${width}px`).toBeGreaterThan(200)
    }
  })
})
