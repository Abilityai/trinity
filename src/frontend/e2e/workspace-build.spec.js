import { test, expect } from '@playwright/test'

/**
 * Workspace Build: chat on the left, the person's world on the right.
 *
 * HERMETIC and `@smoke`: no live agent and no model turn. The guide's canvas is
 * mocked, which is the one thing this page reads from an agent, so this proves
 * the wiring the unit specs cannot: the sidebar door, the route, the questions,
 * the first message reaching the real conversation, and the world drawing what
 * the guide reports. It does not prove that the guide agent itself behaves.
 */

const API = '**/api/enterprise/client-portal'
const GUIDE = 'build-guide'
const json = (body, status = 200) => ({ status, contentType: 'application/json', body: JSON.stringify(body) })

const STATE = {
  schema: 'trinity-build/v1',
  stage: 'build',
  company: { name: 'Acme Freight', industry: 'Logistics', size: '40 people' },
  person: { role: 'Head of Operations' },
  level: 'practitioner',
  profile_complete: true,
  audit: { status: 'done', summary: 'Customer email takes most of the operations team\'s morning.' },
  departments: [
    { name: 'Operations', opportunities: [
      { title: 'Triage customer emails', why: 'Two hours every morning', effort: 'small', value: 'high', recommended: true },
      { title: 'Chase late carriers', why: 'Done by hand from a spreadsheet', effort: 'medium', value: 'medium' },
    ] },
    { name: 'Finance', opportunities: [
      { title: 'Match invoices to shipments', why: 'PDFs from 30 carriers', effort: 'large', value: 'high' },
    ] },
  ],
  data_sources: [
    { name: 'Shared inbox', department: 'Operations', holds: 'Customer emails', format: 'Email', access: 'known' },
    { name: 'Carrier sheet', department: 'Operations', holds: 'Carrier contacts and rates', format: 'Google Sheet', access: 'unknown' },
  ],
  context_md: '# Acme Freight\n\n## Terminology\n\n- **Lane**: a regular route between two depots.\n- **POD**: proof of delivery.\n',
  agents: [
    {
      id: 'email-triage', name: 'Email triage', department: 'Operations', purpose: 'Sorts the shared inbox and drafts replies for a person to send.',
      kind: 'agent', status: 'building',
      skills: [
        { name: 'triage-inbox', purpose: 'Labels each email by what it needs.', status: 'done' },
        { name: 'draft-reply', purpose: 'Drafts a reply using context.md.', status: 'building' },
      ],
      connections: [{ platform: 'Google Workspace', why: 'Reads the shared inbox', status: 'needed' }],
      variables: ['GOOGLE_OAUTH_TOKEN'],
      schedules: [{ name: 'Morning sweep', when: 'Weekdays at 7:00', enabled: false }],
    },
  ],
  next_step: 'Atlas is writing the draft-reply skill.',
}

const canvas = (state) => ({
  id: 'build',
  title: 'Your build',
  updated_at: '2026-10-31T10:00:00Z',
  blocks: [
    { id: 'summary', kind: 'markdown', markdown: 'Building Email triage.' },
    { id: 'state', kind: 'json', payload: state },
  ],
})

// Not an admin unless a test says so: an admin's onboarding is the setup panel.
async function mockWorkspace(page, { agents, state = null, orchestration = false, claude = true, role = 'creator', guide = GUIDE } = {}) {
  const sent = []
  const tasks = []
  sent.tasks = tasks
  await page.route('**/api/**', (route) => route.fulfill(json({})))
  await page.route('**/api/setup/status', (route) => route.fulfill(json({ setup_completed: true })))
  await page.route('**/api/settings/feature-flags', (route) => route.fulfill(json({ claude_auth_configured: claude })))
  await page.route('**/api/users/me', (route) => route.fulfill(json({ email: 'e2e@example.com', role })))
  await page.route(`${API}/my-agents*`, (route) => route.fulfill(json({
    client_email: 'e2e@example.com',
    agents,
    multi_agent_chat_available: true,
    orchestration_available: orchestration,
  })))
  await page.route(`${API}/sessions*`, (route) => route.fulfill(json({ sessions: [] })))
  await page.route(`${API}/chat-state*`, (route) => route.fulfill(json({ chats: [] })))
  await page.route(`${API}/asks*`, (route) => route.fulfill(json({ asks: [] })))
  await page.route(`${API}/agents/${guide}/canvas/build`, (route) => (state
    ? route.fulfill(json(canvas(state)))
    : route.fulfill(json({ detail: 'not found' }, 404))))
  await page.route(`**/api/agents/${guide}/canvas/company-research`, (route) => route.fulfill(json({ detail: 'not found' }, 404)))
  await page.route(`${API}/agents/${guide}/canvas/company-research`, (route) => route.fulfill(json({ detail: 'not found' }, 404)))
  await page.route(`**/api/agents/${guide}/task`, (route) => {
    tasks.push(route.request().postDataJSON())
    return route.fulfill(json({ status: 'accepted', execution_id: 'exec-1' }))
  })
  await page.route(`**/api/agents/${guide}/executions/*`, (route) => route.fulfill(json({ id: 'exec-1', status: 'running' })))
  await page.route(`${API}/agents/${guide}/chat/stream`, (route) => {
    sent.push(route.request().postDataJSON())
    return route.fulfill(json({ detail: 'mocked' }, 503))
  })
  await page.route(`${API}/agents/${guide}/chat`, (route) => {
    sent.push(route.request().postDataJSON())
    return route.fulfill(json({ detail: 'mocked' }, 503))
  })
  return sent
}

// Presses through a level moment if the layout shows one (a card not whole on screen does).
async function passMomentIfShown(page) {
  const moment = page.getByTestId('build-moment')
  await page.waitForTimeout(800)
  if (await moment.isVisible()) await passMoment(moment)
}

// Steps through a level moment (one or two stops) so it doesn't cover what a test clicks.
async function passMoment(moment) {
  for (let i = 0; i < 3 && await moment.isVisible(); i++) await moment.getByTestId('build-tour-next').click()
}

const GUIDE_CARD = { name: GUIDE, owned: true, display_label: 'Build Guide', description: 'e2e fixture', availability: 'ready', playbooks: [] }

