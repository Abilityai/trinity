import { test, expect, request } from '@playwright/test'
import { agentExists, missingAgentReason } from './helpers/agent-probe.js'

/**
 * "Continue as Chat" (EXEC-023) e2e — #1672.
 *
 * Guards the root-cause regression: AgentDetail is KeepAlive-cached (App.vue), so
 * the COMMON path into Continue-as-Chat — open an agent (caches the component),
 * click into one of its executions, then "Continue as Chat" back to AgentDetail —
 * re-activates the cached instance via onActivated, NOT a fresh onMounted. The
 * original code applied the deep-link landing ONLY in onMounted, so on this path
 * the resume id was silently dropped and the chat "worked" only after a hard page
 * reload. This spec reproduces that exact navigation and asserts the resuming
 * ChatPanel + resume banner.
 *
 * ent#358 note: the session-mode fork this spec used to have to defeat is gone —
 * continuous conversation moved to the Workspace, so the Chat tab renders one
 * surface and there is no saved mode preference left to protect. The KeepAlive
 * regression itself is unchanged and still worth pinning.
 *
 * !!! LOCAL EXECUTION NOTE !!!
 * @interactive, NOT @smoke: this drives a real task execution (a live Claude call,
 * 10-60s) against a running agent. It runs in CI
 * on `ui`-labeled PRs and via `npm run test:e2e -- continue-as-chat.spec`. It was
 * authored against a worktree WITHOUT a live stack and has NOT been executed locally
 * — validate in CI or against a real stack before relying on it.
 *
 * Required env: ADMIN_PASSWORD (auth.setup.js) + SESSION_TEST_AGENT (default
 * "testfix"); the agent must exist, be running, and use the Claude runtime.
 *
 * A MISSING fixture agent reads as SKIPPED, never broken (#2199). Note the
 * probe only proves EXISTENCE — if the agent exists but is stopped or on a
 * non-Claude runtime, the failures below are real and must not be read as
 * "fixture absent".
 */

const TEST_AGENT = process.env.SESSION_TEST_AGENT || 'testfix'
const FLAG_KEY = 'session_tab_enabled'

let api
let token
let priorFlag
// `flagTouched` gates the restore: without it, a failure BEFORE the flag was
// ever written leaves priorFlag === undefined, and afterAll's `=== null` test
// is false, so it PUT `{ value: undefined }` onto a fleet-wide platform
// setting (#2199).
let flagTouched = false
let agentReady = false
let skipReason = ''

test.beforeAll(async ({ baseURL }) => {
  api = await request.newContext({ baseURL })
  const loginResp = await api.post('/api/token', {
    form: { username: 'admin', password: process.env.ADMIN_PASSWORD || '' },
  })
  if (!loginResp.ok()) {
    // Environment gap, not a product failure — skip rather than throw (#2199).
    skipReason = `admin login failed (${loginResp.status()}) — check ADMIN_PASSWORD`
    return
  }
  token = (await loginResp.json()).access_token

  // Probe BEFORE mutating the platform flag, and `return` on failure. Setting
  // a flag alone does NOT abort beforeAll: without this early return execution
  // falls straight through and flips a FLEET-WIDE setting for a run that is
  // guaranteed to skip (#2199).
  if (!(await agentExists(TEST_AGENT, { baseURL, token }))) {
    skipReason = missingAgentReason(TEST_AGENT, 'SESSION_TEST_AGENT')
    return
  }
  agentReady = true

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
  flagTouched = true
})

test.afterAll(async () => {
  if (!api) return
  // Restore ONLY what we actually flipped.
  if (flagTouched) {
    const headers = { Authorization: `Bearer ${token}` }
    if (priorFlag === null) {
      await api.delete(`/api/settings/${FLAG_KEY}`, { headers })
    } else {
      await api.put(`/api/settings/${FLAG_KEY}`, { headers, data: { value: priorFlag } })
    }
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
  test.beforeEach(() => {
    test.skip(!agentReady, skipReason)
  })

  test('@interactive KeepAlive navigation lands in legacy chat with the resume banner', async ({ page }) => {
    const execution = await createResumableExecution()

    // 1. Visit the agent first — this MOUNTS and KeepAlive-caches AgentDetail, so
    //    the later return navigation goes through onActivated (the regressed path).
    await page.goto(`/agents/${TEST_AGENT}?tab=overview`)
    await expect(page.getByRole('button', { name: 'Tasks' })).toBeVisible({ timeout: 15000 })

    // 2. Open the execution detail, then click Continue as Chat.
    await page.goto(`/agents/${TEST_AGENT}/executions/${execution.id}`)
    const continueBtn = page.getByRole('button', { name: 'Continue as Chat' })
    await expect(continueBtn).toBeVisible({ timeout: 15000 })
    await continueBtn.click()

    // 3. We are back on AgentDetail via onActivated (cached instance) and the
    //    resuming ChatPanel renders. ent#358 removed the session-mode fork this
    //    landing used to have to force its way past, so the assertion is now
    //    simply "the resume landed": the New Chat affordance plus the banner.
    await expect(page.getByRole('button', { name: /New Chat/ })).toBeVisible({ timeout: 15000 })
    // Resume banner copy (ChatPanel.vue): "Continuing from execution <id>...".
    await expect(page.getByText(/Continuing from execution/i)).toBeVisible({ timeout: 10000 })
  })
})
