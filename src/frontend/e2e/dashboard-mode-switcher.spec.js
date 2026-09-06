import { test, expect } from '@playwright/test'
import { VIEW_MODES } from '../src/utils/viewModes.js'

/**
 * Dashboard view-mode switcher: pinned position + `v` cycle (#2536).
 *
 * THE DEFECT: the header's right-hand controls cluster is a right-anchored,
 * non-shrinking flex row (`justify-between` + `flex-shrink-0`) and the
 * Timeline / Grid / List switcher sat in the MIDDLE of it. Two conditional
 * siblings rendered to its right — the grid-only Tidy up / Reset pair and the
 * history-loading spinner — so entering Grid moved the control the operator
 * had just clicked (~124px) and every history fetch twitched it (~24px).
 *
 * THE MECHANISM: in an LTR row flex container a child's left edge is
 * `cluster.right − Σ(width + gap of every sibling to its RIGHT)`. A child's x
 * depends only on what is to its right. The switcher is now the cluster's
 * LAST element child, so that sum is zero and `x = cluster.right − width`,
 * both invariant across mode and fetch state. This is a reorder, not a
 * reservation: the cluster's width per (mode, loading) state is unchanged, so
 * the #1830 stats-overflow sweep sees the same inputs it always did.
 *
 * SPINNER DETERMINISM: the history request is HELD at the network boundary
 * (`page.route` on `/api/activities/timeline`, the `background-refresh-
 * invisible.spec.js` / `dashboard-hotkey-readiness.spec.js` idiom) — a delayed
 * response, never a `waitForTimeout` in the test's own control flow. The store
 * sets `isLoadingHistory` before the GET and clears it in `finally`, so a held
 * route shows the spinner from mount until release, and a Refresh click
 * re-shows it. The 60 s timeline poll and WS `resync_required` also call this
 * fetch, but neither fires within a test's window.
 *
 * PERSISTED MODE: a fresh context carries NO saved mode (`dashboard-list-
 * view.spec.js` pins `localStorage['trinity-dashboard-view'] === null`), so
 * every test starts in Timeline. Test B clears the key ONCE in-page; it never
 * uses `page.addInitScript` for it — init scripts re-run on every navigation
 * INCLUDING `page.reload()`, so a reload-based persistence check would see the
 * key wiped and go red on the fixed tree.
 *
 * All three tests are `@smoke`: CI runs `test:e2e:smoke` only, they are
 * fixture-free (only `trinity-system` is needed), fast and deterministic. The
 * required CI gate for the layout invariant is the vitest AST guard in
 * `tests/unit/viewModeStructure.spec.js`; this spec is the live-browser proof.
 */

const MODE_KEY = 'trinity-dashboard-view'
const HISTORY_ROUTE = '**/api/activities/timeline**'
const BOX_TOLERANCE_PX = 1 // the #2358 spec's tolerance; the unfixed deltas are ~124px and ~24px

const switcher = (page) => page.getByTestId('view-mode-switcher')
const spinner = (page) => page.getByTestId('history-loading')
const refreshButton = (page) => page.getByTestId('refresh-all')
const modeButton = (page, mode) => page.getByRole('button', { name: mode, exact: true })
const systemAgent = (page) => page.locator('[data-agent="trinity-system"]')

/** A promise the test resolves by hand — the route handler awaits it. */
function holdable() {
  let release
  const held = new Promise((r) => {
    release = r
  })
  return { held, release }
}

/**
 * Each pane has a marker only it renders: the grid's `.fleet-canvas`, the
 * List panel's name search. Timeline is asserted as "fleet painted AND
 * neither of the other two markers exists" — a negative assertion on its own
 * would pass on a blank page.
 */
