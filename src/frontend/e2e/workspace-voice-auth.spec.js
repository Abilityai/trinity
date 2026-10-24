import { test, expect } from '@playwright/test'

/**
 * trinity-enterprise#595 — the Workspace voice socket, seen from the browser.
 *
 * The voice socket authenticates with a single-use, short-lived credential; no
 * session token appears in any URL. This is what the unit harness cannot see:
 * the URL the BROWSER opens and the first frame it sends. The socket is fully
 * mocked with `page.routeWebSocket` (no backend socket, no provider, no mic
 * needed for the assertions), the start route answers with a fresh ticket per
 * call, and the roster / thread reads are synthetic — the hermetic @smoke shape
 * `workspace-voice-query.spec.js` documents (CI has no user agents and an admin
 * with no email).
 *
 * First call: the server refuses the ticket → the refusal's words are on
 * screen and the call is not live. Second call: a FRESH ticket, an
 * acknowledgement the test releases itself, and no audio frame before it.
 */
const AGENT = 'e2e-voice-auth'
const API = '**/api/enterprise/client-portal'
const REFUSAL = 'The voice connection expired or could not be verified. Start a new call.'

const json = (body) => ({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
const ticketFor = (n) => `${String(n).padStart(3, '0')}${'t'.repeat(40)}` // 43 url-safe chars

async function stubWorkspace(page) {
  await page.route(`${API}/my-agents*`, (route) => route.fulfill(json({
    client_email: 'e2e@example.com',
    agents: [{ name: AGENT, display_label: 'Voice Auth', description: 'e2e fixture', availability: 'ready', playbooks: [] }],
    realtime_voice: { available: true, reason: null },
    multi_agent_chat_available: false,
  })))
  await page.route(`${API}/sessions*`, (route) => route.fulfill(json({ sessions: [] })))
  await page.route(`${API}/chat-state*`, (route) => route.fulfill(json({ chats: [] })))
  await page.route(`${API}/agents/${AGENT}/history*`, (route) => route.fulfill(json({
    agent_name: AGENT, session_id: null, messages: [],
  })))
  await page.route(`${API}/agents/${AGENT}/sessions`, (route) => {
    if (route.request().method() !== 'POST') return route.fallback()
    return route.fulfill(json({ id: 'e2e-voice-auth-session', agent_name: AGENT }))
  })
  await page.route(`**/api/agents/*/voice/*/panel`, (route) => route.fulfill(json({
    agent_name: AGENT, canvas_id: 'main', blocks: [], updated_at: null, stale: false,
  })))
}

test.describe('the Workspace voice socket credential', () => {
  test('@smoke no credential in the URL; the start ticket is the first frame; refusal has words; retry is fresh', async ({ page }) => {
    await stubWorkspace(page)
    const starts = []
    await page.route(`${API}/agents/*/voice/start`, (route) => {
      const n = starts.length + 1
      starts.push(route.request().headers()['authorization'] || null)
      return route.fulfill(json({
        voice_session_id: `vs_e2e_${n}`,
        websocket_url: `/ws/voice/vs_e2e_${n}`,
        portal_session_id: 'e2e-voice-auth-session',
        max_duration_seconds: 60,
        websocket_ticket: ticketFor(n),
        websocket_ticket_expires_in: 30,
      }))
    })

    const sockets = []
    await page.routeWebSocket(/\/ws\/voice\//, (ws) => {
      // `ack` is released by the test itself, after its pre-acknowledgement
      // assertions — never on a timer that a slow runner could outrun.
      const rec = { url: ws.url(), frames: [], acked: false, ack: null }
      sockets.push(rec)
      ws.onMessage((message) => {
        const frame = JSON.parse(String(message))
        rec.frames.push(frame)
        if (frame.type !== 'auth') return
        if (sockets.length === 1) {
          ws.send(JSON.stringify({ type: 'status', state: 'ended', reason: 'error', message: REFUSAL }))
          ws.close({ code: 4001, reason: 'Invalid or expired voice ticket' })
        } else {
          rec.ack = () => { rec.acked = true; ws.send(JSON.stringify({ type: 'auth_ok' })) }
        }
      })
    })

    await page.goto(`/workspace?agent=${AGENT}`)
    const call = page.getByTestId('portal-voice-call')
    await call.waitFor({ timeout: 20000 })
    await expect(call).toBeEnabled()

    // 1 — refused: words on screen, no live call.
    await call.click()
    await expect(page.getByText(REFUSAL)).toBeVisible({ timeout: 15000 })
    await expect(call).toHaveAttribute('aria-pressed', 'false')
    expect(sockets).toHaveLength(1)
    const first = new URL(sockets[0].url)
    expect(first.pathname).toBe('/ws/voice/vs_e2e_1')
    expect(first.search).toBe('')
    expect(sockets[0].frames[0]).toEqual({ type: 'auth', ticket: ticketFor(1) })

    // 2 — a fresh ticket; nothing but the auth frame before the acknowledgement.
    await call.click()
    await expect.poll(() => sockets.length, { timeout: 15000 }).toBe(2)
    await expect.poll(() => sockets[1].frames.length, { timeout: 15000 }).toBeGreaterThan(0)
    expect(sockets[1].frames[0]).toEqual({ type: 'auth', ticket: ticketFor(2) })
    expect(new URL(sockets[1].url).search).toBe('')
    expect(sockets[1].frames.filter((f) => f.type === 'audio')).toEqual([])
    expect(sockets[1].acked).toBe(false)
    // Held: a moment later still nothing but the auth frame, and no live call.
    await page.waitForTimeout(500)
    expect(sockets[1].frames.filter((f) => f.type !== 'auth')).toEqual([])
    expect(typeof sockets[1].ack).toBe('function')
    sockets[1].ack()
    expect(sockets[1].acked).toBe(true)

    // No socket URL carried the bearer the start requests were sent with.
    for (const bearer of starts.filter(Boolean)) {
      const token = bearer.replace(/^Bearer\s+/i, '')
      for (const s of sockets) expect(s.url).not.toContain(token)
    }
    // Leave the call (it is live, or ended with the mic's own words).
    await page.keyboard.press('Escape')
  })
})
