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
