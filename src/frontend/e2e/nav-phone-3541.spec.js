import { test, expect } from '@playwright/test'

/**
 * #3541 — below 640px the nav bar's link row is not displayed, and before this
 * fix nothing replaced it: on a phone the only ways between pages were the
 * logo and typing a URL. The menu button opens the same links in a bottom
 * sheet. Measured in a real browser at phone widths, both themes.
 */

const PHONES = [
  { width: 320, height: 640 },
  { width: 390, height: 844 },
  { width: 639, height: 900 },
]

async function gotoAt(page, viewport, path, theme = 'light') {
  await page.setViewportSize(viewport)
  await page.addInitScript((t) => localStorage.setItem('trinity-theme', t), theme)
  await page.goto(path)
  await expect(page.locator('[data-nav-sheet-trigger]')).toBeVisible({ timeout: 15000 })
}

const box = (loc) => loc.evaluate((el) => {
  const r = el.getBoundingClientRect()
  return { w: r.width, h: r.height, right: r.right }
})

test.describe('phone navigation (#3541)', () => {
  for (const vp of PHONES) {
    test(`@smoke every page link is reachable at ${vp.width}px`, async ({ page }) => {
      await gotoAt(page, vp, '/library')
      const trigger = page.locator('[data-nav-sheet-trigger]')

      // A thumb-sized target, inside the viewport, and the bar stays one row.
      const t = await box(trigger)
      expect(Math.min(t.w, t.h)).toBeGreaterThanOrEqual(44)
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(vp.width)
      expect(await page.locator('nav .flex.justify-between').evaluate((el) => el.offsetHeight)).toBe(64)

      await trigger.click()
      const sheet = page.getByRole('dialog', { name: 'Navigation' })
      await expect(sheet).toBeVisible()
      for (const name of ['Dashboard', 'Library', 'Operations', 'Settings', 'Workspace']) {
        const link = sheet.getByRole('link', { name: new RegExp(`^${name}`) })
        await expect(link).toBeVisible()
        expect((await box(link)).h).toBeGreaterThanOrEqual(44)
      }
      // The page you are on is marked, and only that one.
      await expect(sheet.locator('[aria-current="page"]')).toHaveCount(1)
      await expect(sheet.locator('[aria-current="page"]')).toContainText('Library')

      // A tap navigates and the sheet goes away.
      await sheet.getByRole('link', { name: /^Settings/ }).click()
      await expect(page).toHaveURL(/\/settings/)
      await expect(sheet).toBeHidden()
    })
  }

  test('Escape closes the sheet and focus returns to the menu button', async ({ page }) => {
    await gotoAt(page, PHONES[1], '/')
    const trigger = page.locator('[data-nav-sheet-trigger]')
    await trigger.focus()
    await page.keyboard.press('Enter')
    const sheet = page.getByRole('dialog', { name: 'Navigation' })
    await expect(sheet).toBeVisible()
    await expect(trigger).toHaveAttribute('aria-expanded', 'true')
    await page.keyboard.press('Escape')
    await expect(sheet).toBeHidden()
    await expect(trigger).toBeFocused()
  })

  for (const theme of ['light', 'dark']) {
    test(`every link meets AA contrast on the ${theme} sheet`, async ({ page }) => {
      await gotoAt(page, PHONES[1], '/library', theme)
      await page.locator('[data-nav-sheet-trigger]').click()
      const sheet = page.getByRole('dialog', { name: 'Navigation' })
      await expect(sheet).toBeVisible()
      const ratios = await sheet.locator('[data-nav-sheet-item]').evaluateAll((els) => {
        const rgb = (c) => (c.match(/[\d.]+/g) || []).slice(0, 4).map(Number)
        const lum = ([r, g, b]) => {
          const ch = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4 }
          return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)
        }
        // The nearest opaque ground: the active row's tint sits over the panel.
        const ground = (el) => {
          let [r, g, b, a = 1] = [255, 255, 255, 0]
          const stack = []
          for (let n = el; n; n = n.parentElement) {
            const c = rgb(getComputedStyle(n).backgroundColor)
            if (c.length >= 3 && (c[3] ?? 1) > 0) { stack.push(c); if ((c[3] ?? 1) === 1) break }
          }
          for (const c of stack.reverse()) {
            const al = c[3] ?? 1
            r = c[0] * al + r * (1 - al); g = c[1] * al + g * (1 - al); b = c[2] * al + b * (1 - al); a = 1
          }
          return [r, g, b]
        }
        return els.map((el) => {
          const label = el.querySelector('span') || el
          const L1 = lum(rgb(getComputedStyle(label).color))
          const L2 = lum(ground(el))
          const [hi, lo] = L1 > L2 ? [L1, L2] : [L2, L1]
          return { text: el.textContent.trim(), ratio: (hi + 0.05) / (lo + 0.05) }
        })
      })
      expect(ratios.length).toBeGreaterThan(3)
      for (const { text, ratio } of ratios) expect(ratio, `${theme}: ${text}`).toBeGreaterThanOrEqual(4.5)
    })
  }

  test('from 640px up the link row is back and the menu button is gone', async ({ page }) => {
    await page.setViewportSize({ width: 640, height: 900 })
    await page.goto('/')
    await expect(page.getByRole('link', { name: 'Dashboard', exact: true })).toBeVisible({ timeout: 15000 })
    await expect(page.locator('[data-nav-sheet-trigger]')).toBeHidden()
  })
})
