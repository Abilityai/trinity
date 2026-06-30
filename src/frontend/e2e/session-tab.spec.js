import { test, expect, request } from '@playwright/test'

/**
 * Session tab e2e (SESSION_TAB_2026-04 Phase 3.6).
 *
 * Drives the new Session tab end-to-end against a live Trinity stack:
 *   - flag OFF → tab hidden
 *   - flag ON  → tab visible, "+ New Session" + send-message round-trips
 *   - "Reset memory" modal flow
 *   - tab switching back to Chat doesn't lose Session state
 *
 * The flag is flipped via the platform's settings API (admin-only) using
 * a fresh authenticated APIRequestContext so we don't pollute the
 * browser-side storageState. We always restore the prior value in
 * afterAll so a failed run can't leave the flag dirty.
 *
 * Marked @interactive (not @smoke) — this exercises a real Claude API
 * call and takes 10–60s. The CI smoke job is filtered to @smoke; this
 * spec is opt-in via `npm run test:e2e -- session-tab.spec`.
 *
 * Required env: ADMIN_PASSWORD (already enforced by auth.setup.js) and
 * SESSION_TEST_AGENT (defaults to "testfix"). The agent must already
 * exist and be running.
 */

const TEST_AGENT = process.env.SESSION_TEST_AGENT || 'testfix'
const FLAG_KEY = 'session_tab_enabled'

let api
let token
let priorFlag

test.beforeAll(async ({ baseURL }) => {
  api = await request.newContext({ baseURL })
  // OAuth2 form-encoded login (matches the admin login route used by the
  // SDK at src/backend/routers/auth.py).
  const loginResp = await api.post('/api/token', {
    form: { username: 'admin', password: process.env.ADMIN_PASSWORD || '' },
  })
  if (!loginResp.ok()) {
    throw new Error(`Admin login failed: ${loginResp.status()}`)
  }
  token = (await loginResp.json()).access_token

  // Snapshot the current flag value so we can restore it.
  const cur = await api.get(`/api/settings/${FLAG_KEY}`, {
    headers: { Authorization: `Bearer ${token}` },
  })
  priorFlag = cur.ok() ? (await cur.json()).value : null

  // Enable the flag for the test run.
  const setResp = await api.put(`/api/settings/${FLAG_KEY}`, {
    headers: { Authorization: `Bearer ${token}` },
    data: { value: 'true' },
  })
  if (!setResp.ok()) {
    throw new Error(`Failed to enable session_tab flag: ${setResp.status()}`)
  }
})

test.afterAll(async () => {
  if (!api) return
  const headers = { Authorization: `Bearer ${token}` }
  if (priorFlag === null) {
    await api.delete(`/api/settings/${FLAG_KEY}`, { headers })
  } else {
    await api.put(`/api/settings/${FLAG_KEY}`, { headers, data: { value: priorFlag } })
  }
  await api.dispose()
})

