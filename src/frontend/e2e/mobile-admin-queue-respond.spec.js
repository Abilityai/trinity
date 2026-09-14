import { test, expect } from '@playwright/test'

/**
 * /m mobile admin — answering operator-queue items (#2370).
 *
 * The defect: every option tap on `/m` POSTed `{ response: 'approved',
 * response_text: <option> }` — a Deny was recorded as an approval, the tapped
 * option landed in the note, a typed answer to a question landed in the note
 * too, and the card answered on ONE tap with no note and no explicit submit
 * (desktop `QueueCard` is select → note → Send). The agent reads `response` as
 * the decision, so the field is load-bearing.
 *
 * WHAT IS PROVED, AND HOW. One method-aware `page.route` on the
 * `/api/operator-queue` prefix serves the GET from test-scoped state and
 * CAPTURES every `POST …/respond` body (the route is shared between GET and
 * POST on purpose: two routes on one prefix are last-registered-wins, and a
 * GET fixture that ignored the method would swallow the POST with a 200 and
 * make "no POST fired" pass vacuously). "An option tap sends nothing" is proved
 * by ORDER, not by a sleep: tap → the approval form is the FIRST thing that
 * happens (a race between the form appearing and any `/respond` response) →
 * then Send → `waitForResponse(/respond)` → exactly ONE captured body and it is
 * the Send's. On the pre-fix code the tap itself POSTs `{response:'approved',
 * response_text:'Deny'}` and no Send control ever renders, so the race reports
 * `posted` and the test fails with that body in the message.
 *
 * PROVED AGAINST THE PRE-FIX CODE (source stashed, spec kept): the first test
 * fails with `a tap must not answer; captured POSTs: [{"id":…,"body":
 * {"response":"approved","response_text":"Deny"}}]` — the behavioural proof;
 * the other six fail earlier, on the `data-testid` hooks that only the fixed
 * card carries (stated so nobody mistakes those for behaviour proofs).
 *
 * Mock state lives INSIDE each test (CI runs `retries: 2`, `fullyParallel`).
 * `page.clock.install()` before `goto` so the 15 s poll never refetches under
 * the assertions. `serviceWorkers: 'block'` because mobile-sw.js registers a
 * fetch handler; interception must not depend on it. `@smoke`: synthetic
 * payloads only, so the zero-agent CI stack can run it.
 */

