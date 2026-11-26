import { test, expect } from '@playwright/test'
import { openOrb } from './helpers/brain-orb.js'

/**
 * trinity-enterprise#1302 — Brain Orb ambient (watch-only) mode.
 *
 * Two harnesses, neither needs an agent:
 *   - STANDALONE page + FALLBACK graph (as the other orb specs): enter / leave,
 *     and that leaving puts the orb back exactly as it was.
 *   - EMBEDDED: a route-fulfilled host page plays AgentBrainOrb.vue's part (the
 *     `brain-orb:init` handshake) and every platform read is route-mocked, so the
 *     mode's words can be asserted against known signals — a running run, a
 *     stopped agent, an expired sign-in.
 *
 * What decides the words (classifier, redaction, mood ladder) is unit-tested in
 * tests/unit/brainOrbAmbientCore.spec.js; this file proves the wiring.
 * Skips without WebGL, like the other orb specs.
 */

const amb = (page) => page.evaluate(() => window.orbAmbient())

test.describe('brain orb — ambient mode, standalone (ent#1302)', () => {
  test('@smoke M enters, any key leaves, and the selection and camera come back as they were', async ({ page }) => {
    await openOrb(page)
    // pin a selection the way a person does: search, Enter
    await page.keyboard.press('/')
    await page.keyboard.type('note 12')
    await page.keyboard.press('Enter')
    await expect.poll(() => page.evaluate(() => window.orbHover().i)).toBeGreaterThanOrEqual(0)
    await page.evaluate(() => document.getElementById('scene').focus())
    await page.waitForFunction(() => !window.orbState().focusing)
    const before = await page.evaluate(() => ({ sel: window.orbHover().i, dist: window.orbState().camTargetDist }))
    await expect(page.locator('#inspector')).toBeVisible()

    await page.keyboard.press('m')
    await expect(page.locator('#ambientHud')).toBeVisible()
    await expect(page.locator('#inspector')).toBeHidden()
    await expect(page.locator('#controls')).toBeHidden()
    expect((await amb(page)).active).toBe(true)
    // no platform behind the standalone page: it says so instead of pretending
    await expect(page.locator('#amVerb')).toHaveText('not connected')
    await expect(page.locator('#amMoodWord')).toHaveText('unknown')
    await expect(page.locator('#amMissing')).toContainText('graph only')

    // moving the pointer is not a deliberate interaction
    await page.mouse.move(300, 300)
    await page.mouse.move(500, 400)
    expect((await amb(page)).active).toBe(true)

    await page.keyboard.press('Escape')   // would normally also clear the selection
    await expect(page.locator('#ambientHud')).toBeHidden()
    await expect(page.locator('#inspector')).toBeVisible()
    const after = await page.evaluate(() => ({ sel: window.orbHover().i, dist: window.orbState().camTargetDist }))
    expect(after).toEqual(before)
    const left = await amb(page)
    expect(left).toMatchObject({ active: false, arcs: 0, waves: 0, labels: 0, heated: 0 })
  })

  test('the control enters it and a click leaves it without selecting anything', async ({ page }) => {
    await openOrb(page)
    await page.locator('#ambientBtn').click()
    await expect(page.locator('#ambientHud')).toBeVisible()
    const vp = page.viewportSize()
    await page.mouse.click(vp.width / 2, vp.height / 2)   // dead centre of the orb
    await expect(page.locator('#ambientHud')).toBeHidden()
    expect(await page.evaluate(() => window.orbHover().i)).toBe(-1)
  })
})

/* ------------------------------------------------------------------ embedded */
const HOST = '/__e2e-orb-host'
const GRAPH = {
  meta: { total_nodes: 3, total_edges: 1 },
  nodes: [
    { id: 'Books/risk/Ambiguity Aversion.md', title: 'Ambiguity Aversion', layer: 2, degree: 2, heat: 0.2, age_days: 0 },
    { id: '02-Permanent/Dopamine.md', title: 'Dopamine', layer: 3, degree: 2, heat: 0.1, age_days: 40 },
    { id: '02-Permanent/Flow.md', title: 'Flow', layer: 3, degree: 1, heat: 0.1, age_days: 40 },
  ],
  edges: [{ source: '02-Permanent/Dopamine.md', target: '02-Permanent/Flow.md', type: 'associates', weight: 0.5 }],
}
const sse = (entries) => entries.map((e) => `data: ${JSON.stringify(e)}\n\n`).join('') + 'data: {"type":"stream_end"}\n\n'
const STREAM = [
  { type: 'user', message: { content: 'PROMPT-SECRET what is my salary' } },
  { type: 'assistant', message: { content: [{ type: 'thinking', thinking: 'REASONING-SECRET' }] } },
  { type: 'assistant', message: { content: [{ type: 'tool_use', id: 't1', name: 'Bash', input: { command: 'curl COMMAND-SECRET', description: 'Fetch the feed' } }] } },
  { type: 'assistant', message: { content: [{ type: 'tool_use', id: 't2', name: 'Read', input: { file_path: '/home/developer/Brain/02-Permanent/Dopamine.md' } }] } },
  { type: 'assistant', message: { content: [{ type: 'tool_use', id: 't3', name: 'Read', input: { file_path: '/home/developer/FILENAME-SECRET.env' } }] } },
  { type: 'assistant', message: { content: [{ type: 'tool_use', id: 't4', name: 'Edit', input: { file_path: '/home/developer/Brain/Books/risk/Ambiguity Aversion.md' } }] } },
]

