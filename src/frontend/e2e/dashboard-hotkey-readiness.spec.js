import { test, expect } from '@playwright/test'

/**
 * Dashboard `/` hotkey interaction readiness (#2200).
 *
 * The ent#261 type-to-filter hotkey used to be registered at the END of an
 * `async onMounted`, after `await Promise.allSettled([...5 fetches...])`. The
 * fleet paints as soon as `fetchAgents()` ALONE resolves (the `agents.length > 0`
 * template gate), but the listener waited on the SLOWEST of the five — so the
 * dashboard rendered as interactive while every `/` was silently dropped.
 *
 * WHY THIS SPEC EXISTS SEPARATELY from `dashboard-type-filter.spec.js`: that
 * suite exercises the feature and is exposed to this bug only as a RACE — on a
 * fast idle machine it can pass over the live defect, which is exactly how this
 * shipped. This spec MANUFACTURES the window instead of hoping for it, so it is
 * deterministic in both directions: it fails 100% of the time on the unfixed
 * tree and passes 100% of the time on the fixed one.
 *
 * MECHANISM: delay one NON-PAINT fetch at the network boundary with
 * `page.route` (already this suite's idiom — see `honest-failed-states.spec.js`,
 * `agent-not-found.spec.js`, `circuit-breaker-badge.spec.js`). `/api/system-views`
 * is in the awaited batch but gates nothing this spec asserts on.
 *
 * The agents endpoint is deliberately NOT intercepted — it is the paint gate. Delay
 * that instead and the tiles never appear, the spec passes on the unfixed tree
 * for the wrong reason, and it proves nothing.
 *
 * NOT a `waitForTimeout`: the delay is applied to a mocked RESPONSE, never to the
 * test's own control flow. Every assertion below is an auto-retrying web-first
 * expect. The AC forbids the test sleeping to let the product catch up; this
 * makes the product's lateness observable and then refuses to wait for it.
 *
 * Runs against a live stack — every install has at least `trinity-system`.
 */

// The manufactured window. Assertions below use a timeout comfortably UNDER it,
// so a dropped keypress can never be rescued by the listener arriving late.
const STALL_MS = 2000
const ASSERT_MS = 1200

const pill = (page) => page.getByTestId('filter-pill')
const pillInput = (page) => pill(page).getByRole('textbox', { name: 'Filter agents' })

/** Hold one non-paint mount fetch open for STALL_MS, then let it through. */
async function stallNonPaintFetch(page) {
  await page.route('**/api/system-views**', async (route) => {
    await new Promise((resolve) => setTimeout(resolve, STALL_MS))
    await route.continue()
  })
}

test.describe('dashboard hotkey readiness (#2200)', () => {
  test('@smoke / opens the filter pill the moment the fleet paints, even while a mount fetch is still in flight', async ({
    page,
  }) => {
    await stallNonPaintFetch(page)

    await page.goto('/')

    // The paint gate: tiles are visible as soon as fetchAgents() alone resolves.
    // `/api/system-views` is still stalled at this point — this IS the window.
    await expect(page.locator('[data-agent="trinity-system"]')).toBeVisible({ timeout: 15000 })

    await page.keyboard.press('/')

    // Unfixed: the listener does not exist yet, the keypress reaches `document`
    // with defaultPrevented: false, and nothing consumes it — this times out.
    await expect(pill(page), 'the `/` hotkey must be armed by the time the fleet is painted').toBeVisible({
      timeout: ASSERT_MS,
    })
    await expect(pillInput(page)).toBeFocused({ timeout: ASSERT_MS })
  })

  test('the pill accepts typing in that same window, and the query survives the pending fetch', async ({ page }) => {
    await stallNonPaintFetch(page)

    await page.goto('/')
    await expect(page.locator('[data-agent="trinity-system"]')).toBeVisible({ timeout: 15000 })

    await page.keyboard.press('/')
    await expect(pill(page)).toBeVisible({ timeout: ASSERT_MS })
    // Always await focus before typing — keystrokes racing the nextTick focus
    // land on body and are swallowed by the editable-target guard.
    await expect(pillInput(page)).toBeFocused({ timeout: ASSERT_MS })

    await page.keyboard.type('trinity')
    await expect(pillInput(page)).toHaveValue('trinity')

    // The intent is captured, not discarded: once the stalled fetch lands and the
    // rest of the mount completes, the filter is still applied.
    await expect(page.getByTestId('filter-match-count')).toBeVisible({ timeout: 15000 })
    await expect(page.locator('[data-agent="trinity-system"]')).toBeVisible()
  })
})
