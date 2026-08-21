import { test, expect, request } from '@playwright/test'
import { agentExists, missingAgentReason, tokenFromStorageState } from './helpers/agent-probe.js'

/**
 * Background refresh is invisible (#1927, design-system p13/p14/p15/p5).
 *
 * The defect: four surfaces gated their loading UI on "a fetch is in flight",
 * so every background poll swapped rendered content for a spinner / "Loading…"
 * / a placeholder — content → spinner → content, every 10–15 s — and one
 * surface re-expanded a card the operator had deliberately collapsed on every
 * poll delta. The fix gates on "no data yet" and keeps data on screen through
 * a refresh, including a FAILED refresh (stale banner, never a spinner, never
 * the empty copy).
 *
 * HOW THESE TESTS SEE A RE-FLASH DETERMINISTICALLY. A mutation-observer
 * "strobe counter" is not reliable (a legitimate in-place value swap mutates
 * text nodes too, and a one-frame flash can slip past a count). Instead each
 * test HOLDS the poll request open at the network boundary (`page.route` with a
 * deferred fulfil), so "the fetch is in flight" is a steady state that can be
 * asserted against: the loading copy has count 0 AND the element that rendered
 * the data before the poll is the SAME node (`isConnected`) — a bare
 * `v-if="loading"` unmounts it for the entire hold. Polls are driven with
 * `page.clock` (`install` before navigation, `runFor` to fire the interval),
 * never wall-clock waits.
 *
 * The second half of every surface: abort the poll AFTER a good load — the
 * data must stay, the stale banner (`inline-error`) must appear, and neither
 * the loading copy nor the failed block nor the empty copy may render.
 *
 * CI scope. `@smoke` tests run on the zero-agent CI stack because they feed the
 * surface synthetic payloads via `route.fulfill` (Operations, /m). The
 * Schedules and Info tests need a real, non-system agent and are local-only
 * (`@interactive`), guarded with the shared authenticated probe so a missing
 * fixture SKIPS with a named reason while a broken probe FAILS loud.
 */

// ---------------------------------------------------------------- helpers

/** A deferred fulfil: the route stays pending until `release()` is called. */
function holdable() {
  let release
  const held = new Promise((r) => { release = r })
  return { held, release }
}

function queueItem(i) {
  const now = new Date().toISOString()
  return {
    id: `e2e-1927-${i}`,
    agent_name: 'e2e-agent',
    type: 'question',
    status: 'pending',
    priority: 'medium',
    title: `Question ${i} from e2e-agent`,
    question: `Is this item ${i} still on screen?`,
    options: null,
    context: {},
    execution_id: null,
    created_at: now,
    expires_at: null,
    response: null,
    response_text: null,
    responded_by_email: null,
    responded_at: null,
    acknowledged_at: null,
  }
}

const queuePayload = (n) => ({ items: Array.from({ length: n }, (_, i) => queueItem(i + 1)), count: n })

/**
 * Serve `first` on the first matching request, then HOLD every later request
 * until `release()`; after release, serve `later` (defaults to `first`).
 * Returns the release function and a counter.
 */
async function routeFirstThenHold(page, matcher, first, later = first) {
  const { held, release } = holdable()
  const calls = { n: 0 }
  await page.route(matcher, async (route) => {
    calls.n += 1
    if (calls.n === 1) return route.fulfill({ json: first })
    await held
    return route.fulfill({ json: later })
  })
  return { release, calls }
}

/** Serve `first` once, then fail every later request at the transport. */
async function routeFirstThenAbort(page, matcher, first) {
  const calls = { n: 0 }
  await page.route(matcher, async (route) => {
    calls.n += 1
    if (calls.n === 1) return route.fulfill({ json: first })
    return route.abort('failed')
  })
  return calls
}

const isPath = (pathname) => (url) => new URL(url).pathname === pathname
const startsWithPath = (prefix) => (url) => new URL(url).pathname.startsWith(prefix)