async function openEmbedded(page, { agent = { status: 'running' }, runs = [], status = 200 } = {}) {
  const calls = []
  await page.route(`**${HOST}`, (r) => r.fulfill({
    contentType: 'text/html',
    body: `<!doctype html><body style="margin:0"><iframe id="orb" src="/brain-orb/index.html" style="position:fixed;inset:0;width:100%;height:100%;border:0"></iframe>
<script>addEventListener('message', e => { if (e.data && e.data.type === 'brain-orb:ready')
  document.getElementById('orb').contentWindow.postMessage({ type: 'brain-orb:init', agentName: 'demo', apiBase: '', authToken: 'e2e' }, location.origin) })</script></body>`,
  }))
  await page.route('**/api/agents/demo**', (route) => {
    const req = route.request(), path = new URL(req.url()).pathname.replace('/api/agents/demo', '')
    calls.push(req.method() + ' ' + path)
    const json = (body, code = 200) => route.fulfill({ status: code, contentType: 'application/json', body: JSON.stringify(body) })
    if (path === '/brain-orb/data') return json(GRAPH)
    if (path.startsWith('/brain-orb/')) return json({ detail: 'not found' }, 404)
    if (status !== 200) return json({ detail: 'nope' }, status)
    if (path === '') return json({ name: 'demo', ...agent })
    if (path === '/executions') return json(runs)
    if (path === '/queue') return json({ queue_length: 0 })
    if (path === '/schedules') return json([])
    if (path === '/executions/r1/stream') return route.fulfill({ contentType: 'text/event-stream', body: sse(STREAM) })
    return json({ detail: 'not found' }, 404)
  })
  await page.goto(HOST)
  const hasWebGL = await page.evaluate(() => { const c = document.createElement('canvas'); return !!(c.getContext('webgl2') || c.getContext('webgl')) })
  test.skip(!hasWebGL, 'no WebGL in this Chromium')
  const orb = page.frameLocator('#orb')
  await expect(orb.locator('#loading')).toBeHidden({ timeout: 20000 })
  await orb.locator('#ambientBtn').click()
  await expect(orb.locator('#ambientHud')).toBeVisible()
  return { orb, calls }
}

test.describe('brain orb — ambient mode, embedded against mocked platform reads (ent#1302)', () => {
  const now = () => new Date().toISOString()

  test('a running run: names the playbook, lists the notes it used, and withholds everything else', async ({ page }) => {
    const { orb, calls } = await openEmbedded(page, {
      runs: [{ id: 'r1', status: 'running', started_at: now(), completed_at: null, message: '/daily-digest ARGS-SECRET', triggered_by: 'manual', context_used: 30, context_max: 100 }],
    })
    await expect(orb.locator('#amStatus')).toHaveText('running a task')
    await expect(orb.locator('#amRun')).toContainText('/daily-digest')
    await expect(orb.locator('#amUsed')).toContainText('Ambiguity Aversion')
    await expect(orb.locator('#amUsed')).toContainText('Dopamine')
    await expect(orb.locator('#amVerb')).toHaveText('writing a note')
    await expect(orb.locator('#amWhat')).toHaveText('Ambiguity Aversion')
    await expect(orb.locator('#amMoodWord')).toHaveText('working')   // a run in progress outranks "digesting" in the ladder
    await expect(orb.locator('#amCtx')).toHaveText('30%')
    const text = await orb.locator('#ambientHud').innerText()
    expect(text).not.toMatch(/SECRET/)
    // read-only: the mode never issues anything but GETs
    expect(calls.filter((c) => !c.startsWith('GET '))).toEqual([])
  })

  test('a stopped agent says so in words and reads asleep', async ({ page }) => {
    const { orb } = await openEmbedded(page, { agent: { status: 'stopped' } })
    await expect(orb.locator('#amVerb')).toHaveText('stopped')
    await expect(orb.locator('#amWhat')).toHaveText('the agent is not running')
    await expect(orb.locator('#amMoodWord')).toHaveText('asleep')
  })

  test('an expired sign-in is named, not animated over', async ({ page }) => {
    const { orb } = await openEmbedded(page, { status: 401 })
    await expect(orb.locator('#amVerb')).toHaveText('signed out')
    await expect(orb.locator('#amMoodWord')).toHaveText('unknown')
  })
})