test.describe('/m mobile admin — queue answers (#2370)', () => {
  test.use({ serviceWorkers: 'block' })

  const isPath = (pathname) => (url) => new URL(url).pathname === pathname
  const startsWithPath = (prefix) => (url) => new URL(url).pathname.startsWith(prefix)

  const fleet = {
    timestamp: new Date().toISOString(),
    summary: { total: 1, running: 1, stopped: 0, high_context: 0 },
    agents: [{
      name: 'e2e-agent', status: 'running', is_system: false, template: 'e2e',
      context_percent: 12, context_used: 24000, context_max: 200000,
      memory_mb: 128, cpu_percent: 1, uptime: '1h',
    }],
  }

  function queueItem(id, overrides = {}) {
    const now = new Date().toISOString()
    return {
      id,
      agent_name: 'e2e-agent',
      type: 'question',
      status: 'pending',
      priority: 'medium',
      title: `Title of ${id}`,
      question: `Question body of ${id}`,
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
      ...overrides,
    }
  }

  const APPROVAL = 'e2e-2370-approval'
  const QUESTION = 'e2e-2370-question'
  const ALERT = 'e2e-2370-alert'

  function freshState() {
    return {
      items: [
        queueItem(APPROVAL, { type: 'approval', options: ['Approve', 'Deny'], priority: 'high' }),
        queueItem(QUESTION, { type: 'question' }),
        queueItem(ALERT, { type: 'alert' }),
      ],
      posts: [],
      failNext: false,    // next POST → 500
      refuseNext: false,  // next POST → 409 (item left pending under us)
    }
  }

  /** The ONE method-aware handler: GET → current items, POST …/respond → capture + fulfil. */
  async function mockQueue(page, state) {
    await page.route(startsWithPath('/api/operator-queue'), async (route) => {
      const req = route.request()
      const pathname = new URL(req.url()).pathname
      if (req.method() === 'POST' && /\/respond$/.test(pathname)) {
        const id = pathname.split('/').at(-2)
        const body = req.postDataJSON()
        state.posts.push({ id, body })
        if (state.failNext) {
          state.failNext = false
          return route.fulfill({ status: 500, json: { detail: 'e2e: injected failure' } })
        }
        if (state.refuseNext) {
          state.refuseNext = false
          // The real 409: somebody else resolved it first; it is gone on refetch.
          state.items = state.items.filter((i) => i.id !== id)
          return route.fulfill({ status: 409, json: { detail: "Item is no longer pending (now 'cancelled') — response was not recorded" } })
        }
        const item = state.items.find((i) => i.id === id)
        state.items = state.items.filter((i) => i.id !== id)
        return route.fulfill({ json: { ...item, status: 'responded', ...body } })
      }
      return route.fulfill({ json: { items: state.items, count: state.items.length } })
    })
  }

  async function mockFleet(page) {
    await page.route(isPath('/api/agents/autonomy-status'), (r) => r.fulfill({ json: { 'e2e-agent': { autonomy_enabled: false } } }))
    await page.route(isPath('/api/agents/execution-stats'), (r) => r.fulfill({ json: { agents: [] } }))
    await page.route(isPath('/api/notifications'), (r) => r.fulfill({ json: { count: 0, notifications: [] } }))
    await page.route(isPath('/api/ops/fleet/status'), (r) => r.fulfill({ json: fleet }))
  }

  async function openQueue(page, state) {
    await page.clock.install()
    await mockFleet(page)
    await mockQueue(page, state)
    await page.goto('/m')
    await page.locator('nav.tab-bar button', { hasText: 'Ops' }).click()
    // `.ops-card` exists on both the fixed and the pre-fix card (the testid
    // hooks only on the fixed one), so the prove-fail run reaches the tap.
    await expect(page.locator('.ops-card')).toHaveCount(3, { timeout: 15000 })
  }

  const card = (page, id) => page.locator(`[data-testid="queue-card"][data-item-id="${id}"]`)
  const respondResponse = (page) => page.waitForResponse((r) => /\/api\/operator-queue\/.+\/respond$/.test(new URL(r.url()).pathname))

  test('@smoke approval: a tap selects, Send sends — {response: <option>, response_text: null}', async ({ page }) => {
    const state = freshState()
    await openQueue(page, state)
    // Cross-version locators on purpose: the card by its question text and the
    // option by its accessible name exist on the PRE-FIX card too, so on the
    // old code this test reaches the tap and fails on the captured POST body
    // (`{response:'approved', response_text:'Deny'}`), not on a missing hook.
    const approval = page.locator('.ops-card', { hasText: `Question body of ${APPROVAL}` })
    await expect(approval).toHaveCount(1)

    // The tap must reveal the form, never POST. Race the two outcomes.
    const deny = approval.getByRole('button', { name: 'Deny', exact: true })
    const first = await Promise.race([
      respondResponse(page).then(() => 'posted'),
      approval.getByTestId('queue-send').waitFor({ state: 'visible' }).then(() => 'form'),
      deny.click().then(() => new Promise(() => {})), // the click itself never "wins"
    ])
    expect(first, `a tap must not answer; captured POSTs: ${JSON.stringify(state.posts)}`).toBe('form')
    expect(state.posts).toHaveLength(0)

    // Type line + title render (was a blank `request_type` line, and no title).
    await expect(approval.getByTestId('queue-type')).toHaveText('Needs approval')
    await expect(approval.getByTestId('queue-title')).toHaveText(`Title of ${APPROVAL}`)

    await expect(deny).toHaveAttribute('aria-pressed', 'true')
    await expect(approval.getByTestId('queue-consequence')).toContainText('Deny')
    // p19: the safe action is focused first.
    await expect(approval.getByTestId('queue-cancel')).toBeFocused()
    const send = approval.getByTestId('queue-send')
    await expect(send).toHaveText(/Send: Deny/)

    const [res] = await Promise.all([respondResponse(page), send.click()])
    expect(res.ok()).toBe(true)
    expect(state.posts).toHaveLength(1)
    expect(state.posts[0]).toEqual({ id: APPROVAL, body: { response: 'Deny', response_text: null } })

    // The answered card is gone; the other two stay.
    await expect(approval).toHaveCount(0)
    await expect(page.getByTestId('queue-card')).toHaveCount(2)
  })

  test('@smoke approval with a note: the note rides response_text; Enter in the note does NOT send', async ({ page }) => {
    const state = freshState()
    await openQueue(page, state)
    const approval = card(page, APPROVAL)

    await approval.getByTestId('queue-option').filter({ hasText: 'Approve' }).click()
    const note = approval.getByTestId('queue-note')
    await note.fill('  not today  ')
    await note.press('Enter')
    // Send is the only path: the Enter above must not have produced a POST, so
    // after Send there is exactly one, and it is the one with the note.
    await Promise.all([respondResponse(page), approval.getByTestId('queue-send').click()])
    expect(state.posts).toHaveLength(1)
    expect(state.posts[0].body).toEqual({ response: 'Approve', response_text: 'not today' })
  })

  test('@smoke Cancel puts the option row back without sending', async ({ page }) => {
    const state = freshState()
    await openQueue(page, state)
    const approval = card(page, APPROVAL)

    const deny = approval.getByTestId('queue-option').filter({ hasText: 'Deny' })
    await deny.click()
    await expect(approval.getByTestId('queue-send')).toBeVisible()
    await approval.getByTestId('queue-cancel').click()
    await expect(approval.getByTestId('queue-send')).toHaveCount(0)
    await expect(deny).toHaveAttribute('aria-pressed', 'false')
    expect(state.posts).toHaveLength(0)
    await expect(approval).toHaveCount(1)
  })

  test('@smoke question: the typed answer IS the response, never the note', async ({ page }) => {
    const state = freshState()
    await openQueue(page, state)
    const question = card(page, QUESTION)
    await expect(question.getByTestId('queue-type')).toHaveText('Question')

    await question.getByTestId('queue-answer').fill('  yes, go  ')
    await Promise.all([respondResponse(page), question.getByTestId('queue-answer-send').click()])
    expect(state.posts).toHaveLength(1)
    expect(state.posts[0]).toEqual({ id: QUESTION, body: { response: 'yes, go', response_text: null } })
    await expect(question).toHaveCount(0)
  })

  test('@smoke alert: Got it acknowledges (desktop parity — no "Type response…" box)', async ({ page }) => {
    const state = freshState()
    await openQueue(page, state)
    const alert = card(page, ALERT)
    await expect(alert.getByTestId('queue-type')).toHaveText('Heads up')
    await expect(alert.getByTestId('queue-answer')).toHaveCount(0)

    await Promise.all([respondResponse(page), alert.getByTestId('queue-ack').click()])
    expect(state.posts).toHaveLength(1)
    expect(state.posts[0]).toEqual({ id: ALERT, body: { response: 'acknowledged', response_text: null } })
    await expect(alert).toHaveCount(0)
  })

  test('@smoke a failed send keeps the card, the selection and the note, and says so next to the control', async ({ page }) => {
    const state = freshState()
    state.failNext = true
    await openQueue(page, state)
    const approval = card(page, APPROVAL)

    const deny = approval.getByTestId('queue-option').filter({ hasText: 'Deny' })
    await deny.click()
    await approval.getByTestId('queue-note').fill('keep me')
    await Promise.all([respondResponse(page), approval.getByTestId('queue-send').click()])

    const err = approval.getByTestId('queue-respond-error')
    await expect(err).toBeVisible()
    await expect(err).toContainText(/still waiting/i)
    await expect(approval).toHaveCount(1)
    await expect(deny).toHaveAttribute('aria-pressed', 'true')
    await expect(approval.getByTestId('queue-note')).toHaveValue('keep me')
    expect(state.posts).toHaveLength(1)

    // Retry succeeds with the kept selection + note.
    await Promise.all([respondResponse(page), approval.getByTestId('queue-send').click()])
    expect(state.posts).toHaveLength(2)
    expect(state.posts[1].body).toEqual({ response: 'Deny', response_text: 'keep me' })
    await expect(approval).toHaveCount(0)
  })

  test('@smoke no longer pending (409): the answer is reported as NOT recorded and the card leaves', async ({ page }) => {
    const state = freshState()
    state.refuseNext = true
    await openQueue(page, state)
    const approval = card(page, APPROVAL)

    await approval.getByTestId('queue-option').filter({ hasText: 'Deny' }).click()
    await Promise.all([respondResponse(page), approval.getByTestId('queue-send').click()])

    const notice = page.locator('.action-error')
    await expect(notice).toBeVisible()
    await expect(notice).toContainText(/no longer pending/i)
    await expect(notice).toContainText(/not recorded/i)
    await expect(approval).toHaveCount(0)
    await expect(page.getByTestId('queue-card')).toHaveCount(2)
  })
})