async function expectMode(page, mode) {
  const canvas = page.locator('.fleet-canvas')
  const listSearch = page.getByPlaceholder('Search agents...')
  if (mode === 'grid') {
    await expect(canvas, 'grid pane mounted').toBeVisible({ timeout: 15000 })
  } else if (mode === 'list') {
    await expect(listSearch, 'list pane mounted').toBeVisible({ timeout: 15000 })
  } else {
    await expect(systemAgent(page), 'timeline row painted').toBeVisible({ timeout: 15000 })
    await expect(canvas, 'grid pane unmounted').toHaveCount(0)
    await expect(listSearch, 'list pane unmounted').toHaveCount(0)
  }
}

/** Direct measurement (no retry): by the time the pane marker is visible the header has re-laid out. */
async function expectSameBox(page, ref, when) {
  const box = await switcher(page).boundingBox()
  expect(box, `switcher has a box ${when}`).not.toBeNull()
  for (const k of ['x', 'y', 'width', 'height']) {
    expect(
      Math.abs(box[k] - ref[k]),
      `switcher ${k} moved ${when}: ${ref[k]} → ${box[k]} (a control to its RIGHT mounted or unmounted — it must be the cluster's LAST child, #2536)`
    ).toBeLessThanOrEqual(BOX_TOLERANCE_PX)
  }
}