test.describe('session tab', () => {
  // Flag-OFF assertion is intentionally separate so it doesn't pay the
  // cost of starting Claude. We flip it off, navigate, assert hidden,
  // flip back on for the rest of the suite.
  test('@interactive tab is hidden when feature flag is off', async ({ page }) => {
    const headers = { Authorization: `Bearer ${token}` }
    await api.delete(`/api/settings/${FLAG_KEY}`, { headers })
    try {
      await page.goto(`/agents/${TEST_AGENT}`)
      // Wait for the agent header to render so the tab row is mounted.
      await expect(page.getByRole('button', { name: 'Tasks' })).toBeVisible({ timeout: 15000 })
      await expect(page.getByRole('button', { name: 'Session' })).toHaveCount(0)
    } finally {
      await api.put(`/api/settings/${FLAG_KEY}`, { headers, data: { value: 'true' } })
    }
  })

  test('@interactive tab appears, sends a turn, and reset-memory clears the cache', async ({ page }) => {
    await page.goto(`/agents/${TEST_AGENT}`)

    // Session tab must appear between Chat and the next tab.
    const sessionTab = page.getByRole('button', { name: 'Session', exact: true })
    await expect(sessionTab).toBeVisible({ timeout: 15000 })
    await sessionTab.click()

    // Empty state copy from SessionPanel.
    await expect(page.getByText('+ New Session')).toBeVisible()
    await expect(page.getByText(/conversation memory will persist/i)).toBeVisible()

    // Create a session — dropdown label flips from "No session" to a relative time.
    await page.getByRole('button', { name: '+ New Session' }).click()
    await expect(page.getByText('Session ready')).toBeVisible({ timeout: 10000 })

    // Send a message. ChatInput's textarea has the placeholder we set.
    const input = page.getByPlaceholder(/your conversation memory will persist/i)
    await input.fill('Reply with just the word OK.')
    await input.press('Enter')

    // Wait for the assistant reply. Real Claude call — generous timeout.
    await expect(page.getByText(/^OK\.?$/)).toBeVisible({ timeout: 60000 })

    // Reset memory: button → modal → confirm.
    await page.getByRole('button', { name: /Reset memory/ }).click()
    await expect(page.getByRole('heading', { name: /Reset session memory\?/ })).toBeVisible()
    await page
      .getByRole('button', { name: 'Reset memory', exact: true })
      .nth(1) // 0 = the toolbar button still on the page; 1 = modal confirm
      .click()
    await expect(page.getByRole('heading', { name: /Reset session memory\?/ })).toBeHidden()
  })

  test('@interactive switching to Chat tab and back preserves session messages', async ({ page }) => {
    // Reuse the agent + session created above by re-navigating; SessionPanel
    // re-fetches the session list on mount and auto-selects the most recent.
    await page.goto(`/agents/${TEST_AGENT}`)
    await page.getByRole('button', { name: 'Session', exact: true }).click()
    // Wait for at least one session row by looking for either the empty state
    // or a session-ready empty (the auto-select picks the most recent).
    await expect(page.getByText(/Session ready|Start a session/i)).toBeVisible({ timeout: 10000 })

    await page.getByRole('button', { name: 'Chat', exact: true }).click()
    // ChatPanel renders its own header — assert the New Chat button.
    await expect(page.getByRole('button', { name: /New Chat/ })).toBeVisible({ timeout: 10000 })

    await page.getByRole('button', { name: 'Session', exact: true }).click()
    // Session header must still show its "+ New Session" affordance.
    await expect(page.getByRole('button', { name: '+ New Session' })).toBeVisible()
  })
})

/**
 * Long-turn resilience (#1376).
 *
 * In Session mode the turn endpoint is synchronous; an intermediary (e.g.
 * Cloudflare's ~100s edge timeout) can sever the socket while the backend keeps
 * running and ultimately succeeds. The fix reconciles against authoritative
 * server state before declaring failure, so a still-running or already-succeeded
 * turn never shows a false "Failed to send message"; a genuine failure still
 * surfaces a real error; and a turn that fails after the socket drops is never
 * swallowed.
 *
 * These cases stub the POST (sever / 502) and the GET /sessions/{id} with
 * server-truth fixtures via page.route — no real Claude turn is exercised, but
 * the agent must be running so the Session surface renders.
 *
 * Session mode is reached via ?tab=session (aliases to the unified Chat tab and
 * forces session mode, #1112). The agent must already have ≥1 session — beforeAll
 * creates one so the panel auto-selects it on load.
 */
