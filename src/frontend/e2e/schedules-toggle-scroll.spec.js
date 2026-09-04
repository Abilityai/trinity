import { test, expect, request } from '@playwright/test'

/**
 * Schedules tab — toggling a schedule must not move the page (#1634).
 *
 * The bug: the panel-wide spinner was gated on the in-flight flag alone
 * (`v-if="loading"`), so every `loadSchedules()` refetch unmounted the whole
 * list. The document collapsed to spinner height, the browser clamped
 * `window.scrollY` to 0, and the row you had just clicked was thrown off
 * screen. The fix gates the spinner on "no data yet"
 * (`loading && schedules.length === 0`), plus four supporting changes.
 *
 * What each test pins:
 *   T1  toggling does not move the page          — the fix itself (AC #1/#3/#6)
 *   T2  per-row spinner + disabled control       — declared regression guard (AC #2)
 *   T3  panel spinner on genuine first load      — declared regression guard (AC #4, mount)
 *   T4  agent switch still shows the spinner     — the watcher clear (AC #4, switch)
 *   T5  a superseded load never paints           — the `loadSeq` guard
 *
 * MEASUREMENT NOTE — the primary invariant is the toggled row's viewport `y`,
 * NOT `window.scrollY`. Chrome's default `overflow-anchor: auto` *deliberately*
 * mutates the scroll offset when content above the anchor changes height,
 * precisely so the visible content stays still. Asserting `scrollY` parity
 * would therefore fail on correct behaviour. `scrollY` is kept only as a coarse
 * secondary signal (the bug moves it by hundreds of pixels, not units).
 *
 * FIXTURE SAFETY — these schedules are created on a LIVE agent against a LIVE
 * scheduler. Every one uses a far-future cron (Feb 29) so none can ever
 * dispatch a real Claude turn, burn tokens, or pollute the executions table
 * this very panel reads. `beforeAll` sweeps leftovers from a crashed run and
 * `afterAll` deletes each seeded id individually.
 *
 * @interactive — needs a real, non-system, non-ephemeral agent. CI runs
 * `test:e2e:smoke` (@smoke only) against a zero-agent stack, so this file is
 * deliberately local-only and cannot be tagged @smoke.
 */

test.describe.configure({ mode: 'serial' })

// Feb 29 — a valid 5-field cron whose next occurrence is years away. The
// fixture must never actually fire.
const FAR_FUTURE_CRON = '0 4 29 2 *'
const FIXTURE_PREFIX = 'e2e-1634-'
const SEED_COUNT = 14
// Two seeded ENABLED rows hold the #1796 autonomy banner's visibility
// invariant across every toggle: the banner is gated on
// `enabledScheduleCount > 0`, so starting at 2 means no toggle can cross the
// 0↔1 boundary and make the banner appear/disappear. A banner appearing is a
// legitimate reflow that would move every row — and would be indistinguishable
// from the bug.
const SEED_ENABLED = 2

let api
let token
let AGENT_A = process.env.SCHEDULES_TEST_AGENT || ''
let AGENT_B = ''
/** @type {{id: string, name: string, enabled: boolean}[]} */
let seeded = []

function authHeaders() {
  return { Authorization: `Bearer ${token}` }
}

async function listSchedules(agent) {
  const resp = await api.get(`/api/agents/${agent}/schedules`, { headers: authHeaders() })
  if (!resp.ok()) return []
  const body = await resp.json()
  return Array.isArray(body) ? body : []
}

async function deleteSchedule(agent, id) {
  try {
    await api.delete(`/api/agents/${agent}/schedules/${id}`, { headers: authHeaders() })
  } catch {
    // teardown is best-effort — never fail a run on cleanup
  }
}

async function sweepFixtures(agent) {
  const existing = await listSchedules(agent)
  for (const s of existing) {
    if (typeof s.name === 'string' && s.name.startsWith(FIXTURE_PREFIX)) {
      await deleteSchedule(agent, s.id)
    }
  }
}

