import { test, expect } from '@playwright/test'

/**
 * Workspace drafts — unsent input survives switching (trinity-enterprise#657).
 *
 * HERMETIC and `@smoke`, deliberately. Drafts are entirely client-side, so
 * this needs no live agent and no model turn — and CI runs `npm run
 * test:e2e:smoke` only (`.github/workflows/frontend-e2e.yml`), so an
 * `@interactive` spec here would be a live consumer nothing ever executes
 * (the #2829 class, which is exactly what this file exists to avoid).
 *
 * It is the ONLY thing that executes the wiring: that `PortalConversation`
 * and `PortalRoom` each call `useComposerDraft`, that the shell stamps
 * `hasDraft` onto its thread list, and that the sidebar, the tab strip and the
 * row render the mark from it. The unit specs prove the rules; nothing but a
 * real mount proves the components are plugged into them — delete either
 * composable call and only this file goes red.
 *
 * What it does NOT try to see: tab widths and overflow packing (that is
 * `workspace-chat-tabs.spec.js`' business, and it needs a live fixture).
 */

const AGENT_A = 'e2e-drafts-a'
const AGENT_B = 'e2e-drafts-b'
const SESSION_A = 'sess-drafts-a'
const SESSION_A2 = 'sess-drafts-a2'
const SESSION_B = 'sess-drafts-b'
const ROOM = 'room-drafts-1'
const API = '**/api/enterprise/client-portal'

const json = (body) => ({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })

const agentCard = (name, label) => ({
  name, display_label: label, description: 'e2e fixture', availability: 'ready', playbooks: [],
})

const sessionRow = (id, agent, title, when) => ({
  id, agent_name: agent, title, created_at: when, last_message_at: when, message_count: 2,
})

async function mockWorkspace(page) {
  await page.route(`${API}/my-agents*`, (route) => route.fulfill(json({
    client_email: 'e2e@example.com',
    agents: [agentCard(AGENT_A, 'Drafts A'), agentCard(AGENT_B, 'Drafts B')],
    // #2128: the roster is the portal's only capability channel — without this
    // the room URL renders the refusal panel instead of the room.
    multi_agent_chat_available: true,
  })))
  await page.route(`${API}/sessions*`, (route) => route.fulfill(json({
    sessions: [
      sessionRow(SESSION_A, AGENT_A, 'A — first', '2026-09-03T10:00:00Z'),
      sessionRow(SESSION_A2, AGENT_A, 'A — second', '2026-09-02T10:00:00Z'),
      sessionRow(SESSION_B, AGENT_B, 'B — only', '2026-09-01T10:00:00Z'),
    ],
  })))
  await page.route(`${API}/chat-state*`, (route) => route.fulfill(json({ chats: [] })))
  await page.route(`${API}/agents/*/history*`, (route) => route.fulfill(json({
    session_id: SESSION_A, messages: [{ id: 'm1', role: 'assistant', content: 'Hello.' }],
  })))
  await page.route('**/api/rooms', (route) => route.fulfill(json({
    rooms: [{ id: ROOM, name: 'Drafts room', status: 'open', agent_names: [AGENT_A, AGENT_B], is_room: true,
              created_at: '2026-09-04T10:00:00Z', last_message_at: '2026-09-04T10:00:00Z' }],
  })))
  await page.route(`**/api/rooms/${ROOM}*`, (route) => route.fulfill(json({
    id: ROOM,
    name: 'Drafts room',
    status: 'open',
    participants: [
      { kind: 'agent', identity: AGENT_A, left_at: null },
      { kind: 'agent', identity: AGENT_B, left_at: null },
      { kind: 'client', identity: 'e2e@example.com', left_at: null },
    ],
    messages: [],
  })))
}

/** The conversation composer (the room's is the same element in its own stage). */
const composer = (page) => page.locator('main textarea').first()
const sidebarRow = (page, label) => page.getByRole('button', { name: new RegExp(label) }).first()