test.describe('Workspace Build @smoke', () => {
  // The tour has its own test. Everywhere else it just gets out of the way.
  test.beforeEach(async ({ page }, info) => {
    if (!info.title.includes('tour')) {
      await page.addLocatorHandler(page.getByTestId('build-tour-skip'), (skip) => skip.click())
    }
  })
  // The level moments have their own tests. Everywhere else they are stepped through.
  test.beforeEach(async ({ page }, info) => {
    if (!info.title.includes('moment')) await page.addLocatorHandler(page.getByTestId('build-moment'), passMoment)
  })

  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
  })

  test('an admin with nothing here yet gets the one setup panel, not a second flow', async ({ page }) => {
    await mockWorkspace(page, { agents: [GUIDE_CARD], role: 'admin', claude: false })
    await page.goto('/workspace')
    await expect(page).toHaveURL(/onboarding=1.*from=workspace/)
    await expect(page.getByTestId('build-onboarding')).toHaveCount(0)
  })

  test('a fresh install counts as new, even with its sample agents running', async ({ page }) => {
    await mockWorkspace(page, { agents: [GUIDE_CARD, { name: 'cornelius', availability: 'ready', playbooks: [] }] })
    await page.route('**/api/onboarding/first-run', (route) => route.fulfill(json({ first_run: true, seeded_agents: ['cornelius'], own_agent_count: 0 })))
    await page.goto('/workspace')
    await expect(page.getByTestId('build-onboarding')).toBeVisible()
  })

  test('an admin with agents of their own keeps the usual Workspace', async ({ page }) => {
    await mockWorkspace(page, { agents: [GUIDE_CARD, { name: 'finance-close', availability: 'ready', playbooks: [] }], role: 'admin' })
    await page.goto('/workspace')
    await expect(page).toHaveURL(/\/workspace\/inbox/)
  })

  test('the team can be reached from the sidebar, the page and onboarding', async ({ page }) => {
    await mockWorkspace(page, { agents: [GUIDE_CARD] })
    await page.goto('/workspace')
    await page.getByTestId('build-onboarding-help').click()
    const help = page.getByTestId('build-help')
    await expect(help).toBeVisible()
    await expect(page.getByTestId('build-help-email')).toHaveAttribute('href', /^mailto:support@ability\.ai\?subject=/)
    await expect(page.getByTestId('build-help-docs')).toHaveAttribute('href', 'https://docs.ability.ai')
    await expect(page.getByTestId('build-help-workshops')).toHaveAttribute('href', 'https://www.ability.ai/workshops')
    await page.getByTestId('build-help-close').click()
    await expect(help).toHaveCount(0)
    await page.getByTestId('build-onboarding-skip').click()
    await page.getByTestId('sidebar-get-help').click()
    await page.getByTestId('build-help-intro').click()
    await expect(page.locator('[data-testid="intro-video-file"], [data-testid="intro-video"]').first()).toBeVisible()
    await page.getByTestId('build-help-close').click()
    await page.getByTestId('sidebar-build').click()
    await page.getByTestId('build-world-get-help').click()
    await expect(help).toBeVisible()
  })

  test('a first visit opens the window on any page, and it cannot be dismissed', async ({ page }) => {
    await mockWorkspace(page, { agents: [GUIDE_CARD] })
    await page.goto('/workspace')
    const dialog = page.getByTestId('build-onboarding')
    await expect(dialog).toBeVisible()
    await expect(dialog).toContainText("Let's get started")
    await page.keyboard.press('Escape')
    await page.mouse.click(5, 5)
    await expect(dialog).toBeVisible()
  })

  test('the window walks through you, the company and the audit, then starts the chat', async ({ page }) => {
    const sent = await mockWorkspace(page, { agents: [GUIDE_CARD] })
    await page.goto('/workspace/build')
    await page.getByTestId('build-onboarding-start').click()
    await page.getByTestId('build-onboarding-input').fill('Head of Marketing')
    await page.getByTestId('build-onboarding-continue').click()
    await expect(page.getByTestId('build-role')).toContainText('Head of Marketing')
    await page.getByRole('button', { name: 'None' }).click()
    await page.getByRole('button', { name: "I've never built one" }).click()
    await page.getByTestId('what-is-an-agent-done').click()
    await page.getByRole('button', { name: 'Not yet' }).click()
    await page.getByTestId('build-onboarding-input').fill('Acme Freight')
    await page.keyboard.press('Enter')
    await expect(page.getByTestId('build-company')).toHaveText('Acme Freight')
    await page.getByTestId('build-onboarding-input').fill('acmefreight.com')
    await page.keyboard.press('Enter')

    const audit = page.getByTestId('build-onboarding-audit')
    await expect(audit).toContainText("You told us you're Head of Marketing")
    await expect(audit).toContainText("don't need any technical knowledge")
    expect(sent).toHaveLength(0)
    await page.getByTestId('build-onboarding-audit-start').click()
    await expect(page.getByTestId('build-onboarding')).toHaveCount(0)
    await expect(page.getByTestId('build-level')).toContainText('explains each step')
    // The first message waits for the tour, and acting on the page dismisses it.
    await expect(async () => {
      await page.getByTestId('portal-build').hover()
      expect(sent.length).toBe(1)
    }).toPass()
    expect(sent[0].message).toContain('My role: Head of Marketing.')
    expect(sent[0].message).toContain('Company website: acmefreight.com.')
    expect(sent[0].message).toContain('Starting level: newcomer')
    expect(sent[0].message).toContain("AI audit: yes")
  })

  test('skipping the audit asks for any agent as a test, and offers the audit again', async ({ page }) => {
    const sent = await mockWorkspace(page, { agents: [GUIDE_CARD] })
    await page.goto('/workspace')
    await page.getByTestId('build-onboarding-start').click()
    await page.getByTestId('build-onboarding-input').fill('Head of Operations')
    await page.keyboard.press('Enter')
    await page.getByRole('button', { name: 'None' }).click()
    await page.getByRole('button', { name: "I've never built one" }).click()
    await page.getByTestId('what-is-an-agent-done').click()
    await page.getByRole('button', { name: 'Not yet' }).click()
    await page.getByTestId('build-onboarding-input').fill('Acme Freight')
    await page.keyboard.press('Enter')
    await page.getByTestId('build-onboarding-no-website').click()
    await page.getByTestId('build-onboarding-audit-skip').click()
    await expect(page).toHaveURL(/\/workspace\/build$/)
    // The first message waits for the tour, and acting on the page dismisses it.
    await expect(async () => {
      await page.getByTestId('portal-build').hover()
      expect(sent.length).toBe(1)
    }).toPass()
    expect(sent[0].message).toMatch(/AI audit: skipped.*test/)
    await expect(page.getByTestId('build-audit-nudge')).toContainText('You skipped the AI audit')
    await expect(page.getByTestId('sidebar-profile-incomplete')).toHaveCount(0)
  })

  test('Back returns to an answer, and a reload picks up at the same question', async ({ page }) => {
    await mockWorkspace(page, { agents: [GUIDE_CARD] })
    await page.goto('/workspace')
    await page.getByTestId('build-onboarding-start').click()
    await page.getByTestId('build-onboarding-input').fill('Head of Operations')
    await page.keyboard.press('Enter')
    await expect(page.getByTestId('build-onboarding-software')).toBeVisible()
    await page.getByTestId('build-onboarding-back').click()
    await expect(page.getByTestId('build-onboarding-input')).toHaveValue('Head of Operations')
    await page.reload()
    await expect(page.getByTestId('build-onboarding-software')).toBeVisible()
  })

  test('skipping shows the profile in red, and the red opens the window again', async ({ page }) => {
    const sent = await mockWorkspace(page, { agents: [GUIDE_CARD] })
    await page.goto('/workspace')
    await page.getByTestId('build-onboarding-skip').click()
    await expect(page.getByTestId('build-onboarding')).toHaveCount(0)
    const red = page.getByTestId('sidebar-profile-incomplete')
    await expect(red).toBeVisible()
    expect(sent).toHaveLength(0)

    await page.reload()
    await expect(page.getByTestId('build-onboarding')).toHaveCount(0)
    await expect(red).toBeVisible()

    await page.getByTestId('sidebar-build').click()
    await expect(page.getByTestId('build-profile-incomplete')).toBeVisible()
    await expect(page.getByTestId('build-audit-nudge')).toBeVisible()
    await expect(page.locator('[data-step-state="incomplete"]')).toHaveCount(1)
    // The first message waits for the tour, and acting on the page dismisses it.
    await expect(async () => {
      await page.getByTestId('portal-build').hover()
      expect(sent.length).toBe(1)
    }).toPass()
    expect(sent[0].message).toMatch(/skipped the setup questions/i)

    await page.getByTestId('build-step-finish-profile').click()
    await page.getByTestId('build-onboarding-start').click()
    await page.getByTestId('build-onboarding-input').fill('Head of Operations')
    await page.keyboard.press('Enter')
    await page.getByRole('button', { name: 'None' }).click()
    await page.getByRole('button', { name: "I've never built one" }).click()
    await page.getByTestId('what-is-an-agent-done').click()
    await page.getByRole('button', { name: 'Not yet' }).click()
    await page.getByTestId('build-onboarding-input').fill('Acme Freight')
    await page.keyboard.press('Enter')
    await page.getByTestId('build-onboarding-no-website').click()
    await page.getByTestId('build-onboarding-audit-start').click()

    await expect(red).toHaveCount(0)
    await expect(page.getByTestId('build-profile-incomplete')).toHaveCount(0)
    await expect.poll(() => sent.length).toBe(2)
    expect(sent[1].message).toContain('Acme Freight')
    expect(sent[1].message).toContain('AI audit: yes')
  })

  test('the research starts with the company, runs beside the chat, and shows under Findings', async ({ page }) => {
    const sent = await mockWorkspace(page, { agents: [GUIDE_CARD] })
    await page.goto('/workspace')
    await page.getByTestId('build-onboarding-start').click()
    await page.getByTestId('build-onboarding-input').fill('Head of Marketing')
    await page.keyboard.press('Enter')
    await page.getByRole('button', { name: 'None' }).click()
    await page.getByRole('button', { name: "I've never built one" }).click()
    await page.getByTestId('what-is-an-agent-done').click()
    await page.getByRole('button', { name: 'Not yet' }).click()
    await page.getByTestId('build-onboarding-input').fill('Acme Freight')
    await page.keyboard.press('Enter')
    expect(sent.tasks).toHaveLength(0)
    await page.getByTestId('build-onboarding-input').fill('acmefreight.com')
    await page.keyboard.press('Enter')
    await expect.poll(() => sent.tasks.length).toBe(1)
    expect(sent.tasks[0]).toMatchObject({ async_mode: true, message: '/company-research Company: Acme Freight. Website: acmefreight.com.' })

    await page.getByTestId('build-onboarding-audit-start').click()
    // The first message waits for the tour, and acting on the page dismisses it.
    await expect(async () => {
      await page.getByTestId('portal-build').hover()
      expect(sent.length).toBe(1)
    }).toPass()
    expect(sent[0].message).toContain('Company research: running in the background.')
    await page.getByRole('button', { name: /Findings/ }).click()
    await expect(page.getByTestId('build-research')).toContainText('keeps going while you talk')

    await page.route(`**/api/enterprise/client-portal/agents/${GUIDE}/canvas/company-research`, (route) => route.fulfill(json({
      id: 'company-research', updated_at: '2026-11-01T10:00:00Z', blocks: [{ id: 'report', kind: 'markdown', markdown: '## Competitors\n\nBig Freight Co' }],
    })))
    await page.reload()
    await page.getByRole('button', { name: /Findings/ }).click()
    await expect(page.getByTestId('build-research-report')).toContainText('Big Freight Co')
    expect(sent.tasks).toHaveLength(1)
  })

  test('a research job that fails says so, and can be tried again', async ({ page }) => {
    const sent = await mockWorkspace(page, { agents: [GUIDE_CARD] })
    await page.route(`**/api/agents/${GUIDE}/executions/*`, (route) => route.fulfill(json({ id: 'exec-1', status: 'failed', error: 'Authentication failure: Not logged in' })))
    await page.goto('/workspace')
    await page.getByTestId('build-onboarding-start').click()
    await page.getByTestId('build-onboarding-input').fill('Head of Operations')
    await page.keyboard.press('Enter')
    await page.getByRole('button', { name: 'None' }).click()
    await page.getByRole('button', { name: "I've never built one" }).click()
    await page.getByTestId('what-is-an-agent-done').click()
    await page.getByRole('button', { name: 'Not yet' }).click()
    await page.getByTestId('build-onboarding-input').fill('Acme Freight')
    await page.keyboard.press('Enter')
    await page.getByTestId('build-onboarding-no-website').click()
    await expect.poll(() => sent.tasks.length).toBe(1)
    await page.getByTestId('build-onboarding-audit-start').click()
    await page.getByRole('button', { name: /Findings/ }).click()
    await expect(page.getByTestId('build-research-failed')).toContainText("Claude isn't answering")
    await page.getByTestId('build-research-retry').click()
    await expect.poll(() => sent.tasks.length).toBe(2)
  })

  test('the tour walks round the Workspace the first time, then stays away', async ({ page }) => {
    await mockWorkspace(page, { agents: [GUIDE_CARD], state: STATE })
    await page.goto('/workspace/build')

    await expect(page.getByTestId('build-tour')).toBeVisible()
    await expect(page.getByTestId('build-tour-body')).toContainText('Build Guide is yours: it helps you find the best agents to build, and builds them with you.')
    for (const title of ['This is where you talk', 'This is your company page', 'This is your Inbox', 'You can always get help']) {
      await page.getByTestId('build-tour-next').click()
      await expect(page.locator('#build-tour-title')).toHaveText(title)
    }
    await page.getByTestId('build-tour-next').click()
    await expect(page.getByTestId('build-tour')).toHaveCount(0)

    await page.getByTestId('sidebar-get-help').click()
    await page.getByTestId('build-help-tour').click()
    await expect(page.getByTestId('build-tour')).toBeVisible()
  })

  test('the world draws what the guide reports, follows it, and zooms in', async ({ page }) => {
    await mockWorkspace(page, { agents: [GUIDE_CARD], state: { ...STATE, focus: { agent: 'email-triage', skill: 'draft-reply' } } })
    await page.goto('/workspace/build')

    await expect(page.getByTestId('build-agent-detail')).toBeVisible()
    await expect(page.getByTestId('build-agent-variables')).toContainText('GOOGLE_OAUTH_TOKEN')
    await expect(page.getByTestId('build-agent-schedules')).toContainText('Off until you turn it on')
    await expect(page.getByText('Drafts a reply using context.md.')).toBeVisible()

    await page.getByTestId('build-world-back').click()
    await expect(page.getByTestId('build-world-map')).toBeVisible()
    await expect(page.getByTestId('build-zone')).toHaveCount(2)
    await expect(page.getByTestId('build-orchestrator')).toContainText('Not on this plan')
    await page.getByTestId('build-agent-email-triage').click()
    await expect(page.getByTestId('build-agent-detail')).toBeVisible()

    await page.getByRole('button', { name: /Findings/ }).click()
    await expect(page.getByTestId('build-findings')).toContainText('Triage customer emails')
    await expect(page.getByTestId('build-data-sources')).toContainText('Carrier sheet')
    await page.getByRole('button', { name: 'Context' }).click()
    await expect(page.getByTestId('build-context')).toContainText('proof of delivery')
  })

  test('an agent the guide calls live is checked against the real roster', async ({ page }) => {
    const live = { ...STATE, stage: 'live', agents: [{ ...STATE.agents[0], status: 'live', live_name: 'not-on-this-instance' }], focus: { agent: 'email-triage' } }
    await mockWorkspace(page, { agents: [GUIDE_CARD], state: live })
    await page.goto('/workspace/build')
    await expect(page.getByTestId('build-agent-not-found')).toBeVisible()
    await expect(page.getByTestId('build-agent-open')).toHaveCount(0)
  })

  test('a real agent offers its chat and where to add its keys', async ({ page }) => {
    const ready = { ...STATE, agents: [{ ...STATE.agents[0], status: 'ready', live_name: 'email-triage' }], focus: { agent: 'email-triage' } }
    await mockWorkspace(page, { agents: [GUIDE_CARD, { name: 'email-triage', availability: 'stopped', playbooks: [] }], state: ready })
    await page.goto('/workspace/build')
    await expect(page.getByTestId('build-agent-open')).toHaveAttribute('href', '/workspace/a/email-triage')
    await expect(page.getByTestId('build-agent-add-keys')).toHaveAttribute('href', '/agents/email-triage?tab=credentials')
    await expect(page.getByTestId('build-agent-detail')).toContainText('Stopped')
  })

  test('the chat stays on this page: nothing in its header leads away', async ({ page }) => {
    await mockWorkspace(page, { agents: [GUIDE_CARD], state: { ...STATE, focus: {} } })
    await page.goto('/workspace/build')
    await expect(page.getByTestId('portal-focused-agent')).toContainText('Build Guide')
    await expect(page.getByTestId('portal-agent-picker')).toHaveCount(0)
    await expect(page.getByTestId('new-chat-header')).toHaveCount(0)
    await expect(page.getByTestId('portal-voice-call')).toHaveCount(0)
  })

  test('Build this hands the idea to the chat without sending it', async ({ page }) => {
    const sent = await mockWorkspace(page, { agents: [GUIDE_CARD], state: { ...STATE, focus: {} } })
    await page.goto('/workspace/build')
    await page.getByRole('button', { name: /Findings/ }).click()
    await page.getByTestId('build-this').first().click()
    await expect(page.locator('main textarea').first()).toHaveValue("Let's build this: Triage customer emails.")
    expect(sent).toHaveLength(0)
  })

  test('the orchestrator card offers design only where the instance allows it', async ({ page }) => {
    await mockWorkspace(page, { agents: [GUIDE_CARD], state: { ...STATE, focus: {} }, orchestration: true })
    await page.goto('/workspace/build')
    const card = page.getByTestId('build-orchestrator')
    await expect(card).not.toContainText('Not on this plan')
    await card.getByRole('button', { name: 'Design one' }).click()
    await expect(page.locator('main textarea').first()).toHaveValue(/Orchestrators are turned on/)
  })

  test('on a phone, chat and world are one tap apart', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 780 })
    await mockWorkspace(page, { agents: [GUIDE_CARD], state: { ...STATE, focus: {} } })
    await page.goto('/workspace/build')
    await expect(page.getByTestId('build-world')).toBeHidden()
    await page.getByRole('tab', { name: 'Your company' }).click()
    await expect(page.getByTestId('build-world')).toBeVisible()
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
    expect(overflow).toBeLessThanOrEqual(0)
  })

  test('agents already on the instance are part of the world', async ({ page }) => {
    await mockWorkspace(page, {
      agents: [GUIDE_CARD, { name: 'agent-builder', availability: 'ready', playbooks: [] }, { name: 'finance-close', display_label: 'Finance Close', availability: 'ready', playbooks: [] }],
      state: { ...STATE, agents: [], focus: {} },
    })
    await page.goto('/workspace/build')
    const existing = page.getByTestId('build-instance-agents')
    await expect(existing).toContainText('Finance Close')
    await expect(existing).not.toContainText('agent-builder')
  })

  test('an instance with agents of its own keeps its usual Workspace', async ({ page }) => {
    await mockWorkspace(page, { agents: [GUIDE_CARD, { name: 'finance-close', availability: 'ready', playbooks: [] }] })
    await page.goto('/workspace')
    await expect(page).toHaveURL(/\/workspace\/inbox/)
    await expect(page.getByTestId('build-onboarding')).toHaveCount(0)
    await page.getByTestId('sidebar-build').click()
    await expect(page.getByTestId('build-onboarding')).toBeVisible()
  })

  test('a guide that already knows them means no window', async ({ page }) => {
    await mockWorkspace(page, { agents: [GUIDE_CARD], state: { ...STATE, focus: {} } })
    await page.goto('/workspace')
    await expect(page).toHaveURL(/\/workspace\/inbox/)
    await page.getByTestId('sidebar-build').click()
    await expect(page.getByTestId('portal-build')).toBeVisible()
    await expect(page.getByTestId('build-onboarding')).toHaveCount(0)
  })

  async function answerYou(page) {
    await page.getByTestId('build-onboarding-start').click()
    await page.getByTestId('build-onboarding-input').fill('Head of Operations')
    await page.keyboard.press('Enter')
    await page.getByRole('button', { name: 'None' }).click()
    await page.getByRole('button', { name: "I've never built one" }).click()
    await page.getByTestId('what-is-an-agent-done').click()
    await page.getByRole('button', { name: 'Not yet' }).click()
  }

  async function answerCompany(page) {
    await page.getByTestId('build-onboarding-input').fill('Acme Freight')
    await page.keyboard.press('Enter')
    await page.getByTestId('build-onboarding-no-website').click()
  }

  test('the window connects Claude before the company, and nothing runs until it is', async ({ page }) => {
    const sent = await mockWorkspace(page, { agents: [GUIDE_CARD], claude: false })
    await page.goto('/workspace')
    await answerYou(page)
    await expect(page.getByTestId('build-onboarding-connect')).toBeVisible()
    await expect(page.getByTestId('build-onboarding-connect-open')).toHaveAttribute('href', '/?onboarding=1')

    await page.getByTestId('build-onboarding-connect-recheck').click()
    await expect(page.getByTestId('build-onboarding-connect-missing')).toBeVisible()
    expect(sent).toHaveLength(0)
    expect(sent.tasks).toHaveLength(0)

    await page.route('**/api/settings/feature-flags', (route) => route.fulfill(json({ claude_auth_configured: true })))
    await page.getByTestId('build-onboarding-connect-recheck').click()
    await answerCompany(page)
    await expect.poll(() => sent.tasks.length).toBe(1)
    await page.getByTestId('build-onboarding-audit-start').click()
    await expect(page).toHaveURL(/\/workspace\/build$/)
    // The first message waits for the tour, and acting on the page dismisses it.
    await expect(async () => {
      await page.getByTestId('portal-build').hover()
      expect(sent.length).toBe(1)
    }).toPass()
    expect(sent[0].message).toContain('Acme Freight')

    await page.reload()
    await expect(page.locator('main textarea').first()).toBeVisible()
    expect(sent).toHaveLength(1)
    expect(sent.tasks).toHaveLength(1)
  })

  test('leaving before the company leaves the profile red, and the page still asks for Claude', async ({ page }) => {
    await mockWorkspace(page, { agents: [GUIDE_CARD], claude: false })
    await page.goto('/workspace')
    await answerYou(page)
    await page.getByTestId('build-onboarding-skip').click()
    await expect(page.getByTestId('sidebar-profile-incomplete')).toBeVisible()
    await page.getByTestId('sidebar-build').click()
    await expect(page.getByTestId('build-connect')).toBeVisible()
    await expect(page.getByTestId('build-onboarding')).toHaveCount(0)
  })

  test('the window sets up the guide before the company when there is none', async ({ page }) => {
    await mockWorkspace(page, { agents: [] })
    let deployed = null
    await page.route('**/api/systems/deploy', (route) => {
      deployed = route.request().postDataJSON()
      return route.fulfill(json({ system_name: 'workspace-build', agents_created: [GUIDE] }))
    })
    await page.goto('/workspace')
    await answerYou(page)
    await page.getByTestId('build-onboarding-deploy').click()
    await expect.poll(() => deployed?.manifest || '').toContain('template: local:build-guide')
  })

  test("a person's Atlas without permission to create agents is called out", async ({ page }) => {
    await mockWorkspace(page, { agents: [GUIDE_CARD] })
    let granted = false
    await page.route(`**/api/agents/${GUIDE}/capability-grants`, (route) => route.fulfill(json({
      agent_name: GUIDE, grants: [{ capability: 'agents.manage', granted }],
    })))
    await page.goto('/workspace/build')
    await expect(page.getByTestId('build-grant-held-back')).toContainText("Build Guide can't create agents yet. An administrator needs to turn on \"Manage other agents\" for it")
    granted = true
    await page.reload()
    await expect(page.getByTestId('build-onboarding')).toBeVisible()
    await expect(page.getByTestId('build-grant-held-back')).toHaveCount(0)
  })

  test('the sidebar door opens the page', async ({ page }) => {
    await mockWorkspace(page, { agents: [GUIDE_CARD] })
    await page.goto('/workspace/inbox')
    await page.getByTestId('build-onboarding-skip').click()
    await page.getByTestId('sidebar-build').click()
    await expect(page).toHaveURL(/\/workspace\/build$/)
    await expect(page.getByTestId('portal-build')).toBeVisible()
  })
  // ------------------------------------------------------------ routes by experience

  test("a newcomer sees what an agent is right after saying so, and someone who has built a few doesn't", async ({ page }) => {
    await mockWorkspace(page, { agents: [GUIDE_CARD] })
    await page.goto('/workspace')
    await page.getByTestId('build-onboarding-start').click()
    await page.getByTestId('build-onboarding-input').fill('Head of Operations')
    await page.keyboard.press('Enter')
    await page.getByRole('button', { name: 'None' }).click()
    await page.getByRole('button', { name: "I've never built one" }).click()
    const dialog = page.getByTestId('build-onboarding')
    await expect(dialog.getByTestId('what-is-an-agent-video')).toHaveAttribute('src', '/intro/what-is-an-agent.mp4')
    await expect(dialog.getByTestId('build-onboarding-progress')).toHaveText('Step 5 of 9')

    await page.getByTestId('build-onboarding-back').click()
    await page.getByRole('button', { name: "I've built a few" }).click()
    await expect(dialog.getByTestId('what-is-an-agent')).toHaveCount(0)
    await expect(dialog.getByTestId('build-onboarding-plans')).toContainText("Do you know what you'd like to build first?")
  })

  test('knowing exactly what to build makes talking it through with Atlas the main button', async ({ page }) => {
    const sent = await mockWorkspace(page, { agents: [GUIDE_CARD] })
    await page.goto('/workspace')
    await page.getByTestId('build-onboarding-start').click()
    await page.getByTestId('build-onboarding-input').fill('Head of Operations')
    await page.keyboard.press('Enter')
    await page.getByRole('button', { name: 'None' }).click()
    await page.getByRole('button', { name: "I've built a few" }).click()
    await page.getByRole('button', { name: 'I know exactly' }).click()
    await page.getByTestId('build-onboarding-input').fill('Acme Freight')
    await page.keyboard.press('Enter')
    await page.getByTestId('build-onboarding-no-website').click()

    const audit = page.getByTestId('build-onboarding-audit')
    await expect(audit).toContainText('The AI audit is still recommended')
    await expect(page.getByTestId('build-onboarding-audit-agent')).toHaveText('Talk it through with Atlas')
    await expect(page.getByTestId('build-onboarding-audit-start')).toBeVisible()
    await page.getByTestId('build-onboarding-audit-agent').click()
    await expect(page).toHaveURL(/\/workspace\/build$/)
    // The first message waits for the tour, and acting on the page dismisses it.
    await expect(async () => {
      await page.getByTestId('portal-build').hover()
      expect(sent.length).toBe(1)
    }).toPass()
    expect(sent[0].message).toContain('What to build first: I know exactly.')
    expect(sent[0].message).toContain('AI audit: skipped for now. First agent: from scratch.')
  })

  test("with every question answered, the red profile opens Atlas's chat, not the questions", async ({ page }) => {
    const sent = await mockWorkspace(page, { agents: [GUIDE_CARD] })
    await page.goto('/workspace')
    await page.getByTestId('build-onboarding-start').click()
    await page.getByTestId('build-onboarding-input').fill('Head of Operations')
    await page.keyboard.press('Enter')
    await page.getByRole('button', { name: 'None' }).click()
    await page.getByRole('button', { name: "I've built a few" }).click()
    await page.getByRole('button', { name: 'Not yet' }).click()
    await page.getByTestId('build-onboarding-input').fill('Acme Freight')
    await page.keyboard.press('Enter')
    await page.getByTestId('build-onboarding-no-website').click()
    await page.getByTestId('build-onboarding-audit-skip').click()
    await expect(async () => {
      await page.getByTestId('portal-build').hover()
      expect(sent.length).toBe(1)
    }).toPass()

    // Atlas has written a state, and hasn't marked the profile complete yet.
    const notYet = { ...STATE, stage: 'profile', profile_complete: false, audit: { status: 'not_started', summary: '' }, agents: [], focus: {} }
    await page.route(`${API}/agents/${GUIDE}/canvas/build`, (route) => route.fulfill(json(canvas(notYet))))
    await page.reload()
    await expect(page.getByTestId('sidebar-profile-incomplete')).toBeVisible()
    await page.getByTestId('build-step-finish-profile').click()
    await expect(page.getByTestId('build-onboarding')).toHaveCount(0)
    await expect.poll(() => sent.length).toBe(2)
    // Atlas already has their answers, so this asks for the talk and nothing else.
    expect(sent[1].message).toBe("Profile: I'd like to tell you about my work first, so you know what my job involves.")
  })

  // The main app restores a session only from a token that names its sign-in mode, and the
  // Dashboard reads lists that the catch-all mock answers with an empty object.
  async function asAdminOnTheDashboard(page) {
    const b64 = (o) => Buffer.from(JSON.stringify(o)).toString('base64url')
    const token = `${b64({ alg: 'none', typ: 'JWT' })}.${b64({ sub: 'admin', mode: 'admin', exp: 4102444800 })}.x`
    await page.addInitScript((t) => localStorage.setItem('token', t), token)
    const isPath = (path) => (url) => url.pathname === path
    await page.route(isPath('/api/agents'), (route) => route.fulfill(json([])))
    await page.route(isPath('/api/activities/timeline'), (route) => route.fulfill(json({ activities: [] })))
    await page.route(isPath('/api/agents/context-stats'), (route) => route.fulfill(json({ agents: [] })))
    await page.route(isPath('/api/agents/execution-stats'), (route) => route.fulfill(json({ agents: [] })))
    await page.route(isPath('/api/system-views'), (route) => route.fulfill(json([])))
    await page.route(isPath('/api/tags'), (route) => route.fulfill(json([])))
  }

  // An admin answers in the setup panel. Continue moves on; on the audit step they skip it.
  async function panelUntil(page, key) {
    for (let i = 0; i < 14 && !(await page.getByTestId(`first-run-step-${key}`).count()); i++) {
      await page.locator('[data-testid^="first-run-next-"]').click()
    }
    await expect(page.getByTestId(`first-run-step-${key}`)).toBeVisible()
  }

  test('an experienced admin who skips the audit ends on the Dashboard with the feature tour, once', async ({ page }) => {
    await mockWorkspace(page, { agents: [GUIDE_CARD], role: 'admin' })
    await asAdminOnTheDashboard(page)
    await page.goto('/?onboarding=1')
    await expect(page.getByTestId('first-run-overlay')).toBeVisible()
    await panelUntil(page, 'profile')
    await page.getByTestId('first-run-question-input').fill('CTO')
    await page.getByRole('button', { name: 'Save' }).click()
    await page.getByRole('button', { name: "I'm a developer" }).click()
    await page.getByRole('button', { name: 'I build them all the time' }).click()
    await expect(page.getByTestId('what-is-an-agent')).toHaveCount(0)
    await page.getByTestId('first-run-choices-plans').getByRole('button', { name: 'Not yet' }).click()
    await panelUntil(page, 'company')
    await page.getByTestId('first-run-question-input').fill('Acme Freight')
    await page.getByRole('button', { name: 'Save' }).click()
    await page.getByTestId('first-run-no-website').click()
    await panelUntil(page, 'audit')
    // Answered and experienced, so the tour is due, but never over the panel.
    await expect(page.getByTestId('first-run-audit-profile')).toHaveText('Or just tell Atlas about your work (about 2 minutes)')
    await expect(page.getByTestId('build-tour')).toHaveCount(0)
    await page.getByTestId('first-run-skip-audit').click()
    await panelUntil(page, 'done')
    await expect(page.getByTestId('first-run-step-done')).toContainText("shows you round Trinity's dashboard")
    await expect(page.getByTestId('build-tour')).toHaveCount(0)
    await page.getByTestId('first-run-next-done').click()

    await expect(page.getByTestId('first-run-overlay')).toHaveCount(0)
    await expect(page).toHaveURL(/\/$/)
    await expect(page.getByTestId('build-tour')).toBeVisible()
    await expect(page.locator('#build-tour-title')).toHaveText('Your agents')
    await page.getByTestId('build-tour-skip').click()
    await expect(page.getByTestId('build-tour')).toHaveCount(0)

    await page.reload()
    await expect(page.getByTestId('view-mode-switcher')).toBeVisible()
    await expect(page.getByTestId('build-tour')).toHaveCount(0)

    // Asked for again from Get help: shown, and consumed, so a reload doesn't repeat it.
    await page.goto('/?tour=features')
    await expect(page.getByTestId('build-tour')).toBeVisible()
    await expect(page).not.toHaveURL(/tour=features/)
    await page.getByTestId('build-tour-skip').click()
    await page.reload()
    await expect(page.getByTestId('view-mode-switcher')).toBeVisible()
    await expect(page.getByTestId('build-tour')).toHaveCount(0)
  })

  test('at phone width the Dashboard has no feature tour, stays usable, and counts it as seen', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    const intake = {
      current: null, answers: { role: 'CTO', software: 'developer', agents: 'built', company: 'Acme Freight' },
      done: true, skipped: false, sent: false, finished: true, audit: 'skip', savedAt: Date.now(),
    }
    await page.addInitScript((value) => {
      for (const email of ['', 'tester@example.com', 'e2e@example.com']) {
        localStorage.setItem(`trinity-workspace-onboarding:${email}`, value)
      }
    }, JSON.stringify(intake))
    await mockWorkspace(page, { agents: [GUIDE_CARD], role: 'admin' })
    await asAdminOnTheDashboard(page)
    await page.goto('/')
    await expect(page.getByTestId('view-mode-switcher')).toBeVisible()
    await expect.poll(() => page.evaluate(() => Object.keys(localStorage)
      .filter((k) => k.startsWith('trinity-workspace-onboarding:'))
      .some((k) => JSON.parse(localStorage.getItem(k)).featureTourSeen === true))).toBe(true)
    await expect(page.getByTestId('build-tour')).toHaveCount(0)
    await page.getByTestId('view-mode-switcher').getByRole('button').last().click()
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
    expect(overflow).toBeLessThanOrEqual(0)
  })
  // ------------------------------------------------------------ after the final review

  // Answers saved in this browser, as if they had finished setup earlier.
  async function seedAnswers(page, intake) {
    await page.addInitScript((value) => {
      for (const email of ['', 'tester@example.com', 'e2e@example.com']) {
        localStorage.setItem(`trinity-workspace-onboarding:${email}`, value)
      }
    }, JSON.stringify({ current: null, done: true, skipped: false, finished: true, savedAt: Date.now(), ...intake }))
  }

  // Whether the Dashboard's feature tour was ever drawn, however briefly.
  async function recordDashboardTour(page) {
    await page.addInitScript(() => {
      window.__dashboardTourDrawn = false
      new MutationObserver(() => {
        if (location.pathname === '/' && document.querySelector('[data-testid="build-tour"]')) window.__dashboardTourDrawn = true
      }).observe(document, { childList: true, subtree: true })
    })
  }

  async function answerPanelAsExperienced(page) {
    await panelUntil(page, 'profile')
    await page.getByTestId('first-run-question-input').fill('CTO')
    await page.getByRole('button', { name: 'Save' }).click()
    await page.getByRole('button', { name: "I'm a developer" }).click()
    await page.getByRole('button', { name: 'I build them all the time' }).click()
    await page.getByTestId('first-run-choices-plans').getByRole('button', { name: 'Not yet' }).click()
    await panelUntil(page, 'company')
    await page.getByTestId('first-run-question-input').fill('Acme Freight')
    await page.getByRole('button', { name: 'Save' }).click()
    await page.getByTestId('first-run-no-website').click()
    await panelUntil(page, 'audit')
  }

  // Setup opens by itself (no `?onboarding=1` in the address): a first run with no agent yet.
  async function setupOpensByItself(page) {
    await page.addInitScript(() => localStorage.removeItem('trinity_first_run_closed'))
    await page.route('**/api/onboarding/first-run', (route) => route.fulfill(json({ first_run: true, seeded_agents: [], own_agent_count: 0 })))
  }

  test('an experienced admin who starts the audit goes to Atlas without the Dashboard tour flashing up', async ({ page }) => {
    await mockWorkspace(page, { agents: [GUIDE_CARD], role: 'admin' })
    await asAdminOnTheDashboard(page)
    await setupOpensByItself(page)
    await recordDashboardTour(page)
    await page.goto('/')
    await answerPanelAsExperienced(page)
    await page.getByTestId('first-run-audit-start').click()
    await page.getByTestId('confirm-dialog-confirm').click()
    await expect(page).toHaveURL(/\/workspace\/build$/)
    expect(await page.evaluate(() => window.__dashboardTourDrawn)).toBe(false)
  })

  test('the Dashboard tour waits while setup opens by itself, and never shows under it', async ({ page }) => {
    await seedAnswers(page, { answers: { role: 'CTO', software: 'developer', agents: 'built', company: 'Acme Freight' }, audit: 'skip' })
    await mockWorkspace(page, { agents: [GUIDE_CARD], role: 'admin' })
    await asAdminOnTheDashboard(page)
    await setupOpensByItself(page)
    await recordDashboardTour(page)
    await page.goto('/')
    await expect(page.getByTestId('first-run-overlay')).toBeVisible()
    expect(await page.evaluate(() => window.__dashboardTourDrawn)).toBe(false)
  })

  test('an experienced admin sent from the Workspace to setup still ends on the Dashboard tour', async ({ page }) => {
    await mockWorkspace(page, { agents: [GUIDE_CARD], role: 'admin' })
    await asAdminOnTheDashboard(page)
    await page.goto('/workspace')
    await expect(page).toHaveURL(/onboarding=1.*from=workspace/)
    await answerPanelAsExperienced(page)
    await page.getByTestId('first-run-skip-audit').click()
    await panelUntil(page, 'done')
    await page.getByTestId('first-run-next-done').click()
    await expect(page).toHaveURL(/localhost(:\d+)?\/$/)
    await expect(page.locator('#build-tour-title')).toHaveText('Your agents')
  })

  test('where the nav folds links into "N more", the tour only stops at what is on screen', async ({ page }) => {
    await page.setViewportSize({ width: 720, height: 900 })
    await mockWorkspace(page, { agents: [GUIDE_CARD], role: 'admin' })
    await asAdminOnTheDashboard(page)
    await page.goto('/?tour=features')
    await expect(page.getByTestId('build-tour')).toBeVisible()
    const TITLES = { 'view-modes': 'Your agents', 'nav-operations': 'Operations', 'nav-library': 'Library', 'nav-workspace': 'Workspace', 'nav-settings': 'Settings' }
    const titles = []
    for (let i = 0; i < 6 && await page.getByTestId('build-tour').count(); i++) {
      titles.push(await page.locator('#build-tour-title').textContent())
      await page.getByTestId('build-tour-next').click()
    }
    const onScreen = await page.locator('[data-tour]').evaluateAll((els) => els
      .filter((el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 })
      .map((el) => el.getAttribute('data-tour')))
    // The width is chosen so that some links really are folded away.
    expect(await page.locator('[data-nav-overflow-trigger]').isVisible()).toBe(true)
    expect(titles).toEqual(Object.keys(TITLES).filter((t) => onScreen.includes(t)).map((t) => TITLES[t]))
  })

  // Levels: one agent of the person's own, and its executions as the instance has them.
  const OWN_AGENT = { name: 'email-triage', owned: true, display_label: 'Email triage', description: 'e2e fixture', availability: 'ready', playbooks: [] }
  const RAN = { status: 'success', triggered_by: 'chat', source_agent_name: null, started_at: '2026-11-03T10:00:00Z', completed_at: '2026-11-03T10:01:00Z' }
  const LEVEL_ANSWERS = { answers: { role: 'Head of Operations', software: 'none', agents: 'tried', company: 'Acme Freight' }, audit: 'skip', tourSeen: true }
  // Saved once, so what the page writes back (levelsSeen) survives a reload.
  async function seedAnswersOnce(page, intake) {
    await page.addInitScript((value) => {
      for (const email of ['', 'tester@example.com', 'e2e@example.com']) {
        const key = `trinity-workspace-onboarding:${email}`
        if (!localStorage.getItem(key)) localStorage.setItem(key, value)
      }
    }, JSON.stringify({ current: null, done: true, skipped: false, finished: true, savedAt: Date.now(), ...intake }))
  }
  const levelRoute = (page, path, body) => page.route(`**/api/agents/${OWN_AGENT.name}/${path}`, (route) => route.fulfill(json(body)))

  const NEXT_2 = 'Next: Level 2, A new playbook.'
  const PLAYBOOKS = (names) => ({ skills: names.map((name) => ({ name, source: 'agent' })), count: names.length })
  // Reads held until a test lets them through, as when an agent does something mid-visit.
  function gate() {
    let open
    const opened = new Promise((resolve) => { open = resolve })
    return { opened, open }
  }

  test('having an agent of their own reaches Level 1: celebrated once, and Atlas is told once, after its moment', async ({ page }) => {
    await seedAnswersOnce(page, { ...LEVEL_ANSWERS, sent: true })
    const sent = await mockWorkspace(page, { agents: [GUIDE_CARD, OWN_AGENT], state: STATE })
    await page.goto('/workspace/build')
    await expect(page.getByTestId('build-levels-count')).toHaveText('1 of 7')
    await expect(page.getByTestId('build-level-celebrate')).toContainText('Level 1 reached: First agent')
    // Atlas is told once they've seen the moment, not over it.
    await expect(page.getByTestId('build-moment')).toBeVisible()
    await page.waitForTimeout(500)
    expect(sent).toHaveLength(0)
    await passMoment(page.getByTestId('build-moment'))
    await expect.poll(() => sent.length).toBe(1)
    expect(sent[0].message).toBe(`Level reached: Level 1, First agent.\n${NEXT_2}`)
    await page.reload()
    await expect(page.getByTestId('build-levels-count')).toHaveText('1 of 7')
    await page.waitForTimeout(1500)
    await expect(page.getByTestId('build-level-celebrate')).toHaveCount(0)
    expect(sent).toHaveLength(1)
  })

  test('the levels moment: lit once when the first build starts, on Your company', async ({ page }) => {
    await seedAnswersOnce(page, { ...LEVEL_ANSWERS, sent: true })
    await mockWorkspace(page, { agents: [GUIDE_CARD], state: STATE })
    await page.goto('/workspace/build')
    const moment = page.getByTestId('build-moment')
    await expect(moment.locator('#build-tour-title')).toHaveText('Your levels')
    await expect(moment.getByTestId('build-tour-body')).toContainText("We'll take you through these steps to learn Trinity")
    await expect(moment.getByTestId('build-tour-skip')).toHaveCount(0)
    await expect(page.getByTestId('build-levels')).toBeInViewport()
    await moment.getByTestId('build-tour-next').click()
    await expect(moment).toHaveCount(0)
    await page.reload()
    await expect(page.getByTestId('build-levels-count')).toHaveText('0 of 7')
    await page.waitForTimeout(1500)
    await expect(moment).toHaveCount(0)
  })

  test('the Level 1 moment: their new agent lit in the sidebar, then Level 1 ticked on the card', async ({ page }) => {
    await seedAnswersOnce(page, { ...LEVEL_ANSWERS, sent: true, levelsIntroSeen: true })
    await mockWorkspace(page, { agents: [GUIDE_CARD, OWN_AGENT], state: { ...STATE, focus: { agent: '', skill: '' } } })
    await page.goto('/workspace/build')
    const moment = page.getByTestId('build-moment')
    await expect(moment.locator('#build-tour-title')).toHaveText('Email triage is yours')
    await expect(moment.getByTestId('build-tour-body')).toHaveText('Your agents live here: click one any time to talk to it.')
    await moment.getByTestId('build-tour-next').click()
    await expect(moment.locator('#build-tour-title')).toHaveText('Level 1 done: First agent')
    await expect(moment.getByTestId('build-tour-body')).toHaveText('Next, Level 2: A new playbook.')
    await expect(page.getByTestId('build-level-celebrate')).toContainText('Level 1 reached: First agent')
    await moment.getByTestId('build-tour-next').click()
    await expect(moment).toHaveCount(0)
  })

  test('a level reached while another moment is open gets its own moment after that one', async ({ page }) => {
    await seedAnswersOnce(page, { ...LEVEL_ANSWERS, sent: true, levelsSeen: [1], levelsIntroSeen: true })
    // The right side follows Atlas into the agent, so each moment brings it back to Your company.
    const sent = await mockWorkspace(page, { agents: [GUIDE_CARD, OWN_AGENT], state: { ...STATE, focus: { agent: 'email-triage', skill: '' } } })
    await levelRoute(page, 'credential-requirements', { state: 'ok', summary: { total: 2, set: 2, missing: 0, unknown: 0, blocking: 0 } })
    let shared = false
    await page.route(`**/api/agents/${OWN_AGENT.name}/shares`, (route) => route.fulfill(json(shared ? [{ shared_with_email: 'sam@example.com' }] : [])))
    await page.goto('/workspace/build')
    const moment = page.getByTestId('build-moment')
    await expect(moment.locator('#build-tour-title')).toHaveText('Level 3 done: Connected')
    shared = true
    await page.evaluate(() => window.dispatchEvent(new Event('focus')))
    await expect(page.getByTestId('build-levels-count')).toHaveText('3 of 7')
    await moment.getByTestId('build-tour-next').click()
    await expect(moment.locator('#build-tour-title')).toHaveText('Level 4 done: In control')
    await expect(page.getByTestId('build-level-celebrate')).toContainText('Level 4 reached: In control')
    await moment.getByTestId('build-tour-next').click()
    await expect(moment).toHaveCount(0)
    // And back to the agent Atlas was on.
    await expect(page.getByTestId('build-world-map')).toHaveCount(0)
    await expect.poll(() => sent.filter((m) => /Level reached: Level [34]/.test(m.message)).length).toBeGreaterThan(0)
    const told = sent.map((m) => m.message).join('\n')
    expect(told.match(/Level reached: Level 3/g)).toHaveLength(1)
    expect(told.match(/Level reached: Level 4/g)).toHaveLength(1)
  })

  test('a reload during a level moment shows it again, and Atlas is told once, after it', async ({ page }) => {
    await seedAnswersOnce(page, { ...LEVEL_ANSWERS, sent: true, levelsSeen: [1], levelsIntroSeen: true })
    const sent = await mockWorkspace(page, { agents: [GUIDE_CARD, OWN_AGENT], state: STATE })
    await levelRoute(page, 'credential-requirements', { state: 'ok', summary: { total: 2, set: 2, missing: 0, unknown: 0, blocking: 0 } })
    await page.goto('/workspace/build')
    const moment = page.getByTestId('build-moment')
    await expect(moment.locator('#build-tour-title')).toHaveText('Level 3 done: Connected')
    await page.reload()
    await expect(moment.locator('#build-tour-title')).toHaveText('Level 3 done: Connected')
    expect(sent).toHaveLength(0)
    await moment.getByTestId('build-tour-next').click()
    await expect.poll(() => sent.length).toBe(1)
    expect(sent[0].message).toBe(`Level reached: Level 3, Connected.\n${NEXT_2}`)
    await page.reload()
    await expect(page.getByTestId('build-levels-count')).toHaveText('2 of 7')
    await page.waitForTimeout(1500)
    await expect(moment).toHaveCount(0)
    expect(sent).toHaveLength(1)
  })

  test('the line that tells Atlas a level was reached shows as a note in the chat, not as their message', async ({ page }) => {
    await seedAnswersOnce(page, { ...LEVEL_ANSWERS, sent: true, levelsIntroSeen: true })
    await mockWorkspace(page, { agents: [GUIDE_CARD, OWN_AGENT], state: STATE })
    // Atlas is still answering, so the line stays as sent.
    await page.route(`${API}/agents/${GUIDE}/chat/stream`, () => new Promise(() => {}))
    await page.goto('/workspace/build')
    await expect(page.getByTestId('portal-note-line')).toHaveText('Level 1 reached. Build Guide was told.')
    await expect(page.getByTestId('build-chat').getByText(/Level reached: Level 1/)).toHaveCount(0)
  })

  test('the playbooks an agent came with reach nothing; one added later reaches Level 2 with its moment, and the first look survives a reload', async ({ page }) => {
    await seedAnswersOnce(page, { ...LEVEL_ANSWERS, sent: true, levelsSeen: [1] })
    const sent = await mockWorkspace(page, { agents: [GUIDE_CARD, OWN_AGENT], state: STATE })
    let names = ['sort-inbox', 'onboarding']
    await page.route(`**/api/agents/${OWN_AGENT.name}/playbooks*`, (route) => route.fulfill(json(PLAYBOOKS(names))))
    await page.goto('/workspace/build')
    await expect(page.getByTestId('build-levels-count')).toHaveText('1 of 7')
    await page.waitForTimeout(1000)
    expect(sent).toHaveLength(0)
    // Reloaded, the page still compares with its first look, not with what it sees now.
    names = [...names, 'weekly-report']
    await page.reload()
    await expect(page.getByTestId('build-level-celebrate')).toContainText('Level 2 reached: A new playbook')
    await expect(page.getByTestId('build-levels-count')).toHaveText('2 of 7')
    await expect(page.getByTestId('build-moment').locator('#build-tour-title')).toHaveText('Level 2 done: A new playbook')
    await passMoment(page.getByTestId('build-moment'))
    await expect.poll(() => sent.length).toBe(1)
    expect(sent[0].message).toBe('Level reached: Level 2, A new playbook.\nNext: Level 3, Connected.')
  })

  test('Do it with Atlas puts the next level\'s request in the composer, without sending it', async ({ page }) => {
    await seedAnswers(page, { ...LEVEL_ANSWERS, sent: true, levelsSeen: [1] })
    const sent = await mockWorkspace(page, { agents: [GUIDE_CARD, OWN_AGENT], state: STATE })
    await page.goto('/workspace/build')
    await expect(page.getByTestId('build-levels-count')).toHaveText('1 of 7')
    await page.getByTestId('build-level-ask').click()
    await expect(page.locator('main textarea').first()).toHaveValue('Help me reach Level 2: add a new playbook to one of my agents.')
    await page.waitForTimeout(1000)
    expect(sent).toHaveLength(0)
  })

  test('every key their agent needs being set reaches Level 3 with its moment; with none needed it is skipped', async ({ page }) => {
    await seedAnswers(page, { ...LEVEL_ANSWERS, sent: true, levelsSeen: [1] })
    const sent = await mockWorkspace(page, { agents: [GUIDE_CARD, OWN_AGENT], state: STATE })
    await levelRoute(page, 'credential-requirements', { state: 'ok', summary: { total: 2, set: 2, missing: 0, unknown: 0, blocking: 0 } })
    await page.goto('/workspace/build')
    await expect(page.getByTestId('build-level-celebrate')).toContainText('Level 3 reached: Connected')
    await expect(page.getByTestId('build-moment').locator('#build-tour-title')).toHaveText('Level 3 done: Connected')
    await passMoment(page.getByTestId('build-moment'))
    await expect.poll(() => sent.length).toBe(1)
    expect(sent[0].message).toBe(`Level reached: Level 3, Connected.\n${NEXT_2}`)
  })

  test('with no keys to set, Level 3 shows as not needed, and nothing is said about it', async ({ page }) => {
    await seedAnswers(page, { ...LEVEL_ANSWERS, sent: true, levelsSeen: [1] })
    const sent = await mockWorkspace(page, { agents: [GUIDE_CARD, OWN_AGENT], state: STATE })
    await levelRoute(page, 'credential-requirements', { state: 'no_credentials_required', summary: {} })
    await page.goto('/workspace/build')
    await expect(page.getByTestId('build-level-3')).toContainText('Not needed')
    await expect(page.getByTestId('build-levels-count')).toHaveText('2 of 7')
    await page.waitForTimeout(1500)
    expect(sent).toHaveLength(0)
  })

  test("levels already reached when they arrive show on the card, but aren't told over Atlas's first question; a later one is told (no moment: the card is on screen)", async ({ page }) => {
    await seedAnswersOnce(page, LEVEL_ANSWERS)
    const sent = await mockWorkspace(page, { agents: [GUIDE_CARD, OWN_AGENT] })
    let shared = false
    await page.route(`**/api/agents/${OWN_AGENT.name}/shares`, (route) => route.fulfill(json(shared ? [{ shared_with_email: 'sam@example.com' }] : [])))
    await page.goto('/workspace/build')
    await expect.poll(() => sent.length).toBe(1)
    expect(sent[0].message).toContain('My role: Head of Operations.')
    await expect(page.getByTestId('build-level-celebrate')).toContainText('Level 1 reached: First agent')
    await page.waitForTimeout(1500)
    expect(sent).toHaveLength(1)
    // One reached later in the same visit is told as usual.
    shared = true
    await page.evaluate(() => window.dispatchEvent(new Event('focus')))
    // The Levels card is whole on screen here: its own tick, no moment, and Atlas is told.
    await expect.poll(() => sent.length).toBe(2)
    await expect(page.getByTestId('build-moment')).toHaveCount(0)
    expect(sent[1].message).toBe(`Level reached: Level 4, In control.\n${NEXT_2}`)
    // And Level 1 was saved as seen, so it isn't celebrated again.
    await page.reload()
    await expect(page.getByTestId('build-levels-count')).toHaveText('2 of 7')
    await page.waitForTimeout(1500)
    await expect(page.getByTestId('build-level-celebrate')).toHaveCount(0)
  })

  test('two levels reached at once: one celebration for the higher, one moment, and one message with both', async ({ page }) => {
    await seedAnswers(page, { ...LEVEL_ANSWERS, sent: true })
    const sent = await mockWorkspace(page, { agents: [GUIDE_CARD, OWN_AGENT], state: STATE })
    await levelRoute(page, 'shares', [{ shared_with_email: 'sam@example.com' }])
    await page.goto('/workspace/build')
    await expect(page.getByTestId('build-levels-count')).toHaveText('2 of 7')
    await expect(page.getByTestId('build-level-celebrate')).toContainText('Level 4 reached: In control')
    // Their only agent lit first, as Level 1 is one of them, then both levels on the card.
    const moment = page.getByTestId('build-moment')
    await expect(moment.locator('#build-tour-title')).toHaveText('Email triage is yours')
    await moment.getByTestId('build-tour-next').click()
    await expect(moment.locator('#build-tour-title')).toHaveText('Levels 1 and 4 done')
    await moment.getByTestId('build-tour-next').click()
    await expect.poll(() => sent.length).toBe(1)
    expect(sent[0].message).toBe(`Level reached: Level 1, First agent.\nLevel reached: Level 4, In control.\n${NEXT_2}`)
  })

  test("opened straight on the Build page, Trinity's samples never count as theirs", async ({ page }) => {
    await seedAnswers(page, LEVEL_ANSWERS)
    const samples = ['acme-scout', 'acme-sage', 'acme-scribe']
    const sent = await mockWorkspace(page, { agents: [GUIDE_CARD, ...samples.map((name) => ({ name, owned: true, display_label: name, availability: 'ready', playbooks: [] }))] })
    await page.route('**/api/onboarding/first-run', (route) => route.fulfill(json({ first_run: true, seeded_agents: samples, own_agent_count: 0 })))
    for (const name of samples) {
      await page.route(`**/api/agents/${name}/executions*`, (route) => route.fulfill(json([RAN])))
      await page.route(`**/api/agents/${name}/permissions`, (route) => route.fulfill(json({ permitted_agents: samples.filter((n) => n !== name).map((n) => ({ name: n, permitted: true })) })))
    }
    await page.goto('/workspace/build')
    await expect.poll(() => sent.length).toBe(1)
    expect(sent[0].message).toContain('Sample agents that came with Trinity: ')
    expect(sent[0].message).not.toContain('Agents already on this instance: acme')
    await expect(page.getByTestId('build-levels-count')).toHaveText('0 of 7')
    await page.waitForTimeout(1500)
    expect(sent).toHaveLength(1)
  })

  test("when the page can't tell which agents are samples, it reads no levels and says nothing", async ({ page }) => {
    await seedAnswers(page, { ...LEVEL_ANSWERS, sent: true })
    const sent = await mockWorkspace(page, { agents: [GUIDE_CARD, OWN_AGENT], state: STATE })
    await page.route('**/api/onboarding/first-run', (route) => route.fulfill(json({ detail: 'down' }, 500)))
    await page.goto('/workspace/build')
    await expect(page.getByTestId('build-world')).toBeVisible()
    await page.waitForTimeout(1500)
    await expect(page.getByTestId('build-levels')).toHaveCount(0)
    expect(sent).toHaveLength(0)
  })

  test('a level reached while they are typing waits, and never replaces what they wrote (any moment pressed through)', async ({ page }) => {
    await seedAnswers(page, { ...LEVEL_ANSWERS, sent: true, levelsSeen: [1] })
    const sent = await mockWorkspace(page, { agents: [GUIDE_CARD, OWN_AGENT], state: STATE })
    let shared = false
    await page.route(`**/api/agents/${OWN_AGENT.name}/shares`, (route) => route.fulfill(json(shared ? [{ shared_with_email: 'sam@example.com' }] : [])))
    await page.goto('/workspace/build')
    await expect(page.getByTestId('build-levels-count')).toHaveText('1 of 7')
    const composer = page.locator('main textarea').first()
    await composer.fill('Something I am still writing')
    shared = true
    await page.evaluate(() => window.dispatchEvent(new Event('focus')))
    await expect(page.getByTestId('build-levels-count')).toHaveText('2 of 7')
    await page.waitForTimeout(1500)
    expect(sent).toHaveLength(0)
    await expect(composer).toHaveValue('Something I am still writing')
    await composer.fill('')
    await page.getByTestId('build-world').click({ position: { x: 5, y: 5 } })
    await passMomentIfShown(page)
    await expect.poll(() => sent.length).toBe(1)
    expect(sent[0].message).toBe(`Level reached: Level 4, In control.\n${NEXT_2}`)
  })

  test("a level reached while the guide replies waits, joins a card's outcome, and goes once; with the card on screen, no moment", async ({ page }) => {
    const { sent } = await withProposal(page, { id: 'p9', action: 'allow-call', from: 'researcher', to: 'writer' })
    const reply = gate()
    await page.route(`${API}/agents/${GUIDE}/chat/stream`, async (route) => {
      sent.push(route.request().postDataJSON())
      if (sent.length === 1) await reply.opened
      return route.fulfill(json({ detail: 'mocked' }, 503))
    })
    let shared = false
    await page.route('**/api/agents/writer/shares', (route) => route.fulfill(json(shared ? [{ shared_with_email: 'sam@example.com' }] : [])))
    await page.goto('/workspace/build')
    await expect(page.getByTestId('build-fleet-card-words')).toHaveText('Let Researcher call Writer')
    const composer = page.locator('main textarea').first()
    await composer.fill('Hi')
    await composer.press('Enter')
    await expect.poll(() => sent.length).toBe(1)
    shared = true
    await page.evaluate(() => window.dispatchEvent(new Event('focus')))
    await expect(page.getByTestId('build-level-celebrate')).toContainText('Level 4 reached: In control')
    await page.getByTestId('build-fleet-confirm').click()
    await page.waitForTimeout(1000)
    expect(sent).toHaveLength(1)
    reply.open()
    await expect.poll(() => sent.length).toBeGreaterThan(1)
    expect(sent[1].message).toBe(`Done: Let Researcher call Writer.\nLevel reached: Level 4, In control.\n${NEXT_2}`)
    // The Levels card was already whole on screen: its own tick says it, with no dimming.
    await expect(page.getByTestId('build-moment')).toHaveCount(0)
    await page.waitForTimeout(1500)
    expect(sent.filter((m) => m.message.includes('Level reached: Level 4'))).toHaveLength(1)
  })

  test('with the page open in two tabs, the guide is told once (any moment pressed through)', async ({ page }) => {
    await seedAnswersOnce(page, { ...LEVEL_ANSWERS, sent: true, levelsSeen: [1] })
    const sent = await mockWorkspace(page, { agents: [GUIDE_CARD, OWN_AGENT], state: STATE })
    let shared = false
    const shares = (route) => route.fulfill(json(shared ? [{ shared_with_email: 'sam@example.com' }] : []))
    await page.route(`**/api/agents/${OWN_AGENT.name}/shares`, shares)
    const other = await page.context().newPage()
    await seedAnswersOnce(other, { ...LEVEL_ANSWERS, sent: true, levelsSeen: [1] })
    const sentOther = await mockWorkspace(other, { agents: [GUIDE_CARD, OWN_AGENT], state: STATE })
    await other.route(`**/api/agents/${OWN_AGENT.name}/shares`, shares)
    await page.goto('/workspace/build')
    await other.goto('/workspace/build')
    await expect(page.getByTestId('build-levels-count')).toHaveText('1 of 7')
    await expect(other.getByTestId('build-levels-count')).toHaveText('1 of 7')
    shared = true
    await page.evaluate(() => window.dispatchEvent(new Event('focus')))
    await passMomentIfShown(page)
    await expect.poll(() => sent.length).toBe(1)
    await other.evaluate(() => window.dispatchEvent(new Event('focus')))
    await expect(other.getByTestId('build-levels-count')).toHaveText('2 of 7')
    await other.waitForTimeout(1500)
    expect(sentOther).toHaveLength(0)
  })

  test('the first time, the chat is frozen behind "getting ready" until the guide has replied', async ({ page }) => {
    await seedAnswers(page, { answers: { role: 'Head of Operations', software: 'none', agents: 'tried', company: 'Acme Freight' }, audit: 'skip', tourSeen: true })
    const sent = await mockWorkspace(page, { agents: [GUIDE_CARD] })
    let release
    const replying = new Promise((resolve) => { release = resolve })
    await page.route(`${API}/agents/${GUIDE}/chat/stream`, async (route) => {
      sent.push(route.request().postDataJSON())
      await replying
      return route.fulfill(json({ detail: 'mocked' }, 503))
    })
    await page.goto('/workspace/build')
    await expect.poll(() => sent.length).toBe(1)
    const ready = page.getByTestId('build-getting-ready')
    await expect(ready).toBeVisible()
    await expect(ready).toContainText('Build Guide is getting ready')
    // Nothing under it can be typed in or pressed, Stop included.
    await expect(page.locator('[data-testid="build-chat"][inert]')).toHaveCount(1)
    await expect(page.getByTestId('build-first-reply-notice')).toHaveCount(0)
    release()
    await expect(ready).toHaveCount(0)
    await expect(page.locator('[data-testid="build-chat"][inert]')).toHaveCount(0)
    await page.locator('main textarea').first().fill('Hello')
  })

  test('clicking the red profile while Atlas is replying waits for the reply', async ({ page }) => {
    await seedAnswers(page, { answers: { role: 'Head of Operations', software: 'none', agents: 'tried', company: 'Acme Freight' }, audit: 'skip', sent: true, tourSeen: true })
    const notYet = { ...STATE, stage: 'profile', profile_complete: false, audit: { status: 'not_started', summary: '' }, agents: [], focus: {} }
    const sent = await mockWorkspace(page, { agents: [GUIDE_CARD], state: notYet })
    let release
    const replying = new Promise((resolve) => { release = resolve })
    await page.route(`${API}/agents/${GUIDE}/chat/stream`, async (route) => {
      sent.push(route.request().postDataJSON())
      if (sent.length === 1) await replying
      return route.fulfill(json({ detail: 'mocked' }, 503))
    })
    await page.goto('/workspace/build')
    const composer = page.locator('main textarea').first()
    await composer.fill('What can you do?')
    await composer.press('Enter')
    await expect.poll(() => sent.length).toBe(1)
    await page.getByTestId('build-step-finish-profile').click()
    await page.waitForTimeout(1500)
    expect(sent).toHaveLength(1)
    release()
    await expect.poll(() => sent.length).toBe(2)
    expect(sent[1].message).toBe("Profile: I'd like to tell you about my work first, so you know what my job involves.")
  })
  test("a person's own Atlas is the one they chat with, never a colleague's shared with them", async ({ page }) => {
    await seedAnswers(page, { answers: { role: 'Head of Operations', software: 'none', agents: 'tried', company: 'Acme Freight' }, audit: 'start', sent: true, tourSeen: true })
    const mine = { ...GUIDE_CARD, name: 'atlas-22222222', display_label: 'Atlas' }
    const theirs = { ...GUIDE_CARD, name: 'atlas-11111111', display_label: 'Atlas', owned: false }
    await mockWorkspace(page, { agents: [theirs, mine], state: STATE, guide: mine.name })
    const read = []
    page.on('request', (r) => { if (/\/canvas\/build$/.test(new URL(r.url()).pathname)) read.push(new URL(r.url()).pathname) })
    await page.goto('/workspace/build')
    await expect(page.getByTestId('build-world')).toContainText('Acme Freight')
    expect(read.length).toBeGreaterThan(0)
    expect(read.every((path) => path.includes('/agents/atlas-22222222/'))).toBe(true)
  })

  test("with only a colleague's guide around, setting up makes one of their own", async ({ page }) => {
    await seedAnswers(page, { answers: { role: 'Head of Operations', software: 'none', agents: 'tried', company: 'Acme Freight' }, audit: 'start', tourSeen: true })
    await mockWorkspace(page, { agents: [{ ...GUIDE_CARD, owned: false }] })
    const deployed = []
    await page.route('**/api/systems/deploy', (route) => {
      deployed.push(route.request().postDataJSON().manifest)
      return route.fulfill(json({ status: 'deployed', failed: [] }))
    })
    await page.goto('/workspace/build')
    await page.getByTestId('build-setup-deploy').click()
    await expect.poll(() => deployed.length).toBe(1)
    expect(deployed[0]).toMatch(/\n  atlas-[0-9a-f]{8}:\n    template: local:build-guide/)
    expect(deployed[0]).not.toContain('agent-builder')
  })
  test("answers saved during the old handover open Atlas's chat, with no second agent", async ({ page }) => {
    await seedAnswers(page, {
      answers: { role: 'Head of Operations', software: 'none', agents: 'tried', company: 'Acme Freight' },
      audit: 'start', sent: true, tourSeen: true,
      buildChat: 'builder', handoffSent: 'h1', handoffBefore: ['build-guide'], handoffReported: false,
    })
    const sent = await mockWorkspace(page, { agents: [GUIDE_CARD, { name: 'agent-builder', owned: true, availability: 'ready', playbooks: [] }], state: { ...STATE, handoff: { id: 'h2', brief: '/build-agent new brief' } } })
    await page.goto('/workspace/build')
    await expect(page.getByTestId('build-world')).toContainText('Acme Freight')
    await expect(page.getByTestId('build-chat-switch')).toHaveCount(0)
    await page.waitForTimeout(800)
    expect(sent).toHaveLength(0)
    const composer = page.locator('main textarea').first()
    await composer.fill('What next?')
    await composer.press('Enter')
    await expect.poll(() => sent.length).toBe(1)
    expect(sent[0].message).toBe('What next?')
  })
  // A settings change Atlas proposes: the page's card, Confirm once, and Atlas is told.
  async function withProposal(page, proposal, { permitted = [], refuse = null } = {}) {
    // Level 1 is reached by their own agents at once; these tests are about the cards.
    await seedAnswers(page, { answers: { role: 'Head of Operations', software: 'none', agents: 'tried', company: 'Acme Freight' }, audit: 'start', sent: true, tourSeen: true, levelsSeen: [1] })
    const mine = (name, label) => ({ name, display_label: label, owned: true, availability: 'ready', playbooks: [] })
    const sent = await mockWorkspace(page, { agents: [GUIDE_CARD, mine('researcher', 'Researcher'), mine('writer', 'Writer')], state: STATE })
    await page.route(`${API}/agents/${GUIDE}/canvas/fleet`, (route) => route.fulfill(json({
      id: 'fleet', title: 'Fleet', blocks: [{ id: 'proposal', kind: 'json', payload: proposal }],
    })))
    const changes = []
    const files = []
    let allowed = [...permitted]
    await page.route('**/api/agents/researcher/permissions', (route) => route.fulfill(json({ source_agent: 'researcher', permitted_agents: allowed.map((name) => ({ name, permitted: true })) })))
    await page.route('**/api/agents/researcher/permissions/writer', (route) => {
      changes.push(route.request().method())
      if (refuse) return route.fulfill(json({ detail: refuse }, 403))
      allowed = ['writer']
      return route.fulfill(json({ ok: true }))
    })
    await page.route(`**/api/agents/${GUIDE}/files?path=fleet-state.json`, (route) => {
      files.push(JSON.parse(route.request().postData() || '{}').content)
      return route.fulfill(json({ ok: true }))
    })
    return { sent, changes, files }
  }

  test('a change Atlas proposes is a card in the page\'s words; Confirm makes it once and tells Atlas', async ({ page }) => {
    const { sent, changes, files } = await withProposal(page, { id: 'p1', action: 'allow-call', from: 'researcher', to: 'writer', words: 'Delete everything' })
    await page.goto('/workspace/build')
    await expect(page.getByTestId('build-fleet-card-words')).toHaveText('Let Researcher call Writer')
    await expect(page.getByTestId('build-fleet-card')).not.toContainText('Delete everything')
    await expect.poll(() => files.length).toBeGreaterThan(0)
    expect(JSON.parse(files[0]).agents.map((a) => a.name)).toEqual(['researcher', 'writer'])
    await page.getByTestId('build-fleet-confirm').click()
    await expect.poll(() => sent.length).toBe(1)
    expect(changes).toEqual(['POST'])
    expect(sent[0].message).toBe('Done: Let Researcher call Writer.')
    expect(JSON.parse(files.at(-1)).agents[0].may_call).toEqual(['writer'])
  })

  test('a change the page does not know is never a card, and nothing is sent anywhere', async ({ page }) => {
    const { sent, changes } = await withProposal(page, { id: 'p2', action: 'delete-agent', agent: 'writer' })
    await page.goto('/workspace/build')
    await expect(page.getByTestId('build-fleet-card')).toContainText("Atlas suggested a change this page can't make.")
    await expect(page.getByTestId('build-fleet-confirm')).toHaveCount(0)
    await page.getByTestId('build-fleet-dismiss').click()
    await expect(page.getByTestId('build-fleet-card')).toHaveCount(0)
    expect(changes).toEqual([])
    expect(sent).toHaveLength(0)
  })

  test('a refused change says so, changes nothing, and Atlas is told why', async ({ page }) => {
    const { sent, changes } = await withProposal(page, { id: 'p3', action: 'allow-call', from: 'researcher', to: 'writer' }, { refuse: 'Only the owner can modify agent permissions' })
    await page.goto('/workspace/build')
    await page.getByTestId('build-fleet-confirm').click()
    await expect(page.getByTestId('build-fleet-card')).toContainText('Nothing was changed. Only the owner can modify agent permissions')
    await expect.poll(() => sent.length).toBe(1)
    expect(changes).toEqual(['POST'])
    expect(sent[0].message).toBe("That didn't work: Let Researcher call Writer. Only the owner can modify agent permissions")
  })
  test("a colleague starts from the company a first person's guide already researched", async ({ page }) => {
    await seedAnswers(page, { answers: { role: 'Head of Sales', software: 'none', agents: 'tried' }, audit: 'start', tourSeen: true })
    const sent = await mockWorkspace(page, { agents: [GUIDE_CARD] })
    await page.route('**/api/workspace-build/company', (route) => route.fulfill(json({
      name: 'Acme Freight', website: 'acme.example', research_md: '# Acme Freight\nMoves freight.', context_md: 'Acme moves freight.', updated_by: 'first@example.com', updated_at: '2026-11-03T12:00:00Z',
    })))
    const files = []
    await page.route(`**/api/agents/${GUIDE}/files?path=*`, (route) => {
      files.push(new URL(route.request().url()).searchParams.get('path'))
      return route.fulfill(json({ ok: true }))
    })
    await page.goto('/workspace/build')
    await expect.poll(() => sent.length).toBe(1)
    expect(files).toEqual(expect.arrayContaining(['company-research.md', 'context.md']))
    expect(sent[0].message).toContain('My company: Acme Freight.')
    expect(sent[0].message).toContain('Company research: shared by a colleague, already in company-research.md.')
    expect(sent.tasks).toHaveLength(0)
  })
  test('an agent the guide just built reaches the page without a reload, so its card can be confirmed', async ({ page }) => {
    await seedAnswers(page, { answers: { role: 'Head of Operations', software: 'none', agents: 'tried', company: 'Acme Freight' }, audit: 'start', sent: true, sentTo: GUIDE, tourSeen: true })
    const mine = (name, label) => ({ name, display_label: label, owned: true, availability: 'ready', playbooks: [] })
    const built = { ...STATE, agents: [{ ...STATE.agents[0], id: 'writer', name: 'Writer', live_name: 'writer', status: 'ready' }] }
    await mockWorkspace(page, { agents: [GUIDE_CARD, mine('researcher', 'Researcher')], state: built })
    let rosterReads = 0
    await page.route(`${API}/my-agents*`, (route) => {
      rosterReads += 1
      const agents = rosterReads === 1 ? [GUIDE_CARD, mine('researcher', 'Researcher')] : [GUIDE_CARD, mine('researcher', 'Researcher'), mine('writer', 'Writer')]
      return route.fulfill(json({ client_email: 'e2e@example.com', agents }))
    })
    await page.route(`${API}/agents/${GUIDE}/canvas/fleet`, (route) => route.fulfill(json({
      id: 'fleet', title: 'Fleet', blocks: [{ id: 'proposal', kind: 'json', payload: { id: 'p1', action: 'allow-call', from: 'researcher', to: 'writer' } }],
    })))
    await page.goto('/workspace/build')
    await expect(page.getByTestId('build-fleet-card-words')).toHaveText('Let Researcher call Writer', { timeout: 15000 })
    expect(rosterReads).toBeGreaterThan(1)
  })

  test("answers sent to the shared guide before go again to the person's own Atlas", async ({ page }) => {
    const mineGuide = 'atlas-22222222'
    await seedAnswers(page, { answers: { role: 'Head of Operations', software: 'none', agents: 'tried', company: 'Acme Freight' }, audit: 'start', sent: true, tourSeen: true })
    const sent = await mockWorkspace(page, { agents: [{ ...GUIDE_CARD, name: mineGuide, display_label: 'Atlas' }], guide: mineGuide })
    await page.goto('/workspace/build')
    await expect.poll(() => sent.length).toBe(1)
    expect(sent[0].message).toContain('My role: Head of Operations.')
  })

  test('a change the page cannot confirm says so, and never claims nothing changed', async ({ page }) => {
    // Level 1 is reached by their own agents at once; this test is about the card.
    await seedAnswers(page, { answers: { role: 'Head of Operations', software: 'none', agents: 'tried', company: 'Acme Freight' }, audit: 'start', sent: true, sentTo: GUIDE, tourSeen: true, levelsSeen: [1] })
    const mine = (name, label) => ({ name, display_label: label, owned: true, availability: 'ready', playbooks: [] })
    const sent = await mockWorkspace(page, { agents: [GUIDE_CARD, mine('researcher', 'Researcher'), mine('writer', 'Writer')], state: STATE })
    await page.route(`${API}/agents/${GUIDE}/canvas/fleet`, (route) => route.fulfill(json({
      id: 'fleet', title: 'Fleet', blocks: [{ id: 'proposal', kind: 'json', payload: { id: 'p4', action: 'allow-call', from: 'researcher', to: 'writer' } }],
    })))
    await page.route('**/api/agents/researcher/permissions/writer', (route) => route.fulfill(json({ detail: 'upstream' }, 502)))
    await page.goto('/workspace/build')
    await page.getByTestId('build-fleet-confirm').click()
    await expect(page.getByTestId('build-fleet-card')).toContainText('This may or may not have been made.')
    await expect(page.getByTestId('build-fleet-card')).not.toContainText('Nothing was changed')
    await expect.poll(() => sent.length).toBe(1)
    expect(sent[0].message).toMatch(/^Not sure it worked: Let Researcher call Writer\./)
  })

  test("publishing the company never blanks what a colleague's guide kept", async ({ page }) => {
    await seedAnswers(page, { answers: { role: 'Head of Operations', software: 'none', agents: 'tried', company: 'Acme Freight' }, audit: 'start', sent: true, sentTo: GUIDE, tourSeen: true })
    await mockWorkspace(page, { agents: [GUIDE_CARD], state: { ...STATE, context_md: 'New context from this audit.' } })
    const puts = []
    await page.route('**/api/workspace-build/company', (route) => {
      if (route.request().method() === 'PUT') {
        puts.push(route.request().postDataJSON())
        return route.fulfill(json({ ok: true }))
      }
      return route.fulfill(json({ name: 'Acme Freight', website: 'acme.example', research_md: '# Acme Freight', context_md: 'Older context.', updated_by: 'first@example.com', updated_at: '2026-11-03T12:00:00Z' }))
    })
    await page.goto('/workspace/build')
    await expect.poll(() => puts.length).toBe(1)
    expect(puts[0]).toEqual({ name: 'Acme Freight', website: 'acme.example', research_md: '# Acme Freight', context_md: 'New context from this audit.' })
  })
})