test.beforeAll(async ({ baseURL }) => {
  api = await request.newContext({ baseURL })

  const loginResp = await api.post('/api/token', {
    form: { username: 'admin', password: process.env.ADMIN_PASSWORD || '' },
  })
  if (!loginResp.ok()) throw new Error(`Admin login failed: ${loginResp.status()}`)
  token = (await loginResp.json()).access_token

  const listResp = await api.get('/api/agents', { headers: authHeaders() })
  if (!listResp.ok()) throw new Error(`Agent list failed: ${listResp.status()}`)
  const body = await listResp.json()
  const agents = Array.isArray(body) ? body : body.agents || []

  // A schedule on an ephemeral agent is a 400 (`schedule_on_ephemeral_agent`),
  // and the system agent must not be touched at all.
  const usable = agents
    .filter((a) => !a.is_system && !a.ephemeral && !String(a.name).startsWith('trinity-system'))
    .map((a) => a.name)

  if (AGENT_A && !usable.includes(AGENT_A)) {
    throw new Error(`SCHEDULES_TEST_AGENT="${AGENT_A}" is not a usable (non-system, non-ephemeral) agent`)
  }
  if (!AGENT_A) {
    if (usable.length === 0) throw new Error('No non-system, non-ephemeral agent available to test schedules')
    AGENT_A = usable[0]
  }
  AGENT_B = usable.find((n) => n !== AGENT_A) || ''
  // HARD requirement, not a skip: T4/T5 are the only evidence that the watcher
  // clear and the loadSeq guard aren't dead code. A "3 passed, 2 skipped" run
  // reads as green while the red-first matrix silently never executed.
  if (!AGENT_B) {
    throw new Error(
      'This spec needs TWO non-system, non-ephemeral agents (T4/T5 exercise ' +
        'the agent-switch path). Create a second agent, or set ' +
        'SCHEDULES_TEST_AGENT to pick which one gets the fixture rows.'
    )
  }

  // A previous crashed run may have left fixture rows behind.
  await sweepFixtures(AGENT_A)

  seeded = []
  for (let i = 0; i < SEED_COUNT; i++) {
    const enabled = i < SEED_ENABLED
    const name = `${FIXTURE_PREFIX}${String(i).padStart(2, '0')}`
    const resp = await api.post(`/api/agents/${AGENT_A}/schedules`, {
      headers: authHeaders(),
      data: {
        name,
        cron_expression: FAR_FUTURE_CRON,
        message: 'e2e #1634 fixture — far-future cron, must never run',
        enabled,
        timezone: 'UTC',
        description: 'Playwright fixture for the #1634 scroll-stability spec',
        // timeout_seconds omitted on purpose: NULL means "inherit the agent's
        // execution_timeout_seconds", which cannot trip the #929
        // `schedule_timeout_exceeds_agent_cap` 400 the way a literal would.
      },
    })
    if (!resp.ok()) {
      throw new Error(`Seeding ${name} failed: ${resp.status()} ${await resp.text()}`)
    }
    const created = await resp.json()
    seeded.push({ id: created.id, name, enabled })
  }
})

test.afterAll(async () => {
  if (api && AGENT_A) {
    for (const s of seeded) await deleteSchedule(AGENT_A, s.id)
  }
  if (api) await api.dispose()
})

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

function rowFor(page, scheduleId) {
  return page.locator(`[data-testid="schedule-row"][data-schedule-id="${scheduleId}"]`)
}

/**
 * Wait until the list has painted AND the #1115 per-schedule perf chips have
 * landed. `loadSchedules()` fires `loadPerf()` un-awaited, and the rollup
 * returns a row for EVERY non-deleted schedule (including zero-run ones), so
 * each row grows by a chip line a beat after the list first paints. Baselining
 * before that measures a layout that is still moving.
 */
async function waitForListSettled(page) {
  const rows = page.getByTestId('schedule-row')
  await expect(rows.first()).toBeVisible({ timeout: 20000 })
  await expect
    .poll(
      async () => {
        const total = await rows.count()
        const withChips = await page.locator('[data-testid="schedule-row"]:has-text("7d success")').count()
        return total > 0 && withChips === total
      },
      { timeout: 20000, message: 'perf chips never finished rendering on every row' }
    )
    .toBe(true)
  // Final settle for the chip reflow.
  await page.waitForTimeout(400)
}

async function openSchedulesTab(page) {
  // .first(): the visible tab row precedes OverflowTabs' aria-hidden measuring
  // mirror, which is also a nav.-mb-px (#1114).
  await expect(page.locator('nav.-mb-px').first()).toBeVisible({ timeout: 20000 })
  await page.getByRole('button', { name: 'Schedules', exact: true }).first().click()
}

