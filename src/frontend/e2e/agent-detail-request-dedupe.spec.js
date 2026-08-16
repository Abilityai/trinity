import { test, expect, request } from '@playwright/test'

/**
 * Agent Detail request de-duplication (#2198).
 *
 * One page load issued the same GET several times: `/api/agents/{name}` and
 * `avatar/emotions` twice (Vue fires onMounted AND onActivated on the first
 * mount of a KeepAlive'd component — `App.vue` includes 'AgentDetail'),
 * `/exists` three times and `/agent-dashboard/{name}` NINE times (four triggers
 * of `checkDashboardExists`, each running its own 3-step boot retry ladder),
 * `/info` three times, and `/playbooks` twice.
 *
 * Two things are asserted, and the second matters as much as the first:
 *
 *   1. no agent-scoped endpoint is fetched more than once per mount, EXCEPT the
 *      known pollers;
 *   2. `/activity` is STILL polled at ~5s. It appears ~4x in a 20s window and
 *      looks exactly like the bug being fixed. It is not: `useSessionActivity`
 *      clears before re-registering, and `onActivated`'s `startAllPolling()`
 *      no-ops on the initial mount because it runs before its own
 *      `await loadAgent()`, when `agent.value` is still null. AC #5 says
 *      preserve it, so a future "cleanup" that silences it must fail here.
 *
 * @smoke so it runs at all: `frontend-e2e` executes `test:e2e:smoke` only.
 * Even tagged it cannot block a merge (the workflow is advisory), which is why
 * the load-bearing assertions for this issue live in vitest and pytest — this
 * is the end-to-end confirmation, not the gate.
 *
 * Needs a RUNNING agent: `/exists`, `/info` and `/playbooks` are all gated on
 * `status === 'running'`, so on a stopped agent this would pass vacuously. It
 * skips rather than lying if the instance has none.
 */

let TEST_AGENT = process.env.TABS_TEST_AGENT || ''
let api

// Polls, not duplicates. Anything matching these is exempt from the
// "at most once" rule; everything else is not.
const POLLED = [
  /\/api\/agents\/[^/]+\/activity$/,      // 5s — AC #5, asserted separately below
  /\/api\/agents\/[^/]+\/stats$/,         // live telemetry
  /\/api\/agents\/[^/]+\/git\/status$/,   // git status poll
  /\/api\/notifications\/count$/,
  /\/api\/agents\/[^/]+\/notifications\/count$/,
]

// The boot retry ladder for a dashboard-less agent. One probe may legitimately
// issue up to 3 requests when the agent's answer is inconclusive; a SETTLED
// answer stops it at 1. Bounded, never per-trigger.
const LADDER = /\/api\/agent-dashboard\/[^/]+$/

test.beforeAll(async ({ baseURL }) => {
  api = await request.newContext({ baseURL })
  const loginResp = await api.post('/api/token', {
    form: { username: 'admin', password: process.env.ADMIN_PASSWORD || '' },
  })
  if (!loginResp.ok()) throw new Error(`Admin login failed: ${loginResp.status()}`)
  const token = (await loginResp.json()).access_token
  const listResp = await api.get('/api/agents', {
    headers: { Authorization: `Bearer ${token}` },
  })
  const body = await listResp.json()
  const agents = Array.isArray(body) ? body : body.agents || []
  const running = agents.filter((a) => a.status === 'running' && !a.is_system)
  if (TEST_AGENT && !running.some((a) => a.name === TEST_AGENT)) TEST_AGENT = ''
  if (!TEST_AGENT && running.length) TEST_AGENT = running[0].name
})

test.describe('agent detail — request de-duplication', () => {
  test('@smoke no endpoint is fetched twice on one mount', async ({ page }) => {
    test.skip(!TEST_AGENT, 'needs a running non-system agent')

    const counts = new Map()
    page.on('request', (req) => {
      const path = new URL(req.url()).pathname
      if (!path.startsWith('/api/')) return
      counts.set(path, (counts.get(path) || 0) + 1)
    })

    await page.goto(`/agents/${TEST_AGENT}`)
    // Long enough to cover the full 0s/3s/9s ladder window, so a regression
    // that reintroduces it is caught rather than merely outrun.
    await page.waitForTimeout(12000)

    const offenders = [...counts.entries()].filter(([path, n]) => {
      if (n < 2) return false
      if (POLLED.some((re) => re.test(path))) return false
      if (LADDER.test(path)) return n > 3
      return true
    })

    expect(
      offenders,
      `these endpoints were fetched more than once for a single page load: ` +
      offenders.map(([p, n]) => `${p} x${n}`).join(', ')
    ).toEqual([])
  })

  test('@smoke /activity is STILL polled — that one is correct (AC 5)', async ({ page }) => {
    test.skip(!TEST_AGENT, 'needs a running non-system agent')

    const stamps = []
    page.on('request', (req) => {
      if (/\/api\/agents\/[^/]+\/activity$/.test(new URL(req.url()).pathname)) {
        stamps.push(Date.now())
      }
    })

    await page.goto(`/agents/${TEST_AGENT}`)
    // Wait for the agent to actually LOAD before opening the measurement
    // window. Polling only starts once `agent.value` is set, so a fixed window
    // from `goto` measures load latency as much as interval — which made this
    // flake under parallel workers on a loaded instance (it counted 1 poll and
    // failed, on code where polling was fine).
    await expect(page.locator('nav.-mb-px').first()).toBeVisible({ timeout: 30000 })
    stamps.length = 0
    await page.waitForTimeout(12000)

    // Two or more in a 12s window of a LOADED page means the ~5s interval
    // survived. Deliberately not an exact count: the point is that polling
    // exists, not its precise phase.
    expect(
      stamps.length,
      '/api/agents/{name}/activity stopped polling. It looks like a duplicate ' +
      'and is not — useSessionActivity clears before re-registering. Removing ' +
      'it is a regression, not a cleanup (#2198 AC 5).'
    ).toBeGreaterThanOrEqual(2)

    // At least one gap in a plausible polling band proves an INTERVAL is
    // running, rather than a burst of start-up fetches that then stops.
    //
    // Deliberately not "every gap > 2s": several call sites legitimately
    // (re)start polling around load — the status watcher on `running`, plus
    // `startAllPolling()` — and `useSessionActivity` clears before
    // re-registering, so an extra immediate fetch tens of ms apart is
    // start-up noise, not a runaway loop. Asserting on every gap would pin
    // incidental timing this change does not control, and flake.
    const gaps = stamps.slice(1).map((t, i) => t - stamps[i])
    expect(
      gaps.some((g) => g >= 2500 && g <= 9000),
      `no ~5s interval observed between /activity requests (gaps: ${gaps.join(',')})`
    ).toBe(true)
  })
})