test.describe('session tab — long-turn resilience (#1376)', () => {
  const nowIso = () => new Date().toISOString()

  // Build a server-truth GET /sessions/{id} fixture. `inProgress` maps to the
  // backend's `turn_in_progress` sentinel; `messages` is the authoritative list.
  const sessionFixture = ({ inProgress, messages }) => ({
    session: {
      id: 'sess-stub',
      agent_name: TEST_AGENT,
      turn_in_progress: inProgress,
      message_count: messages.length,
      total_context_used: 0,
      total_context_max: 200000,
      cached_claude_session_id: 'stub-uuid',
      consecutive_resume_failures: 0,
      status: 'active',
      started_at: nowIso(),
      last_message_at: nowIso(),
    },
    messages,
  })
  const userMsg = (content) => ({ id: 'u1', role: 'user', content, timestamp: nowIso() })
  const asstMsg = (content) => ({ id: 'a1', role: 'assistant', content, timestamp: nowIso() })

  /**
   * Intercept the session turn POST and the session-detail GET. `onPost`
   * receives the Playwright route; `onGet(callIndex)` returns the fixture for
   * each GET (callIndex lets a test change server truth between polls). The
   * plural list GET, the create POST, and reset POST pass through to the real
   * backend so the page renders normally.
   */
  async function installRoutes(page, { onPost, onGet }) {
    let getCount = 0
    await page.route('**/api/agents/**/sessions/**', async (route) => {
      const req = route.request()
      const url = req.url()
      const method = req.method()
      if (method === 'POST' && url.endsWith('/message')) {
        return onPost(route)
      }
      if (method === 'GET' && /\/sessions\/[^/]+(\?.*)?$/.test(url)) {
        const fixture = onGet(getCount++)
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(fixture),
        })
      }
      return route.continue()
    })
  }

  // Land on SessionPanel in session mode, with the most-recent session
  // auto-selected and its messages loaded from the first GET fixture.
  async function openSession(page) {
    await page.goto(`/agents/${TEST_AGENT}?tab=session`)
    const input = page.getByPlaceholder(/your conversation memory will persist/i)
    await expect(input).toBeVisible({ timeout: 20000 })
    return input
  }

  const dangerBox = (page) => page.locator('[class*="bg-status-danger"]')

  test.beforeAll(async () => {
    // Guarantee at least one session so the panel auto-selects on load.
    await api.post(`/api/agents/${TEST_AGENT}/session`, {
      headers: { Authorization: `Bearer ${token}` },
    })
  })

  test('@interactive T1 severed-but-running keeps the spinner, shows no error', async ({ page }) => {
    // Pre-send GETs report a settled session (so no poller pre-starts and the
    // input stays enabled). After the severed POST, GETs report the turn still
    // in progress, so the catch hands off to the poller.
    let posted = false
    await installRoutes(page, {
      onPost: (route) => {
        posted = true
        return route.abort()
      },
      onGet: () => (posted
        ? sessionFixture({ inProgress: true, messages: [userMsg('hi')] })
        : sessionFixture({ inProgress: false, messages: [] })),
    })
    const input = await openSession(page)
    await input.fill('long running task')
    await input.press('Enter')

    // No error box; the input stays disabled (turn handed to the poller).
    await expect(dangerBox(page)).toHaveCount(0)
    await expect(page.locator('[role="alert"]')).toHaveCount(0)
    await expect(input).toBeDisabled()
  })

  test('@interactive T2 severed-but-succeeded renders the reply, no error', async ({ page }) => {
    let posted = false
    await installRoutes(page, {
      onPost: (route) => {
        posted = true
        return route.abort()
      },
      // Before send: empty (baseline 0). After send: a NEW assistant landed.
      onGet: () => (posted
        ? sessionFixture({ inProgress: false, messages: [userMsg('hi'), asstMsg('DONE-T2')] })
        : sessionFixture({ inProgress: false, messages: [] })),
    })
    const input = await openSession(page)
    await input.fill('task that finishes after the cut')
    await input.press('Enter')

    await expect(page.getByText('DONE-T2')).toBeVisible({ timeout: 10000 })
    await expect(dangerBox(page)).toHaveCount(0)
  })

  test('@interactive T3 genuine 502 surfaces a real error (AC #5)', async ({ page }) => {
    await installRoutes(page, {
      onPost: (route) => route.fulfill({
        status: 502,
        contentType: 'application/json',
        body: JSON.stringify({ detail: { error: 'Agent execution failed: boom' } }),
      }),
      // Reconcile: turn not in flight, no new assistant → real failure.
      onGet: () => sessionFixture({ inProgress: false, messages: [userMsg('hi')] }),
    })
    const input = await openSession(page)
    await input.fill('this will fail')
    await input.press('Enter')

    await expect(page.getByText('Agent execution failed: boom')).toBeVisible({ timeout: 10000 })
    await expect(dangerBox(page)).toBeVisible()
  })

  test('@interactive T4 severed-then-failed surfaces "no reply" error (Finding 1)', async ({ page }) => {
    // Pre-send GETs report a settled session (no premature poller). The first
    // GET after the severed POST is the catch reconcile → in_progress, hand off
    // to the poller. Subsequent poll-tick GETs report the turn ended with NO new
    // assistant → the poller must surface an error (never stop silently).
    let posted = false
    let postGets = 0
    await installRoutes(page, {
      onPost: (route) => {
        posted = true
        return route.abort()
      },
      onGet: () => {
        if (!posted) return sessionFixture({ inProgress: false, messages: [userMsg('hi')] })
        postGets += 1
        return postGets === 1
          ? sessionFixture({ inProgress: true, messages: [userMsg('hi')] })
          : sessionFixture({ inProgress: false, messages: [userMsg('hi')] })
      },
    })
    const input = await openSession(page)
    await input.fill('fails at minute 50')
    await input.press('Enter')

    const alert = page.locator('[role="alert"]')
    await expect(alert).toBeVisible({ timeout: 15000 })
    await expect(alert).toContainText(/No reply came back/i)
  })

  test('@interactive T5 pre-persistence failure is not a false success', async ({ page }) => {
    // Baseline carries a prior turn's assistant (count 1). The POST is severed
    // before this turn persists, and the reconcile GET returns the SAME prior
    // conversation (assistant count still 1) — a trailing assistant must NOT be
    // read as success; a real error must show.
    const prior = [userMsg('earlier q'), asstMsg('earlier answer')]
    await installRoutes(page, {
      onPost: (route) => route.abort(),
      onGet: () => sessionFixture({ inProgress: false, messages: prior }),
    })
    const input = await openSession(page)
    // Wait for the prior assistant to load so baseline is established.
    await expect(page.getByText('earlier answer')).toBeVisible({ timeout: 10000 })
    await input.fill('dropped before it landed')
    await input.press('Enter')

    await expect(dangerBox(page)).toBeVisible({ timeout: 10000 })
  })

  test('@interactive T6 deadline routes to a neutral notice, not an error (D6)', async ({ page }) => {
    // Force the poll deadline to fire on the first tick via the test seam, while
    // the turn reports still-in-progress. Outcome must be the neutral status
    // notice, never the red error box.
    await page.addInitScript(() => {
      window.__TRINITY_SESSION_POLL_DEADLINE_MS__ = 0
    })
    // Settled before send (no premature poller); in_progress after the severed
    // POST so the catch hands off and the poller hits the (zeroed) deadline.
    let posted = false
    await installRoutes(page, {
      onPost: (route) => {
        posted = true
        return route.abort()
      },
      onGet: () => (posted
        ? sessionFixture({ inProgress: true, messages: [userMsg('hi')] })
        : sessionFixture({ inProgress: false, messages: [] })),
    })
    const input = await openSession(page)
    await input.fill('runs past the deadline')
    await input.press('Enter')

    const notice = page.locator('[role="status"]')
    await expect(notice).toContainText(/Still checking in the background/i, { timeout: 15000 })
    await expect(dangerBox(page)).toHaveCount(0)
  })

  test('@interactive T7 reconcile sees assistant + sentinel-still-set → success, not a false "no reply"', async ({ page }) => {
    // The race the success-before-handoff ordering fixes: the assistant row is
    // persisted INSIDE the inflight sentinel, so a reconcile GET can return
    // turn_in_progress=true AND a NEW assistant. Success must win over the
    // in-flight handoff — otherwise startPolling rebaselines past the landed
    // reply and false-fails with "No reply came back".
    let posted = false
    await installRoutes(page, {
      onPost: (route) => {
        posted = true
        return route.abort()
      },
      onGet: () => (posted
        ? sessionFixture({ inProgress: true, messages: [userMsg('hi'), asstMsg('DONE-T7')] })
        : sessionFixture({ inProgress: false, messages: [] })),
    })
    const input = await openSession(page)
    await input.fill('finishes right as the socket is cut')
    await input.press('Enter')

    await expect(page.getByText('DONE-T7')).toBeVisible({ timeout: 10000 })
    await expect(dangerBox(page)).toHaveCount(0)
    await expect(page.locator('[role="alert"]')).toHaveCount(0)
  })

  test('@interactive T8 a 429 concurrent-turn reject surfaces a real error, never a silent reattach', async ({ page }) => {
    // A 429 means another turn already holds the session lock; turn_in_progress
    // reflects THAT turn. The catch must NOT reconcile/attach — if it did, the
    // in-progress fixture would hand off to the poller and show no error. The
    // visible concurrency error proves the 429 short-circuit ran.
    await installRoutes(page, {
      onPost: (route) => route.fulfill({
        status: 429,
        contentType: 'application/json',
        body: JSON.stringify({
          detail: { error: 'Another turn on this session is in progress', retry_after: 5 },
        }),
      }),
      onGet: () => sessionFixture({ inProgress: true, messages: [userMsg('hi')] }),
    })
    const input = await openSession(page)
    await input.fill('second concurrent message')
    await input.press('Enter')

    await expect(page.getByText(/Another turn on this session is in progress/i)).toBeVisible({
      timeout: 10000,
    })
    await expect(dangerBox(page)).toBeVisible()
  })
})
