import { test, expect } from '@playwright/test'
import { pickLabelFixture, readLabel, writeLabel } from './helpers/agent-label.js'

/**
 * Dashboard List view e2e (trinity-enterprise#260).
 *
 * The List is the third dashboard mode (Timeline / Grid / List) — the retired
 * standalone Agents page consolidated into the dashboard chassis. These specs
 * cover the mode toggle + row rendering, mode persistence (AC-4), the
 * /agents → /?view=list redirect (one-shot, NON-persisting, param stripped —
 * AC-2), the local name filter + filtered-empty recovery, and a three-mode
 * round-trip.
 *
 * Runs against a live stack — every install has at least `trinity-system`, so
 * no fixtures are needed. Rows carry `data-agent="<slug>"` (the grid's tile
 * pattern) so locators never text-match display names. Deliberately NO
 * toggle-visibility assertions: CI's only agent is the system agent, whose Run
 * toggle is guarded (grid parity) and whose Autonomy/ReadOnly toggles are
 * `invisible` by design.
 *
 * The auth storageState pre-seeds `trinity_onboarding_dismissed_v1`, so the
 * onboarding wizard never auto-opens under these tests; the explicit
 * `?onboarding=1` param bypasses that key (covered below).
 */

const MODE_KEY = 'trinity-dashboard-view'

async function gotoList(page) {
  await page.goto('/')
  await page.getByRole('button', { name: 'list', exact: true }).click()
  await expect(page.getByPlaceholder('Search agents...')).toBeVisible({ timeout: 15000 })
}

test.describe('dashboard list view (trinity-enterprise#260)', () => {
  test('@smoke mode toggle shows List and renders the system agent row', async ({ page }) => {
    await gotoList(page)

    const sysRow = page.locator('[data-agent="trinity-system"]')
    await expect(sysRow).toBeVisible({ timeout: 15000 })
    await expect(sysRow).toContainText('SYSTEM')
    // Name links to the agent detail page.
    await expect(sysRow.locator('a[href="/agents/trinity-system"]').first()).toBeVisible()

    // List-mode toolbar furniture (name search, status filter, sort).
    await expect(page.getByRole('button', { name: 'Running', exact: true })).toBeVisible()
    await expect(page.locator('select').filter({ hasText: 'Newest First' })).toBeVisible()
  })

  test('mode choice persists across reload (AC-4)', async ({ page }) => {
    await gotoList(page)
    expect(await page.evaluate((k) => localStorage.getItem(k), MODE_KEY)).toBe('list')

    await page.reload()
    // Lands straight back in list mode without touching the toggle.
    await expect(page.getByPlaceholder('Search agents...')).toBeVisible({ timeout: 15000 })
  })

  test('@smoke /agents redirects into List mode, strips the param, does NOT persist (AC-2)', async ({ page }) => {
    await page.goto('/agents')

    // Lands on the bare Dashboard URL — `view` applied then stripped.
    await expect(page).toHaveURL(/^https?:\/\/[^/]+\/$/, { timeout: 10000 })
    await expect(page.locator('[data-agent="trinity-system"]')).toBeVisible({ timeout: 15000 })
    await expect(page.getByPlaceholder('Search agents...')).toBeVisible()

    // The redirect is a one-shot intent, not a preference statement: the saved
    // view selection must be untouched (fresh context → still unset).
    expect(await page.evaluate((k) => localStorage.getItem(k), MODE_KEY)).toBeNull()
  })

  test('/agents?onboarding=1 keeps the onboarding param through the redirect', async ({ page }) => {
    await page.goto('/agents?onboarding=1')

    // `view` is stripped; `onboarding` survives the query-preserving redirect
    // AND the Dashboard's param strip (it spreads the rest of the query).
    await expect(page).toHaveURL(/\?onboarding=1$/, { timeout: 10000 })
    // Explicit ?onboarding=1 bypasses the pre-seeded dismissal key.
    await expect(page.locator('#onboarding-title')).toBeVisible({ timeout: 15000 })
    // Behind the wizard, the List pane mounted.
    await expect(page.getByPlaceholder('Search agents...')).toBeVisible()
  })

  test('name filter narrows rows and filtered-empty recovers via Clear all', async ({ page }) => {
    await gotoList(page)
    await expect(page.locator('[data-agent="trinity-system"]')).toBeVisible({ timeout: 15000 })

    // A query no agent matches → filtered-empty state (panel-local), with the
    // recovery affordance.
    await page.getByPlaceholder('Search agents...').fill('zzz-no-such-agent')
    await expect(page.locator('[data-agent]')).toHaveCount(0)
    await expect(page.getByText('No matching agents')).toBeVisible()

    await page.getByRole('button', { name: 'Clear all filters' }).click()
    await expect(page.locator('[data-agent="trinity-system"]')).toBeVisible()
  })

  test('three-mode round-trip mounts and unmounts panes cleanly', async ({ page }) => {
    await gotoList(page)
    await expect(page.locator('[data-agent="trinity-system"]')).toBeVisible({ timeout: 15000 })

    // List → Grid: the list toolbar tears down, the fleet canvas mounts.
    await page.getByRole('button', { name: 'grid', exact: true }).click()
    await expect(page.getByPlaceholder('Search agents...')).toHaveCount(0)
    await expect(page.locator('.fleet-canvas')).toBeVisible({ timeout: 15000 })

    // Grid → Timeline: the canvas tears down.
    await page.getByRole('button', { name: 'timeline', exact: true }).click()
    await expect(page.locator('.fleet-canvas')).toHaveCount(0)

    // Timeline → List: the list pane remounts.
    await page.getByRole('button', { name: 'list', exact: true }).click()
    await expect(page.getByPlaceholder('Search agents...')).toBeVisible({ timeout: 15000 })
    await expect(page.locator('[data-agent="trinity-system"]')).toBeVisible({ timeout: 15000 })
  })
})

