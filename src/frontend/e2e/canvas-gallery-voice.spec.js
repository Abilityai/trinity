import { test, expect } from '@playwright/test'
import {
  GALLERY,
  GALLERY_AGENT,
  adminToken,
  seedGallery,
  clearGallery,
  measureKit,
  settled,
  unclip,
} from './helpers/canvas-gallery.js'

// The canvas gallery on the Workspace VOICE column (#2583, the surface #2581
// reported): during a call the agent's canvas takes the right 60% of the
// Workspace (`PortalVoiceCanvas`, ent#534). It is the same `CanvasPanel`, but
// a different width class from the rail and Agent Detail, and it reads a
// different route, so it is measured on its own.
//
// A real call needs a Gemini session and a microphone. Only the call PLUMBING
// is mocked here — the start request, the audio socket, and the panel read —
// so the real column mounts on the real page and renders the real gallery
// rows; nothing about the layout is faked. Canvases are switched the way the
// column switches them in a call: a panel-tool `tool_result` frame on the
// socket bumps `panelVersion`, and the column refetches.

const WIDTHS = [1280, 1920]
const THEMES = ['light', 'dark']
const SHOTS = 'e2e/canvas-gallery-shots'
const KEEP = !!process.env.KEEP_GALLERY
const VOICE_SID = 'gallery-voice'

test.describe.configure({ mode: 'serial' })
test.use({
  permissions: ['microphone'],
  launchOptions: {
    args: [
      '--use-fake-device-for-media-stream',
      '--use-fake-ui-for-media-stream',
      '--autoplay-policy=no-user-gesture-required',
    ],
  },
})

let token = ''
let details = {}     // canvas_id → the decorated canvas row, as the panel route returns it
let threadId = null  // the Workspace thread the call is bound to

test.beforeAll(async ({ request, baseURL }) => {
  token = await adminToken(request, baseURL)
  const failures = await seedGallery(request, baseURL, token)
  expect(failures, 'every gallery canvas is accepted by the write route').toEqual([])
  const auth = { Authorization: `Bearer ${token}` }
  for (const c of GALLERY) {
    const res = await request.get(`${baseURL}/api/agents/${GALLERY_AGENT}/canvas/${c.canvas_id}`, { headers: auth })
    expect(res.ok(), `read back ${c.canvas_id}`).toBe(true)
    details[c.canvas_id] = await res.json()
  }
  // The call needs a thread to bind its transcript to. Reuse one of the
  // agent's Workspace threads when there is one; make one otherwise.
  const portal = `${baseURL}/api/enterprise/client-portal/agents/${GALLERY_AGENT}/sessions`
  const list = await request.get(portal, { headers: auth })
  const rows = list.ok() ? (await list.json()).sessions || [] : []
  threadId = rows[0]?.id || rows[0]?.session_id || null
  if (!threadId) {
    const made = await request.post(portal, { headers: auth, data: {} })
    expect(made.ok(), 'open a Workspace thread for the call').toBe(true)
    const body = await made.json()
    threadId = body.id || body.session_id
  }
  expect(threadId).toBeTruthy()
})

test.afterAll(async ({ request, baseURL }) => {
  if (KEEP) return
  await clearGallery(request, baseURL, await adminToken(request, baseURL))
})

for (const theme of THEMES) {
  for (const width of WIDTHS) {
    test(`voice · ${theme} · ${width}px — every gallery canvas renders bounded in the call column`, async ({ page }) => {
      test.setTimeout(10 * 60 * 1000)
      await page.setViewportSize({ width, height: 900 })
      await page.addInitScript((t) => localStorage.setItem('trinity-theme', t), theme)

      // What the panel route serves right now; switched per canvas below.
      let current = null
      let socket = null
      // The entry control reads the roster's `realtime_voice` capability. The
      // call itself is mocked below, so the capability is asserted here too —
      // otherwise a stack without a voice key (or an older backend that omits
      // the field) has no way into the column at all.
      await page.route('**/api/enterprise/client-portal/my-agents*', async (route) => {
        const res = await route.fetch()
        const body = await res.json()
        await route.fulfill({ response: res, json: { ...body, realtime_voice: { available: true, reason: null } } })
      })
      await page.route('**/api/enterprise/client-portal/agents/*/voice/start', (route) => route.fulfill({
        json: { voice_session_id: VOICE_SID, websocket_url: `/ws/voice/${VOICE_SID}`, chat_session_id: threadId },
      }))
      await page.route(`**/api/agents/*/voice/${VOICE_SID}/panel`, (route) => route.fulfill({ json: current }))
      await page.routeWebSocket((url) => url.pathname.endsWith(`/ws/voice/${VOICE_SID}`), (ws) => { socket = ws })

      await page.goto(`/workspace/c/${threadId}`)
      const call = page.getByTestId('portal-voice-call')
      await call.waitFor({ timeout: 20000 })
      await expect(call, 'voice is available on this instance').toBeEnabled()
      // The first panel read happens on mount; give it a real canvas so the
      // column shows a board rather than its empty state.
      current = { ...details[GALLERY[0].canvas_id], updated_at: new Date().toISOString() }
      await call.click({ timeout: 10000 })
      const column = page.getByTestId('portal-voice-canvas')
      await column.waitFor({ timeout: 20000 })

      const report = []
      for (const [i, canvas] of GALLERY.entries()) {
        // A distinct stamp per switch: the column replaces its board only when
        // `updated_at` moved (`canvasChanged`).
        current = { ...details[canvas.canvas_id], updated_at: new Date(Date.now() + i * 1000).toISOString() }
        await expect.poll(() => socket, { timeout: 15000, message: 'the audio socket opened' }).toBeTruthy()
        socket.send(JSON.stringify({ type: 'tool_result', tool: 'update_panel' }))
        await expect
          .poll(
            () => page.evaluate(() =>
              [...document.querySelectorAll('[data-testid="portal-voice-canvas"] [data-canvas-block]')].map((b) => b.dataset.canvasBlock),
            ),
            { timeout: 30000, message: `${canvas.canvas_id} renders every block it holds in the voice column` },
          )
          .toEqual(details[canvas.canvas_id].blocks.map((b) => b.id))
        await settled(page)
        const result = await page.evaluate(measureKit)
        report.push({ canvas: canvas.canvas_id, ...result })
        await unclip(page)
        await page.screenshot({ path: `${SHOTS}/voice-${theme}-${width}-${canvas.canvas_id}.png`, fullPage: true })
      }

      // The column itself never widens the page (the #2581 report).
      const pageOverflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
      expect(pageOverflow, 'the Workspace page does not scroll horizontally during a call').toBeLessThanOrEqual(1)

      const problems = report.filter((r) => r.issues.length)
      const summary = problems
        .map((r) => `${r.canvas}:\n` + r.issues.map((x) => `    ${x.id ? `[${x.kind}#${x.id}] ` : ''}${x.msg}`).join('\n'))
        .join('\n')
      expect(problems, `layout problems in the voice column (${theme}/${width}):\n${summary}`).toEqual([])
    })
  }
}
