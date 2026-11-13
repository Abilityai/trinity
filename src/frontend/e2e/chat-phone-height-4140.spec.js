import { test, expect, request } from '@playwright/test'

/**
 * #4140: on Chat and Tasks the agent page was locked to the screen height
 * (h-screen overflow-hidden). On a phone the header card alone is about 500px
 * tall, so at 390x667 Chat's composer sat below the bottom edge with nothing
 * to scroll. Below `sm` the page now scrolls and the tab card is one screen
 * tall; from `sm` up the locked layout is unchanged.
 * @interactive: needs an agent.
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

for (const theme of ['light', 'dark']) {
  test(`the Chat composer and New Chat are reachable at 390x667 (${theme})`, async ({ page }) => {
    test.skip(!AGENT, 'no agent on this stack')
    await page.addInitScript((t) => localStorage.setItem('trinity-theme', t), theme)
    await page.setViewportSize({ width: 390, height: 667 })
    await page.goto(`/agents/${AGENT}?tab=chat`)
    const card = page.getByTestId('agent-tabs')
    await expect(card).toBeVisible({ timeout: 15000 })
    // The card is about one screen tall, not squeezed into what the header leaves.
    const cardH = (await card.boundingBox()).height
    expect(cardH).toBeGreaterThan(667 * 0.8)
    await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight))
    await page.waitForTimeout(200)
    const c = await card.boundingBox()
    expect(c.y).toBeGreaterThanOrEqual(0)
    expect(c.y + c.height).toBeLessThanOrEqual(667)
    const composer = card.locator('textarea').last()
    if (await composer.count()) {
      const t = await composer.boundingBox()
      expect(t.y).toBeGreaterThanOrEqual(0)
      expect(t.y + t.height).toBeLessThanOrEqual(667)
    }
    const newChat = card.getByRole('button', { name: /New Chat/ })
    if (await newChat.count()) {
      const n = await newChat.boundingBox()
      expect(n.x + n.width).toBeLessThanOrEqual(c.x + c.width)
    }
  })
}

test('from sm up the Chat page stays locked to the screen', async ({ page, isMobile }) => {
  test.skip(!AGENT, 'no agent on this stack')
  test.skip(isMobile, 'desktop layout check')
  await page.setViewportSize({ width: 1280, height: 800 })
  await page.goto(`/agents/${AGENT}?tab=chat`)
  await expect(page.getByTestId('agent-tabs')).toBeVisible({ timeout: 15000 })
  const docH = await page.evaluate(() => document.documentElement.scrollHeight)
  expect(docH).toBeLessThanOrEqual(800)
})
