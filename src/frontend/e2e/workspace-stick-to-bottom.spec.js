import { test, expect } from '@playwright/test'

/**
 * Workspace transcripts do not yank the reader to the bottom (#2624).
 *
 * Both chat surfaces called an unconditional `scrollTop = scrollHeight` on
 * every arrival, so scrolling up to re-read an answer bought you until the next
 * message — in a room, every 3s, from any participant. The composable that
 * replaced it is unit-tested against a plain object carrying the three numbers
 * the decision reads; what a DOM-less harness structurally cannot do is prove
 * that a REAL scroll container, with a real layout engine and a real poll,
 * stays where the reader put it. That is this file's whole job, and the
 * assertions are all `scrollTop`.
 *
 * The room is the surface under test for the arrival case because its arrivals
 * are the deterministic ones: the 3s poll fetches, so a mock that answers the
 * second call with one more message IS an arrival, with no LLM and no timing
 * guesswork. The conversation surface shares the same composable — the point of
 * putting it in one place — so what is left to prove there is the property that
 * is its own (a thread opens at the bottom).
 *
 * `page.route()` mocks rather than a live stack, for the reason
 * `workspace-code-blocks.spec.js` records: CI runs against a stack with no
 * agents. The mocks are the requests the shell makes before a room paints.
 *
 * @smoke — runs on any PR touching src/frontend (#1526).
 */

const AGENT = 'e2e-stick'
const ROOM = 'room-stick-1'
const SESSION = 'sess-stick-1'
const API = '**/api/enterprise/client-portal'

const json = (body) => ({
  status: 200,
  contentType: 'application/json',
  body: JSON.stringify(body),
})

/** Enough turns that the transcript is taller than any viewport we use. */
function longTranscript(count) {
  return Array.from({ length: count }, (_, i) => ({
    seq: i + 1,
    kind: 'message',
    sender_kind: i % 2 === 0 ? 'client' : 'agent',
    sender_identity: i % 2 === 0 ? 'e2e@example.com' : AGENT,
    content: `Message ${i + 1}. ${'Filler so the bubble takes a couple of lines. '.repeat(4)}`,
    created_at: new Date(Date.now() - (count - i) * 60000).toISOString(),
  }))
}

const HISTORY = longTranscript(40)

function roomPayload(messages) {
  return {
    id: ROOM,
    name: 'Standup',
    status: 'open',
    participants: [
      { kind: 'agent', identity: AGENT, left_at: null },
      { kind: 'client', identity: 'e2e@example.com', left_at: null },
    ],
    messages,
  }
}

async function mockShell(page) {
  await page.route(`${API}/my-agents*`, (route) => route.fulfill(json({
    client_email: 'e2e@example.com',
    agents: [{
      name: AGENT,
      display_label: 'Stick',
      description: 'e2e fixture',
      availability: 'ready',
      playbooks: [],
    }],
    // The room surface does not render at all without this (#2128 — the roster
    // payload is the portal's only capability channel).
    multi_agent_chat_available: true,
  })))
  await page.route(`${API}/sessions*`, (route) => route.fulfill(json({ sessions: [] })))
  await page.route(`${API}/chat-state*`, (route) => route.fulfill(json({ chats: [] })))
  await page.route('**/api/rooms', (route) => route.fulfill(json({ rooms: [] })))
}

/**
 * The room endpoint, with an arrival under the test's control.
 *
 * `load()` sends `since=<last seq>`, so the first (full) call and the poll's
 * incremental calls are distinguishable from the query alone — no call
 * counting, which would couple the test to how many times the poll happens to
 * have fired.
 */
async function mockRoom(page, { pending }) {
  await page.route(`**/api/rooms/${ROOM}*`, (route) => {
    const since = Number(new URL(route.request().url()).searchParams.get('since') || 0)
    if (since === 0) return route.fulfill(json(roomPayload(HISTORY)))
    const fresh = pending.value.filter((m) => m.seq > since)
    return route.fulfill(json(roomPayload(fresh)))
  })
}

const transcript = (page) => page.locator('main div.overflow-y-auto').first()

/** How far the container is scrolled, right now. */
const scrollTopOf = (locator) => locator.evaluate((el) => el.scrollTop)

