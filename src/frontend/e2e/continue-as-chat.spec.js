import { test, expect, request } from '@playwright/test'

/**
 * "Continue as Chat" (EXEC-023) e2e — #1672.
 *
 * Guards the root-cause regression: AgentDetail is KeepAlive-cached (App.vue), so
 * the COMMON path into Continue-as-Chat — open an agent (caches the component),
 * click into one of its executions, then "Continue as Chat" back to AgentDetail —
 * re-activates the cached instance via onActivated, NOT a fresh onMounted. The
 * original code forced legacy chat mode ONLY in onMounted, so on this path
 * routeForcedMode stayed null, effectiveChatMode fell back to the default 'session'
 * mode, SessionPanel rendered instead of the legacy ChatPanel, and the resume id was
 * silently dropped — the chat "worked" only after a hard page reload. This spec
 * reproduces that exact navigation and asserts the legacy ChatPanel + resume banner.
 *
 * Also covers:
 *   - the forced-legacy landing does NOT overwrite localStorage['trinity.chatMode']
 *     (the user's saved session-mode preference survives — AC #1672).
 *
 * !!! LOCAL EXECUTION NOTE !!!
 * @interactive, NOT @smoke: this drives a real task execution (a live Claude call,
 * 10-60s) against a running agent, exactly like session-tab.spec.js. It runs in CI
 * on `ui`-labeled PRs and via `npm run test:e2e -- continue-as-chat.spec`. It was
 * authored against a worktree WITHOUT a live stack and has NOT been executed locally
 * — validate in CI or against a real stack before relying on it.
 *
 * Required env: ADMIN_PASSWORD (auth.setup.js) + SESSION_TEST_AGENT (default
 * "testfix"); the agent must exist, be running, and use the Claude runtime.
 */

const TEST_AGENT = process.env.SESSION_TEST_AGENT || 'testfix'
const FLAG_KEY = 'session_tab_enabled'
const CHAT_MODE_KEY = 'trinity.chatMode'

let api
let token
let priorFlag

test.beforeAll(async ({ baseURL }) => {
  api = await request.newContext({ baseURL })
  const loginResp = await api.post('/api/token', {
    form: { username: 'admin', password: process.env.ADMIN_PASSWORD || '' },
  })
  if (!loginResp.ok()) throw new Error(`Admin login failed: ${loginResp.status()}`)
  token = (await loginResp.json()).access_token

  // Session mode ON is the interesting case: it is the DEFAULT the resume landing
  // must override. With the flag off, Chat is always legacy and the bug can't show.
  const cur = await api.get(`/api/settings/${FLAG_KEY}`, {
    headers: { Authorization: `Bearer ${token}` },
  })
  priorFlag = cur.ok() ? (await cur.json()).value : null
  const setResp = await api.put(`/api/settings/${FLAG_KEY}`, {
    headers: { Authorization: `Bearer ${token}` },
    data: { value: 'true' },
  })
  if (!setResp.ok()) throw new Error(`Failed to enable session_tab flag: ${setResp.status()}`)
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

/** Trigger one task and poll until it terminates carrying a real claude_session_id. */
async function createResumableExecution() {
  const headers = { Authorization: `Bearer ${token}` }
  const runResp = await api.post(`/api/agents/${TEST_AGENT}/task`, {
    headers,
    data: { message: 'Reply with just the word DONE.', async_mode: true },
  })
  if (!runResp.ok()) throw new Error(`task trigger failed: ${runResp.status()}`)

  // Poll executions for a terminal row with a real (non-sentinel) session id.
  const deadline = Date.now() + 90_000
  while (Date.now() < deadline) {
    const listResp = await api.get(
      `/api/agents/${TEST_AGENT}/executions?limit=5`,
      { headers },
    )
    if (listResp.ok()) {
      const rows = (await listResp.json()).executions || (await listResp.json())
      const arr = Array.isArray(rows) ? rows : rows?.executions || []
      const done = arr.find(
        (e) =>
          e.claude_session_id &&
          !['dispatched', 'dispatched_async'].includes(e.claude_session_id) &&
          ['success', 'failed', 'completed'].includes(e.status),
      )
      if (done) return done
    }
    await new Promise((r) => setTimeout(r, 3000))
  }
  throw new Error('no resumable execution appeared within 90s')
}

test.describe('continue as chat (#1672)', () => {
  test('@interactive KeepAlive navigation lands in legacy chat with the resume banner', async ({ page }) => {
    const execution = await createResumableExecution()

    // 1. Visit the agent first — this MOUNTS and KeepAlive-caches AgentDetail, so
    //    the later return navigation goes through onActivated (the regressed path).
    await page.goto(`/agents/${TEST_AGENT}?tab=overview`)
    await expect(page.getByRole('button', { name: 'Tasks' })).toBeVisible({ timeout: 15000 })

    // Saved preference is the default 'session' (not yet written) — capture it so we
    // can assert the resume landing does not overwrite it.
    const priorPref = await page.evaluate((k) => localStorage.getItem(k), CHAT_MODE_KEY)

    // 2. Open the execution detail, then click Continue as Chat.
    await page.goto(`/agents/${TEST_AGENT}/executions/${execution.id}`)
    const continueBtn = page.getByRole('button', { name: 'Continue as Chat' })
    await expect(continueBtn).toBeVisible({ timeout: 15000 })
    await continueBtn.click()

    // 3. We are back on AgentDetail via onActivated (cached instance). The fix forces
    //    legacy ChatPanel: assert its "New Chat" affordance AND the resume banner —
    //    NOT SessionPanel's "+ New Session". Pre-fix, SessionPanel rendered here.
    await expect(page.getByRole('button', { name: /New Chat/ })).toBeVisible({ timeout: 15000 })
    // Resume banner copy (ChatPanel.vue): "Continuing from execution <id>...".
    await expect(page.getByText(/Continuing from execution/i)).toBeVisible({ timeout: 10000 })
    await expect(page.getByRole('button', { name: '+ New Session' })).toHaveCount(0)

    // 4. The forced-legacy landing must NOT persist to the saved preference.
    const afterPref = await page.evaluate((k) => localStorage.getItem(k), CHAT_MODE_KEY)
    expect(afterPref).toBe(priorPref)
  })
})
