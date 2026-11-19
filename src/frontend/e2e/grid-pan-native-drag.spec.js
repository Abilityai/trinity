import { test, expect } from '@playwright/test'
import { clearBrowserGridState, resetServerGridPrefs } from './helpers/grid-prefs.js'

/**
 * Grid canvas pan must never turn into a browser drag.
 *
 * With any canvas text selected (zone stat line, legend, a label after a
 * double-click), the next press-and-move on the background started the
 * browser's native drag of that selection: a no-drop cursor, a pointercancel
 * and a pan that stopped after a few pixels. Images and links inside a tile
 * could start a native drag the same way.
 *
 * Runs against a live stack — `trinity-system` always exists, so no fixtures.
 */

async function gotoGrid(page) {
  await page.goto('/')
  await resetServerGridPrefs(page)
  await page.getByRole('button', { name: 'grid', exact: true }).click()
  await expect(page.locator('.fleet-canvas')).toBeVisible()
  await expect(page.locator('.gv-tile').first()).toBeVisible({ timeout: 15000 })
}

const worldTransform = (page) =>
  page.locator('.gv-world').evaluate((el) => el.style.transform || getComputedStyle(el).transform)

// A press-and-move on empty canvas, well away from tiles, legend and controls.
async function dragBackground(page, dx, dy) {
  const box = await page.locator('.fleet-canvas').boundingBox()
  const x = box.x + box.width - 60
  const y = box.y + box.height - 90
  await page.mouse.move(x, y)
  await page.mouse.down()
  await page.mouse.move(x + dx, y + dy, { steps: 8 })
  await page.mouse.up()
}

test.describe('grid canvas pan vs native drag', () => {
  test.beforeEach(async ({ page }) => {
    await clearBrowserGridState(page)
  })

  test('@smoke panning still works after canvas text was selected', async ({ page }) => {
    await gotoGrid(page)
    await page.evaluate(() => {
      window.__nativeDrags = 0
      window.__pointerCancels = 0
      document.addEventListener('dragstart', () => window.__nativeDrags++, true)
      document.addEventListener('pointercancel', () => window.__pointerCancels++, true)
      const range = document.createRange()
      range.selectNodeContents(document.querySelector('.fleet-canvas'))
      const sel = window.getSelection()
      sel.removeAllRanges()
      sel.addRange(range)
    })
    expect(await page.evaluate(() => window.getSelection().toString().length)).toBeGreaterThan(0)

    const before = await worldTransform(page)
    await dragBackground(page, 120, 80)
    const after = await worldTransform(page)

    expect(after).not.toBe(before)
    // The whole 120x80 move was applied: the pan was not cut short.
    const t = (s) => s.match(/translate\(([-\d.]+)px, ([-\d.]+)px\)/).slice(1).map(Number)
    const [bx, by] = t(before)
    const [ax, ay] = t(after)
    expect(ax - bx).toBeCloseTo(120, 0)
    expect(ay - by).toBeCloseTo(80, 0)
    expect(await page.evaluate(() => window.__nativeDrags)).toBe(0)
    expect(await page.evaluate(() => window.__pointerCancels)).toBe(0)
    // The stale selection is gone rather than left to be dragged later.
    expect(await page.evaluate(() => window.getSelection().toString())).toBe('')
  })

  test('@smoke a pan across zone and tile text selects nothing', async ({ page }) => {
    await gotoGrid(page)
    const box = await page.locator('.fleet-canvas').boundingBox()
    const tile = await page.locator('.gv-tile').first().boundingBox()
    // Start on empty canvas, end over a tile: the sweep crosses text.
    await page.mouse.move(box.x + box.width - 60, box.y + box.height - 60)
    await page.mouse.down()
    await page.mouse.move(tile.x + tile.width / 2, tile.y + tile.height / 2, { steps: 12 })
    await page.mouse.up()
    expect(await page.evaluate(() => window.getSelection().toString())).toBe('')
  })

  test('@smoke the browser cannot start a native drag from a tile image or link', async ({
    page,
  }) => {
    await gotoGrid(page)
    // Synthetic dragstart on every kind of element that can start one inside
    // a tile: its image (avatar) and link, or the tile itself when it has none.
    const prevented = await page.evaluate(() => {
      const tile = document.querySelector('.gv-tile')
      const targets = [...tile.querySelectorAll('img, a')]
      if (!targets.length) targets.push(tile)
      return targets.map((el) => {
        const ev = new DragEvent('dragstart', { bubbles: true, cancelable: true })
        el.dispatchEvent(ev)
        return ev.defaultPrevented
      })
    })
    expect(prevented.length).toBeGreaterThan(0)
    expect(prevented.every(Boolean)).toBe(true)
  })
})