/**
 * Same-document A→B agent switch.
 *
 * PREMISE (load-bearing): the A→Dashboard→B legs PRESERVE the AgentDetail
 * component rather than remounting it — App.vue wraps <router-view> in
 * `<KeepAlive :include="['SystemAgent','AgentDetail']">` and AgentDetail sets
 * `defineOptions({ name: 'AgentDetail' })` so the include matches. Had it
 * remounted, T4/T5 would pass even with the watcher-clear and loadSeq guard
 * deleted — the exact tautology the notes below warn about.
 *
 * This MUST NOT use `page.goto()`: a goto is a full document navigation that
 * tears down Vue and remounts SchedulesPanel, so the `props.agentName` watcher
 * never fires and the test would pass even with the watcher-clear deleted.
 * `page.goBack()` between two gotos is no better — each goto is its own
 * *document*, so back is another cross-document navigation.
 *
 * Both clicks below are same-document router pushes, so `route.params.name`
 * flips inside the SAME document: AgentDetail's `watch(() => route.params.name)`
 * → `loadAgent()` → `:agent-name` prop flip → SchedulesPanel's watcher.
 * (The intermediate `/` leg is a no-op: that watcher guards on
 * `newName && newName !== oldName`, and `route.params.name` is undefined at `/`.)
 *
 * `activeTab` survives the round trip — it is never URL-synced, and
 * `applyDeepLinkRouting()` only acts when `route.query.tab` is present — so the
 * panel is still mounted on Schedules when we land on B.
 *
 * LOCATOR NOTE — the agent row is addressed by `data-agent` + `href`, never by
 * link text. `AgentListPanel` renders `agentNameParts(agent).primary` (#2358 —
 * the same resolution `agentDisplayName` performs, now composed so the slug can
 * ride the row's secondary line), which is the owner-settable `display_label`
 * when one is set (ent#181/#1640), so a
 * `getByRole('link', { name: <slug> })` would silently stop matching the moment
 * anyone labels the agent. `dashboard-list-view.spec.js` already states this as
 * the house rule: "Rows carry `data-agent="<slug>"` … so locators never
 * text-match display names."
 */
async function switchAgentInSpa(page, targetAgent) {
  await page.getByRole('link', { name: 'Dashboard', exact: true }).first().click()
  // List mode is what renders a per-agent row; wait for its toolbar so a slow
  // agent fetch can't be mistaken for a missing row.
  await expect(page.getByPlaceholder('Search agents...')).toBeVisible({ timeout: 20000 })
  const targetLink = page
    .locator(`[data-agent="${targetAgent}"] a[href="/agents/${targetAgent}"]`)
    .first()
  await expect(targetLink).toBeVisible({ timeout: 20000 })
  await targetLink.click()
  await expect(page).toHaveURL(new RegExp(`/agents/${targetAgent}(\\?|$|/)`), { timeout: 20000 })
}

// The Dashboard's List mode is what renders a per-agent <router-link>. The
// view mode is read from localStorage at store init, so it must be seeded
// BEFORE the first document loads.
async function forceDashboardListMode(page) {
  await page.addInitScript(() => {
    localStorage.setItem('trinity-dashboard-view', 'list')
  })
}

// ---------------------------------------------------------------------------
// tests
// ---------------------------------------------------------------------------

