import { test, expect } from '@playwright/test'
import { ALL_LAYOUT_KEYS, WIDGET_PREFS_KEY } from '../src/utils/gridStorageKeys.js'
import { widgetKey } from '../src/utils/gridWidgets.js'

/**
 * Grid INFO-TILE loading motion e2e (trinity-enterprise#449).
 *
 * The agent tiles' scanline zones are covered in `dashboard-grid-view.spec.js`
 * (ent#245). This file is the info-tile half: the Executions tile owns its own
 * loading face inside the chassis slot, which is a different mechanism —
 * `InfoTile`'s `owns-loading` handoff — and therefore a different failure mode.
 *
 * DATA-CONTROLLED on purpose. A settle-only assertion on a live stack cannot
 * tell "the beam played and finished" from "the primitive never mounted": a
 * quiet install has zero executions in 24h, so the tile lands on the chassis
 * `empty` message, the slot is never rendered, and every count below is
 * vacuously green. So the timeline response is HELD on a deferred while the
 * loading face is asserted, then fulfilled with a fixture, and the reveal is
 * asserted after.
 *
 * `gotoGrid` is duplicated from `dashboard-grid-view.spec.js` rather than
 * imported: it is not exported there, and exporting it would mean editing a
 * file another lane is appending to. The storage-key reset IS shared, through
 * `gridStorageKeys.js` — the #2199 rule (a hand-copied key literal desyncs
 * silently and every read-back resolves `null`).
 */

const EXEC_TILE = `.gv-tile-widget[data-agent="${widgetKey('executions')}"]`

/** 24 hourly buckets, several with real runs — enough to draw a stack + rail. */
function timelineFixture() {
  const buckets = []
  for (let h = 0; h < 24; h += 1) {
    const hour = String(h).padStart(2, '0')
    const runs = h % 4 === 0 ? 3 + (h % 5) : 0
    const failed = h % 8 === 0 ? 1 : 0
    buckets.push({
      bucket: `2026-09-03T${hour}`,
      total: runs,
      success: Math.max(0, runs - failed),
      failed,
      context_used: 0,
      cost: 0,
      by_trigger: runs
        ? { Scheduled: { total: runs, failed }, 'Chat/Tasks': { total: 1, failed: 0 } }
        : {},
    })
  }
  return { buckets, trigger_order: ['Chat/Tasks', 'MCP', 'Scheduled', 'Other'] }
}

async function gotoGrid(page) {
  // Copied from dashboard-grid-view.spec.js (not exported there — see header).
  await page.goto('/')
  await page.getByRole('button', { name: 'grid', exact: true }).click()
  await expect(page.locator('.fleet-canvas')).toBeVisible()
}

test.describe('grid info-tile loading motion (trinity-enterprise#449)', () => {
  test.beforeEach(async ({ page }) => {
    // Every layout generation, not just the current one: `_loadSavedRaw`
    // migrates a v1 blob into v2 when v2 is absent, so clearing only the
    // current key lets a stale layout be migrated straight back in (#2199).
    await page.addInitScript(
      (keys) => {
        keys.forEach((k) => localStorage.removeItem(k))
      },
      [...ALL_LAYOUT_KEYS, WIDGET_PREFS_KEY]
    )
  })

  test('Executions info tile loads with the scanline, never the chassis skeleton (ent#449)', async ({
    page,
  }) => {
    // Hold the timeline read open so the loading face is observable for as long
    // as the assertions need, instead of racing a fast local response.
    let release = null
    const held = new Promise((resolve) => {
      release = resolve
    })
    await page.route('**/api/executions/timeline*', async (route) => {
      await held
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(timelineFixture()),
      })
    })

    await gotoGrid(page)
    const tile = page.locator(EXEC_TILE)
    await expect(tile).toBeVisible({ timeout: 15000 })

    // --- while the read is held: the tile's OWN loading face ----------------
    // The chassis skeleton must be gone entirely — two loading faces on one
    // tile is the state the `owns-loading` handoff exists to prevent.
    await expect(tile.locator('.scan-track')).toHaveCount(1, { timeout: 15000 })
    await expect(tile.locator('.it-skel')).toHaveCount(0)
    // The headline renders DURING loading (it sits outside the zone) and must
    // admit it has no numbers yet rather than claiming "0 runs".
    await expect(tile.locator('.ex-total')).toHaveText('—')
    // And never "— failed": the failure segment is gated on a non-zero count,
    // so a loading face that leaked a dash into it would assert that failures
    // exist before anything has been read. The unit suite pins the value; this
    // is the only place the rendered STRING can be checked (vitest runs
    // node-environment here, with no DOM to mount into).
    await expect(tile.locator('.ex-fail')).toHaveCount(0)
    // Nothing is drawn under the track.
    await expect(tile.locator('.ex-chart')).toHaveCount(0)

    // --- release: one reveal, then a settled, unclipped chart ---------------
    release()

    await expect(tile.locator('.scan-track')).toHaveCount(0, { timeout: 20000 })
    // The primitive is still there — the same instance, patched, not a fresh
    // mount that skipped the reveal (a remount would also satisfy the count
    // above, which is why the wrapper is asserted rather than only the track).
    await expect(tile.locator('.scan-content')).toHaveCount(1)
    await expect(tile.locator('.ex-chart')).toBeVisible()
    await expect(tile.locator('.ex-total')).not.toHaveText('—')

    // A retained non-none clip-path is the signature of the stuck-`revealing`
    // bug class (lost animationend, scoped-keyframe rename): assert it never
    // survives the pass.
    await expect
      .poll(
        () =>
          page.evaluate(
            (sel) =>
              [...document.querySelectorAll(`${sel} .scan-content`)].filter(
                (el) => getComputedStyle(el).clipPath !== 'none'
              ).length,
            EXEC_TILE
          ),
        { timeout: 10000 }
      )
      .toBe(0)
  })

  test('reduced motion: the Executions tile chart appears instantly, never mid-wipe (ent#449)', async ({
    page,
  }) => {
    await page.emulateMedia({ reducedMotion: 'reduce' })
    // Data-controlled for the same reason as the test above, and it matters
    // MORE here: every assertion below is a count of zero or an absence, all
    // of which a tile that landed on the chassis `empty` message satisfies
    // without the primitive ever mounting. No deferral, though — under reduced
    // motion there is no wipe to catch mid-flight, only an end state.
    await page.route('**/api/executions/timeline*', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(timelineFixture()),
      })
    )

    await gotoGrid(page)

    const tile = page.locator(EXEC_TILE)
    await expect(tile).toBeVisible({ timeout: 15000 })
    await expect(tile.locator('.it-skel')).toHaveCount(0)
    await expect(tile.locator('.scan-track')).toHaveCount(0, { timeout: 20000 })

    // The zeros above are only evidence if the primitive actually mounted and
    // the chart actually arrived through it.
    await expect(tile.locator('.scan-content')).toHaveCount(1)
    await expect(tile.locator('.ex-chart')).toBeVisible()
    await expect(tile.locator('.ex-total')).not.toHaveText('—')

    // The reveal phase is skipped entirely under reduced motion, by the
    // primitive's matchMedia check AND its CSS belt — so no wrapper anywhere in
    // the tile may ever hold a clip-path.
    expect(
      await page.evaluate(
        (sel) =>
          [...document.querySelectorAll(`${sel} .scan-content`)].filter(
            (el) => getComputedStyle(el).clipPath !== 'none'
          ).length,
        EXEC_TILE
      )
    ).toBe(0)
  })
})
