// @vitest-environment jsdom
/**
 * ent#1235 — the project's lifecycle and mode on the page, and the runs that
 * wait for a person's go.
 *
 * The server is the authority for every gate; the page decides what to OFFER,
 * what to send and what to say, so each is proven on the rendered component
 * (#2918):
 *   - the state badge reads the lifecycle (Not started / Running / Stopped /
 *     Done / Archived) with who changed it and when; an unknown value reads
 *     "Unknown" and offers neither Start nor Stop — it never breaks the page;
 *   - Start project / Stop project for edit rights only; Stop asks first and
 *     names the runs still finishing; a stopped project carries a banner;
 *   - the mode is a selector for edit rights and read-only for everyone else;
 *   - the runs waiting card: checkboxes, Approve selected / Decline, one outcome
 *     per id in words, and "Nothing is waiting for your go" when empty;
 *   - a task: "Needs my go" sends `needs_go`, a task rejected twice says why,
 *     a live run is named, and Start is replaced by the reason on a project
 *     that isn't running; a refused Start names the refusal.
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
import ProjectRunsWaiting from '@/components/portal/projects/ProjectRunsWaiting.vue'
import ProjectTasks from '@/components/portal/projects/ProjectTasks.vue'
import ProjectTaskStart from '@/components/portal/projects/ProjectTaskStart.vue'
import {
  STATUS_BADGE, lifecycleActions, modeLabel, needsDecisionReason, projectMode, projectState, runLabel,
  runOutcomeLabel, stateBy, stateLabel, statusLabel, stopMessage,
} from '@/components/portal/projects/projectsUtils'

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
    id: 'prj_1', name: 'Q4 launch', goal: 'Ship it.', status: 'active', state: 'running', mode: 'track',
    status_by: ME, status_at: '2026-11-12T08:00:00Z', visibility: 'members',
    created_by: ME, updated_at: '2026-11-12T08:00:00Z', archived_at: null,
    steward: { kind: 'person', ref: ME },
    members: [{ email: ME, role: 'creator' }],
    agents: [{ agent_name: 'scout', state: 'active' }],
    my_chats: [], rooms: [], others_chat_count: 0,
    can: { manage_members: true, manage_agents: true, edit: true, archive: true, work: true, contribute: true, set_health: true },
    rollup: { by_status: {}, awaiting_verification: [], stuck: [], stale: [], health: null, health_due: false, last_activity_at: null },
    metrics: [], asks: { mine: [], others_open: 0 },
    ...over,
  }
}

async function page(p) {
  projects.fetchProject = vi.fn()
  projects.pageState = vi.fn(() => ({ data: p, loaded: true, loading: false, error: null, notFound: false }))
  projects.startProject = vi.fn().mockResolvedValue({})
  projects.stopProject = vi.fn().mockResolvedValue({})
  projects.setMode = vi.fn().mockResolvedValue({})
  projects.update = vi.fn().mockResolvedValue({})
  projects.fetchRunsWaiting = vi.fn().mockResolvedValue([])
  const w = mount(ProjectPage, {
    props: { projectId: 'prj_1', myEmail: ME },
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
  router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:p(.*)*', component: { template: '<div/>' } }] })
  await router.push('/workspace/projects/prj_1')
  await router.isReady()
})

// --- the rules in words ----------------------------------------------------------------

describe('ent#1235 — state, mode and run words', () => {
  it('Paused now reads Stopped and Active reads Running; a new project reads Not started', () => {
    expect(statusLabel('active')).toBe('Running')
    expect(statusLabel('paused')).toBe('Stopped')
    expect(statusLabel('not_started')).toBe('Not started')
    expect(STATUS_BADGE.not_started).toBe('neutral')
  })

  it('the state comes from the server, falls back to the stored status, and archived wins', () => {
    expect(projectState({ state: 'stopped', status: 'paused' })).toBe('stopped')
    expect(projectState({ status: 'paused' })).toBe('stopped')          // an older server sends no `state`
    expect(projectState({ state: 'running', status: 'active', archived_at: 'x' })).toBe('archived')
    expect(projectState({ state: 'hibernating', status: 'active' })).toBe('running')
  })

  it('an unknown value reads Unknown and offers no lifecycle verb (never a guess that lets work start)', () => {
    expect(projectState({ status: 'frozen' })).toBe('unknown')
    expect(stateLabel('unknown')).toBe('Unknown')
    expect(lifecycleActions({ status: 'frozen', can: { edit: true } }).primary).toBeNull()
    expect(modeLabel('turbo')).toBe('Unknown')
    // An unknown MODE offers nothing either, even on a known state.
    expect(lifecycleActions({ status: 'active', mode: 'turbo', can: { edit: true } })).toEqual({ primary: null, markDone: false })
    expect(lifecycleActions({ status: 'active', can: { edit: true } }).primary.verb).toBe('stop')   // no mode reads Track
    expect(runLabel({ state: 'teleported' })).toBe('Unknown')
    expect(runOutcomeLabel('some_new_code')).toBe('Not done (some_new_code)')
  })

  it('a project read without a mode is Track, the safe default', () => {
    expect(projectMode({})).toBe('track')
    expect(projectMode({ mode: 'auto' })).toBe('auto')
  })

  it('Start or Stop is offered by state, to edit rights only', () => {
    const can = { edit: true }
    expect(lifecycleActions({ status: 'not_started', can }).primary.verb).toBe('start')
    expect(lifecycleActions({ status: 'active', can }).primary.verb).toBe('stop')
    expect(lifecycleActions({ status: 'paused', can }).primary.verb).toBe('start')
    expect(lifecycleActions({ status: 'done', can }).primary.label).toBe('Reopen project')
    expect(lifecycleActions({ status: 'active', can: { edit: false } }).primary).toBeNull()
    expect(lifecycleActions({ status: 'active', can, archived_at: 'x' }).primary).toBeNull()
  })

  it('who changed the lifecycle, as a verb', () => {
    expect(stateBy({ status: 'active', status_by: ME }, ME)).toBe('Started by you')
    expect(stateBy({ status: 'paused', status_by: 'alex@example.com' }, ME)).toBe('Stopped by alex@example.com')
    expect(stateBy({ status: 'not_started' }, ME)).toBe('')
  })

  it('the stop message names the runs still finishing, and never claims none when it is not told', () => {
    expect(stopMessage({ runs_live: [{ task_id: 'T-003', agent_name: 'scout' }] })).toContain('still finishing: T-003 on scout.')
    expect(stopMessage({ runs_live: [] })).toContain('No run is under way.')
    expect(stopMessage({})).toContain('Runs already under way finish.')
  })

  it('a run ended reads by how it ended', () => {
    expect(runLabel({ state: 'ended', end_status: 'gated' })).toBe('Stopped by a gate')
    expect(runLabel({ state: 'proposed' })).toBe('Waiting for a go')
  })

  it('the needs-a-decision reason is the last send-back, without the marker', () => {
    const task = { needs_decision: true, log: [{ body: 'Tests missing. (rejected twice — needs a decision)' }] }
    expect(needsDecisionReason(task)).toBe('Rejected twice. The last reason: Tests missing.')
    expect(needsDecisionReason({ needs_decision: true, log: [] })).toBe('Rejected twice. A person decides what happens next.')
    expect(needsDecisionReason({ needs_decision: false })).toBe('')
  })
})

// --- the page ---------------------------------------------------------------------------

describe('ent#1235 — the project page', () => {
  it('a running project shows its state with who and when, and Stop asks first, naming the runs', async () => {
    await page(project({ runs_live: [{ id: 'R-1', task_id: 'T-003', agent_name: 'scout' }] }))
    expect(byTestId('project-state').textContent).toContain('Running')
    expect(byTestId('project-state-by').textContent).toContain('Started by you')
    expect(byTestId('project-stopped-banner')).toBeNull()
    expect(byTestId('project-start')).toBeNull()

    byTestId('project-stop').click()
    await flushPromises()
    expect(byTestId('confirm-dialog-message').textContent).toContain('still finishing: T-003 on scout.')
    expect(projects.stopProject).not.toHaveBeenCalled()
    byTestId('confirm-dialog-confirm').click()
    await flushPromises()
    expect(projects.stopProject).toHaveBeenCalledWith('prj_1')
  })

  it('a project not started offers Start project, which starts it', async () => {
    await page(project({ status: 'not_started', state: 'not_started', status_by: null, status_at: null }))
    expect(byTestId('project-state').textContent).toContain('Not started')
    expect(byTestId('project-state-by')).toBeNull()
    byTestId('project-start').click()
    await flushPromises()
    expect(projects.startProject).toHaveBeenCalledWith('prj_1')
  })

  it('a stopped project carries the banner and offers Start again', async () => {
    await page(project({ status: 'paused', state: 'stopped' }))
    expect(byTestId('project-state').textContent).toContain('Stopped')
    expect(byTestId('project-stopped-banner').textContent).toContain('This project is stopped.')
    expect(byTestId('project-start')).not.toBeNull()
  })

  it('without edit rights: no Start or Stop, and the mode is read-only', async () => {
    await page(project({ mode: 'propose', can: { contribute: true, edit: false } }))
    expect(byTestId('project-stop')).toBeNull()
    expect(byTestId('project-start')).toBeNull()
    expect(byTestId('project-mode-select')).toBeNull()
    expect(byTestId('project-mode').textContent).toContain('Mode: Propose')
  })

  it('with edit rights the mode is a selector, and a change is sent', async () => {
    await page(project())
    const select = byTestId('project-mode-select') || document.getElementById('project-mode')
    expect(select).not.toBeNull()
    select.value = 'auto'
    select.dispatchEvent(new Event('change'))
    await flushPromises()
    expect(projects.setMode).toHaveBeenCalledWith('prj_1', 'auto')
  })

  it('an unknown status never breaks the page: Unknown, and nothing offered', async () => {
    await page(project({ status: 'frozen', state: undefined, mode: 'warp' }))
    expect(byTestId('project-page')).not.toBeNull()
    expect(byTestId('project-state').textContent).toContain('Unknown')
    expect(byTestId('project-start')).toBeNull()
    expect(byTestId('project-stop')).toBeNull()
  })

  it('an unknown mode on a running project offers no Start, Stop or Mark done', async () => {
    await page(project({ status: 'active', state: 'running', mode: 'turbo' }))
    expect(byTestId('project-page')).not.toBeNull()
    expect(byTestId('project-start')).toBeNull()
    expect(byTestId('project-stop')).toBeNull()
    expect(byTestId('project-mark-done')).toBeNull()
  })

  it('the runs waiting card shows in Propose and Auto, not in Track', async () => {
    await page(project({ mode: 'track' }))
    expect(byTestId('project-runs-waiting')).toBeNull()
    document.body.innerHTML = ''
    await page(project({ mode: 'propose' }))
    expect(byTestId('project-runs-waiting')).not.toBeNull()
    expect(byTestId('project-runs-waiting-empty').textContent).toContain('Nothing is waiting for your go')
  })
})

// --- the runs waiting card --------------------------------------------------------------

const RUN = (id, task, over = {}) => ({
  id, task_id: task, agent_name: 'scout', kind: 'dispatch', state: 'proposed', mode: 'propose',
  requested_by: 'agent:steward', requested_at: '2026-11-12T08:00:00Z', ...over,
})

async function waiting(runs, props = {}) {
  projects.fetchRunsWaiting = vi.fn().mockResolvedValueOnce(runs).mockResolvedValue([])
  projects.decideRuns = vi.fn().mockResolvedValue([
    { id: 'R-1', outcome: 'started' }, { id: 'R-2', outcome: 'already_decided' },
  ])
  const w = mount(ProjectRunsWaiting, {
    props: { projectId: 'prj_1', canContribute: true, myEmail: ME, ...props },
    attachTo: document.body,
  })
  await flushPromises()
  return w
}

describe('ent#1235 — runs waiting for a go', () => {
  it('approves the selected runs and says each one\'s outcome', async () => {
    await waiting([RUN('R-1', 'T-001'), RUN('R-2', 'T-002'), RUN('R-3', 'T-003')])
    expect(byTestId('project-run-waiting-R-1').textContent).toContain('steward (steward)')
    expect(byTestId('project-runs-approve').disabled).toBe(true)        // nothing picked yet
    for (const id of ['R-1', 'R-2']) {
      const box = byTestId(`project-run-pick-${id}`)
      box.checked = true
      box.dispatchEvent(new Event('change'))
      await flushPromises()
    }
    byTestId('project-runs-approve').click()
    await flushPromises()
    expect(projects.decideRuns).toHaveBeenCalledWith('prj_1', 'approve', ['R-1', 'R-2'], null)
    expect(byTestId('project-run-outcome-R-1').textContent).toContain('T-001: Started')
    expect(byTestId('project-run-outcome-R-2').textContent).toContain('T-002: Already decided by someone else')
    expect(byTestId('project-runs-waiting-empty').textContent).toContain('Nothing is waiting for your go')
  })

  it('declines with the note', async () => {
    await waiting([RUN('R-1', 'T-001')])
    const box = byTestId('project-run-pick-R-1')
    box.checked = true
    box.dispatchEvent(new Event('change'))
    const note = document.getElementById('runs-decline-note')
    note.value = 'Not this week'
    note.dispatchEvent(new Event('input'))
    await flushPromises()
    byTestId('project-runs-decline').click()
    await flushPromises()
    expect(projects.decideRuns).toHaveBeenCalledWith('prj_1', 'decline', ['R-1'], 'Not this week')
  })

  it('a reader without contribute rights sees the runs but no checkbox and no verb', async () => {
    await waiting([RUN('R-1', 'T-001')], { canContribute: false })
    expect(byTestId('project-run-waiting-R-1')).not.toBeNull()
    expect(byTestId('project-run-pick-R-1')).toBeNull()
    expect(byTestId('project-runs-approve')).toBeNull()
  })
})

// --- a task -----------------------------------------------------------------------------

const TASK = (over = {}) => ({
  id: 'T-001', title: 'Draft the brief', status: 'active', priority: 'p2', owner: null, agent: null,
  objective: '', done_definition: '', updated_at: '2026-11-12T08:00:00Z', log: [], parent: null,
  for_person: false, subtasks: { ids: [], total: 0, done: 0 }, blocked_by: [], waiting_for: [],
  creator: { kind: 'person', name: ME }, needs_go: false, needs_decision: false, rejections: 0, ...over,
})

async function openTask(task, props = {}) {
  projects.fetchTasks = vi.fn()
  projects.tasks = { prj_1: { rows: [task], loaded: true, error: null } }
  projects.fetchTask = vi.fn().mockResolvedValue(task)
  projects.updateTask = vi.fn().mockResolvedValue({})
  projects.startPreview = vi.fn().mockResolvedValue({
    task_id: task.id, agent: 'scout', agents: { available: ['scout'], not_on_your_list: [] }, message: 'go',
    message_sha: 's', context: [], read_budget_bytes: 204800, log_excerpt: [], chats: [], cost_note: '',
  })
  projects.fetchProjectPeople = vi.fn().mockResolvedValue([])
  mount(ProjectTasks, {
    props: { projectId: 'prj_1', canContribute: true, myEmail: ME, project: project(), ...props },
    attachTo: document.body,
  })
  await flushPromises()
  byTestId(`project-task-${task.id}`).click()
  await flushPromises()
}

describe('ent#1235 — a task', () => {
  it('"Needs my go" sends needs_go, and the row says a held task needs a go', async () => {
    await openTask(TASK())
    document.querySelector('[data-testid="project-task-needs-go"] [role="switch"]').click()
    await flushPromises()
    expect(projects.updateTask).toHaveBeenCalledWith('prj_1', 'T-001', { needs_go: true })

    document.body.innerHTML = ''
    await openTask(TASK({ needs_go: true }))
    expect(byTestId('project-task-needs-go-T-001').textContent).toContain('Needs a go')
  })

  it('a task rejected twice says it needs a decision, and why', async () => {
    await openTask(TASK({
      status: 'needs-decision', needs_decision: true, rejections: 2,
      log: [{ id: 1, kind: 'note', author: 'agent:steward', created_at: '2026-11-12T08:00:00Z', body: 'No tests. (rejected twice — needs a decision)' }],
    }))
    expect(byTestId('project-task-needs-decision').textContent).toContain('The last reason: No tests.')
  })

  it('a live run is named on the row and in the detail', async () => {
    await openTask(TASK({ run: { id: 'R-9', agent_name: 'scout', state: 'started', started_at: '2026-11-12T08:00:00Z' } }))
    expect(byTestId('project-task-run-T-001').textContent).toContain('Running')
    expect(byTestId('project-task-live-run').textContent).toContain('Run R-9 on scout: Running')
  })

  it('on a stopped project Start is replaced by the reason', async () => {
    await openTask(TASK(), { project: project({ status: 'paused', state: 'stopped' }) })
    expect(byTestId('project-task-start-not-running').textContent).toContain('This project is stopped')
    expect(byTestId('project-task-start-go')).toBeNull()
  })
})

describe('ent#1235 — a refused Start names the refusal', () => {
  it('a run already under way', async () => {
    projects.startPreview = vi.fn().mockResolvedValue({
      task_id: 'T-001', agent: 'scout', agents: { available: ['scout'], not_on_your_list: [] }, message: 'go',
      message_sha: 's', context: [], read_budget_bytes: 204800, log_excerpt: [], chats: [], cost_note: '',
    })
    projects.startTask = vi.fn().mockRejectedValue({
      response: { status: 409, data: { detail: { code: 'task_run_live', message: 'server words' } } },
    })
    const w = mount(ProjectTaskStart, { props: { projectId: 'prj_1', taskId: 'T-001', canContribute: true, projectState: 'running' } })
    await flushPromises()
    await w.find('[data-testid="project-task-start-go"]').trigger('click')
    await flushPromises()
    expect(w.text()).toContain('A run is already under way on this task.')
  })
})
