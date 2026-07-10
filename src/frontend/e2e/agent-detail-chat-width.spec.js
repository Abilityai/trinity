import { test, expect, request } from '@playwright/test'

/**
 * Agent Detail — Chat tab width parity (#954).
 *
 * Switching between the Chat tab and any other tab must NOT shift or resize the
 * content area. The Chat tab puts the page into a "fullscreen" layout mode
 * (root becomes a flex column); before #954 the `mx-auto` on <main> collapsed it
 * to content width in that mode, so the whole tab card jumped left/narrower on
 * every switch. The fix pins <main> to `w-full max-w-[1400px]` in both modes.
 *
 * This measures the tab card's bounding box (the `.rounded-lg` wrapping the tab
 * strip) on Overview vs Chat and asserts it does not move or change width, then
 * that it snaps back identically. @interactive — needs a real agent to exist.
 */

let TEST_AGENT = process.env.TABS_TEST_AGENT || ''
let api

test.beforeAll(async ({ baseURL }) => {
  api = await request.newContext({ baseURL })
  const loginResp = await api.post('/api/token', {
    form: { username: 'admin', password: process.env.ADMIN_PASSWORD || '' },
  })
  if (!loginResp.ok()) throw new Error(`Admin login failed: ${loginResp.status()}`)
  const token = (await loginResp.json()).access_token
  const listResp = await api.get('/api/agents', {
    headers: { Authorization: `Bearer ${token}` },
  })
  const body = await listResp.json()
  const agents = Array.isArray(body) ? body : body.agents || []
  const names = agents.map((a) => a.name)
  if (!TEST_AGENT || !names.includes(TEST_AGENT)) {
    if (names.length === 0) throw new Error('No agents available to test chat width')
    TEST_AGENT = names[0]
  }
})

test.afterAll(async () => {
  if (api) await api.dispose()
})

// Bounding box of the tab card (the .rounded-lg wrapping the OverflowTabs nav).
async function cardBox(page) {
  return page.evaluate(() => {
    const nav = document.querySelector('nav.-mb-px')
    if (!nav) return null
    const card = nav.closest('.rounded-lg')
    if (!card) return null
    const r = card.getBoundingClientRect()
    return { x: Math.round(r.x), w: Math.round(r.width) }
  })
}

test.describe('Agent Detail chat-tab width parity (#954)', () => {
  test('@interactive Chat tab does not shift or resize the content card', async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 1000 })
    await page.goto(`/agents/${TEST_AGENT}`)

    // Overview (default landing tab) baseline. .first(): the visible tab row
    // precedes OverflowTabs' aria-hidden measuring mirror, also a nav.-mb-px
    // (#1114 broke the bare locator with a strict-mode violation).
    await expect(page.locator('nav.-mb-px').first()).toBeVisible({ timeout: 15000 })
    await page.waitForTimeout(300)
    const overview = await cardBox(page)
    expect(overview).not.toBeNull()

    // Switch to Chat (fullscreen layout mode).
    await page.getByRole('button', { name: 'Chat', exact: true }).first().click()
    await page.waitForTimeout(500)
    const chat = await cardBox(page)

    // No horizontal shift, no width change (the #954 regression).
    expect(Math.abs(chat.x - overview.x)).toBeLessThanOrEqual(1)
    expect(Math.abs(chat.w - overview.w)).toBeLessThanOrEqual(1)

    // Snaps back identically when leaving Chat.
    await page.getByRole('button', { name: 'Overview', exact: true }).first().click()
    await page.waitForTimeout(400)
    const back = await cardBox(page)
    expect(Math.abs(back.x - overview.x)).toBeLessThanOrEqual(1)
    expect(Math.abs(back.w - overview.w)).toBeLessThanOrEqual(1)
  })
})
