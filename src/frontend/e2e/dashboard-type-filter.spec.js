import { test, expect } from '@playwright/test'

/**
 * Dashboard hotkey type-to-filter e2e (trinity-enterprise#261).
 *
 * `/` opens a floating filter pill over the pane area; typing filters agents
 * live across all three dashboard modes (Timeline / Grid / List) through the
 * store-level `visibleAgents` seam. Esc clears + dismisses; nothing persists.
 *
 * Spec-writing rules (plan eng F3/F9):
 * - After pressing `/`, ALWAYS await `toBeFocused()` on the pill input BEFORE
 *   `keyboard.type()` — keystrokes racing the nextTick focus land on body and
 *   are swallowed by the editable-target guard (guaranteed flake otherwise).
 * - Count assertions use regexes like /^1 of \d+ match$/ — X is the claim, Y
 *   is environment (a seeded canary agent must not break the spec).
 *
 * Runs against a live stack — every install has at least `trinity-system`.
 * The auth storageState pre-seeds the onboarding dismissal key, so the wizard
 * never auto-opens under these tests.
 */

const pill = (page) => page.getByTestId('filter-pill')
const pillInput = (page) => pill(page).getByRole('textbox', { name: 'Filter agents' })
const matchCount = (page) => page.getByTestId('filter-match-count')
const queryEmpty = (page) => page.getByTestId('filter-query-empty')
const kbdHint = (page) => page.getByTestId('filter-kbd-hint')

async function gotoTimeline(page) {
  await page.goto('/')
  // Timeline is the default mode; wait for the fleet row so the filter has
  // loaded data to operate on.
  await expect(page.locator('[data-agent="trinity-system"]')).toBeVisible({ timeout: 15000 })
}

async function gotoGrid(page) {
  await page.goto('/')
  await page.getByRole('button', { name: 'grid', exact: true }).click()
  await expect(page.locator('.fleet-canvas')).toBeVisible()
  await expect(page.locator('.gv-tile[data-agent="trinity-system"]')).toBeVisible({ timeout: 15000 })
}

async function gotoList(page) {
  await page.goto('/')
  await page.getByRole('button', { name: 'list', exact: true }).click()
  await expect(page.locator('[data-agent="trinity-system"]')).toBeVisible({ timeout: 15000 })
}

async function openFilter(page) {
  await page.keyboard.press('/')
  await expect(pill(page)).toBeVisible()
  await expect(pillInput(page)).toBeFocused()
}

