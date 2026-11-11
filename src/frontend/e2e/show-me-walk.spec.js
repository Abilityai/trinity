import { test, expect } from '@playwright/test'

/**
 * Show me, walked: from the card Atlas proposes in the Workspace chat to an agent's saved
 * keys. The person clicks Show me, a new tab opens on the Dashboard, and each thing to
 * click is lit up in turn: the agent, its Settings, Credentials, each key's box, Save.
 *
 * HERMETIC: the backend is mocked, so this proves the walk across the real pages and the
 * real credentials list. It does not prove what Atlas itself proposes.
 */

const API = '**/api/enterprise/client-portal'
const GUIDE = 'build-guide'
const AGENT = 'game-promoter'
const json = (body, status = 200) => ({ status, contentType: 'application/json', body: JSON.stringify(body) })
const isPath = (path) => (url) => url.pathname === path

const STATE = {
  schema: 'trinity-build/v1', stage: 'live', company: { name: 'Acme Freight' }, person: { role: 'Head of Operations' },
  level: 'practitioner', profile_complete: true, audit: { status: 'done' }, departments: [], data_sources: [], agents: [],
}

const promoter = {
  name: AGENT, display_label: 'Game Promoter', status: 'running', owner: 'admin', can_share: true, can_delete: true,
  runtime: 'claude-code', is_system: false, tags: [], autonomy_enabled: false,
}
const REPORTER = 'match-reporter'
const reporter = { ...promoter, name: REPORTER, display_label: 'Match Reporter' }

const report = (set = []) => ({
  state: set.length >= 2 ? 'ready' : 'needs_credentials',
  requirements: [
    { name: 'SUPABASE_URL', title: 'Club app address (Supabase)', description: 'Your Supabase project URL.', required: true, secret: false, format: 'text', status: set.includes('SUPABASE_URL') ? 'set' : 'missing' },
    { name: 'SUPABASE_KEY', title: 'Club app key (read-only preferred)', description: 'A key that can read games and players.', required: true, secret: true, format: 'secret', status: set.includes('SUPABASE_KEY') ? 'set' : 'missing' },
    { name: 'WHATSAPP_TOKEN', title: 'WhatsApp Cloud API token', required: false, secret: true, format: 'secret', status: 'missing' },
  ],
  summary: { blocking: 2 - set.length, set: set.length, platform_injected_excluded: 0 },
  errors: [],
})