test.describe('Schedules tab toggle scroll stability (#1634)', () => {
  /**
   * T1 — the load-bearing proof. RED against the unmodified tree: pre-fix the
   * list unmounts on the post-toggle refetch, the document collapses, and the
   * row is thrown to the top (Δy of hundreds of px).
   */
  test('@interactive toggling a schedule does not move the page (AC #1, #3, #6)', async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 800 })
    await page.goto(`/agents/${AGENT_A}?tab=schedules`)
    await waitForListSettled(page)

    // Three consecutive seeded rows, all starting DISABLED so no toggle can
    // cross the banner's 0↔1 enabled boundary (see SEED_ENABLED).
    //
    // Index window: the disabled set is seeded rows 2..13, so slice(4, 7) is
    // seeded 6/7/8 — the 7th–9th of at least 14 rows, i.e. mid-list with five
    // rows still below them. With 14 rows of ≥100px against an 800px viewport
    // the document is always scrollable and those rows always sit well below
    // the fold, so `scrollIntoView({ block: 'center' })` always has room to
    // scroll. Pre-existing schedules on the agent only push them further down.
    const targets = seeded.filter((s) => !s.enabled).slice(4, 7)
    expect(targets.length).toBe(3)

    for (const target of targets) {
      const row = rowFor(page, target.id)
      await expect(row).toHaveCount(1)

      // Park the row mid-viewport so there is scroll room in both directions.
      await row.evaluate((el) => el.scrollIntoView({ block: 'center' }))
      await page.waitForTimeout(300)

      // Re-baseline before EVERY iteration: scroll anchoring can legitimately
      // shift the offset between iterations.
      const beforeY = (await row.boundingBox()).y
      const beforeScroll = await page.evaluate(() => window.scrollY)
      // FIXTURE PRECONDITION, not the assertion under test: if the page is not
      // actually scrolled, the bug (clamp-to-top) cannot manifest and a green
      // result would prove nothing. Fail loudly and unambiguously instead.
      expect(
        beforeScroll,
        `fixture precondition failed: the page is not scrolled after centring row ${target.name}, so the clamp-to-top bug could not manifest`
      ).toBeGreaterThan(0)

      const pill = row.getByTestId('schedule-status')
      const wasActive = (await pill.innerText()).trim() === 'Active'

      await row.getByTestId('schedule-toggle').click()

      // Row-scoped: 12 of the 14 fixture rows read "Disabled", so an unscoped
      // text locator is ambiguous by construction. Waiting on the pill also
      // proves AC #3 — the refetched row reflects the new server state.
      await expect(pill).toHaveText(wasActive ? 'Disabled' : 'Active', { timeout: 20000 })
      await page.waitForTimeout(400) // let any legitimate reflow settle

      const afterY = (await row.boundingBox()).y
      const afterScroll = await page.evaluate(() => window.scrollY)

      // PRIMARY — scroll-anchoring-robust.
      expect(
        Math.abs(afterY - beforeY),
        `row ${target.name} moved in the viewport after toggling`
      ).toBeLessThanOrEqual(2)
      // SECONDARY — coarse. The bug moves this by hundreds of px.
      expect(
        Math.abs(afterScroll - beforeScroll),
        `document scroll jumped after toggling ${target.name}`
      ).toBeLessThanOrEqual(5)
    }
  })

  /**
   * T2 — declared regression guard (green on every baseline). Justified because
   * the fix makes the per-row spinner the ONLY remaining feedback on the toggle
   * path: the panel-wide spinner no longer appears there.
   */
  test('@interactive the toggled row shows its own spinner and disables its control (AC #2)', async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 800 })

    let release
    const held = new Promise((resolve) => {
      release = resolve
    })
    await page.route(
      (url) => /^\/api\/agents\/[^/]+\/schedules\/[^/]+\/(enable|disable)$/.test(url.pathname),
      async (route) => {
        await held
        await route.continue()
      }
    )

    await page.goto(`/agents/${AGENT_A}?tab=schedules`)
    await waitForListSettled(page)

    const target = seeded.find((s) => !s.enabled)
    const row = rowFor(page, target.id)
    const button = row.getByTestId('schedule-toggle')

    await button.click()

    // Scoped to the toggle button: the row holds three conditional
    // animate-spin SVGs (trigger/toggle/delete) and this test is about the
    // toggle's own affordance.
    await expect(button.locator('svg.animate-spin')).toBeVisible({ timeout: 10000 })
    await expect(button).toBeDisabled()

    release()

    await expect(button.locator('svg.animate-spin')).toHaveCount(0, { timeout: 20000 })
    await expect(button).toBeEnabled()
  })

  /**
   * T3 — declared regression guard for AC #4's mount clause. Entering the tab
   * is a genuine remount (AgentDetail gates the panel behind a real `v-if`),
   * so `schedules` is empty and the panel-wide spinner MUST still show.
   */
  test('@interactive the panel spinner still shows on a genuine first load (AC #4, mount)', async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 800 })

    let release
    const held = new Promise((resolve) => {
      release = resolve
    })
    // EXACT pathname equality + a method check. A glob or `endsWith` would also
    // catch `POST .../schedules` (create shares the pathname),
    // `GET .../schedules/analytics-summary`, `.../{id}/executions` and
    // `.../{id}/webhook`.
    await page.route(
      (url) => url.pathname === `/api/agents/${AGENT_A}/schedules`,
      async (route) => {
        if (route.request().method() !== 'GET') return route.continue()
        await held
        await route.continue()
      }
    )

    await page.goto(`/agents/${AGENT_A}`)
    await openSchedulesTab(page)

    await expect(page.getByText('Loading schedules...')).toBeVisible({ timeout: 15000 })
    await expect(page.getByTestId('schedule-row')).toHaveCount(0)

    release()

    await expect(page.getByText('Loading schedules...')).toHaveCount(0, { timeout: 20000 })
    await expect(page.getByTestId('schedule-row').first()).toBeVisible({ timeout: 20000 })
  })

  /**
   * T4 — RED with the fix applied but the watcher clear removed. Without the
   * clear, agent A's rows are still in `schedules`, so `schedules.length !== 0`
   * suppresses the spinner AND agent A's rows render under agent B's header.
   */
  test('@interactive switching agents in-app still shows the panel spinner (AC #4, switch)', async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 800 })
    await forceDashboardListMode(page)

    // Hold agent B's schedules GET open so the transient state is observable.
    let release
    const held = new Promise((resolve) => {
      release = resolve
    })
    await page.route(
      (url) => url.pathname === `/api/agents/${AGENT_B}/schedules`,
      async (route) => {
        if (route.request().method() !== 'GET') return route.continue()
        await held
        await route.continue()
      }
    )

    await page.goto(`/agents/${AGENT_A}?tab=schedules`)
    await waitForListSettled(page)

    await switchAgentInSpa(page, AGENT_B)

    // The spinner must be back...
    await expect(page.getByText('Loading schedules...')).toBeVisible({ timeout: 20000 })
    // ...and agent A's rows must be gone, not lingering under B's header.
    await expect(page.locator(`[data-testid="schedule-row"]:has-text("${FIXTURE_PREFIX}")`)).toHaveCount(0)

    release()
    await expect(page.getByText('Loading schedules...')).toHaveCount(0, { timeout: 20000 })
    // B never had fixture rows seeded.
    await expect(page.locator(`[data-testid="schedule-row"]:has-text("${FIXTURE_PREFIX}")`)).toHaveCount(0)
  })

  /**
   * T5 — RED with the fix + watcher clear applied but the `loadSeq` guard
   * removed. Clearing the array cannot stop a LATE response: A's GET resolves
   * after B's and repaints A's rows under B's header.
   */
  test('@interactive a superseded schedules load never paints over the newer agent', async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 800 })
    await forceDashboardListMode(page)

    // A's GET is gated on an explicit release, NOT a wall-clock delay. A fixed
    // sleep only *probably* outlives the Dashboard→B click-through; if the
    // click-through is slower, A resolves while it is still the newest load, B's
    // watcher then reloads cleanly, and the test goes green with the `loadSeq`
    // guard deleted — the same tautology that sank the first draft of T4.
    // Releasing A only after B's load has visibly finished makes "A resolves
    // last" a certainty rather than a timing bet.
    let releaseA
    const heldA = new Promise((resolve) => {
      releaseA = resolve
    })
    await page.route(
      (url) => url.pathname === `/api/agents/${AGENT_A}/schedules`,
      async (route) => {
        if (route.request().method() !== 'GET') return route.continue()
        await heldA
        await route.continue()
      }
    )

    // Land on A and switch away WHILE A's GET is still in flight.
    await page.goto(`/agents/${AGENT_A}?tab=schedules`)
    await expect(page.getByText('Loading schedules...')).toBeVisible({ timeout: 15000 })

    await switchAgentInSpa(page, AGENT_B)

    // B's load owns the panel and has completed.
    await expect(page.getByText('Loading schedules...')).toHaveCount(0, { timeout: 20000 })
    await expect(page.locator(`[data-testid="schedule-row"]:has-text("${FIXTURE_PREFIX}")`)).toHaveCount(0)

    // NOW let A's superseded response land. Without the guard it writes A's rows
    // into `schedules` under B's header.
    releaseA()
    await page.waitForTimeout(1500)

    await expect(page).toHaveURL(new RegExp(`/agents/${AGENT_B}(\\?|$|/)`))
    await expect(page.locator(`[data-testid="schedule-row"]:has-text("${FIXTURE_PREFIX}")`)).toHaveCount(0)
    // The late response must not have cleared a flag B's load owns, nor left the
    // panel stuck loading.
    await expect(page.getByText('Loading schedules...')).toHaveCount(0)
  })
})