test.describe('Workspace stick-to-bottom (#2624)', () => {
  test.beforeEach(async ({ page }) => { await mockShell(page) })

  test('@smoke a room opens at the bottom, and an arrival while scrolled up does not move it', async ({ page }) => {
    const pending = { value: [] }
    await mockRoom(page, { pending })

    await page.setViewportSize({ width: 1280, height: 720 })
    await page.goto(`/workspace/r/${ROOM}`)

    const box = transcript(page)
    await expect(box.getByText('Message 40.', { exact: false })).toBeVisible()

    // AC: first load opens at the bottom.
    const atBottom = await box.evaluate((el) => el.scrollHeight - el.scrollTop - el.clientHeight)
    expect(atBottom).toBeLessThanOrEqual(2)

    // The reader scrolls up to re-read something.
    await box.evaluate((el) => { el.scrollTop = 0 })
    await expect.poll(() => scrollTopOf(box)).toBe(0)

    // A participant posts while they are reading.
    pending.value = [{
      seq: 41,
      kind: 'message',
      sender_kind: 'agent',
      sender_identity: AGENT,
      content: 'A reply that arrived while you were reading.',
      created_at: new Date().toISOString(),
    }]

    // It lands — the transcript grows — and the viewport does not move.
    await expect(box.getByText('A reply that arrived while you were reading.')).toBeVisible()
    expect(await scrollTopOf(box)).toBe(0)

    // AC: an honest affordance says what was missed and returns in one click.
    const jump = page.getByRole('button', { name: /new message/ })
    await expect(jump).toBeVisible()
    await expect(jump).toHaveText(/1 new message/)

    await jump.click()
    await expect.poll(async () =>
      box.evaluate((el) => el.scrollHeight - el.scrollTop - el.clientHeight)
    ).toBeLessThanOrEqual(2)
    // AC: it disappears once caught up.
    await expect(jump).toBeHidden()
  })

  test('@smoke scrolling back to the bottom re-arms following on its own', async ({ page }) => {
    const pending = { value: [] }
    await mockRoom(page, { pending })

    await page.setViewportSize({ width: 1280, height: 720 })
    await page.goto(`/workspace/r/${ROOM}`)
    const box = transcript(page)
    await expect(box.getByText('Message 40.', { exact: false })).toBeVisible()

    await box.evaluate((el) => { el.scrollTop = 0 })
    pending.value = [{
      seq: 41, kind: 'message', sender_kind: 'agent', sender_identity: AGENT,
      content: 'First arrival.', created_at: new Date().toISOString(),
    }]
    await expect(box.getByText('First arrival.')).toBeVisible()

    // Back to the bottom by hand — no button, no sticky detached state.
    await box.evaluate((el) => { el.scrollTop = el.scrollHeight })
    await expect(page.getByRole('button', { name: /new message/ })).toBeHidden()

    // The next arrival is followed again.
    pending.value = [
      ...pending.value,
      {
        seq: 42, kind: 'message', sender_kind: 'agent', sender_identity: AGENT,
        content: 'Second arrival, followed.', created_at: new Date().toISOString(),
      },
    ]
    await expect(box.getByText('Second arrival, followed.')).toBeVisible()
    await expect.poll(async () =>
      box.evaluate((el) => el.scrollHeight - el.scrollTop - el.clientHeight)
    ).toBeLessThanOrEqual(2)
  })

  test('@smoke a chat thread still opens at the bottom', async ({ page }) => {
    // The other surface. It shares the composable, so what is worth proving
    // here is the property that belongs to the load path: a thread opens on its
    // newest message, not on its oldest.
    await page.route(`${API}/sessions*`, (route) => route.fulfill(json({
      sessions: [{
        id: SESSION,
        agent_name: AGENT,
        title: 'Long thread',
        created_at: new Date().toISOString(),
        last_message_at: new Date().toISOString(),
        message_count: 40,
      }],
    })))
    await page.route(`${API}/agents/${AGENT}/history*`, (route) => route.fulfill(json({
      agent_name: AGENT,
      session_id: SESSION,
      messages: HISTORY.map((m, i) => ({
        id: `m${i + 1}`,
        role: m.sender_kind === 'agent' ? 'assistant' : 'user',
        content: m.content,
      })),
    })))

    await page.setViewportSize({ width: 1280, height: 720 })
    await page.goto(`/workspace/c/${SESSION}`)

    const box = transcript(page)
    await expect(box.getByText('Message 40.', { exact: false })).toBeVisible()
    await expect.poll(async () =>
      box.evaluate((el) => el.scrollHeight - el.scrollTop - el.clientHeight)
    ).toBeLessThanOrEqual(2)
  })
})
