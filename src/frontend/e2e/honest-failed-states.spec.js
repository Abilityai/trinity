import { test, expect, request } from '@playwright/test'
import {
  agentExists,
  missingAgentReason,
  tokenFromStorageState,
} from './helpers/agent-probe.js'

/**
 * Honest loading / empty / failed states (#1926, design-system p15 + p25).
 *
 * The defect these guard: a failed fetch was rendered with the EMPTY state's
 * copy ("No tasks yet", "All caught up", "No templates found"), which points
 * the user at the wrong remedy — create something, or relax — when the real
 * remedy is retry. A first-load window rendered as empty for the same reason.
 *
 * Each test forces the failure at the network boundary with `page.route`,
 * which is the only way to reach the failed branch deterministically against a
 * healthy stack. The assertions are two-sided on purpose: the failed state must
 * appear AND the empty-state copy must not — asserting only the former would
 * still pass if both rendered.
 */

const TEST_AGENT = process.env.TEST_AGENT || 'trinity-system'

/** Fail every matching request as a transport error (the audit's scenario). */
async function failRoute(page, pattern) {
  await page.route(pattern, (route) => route.abort('failed'))
}

test.describe('operator queue — failed fetch is not "All caught up" (#1926)', () => {
  test('@smoke renders the failed state and never the all-clear copy', async ({ page }) => {
    await failRoute(page, '**/api/operator-queue?**')
    await failRoute(page, '**/api/operator-queue')

    await page.goto('/operations?tab=needs-response')

    const failed = page.getByTestId('load-failed')
    await expect(failed).toBeVisible({ timeout: 15000 })
    await expect(failed).toContainText(/couldn't load the queue/i)
    // The lie the issue is about: claiming the operator has nothing to answer
    // when we simply could not ask.
    await expect(page.getByText('All caught up')).toHaveCount(0)
    // Principle 25 — a retry, not a dead end.
    await expect(failed.getByRole('button', { name: /try again/i })).toBeVisible()
  })

  test('retry re-issues the request and recovers into the real state', async ({ page }) => {
    let attempts = 0
    await page.route('**/api/operator-queue**', async (route) => {
      attempts += 1
      if (attempts === 1) return route.abort('failed')
      return route.fulfill({ json: { items: [] } })
    })

    await page.goto('/operations?tab=needs-response')
    await expect(page.getByTestId('load-failed')).toBeVisible({ timeout: 15000 })

    await page.getByTestId('load-failed').getByRole('button', { name: /try again/i }).click()

    // Now — and only now, after a SUCCEEDED fetch that returned zero — the
    // empty state is the honest answer.
    await expect(page.getByText('All caught up')).toBeVisible({ timeout: 15000 })
    await expect(page.getByTestId('load-failed')).toHaveCount(0)
  })
})

test.describe('notifications — failed fetch is not "No events yet" (#1926)', () => {
  test('renders the failed state instead of a blank body or the empty copy', async ({ page }) => {
    await failRoute(page, '**/api/notifications?**')

    await page.goto('/operations?tab=notifications')

    const failed = page.getByTestId('load-failed')
    await expect(failed).toBeVisible({ timeout: 15000 })
    await expect(failed).toContainText(/couldn't load notifications/i)
    await expect(page.getByText('No events yet')).toHaveCount(0)
  })
})

test.describe('task history — failed fetch is not "No tasks yet" (#1926)', () => {
  test.beforeEach(async ({ page, baseURL }) => {
    test.skip(
      !(await agentExists(TEST_AGENT, { baseURL })),
      missingAgentReason(TEST_AGENT, 'TEST_AGENT')
    )
  })

  test('renders the failed state and keeps the card footprint', async ({ page }) => {
    await failRoute(page, `**/api/agents/${TEST_AGENT}/executions**`)

    await page.goto(`/agents/${TEST_AGENT}?tab=tasks`)

    const card = page.getByTestId('task-history-card')
    await expect(card).toBeVisible({ timeout: 15000 })
    await expect(card.getByTestId('load-failed')).toBeVisible()
    await expect(card.getByTestId('load-failed')).toContainText(/couldn't load task history/i)
    await expect(page.getByText('No tasks yet')).toHaveCount(0)
  })
})

test.describe('schedule toggle — a failed verb is visible, not console-only (#1926)', () => {
  test.beforeEach(async ({ page, baseURL }) => {
    // Authenticated: /schedules is behind auth, so an anonymous context 401s,
    // yields [] and skips claiming "no schedules" — a FALSE diagnosis of a real
    // auth failure. Same class as the agent probe (#2199), and the same
    // contract: only definitive answers skip — a 404 (agent absent) or a real
    // empty list. A 401/5xx/transport error is a broken probe and must FAIL.
    const token = tokenFromStorageState()
    const api = await request.newContext({
      baseURL,
      extraHTTPHeaders: token ? { Authorization: `Bearer ${token}` } : {},
    })
    let schedules
    try {
      const resp = await api.get(`/api/agents/${TEST_AGENT}/schedules`)
      if (resp.status() === 404) {
        // Definitive: the agent itself is absent (uniform Invariant #8 shape).
        test.skip(true, missingAgentReason(TEST_AGENT, 'TEST_AGENT'))
      }
      if (!resp.ok()) {
        throw new Error(
          `schedules probe for '${TEST_AGENT}' broke: HTTP ${resp.status()} — ` +
            `auth/stack fault, not "no schedules"; refusing to skip (#2199)`
        )
      }
      schedules = await resp.json()
    } finally {
      await api.dispose()
    }
    test.skip(
      !Array.isArray(schedules) || schedules.length === 0,
      `TEST_AGENT '${TEST_AGENT}' has no schedules to toggle`
    )
  })

  test('a rejected enable/disable surfaces a persistent inline error', async ({ page }) => {
    await page.route(`**/api/agents/${TEST_AGENT}/schedules/*/enable`, (route) => route.abort('failed'))
    await page.route(`**/api/agents/${TEST_AGENT}/schedules/*/disable`, (route) => route.abort('failed'))

    await page.goto(`/agents/${TEST_AGENT}?tab=schedules`)
    await page.getByTitle(/enable|disable/i).first().click()

    const err = page.getByTestId('inline-error')
    await expect(err).toBeVisible({ timeout: 15000 })
    await expect(err).toContainText(/couldn't/i)

    // Principle 18: this is not a toast — it must still be here later.
    await page.waitForTimeout(4000)
    await expect(err).toBeVisible()
  })
})
