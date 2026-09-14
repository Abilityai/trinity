import { test, expect } from '@playwright/test'

/**
 * The rail column is reserved from first paint (#2711).
 *
 * On a cold load the conversation column rendered full width and then lost the
 * rail's width the moment the roster arrived, sliding the composer, the thread
 * and the header left — animated since #2676, so a 300ms slide rather than a
 * one-frame jump, but the contract's layout-stability rule says loading and
 * loaded share one footprint and nothing shifts on arrival.
 *
 * Measured the way the AC states it: the composer's Send button keeps the same
 * x from first paint through roster arrival.
 */
const WIDTHS = [1440, 1024, 768, 640]

async function coldLoadAndTrack(page, width) {
  await page.setViewportSize({ width, height: 900 })
  await page.goto('/workspace')
  // Sample from the first frame that has a composer through the roster landing.
  const send = page.getByTestId('portal-send').or(page.getByRole('button', { name: /send/i })).first()
  await send.waitFor({ timeout: 20000 })

  const xs = []
  for (let i = 0; i < 24; i++) {
    const box = await send.boundingBox().catch(() => null)
    if (box) xs.push(Math.round(box.x))
    await page.waitForTimeout(100)
  }
  return xs
}

test.describe('workspace rail column reservation (#2711)', () => {
  for (const width of WIDTHS) {
    test(`@interactive the composer does not shift on roster arrival at ${width}px`, async ({ page }) => {
      const xs = await coldLoadAndTrack(page, width)
      expect(xs.length, 'never measured the composer').toBeGreaterThan(5)
      const spread = Math.max(...xs) - Math.min(...xs)
      expect(
        spread,
        `Send moved ${spread}px during load at ${width}px — samples: ${xs.join(',')}`
      ).toBeLessThanOrEqual(1)
    })
  }

  test('@interactive the column is present while loading and keeps its width after', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.goto('/workspace')
    const column = page.getByTestId('ws-rail-column')
    await column.waitFor({ timeout: 20000 })
    // Reserved first: present, and explicitly marked as holding space only.
    const reservedWidth = (await column.boundingBox())?.width
    expect(reservedWidth, 'the reserved column has no width').toBeGreaterThan(0)

    // Then the rail lands in the same column, at the same width.
    await page.waitForFunction(
      () => document.querySelector('[data-testid="ws-rail-column"]')?.dataset.reserved === undefined,
      null,
      { timeout: 20000 }
    )
    const settledWidth = (await column.boundingBox())?.width
    expect(Math.abs(settledWidth - reservedWidth), 'the column changed width when the rail arrived')
      .toBeLessThanOrEqual(1)
  })

  // An "agent page carries no rail column" case was written here and removed:
  // `/workspace/a/:name` REDIRECTS into a conversation (`/workspace/c/<id>`),
  // which legitimately has a rail, so the test asserted a premise the router
  // does not hold. The rule itself — `railColumnReservedFor` returning false for
  // an agent page, so the reservation cannot invent a column where the rail
  // never renders — is covered in `tests/unit/portalRailReserve.spec.js`, which
  // can exercise it without depending on which URL the router settles on.
})
