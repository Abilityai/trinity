import { test, expect } from '@playwright/test'
import { ALL_LAYOUT_KEYS, LAYOUT_KEY, WIDGET_PREFS_KEY } from '../src/utils/gridStorageKeys.js'
import { isWidgetKey } from '../src/utils/gridWidgets.js'

/**
 * Dashboard Grid view e2e (trinity-enterprise#47).
 *
 * The Grid is a dashboard mode alongside Timeline and List (List consolidated
 * the retired Agents page in trinity-enterprise#260; the legacy Graph / Vue
 * Flow canvas was decommissioned in #1689): a magnetic tile canvas on an
 * unbounded pan/zoom lattice. These specs cover the mode toggle + persistence,
 * tile rendering, drag-to-cell with swap, tidy/reset, and that the Timeline
 * mode is untouched. Toggle clicks use exact-name selectors, so the third
 * mode button doesn't affect them; List-mode behaviour lives in
 * dashboard-list-view.spec.js.
 *
 * Runs against a live stack — every install has at least `trinity-system`,
 * so no fixtures are needed. Layout state is reset per test via
 * localStorage.
 */

// LAYOUT_KEY / ALL_LAYOUT_KEYS / WIDGET_PREFS_KEY are imported from the store's
// own module (#2199) — a hand-copied literal here silently desynced on the
// #2042 v1->v2 bump and both read-back assertions below resolved `null`.
const MODE_KEY = 'trinity-dashboard-view'

async function gotoGrid(page) {
  await page.goto('/')
  await page.getByRole('button', { name: 'grid', exact: true }).click()
  await expect(page.locator('.fleet-canvas')).toBeVisible()
}

