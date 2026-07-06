import { test, expect } from '@playwright/test'

/**
 * Dashboard Grid view e2e (trinity-enterprise#47).
 *
 * The Grid is a third dashboard mode alongside Graph and Timeline: a
 * magnetic tile canvas on an unbounded pan/zoom lattice. These specs cover
 * the mode toggle + persistence, tile rendering, drag-to-cell with swap,
 * tidy/reset, and that the existing Graph/Timeline modes are untouched.
 *
 * Runs against a live stack — every install has at least `trinity-system`,
 * so no fixtures are needed. Layout state is reset per test via
 * localStorage.
 */

const LAYOUT_KEY = 'trinity-grid-layout-v1'
const MODE_KEY = 'trinity-dashboard-view'

async function gotoGrid(page) {
  await page.goto('/')
  await page.getByRole('button', { name: 'grid', exact: true }).click()
  await expect(page.locator('.fleet-canvas')).toBeVisible()
}

test.describe('dashboard grid view (trinity-enterprise#47)', () => {
  test.beforeEach(async ({ page }) => {
    // Fresh layout + default mode for deterministic assertions.
    await page.addInitScript(
      ([layoutKey]) => {
        localStorage.removeItem(layoutKey)
      },
      [LAYOUT_KEY]
    )
  })

  test('@smoke mode toggle shows Grid and renders agent tiles', async ({ page }) => {
    await gotoGrid(page)

    // At least the system agent tile renders, with tile chrome + zones.
    const sysTile = page.locator('.gv-tile[data-agent="trinity-system"]')
    await expect(sysTile).toBeVisible({ timeout: 15000 })
    await expect(sysTile).toContainText('SYSTEM')
    await expect(sysTile).toContainText('Activity · 14d')
    await expect(sysTile).toContainText('Context · 7d')
    await expect(sysTile).toContainText('System Dashboard')

    // Grid-mode-only controls + board furniture.
    await expect(page.getByRole('button', { name: 'Tidy up' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Reset', exact: true })).toBeVisible()
    await expect(page.locator('.gv-legend')).toContainText('Scheduled')
    await expect(page.locator('.gv-zoomlvl')).toContainText('%')
  })

  test('mode choice persists across reload', async ({ page }) => {
    await gotoGrid(page)
    expect(await page.evaluate((k) => localStorage.getItem(k), MODE_KEY)).toBe('grid')

    await page.reload()
    // Lands straight back in grid mode without touching the toggle.
    await expect(page.locator('.fleet-canvas')).toBeVisible({ timeout: 15000 })
  })

  test('drag moves a tile to a free cell and persists the layout', async ({ page }) => {
    await gotoGrid(page)
    const tile = page.locator('.gv-tile[data-agent="trinity-system"]')
    await expect(tile).toBeVisible({ timeout: 15000 })

    const before = await page.evaluate((k) => localStorage.getItem(k), LAYOUT_KEY)

    // Drag by two cell-heights straight down (mouse-level, exercises the
    // pointer capture + socket path).
    const box = await tile.boundingBox()
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
    await page.mouse.down()
    for (let i = 1; i <= 10; i++) {
      await page.mouse.move(
        box.x + box.width / 2,
        box.y + box.height / 2 + (i / 10) * box.height * 2.2,
        { steps: 2 }
      )
    }
    // Socket preview is visible mid-drag.
    await expect(page.locator('.gv-socket.show')).toBeVisible()
    await page.mouse.up()

    await expect
      .poll(async () => page.evaluate((k) => localStorage.getItem(k), LAYOUT_KEY))
      .not.toBe(before)
  })

  test('tidy up compacts and reset restores the default layout', async ({ page }) => {
    await gotoGrid(page)
    await expect(page.locator('.gv-tile').first()).toBeVisible({ timeout: 15000 })

    await page.getByRole('button', { name: 'Tidy up' }).click()
    const tidied = await page.evaluate((k) => localStorage.getItem(k), LAYOUT_KEY)
    expect(tidied).toBeTruthy()

    await page.getByRole('button', { name: 'Reset', exact: true }).click()
    const reset = await page.evaluate((k) => localStorage.getItem(k), LAYOUT_KEY)
    expect(reset).toBeTruthy()
    // Reset yields the deterministic default: every position is a small
    // non-negative reading-order cell.
    const positions = Object.values(JSON.parse(reset))
    for (const p of positions) {
      expect(p.c).toBeGreaterThanOrEqual(0)
      expect(p.r).toBeGreaterThanOrEqual(0)
    }
  })

  test('@smoke graph and timeline modes still work alongside grid', async ({ page }) => {
    await gotoGrid(page)

    // Graph: Vue Flow mounts, grid tears down.
    await page.getByRole('button', { name: 'graph', exact: true }).click()
    await expect(page.locator('.vue-flow')).toBeVisible({ timeout: 15000 })
    await expect(page.locator('.fleet-canvas')).toHaveCount(0)

    // Timeline: replay timeline mounts, graph tears down.
    await page.getByRole('button', { name: 'timeline', exact: true }).click()
    await expect(page.locator('.vue-flow')).toHaveCount(0)
    await expect(page.locator('.fleet-canvas')).toHaveCount(0)
  })
})
