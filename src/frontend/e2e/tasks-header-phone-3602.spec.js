import { test, expect, request } from '@playwright/test'

/**
 * #3602: at phone width the Tasks tab header kept the title, the trigger
 * filter, the queue badge and Refresh on one row, so Refresh ran past the
 * panel's right edge (clipped by the card's overflow-hidden). The header now
 * wraps. Checks 390 and 320 wide, light and dark.
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

for (const width of [390, 320]) {
  for (const theme of ['light', 'dark']) {
    test(`Refresh is fully on screen at ${width}px (${theme})`, async ({ page }) => {
      test.skip(!AGENT, 'no agent on this stack')
      await page.addInitScript((t) => localStorage.setItem('trinity-theme', t), theme)
      await page.setViewportSize({ width, height: 800 })
      await page.goto(`/agents/${AGENT}?tab=tasks`)
      const refresh = page.getByRole('button', { name: /^\s*Refresh\s*$/ }).first()
      await expect(refresh).toBeVisible({ timeout: 15000 })
      const card = page.getByTestId('agent-tabs')
      const r = await refresh.boundingBox()
      const c = await card.boundingBox()
      expect(r.x).toBeGreaterThanOrEqual(c.x)
      expect(r.x + r.width).toBeLessThanOrEqual(c.x + c.width)
      // Tappable: the button's own centre is the topmost element there.
      const hit = await refresh.evaluate((el) => {
        const b = el.getBoundingClientRect()
        const top = document.elementFromPoint(b.left + b.width / 2, b.top + b.height / 2)
        return el === top || el.contains(top)
      })
      expect(hit).toBe(true)
      const docW = await page.evaluate(() => document.documentElement.scrollWidth)
      expect(docW).toBeLessThanOrEqual(width)
    })
  }
}
