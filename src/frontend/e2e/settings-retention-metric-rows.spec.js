import { test, expect } from '@playwright/test'

/**
 * Settings → Retention: the two metric-point rows (trinity-enterprise#671).
 *
 * The rows save through the managed retention endpoint, which exists only in an
 * edition that has it — CI's stack does not. So this spec is hermetic at exactly
 * three seams and live everywhere else:
 *
 *   - `GET /api/settings/retention` is fetched from the REAL backend and only
 *     the fields under test are patched in (edition, the metric window's value +
 *     source, the `quotas` block), so the rest of the payload's shape cannot
 *     drift from what the backend actually sends. The patch is STATEFUL: a
 *     successful PUT updates it, so "the row re-renders the saved value" is a
 *     real round-trip through the page's own reload, not a re-read of the input.
 *   - `GET /api/settings/feature-flags` is the real response with the
 *     `retention` entitlement added or removed.
 *   - `PUT /api/enterprise/retention/config` is recorded and answered here —
 *     including a real-shaped pydantic 422 for the out-of-bounds case, whose
 *     message the panel must show beside the form.
 *
 * What the managed endpoint itself accepts is proven on its own side (the
 * enterprise suite, including a guard that every key in
 * `utils/retentionFields.js` is one it knows). Nothing here writes a setting.
 */

const RETENTION_URL = '**/api/settings/retention'
const FLAGS_URL = '**/api/settings/feature-flags'
const MANAGED_URL = '**/api/enterprise/retention/config'

const CAP = '#retention-metrics_daily_point_cap'
const WINDOW = '#retention-metrics_retention_days'
const HEALTH = '#retention-health_check_retention_days'

async function stubRetention(page, { entitled = true, cap = 100000, capSource = 'code-default', windowDays = 365, windowSource = 'db-row', reject = null } = {}) {
  const state = { cap, capSource, windowDays, windowSource }
  const puts = []

  await page.route(FLAGS_URL, async (route) => {
    const response = await route.fetch()
    const json = await response.json()
    const others = (json.enterprise_features || []).filter((f) => f !== 'retention')
    json.enterprise_features = entitled ? [...others, 'retention'] : others
    await route.fulfill({ response, json })
  })

  await page.route(RETENTION_URL, async (route) => {
    if (route.request().method() !== 'GET') return route.continue()
    const response = await route.fetch()
    const json = await response.json()
    json.edition = entitled ? 'enterprise' : 'community'
    json.windows = { ...json.windows, metrics_retention_days: state.windowDays }
    json.sources = { ...json.sources, metrics_retention_days: state.windowSource }
    json.quotas = { metrics_daily_point_cap: { value: state.cap, source: state.capSource } }
    await route.fulfill({ response, json })
  })

  await page.route(MANAGED_URL, async (route) => {
    if (route.request().method() !== 'PUT') return route.continue()
    const body = route.request().postDataJSON()
    puts.push(body)
    if (reject) return route.fulfill({ status: 422, json: reject })
    if ('metrics_daily_point_cap' in body) state.cap = body.metrics_daily_point_cap
    if ('metrics_retention_days' in body) state.windowDays = body.metrics_retention_days
    return route.fulfill({ json: { edition: 'enterprise', ...body } })
  })

  return { puts }
}

async function openRetention(page) {
  await page.goto('/settings?tab=retention')
  await expect(page.getByRole('heading', { name: 'Data Retention' })).toBeVisible({ timeout: 15000 })
  await expect(page.locator(HEALTH)).toBeVisible({ timeout: 15000 })
}

test.describe('Settings → Retention metric rows (trinity-enterprise#671)', () => {
  test('@smoke saving the quota sends only that field and the row re-renders the saved value', async ({ page }) => {
    const { puts } = await stubRetention(page)
    await openRetention(page)

    const save = page.getByRole('button', { name: 'Save retention' })
    await expect(page.locator(CAP)).toHaveValue('100000')
    await expect(page.locator(WINDOW)).toHaveValue('365')
    // Nothing changed yet — nothing to save.
    await expect(save).toBeDisabled()

    await page.locator(CAP).fill('5000')
    await expect(save).toBeEnabled()
    await save.click()

    await expect(page.getByText('Saved — applied live.')).toBeVisible({ timeout: 10000 })
    // Only the edited field — an untouched knob's code default never becomes a stored row.
    expect(puts).toEqual([{ metrics_daily_point_cap: 5000 }])
    await expect(page.locator(CAP)).toHaveValue('5000')
    await expect(save).toBeDisabled()

    // "Saved" describes the values on screen — the next edit makes it untrue.
    await page.locator(CAP).fill('6000')
    await expect(save).toBeEnabled()
    await expect(page.getByText('Saved — applied live.')).toHaveCount(0)
  })

  test('@smoke an out-of-bounds quota is rejected by name, beside the form, and the panel stays', async ({ page }) => {
    const { puts } = await stubRetention(page, {
      reject: {
        detail: [{
          type: 'less_than_equal',
          loc: ['body', 'metrics_daily_point_cap'],
          msg: 'Input should be less than or equal to 10000000',
          input: 10000001,
          ctx: { le: 10000000 },
        }],
      },
    })
    await openRetention(page)

    await page.locator(CAP).fill('10000001')
    await page.getByRole('button', { name: 'Save retention' }).click()

    const error = page.getByTestId('inline-error')
    await expect(error).toBeVisible({ timeout: 10000 })
    await expect(error).toContainText('metrics_daily_point_cap: Input should be less than or equal to 10000000')
    // The form is still there with the operator's value, so it can be corrected.
    await expect(page.locator(CAP)).toBeVisible()
    await expect(page.locator(CAP)).toHaveValue('10000001')
    await expect(page.getByText('Saved — applied live.')).toHaveCount(0)
    expect(puts).toEqual([{ metrics_daily_point_cap: 10000001 }])
  })

  test('@smoke an env-sourced row is read-only, badged, and never sent', async ({ page }) => {
    const { puts } = await stubRetention(page, { cap: 250, capSource: 'env' })
    await openRetention(page)

    // The window is not env-sourced, so it is editable — asserted FIRST: every
    // input is also disabled while the entitlement is still loading, and the
    // quota's lock must not pass for that reason.
    await expect(page.locator(WINDOW)).toBeEnabled()
    await expect(page.locator(CAP)).toBeDisabled()
    await expect(page.locator(CAP)).toHaveValue('250')
    await expect(page.locator('label[for="retention-metrics_daily_point_cap"]').getByText('env', { exact: true })).toBeVisible()

    await page.locator(HEALTH).fill('14')
    await page.getByRole('button', { name: 'Save retention' }).click()
    await expect(page.getByText('Saved — applied live.')).toBeVisible({ timeout: 10000 })
    expect(puts).toEqual([{ health_check_retention_days: 14 }])
  })

  test('@smoke Community shows neither metric row; the sibling windows still render', async ({ page }) => {
    await stubRetention(page, { entitled: false })
    await openRetention(page)

    await expect(page.locator(CAP)).toHaveCount(0)
    await expect(page.locator(WINDOW)).toHaveCount(0)
    await expect(page.locator(HEALTH)).toBeVisible()
    await expect(page.getByRole('button', { name: 'Save retention' })).toHaveCount(0)
  })
})