test.describe('dashboard view-mode switcher (#2536)', () => {
  test('@smoke switcher bounding box is identical across modes, with and without the history spinner', async ({
    page,
  }) => {
    let gate = holdable()
    await page.route(HISTORY_ROUTE, async (route) => {
      await gate.held
      await route.continue()
    })

    await page.goto('/')
    // Fleet painted (fetchAgents resolved); the held history GET keeps the spinner up.
    await expect(systemAgent(page)).toBeVisible({ timeout: 15000 })
    await expect(spinner(page), 'the held history request shows the spinner').toBeVisible()

    const ref = await switcher(page).boundingBox()
    expect(ref, 'reference box measured with the spinner present').not.toBeNull()

    // Spinner unmounts → nothing moves.
    gate.release()
    await expect(spinner(page)).toBeHidden({ timeout: 15000 })
    await expectSameBox(page, ref, 'after the spinner unmounted')

    // Grid mounts Tidy up / Reset (conditional siblings) → nothing moves;
    // List → nothing moves; back to Timeline → nothing moves.
    for (const mode of [...VIEW_MODES.slice(1), VIEW_MODES[0]]) {
      await modeButton(page, mode).click()
      await expectMode(page, mode)
      if (mode === 'grid') {
        // Proves the conditional siblings are PRESENT while measuring.
        await expect(page.getByRole('button', { name: 'Tidy up' })).toBeVisible()
        await expect(page.getByRole('button', { name: 'Reset', exact: true })).toBeVisible()
      }
      await expectSameBox(page, ref, `in ${mode} mode`)
    }

    // A user-triggered refresh re-shows the spinner → nothing moves.
    gate = holdable()
    await refreshButton(page).click()
    await expect(spinner(page), 'Refresh re-shows the spinner while the GET is held').toBeVisible({
      timeout: 15000,
    })
    await expectSameBox(page, ref, 'while Refresh holds the spinner')
    gate.release()
    await expect(spinner(page)).toBeHidden({ timeout: 15000 })

    // Structural belt: the mechanism itself, asserted on a real boolean.
    const isLast = await switcher(page).evaluate((el) => el.nextElementSibling === null)
    expect(
      isLast,
      'the switcher must be the LAST element child of the controls cluster — a sibling after it is what re-opens the jump'
    ).toBe(true)
  })

  test('@smoke `v` cycles Timeline → Grid → List → Timeline and persists through setViewMode', async ({ page }) => {
    await page.goto('/')
    // Belt-and-braces: clear the saved mode ONCE, in the page, then reload.
    // Never `addInitScript` for this key — it re-runs on `page.reload()` and
    // would wipe the persisted mode the last step asserts on.
    await page.evaluate((k) => localStorage.removeItem(k), MODE_KEY)
    await page.reload()
    await expectMode(page, VIEW_MODES[0])

    for (const next of [...VIEW_MODES.slice(1), VIEW_MODES[0]]) {
      await page.keyboard.press('v')
      await expectMode(page, next)
      // Same store path as a click → the saved selection follows the pane.
      // Read in the BROWSER context — Playwright specs run in Node.
      await expect
        .poll(() => page.evaluate((k) => localStorage.getItem(k), MODE_KEY), {
          message: `localStorage['${MODE_KEY}'] follows the hotkey to ${next}`,
        })
        .toBe(next)
    }

    // Persistence across a reload (persist: true — distinct from the `/agents`
    // redirect's one-shot `persist: false`, which dashboard-list-view pins).
    await page.keyboard.press('v')
    await expectMode(page, 'grid')
    await page.reload()
    await expectMode(page, 'grid')
  })

  test('@smoke guards: `v` is inert in editable targets, under chords and Shift, and inside the filter pill; the switcher advertises the key', async ({
    page,
  }) => {
    await page.goto('/')
    await expectMode(page, 'timeline')

    // Editable target (guard 4): the TIME-RANGE select specifically — the first
    // <select> is the Owner filter on any multi-owner stack, and a native
    // type-ahead on `v` there could pick an owner starting with "v" and filter
    // trinity-system away. The time-range options (1h/6h/24h/3d/7d) have no "v".
    const timeRange = page.locator('select').filter({ hasText: '24h' })
    await timeRange.focus()
    await expect(timeRange).toBeFocused()
    await page.keyboard.press('v')
    await expectMode(page, 'timeline')
    await timeRange.blur()

    // Chords (guard 2) and Shift+V (reserved — the letter never needs Shift).
    for (const chord of ['Control+v', 'Alt+v', 'Meta+v', 'Shift+v']) {
      await page.keyboard.press(chord)
      await expectMode(page, 'timeline')
    }

    // Inside the filter pill (guard 4 again): `v` is a character, not a hotkey.
    await page.keyboard.press('/')
    const pillInput = page.getByTestId('filter-pill').getByRole('textbox', { name: 'Filter agents' })
    await expect(pillInput).toBeFocused()
    await page.keyboard.type('v')
    await expect(pillInput).toHaveValue('v')
    // The typed `v` is now a live QUERY, and `trinity-system` has no "v" in
    // it — its timeline row is legitimately filtered out. So assert the PANE
    // stayed Timeline (neither other pane mounted), not the row.
    await expect(page.locator('.fleet-canvas'), 'grid pane not mounted by the typed v').toHaveCount(0)
    await expect(page.getByPlaceholder('Search agents...'), 'list pane not mounted by the typed v').toHaveCount(0)
    await page.keyboard.press('Escape')
    await expect(page.getByTestId('filter-pill')).toBeHidden()
    // Query cleared → the row is back, and all three timeline markers hold.
    await expectMode(page, 'timeline')

    // POSITIVE CONTROL: the listener was armed and the presses above were
    // SUPPRESSED, not lost — a bare `v` now switches.
    await page.keyboard.press('v')
    await expectMode(page, 'grid')

    // Open modal (guard 5): focus a BUTTON inside the Create Agent modal so
    // guard 4 (editable targets) cannot be the one suppressing the key —
    // this step proves the modal gate specifically.
    await page.getByRole('button', { name: 'Create Agent' }).click()
    const cancel = page.getByRole('button', { name: 'Cancel', exact: true })
    await expect(cancel).toBeVisible()
    await cancel.focus()
    await page.keyboard.press('v')
    await expectMode(page, 'grid')
    await cancel.click()
    await expect(cancel).toBeHidden()

    // Discoverability: the wrapper's tooltip names the key, the way the filter
    // button advertises `/`. A `title`, never an `aria-label` — the buttons'
    // accessible names (`timeline` / `grid` / `list`) are contract.
    await expect(switcher(page)).toHaveAttribute('title', /press v/i)
    for (const mode of VIEW_MODES) {
      await expect(modeButton(page, mode)).not.toHaveAttribute('aria-label')
    }
  })
})
