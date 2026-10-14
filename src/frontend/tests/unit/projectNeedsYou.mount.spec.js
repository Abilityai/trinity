// @vitest-environment jsdom
/**
 * #3574 — a project's "Needs you", mounted.
 *   - a pending ask is ONE Inbox-style row (agent, title) — the full ask card
 *     (PortalAsks) and the ask's body are NOT in the DOM until Answer;
 *   - Answer mounts the ask card in place with its options and takes focus;
 *     Collapse removes it and returns focus to Answer;
 *   - "Open in Inbox" is the Inbox's own addressing for that ask;
 *   - ids but no list yet is loading, never "nothing waiting"; an ended ask
 *     nobody opened is not listed; an opened ask that ends stays until collapsed.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: true, authHeader: {}, logout: vi.fn() }),
}))
vi.mock('axios', () => {
  const mk = () => ({
    get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  })
  return {
    default: Object.assign(
      { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), create: mk },
      { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
        defaults: { headers: { common: {} } } },
    ),
  }
})

import { useClientPortalStore } from '@/stores/clientPortal'
import ProjectNeedsYou from '@/components/portal/projects/ProjectNeedsYou.vue'

const byTestId = (id) => document.querySelector(`[data-testid="${id}"]`)

const LONG_BODY = [
  'The plan is done. It removes the keyless door and adds a token door: a verifier, outbound credentials, and per-client isolation.',
  'Under autonomous mode I may rule taste decisions myself, except ones that touch schema or public contracts. Four of these do:',
  '- T2 (schema): a new column `schedule_executions.source_client` for the client identity.',
  '- T4 (schema): the keyless columns become inert. Migration `0098_a2a_token_auth` sweeps them.',
].join('\n\n')

const ask = (over = {}) => ({
  id: 'q1', agent_name: 'scout', kind: 'approval', title: 'Approve the plan’s 7 design calls?',
  question: LONG_BODY, options: ['Approve all', 'Hold'], status: 'pending', created_at: '2026-09-30T10:00:00Z', ...over,
})

let portal
let router
let wrapper

beforeEach(async () => {
  document.body.innerHTML = ''
  setActivePinia(createPinia())
  vi.clearAllMocks()
  portal = useClientPortalStore()
  portal.asksAvailable = true
  portal.asksLoaded = true
  portal.asks = [ask()]
  router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:p(.*)*', component: { template: '<div/>' } }] })
  await router.push('/workspace/projects/prj_1')
})
afterEach(() => { wrapper?.unmount(); wrapper = null })

const mountIt = (props = {}) => {
  wrapper = mount(ProjectNeedsYou, {
    props: { askIds: ['q1'], ...props },
    global: { plugins: [router] },
    attachTo: document.body,
  })
  return wrapper
}

describe('a pending ask is a list row', () => {
  it('shows the agent and the title on one line, never the body or the ask card', async () => {
    mountIt()
    await flushPromises()
    const row = byTestId('project-ask-row-q1')
    expect(row).not.toBeNull()
    expect(row.textContent).toContain('scout')
    expect(byTestId('project-ask-title-q1').textContent).toContain('Approve the plan')
    expect(byTestId('project-ask-title-q1').className).toContain('truncate')
    expect(document.body.textContent).not.toContain('Under autonomous mode')
    expect(document.body.textContent).not.toContain('source_client')
    expect(byTestId('portal-ask-q1')).toBeNull()
    expect(byTestId('portal-ask-option-q1')).toBeNull()
    expect(byTestId('project-ask-q1').getAttribute('data-open')).toBe('false')
  })

  it('links to the ask in the Inbox, selected and filtered to its agent', async () => {
    mountIt()
    await flushPromises()
    const href = byTestId('project-ask-inbox-q1').getAttribute('href')
    expect(href).toContain('/workspace/inbox')
    expect(href).toContain('tab=action')
    expect(href).toContain('from=scout')
    expect(href).toMatch(/item=ask(%3A|:)q1/)
  })

  it('a short ask without a title shows its question as the title', async () => {
    portal.asks = [ask({ title: '', question: 'Ship it?', options: [] })]
    mountIt()
    await flushPromises()
    expect(byTestId('project-ask-title-q1').textContent).toContain('Ship it?')
  })
})

describe('Answer opens the ask card in place', () => {
  it('mounts PortalAsks with its options and takes focus; Collapse removes it and focus returns', async () => {
    mountIt()
    await flushPromises()
    byTestId('project-ask-answer-q1').click()
    await flushPromises()
    expect(byTestId('project-ask-q1').getAttribute('data-open')).toBe('true')
    expect(byTestId('portal-ask-q1')).not.toBeNull()
    expect(document.querySelectorAll('[data-testid="portal-ask-option-q1"]').length).toBe(2)
    expect(document.body.textContent).toContain('Under autonomous mode')
    expect(document.activeElement).toBe(byTestId('portal-ask-q1'))
    expect(byTestId('project-ask-row-q1')).toBeNull()

    byTestId('project-ask-collapse-q1').click()
    await flushPromises()
    expect(byTestId('portal-ask-q1')).toBeNull()
    expect(byTestId('project-ask-row-q1')).not.toBeNull()
    expect(document.activeElement).toBe(byTestId('project-ask-answer-q1'))
  })

  it('an opened ask that ends stays drawn as ended until collapsed, then is gone', async () => {
    mountIt()
    await flushPromises()
    byTestId('project-ask-answer-q1').click()
    await flushPromises()
    portal.asks = [ask({ status: 'answered', ended_at: '2026-09-30T11:00:00Z', ended_by: 'you' })]
    await flushPromises()
    expect(byTestId('portal-ask-q1')).not.toBeNull()
    expect(byTestId('portal-ask-q1').getAttribute('data-status')).toBe('answered')
    byTestId('project-ask-collapse-q1').click()
    await flushPromises()
    expect(byTestId('project-ask-q1')).toBeNull()
    expect(byTestId('project-needs-you-body').textContent).toContain('Nothing on this project is waiting on you.')
  })

  it('an ask inside its Undo window is forced open so Undo stays reachable', async () => {
    portal.asks = [ask({ status: 'dismissed' })]
    portal.askDismissals = { q1: { until: Date.now() + 5000, committing: false } }
    mountIt()
    await flushPromises()
    expect(byTestId('project-ask-q1').getAttribute('data-open')).toBe('true')
    expect(byTestId('project-ask-row-q1')).toBeNull()
    expect(byTestId('portal-ask-q1')).not.toBeNull()
    // Collapse would change nothing while the window is open, so it is absent.
    expect(byTestId('project-ask-collapse-q1')).toBeNull()
  })

  it('Answer and Collapse carry the touch-screen floor', async () => {
    mountIt()
    await flushPromises()
    expect(byTestId('project-ask-answer-q1').className).toContain('[@media(pointer:coarse)]:min-h-11')
    byTestId('project-ask-answer-q1').click()
    await flushPromises()
    expect(byTestId('project-ask-collapse-q1').className).toContain('[@media(pointer:coarse)]:min-h-11')
  })
})

describe('gates', () => {
  it('ids but no list yet is loading, not "nothing waiting"', async () => {
    portal.asksLoaded = false
    portal.asks = []
    mountIt()
    await flushPromises()
    expect(byTestId('project-needs-you-loading')).not.toBeNull()
    expect(byTestId('project-needs-you-loading').getAttribute('aria-busy')).toBe('true')
    expect(byTestId('project-needs-you-body').textContent).not.toContain('Nothing on this project')
  })

  it('no ids is "nothing waiting" even before the list loads', async () => {
    portal.asksLoaded = false
    portal.asks = []
    mountIt({ askIds: [] })
    await flushPromises()
    expect(byTestId('project-needs-you-loading')).toBeNull()
    expect(byTestId('project-needs-you-body').textContent).toContain('Nothing on this project is waiting on you.')
  })

  it('an ended ask nobody opened, or an id not on this project, is not listed', async () => {
    portal.asks = [ask({ status: 'expired' }), ask({ id: 'q9', title: 'Elsewhere' })]
    mountIt()
    await flushPromises()
    expect(byTestId('project-ask-q1')).toBeNull()
    expect(document.body.textContent).not.toContain('Elsewhere')
    expect(byTestId('project-needs-you-body').textContent).toContain('Nothing on this project is waiting on you.')
  })

  it('the read having failed says so and offers the Inbox', async () => {
    mountIt({ unavailable: true })
    await flushPromises()
    expect(byTestId('project-needs-you-body').textContent).toContain("Couldn't read your Inbox just now")
    expect(byTestId('project-ask-q1')).toBeNull()
  })

  it('a failed or absent Inbox list is the failed line, never a skeleton that stays', async () => {
    portal.asksLoaded = false
    portal.asksFailed = true
    portal.asks = []
    mountIt()
    await flushPromises()
    expect(byTestId('project-needs-you-loading')).toBeNull()
    expect(byTestId('project-needs-you-failed')).not.toBeNull()

    portal.asksFailed = false
    portal.asksAbsent = true
    await flushPromises()
    expect(byTestId('project-needs-you-loading')).toBeNull()
    expect(byTestId('project-needs-you-failed')).not.toBeNull()
  })

  it('a failed refresh keeps the rows on screen (stale, not gone)', async () => {
    portal.asksFailed = true
    mountIt()
    await flushPromises()
    expect(byTestId('project-ask-row-q1')).not.toBeNull()
    expect(byTestId('project-needs-you-failed')).toBeNull()
  })
})