/**
 * Column alignment + honest identity (#2358).
 *
 * Three defects on one surface, all row-shaped:
 *
 *  1. The header and every row were INDEPENDENT grids sharing a copy of one
 *     template string, so each resolved its own `auto` tracks and the `1fr`
 *     name track absorbed the difference — a row reading `14/16` put
 *     Controls / Success at a different x than a row reading `0`.
 *  2. A labelled agent rendered the label alone, with the slug on a `title`.
 *  3. Every name trailed a runtime pill and a subscription-pressure badge.
 *
 * ⚠️ CI's fleet is ONE agent (`trinity-system`). ⚠️
 * With a single row, test 1 proves HEADER parity only — row-to-row alignment
 * across varying `Exec / Sched` content is structural (one subgrid, one set of
 * tracks) and is checked by eye on a real fleet at review time. The resize
 * re-run is a layout-stability check, NOT a varying-content one: the single
 * agent's content does not change between viewports.
 *
 * ⚠️ These tests LABEL a live agent. ⚠️
 * They read the prior label first and restore it in `test.afterEach` — a
 * `finally` inside a body that times out is not reliably run — and they are
 * serial, because `playwright.config` is `fullyParallel: true` and a sibling
 * worker asserting on the same fleet would see the borrowed label. No other
 * spec asserts the system agent's RENDERED name (they locate by `data-agent`
 * and assert `SYSTEM`), so the borrow is invisible to them.
 */

const FIXTURE_LABEL = 'Platform Orchestrator'
const COLUMNS = ['name', 'status', 'controls', 'success', 'stats']

/** Header/row x-parity for every labelled column, at the current viewport. */
async function expectColumnsAligned(page, note) {
  for (const col of COLUMNS) {
    const header = page.locator(`[data-testid="list-header"] [data-col="${col}"]`)
    await expect(header, `${note}: header cell ${col}`).toBeVisible()
    const headerBox = await header.boundingBox()

    const cells = page.locator(`[data-agent] [data-col="${col}"]`)
    const count = await cells.count()
    expect(count, `${note}: rows carry a ${col} cell`).toBeGreaterThan(0)
    for (let i = 0; i < count; i++) {
      const box = await cells.nth(i).boundingBox()
      expect(box, `${note}: row ${i} ${col} cell has a box`).not.toBeNull()
      // One sizing context ⇒ the header and EVERY row start this column at the
      // same x. Sub-pixel layout rounding is the only tolerance allowed.
      expect(
        Math.abs(box.x - headerBox.x),
        `${note}: row ${i} column "${col}" x=${box.x} vs header x=${headerBox.x}`
      ).toBeLessThanOrEqual(1)
    }
  }
}