// ================================================================ Operations

test.describe('Operations — Needs Response feed (#1927)', () => {
  test('@smoke a background poll swaps values in place — no re-flash, no unmount', async ({ page }) => {
    await page.clock.install()
    const { release } = await routeFirstThenHold(page, startsWithPath('/api/operator-queue'), queuePayload(2))

    await page.goto('/operations?tab=needs-response')
    const cards = page.getByTestId('queue-card')
    await expect(cards).toHaveCount(2, { timeout: 15000 })
    const firstCardBefore = await cards.first().elementHandle()

    // Fire the 10 s container-level poll; the response is now held open.
    await page.clock.runFor(10_500)

    // Steady state "fetch in flight": the feed must be untouched.
    await expect(page.getByText('Checking the queue…')).toHaveCount(0)
    await expect(page.getByTestId('load-failed')).toHaveCount(0)
    await expect(cards).toHaveCount(2)
    expect(await firstCardBefore.evaluate((el) => el.isConnected)).toBe(true)

    release()
    await expect(cards).toHaveCount(2)
    expect(await firstCardBefore.evaluate((el) => el.isConnected)).toBe(true)
    await expect(page.getByText('Checking the queue…')).toHaveCount(0)
  })

  test('@smoke a collapsed card stays collapsed across a poll that adds an item (principle 5)', async ({ page }) => {
    await page.clock.install()
    let n = 0
    await page.route(startsWithPath('/api/operator-queue'), (route) => {
      n += 1
      // First poll: 2 items. Every later poll: 3 items (a delta).
      return route.fulfill({ json: queuePayload(n === 1 ? 2 : 3) })
    })

    await page.goto('/operations?tab=needs-response')
    const cards = page.getByTestId('queue-card')
    await expect(cards).toHaveCount(2, { timeout: 15000 })
    // Landing: the first item is auto-expanded.
    await expect(cards.first()).toHaveAttribute('aria-expanded', 'true')

    // The operator collapses it on purpose.
    await cards.first().getByRole('button', { name: 'Collapse' }).click()
    await expect(cards.first()).toHaveAttribute('aria-expanded', 'false')

    // A poll delta arrives (2 → 3). Before the fix this re-expanded the first card.
    await page.clock.runFor(10_500)
    await expect(cards).toHaveCount(3)
    for (let i = 0; i < 3; i++) {
      await expect(cards.nth(i)).toHaveAttribute('aria-expanded', 'false')
    }
  })

  test('@smoke a failed refresh keeps the cards and says so — no spinner, no "All caught up"', async ({ page }) => {
    await page.clock.install()
    await routeFirstThenAbort(page, startsWithPath('/api/operator-queue'), queuePayload(2))

    await page.goto('/operations?tab=needs-response')
    const cards = page.getByTestId('queue-card')
    await expect(cards).toHaveCount(2, { timeout: 15000 })

    await page.clock.runFor(10_500)

    const banner = page.getByTestId('inline-error')
    await expect(banner).toBeVisible()
    await expect(banner).toContainText(/couldn't refresh the queue/i)
    await expect(cards).toHaveCount(2)
    await expect(page.getByText('Checking the queue…')).toHaveCount(0)
    await expect(page.getByTestId('load-failed')).toHaveCount(0)
    await expect(page.getByText('All caught up')).toHaveCount(0)
  })
})

// ================================================================ /m (MobileAdmin)

test.describe('/m mobile admin (#1927)', () => {
  // mobile-sw.js registers a fetch handler; interception must not depend on it.
  test.use({ serviceWorkers: 'block' })

  const fleet = {
    timestamp: new Date().toISOString(),
    summary: { total: 1, running: 1, stopped: 0, high_context: 0 },
    agents: [{
      name: 'e2e-agent', status: 'running', is_system: false, template: 'e2e',
      context_percent: 12, context_used: 24000, context_max: 200000,
      memory_mb: 128, cpu_percent: 1, uptime: '1h',
    }],
  }
  const autonomy = { 'e2e-agent': { autonomy_enabled: false } }
  const stats = { agents: [] }
  const notifications = { count: 0, notifications: [] }

  async function mockFleet(page) {
    await page.route(isPath('/api/agents/autonomy-status'), (r) => r.fulfill({ json: autonomy }))
    await page.route(isPath('/api/agents/execution-stats'), (r) => r.fulfill({ json: stats }))
    await page.route(isPath('/api/notifications'), (r) => r.fulfill({ json: notifications }))
  }

  test('@smoke Ops › Queue renders {items,count} (the shape it always returned) and polls in place', async ({ page }) => {
    await page.clock.install()
    await mockFleet(page)
    await page.route(isPath('/api/ops/fleet/status'), (r) => r.fulfill({ json: fleet }))
    const { release } = await routeFirstThenHold(page, startsWithPath('/api/operator-queue'), queuePayload(2))

    await page.goto('/m')
    await page.locator('nav.tab-bar button', { hasText: 'Ops' }).click()

    // Before #1927 this tab ALWAYS read "No pending items": the fetcher did
    // `(res.data || []).filter(...)` on the `{items,count}` object and threw.
    const cards = page.locator('.ops-card')
    await expect(cards).toHaveCount(2, { timeout: 15000 })
    await expect(page.getByText('No pending items')).toHaveCount(0)
    const firstBefore = await cards.first().elementHandle()

    await page.clock.runFor(15_500) // the 15 s poll; held open
    await expect(page.getByText('Loading queue...')).toHaveCount(0)
    await expect(cards).toHaveCount(2)
    expect(await firstBefore.evaluate((el) => el.isConnected)).toBe(true)

    release()
    await expect(cards).toHaveCount(2)
    expect(await firstBefore.evaluate((el) => el.isConnected)).toBe(true)
  })

  test('@smoke Ops › Queue: a failed poll keeps the cards and shows the stale banner', async ({ page }) => {
    await page.clock.install()
    await mockFleet(page)
    await page.route(isPath('/api/ops/fleet/status'), (r) => r.fulfill({ json: fleet }))
    await routeFirstThenAbort(page, startsWithPath('/api/operator-queue'), queuePayload(2))

    await page.goto('/m')
    await page.locator('nav.tab-bar button', { hasText: 'Ops' }).click()
    const cards = page.locator('.ops-card')
    await expect(cards).toHaveCount(2, { timeout: 15000 })

    await page.clock.runFor(15_500)
    const banner = page.getByTestId('inline-error')
    await expect(banner).toBeVisible()
    await expect(banner).toContainText(/couldn't refresh the queue/i)
    await expect(cards).toHaveCount(2)
    await expect(page.getByText('Loading queue...')).toHaveCount(0)
    await expect(page.getByText('No pending items')).toHaveCount(0)
  })

  test('@smoke Agents: the 15 s poll never re-flashes "Loading agents..."; a failed poll keeps the list', async ({ page }) => {
    await page.clock.install()
    await mockFleet(page)
    await page.route(startsWithPath('/api/operator-queue'), (r) => r.fulfill({ json: queuePayload(0) }))
    let n = 0
    const { held, release } = holdable()
    await page.route(isPath('/api/ops/fleet/status'), async (route) => {
      n += 1
      if (n === 1) return route.fulfill({ json: fleet })
      if (n === 2) { await held; return route.fulfill({ json: fleet }) }
      return route.abort('failed')
    })

    await page.goto('/m')
    const cards = page.locator('.agent-card')
    await expect(cards).toHaveCount(1, { timeout: 15000 })
    const before = await cards.first().elementHandle()

    // Poll #2: held open.
    await page.clock.runFor(15_500)
    await expect(page.getByText('Loading agents...')).toHaveCount(0)
    await expect(cards).toHaveCount(1)
    expect(await before.evaluate((el) => el.isConnected)).toBe(true)
    release()
    await expect(cards).toHaveCount(1)

    // Poll #3: fails. Data stays, banner appears, no spinner / failed block / empty copy.
    await page.clock.runFor(15_500)
    const banner = page.getByTestId('inline-error')
    await expect(banner).toBeVisible()
    await expect(banner).toContainText(/couldn't refresh agents/i)
    await expect(cards).toHaveCount(1)
    await expect(page.getByText('Loading agents...')).toHaveCount(0)
    await expect(page.getByText('No agents found')).toHaveCount(0)
    await expect(page.getByTestId('load-failed')).toHaveCount(0)
  })

  test('@smoke Agents: a failed FIRST load is the failed state, never "No agents found"', async ({ page }) => {
    await mockFleet(page)
    await page.route(startsWithPath('/api/operator-queue'), (r) => r.fulfill({ json: queuePayload(0) }))
    await page.route(isPath('/api/ops/fleet/status'), (r) => r.abort('failed'))

    await page.goto('/m')
    const failed = page.getByTestId('load-failed')
    await expect(failed).toBeVisible({ timeout: 15000 })
    await expect(failed).toContainText(/couldn't load agents/i)
    await expect(page.getByText('No agents found')).toHaveCount(0)
    await expect(page.getByText('Loading agents...')).toHaveCount(0)
  })

  test('@smoke System: fleet health never re-flashes on its poll (two writers, one loaded flag)', async ({ page }) => {
    await page.clock.install()
    await mockFleet(page)
    await page.route(startsWithPath('/api/operator-queue'), (r) => r.fulfill({ json: queuePayload(0) }))
    const { release } = await routeFirstThenHold(page, isPath('/api/ops/fleet/status'), fleet)

    await page.goto('/m')
    await expect(page.locator('.agent-card')).toHaveCount(1, { timeout: 15000 })
    await page.locator('nav.tab-bar button', { hasText: 'System' }).click()
    const grid = page.locator('.health-grid')
    await expect(grid).toBeVisible()
    const gridBefore = await grid.elementHandle()

    // The System tab's first poll used to show "Loading..." over real numbers
    // (fleetSummary was written by fetchAgents, but only fetchFleetHealth
    // raised the flag). Held open → the grid must stay.
    await page.clock.runFor(15_500)
    await expect(page.locator('.system-section').getByText('Loading...')).toHaveCount(0)
    await expect(grid).toBeVisible()
    expect(await gridBefore.evaluate((el) => el.isConnected)).toBe(true)
    release()
    await expect(grid).toBeVisible()
  })
})

// ================================================================ Schedules — execution history

test.describe('Schedules › execution history (#1927)', () => {
  test.describe.configure({ mode: 'serial' })

  const FAR_FUTURE_CRON = '0 4 29 2 *' // Feb 29 — never fires in any useful horizon
  const NAME = `e2e-1927-${Date.now()}`
  let api
  let token
  let agent = process.env.SCHEDULES_TEST_AGENT || ''
  let scheduleId = null

  const execs = (running) => [
    { id: 'e2e-exec-1', schedule_id: 'x', status: running ? 'running' : 'success', started_at: new Date().toISOString(), completed_at: null, duration_ms: null, message: 'e2e', triggered_by: 'schedule', cost: null },
    { id: 'e2e-exec-2', schedule_id: 'x', status: 'success', started_at: new Date(Date.now() - 60000).toISOString(), completed_at: new Date().toISOString(), duration_ms: 1200, message: 'e2e', triggered_by: 'schedule', cost: 0.01 },
  ]

  test.beforeAll(async ({ baseURL }) => {
    token = tokenFromStorageState()
    api = await request.newContext({ baseURL, extraHTTPHeaders: token ? { Authorization: `Bearer ${token}` } : {} })
    if (!agent) {
      const resp = await api.get('/api/agents')
      if (resp.ok()) {
        const list = await resp.json()
        const pick = (Array.isArray(list) ? list : []).find((a) => !a.is_system && !a.ephemeral)
        agent = pick?.name || ''
      }
    }
  })

  test.afterAll(async () => {
    if (scheduleId && agent) await api.delete(`/api/agents/${agent}/schedules/${scheduleId}`).catch(() => {})
    await api?.dispose()
  })

  test.beforeEach(async ({ baseURL }) => {
    test.skip(!agent, 'no non-system agent on this stack (set SCHEDULES_TEST_AGENT)')
    test.skip(!(await agentExists(agent, { baseURL })), missingAgentReason(agent, 'SCHEDULES_TEST_AGENT'))
  })

  test('@interactive seed a never-firing schedule', async () => {
    const resp = await api.post(`/api/agents/${agent}/schedules`, {
      data: { name: NAME, cron_expression: FAR_FUTURE_CRON, message: 'e2e #1927 — never fires', enabled: false },
    })
    expect(resp.ok(), await resp.text()).toBeTruthy()
    const body = await resp.json()
    scheduleId = body.id || body.schedule?.id
    expect(scheduleId).toBeTruthy()
  })

  test('@interactive the 10 s history poll swaps in place; a failed poll keeps the rows and shows the banner', async ({ page }) => {
    await page.clock.install()
    let n = 0
    const { held, release } = holdable()
    await page.route(startsWithPath(`/api/agents/${agent}/schedules/${scheduleId}/executions`), async (route) => {
      n += 1
      if (n === 1) return route.fulfill({ json: execs(true) }) // a `running` row arms the poll
      if (n === 2) { await held; return route.fulfill({ json: execs(true) }) }
      return route.abort('failed')
    })

    await page.goto(`/agents/${agent}?tab=schedules`)
    const row = page.getByTestId('schedule-row').filter({ hasText: NAME })
    await expect(row).toBeVisible({ timeout: 20000 })
    await row.getByRole('button', { name: /show execution history/i }).click()

    const list = row.getByTestId('executions-list')
    await expect(list).toBeVisible({ timeout: 15000 })
    const listBefore = await list.elementHandle()
    await expect(row.getByTestId('executions-loading')).toHaveCount(0)

    // Poll #2 (held): the spinner must not replace the rows.
    await page.clock.runFor(10_500)
    await expect(row.getByTestId('executions-loading')).toHaveCount(0)
    await expect(list).toBeVisible()
    expect(await listBefore.evaluate((el) => el.isConnected)).toBe(true)
    release()
    await expect(list).toBeVisible()

    // Poll #3 fails: rows stay, banner, no spinner, no "No executions yet".
    await page.clock.runFor(10_500)
    const banner = row.getByTestId('inline-error')
    await expect(banner).toBeVisible()
    await expect(banner).toContainText(/couldn't refresh executions/i)
    await expect(list).toBeVisible()
    await expect(row.getByTestId('executions-loading')).toHaveCount(0)
    await expect(row.getByText('No executions yet')).toHaveCount(0)
  })

  test('@interactive a failed FIRST history fetch is the failed state, never "No executions yet"', async ({ page }) => {
    await page.route(startsWithPath(`/api/agents/${agent}/schedules/${scheduleId}/executions`), (r) => r.abort('failed'))
    await page.goto(`/agents/${agent}?tab=schedules`)
    const row = page.getByTestId('schedule-row').filter({ hasText: NAME })
    await expect(row).toBeVisible({ timeout: 20000 })
    await row.getByRole('button', { name: /show execution history/i }).click()

    const failed = row.getByTestId('load-failed')
    await expect(failed).toBeVisible({ timeout: 15000 })
    await expect(failed).toContainText(/couldn't load executions/i)
    await expect(row.getByText('No executions yet')).toHaveCount(0)
  })
})

// ================================================================ Info tab

test.describe('Agent Info tab (#1927)', () => {
  let agent = process.env.INFO_TEST_AGENT || ''

  const templateInfo = {
    has_template: true,
    name: 'e2e-template',
    display_name: 'E2E Template 1927',
    tagline: 'never served by a real agent',
    description: 'synthetic payload for the background-refresh spec',
    use_cases: ['Say hello'],
  }

  test.beforeAll(async ({ baseURL }) => {
    if (!agent) {
      const token = tokenFromStorageState()
      const api = await request.newContext({ baseURL, extraHTTPHeaders: token ? { Authorization: `Bearer ${token}` } : {} })
      const resp = await api.get('/api/agents')
      if (resp.ok()) {
        const list = await resp.json()
        agent = (Array.isArray(list) ? list : []).find((a) => !a.is_system && !a.ephemeral)?.name || ''
      }
      await api.dispose()
    }
  })

  test.beforeEach(async ({ baseURL }) => {
    test.skip(!agent, 'no non-system agent on this stack (set INFO_TEST_AGENT)')
    test.skip(!(await agentExists(agent, { baseURL })), missingAgentReason(agent, 'INFO_TEST_AGENT'))
  })

  /**
   * Mock the agent as STOPPED and the Start verb as a no-op so clicking the
   * header toggle flips `agent.status` to 'running' in the page (the
   * lifecycle composable sets it after the POST resolves) — which is the
   * InfoPanel watcher that used to blow the rendered content away. No real
   * container is touched.
   */
  async function mockStoppedAgent(page) {
    await page.route(isPath(`/api/agents/${agent}`), async (route) => {
      const res = await route.fetch()
      const body = await res.json()
      return route.fulfill({ json: { ...body, status: 'stopped' } })
    })
    await page.route(isPath(`/api/agents/${agent}/start`), (r) => r.fulfill({ json: { message: 'e2e: mocked start' } }))
  }

  test('@interactive Start/Restart refetches template info silently — content stays on screen', async ({ page }) => {
    await mockStoppedAgent(page)
    let n = 0
    const { held, release } = holdable()
    await page.route(isPath(`/api/agents/${agent}/info`), async (route) => {
      n += 1
      if (n === 1) return route.fulfill({ json: templateInfo })
      await held
      return route.fulfill({ json: templateInfo })
    })

    await page.goto(`/agents/${agent}?tab=info`)
    const content = page.getByTestId('info-content')
    await expect(content).toBeVisible({ timeout: 20000 })
    await expect(content).toContainText('E2E Template 1927')
    const before = await content.elementHandle()

    // The watcher fires on stopped → running; the refetch is held open.
    await page.getByRole('switch', { name: /click to start/i }).click()
    await expect.poll(() => n, { timeout: 10000 }).toBe(2)
    await expect(page.getByTestId('info-loading')).toHaveCount(0)
    await expect(content).toContainText('E2E Template 1927')
    expect(await before.evaluate((el) => el.isConnected)).toBe(true)

    release()
    await expect(content).toContainText('E2E Template 1927')
    await expect(page.getByTestId('info-loading')).toHaveCount(0)
  })

  test('@interactive a failed silent refetch keeps the content and shows the stale banner', async ({ page }) => {
    await mockStoppedAgent(page)
    let n = 0
    await page.route(isPath(`/api/agents/${agent}/info`), (route) => {
      n += 1
      if (n === 1) return route.fulfill({ json: templateInfo })
      return route.abort('failed')
    })

    await page.goto(`/agents/${agent}?tab=info`)
    const content = page.getByTestId('info-content')
    await expect(content).toContainText('E2E Template 1927', { timeout: 20000 })

    await page.getByRole('switch', { name: /click to start/i }).click()
    await expect.poll(() => n, { timeout: 10000 }).toBe(2)

    const banner = page.getByTestId('inline-error')
    await expect(banner).toBeVisible()
    await expect(banner).toContainText(/couldn't refresh template info/i)
    await expect(content).toContainText('E2E Template 1927')
    await expect(page.getByTestId('info-loading')).toHaveCount(0)
    await expect(page.getByTestId('load-failed')).toHaveCount(0)
  })
})