test.describe('Workspace drafts (trinity-enterprise#657)', () => {
  test.beforeEach(async ({ page }) => {
    await mockWorkspace(page)
    // Isolation is the per-test browser context Playwright already gives each
    // case (the shared `storageState` carries the session, never drafts).
    // Deliberately NOT an `addInitScript` clear: that runs on EVERY document
    // load, including `page.reload()`, so it would wipe the bucket the reload
    // case exists to check — and pass anyway by making it empty on both sides.
    await page.setViewportSize({ width: 1280, height: 800 })
  })

  test('@smoke a draft survives switching agents, and the agent row marks it', async ({ page }) => {
    await page.goto(`/workspace/c/${SESSION_A}`)
    await expect(composer(page)).toBeVisible()

    await composer(page).fill('half a thought for A')
    // The mark appears the moment the field holds non-whitespace text.
    await expect(page.locator('[data-testid="agent-draft"]')).toHaveCount(1)

    // Switch to the other agent: a different stage, an empty composer.
    await sidebarRow(page, 'Drafts B').click()
    await expect(composer(page)).toHaveValue('')
    // …and A's row still says it holds something.
    await expect(page.locator('[data-testid="agent-draft"]')).toHaveCount(1)

    // Back to A: the words are where they were left, and the field has focus.
    await sidebarRow(page, 'Drafts A').click()
    await expect(composer(page)).toHaveValue('half a thought for A')
    await expect(composer(page)).toBeFocused()

    // Clearing the field clears the draft and the mark.
    await composer(page).fill('')
    await expect(page.locator('[data-testid="agent-draft"]')).toHaveCount(0)
  })

  test('@smoke two conversations hold two different drafts, and a reload keeps both', async ({ page }) => {
    await page.goto(`/workspace/c/${SESSION_A}`)
    await composer(page).fill('for A')
    await sidebarRow(page, 'Drafts B').click()
    await expect(composer(page)).toHaveValue('')
    await composer(page).fill('for B')
    await expect(page.locator('[data-testid="agent-draft"]')).toHaveCount(2)

    await page.reload()
    await expect(composer(page)).toHaveValue('for B')
    await expect(page.locator('[data-testid="agent-draft"]')).toHaveCount(2)

    await sidebarRow(page, 'Drafts A').click()
    await expect(composer(page)).toHaveValue('for A')
  })

  test('@smoke a room draft comes back, and marks the room row, not its agents', async ({ page }) => {
    await page.goto(`/workspace/r/${ROOM}`)
    await expect(composer(page)).toBeVisible()
    await composer(page).fill('something for the room')

    // The room's own row carries the mark; the two participating agents' rows
    // do not — the room row is always listed, so it is the draft's own door.
    // `:visible` throughout: `OverflowTabs` renders a hidden mirror row of
    // every tab to measure widths, so an unscoped count is doubled wherever a
    // strip is on screen.
    await expect(page.locator('[data-testid="draft-mark"]:visible')).toHaveCount(1)
    await expect(page.locator('[data-testid="agent-draft"]')).toHaveCount(0)

    await sidebarRow(page, 'Drafts A').click()
    await expect(composer(page)).toHaveValue('')
    await sidebarRow(page, 'Drafts room').click()
    await expect(composer(page)).toHaveValue('something for the room')
  })

  test('@smoke a draft on one chat tab does not follow the other, and each tab is marked', async ({ page }) => {
    await page.goto(`/workspace/c/${SESSION_A}`)
    const tabs = page.locator('[data-testid="portal-chat-tabs"]')
    await expect(tabs).toBeVisible()

    await composer(page).fill('first tab words')
    await tabs.getByRole('button', { name: /A — second/ }).click()
    await expect(composer(page)).toHaveValue('')
    await composer(page).fill('second tab words')

    // Both tabs now carry the mark (the strip renders it from the same keys).
    // `:visible` excludes the hidden mirror row, which draws every tab again.
    await expect(tabs.locator('[data-testid="draft-mark"]:visible')).toHaveCount(2)

    await tabs.getByRole('button', { name: /A — first/ }).click()
    await expect(composer(page)).toHaveValue('first tab words')
  })
})
