import { test, expect } from '@playwright/test'

/**
 * abilityai/trinity#3356 — the FIRST click on a chat's star must stick.
 *
 * HERMETIC and `@smoke`. The defect was a race, so the spec builds the race on
 * purpose: a `GET /chat-state` is held open (issued by the 20 s poll, through
 * Playwright's clock) while the star is clicked, and released after the PUT
 * has landed — carrying the PRE-click state, exactly as a real in-flight read
 * does. Before the fix that stale answer overwrote the star: the server had
 * it, the screen did not.
 */

const API = '**/api/enterprise/client-portal'
const json = (body) => ({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })

async function mockWorkspace(page, { sessions }) {
  const stars = new Set()
  const ctl = { stars, hold: null }
  await page.route(`${API}/my-agents*`, (r) => r.fulfill(json({
    client_email: 'e2e@example.com',
    agents: [{ name: 'e2e-star', display_label: 'Star agent', description: 'e2e fixture', availability: 'ready', playbooks: [] }],
  })))
  await page.route(`${API}/sessions*`, (r) => r.fulfill(json({ sessions })))
  await page.route(`${API}/agents/*/sessions*`, (r) => r.fulfill(json({ sessions })))
  await page.route(`${API}/chat-state/**`, (r) => {
    const u = r.request().url()
    if (u.endsWith('/star')) {
      const id = decodeURIComponent(u.split('/chat-state/thread/')[1].split('/')[0])
      if (r.request().method() === 'PUT') stars.add(id); else stars.delete(id)
    }
    return r.fulfill({ status: 204 })
  })
  await page.route(`${API}/chat-state*`, async (r) => {
    // The answer is computed when the request ARRIVES — the server's state at
    // read time — and only then, optionally, held.
    const body = json({ chats: [...stars].map((id) => ({ kind: 'thread', id, starred: true, unread: 0 })) })
    if (ctl.hold) { const h = ctl.hold; ctl.hold = null; h.seen(); await h.release }
    return r.fulfill(body)
  })
  await page.route(`${API}/agents/*/history*`, (r) => r.fulfill(json({
    session_id: sessions[0].id, messages: [{ id: 'm1', role: 'assistant', content: 'Hello.' }],
  })))
  await page.route('**/api/rooms', (r) => r.fulfill(json({ rooms: [] })))
  return ctl
}

// Hold the NEXT chat-state read open; `issued` resolves once it has left.
// `release()` lets it answer and resolves only after the page has APPLIED the
// answer (response received + a frame) — asserting before that would pass on
// the broken code too, because the star is still drawn until the stale read lands.
function holdNextRead(ctl, page) {
  let release, seen
  const issued = new Promise((res) => { seen = res })
  ctl.hold = { release: new Promise((res) => { release = res }), seen }
  return {
    issued,
    release: async () => {
      const answered = page.waitForResponse((r) => r.url().includes('/chat-state') && r.request().method() === 'GET')
      release()
      await answered
      await page.evaluate(() => new Promise((res) => requestAnimationFrame(() => requestAnimationFrame(res))))
    },
  }
}

const row = (id, title, over = {}) => ({
  id, agent_name: 'e2e-star', title, created_at: '2026-09-03T10:00:00Z',
  last_message_at: '2026-09-03T10:00:00Z', message_count: 2, ...over,
})

test.describe('Workspace star — first click sticks (#3356)', () => {
  test.beforeEach(async ({ page }) => {
    await page.clock.install()
    await page.setViewportSize({ width: 1280, height: 800 })
  })

  test('@smoke header star survives a chat-state read already in flight', async ({ page }) => {
    const ctl = await mockWorkspace(page, { sessions: [row('s1', 'First chat')] })
    await page.goto('/workspace/c/s1')
    await expect(page.locator('main textarea').first()).toBeVisible()

    const read = holdNextRead(ctl, page)
    await page.clock.fastForward(21000)        // the 20 s poll issues its read
    await read.issued
    await page.locator('main button[aria-label="Star this chat"]').click()
    await expect.poll(() => [...ctl.stars]).toEqual(['s1'])
    await read.release()                       // the stale, pre-click answer lands

    await expect(page.locator('main button[aria-label="Unstar this chat"]')).toHaveCount(1)
    await expect(page.locator('aside button[aria-label="Unstar this chat"]')).toHaveCount(1)
    // …and it is saved, not only shown.
    await page.reload()
    await expect(page.locator('main button[aria-label="Unstar this chat"]')).toHaveCount(1)
  })

  test('@smoke sidebar row star survives the same race', async ({ page }) => {
    const ctl = await mockWorkspace(page, { sessions: [row('s1', 'First chat'), row('s2', 'Second chat', { last_message_at: '2026-09-02T10:00:00Z' })] })
    await page.goto('/workspace/c/s1')
    await expect(page.locator('main textarea').first()).toBeVisible()

    const read = holdNextRead(ctl, page)
    await page.clock.fastForward(21000)
    await read.issued
    const second = page.locator('aside [role="button"]', { hasText: 'Second chat' }).first()
    await second.hover()
    await second.getByRole('button', { name: 'Star this chat' }).click()
    await expect.poll(() => [...ctl.stars]).toEqual(['s2'])
    await read.release()

    await expect(page.locator('aside [role="button"]', { hasText: 'Second chat' }).first()
      .getByRole('button', { name: 'Unstar this chat' })).toBeVisible()
  })

  test('@smoke starring an unused Main lists it under Starred', async ({ page }) => {
    await mockWorkspace(page, { sessions: [row('main1', null, { is_main: true, last_message_at: null, message_count: 0 })] })
    await page.goto('/workspace/c/main1')
    await expect(page.locator('main textarea').first()).toBeVisible()
    await expect(page.locator('aside button[aria-label="Unstar this chat"]')).toHaveCount(0)

    await page.locator('main button[aria-label="Star this chat"]').click()
    await expect(page.locator('aside button[aria-label="Unstar this chat"]')).toHaveCount(1)
  })
})
