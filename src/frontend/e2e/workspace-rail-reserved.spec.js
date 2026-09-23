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

async function coldLoadAndTrack(page, width, path = '/workspace') {
  await page.setViewportSize({ width, height: 900 })
  await page.goto(path)
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

  test('@interactive a direct load of /workspace/c/<session> does not shift the composer', async ({ page }) => {
    // The route the AC names. Every arm above loads bare `/workspace`, which on
    // an instance with threads REDIRECTS into a conversation — so those arms
    // measure the right thing only by accident of the fixture. This one loads
    // the conversation URL directly, as a bookmark or a reload does.
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.goto('/workspace')
    const sessionId = await page.evaluate(async () => {
      const res = await fetch('/api/enterprise/client-portal/sessions', {
        headers: { Authorization: `Bearer ${localStorage.getItem('token')}` },
      })
      if (!res.ok) return null
      const body = await res.json()
      const rows = body.sessions || body.threads || (Array.isArray(body) ? body : [])
      return rows[0]?.session_id || rows[0]?.id || null
    })
    test.skip(!sessionId, 'no existing conversation to deep-link to')

    const xs = await coldLoadAndTrack(page, 1440, `/workspace/c/${sessionId}`)
    expect(xs.length, 'never measured the composer').toBeGreaterThan(5)
    const spread = Math.max(...xs) - Math.min(...xs)
    expect(spread, `Send moved ${spread}px on a direct /workspace/c/<id> load — ${xs.join(',')}`)
      .toBeLessThanOrEqual(1)
  })

  test('@interactive an unfulfilled reservation is given back in one frame, not animated', async ({ page }) => {
    // The review finding this arm exists for: reserving on a route whose roster
    // turns out EMPTY (a caller nobody has shared an agent with) or FAILED used
    // to slide the column away over 300ms — 48 → 22 → 3 → gone — which is the
    // same shift the reservation removes, handed back.
    //
    // The assertion is about the SHAPE of the removal, not its speed: every
    // width ever observed must be either the full reserved width or nothing. An
    // intermediate width is an animation frame, and an animation frame is the
    // bug.
    await page.setViewportSize({ width: 1440, height: 900 })

    for (const stub of [
      { label: 'empty roster', status: 200, body: '{"agents": []}' },
      { label: 'failed roster', status: 500, body: '{"detail":"boom"}' },
    ]) {
      await page.route('**/my-agents*', (route) =>
        route.fulfill({ status: stub.status, contentType: 'application/json', body: stub.body }))
      await page.goto('/workspace')

      const widths = new Set()
      for (let i = 0; i < 22; i++) {
        widths.add(await page.evaluate(() => {
          const c = document.querySelector('[data-testid="ws-rail-column"]')
          return c ? Math.round(c.getBoundingClientRect().width) : 0
        }))
        await page.waitForTimeout(100)
      }
      await page.unroute('**/my-agents*')

      const observed = [...widths].sort((a, b) => a - b)
      const full = Math.max(...observed)
      const intermediate = observed.filter((w) => w > 0 && w < full)
      expect(
        intermediate,
        `${stub.label}: the column animated away through ${intermediate.join(',')}px`
      ).toEqual([])
    }
  })

  // An "agent page carries no rail column" case was written here and removed:
  // `/workspace/a/:name` REDIRECTS into a conversation (`/workspace/c/<id>`),
  // which legitimately has a rail, so the test asserted a premise the router
  // does not hold. The rule itself — `railColumnReservedFor` returning false for
  // an agent page, so the reservation cannot invent a column where the rail
  // never renders — is covered in `tests/unit/portalRailReserve.spec.js`, which
  // can exercise it without depending on which URL the router settles on.
})
