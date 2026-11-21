// @vitest-environment jsdom
/**
 * trinity-enterprise#1233 — the goal's milestones on the project page, mounted.
 *
 *   - the rows sit under the goal on the Overview, in the goal's order, each
 *     with its state, number against target and tasks done;
 *   - an overdue reading is greyed and labelled stale, never drawn as current;
 *     an overdue health update above the list says so;
 *   - a person who can't change the goal sees no edit control;
 *   - a row opens the Tasks tab filtered to that milestone;
 *   - a project without milestones renders the Overview exactly as before.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
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
import { useProjectsStore } from '@/stores/projects'
import ProjectPage from '@/components/portal/projects/ProjectPage.vue'
import ProjectTasks from '@/components/portal/projects/ProjectTasks.vue'

globalThis.ResizeObserver = globalThis.ResizeObserver || class {
  observe() {}
  unobserve() {}
  disconnect() {}
}

const byTestId = (id) => document.querySelector(`[data-testid="${id}"]`)
const ME = 'eva@example.com'
let portal
let projects
let router

function project(over = {}) {
  return {
    id: 'prj_1', name: 'Q4 launch', goal: 'Ship it.', status: 'active', visibility: 'members',
    created_by: ME, updated_at: '2026-09-29T10:00:00Z', archived_at: null,
    steward: { kind: 'person', ref: ME },
    members: [{ email: ME, role: 'creator' }],
    agents: [{ agent_name: 'scout', state: 'active' }, { agent_name: 'bard', state: 'active' }],
    my_chats: [], rooms: [], others_chat_count: 0,
    can: { manage_members: true, manage_agents: true, edit: true, archive: true, work: true, contribute: true, set_health: true },
    rollup: { by_status: {}, awaiting_verification: [], stuck: [], stale: [], health: null, health_due: false, last_activity_at: null },
    metrics: [], asks: { mine: [], others_open: 0 },
    ...over,
  }
}

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
  portal.agents = [{ name: 'scout' }, { name: 'bard' }]
  router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:p(.*)*', component: { template: '<div/>' } }] })
  await router.push('/workspace/projects/prj_1')
  await router.isReady()
})


const target = (over = {}) => ({
  agent_name: 'scout', metric: 'partners', label: 'Design partners', unit: '', target: 10, direction: 'up_good',
  direction_set: null, actual: 4, actual_at: '2026-11-20T10:00:00Z', stale: false, freshness: 'fresh',
  agent_inactive: false, met: false, gap: null,
})
const milestone = (id, name, position, over = {}) => ({
  id, name, position, reached_means: '', reached_at: null, reached_by: null, state: 'in_progress',
  targets: [], worst: null, suggest_reached: false, tasks: { total: 0, done: 0, attention: 0 }, ...over,
})
const MILESTONES = [
  milestone('ms_a', 'Beta open', 0, { state: 'reached', tasks: { total: 2, done: 2, attention: 0 } }),
  milestone('ms_b', 'Ten partners', 1, { targets: [target()], worst: target(), tasks: { total: 5, done: 3, attention: 0 } }),
  milestone('ms_c', 'Churn down', 2, {
    targets: [{ ...target(), metric: 'churn', label: 'Churn', stale: true, freshness: 'stale' }],
    worst: { ...target(), metric: 'churn', label: 'Churn', stale: true, freshness: 'stale' },
  }),
]

describe('ent#1233 — milestones on the Overview', () => {
  it('rows render under the goal in the goal order, with state, number and tasks', async () => {
    await page(project({ milestones: MILESTONES }))
    const rows = [...document.querySelectorAll('[data-testid="project-milestone-list"] > li')]
    expect(rows.map((r) => r.dataset.testid)).toEqual(['project-milestone-ms_a', 'project-milestone-ms_b', 'project-milestone-ms_c'])
    expect(byTestId('project-milestone-state-ms_a').textContent).toContain('Reached')
    expect(byTestId('project-milestone-number-ms_b').textContent).toMatch(/4\s*≥ 10/)
    expect(byTestId('project-milestone-tasks-ms_b').textContent).toBe('3 of 5 tasks done')
    // under the goal: the block follows the header and precedes the overview grid
    const block = byTestId('project-milestones')
    expect(block.compareDocumentPosition(byTestId('project-goal')) & Node.DOCUMENT_POSITION_PRECEDING).toBeTruthy()
    expect(block.compareDocumentPosition(byTestId('project-overview')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('a stale reading is greyed and says stale; a fresh one is not', async () => {
    await page(project({ milestones: MILESTONES }))
    const stale = byTestId('project-milestone-number-ms_c')
    expect(stale.dataset.reading).toBe('stale')
    expect(byTestId('project-milestone-reading-ms_c').textContent).toMatch(/Stale/)
    expect(stale.querySelector('span').className).toContain('text-gray-500')
    const fresh = byTestId('project-milestone-number-ms_b')
    expect(fresh.dataset.reading).toBe('current')
    expect(byTestId('project-milestone-reading-ms_b')).toBeNull()
    expect(fresh.querySelector('span').className).toContain('text-gray-900')
  })

  it('an overdue health update sits above the list and says it is overdue', async () => {
    const health = { state: 'on_track', note: 'Going well', by: ME, at: '2026-10-01T10:00:00Z' }
    await page(project({ milestones: MILESTONES, rollup: { ...project().rollup, health, health_due: true } }))
    const h = byTestId('project-milestones-health')
    expect(h.dataset.overdue).toBe('true')
    expect(h.textContent).toContain('Update overdue')
    expect(h.compareDocumentPosition(byTestId('project-milestone-list')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('an editor gets add, edit and mark-reached; a member who cannot edit sees none', async () => {
    const suggested = MILESTONES.map((m) => (m.id === 'ms_b' ? { ...m, suggest_reached: true } : m))
    const w = await page(project({ milestones: suggested }))
    expect(byTestId('project-milestone-add')).not.toBeNull()
    expect(byTestId('project-milestone-edit-ms_b')).not.toBeNull()
    expect(byTestId('project-milestone-reach-ms_b')).not.toBeNull()
    w.unmount()
    document.body.innerHTML = ''
    await page(project({ milestones: suggested, can: { ...project().can, edit: false } }))
    expect(byTestId('project-milestones')).not.toBeNull()
    expect(byTestId('project-milestone-add')).toBeNull()
    expect(byTestId('project-milestone-edit-ms_b')).toBeNull()
    expect(byTestId('project-milestone-reach-ms_b')).toBeNull()
    expect(byTestId('project-milestones-start')).toBeNull()
  })

  it('clicking a row opens the Tasks tab filtered to that milestone', async () => {
    projects.fetchTasks = vi.fn()
    projects.tasks = { prj_1: { loaded: true, error: null, rows: [
      { id: 'T-001', title: 'Call Acme', status: 'active', priority: 'p2', milestone: 'ms_b', blocked_by: [] },
      { id: 'T-002', title: 'Fix churn', status: 'todo', priority: 'p2', milestone: 'ms_c', blocked_by: [] },
      { id: 'T-003', title: 'Loose end', status: 'todo', priority: 'p2', milestone: null, blocked_by: [] },
    ] } }
    const w = await page(project({ milestones: MILESTONES }))
    await byTestId('project-milestone-open-ms_b').click()
    await flushPromises()
    const tasks = w.findComponent(ProjectTasks)
    expect(tasks.exists()).toBe(true)
    expect(tasks.props('milestoneFilter')).toBe('ms_b')
    expect(byTestId('project-tasks-milestone').value).toBe('ms_b')
    const text = tasks.text()
    expect(text).toContain('Call Acme')
    expect(text).not.toContain('Fix churn')
    expect(text).not.toContain('Loose end')
  })

  it('a project without milestones keeps the Overview as it was', async () => {
    await page(project())
    expect(byTestId('project-milestones')).toBeNull()
    const grid = byTestId('project-overview')
    expect([...grid.children].map((c) => c.dataset.testid)).toEqual(['project-overview-main', 'project-overview-side'])
    // the editor's one entry point is a header action, not a new card
    expect(byTestId('project-milestones-start')).not.toBeNull()
  })
})

// BaseModal teleports to <body>: drive the DOM directly.
const fill = async (sel, value) => {
  const el = document.querySelector(sel)
  el.value = value
  el.dispatchEvent(new Event('input'))
  await flushPromises()
}
const submitForm = async () => {
  byTestId('project-milestone-form').dispatchEvent(new Event('submit', { cancelable: true }))
  await flushPromises()
}

describe('ent#1233 — the milestone dialog', () => {
  it('adds a milestone, then its target from the metric options (linked ones included)', async () => {
    const ProjectMilestoneModal = (await import('@/components/portal/projects/ProjectMilestoneModal.vue')).default
    projects.metricOptions = vi.fn(async () => [{ agent_name: 'scout', metrics: [{ name: 'partners', label: 'Design partners', unit: '' }] }])
    projects.createMilestone = vi.fn(async () => ({ id: 'ms_new' }))
    projects.setMilestoneTargets = vi.fn(async () => ({}))
    const w = mount(ProjectMilestoneModal, { props: { modelValue: true, projectId: 'prj_1' }, attachTo: document.body })
    await flushPromises()
    expect(projects.metricOptions).toHaveBeenCalledWith('prj_1', { all: true })
    await fill('#milestone-name', 'Ten partners')
    await byTestId('project-milestone-target-add').click()
    await flushPromises()
    await fill('#milestone-target-value-0', '10')
    await submitForm()
    expect(projects.createMilestone).toHaveBeenCalledWith('prj_1', { name: 'Ten partners' })
    expect(projects.setMilestoneTargets).toHaveBeenCalledWith('prj_1', 'ms_new', [
      { agent_name: 'scout', metric: 'partners', target: 10, direction: null, tolerance: null },
    ])
    expect(w.emitted('update:modelValue')?.at(-1)).toEqual([false])
  })

  it('a target that is not a number is named and nothing is sent', async () => {
    const ProjectMilestoneModal = (await import('@/components/portal/projects/ProjectMilestoneModal.vue')).default
    projects.metricOptions = vi.fn(async () => [{ agent_name: 'scout', metrics: [{ name: 'partners', label: 'P' }] }])
    projects.createMilestone = vi.fn()
    mount(ProjectMilestoneModal, { props: { modelValue: true, projectId: 'prj_1' }, attachTo: document.body })
    await flushPromises()
    await fill('#milestone-name', 'A')
    await byTestId('project-milestone-target-add').click()
    await flushPromises()
    await submitForm()
    expect(projects.createMilestone).not.toHaveBeenCalled()
    expect(byTestId('project-milestone-target-0').textContent).toMatch(/The target is a number/)
  })
})
