// @vitest-environment jsdom
/**
 * The project page, mounted — #3692, #3694 and trinity-enterprise#896.
 *
 *   - #3692: the goal, a task's objective and a log entry's body are agent
 *     markdown and render through `PortalMarkdown` (DOMPurify inside), so
 *     `**bold**`, backticks and `>` become elements rather than printed text;
 *     a plain goal with line breaks still comes out as paragraphs.
 *   - #3694: ONE layout for the whole Overview — a main stack and a side
 *     stack whose widths never change between rows, with the cards placed
 *     deliberately in each (so a later card cannot silently break the rhythm),
 *     and `lg:items-start` kept on the wrapper so an opened ask never resizes
 *     the other column.
 *   - ent#896: the Overview leads with Continue (my newest linked chat, or
 *     Start with the first agent, the others one click away); one line says
 *     how a project moves, dismissable; a Steward card replaces the header
 *     label and tells the agent steward's sweep and schedule honestly; the
 *     Tasks tab states the verification rule and offers Confirm to people who
 *     may; the no-chat empty state names the next action.
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
import ProjectLog from '@/components/portal/projects/ProjectLog.vue'

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

// --- #3692 --------------------------------------------------------------------------

describe('#3692 — the goal renders as markdown', () => {
  it('bold, inline code and a blockquote become elements, not printed markup', async () => {
    await page(project({ goal: 'Ship the **self-serve** plan via `checkout`.\n\n> No regressions.' }))
    const goal = byTestId('project-goal')
    expect(goal.querySelector('strong').textContent).toBe('self-serve')
    expect(goal.querySelector('code').textContent).toBe('checkout')
    expect(goal.querySelector('blockquote').textContent).toContain('No regressions.')
    expect(goal.textContent).not.toContain('**')
    expect(goal.textContent).not.toContain('`')
  })

  it('a plain goal with line breaks keeps its paragraphs', async () => {
    await page(project({ goal: 'First line.\n\nSecond line.' }))
    const paras = byTestId('project-goal').querySelectorAll('p')
    expect([...paras].map((p) => p.textContent)).toEqual(['First line.', 'Second line.'])
  })

  it('the rendered goal keeps the reading width', async () => {
    await page(project())
    expect(byTestId('project-goal').className).toContain('max-w-3xl')
  })

  it('a log entry body renders markdown', async () => {
    projects.fetchLog = vi.fn()
    projects.logs = { prj_1: { rows: [{ id: 7, kind: 'decision', author: 'agent:scout', created_at: '2026-09-30T10:00:00Z', body: 'Chose **tier B** over `tier-a`.' }], loaded: true, error: null } }
    mount(ProjectLog, { props: { projectId: 'prj_1', canContribute: false }, attachTo: document.body })
    await flushPromises()
    const body = byTestId('project-log-body-7')
    expect(body.querySelector('strong').textContent).toBe('tier B')
    expect(body.querySelector('code').textContent).toBe('tier-a')
  })

  it("a task's objective renders markdown in the task detail", async () => {
    const task = { id: 'T-001', title: 'Brief', status: 'active', priority: 'p2', owner: null, agent: null,
      objective: 'Write the **brief**.', done_definition: null, updated_at: '2026-09-30T10:00:00Z', log: [] }
    projects.fetchTasks = vi.fn()
    projects.tasks = { prj_1: { rows: [task], loaded: true, error: null } }
    projects.fetchTask = vi.fn().mockResolvedValue(task)
    mount(ProjectTasks, { props: { projectId: 'prj_1', canContribute: true }, attachTo: document.body })
    await flushPromises()
    byTestId('project-task-T-001').click()
    await flushPromises()
    expect(byTestId('project-task-objective').querySelector('strong').textContent).toBe('brief')
  })
})

// --- #3694 --------------------------------------------------------------------------

const ids = (root) => [...root.querySelectorAll(':scope > [data-testid]')].map((el) => el.getAttribute('data-testid'))

describe('#3694 — one two-column layout for the whole Overview', () => {
  it('places every card deliberately in the main or the side stack, in a fixed order', async () => {
    await page(project())
    expect(ids(byTestId('project-overview-main'))).toEqual([
      'project-continue', 'project-rollup', 'project-work-on-it', 'project-metrics', 'project-my-chats',
    ])
    expect(ids(byTestId('project-overview-side'))).toEqual([
      'project-steward', 'project-health', 'project-needs-you', 'project-members', 'project-agents',
    ])
  })

  it('the wrapper is the one grid, main spans two tracks, and items-start keeps #3574 fixed', async () => {
    await page(project())
    const grid = byTestId('project-overview')
    expect(grid.className).toContain('lg:grid-cols-3')
    expect(grid.className).toContain('lg:items-start')
    expect(byTestId('project-overview-main').className).toContain('lg:col-span-2')
    // No card is placed outside the two stacks: the grid's only children are them.
    expect(ids(grid)).toEqual(['project-overview-main', 'project-overview-side'])
  })

  it('a guest gets the same stacks with only what was shared', async () => {
    await page(project({ guest: true, can: {} }))
    expect(byTestId('project-overview-main')).not.toBeNull()
    expect(byTestId('project-steward')).toBeNull()
    expect(byTestId('project-health')).toBeNull()
  })
})

// --- ent#896 ------------------------------------------------------------------------

describe('ent#896 — Continue leads the Overview', () => {
  it('continues my newest linked chat, with the other agents one click away', async () => {
    const w = await page(project({
      my_chats: [
        { id: 's-old', agent_name: 'bard', title: 'Old', last_message_at: '2026-09-01T10:00:00Z' },
        { id: 's-new', agent_name: 'scout', title: 'Plan', last_message_at: '2026-09-29T10:00:00Z' },
      ],
    }))
    const primary = byTestId('project-continue-primary')
    expect(primary.textContent).toContain('Continue with scout')
    primary.click()
    expect(w.emitted('open-thread')[0][0]).toEqual({ id: 's-new', agent_name: 'scout' })
    const other = byTestId('project-continue-other-bard')
    expect(other.textContent).toContain('bard')
    other.click()
    expect(w.emitted('open-thread')[1][0]).toEqual({ id: 's-old', agent_name: 'bard' })
  })

  it('with no linked chat it starts one, links it, and the empty state names what follows', async () => {
    portal.createSession = vi.fn().mockResolvedValue({ id: 'new-s' })
    projects.link = vi.fn().mockResolvedValue()
    const w = await page(project())
    const card = byTestId('project-continue')
    expect(byTestId('project-continue-primary').textContent).toContain('Start with scout')
    expect(card.textContent).toContain('Start with scout, and it will record what it does here')
    byTestId('project-continue-primary').click()
    await flushPromises()
    expect(portal.createSession).toHaveBeenCalledWith('scout')
    expect(projects.link).toHaveBeenCalledWith('prj_1', 'thread', 'new-s')
    expect(w.emitted('open-thread')[0][0]).toEqual({ id: 'new-s', agent_name: 'scout' })
  })

  it('with no agent the next action is unchanged: add one in Members & access', async () => {
    await page(project({ agents: [] }))
    expect(byTestId('project-continue-primary')).toBeNull()
    expect(byTestId('project-continue').textContent).toContain('Add one in Members & access')
  })

  it('Continue is the first focusable control of the Overview', async () => {
    await page(project())
    const overview = byTestId('project-overview')
    const first = overview.querySelector('button, a[href], input, select, textarea, [tabindex]:not([tabindex="-1"])')
    expect(first.getAttribute('data-testid')).toBe('project-continue-primary')
  })
})

describe('ent#896 — how a project moves, said once', () => {
  it('shows the line and hides it for good on dismiss', async () => {
    await page(project())
    const line = byTestId('project-how-it-moves')
    expect(line.textContent).toContain('Log')
    expect(line.textContent.toLowerCase()).toContain('steward')
    byTestId('project-how-it-moves-dismiss').click()
    await flushPromises()
    expect(byTestId('project-how-it-moves')).toBeNull()
    document.body.innerHTML = ''
    await page(project())
    expect(byTestId('project-how-it-moves')).toBeNull()
  })
})

describe('ent#896 — the Steward card', () => {
  it('replaces the header label', async () => {
    await page(project())
    expect(document.querySelector('header').textContent).not.toContain('Steward ·')
    expect(byTestId('project-steward')).not.toBeNull()
  })

  it('an agent steward with no sweep and no schedule says both plainly', async () => {
    await page(project({ steward: { kind: 'agent', ref: 'scout', last_sweep_at: null, schedule: { state: 'none', next_run_at: null } } }))
    const card = byTestId('project-steward')
    expect(card.textContent).toContain('scout')
    expect(byTestId('project-steward-sweep').textContent).toContain('No sweep recorded yet')
    expect(byTestId('project-steward-schedule').textContent).toContain('No sweep scheduled')
    expect(card.textContent).toContain("Trinity doesn't run it")
  })

  it('a sweep and a readable schedule show their times; a hidden one says scheduled without a time', async () => {
    await page(project({ steward: { kind: 'agent', ref: 'scout', last_sweep_at: '2026-09-29T09:00:00Z', schedule: { state: 'scheduled', next_run_at: '2026-10-20T09:00:00Z' } } }))
    expect(byTestId('project-steward-sweep').textContent).toContain('Last sweep')
    expect(byTestId('project-steward-sweep').textContent).not.toContain('No sweep recorded')
    expect(byTestId('project-steward-schedule').textContent).toContain('Next run')
    document.body.innerHTML = ''
    await page(project({ steward: { kind: 'agent', ref: 'scout', last_sweep_at: null, schedule: { state: 'hidden', next_run_at: null } } }))
    expect(byTestId('project-steward-schedule').textContent).toBe('Scheduled')
  })

  it('a backend without the sweep facts shows the agent and health, and no schedule line', async () => {
    await page(project({ steward: { kind: 'agent', ref: 'scout' } }))
    expect(byTestId('project-steward').textContent).toContain('scout')
    expect(byTestId('project-steward-schedule')).toBeNull()
    expect(byTestId('project-steward-sweep')).toBeNull()
  })

  it('shows health and a due note from the roll-up', async () => {
    await page(project({
      steward: { kind: 'agent', ref: 'scout', last_sweep_at: null, schedule: { state: 'none', next_run_at: null } },
      rollup: { by_status: {}, awaiting_verification: [], stuck: [], stale: [], health_due: true, last_activity_at: null,
        health: { state: 'at-risk', note: 'Vendor late.', at: '2026-09-01T00:00:00Z', by: 'agent:scout' } },
    }))
    const card = byTestId('project-steward').textContent
    expect(card).toContain('At risk')
    expect(card).toContain('update due')
  })

  it('a person steward who is me sees what waits and a way to the I steward tab', async () => {
    const w = await page(project({
      rollup: { by_status: { 'pending-verification': 2, blocked: 1 }, stuck: [{ id: 'T-3' }], stale: [],
        awaiting_verification: [{ id: 'T-1' }, { id: 'T-2' }], health: null, health_due: false, last_activity_at: null },
    }))
    const card = byTestId('project-steward')
    expect(card.textContent).toContain('You')
    expect(card.textContent).toContain('2 tasks to verify')
    expect(card.textContent).toContain('1 task blocked')
    byTestId('project-steward-open-stewarding').click()
    expect(w.emitted('open-stewarding')).toHaveLength(1)
  })

  it('another person as steward is named, with no I steward link', async () => {
    await page(project({ steward: { kind: 'person', ref: 'bob@example.com' } }))
    expect(byTestId('project-steward').textContent).toContain('bob@example.com')
    expect(byTestId('project-steward-open-stewarding')).toBeNull()
  })
})

describe('ent#896 — the verification rule on Tasks', () => {
  const PENDING = { id: 'T-002', title: 'Deck', status: 'pending-verification', priority: 'p2', owner: null, agent: 'scout',
    objective: null, done_definition: null, updated_at: '2026-09-30T10:00:00Z', log: [] }

  it('states the rule and offers Confirm to a contributor, which marks the task done', async () => {
    projects.fetchTasks = vi.fn()
    projects.tasks = { prj_1: { rows: [PENDING], loaded: true, error: null } }
    projects.updateTask = vi.fn().mockResolvedValue({})
    mount(ProjectTasks, { props: { projectId: 'prj_1', canContribute: true }, attachTo: document.body })
    await flushPromises()
    expect(byTestId('project-tasks-verification-rule').textContent).toContain('confirm')
    byTestId('project-task-confirm-T-002').click()
    await flushPromises()
    expect(projects.updateTask).toHaveBeenCalledWith('prj_1', 'T-002', { status: 'done' })
  })

  it.each([
    ['a viewer', { canContribute: false }],
    ['an archived project', { canContribute: true, archived: true }],
  ])('%s gets the rule but no Confirm', async (_label, props) => {
    projects.fetchTasks = vi.fn()
    projects.tasks = { prj_1: { rows: [PENDING], loaded: true, error: null } }
    mount(ProjectTasks, { props: { projectId: 'prj_1', ...props }, attachTo: document.body })
    await flushPromises()
    expect(byTestId('project-tasks-verification-rule')).not.toBeNull()
    expect(byTestId('project-task-confirm-T-002')).toBeNull()
  })

  it('no rule line when nothing awaits verification', async () => {
    projects.fetchTasks = vi.fn()
    projects.tasks = { prj_1: { rows: [{ ...PENDING, status: 'active' }], loaded: true, error: null } }
    mount(ProjectTasks, { props: { projectId: 'prj_1', canContribute: true }, attachTo: document.body })
    await flushPromises()
    expect(byTestId('project-tasks-verification-rule')).toBeNull()
  })
})
