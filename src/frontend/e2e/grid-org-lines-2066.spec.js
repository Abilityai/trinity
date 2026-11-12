import { test, expect } from '@playwright/test'
import { clearBrowserGridState, resetServerGridPrefs } from './helpers/grid-prefs.js'

/**
 * #2066: on the Grid org overlay, a reporting line leaves the manager tile at
 * BOTTOM-centre and enters the report tile at TOP-centre, whatever the tiles'
 * relative positions: side by side, report above its manager, two rows apart.
 *
 * A fresh stack holds only `trinity-system`, so the fleet is extended in the
 * browser: GET /api/agents gets synthetic agents appended (same row shape as
 * the real system agent) and the user's grid layout is set through the
 * preferences API. Nothing is created on the server and nothing needs cleanup
 * beyond the grid prefs, which beforeEach resets anyway.
 *
 * The check is on the RENDERED geometry: each line's first and last points,
 * mapped to the screen with the SVG's own CTM, against the tiles' rects.
 */

const LAYOUT = {
  'e2e-boss': { c: 1, r: 1 },
  'e2e-below': { c: 1, r: 2 }, // straight below
  'e2e-beside': { c: 2, r: 1 }, // same row: used to be a side-to-side arrow
  'e2e-above': { c: 1, r: 0 }, // above its manager: used to be drawn upward
  'e2e-far': { c: 3, r: 2 }, // one row down, far right: used to anchor on a side
  'trinity-system': { c: 5, r: 0 },
}
const REPORTS = ['e2e-below', 'e2e-beside', 'e2e-above', 'e2e-far']
const OUR_LINES = '.gv-wire[data-edge$="→e2e-boss"]'

async function extendFleet(page) {
  await page.route(
    (url) => url.pathname === '/api/agents',
    async (route) => {
      if (route.request().method() !== 'GET') return route.continue()
      const response = await route.fetch()
      const agents = await response.json()
      const sys = agents.find((a) => a.is_system) || agents[0]
      const fake = (name, tags) => ({
        ...sys,
        name,
        display_label: null,
        is_system: false,
        container_id: null,
        avatar_url: null,
        tags,
      })
      const extra = [
        fake('e2e-boss', ['dept-e2e-org']),
        ...REPORTS.map((n) => fake(n, ['dept-e2e-org', 'reports-to-e2e-boss'])),
      ]
      await route.fulfill({ response, json: [...agents, ...extra] })
    }
  )
}

async function gotoGridWithLayout(page) {
  await page.goto('/')
  await resetServerGridPrefs(page)
  const status = await page.evaluate(async (value) => {
    const res = await fetch('/api/users/me/preferences/grid_layout', {
      method: 'PUT',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${localStorage.getItem('token')}`,
      },
      body: JSON.stringify({ value, base_updated_at: null }),
    })
    return res.status
  }, LAYOUT)
  expect(status).toBe(200)
  await page.reload()
  await page.getByRole('button', { name: 'grid', exact: true }).click()
  await expect(page.locator('.fleet-canvas')).toBeVisible()
  // Only this spec's lines: a live stack may already have an org of its own.
  await expect(page.locator(OUR_LINES)).toHaveCount(REPORTS.length, { timeout: 15000 })
}

/** Screen-space first/last point of each rendered line, keyed by edge id. */
async function lineEnds(page) {
  return page.evaluate(() => {
    const out = {}
    for (const g of document.querySelectorAll('.gv-wire')) {
      const path = g.querySelector('.gv-wire-line')
      const m = path.getScreenCTM()
      const at = (len) => {
        const p = path.getPointAtLength(len)
        return { x: m.a * p.x + m.c * p.y + m.e, y: m.b * p.x + m.d * p.y + m.f }
      }
      out[g.dataset.edge] = { start: at(0), end: at(path.getTotalLength()) }
    }
    return out
  })
}

async function tileRect(page, name) {
  return page.locator(`.gv-tile[data-agent="${name}"]`).evaluate((el) => {
    const r = el.getBoundingClientRect()
    return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, cx: (r.left + r.right) / 2 }
  })
}

/**
 * Per line, how far (screen px) its first point is from the manager's
 * bottom-centre and its last point from the report's top-centre. Lines snap to
 * a new layout while tiles glide there, so callers poll this until it settles.
 */
async function anchorError(page) {
  const ends = await lineEnds(page)
  const boss = await tileRect(page, 'e2e-boss')
  const out = {}
  for (const report of REPORTS) {
    const e = ends[`${report}→e2e-boss`]
    if (!e) {
      out[report] = Infinity
      continue
    }
    const rep = await tileRect(page, report)
    out[report] = Math.max(
      Math.abs(e.start.x - boss.cx),
      Math.abs(e.start.y - boss.bottom),
      Math.abs(e.end.x - rep.cx),
      Math.abs(e.end.y - rep.top)
    )
  }
  return out
}

const SETTLED = Object.fromEntries(REPORTS.map((r) => [r, expect.any(Number)]))
// 2px slack: the canvas is zoomed to fit, so world px are not screen px.
async function expectAnchored(page) {
  await expect
    .poll(async () => Object.values(await anchorError(page)).every((d) => d <= 2), {
      message: 'every line starts at the manager bottom-centre and ends at the report top-centre',
      timeout: 10000,
    })
    .toBe(true)
}

test.describe('grid org lines are drawn top-down (#2066)', () => {
  test.beforeEach(async ({ page }) => {
    await clearBrowserGridState(page)
    await extendFleet(page)
  })

  test('@smoke every line leaves the manager bottom-centre and enters the report top-centre', async ({
    page,
  }) => {
    await gotoGridWithLayout(page)
    await expectAnchored(page)
    expect(await anchorError(page)).toEqual(SETTLED)
  })
})