test.describe('dashboard type-to-filter (trinity-enterprise#261)', () => {
  test('@smoke grid: / filters tiles live, query-empty keeps the pane mounted, Esc restores', async ({ page }) => {
    await gotoGrid(page)

    await openFilter(page)
    await page.keyboard.type('trinity')

    // Matching tile stays; honest count (X is the claim, Y is environment).
    await expect(page.locator('.gv-tile[data-agent="trinity-system"]')).toBeVisible()
    await expect(matchCount(page)).toHaveText(/^1 of \d+ match$/)

    // Zero-match: overlay names the next action, onboarding CTA unreachable,
    // and the grid canvas stays MOUNTED (transient zero-match while typing
    // must never unmount the pane — plan eng F1 pin).
    await pillInput(page).fill('zzz')
    await expect(page.locator('.gv-tile')).toHaveCount(0)
    await expect(queryEmpty(page)).toBeVisible()
    await expect(queryEmpty(page)).toContainText('No agents match "zzz"')
    await expect(page.getByRole('button', { name: 'Get started' })).toHaveCount(0)
    await expect(page.locator('.fleet-canvas')).toBeVisible()

    // Esc (input-scoped): clears + dismisses; tiles restored.
    await page.keyboard.press('Escape')
    await expect(pill(page)).toBeHidden()
    await expect(queryEmpty(page)).toBeHidden()
    await expect(page.locator('.gv-tile[data-agent="trinity-system"]')).toBeVisible()
  })

  test('timeline: / hides non-matching rows and Esc restores them', async ({ page }) => {
    await gotoTimeline(page)

    await openFilter(page)
    await page.keyboard.type('zzz')

    await expect(page.locator('[data-agent]')).toHaveCount(0)
    await expect(queryEmpty(page)).toBeVisible()

    await page.keyboard.press('Escape')
    await expect(pill(page)).toBeHidden()
    await expect(page.locator('[data-agent="trinity-system"]')).toBeVisible()
  })

  test('list: query-empty overlay precedes the true-empty CTA; clear via the pill ×', async ({ page }) => {
    await gotoList(page)

    await openFilter(page)
    await page.keyboard.type('zzz')

    await expect(queryEmpty(page)).toBeVisible()
    // The onboarding CTA must be unreachable while a query is active.
    await expect(page.getByRole('button', { name: 'Get started' })).toHaveCount(0)
    // The panel's OWN filtered-empty card must also stay hidden — the chassis
    // overlay owns query-zero messaging; two contradicting CTAs must never
    // render at once (review fix: panel card gated on a non-empty prop).
    await expect(page.getByText('No matching agents')).toHaveCount(0)

    // Mouse parity: the pill × clears + closes.
    await page.getByTestId('filter-clear').click()
    await expect(pill(page)).toBeHidden()
    await expect(page.locator('[data-agent="trinity-system"]')).toBeVisible()
  })

  test('guards: / in an editable field does not open; / inside the pill types a literal slash', async ({ page }) => {
    await gotoTimeline(page)

    // Focus the time-range <select> — an editable target; `/` must not fire.
    await page.locator('select').first().focus()
    await page.keyboard.press('/')
    await expect(pill(page)).toBeHidden()

    // Blur the select, open the pill, and type a query containing '/':
    // the document listener must not re-toggle; the literal lands in the value.
    await page.locator('select').first().blur()
    await openFilter(page)
    await page.keyboard.type('a/b')
    await expect(pillInput(page)).toHaveValue('a/b')
    await expect(pill(page)).toBeVisible()
  })

  test('discoverability: header kbd hint toggles — opens closed, clears when active', async ({ page }) => {
    await gotoGrid(page)

    await kbdHint(page).click()
    await expect(pill(page)).toBeVisible()
    await expect(pillInput(page)).toBeFocused()
    await page.keyboard.type('trinity')
    await expect(matchCount(page)).toHaveText(/^1 of \d+ match$/)

    // Second click: clears + closes (mouse parity with Esc).
    await kbdHint(page).click()
    await expect(pill(page)).toBeHidden()
    await expect(page.locator('.gv-tile[data-agent="trinity-system"]')).toBeVisible()
  })

  test('nothing persists: reload starts unfiltered with no pill', async ({ page }) => {
    await gotoGrid(page)

    await openFilter(page)
    await page.keyboard.type('trinity')
    await expect(matchCount(page)).toHaveText(/^1 of \d+ match$/)

    await page.reload()
    await expect(page.locator('.gv-tile[data-agent="trinity-system"]')).toBeVisible({ timeout: 15000 })
    await expect(pill(page)).toBeHidden()
  })

  test('cross-mode: filter survives a mode switch; document-Esc backstop clears after focus wanders', async ({ page }) => {
    await gotoGrid(page)

    await openFilter(page)
    await page.keyboard.type('trinity')
    await expect(matchCount(page)).toHaveText(/^1 of \d+ match$/)

    // Switch to timeline: the store query persists and the pill stays visible
    // (deliberate — visible state, Esc anywhere).
    await page.getByRole('button', { name: 'timeline', exact: true }).click()
    await expect(page.locator('[data-agent="trinity-system"]')).toBeVisible({ timeout: 15000 })
    await expect(pill(page)).toBeVisible()
    await expect(matchCount(page)).toHaveText(/^1 of \d+ match$/)

    // Focus wanders out of the input, then Esc: the document-level backstop
    // must still clear (plan strategy F8 pin). blur() is structural — a
    // coordinate click on the pane could land on an agent row / toggle on
    // seeded fleets (flake + side-effect hazard).
    await pillInput(page).blur()
    await expect(pillInput(page)).not.toBeFocused()
    await page.keyboard.press('Escape')
    await expect(pill(page)).toBeHidden()
    await expect(page.locator('[data-agent="trinity-system"]')).toBeVisible()
  })

  test('Enter blurs the input and keeps the filter; / refocuses', async ({ page }) => {
    await gotoGrid(page)

    await openFilter(page)
    await page.keyboard.type('trinity')
    await page.keyboard.press('Enter')

    await expect(pillInput(page)).not.toBeFocused()
    await expect(pill(page)).toBeVisible()
    await expect(matchCount(page)).toHaveText(/^1 of \d+ match$/)

    await page.keyboard.press('/')
    await expect(pillInput(page)).toBeFocused()
    // Refocus must not have clobbered the query.
    await expect(pillInput(page)).toHaveValue('trinity')
  })
})