// The Workspace chat with Atlas's card, and the main app's pages it walks through. Every
// route is on the context, so the Show me tab gets them too.
async function mockAll(context, proposal) {
  const b64 = (o) => Buffer.from(JSON.stringify(o)).toString('base64url')
  const token = `${b64({ alg: 'none', typ: 'JWT' })}.${b64({ sub: 'admin', mode: 'admin', exp: 4102444800 })}.x`
  await context.addInitScript(({ t, answers }) => {
    localStorage.setItem('token', t)
    for (const email of ['', 'tester@example.com', 'e2e@example.com']) localStorage.setItem(`trinity-workspace-onboarding:${email}`, answers)
    // The first walk's Dashboard stop has its own test; the others start after it.
    if (!sessionStorage.getItem('first-walk')) localStorage.setItem('trinity-walk-dashboard-seen', '1')
    // The way to the Dashboard, lit in the Workspace before a first walk, has its own test too.
    if (!sessionStorage.getItem('first-door')) localStorage.setItem('trinity-walk-door-seen', '1')
  }, { t: token, answers: JSON.stringify({ current: null, done: true, skipped: false, finished: true, savedAt: Date.now(), answers: { role: 'Head of Operations', software: 'none', agents: 'tried', company: 'Acme Freight' }, audit: 'start', sent: true, tourSeen: true, featureTourSeen: true }) })

  const injected = []
  let saved = []
  await context.route('**/api/**', (route) => route.fulfill(json({})))
  await context.route('**/api/setup/status', (route) => route.fulfill(json({ setup_completed: true })))
  await context.route('**/api/settings/feature-flags', (route) => route.fulfill(json({ claude_auth_configured: true })))
  await context.route('**/api/users/me', (route) => route.fulfill(json({ email: 'e2e@example.com', role: 'admin' })))

  const mine = (name, label) => ({ name, display_label: label, owned: true, availability: 'ready', playbooks: [] })
  await context.route(`${API}/my-agents*`, (route) => route.fulfill(json({
    client_email: 'e2e@example.com',
    agents: [{ ...mine(GUIDE, 'Build Guide'), description: 'e2e fixture' }, mine(AGENT, 'Game Promoter'), mine(REPORTER, 'Match Reporter')],
    multi_agent_chat_available: true,
    orchestration_available: false,
  })))
  await context.route(`${API}/sessions*`, (route) => route.fulfill(json({ sessions: [] })))
  await context.route(`${API}/chat-state*`, (route) => route.fulfill(json({ chats: [] })))
  await context.route(`${API}/asks*`, (route) => route.fulfill(json({ asks: [] })))
  await context.route(`${API}/agents/${GUIDE}/canvas/build`, (route) => route.fulfill(json({
    id: 'build', title: 'Your build', updated_at: '2026-11-03T10:00:00Z', blocks: [{ id: 'state', kind: 'json', payload: STATE }],
  })))
  await context.route(`**/agents/${GUIDE}/canvas/company-research`, (route) => route.fulfill(json({ detail: 'not found' }, 404)))
  await context.route(`${API}/agents/${GUIDE}/canvas/fleet`, (route) => route.fulfill(json({
    id: 'fleet', title: 'Fleet', blocks: [{ id: 'proposal', kind: 'json', payload: proposal }],
  })))
  await context.route(`${API}/agents/${GUIDE}/chat*`, (route) => route.fulfill(json({ detail: 'mocked' }, 503)))

  await context.route(isPath('/api/agents'), (route) => route.fulfill(json([promoter, reporter])))
  await context.route(isPath('/api/activities/timeline'), (route) => route.fulfill(json({ activities: [] })))
  await context.route(isPath('/api/agents/context-stats'), (route) => route.fulfill(json({ agents: [] })))
  await context.route(isPath('/api/agents/execution-stats'), (route) => route.fulfill(json({ agents: [] })))
  await context.route(isPath('/api/system-views'), (route) => route.fulfill(json([])))
  await context.route(isPath('/api/tags'), (route) => route.fulfill(json([])))
  await context.route(isPath(`/api/agents/${AGENT}`), (route) => route.fulfill(json(promoter)))
  await context.route(isPath(`/api/agents/${AGENT}/credential-requirements`), (route) => route.fulfill(json(report(saved))))
  await context.route(isPath(`/api/agents/${AGENT}/credentials/inject`), (route) => {
    injected.push(route.request().postDataJSON())
    saved = ['SUPABASE_URL', 'SUPABASE_KEY']
    return route.fulfill(json({ success: true }))
  })

  // Sharing, calling, schedules and Autonomy, each changed only through the page's own control.
  const world = { people: [], mayCall: [], digestOn: false, autonomy: false, shares: [], permissionSaves: [], fleetWrites: 0 }
  await context.route(isPath(`/api/agents/${AGENT}/access`), (route) => route.fulfill(json(world.people)))
  await context.route(isPath(`/api/agents/${AGENT}/shares`), (route) => route.fulfill(json(world.shares.map((email) => ({ shared_with_email: email })))))
  await context.route(isPath(`/api/agents/${AGENT}/share`), (route) => {
    const { email } = route.request().postDataJSON()
    world.shares.push(email)
    world.people = [{ email, role: 'user', source: 'share' }]
    return route.fulfill(json({ success: true }))
  })
  await context.route(isPath(`/api/agents/${AGENT}/permissions`), (route) => {
    if (route.request().method() === 'PUT') {
      const body = route.request().postDataJSON()
      world.permissionSaves.push(body)
      world.mayCall = body.permitted_agents || []
      return route.fulfill(json({ success: true }))
    }
    return route.fulfill(json({
      agent_name: AGENT,
      permitted_agents: world.mayCall.map((name) => ({ name })),
      available_agents: [{ name: REPORTER, status: 'running', permitted: world.mayCall.includes(REPORTER) }],
    }))
  })
  const digest = () => ({ id: 's1', name: 'Daily digest', enabled: world.digestOn, cron_expression: '0 9 * * *', timezone: 'UTC', message: 'Post the day', agent_name: AGENT })
  await context.route(isPath(`/api/agents/${AGENT}/schedules`), (route) => route.fulfill(json([digest()])))
  await context.route(isPath(`/api/agents/${AGENT}/schedules/s1/enable`), (route) => { world.digestOn = true; return route.fulfill(json(digest())) })
  await context.route(isPath(`/api/agents/${AGENT}/autonomy`), (route) => {
    if (route.request().method() === 'PUT') world.autonomy = true
    return route.fulfill(json({ autonomy_enabled: world.autonomy, message: 'Autonomy enabled.' }))
  })
  await context.route(`**/api/agents/${GUIDE}/files*`, (route) => {
    if (route.request().method() === 'PUT' && route.request().url().includes('fleet-state.json')) world.fleetWrites += 1
    return route.fulfill(json({ success: true }))
  })
  return { injected, world }
}

