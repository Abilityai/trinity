import { test, expect } from '@playwright/test'
import { VIEW_MODES } from '../src/utils/viewModes.js'

/**
 * Dashboard stats-bar overflow (#1830).
 *
 * The Dashboard header row is [elastic stats cluster | fixed controls cluster].
 * It used to be inelastic: the stats cluster was only ever CLIPPED by
 * `overflow-hidden`, so below ~1170 CSS px of effective width the host
 * telemetry meters were cut mid-element and their boxes ran under the Tags /
 * owner / Grid-Timeline / time-range controls (29 px measured overlap at 900 px
 * on an entitled build). Same defect class as the NavBar row in #1789.
 *
 * The fix makes the stats cluster a size container (`container-type:
 * inline-size` on `.stats-cluster`) and degrades its contents in a defined
 * order as the leftover width shrinks: sparklines → Disk → Mem → telemetry →
 * "messages" → "working now", with the agent count always surviving. Keying on
 * the CONTAINER (not the viewport) is what makes it hold when the controls
 * cluster grows — grid mode adds Tidy up / Reset, and the Tags chip and owner
 * filter only appear on some fleets.
 *
 * This suite pins the two properties that must never regress:
 *   1. no box of the controls cluster intersects any box of the stats cluster
 *   2. nothing in the stats cluster is clipped mid-element by the cluster edge
 * plus no page-level horizontal overflow, across both dashboard modes.
 */

const WIDTHS = [640, 768, 900, 1024, 1170, 1366, 1600]
// ent#260 added the List mode — its toolbar lives inside the pane, but the
// third toggle button + the chassis Create Agent button widen the CONTROLS
// cluster in every mode, so the sweep runs list too. The list is imported from
// the one home (#2536) — same order as before, so the sweep is unchanged; a
// hand-copied literal here is what `viewModeStructure.spec.js` guards against.
const MODES = VIEW_MODES
const ROW = 'main .border-b .flex.items-center.justify-between'

// Geometry probe. Compares every leaf box of the left (stats) cluster against
// every leaf box of the right (controls) cluster, and separately reports any
// stats leaf that straddles the cluster's own right edge (the clip that the
// old markup produced instead of degrading).
const PROBE = () => {
  const row = document.querySelector(
    'main .border-b .flex.items-center.justify-between'
  )
  if (!row) return { error: 'header row not found' }
  const [left, right] = row.children
  const leaves = (root) =>
    [...root.querySelectorAll('*')].filter(
      (e) => e.children.length === 0 && e.getBoundingClientRect().width > 0
    )
  const describe = (e) =>
    `${e.tagName.toLowerCase()}.${String(e.className).slice(0, 40)} "${(
      e.textContent || ''
    )
      .trim()
      .slice(0, 16)}"`

  let worst = { px: 0 }
  for (const l of leaves(left)) {
    const lr = l.getBoundingClientRect()
    for (const r of leaves(right)) {
      const rr = r.getBoundingClientRect()
      const ox = Math.min(lr.right, rr.right) - Math.max(lr.left, rr.left)
      const oy = Math.min(lr.bottom, rr.bottom) - Math.max(lr.top, rr.top)
      if (ox > 0 && oy > 0 && ox > worst.px) {
        worst = { px: Math.round(ox), left: describe(l), right: describe(r) }
      }
    }
  }

  const lbox = left.getBoundingClientRect()
  const clipped = leaves(left)
    .filter((e) => {
      const r = e.getBoundingClientRect()
      // Starts inside the cluster but runs past its right edge (0.5px slack for
      // sub-pixel layout).
      return r.left < lbox.right && r.right > lbox.right + 0.5
    })
    .map(describe)

  return {
    overlapPx: worst.px,
    overlapPair: worst.px ? `${worst.left} ⨯ ${worst.right}` : null,
    clipped,
    scrollWidth: document.documentElement.scrollWidth,
    innerWidth: window.innerWidth,
  }
}

async function gotoDashboard(page, mode) {
  // The store reads the persisted mode on init (stores/network.js), so seeding
  // it is more deterministic than clicking the toggle and racing the re-render.
  await page.addInitScript((m) => {
    localStorage.setItem('trinity-dashboard-view', m)
  }, mode)
  await page.goto('/')
  await page.waitForSelector(ROW, { timeout: 15000 })
  // Host telemetry paints on its first 5s poll; give the meters a beat to land
  // so the sweep measures the widest state, not the pre-fetch one.
  await page.waitForTimeout(1500)
}

test.describe('Dashboard stats bar overflow (#1830)', () => {
  for (const mode of MODES) {
    test(`@interactive ${mode} mode: controls never overlap the stats cluster`, async ({
      page,
    }) => {
      await page.setViewportSize({ width: WIDTHS.at(-1), height: 800 })
      await gotoDashboard(page, mode)

      for (const width of WIDTHS) {
        await page.setViewportSize({ width, height: 800 })
        await page.waitForTimeout(250) // container query re-layout
        const r = await page.evaluate(PROBE)

        expect(r.error, `probe failed at ${width}px`).toBeUndefined()
        expect(
          r.overlapPx,
          `${mode} @ ${width}px — controls overlap stats by ${r.overlapPx}px (${r.overlapPair})`
        ).toBe(0)
        expect(
          r.clipped,
          `${mode} @ ${width}px — stats content clipped mid-element instead of degrading`
        ).toEqual([])
        expect(
          r.scrollWidth,
          `${mode} @ ${width}px — page overflows horizontally`
        ).toBeLessThanOrEqual(r.innerWidth)
      }
    })
  }

  // The ladder must degrade, not amputate: at a comfortable width every meter is
  // still there. Guards an over-eager threshold that would hide telemetry on a
  // normal laptop.
  test('@interactive wide viewport keeps CPU, Mem and Disk meters', async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 800 })
    await gotoDashboard(page, 'timeline')

    for (const metric of ['cpu', 'mem', 'disk']) {
      await expect(page.locator(`[data-metric="${metric}"]`)).toBeVisible()
    }
  })

  // ...and at a narrow width the meters are REMOVED (display:none), not merely
  // scrolled out of a clipping box — which is what the old markup did.
  test('@interactive narrow viewport hides the meters rather than clipping them', async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1600, height: 800 })
    await gotoDashboard(page, 'grid')
    await expect(page.locator('[data-metric="disk"]')).toBeVisible()

    await page.setViewportSize({ width: 700, height: 800 })
    await page.waitForTimeout(250)
    await expect(page.locator('[data-metric="disk"]')).toBeHidden()
    // The agent count is the one thing that always survives.
    await expect(page.locator('.stats-cluster')).toContainText('agents')
  })
})
