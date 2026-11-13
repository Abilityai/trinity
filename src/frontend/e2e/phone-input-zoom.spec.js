import { test, expect, request } from '@playwright/test'

/**
 * iOS Safari zooms the page in when a focused field's text is under 16px and
 * stays zoomed after it blurs. On a touch screen below `sm`, text fields,
 * selects and textareas render at 16px; on the desktop the dense text-xs /
 * text-sm controls keep their size.
 * @interactive: needs an agent for the Tasks page.
 */

let AGENT = process.env.TABS_TEST_AGENT || ''

test.beforeAll(async ({ baseURL }) => {
  if (AGENT) return
  const api = await request.newContext({ baseURL })
  const login = await api.post('/api/token', {
    form: { username: 'admin', password: process.env.ADMIN_PASSWORD || '' },
  })
  if (!login.ok()) throw new Error(`Admin login failed: ${login.status()}`)
  const token = (await login.json()).access_token
  const agents = await (await api.get('/api/agents', { headers: { Authorization: `Bearer ${token}` } })).json()
  const running = agents.find((a) => a.status === 'running' && !a.is_system)
  AGENT = (running || agents.find((a) => !a.is_system) || {}).name || ''
  await api.dispose()
})

const TEXT_CONTROLS = 'input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=range]), select, textarea'

async function smallControls(page) {
  return page.evaluate((sel) => [...document.querySelectorAll(sel)]
    .filter((el) => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden')
    .map((el) => ({ name: el.getAttribute('placeholder') || el.name || el.tagName, size: parseFloat(getComputedStyle(el).fontSize) }))
    .filter((c) => c.size < 16), TEXT_CONTROLS)
}

test.describe('on a phone', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true })
  for (const path of ['/operations?tab=executions', '/events', 'tasks']) {
    test(`text controls are at least 16px on ${path}`, async ({ page }) => {
      const url = path === 'tasks' ? `/agents/${AGENT}?tab=tasks` : path
      test.skip(path === 'tasks' && !AGENT, 'no agent on this stack')
      await page.goto(url)
      await page.waitForLoadState('networkidle').catch(() => {})
      const count = await page.locator(TEXT_CONTROLS).count()
      expect(count).toBeGreaterThan(0)
      expect(await smallControls(page)).toEqual([])
    })
  }
})

test('the desktop keeps its dense controls', async ({ page, isMobile }) => {
  test.skip(isMobile, 'desktop check')
  await page.setViewportSize({ width: 1280, height: 800 })
  await page.goto('/operations?tab=executions')
  await page.waitForLoadState('networkidle').catch(() => {})
  expect((await smallControls(page)).length).toBeGreaterThan(0)
})