test.describe('dashboard list column alignment + identity (#2358)', () => {
  test.describe.configure({ mode: 'serial' })

  let fixtureAgent = null
  let priorLabel = null
  let labelBorrowed = false

  test.afterEach(async ({ baseURL }) => {
    if (!labelBorrowed) return
    labelBorrowed = false
    // Restore EXACTLY what was there — including null. Blind-clearing would
    // wipe an operator's own label on a dev stack.
    await writeLabel(baseURL, fixtureAgent, priorLabel)
  })

  async function borrowLabel(page, baseURL, label = FIXTURE_LABEL) {
    if (fixtureAgent === null) {
      fixtureAgent = await pickLabelFixture(baseURL)
      priorLabel = await readLabel(baseURL, fixtureAgent)
    }
    labelBorrowed = true
    await writeLabel(baseURL, fixtureAgent, label)
    await page.reload()
    await expect(page.getByPlaceholder('Search agents...')).toBeVisible({ timeout: 15000 })
    return page.locator(`[data-agent="${fixtureAgent}"]`)
  }

  test('@smoke lg header and rows resolve ONE set of columns', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    await gotoList(page)
    await expect(page.locator('[data-agent]').first()).toBeVisible({ timeout: 15000 })

    await expectColumnsAligned(page, '1280px')

    // Layout stability across a resize (not varying content — see the header).
    await page.setViewportSize({ width: 1440, height: 900 })
    await expectColumnsAligned(page, '1440px')
  })

  test('the capacity meter sits in grid rows 1-2, never an implicit third row', async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    await gotoList(page)
    const row = page.locator('[data-agent]').first()
    await expect(row).toBeVisible({ timeout: 15000 })

    const meter = await row.locator('[data-testid="capacity-meter-lg"]').boundingBox()
    const name = await row.locator('[data-col="name"]').boundingBox()
    const secondary = await row.locator('[data-testid="row-secondary-lg"]').boundingBox()
    expect(meter, 'the lg row renders a capacity meter').not.toBeNull()

    // Without definite placement in BOTH axes, sparse auto-placement puts the
    // meter in rows 2-3: it would start BELOW the name line and end below the
    // secondary line, opening a third row under every row in the list.
    expect(meter.y, 'meter starts on grid row 1 (at or above the name line)').toBeLessThanOrEqual(
      name.y + 1
    )
    expect(
      meter.y + meter.height,
      'meter ends within grid row 2 (at or above the secondary line bottom)'
    ).toBeLessThanOrEqual(secondary.y + secondary.height + 1)
  })

  test('a labelled agent renders BOTH names, and the slug is selectable', async ({
    page,
    baseURL,
  }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    await gotoList(page)
    const row = await borrowLabel(page, baseURL)
    await expect(row).toBeVisible({ timeout: 15000 })

    // Primary line: the label.
    const nameLink = row
      .locator(`a[href="/agents/${fixtureAgent}"]`)
      .filter({ visible: true })
      .first()
    await expect(nameLink).toHaveText(FIXTURE_LABEL)

    // Secondary line: the slug, as real text — not a tooltip.
    const slug = row.locator('[data-testid="agent-slug-lg"]')
    await expect(slug).toBeVisible()
    await expect(slug).toHaveText(fixtureAgent)
    // FR-4 says copyable, so it must not be selection-blocked.
    const userSelect = await slug.evaluate((el) => getComputedStyle(el).userSelect)
    expect(userSelect).not.toBe('none')
    // And it is not inside the link — clicking the identity must not navigate.
    expect(await slug.evaluate((el) => Boolean(el.closest('a')))).toBe(false)

    // The exception markers that DO earn the name cell are untouched.
    await expect(page.locator('[data-agent="trinity-system"]')).toContainText('SYSTEM')
  })

  test('md and base show the slug too, and a label never changes row height', async ({
    page,
    baseURL,
  }) => {
    await gotoList(page)

    for (const [name, width, slugId] of [
      ['md', 900, 'agent-slug-md'],
      ['base', 390, 'agent-slug-base'],
    ]) {
      await page.setViewportSize({ width, height: 900 })

      // Unlabelled first — this is the height a labelled row must match.
      let row = await borrowLabel(page, baseURL, null)
      await expect(row).toBeVisible({ timeout: 15000 })
      await expect(row.locator(`[data-testid="${slugId}"]`)).toHaveCount(0)
      const unlabelledHeight = (await row.boundingBox()).height

      row = await borrowLabel(page, baseURL)
      await expect(row).toBeVisible({ timeout: 15000 })
      const slug = row.locator(`[data-testid="${slugId}"]`)
      await expect(slug, `${name}: the slug renders`).toBeVisible()
      await expect(slug).toHaveText(fixtureAgent)
      const labelledHeight = (await row.boundingBox()).height

      // The slug rides a line the row ALREADY always renders, so labelling an
      // agent must not make its row taller (contract: layout stability).
      expect(
        Math.abs(labelledHeight - unlabelledHeight),
        `${name}: labelled row ${labelledHeight}px vs unlabelled ${unlabelledHeight}px`
      ).toBeLessThanOrEqual(1)

      // Any runtime badge the row still shows is on the secondary line, below
      // the name — never beside it. (A default-runtime fleet shows none at all,
      // which is the point of the rule; this only fires when one is present.)
      const badge = row.locator('[data-testid="runtime-badge"]').filter({ visible: true })
      if ((await badge.count()) > 0) {
        const badgeBox = await badge.first().boundingBox()
        const linkBox = await row
          .locator(`a[href="/agents/${fixtureAgent}"]`)
          .filter({ visible: true })
          .first()
          .boundingBox()
        expect(badgeBox.y, `${name}: runtime badge is off the name line`).toBeGreaterThan(
          linkBox.y
        )
      }
    }
  })
})
