import { test, expect } from '@playwright/test'

/**
 * #3571: BaseModal centred its panel with the overlay's `items-center`. A
 * centred flex child taller than its scroll container overflows at the top as
 * well as the bottom, and the top overflow cannot be scrolled to, so on a short
 * phone screen a tall dialog lost its title and first field. The issue's case is
 * Workspace > Projects > New project at 390x450; this uses the Dashboard's
 * New View editor, another BaseModal with no max height of its own, so it runs
 * on any stack, with or without the Workspace.
 */

async function openNewView(page, viewport) {
  await page.setViewportSize(viewport)
  await page.goto('/')
  const create = page.getByRole('button', { name: /new view/i }).first()
  await expect(create).toBeVisible({ timeout: 15000 })
  await create.click()
  const dialog = page.locator('[role="dialog"][aria-modal="true"]').last()
  await expect(dialog).toBeVisible()
  return dialog
}

test.describe('a dialog taller than the screen (#3571)', () => {
  for (const viewport of [{ width: 390, height: 450 }, { width: 320, height: 400 }]) {
    test(`@smoke scrolls from its top to its bottom at ${viewport.width}x${viewport.height}`, async ({ page }) => {
      const dialog = await openNewView(page, viewport)
      const panel = dialog.locator(':scope > div').first()
      const tall = await panel.evaluate((el) => el.getBoundingClientRect().height)
      // The premise: the panel is taller than the screen.
      expect(tall).toBeGreaterThan(viewport.height)
      await dialog.evaluate((el) => { el.scrollTop = 0 })
      // At the top of the scroll the panel's top edge is on screen, not above it.
      expect(await panel.evaluate((el) => el.getBoundingClientRect().top)).toBeGreaterThanOrEqual(0)
      await dialog.evaluate((el) => { el.scrollTop = el.scrollHeight })
      expect(await panel.evaluate((el) => el.getBoundingClientRect().bottom)).toBeLessThanOrEqual(viewport.height)
    })
  }

  test('a dialog that fits stays vertically centred', async ({ page }) => {
    const dialog = await openNewView(page, { width: 1280, height: 1400 })
    const panel = dialog.locator(':scope > div').first()
    const r = await panel.evaluate((el) => { const b = el.getBoundingClientRect(); return { top: b.top, bottom: b.bottom } })
    expect(Math.abs(r.top - (1400 - r.bottom))).toBeLessThanOrEqual(2)
  })
})