// Opens Atlas's Show me card in its new tab, sized and ready.
async function openWalk(page, context, words, width = 1440) {
  await page.goto('/workspace/build')
  await expect(page.getByTestId('build-fleet-card')).toContainText(words)
  const [tab] = await Promise.all([context.waitForEvent('page'), page.getByTestId('build-fleet-show-me').click()])
  await tab.setViewportSize({ width, height: 900 })
  await tab.addLocatorHandler(tab.getByTestId('build-tour-skip'), (skip) => skip.click())
  await expect(title(tab)).toHaveText('Your agents live here')
  await tab.locator(`[data-walk-agent="${AGENT}"]:visible`).first().click()
  await expect(tab).toHaveURL(new RegExp(`/agents/${AGENT}`))
  return tab
}

const title = (page) => page.locator('#show-me-walk-title')

test.describe('Show me, walked @smoke', () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.addLocatorHandler(page.getByTestId('build-tour-skip'), (skip) => skip.click())
    // A level moment (the levels card lit) is stepped through, as in workspace-build.spec.js.
    await page.addLocatorHandler(page.getByTestId('build-moment'), async (moment) => {
      for (let i = 0; i < 3 && await moment.isVisible(); i++) await moment.getByTestId('build-tour-next').click()
    })
  })

  test('a first Show me lights the way to the Dashboard in the Workspace, then opens the walk; after that it opens at once', async ({ page, context }) => {
    await context.addInitScript(() => { sessionStorage.setItem('first-door', '1') })
    await mockAll(context, { id: 'p1', action: 'show-me', key: 'people', agent: AGENT })
    await page.goto('/workspace/build')
    const dashboard = page.getByTestId('sidebar-dashboard')
    await expect(dashboard).toHaveAttribute('href', '/')
    await page.getByTestId('build-fleet-show-me').click()
    const door = page.getByTestId('build-walk-door')
    await expect(door).toContainText('This is how you get to the Dashboard')
    // The light is on the sidebar's Dashboard link, and no tab has opened yet.
    const lit = await door.locator('[aria-hidden="true"]').first().boundingBox()
    const link = await dashboard.boundingBox()
    expect(lit.x).toBeLessThanOrEqual(link.x)
    expect(lit.y).toBeLessThanOrEqual(link.y)
    expect(lit.x + lit.width).toBeGreaterThanOrEqual(link.x + link.width)
    expect(context.pages()).toHaveLength(1)
    const [tab] = await Promise.all([context.waitForEvent('page'), door.getByTestId('build-tour-next').click()])
    expect(tab.url()).toContain('walk=people')
    await expect(door).toHaveCount(0)
    expect(await page.evaluate(() => localStorage.getItem('trinity-walk-door-seen'))).toBe('1')
    await tab.close()
  })

  test('the first walk opens on the whole Dashboard, says what it is, then lights the agent', async ({ page, context }) => {
    await context.addInitScript(() => { sessionStorage.setItem('first-walk', '1'); localStorage.removeItem('trinity-walk-dashboard-seen') })
    await mockAll(context, { id: 'p1', action: 'show-me', key: 'credentials', agent: AGENT, keys: 'SUPABASE_URL,SUPABASE_KEY' })
    await page.goto('/workspace/build')
    const [tab] = await Promise.all([context.waitForEvent('page'), page.getByTestId('build-fleet-show-me').click()])
    await tab.setViewportSize({ width: 1440, height: 900 })
    await tab.addLocatorHandler(tab.getByTestId('build-tour-skip'), (skip) => skip.click())
    await expect(title(tab)).toHaveText('This is the Dashboard')
    // The lit part is the Dashboard itself, not one agent.
    const lit = await tab.getByTestId('show-me-walk-spot').boundingBox()
    expect(lit.width).toBeGreaterThan(1000)
    // Its card sits in the middle, over nothing it explains.
    const card = tab.getByTestId('show-me-walk').locator('h2').locator('..')
    await expect.poll(async () => { const b = await card.boundingBox(); return Math.round(Math.abs(b.x + b.width / 2 - 720)) }).toBeLessThan(20)
    if (process.env.SHOT) await tab.screenshot({ path: process.env.SHOT })
    await tab.getByTestId('show-me-walk-next').click()
    await expect(title(tab)).toHaveText('Your agents live here')
    await expect(tab.getByTestId('show-me-walk-body')).toHaveText('Every agent you have is listed here. Click Game Promoter to open it.')
  })

  test("from Atlas's card to saved keys, one lit-up click at a time", async ({ page, context }) => {
    const { injected } = await mockAll(context, { id: 'p1', action: 'show-me', key: 'credentials', agent: AGENT, keys: 'SUPABASE_URL,SUPABASE_KEY' })
    await page.goto('/workspace/build')
    await expect(page.getByTestId('build-fleet-card')).toContainText("Show me how to add Game Promoter's credentials")

    const [tab] = await Promise.all([context.waitForEvent('page'), page.getByTestId('build-fleet-show-me').click()])
    await tab.setViewportSize({ width: 1440, height: 900 })
    await tab.addLocatorHandler(tab.getByTestId('build-tour-skip'), (skip) => skip.click())

    // The Dashboard, with the agent lit up and the walk out of the address.
    await expect(title(tab)).toHaveText('Your agents live here')
    await expect(tab).toHaveURL(/\/$/)
    await expect(tab.getByTestId('show-me-walk-body')).toHaveText('This is the Dashboard: every agent you have is on it. Click Game Promoter to open it.')
    await expect(tab.getByTestId('show-me-walk-spot')).toBeVisible()
    await expect(tab.getByTestId('show-me-walk-block')).toHaveCount(4)

    await tab.locator('[data-walk-agent="game-promoter"]:visible').first().click()
    await expect(tab).toHaveURL(new RegExp(`/agents/${AGENT}`))
    await expect(title(tab)).toHaveText('Its settings')

    await tab.locator('[data-walk="settings-tab"] button').first().click()
    await expect(title(tab)).toHaveText('Credentials')

    await tab.locator('[data-testid="settings-rail"] [data-section="credentials"]').click()
    await expect(title(tab)).toHaveText('SUPABASE_URL')
    await tab.locator('[data-walk-credential="SUPABASE_URL"] input').fill('https://example.supabase.co')
    await tab.getByTestId('show-me-walk-next').click()
    await expect(title(tab)).toHaveText('SUPABASE_KEY')
    await tab.locator('[data-walk-credential="SUPABASE_KEY"] input').fill('not-a-real-key')
    await tab.getByTestId('show-me-walk-next').click()
    await expect(title(tab)).toHaveText('Save them')

    await tab.locator('[data-walk="credentials-save"]').click()
    await expect(title(tab)).toHaveText("That's it")
    await expect(tab.getByTestId('show-me-walk-body')).toContainText('Go back to your chat with Atlas')
    expect(injected).toHaveLength(1)
    const env = injected[0].files['.env']
    expect(env).toContain('SUPABASE_URL="https://example.supabase.co"')
    expect(env).toContain('SUPABASE_KEY="not-a-real-key"')
    expect(env).not.toContain('WHATSAPP_TOKEN')

    await tab.getByTestId('show-me-walk-done').click()
    await expect(tab.getByTestId('show-me-walk')).toHaveCount(0)
  })

  test('sharing: from the card to the person added, typing the email on the page', async ({ page, context }) => {
    const { world } = await mockAll(context, { id: 'w1', action: 'show-me', key: 'people', agent: AGENT })
    const tab = await openWalk(page, context, 'Show me how to share Game Promoter with someone')
    await expect(title(tab)).toHaveText('Its settings')
    await tab.locator('[data-walk="settings-tab"] button').first().click()
    await expect(title(tab)).toHaveText('People')
    await tab.locator('[data-testid="settings-rail"] [data-section="people"]').click()
    await expect(title(tab)).toHaveText('Their email')
    await tab.locator('[data-walk="people-form"] input[type="email"]').fill('colleague@example.com')
    await tab.locator('[data-walk="people-form"] button[type="submit"]').click()
    await expect(title(tab)).toHaveText("That's it")
    await expect(tab.getByTestId('show-me-walk-body')).toContainText('They find it in their Workspace')
    expect(world.shares).toEqual(['colleague@example.com'])
  })

  test('Next on a step that only opens something does the click for them: agent, Settings, People', async ({ page, context }) => {
    await mockAll(context, { id: 'w1n', action: 'show-me', key: 'people', agent: AGENT })
    await page.goto('/workspace/build')
    await expect(page.getByTestId('build-fleet-card')).toContainText('Show me how to share Game Promoter with someone')
    const [tab] = await Promise.all([context.waitForEvent('page'), page.getByTestId('build-fleet-show-me').click()])
    await tab.setViewportSize({ width: 1440, height: 900 })
    await expect(title(tab)).toHaveText('Your agents live here')
    await tab.getByTestId('show-me-walk-press').click()
    await expect(tab).toHaveURL(new RegExp(`/agents/${AGENT}`))
    await expect(title(tab)).toHaveText('Its settings')
    await tab.getByTestId('show-me-walk-press').click()
    await expect(title(tab)).toHaveText('People')
    await tab.getByTestId('show-me-walk-press').click()
    await expect(title(tab)).toHaveText('Their email')
    // Typing the email is theirs: no Next here.
    await expect(tab.getByTestId('show-me-walk-press')).toHaveCount(0)
  })

  test('calling: Permissions found under More on a narrower window, the agent ticked, then saved', async ({ page, context }) => {
    const { world } = await mockAll(context, { id: 'w2', action: 'show-me', key: 'permissions', agent: AGENT, to: REPORTER })
    const tab = await openWalk(page, context, 'Show me how to let Game Promoter call Match Reporter', 900)
    await expect(title(tab)).toHaveText('Permissions')
    await expect(tab.getByTestId('show-me-walk-body')).toHaveText("It's under More. Click More, then Permissions.")
    const more = tab.locator('[data-testid="agent-tabs"] [data-overflow-trigger]:visible').first()
    await more.click()
    await expect(tab.getByTestId('show-me-walk-body')).toHaveText('Which agents Game Promoter may call is set under Permissions. Click it.')
    // Escape closes the menu, not the walk, which goes back to lighting More.
    await tab.keyboard.press('Escape')
    await expect(tab.locator('[data-overflow-menu]')).toHaveCount(0)
    await expect(tab.getByTestId('show-me-walk-body')).toHaveText("It's under More. Click More, then Permissions.")
    await more.click()
    await tab.locator('[data-overflow-menu] [data-tab="permissions"]').click()
    await expect(title(tab)).toHaveText('Tick Match Reporter')
    await tab.locator(`[data-walk-permission="${REPORTER}"] input[type="checkbox"]`).check()
    await expect(title(tab)).toHaveText('Save them')
    await tab.locator('[data-walk="permissions-save"]').click()
    await expect(title(tab)).toHaveText("That's it")
    await expect(tab.getByTestId('show-me-walk-body')).toContainText('Game Promoter may now call Match Reporter.')
    expect(world.permissionSaves).toEqual([{ permitted_agents: [REPORTER] }])
  })

  test('schedules: the switch, then the Autonomy switch at the top, without which no schedule runs', async ({ page, context }) => {
    const { world } = await mockAll(context, { id: 'w3', action: 'show-me', key: 'schedules', agent: AGENT, schedule_id: 's1' })
    const tab = await openWalk(page, context, "Show me how to turn on Game Promoter's Daily digest")
    await expect(title(tab)).toHaveText('Schedules')
    await tab.locator('[data-testid="agent-tabs"] [data-tab="schedules"]:visible').first().click()
    await expect(title(tab)).toHaveText('Turn on Daily digest')
    await tab.locator('[data-walk-schedule="s1"] [data-walk="schedule-toggle"]').click()
    await expect(title(tab)).toHaveText('Turn on Autonomy')
    await expect(tab.getByTestId('show-me-walk-body')).toContainText('While it says Manual')
    const autonomy = tab.locator('[data-walk="autonomy-switch"]')
    await expect(autonomy).toContainText('Manual')
    await autonomy.getByRole('switch').click()
    await expect(title(tab)).toHaveText("That's it")
    await expect(autonomy).toContainText('AUTO')
    expect(world.digestOn).toBe(true)
    expect(world.autonomy).toBe(true)
  })

  test('after a confirmed share, See where this is set shows the people list, not a walk that cannot finish', async ({ page, context }) => {
    const { world } = await mockAll(context, { id: 'c1', action: 'share', agent: AGENT, email: 'colleague@example.com' })
    await page.goto('/workspace/build')
    await expect(page.getByTestId('build-fleet-card-words')).toHaveText('Share Game Promoter with colleague@example.com')
    await page.getByTestId('build-fleet-confirm').click()
    await expect(page.getByTestId('build-fleet-card')).toContainText('Done.')
    expect(world.shares).toEqual(['colleague@example.com'])
    await expect(page.getByTestId('build-fleet-see-where')).toHaveAttribute('href', `/agents/${AGENT}?tab=settings&section=people&showme=people`)
  })

  test('coming back from a walk rewrites the fleet file Atlas reads', async ({ page, context }) => {
    const { world } = await mockAll(context, { id: 'w4', action: 'show-me', key: 'people', agent: AGENT })
    await page.goto('/workspace/build')
    await expect(page.getByTestId('build-fleet-card')).toContainText('Show me how to share Game Promoter with someone')
    await expect.poll(() => world.fleetWrites).toBeGreaterThan(0)
    const before = world.fleetWrites
    const [tab] = await Promise.all([context.waitForEvent('page'), page.getByTestId('build-fleet-show-me').click()])
    await tab.close()
    await page.bringToFront()
    // Focus and visibility arrive together: one rewrite, not two.
    await page.evaluate(() => { window.dispatchEvent(new Event('focus')); document.dispatchEvent(new Event('visibilitychange')) })
    await expect.poll(() => world.fleetWrites).toBeGreaterThan(before)
    await page.waitForTimeout(500)
    expect(world.fleetWrites).toBe(before + 1)
  })

  test('the dimmed page takes no clicks while a part is lit up', async ({ page, context }) => {
    await mockAll(context, { id: 'p2', action: 'show-me', key: 'credentials', agent: AGENT })
    await page.goto(`/?walk=credentials&walk_agent=${AGENT}`)
    await expect(title(page)).toHaveText('Your agents live here')
    await expect(page.getByTestId('show-me-walk-spot')).toBeVisible()
    // A click on the covered page lands on the cover, not on what is under it.
    const hit = await page.evaluate(() => document.elementFromPoint(5, 5)?.getAttribute('data-testid'))
    expect(hit).toBe('show-me-walk-block')
    await page.getByTestId('show-me-walk-stop').click()
    await expect(page.getByTestId('show-me-walk')).toHaveCount(0)
  })
})