test.describe('dashboard grid view (trinity-enterprise#47)', () => {
  test.beforeEach(async ({ page }) => {
    // Fresh layout + default mode for deterministic assertions.
    // ALL generations, not just the current one: `_loadSavedRaw` migrates a v1
    // blob into v2 when v2 is absent, so clearing only LAYOUT_KEY lets a stale
    // layout be migrated straight back in and the board is not clean (#2199).
    // The argument is SPREAD into one flat array and the callback iterates it —
    // nesting it would call removeItem() with an Array and silently clear
    // nothing, which looks identical to a working cleanup.
    await page.addInitScript(
      (keys) => {
        keys.forEach((k) => localStorage.removeItem(k))
      },
      [...ALL_LAYOUT_KEYS, WIDGET_PREFS_KEY]
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

  test('scanline loading settles: chart content visible, no lingering clip-path (ent#245)', async ({ page }) => {
    await gotoGrid(page)
    const sysTile = page.locator('.gv-tile[data-agent="trinity-system"]')
    await expect(sysTile).toBeVisible({ timeout: 15000 })

    // The primitive actually renders (guards against the trivially-green
    // case where the wrapper is absent and every count below is vacuous).
    await expect(sysTile.locator('.scan-content')).toHaveCount(2)

    // Once analytics land, both chart zones must be fully revealed: the
    // loading track gone and NO wrapper stuck mid-wipe. A retained non-none
    // clip-path is the signature of the entire stuck-`revealing` bug class
    // (lost animationend, scoped-keyframe rename) — assert it never survives.
    await expect(sysTile.locator('.scan-track')).toHaveCount(0, { timeout: 20000 })
    await expect
      .poll(
        () =>
          page.evaluate(() =>
            [...document.querySelectorAll('.scan-content')].filter(
              (el) => getComputedStyle(el).clipPath !== 'none'
            ).length
          ),
        { timeout: 10000 }
      )
      .toBe(0)
    // Headline flipped off the em-dash placeholder state (value or 0, not a
    // stuck loading dash on the Activity zone whose data always exists).
    await expect(sysTile.locator('.t-charts')).toBeVisible()
  })

  test('reduced motion: charts render instantly with no beam (ent#245)', async ({ page }) => {
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await gotoGrid(page)
    const sysTile = page.locator('.gv-tile[data-agent="trinity-system"]')
    await expect(sysTile).toBeVisible({ timeout: 15000 })
    await expect(sysTile.locator('.scan-content')).toHaveCount(2)
    await expect(sysTile.locator('.scan-track')).toHaveCount(0, { timeout: 20000 })
    // The reveal phase is skipped entirely under reduced motion — nothing
    // may ever hold a clip-path.
    expect(
      await page.evaluate(() =>
        [...document.querySelectorAll('.scan-content')].filter(
          (el) => getComputedStyle(el).clipPath !== 'none'
        ).length
      )
    ).toBe(0)
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
    // Reset yields the deterministic default. Layout v2 holds TWO occupant
    // kinds (#2042 / ent#325), so the shape differs by kind and asserting
    // `r >= 0` across the whole map is a stale v1-era assumption — v1 held
    // agent keys only. Agents land in non-negative reading-order cells;
    // info tiles are seeded into the band ABOVE the fleet (negative rows,
    // `seedWidgetCells` band [-3, -1] — "spill upward, never into the
    // agents"). `isWidgetKey` is imported rather than matching a hand-copied
    // 'widget:' literal, for the same reason the layout key is (#2199).
    const entries = Object.entries(JSON.parse(reset))
    expect(entries.length).toBeGreaterThan(0)
    for (const [key, p] of entries) {
      expect(Number.isInteger(p.c)).toBe(true)
      expect(Number.isInteger(p.r)).toBe(true)
      expect(p.c).toBeGreaterThanOrEqual(0)
      if (isWidgetKey(key)) {
        expect(p.r).toBeLessThan(0)
      } else {
        expect(p.r).toBeGreaterThanOrEqual(0)
      }
    }
  })

  test('@smoke timeline mode still works alongside grid', async ({ page }) => {
    await gotoGrid(page)

    // Timeline: replay timeline mounts, grid tears down.
    // (the Graph / Vue Flow mode was decommissioned in #1689)
    await page.getByRole('button', { name: 'timeline', exact: true }).click()
    await expect(page.locator('.fleet-canvas')).toHaveCount(0)

    // Back to grid: the fleet canvas remounts.
    await page.getByRole('button', { name: 'grid', exact: true }).click()
    await expect(page.locator('.fleet-canvas')).toBeVisible({ timeout: 15000 })
  })
})

// Imported here rather than beside the file's other imports so this whole
// block is append-only: ES module imports are hoisted, and keeping the edit at
// the end of the file makes a rebase against a concurrent change trivial.
import { FIXTURE_LABEL, pickLabelFixture, readLabel, writeLabel } from './helpers/agent-label.js'

/**
 * Tile identity — the slug the label hides (#2358).
 *
 * Same defect as the List row, and the reason it is fixed on both at once: the
 * fleet must not show one agent under two naming conventions depending on which
 * Dashboard mode is open (§1.3.1 FR-3 names dashboard cards and grid tiles
 * together). The slug rides `.t-repo`, the meta line the tile already always
 * renders, so a labelled tile gains no third line inside its fixed 384x216
 * cell.
 *
 * ⚠️ This test LABELS a live agent. ⚠️ It reads the prior label first and
 * restores it in `test.afterEach` (a `finally` in a body that times out is not
 * reliably run), and runs serially because `playwright.config` is
 * `fullyParallel: true`. `AgentHeader.vue` hides the label pencil for system
 * agents, so a stranded label on `trinity-system` has no UI undo — API only.
 * `serial` only orders tests within this file, and `dashboard-list-view.spec.js`
 * borrows the same agent: run the two together with `--workers=1`. The sentinel
 * `FIXTURE_LABEL` makes a collision loud rather than permanent — see
 * `helpers/agent-label.js`.
 *
 * The drag specs above grab the tile CENTRE, so `.nodrag` on the slug does not
 * interfere with them.
 */
test.describe('grid tile identity (#2358)', () => {
  test.describe.configure({ mode: 'serial' })

  let fixtureAgent = null
  let priorLabel = null
  let labelBorrowed = false

  test.afterEach(async ({ baseURL }) => {
    if (!labelBorrowed) return
    labelBorrowed = false
    await writeLabel(baseURL, fixtureAgent, priorLabel)
  })

  test('a labelled tile shows the slug beside the repo, and an unlabelled one does not', async ({
    page,
    baseURL,
  }) => {
    fixtureAgent = await pickLabelFixture(baseURL)
    priorLabel = await readLabel(baseURL, fixtureAgent)

    await gotoGrid(page)
    const tile = page.locator(`.gv-tile[data-agent="${fixtureAgent}"]`)
    await expect(tile).toBeVisible({ timeout: 15000 })

    // Unlabelled: the primary name IS the slug, so there is nothing to repeat.
    labelBorrowed = true
    await writeLabel(baseURL, fixtureAgent, null)
    await page.reload()
    await expect(tile).toBeVisible({ timeout: 15000 })
    await expect(tile.locator('[data-testid="agent-slug-tile"]')).toHaveCount(0)

    await writeLabel(baseURL, fixtureAgent, FIXTURE_LABEL)
    await page.reload()
    await expect(tile).toBeVisible({ timeout: 15000 })
    await expect(tile.locator('.t-name')).toHaveText(FIXTURE_LABEL)

    const slug = tile.locator('[data-testid="agent-slug-tile"]')
    await expect(slug).toBeVisible()
    await expect(slug).toHaveText(fixtureAgent)
    // Copyable against `.gtile { user-select: none }`, and `nodrag` so a click
    // selects the identity instead of starting a tile drag.
    expect(await slug.evaluate((el) => getComputedStyle(el).userSelect)).toBe('all')
    expect(await slug.evaluate((el) => Boolean(el.closest('.nodrag')))).toBe(true)
  })
})
