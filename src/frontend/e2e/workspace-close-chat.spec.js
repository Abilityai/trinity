import { test, expect } from '@playwright/test'

/**
 * ent#841 — close a chat with the × on its tab: ARCHIVE, never delete.
 * Plus the tab's unread count (the sidebar row's blue badge, on the tab).
 *
 * HERMETIC and `@smoke`: the session list and the archive endpoints are a
 * small stateful fake, so a close really changes what the next read returns.
 */

const API = '**/api/enterprise/client-portal'
const AGENT = 'e2e-close'
const json = (body) => ({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })

const row = (id, title, at, over = {}) => ({
  id, agent_name: AGENT, title, created_at: at, last_message_at: at, message_count: 2, archived_at: null, ...over,
})

async function mockWorkspace(page, { unread = {} } = {}) {
  const sessions = [
    row('main', null, '2026-09-04T10:00:00Z', { is_main: true }),
    row('c1', 'Alpha chat', '2026-09-03T10:00:00Z'),
    row('c2', 'Beta chat', '2026-09-02T10:00:00Z'),
  ]
  const calls = []
  await page.route(`${API}/my-agents*`, (r) => r.fulfill(json({
    client_email: 'e2e@example.com',
    agents: [{ name: AGENT, display_label: 'Close agent', description: 'e2e fixture', availability: 'ready', playbooks: [] }],
  })))
  await page.route(`${API}/sessions*`, (r) => r.fulfill(json({ sessions })))
  await page.route(`${API}/agents/${AGENT}/sessions/*/archive`, (r) => {
    const id = r.request().url().split('/sessions/')[1].split('/')[0]
    const s = sessions.find((x) => x.id === id)
    calls.push(`${r.request().method()} ${id}`)
    s.archived_at = r.request().method() === 'PUT' ? '2026-10-08T10:00:00Z' : null
    return r.fulfill(json({ ...s }))
  })
  await page.route(`${API}/agents/${AGENT}/sessions*`, (r) => r.fulfill(json({ sessions })))
  await page.route(`${API}/chat-state/**`, (r) => r.fulfill({ status: 204 }))
  await page.route(`${API}/chat-state*`, (r) => r.fulfill(json({
    chats: Object.entries(unread).map(([id, n]) => ({ kind: 'thread', id, starred: false, unread: n })),
  })))
  await page.route(`${API}/agents/*/history*`, (r) => r.fulfill(json({
    session_id: 'c1', messages: [{ id: 'm1', role: 'assistant', content: 'Hello.' }],
  })))
  await page.route('**/api/rooms', (r) => r.fulfill(json({ rooms: [] })))
  return { sessions, calls }
}

const strip = (page) => page.locator('[data-testid="portal-chat-tabs"] nav').first()
const tabNamed = (page, name) => strip(page).locator('button:not([data-tab-close])', { hasText: name })

test.describe('Workspace — close a chat from its tab (ent#841)', () => {
  test.beforeEach(async ({ page }) => { await page.setViewportSize({ width: 1280, height: 800 }) })

  test('@smoke × archives the open chat, moves to its neighbour, and Undo brings it back', async ({ page }) => {
    const fake = await mockWorkspace(page)
    await page.goto('/workspace/c/c1')
    await expect(tabNamed(page, 'Alpha chat')).toBeVisible()

    // Main has no ×; the others do, labelled for what they do.
    await expect(strip(page).locator('[data-tab-close="main"]')).toHaveCount(0)
    const x = strip(page).locator('[data-tab-close="c1"]')
    await expect(x).toHaveAttribute('aria-label', 'Archive chat')
    await x.click()

    await expect(tabNamed(page, 'Alpha chat')).toHaveCount(0)
    await expect(page).toHaveURL(/\/workspace\/c\/c2$/)          // the tab to the right
    expect(fake.calls).toEqual(['PUT c1'])
    // Nothing deleted: it is listed under Archived.
    await page.getByTestId('sidebar-archived-toggle').click()
    await expect(page.getByTestId('sidebar-archived')).toContainText('Alpha chat')

    // Undo restores it as an active chat and returns to it.
    await page.getByTestId('portal-undo-toast-undo').click()
    await expect(page).toHaveURL(/\/workspace\/c\/c1$/)
    await expect(tabNamed(page, 'Alpha chat')).toBeVisible()
    await expect(page.getByTestId('sidebar-archived')).toHaveCount(0)
    expect(fake.calls).toEqual(['PUT c1', 'DELETE c1'])
  })

  test('@smoke opening a chat from Archived reopens it', async ({ page }) => {
    const fake = await mockWorkspace(page)
    fake.sessions[2].archived_at = '2026-10-01T10:00:00Z'
    await page.goto('/workspace/c/c1')
    await expect(tabNamed(page, 'Beta chat')).toHaveCount(0)
    await page.getByTestId('sidebar-archived-toggle').click()
    await page.getByTestId('sidebar-archived').getByText('Beta chat').click()
    await expect(page).toHaveURL(/\/workspace\/c\/c2$/)
    await expect(tabNamed(page, 'Beta chat')).toBeVisible()
    expect(fake.calls).toEqual(['DELETE c2'])
  })

  test('@smoke a background chat with new messages shows the blue count on its tab, without resizing it', async ({ page }) => {
    await mockWorkspace(page, { unread: { c2: 3 } })
    await page.goto('/workspace/c/c1')
    const beta = tabNamed(page, 'Beta chat')
    await expect(beta).toBeVisible()
    await expect(beta).toContainText('3')
    await expect(beta).toHaveAttribute('aria-label', 'Beta chat, 3 unread')
    const badge = beta.locator('span.rounded-full').first()
    await expect(badge).toHaveClass(/bg-action-primary-700/)
    // Fixed-width strip: the badged tab is exactly as wide as its sibling.
    const [a, b] = await Promise.all([tabNamed(page, 'Alpha chat').boundingBox(), beta.boundingBox()])
    expect(Math.round(b.width)).toBe(Math.round(a.width))
    // The open chat never carries one.
    await expect(tabNamed(page, 'Alpha chat').locator('span.rounded-full')).toHaveCount(0)
  })
})
