import { test, expect, request } from '@playwright/test'

/**
 * Agent Detail — Tasks tab fills viewport height (#1500).
 *
 * The Tasks tab joins the fullscreen-tab layout (FULLSCREEN_TABS) so its
 * content flex-fills the viewport like the Chat tab, instead of capping the
 * task list at max-h-96 inside a mostly-empty page. Asserted here:
 *   (a) the task-history card never renders below the old 24rem (384px) cap
 *       (min-h-96 floor: the old max is the new minimum), and it GROWS when the
 *       viewport grows — proves the cap is gone AND the min-h-0 flex chain is
 *       intact (a broken chain yields a floor-height card at every viewport;
 *       chrome height varies per agent, so growth — not an absolute — is the
 *       robust signal);
 *   (b) the page itself does not scroll on the Tasks tab (fullscreen
 *       containment — the broken-chain symptom is the document growing to
 *       content height behind overflow-hidden);
 *   (c) width parity Overview↔Tasks↔Overview — extends the #954 protection
 *       (mx-auto collapse in fullscreen mode) to the new fullscreen tab;
 *   (d) at a short viewport the New Task composer is reachable and usable by
 *       scrolling within the panel (the panel root is the fallback scroller;
 *       header clipping at extreme heights is pre-existing Chat-mode parity).
 *
 * The card assertions are data-independent (the card renders in loading/empty/
 * populated states alike); list assertions are conditional on tasks existing.
 * @interactive — needs a real agent to exist.
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
    if (names.length === 0) throw new Error('No agents available to test tasks fill height')
    TEST_AGENT = names[0]
  }
})

test.afterAll(async () => {
  if (api) await api.dispose()
})

// Bounding box of the tab card (the .rounded-lg wrapping the OverflowTabs nav) —
// same anchor as the #954 chat-width spec.
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

async function openTasksTab(page) {
  // .first(): the visible tab row precedes OverflowTabs' aria-hidden measuring
  // mirror, which is also a nav.-mb-px (#1114).
  await expect(page.locator('nav.-mb-px').first()).toBeVisible({ timeout: 15000 })
  await page.getByRole('button', { name: 'Tasks', exact: true }).first().click()
  await expect(page.getByTestId('task-history-card')).toBeVisible({ timeout: 15000 })
  await page.waitForTimeout(300)
}

test.describe('Agent Detail tasks-tab fill height (#1500)', () => {
  test('@interactive task history fills a tall viewport past the old max-h-96 cap', async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 1000 })
    await page.goto(`/agents/${TEST_AGENT}`)
    await openTasksTab(page)

    // (a) Floor: the card never renders below the old 384px cap (minus border).
    const cardEl = page.getByTestId('task-history-card')
    const heightAt1000 = await cardEl.evaluate((el) => el.clientHeight)
    expect(heightAt1000).toBeGreaterThanOrEqual(380)

    // The list (when tasks exist) is the card's scroll region.
    const list = page.getByTestId('task-list')
    if (await list.count()) {
      const overflow = await list.evaluate((el) => getComputedStyle(el).overflowY)
      expect(overflow).toBe('auto')
    }

    // (b) Fullscreen containment: the document itself must not scroll.
    let pageScroll = await page.evaluate(() => ({
      scrollHeight: document.scrollingElement.scrollHeight,
      innerHeight: window.innerHeight,
    }))
    expect(pageScroll.scrollHeight).toBeLessThanOrEqual(pageScroll.innerHeight + 1)

    // (a') Growth: a taller viewport must hand its extra height to the card
    // (the old max-h-96 layout kept the card constant regardless of viewport).
    // Chrome height varies per agent/header state, so assert relative growth
    // with slack for the panel's fallback scroller absorbing part of the delta.
    await page.setViewportSize({ width: 1600, height: 1400 })
    await page.waitForTimeout(300)
    const heightAt1400 = await cardEl.evaluate((el) => el.clientHeight)
    // ≥150 of the 400px delta must reach the card: when the card starts at its
    // floor, part of the delta first absorbs the panel's overflow (chrome+floor
    // minus viewport) before the card can grow. A capped layout grows 0.
    expect(heightAt1400).toBeGreaterThanOrEqual(heightAt1000 + 150)

    // Still contained at the taller viewport.
    pageScroll = await page.evaluate(() => ({
      scrollHeight: document.scrollingElement.scrollHeight,
      innerHeight: window.innerHeight,
    }))
    expect(pageScroll.scrollHeight).toBeLessThanOrEqual(pageScroll.innerHeight + 1)
  })

  test('@interactive Tasks tab does not shift or resize the content card (#954 parity)', async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 1000 })
    await page.goto(`/agents/${TEST_AGENT}`)

    // Overview (default landing tab) baseline.
    await expect(page.locator('nav.-mb-px').first()).toBeVisible({ timeout: 15000 })
    await page.waitForTimeout(300)
    const overview = await cardBox(page)
    expect(overview).not.toBeNull()

    // Switch to Tasks (now a fullscreen layout tab).
    await openTasksTab(page)
    const tasks = await cardBox(page)
    expect(Math.abs(tasks.x - overview.x)).toBeLessThanOrEqual(1)
    expect(Math.abs(tasks.w - overview.w)).toBeLessThanOrEqual(1)

    // Snaps back identically when leaving Tasks.
    await page.getByRole('button', { name: 'Overview', exact: true }).first().click()
    await page.waitForTimeout(400)
    const back = await cardBox(page)
    expect(Math.abs(back.x - overview.x)).toBeLessThanOrEqual(1)
    expect(Math.abs(back.w - overview.w)).toBeLessThanOrEqual(1)
  })

  test('@interactive composer stays reachable on a short viewport via panel scroll', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 600 })
    await page.goto(`/agents/${TEST_AGENT}`)
    await openTasksTab(page)

    // The composer may start below the fold of the panel's fallback scroller;
    // scrolling it into view must work (the page itself cannot scroll).
    const composer = page.getByPlaceholder(/Enter task message/)
    await composer.scrollIntoViewIfNeeded()
    await expect(composer).toBeInViewport()

    // Still no page-level scrolling in the degraded mode.
    const pageScroll = await page.evaluate(() => ({
      scrollHeight: document.scrollingElement.scrollHeight,
      innerHeight: window.innerHeight,
    }))
    expect(pageScroll.scrollHeight).toBeLessThanOrEqual(pageScroll.innerHeight + 1)
  })
})
