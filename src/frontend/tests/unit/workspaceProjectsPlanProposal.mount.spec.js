// @vitest-environment jsdom
/**
 * trinity-enterprise#1234 — the steward's kickoff plan, mounted.
 *
 *   - the project page previews the WHOLE plan from `proposal.plan`: every
 *     milestone with its targets, every task under its milestone, each
 *     blocked-by link as "after <task>";
 *   - the person it waits on approves once (one answer through the ask
 *     endpoint) or declines with a reason (required, sent with the answer);
 *     anyone else reads who it waits on and gets no controls;
 *   - nothing from the plan is in the task list before approval;
 *   - a project with an agent steward and no plan says so, and offers
 *     "Write the plan yourself" to someone who can edit the goal only;
 *   - the Inbox card links to the project page for the whole plan.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, flushPromises, RouterLinkStub } from '@vue/test-utils'
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

import { useClientPortalStore, portalHttp } from '@/stores/clientPortal'
import { useProjectsStore } from '@/stores/projects'
import ProjectPlanProposal from '@/components/portal/projects/ProjectPlanProposal.vue'
import ProjectPage from '@/components/portal/projects/ProjectPage.vue'
import ProjectTasks from '@/components/portal/projects/ProjectTasks.vue'
import PortalAsks from '@/components/portal/PortalAsks.vue'

globalThis.ResizeObserver = globalThis.ResizeObserver || class {
  observe() {}
  unobserve() {}
  disconnect() {}
}

const byTestId = (id) => document.querySelector(`[data-testid="${id}"]`)
const ME = 'eva@example.com'
const BOB = 'bob@example.com'
let portal
let projects
let router

const PLAN = {
  counts: '2 milestones, 4 tasks',
  milestones: [
    { key: 'beta', name: 'Beta live', reached_means: 'Ten design partners use it.',
      targets: [{ agent: 'scout', metric: 'partners', target: 10, direction: 'up', tolerance: null }] },
    { key: 'paid', name: 'Paid launch', reached_means: null,
      targets: [{ agent: 'scout', metric: 'mrr', target: 5000, direction: null, tolerance: null }] },
  ],
  tasks: [
    { key: 'recruit', title: 'Recruit design partners', milestone: 'beta', parent: null, blocked_by: [], agent: 'scout', owner: null, for_person: false },
    { key: 'onboard', title: 'Onboard them', milestone: 'beta', parent: null, blocked_by: ['recruit'], agent: null, owner: null, for_person: false },
    { key: 'pricing', title: 'Set pricing', milestone: 'paid', parent: null, blocked_by: ['onboard', 'T-009'], agent: null, owner: ME, for_person: true },
    { key: 'tidy', title: 'Tidy the backlog', milestone: null, parent: null, blocked_by: [], agent: null, owner: null, for_person: false },
  ],
}
const PROPOSAL = {
  id: 'ppr_1', kind: 'plan', proposed_by: 'scout', proposed_at: '2026-11-21T09:00:00Z',
  waiting_on: ME, waiting_on_people: [ME], ask_id: 'oq-7', note: 'From the goal.', plan: PLAN,
  changes: { Milestones: '1. Beta live', Tasks: '4: …' },
}

function project(over = {}) {
  return {
    id: 'prj_1', name: 'Q4 launch', goal: 'Ship it.', status: 'active', visibility: 'members',
    created_by: ME, updated_at: '2026-11-21T10:00:00Z', archived_at: null,
    steward: { kind: 'agent', ref: 'scout' },
    members: [{ email: ME, role: 'creator' }, { email: BOB, role: 'member' }],
    agents: [{ agent_name: 'scout', state: 'active' }],
    my_chats: [], rooms: [], others_chat_count: 0,
    can: { manage_members: true, manage_agents: true, edit: true, archive: true, work: true, contribute: true, set_health: true },
    rollup: { by_status: {}, awaiting_verification: [], stuck: [], stale: [], health: null, health_due: false, last_activity_at: null },
    metrics: [], asks: { mine: [], others_open: 0 }, milestones: [],
    proposal: null,
    ...over,
  }
}

const preview = (p, myEmail = ME) => mount(ProjectPlanProposal, {
  props: { project: p, myEmail },
  attachTo: document.body,
})

async function page(p, props = {}) {
  projects.fetchProject = vi.fn()
  projects.pageState = vi.fn(() => ({ data: p, loaded: true, loading: false, error: null, notFound: false }))
  const w = mount(ProjectPage, {
    props: { projectId: 'prj_1', myEmail: ME, ...props },
    global: { plugins: [router] },
    attachTo: document.body,
  })
  await flushPromises()
  return w
}

beforeEach(async () => {
  document.body.innerHTML = ''
  localStorage.clear()
  setActivePinia(createPinia())
  vi.clearAllMocks()
  portal = useClientPortalStore()
  projects = useProjectsStore()
  portal.fetchAsks = vi.fn()
  portal.asksLoaded = true
  portal.agents = [{ name: 'scout' }]
  router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:p(.*)*', name: 'any', component: { template: '<div/>' } }] })
  await router.push('/workspace/projects/prj_1')
  await router.isReady()
})

describe('ent#1234 — the plan preview', () => {
  it('lists every milestone with its targets, every task under its milestone, and each link', () => {
    preview(project({ proposal: PROPOSAL }))
    const ms = [...document.querySelectorAll('[data-testid^="project-plan-milestone-"]')].map((e) => e.dataset.testid)
    expect(ms).toEqual(['project-plan-milestone-beta', 'project-plan-milestone-paid'])
    expect(byTestId('project-plan-milestone-beta').textContent).toContain('Ten design partners use it.')
    expect(byTestId('project-plan-target-beta-0').textContent).toMatch(/partners.*scout.*≥\s*10/)
    expect(byTestId('project-plan-target-paid-0').textContent).toMatch(/mrr.*5,?000/)
    // every task, under its own milestone; one under none in its own group
    for (const t of PLAN.tasks) expect(byTestId(`project-plan-task-${t.key}`)).not.toBeNull()
    expect(byTestId('project-plan-milestone-beta').contains(byTestId('project-plan-task-onboard'))).toBe(true)
    expect(byTestId('project-plan-milestone-paid').contains(byTestId('project-plan-task-pricing'))).toBe(true)
    expect(byTestId('project-plan-unplaced').contains(byTestId('project-plan-task-tidy'))).toBe(true)
    // links: a plan key reads as that task's title, an existing task as its id
    expect(byTestId('project-plan-edges-onboard').textContent).toContain('after Recruit design partners')
    expect(byTestId('project-plan-edges-pricing').textContent).toContain('Onboard them')
    expect(byTestId('project-plan-edges-pricing').textContent).toContain('T-009')
    expect(byTestId('project-plan-edges-recruit')).toBeNull()
    expect(byTestId('project-plan-task-pricing').textContent).toMatch(/for a person/i)
    expect(byTestId('project-plan-proposal').textContent).toContain('From the goal.')
    // a bounded region: the plan scrolls inside its box, never grows the page
    const region = byTestId('project-plan-preview')
    expect(region.getAttribute('role')).toBe('region')
    expect(region.className).toMatch(/max-h-/)
  })

  it('Approve answers the ask once and reloads the project', async () => {
    portal.answerAsk = vi.fn(async () => ({ id: 'oq-7', status: 'answered' }))
    preview(project({ proposal: PROPOSAL }))
    const approve = byTestId('project-plan-approve')
    approve.click()
    approve.click()
    await flushPromises()
    expect(portal.answerAsk).toHaveBeenCalledTimes(1)
    expect(portal.answerAsk).toHaveBeenCalledWith('oq-7', { response: 'Approve', responseText: null })
    // the page is re-read from the server: an approved plan is the server's, never built here
    expect(portalHttp.get.mock.calls.map((c) => c[0])).toContain('/api/enterprise/projects/prj_1')
  })

  it('Decline needs a reason, then sends it with the answer', async () => {
    portal.answerAsk = vi.fn(async () => ({ id: 'oq-7', status: 'answered' }))
    const w = preview(project({ proposal: PROPOSAL }))
    byTestId('project-plan-decline').click()
    await flushPromises()
    byTestId('project-plan-decline-send').click()
    await flushPromises()
    expect(portal.answerAsk).not.toHaveBeenCalled()
    expect(byTestId('project-plan-decline-form').textContent).toMatch(/Say why/)
    await w.find('[data-testid="project-plan-decline-form"] textarea').setValue('Too much for one quarter.')
    byTestId('project-plan-decline-send').click()
    await flushPromises()
    expect(portal.answerAsk).toHaveBeenCalledTimes(1)
    expect(portal.answerAsk).toHaveBeenCalledWith('oq-7', { response: 'Decline', responseText: 'Too much for one quarter.' })
  })

  it('a failed answer stays on screen next to the controls', async () => {
    portal.answerAsk = vi.fn(async () => { throw { response: { data: { detail: { message: 'This ask expired before it was answered.' } } } } })
    preview(project({ proposal: PROPOSAL }))
    byTestId('project-plan-approve').click()
    await flushPromises()
    expect(byTestId('project-plan-proposal').textContent).toContain('This ask expired before it was answered.')
  })

  it('someone it does not wait on reads who decides, with nothing to act on', () => {
    preview(project({ proposal: PROPOSAL }), BOB)
    expect(byTestId('project-plan-approve')).toBeNull()
    expect(byTestId('project-plan-decline')).toBeNull()
    expect(byTestId('project-plan-proposal').textContent).toContain(`Waiting on ${ME}`)
    expect(byTestId('project-plan-task-recruit')).not.toBeNull()
  })

  it('a members request is not a plan: no preview', () => {
    preview(project({ proposal: { ...PROPOSAL, kind: 'changes', plan: undefined } }))
    expect(byTestId('project-plan-proposal')).toBeNull()
  })
})

describe('ent#1234 — on the project page', () => {
  it('nothing from the plan is in the task list before approval', async () => {
    projects.fetchTasks = vi.fn()
    projects.tasks = { prj_1: { loaded: true, error: null, rows: [] } }
    const w = await page(project({ proposal: PROPOSAL }))
    expect(byTestId('project-plan-proposal')).not.toBeNull()
    w.vm.$.setupState.tab = 'tasks'
    await flushPromises()
    const tasks = w.findComponent(ProjectTasks)
    expect(tasks.exists()).toBe(true)
    for (const t of PLAN.tasks) expect(tasks.text()).not.toContain(t.title)
  })

  it('no plan yet: an editor is offered to write it, and it opens the milestone editor', async () => {
    await page(project())
    const empty = byTestId('project-plan-empty')
    expect(empty).not.toBeNull()
    expect(empty.textContent).toMatch(/scout/)
    const write = byTestId('project-plan-write')
    expect(write.textContent).toContain('Write the plan yourself')
    // one way in for the editor, not two
    expect(byTestId('project-milestones-start')).toBeNull()
    write.click()
    await flushPromises()
    expect(byTestId('project-milestone-form')).not.toBeNull()
  })

  it('no plan yet: someone who cannot edit the goal reads it, with no button', async () => {
    await page(project({ can: { ...project().can, edit: false } }), { myEmail: BOB })
    expect(byTestId('project-plan-empty')).not.toBeNull()
    expect(byTestId('project-plan-write')).toBeNull()
  })

  it('a waiting plan replaces the empty state', async () => {
    await page(project({ proposal: PROPOSAL }))
    expect(byTestId('project-plan-empty')).toBeNull()
    expect(byTestId('project-plan-proposal')).not.toBeNull()
  })
})

describe('ent#1234 — the Inbox card', () => {
  it('a plan ask links to the project page for the whole plan', async () => {
    portal.asksAvailable = true
    portal.asks = [{
      id: 'oq-7', agent_name: 'scout', kind: 'approval', title: "Approve scout's plan for “Q4 launch”",
      question: 'scout proposes the plan below.', options: ['Approve', 'Decline'], status: 'pending',
      created_at: '2026-11-21T09:00:00Z', proposal: { Milestones: '1. Beta live', Tasks: '4: …' },
      project: { id: 'prj_1', task_id: null, event: 'plan_proposal' },
    }, {
      id: 'oq-8', agent_name: 'scout', kind: 'approval', title: 'Approve changes', question: 'q',
      options: ['Approve', 'Decline'], status: 'pending', created_at: '2026-11-21T09:00:00Z',
      proposal: { 'Add members': BOB }, project: { id: 'prj_1', task_id: null, event: null },
    }]
    const w = mount(PortalAsks, { global: { stubs: { RouterLink: RouterLinkStub } }, attachTo: document.body })
    await flushPromises()
    const links = w.findAllComponents(RouterLinkStub).filter((l) => l.attributes('data-testid') === 'portal-ask-plan-oq-7')
    expect(links).toHaveLength(1)
    expect(links[0].text()).toBe('Open the whole plan')
    expect(links[0].props('to')).toEqual({ name: 'WorkspaceProject', params: { projectId: 'prj_1' } })
    expect(w.find('[data-testid="portal-ask-plan-oq-8"]').exists()).toBe(false)
  })
})
