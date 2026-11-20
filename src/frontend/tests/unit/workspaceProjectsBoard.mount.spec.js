// @vitest-environment jsdom
/**
 * The Tasks tab's board, mounted (trinity-enterprise#957).
 *
 * What a mount proves here: which column a card is drawn in, what one gesture
 * sends, that a card in flight says so and a refused move returns and names
 * the reason, who may move at all, and that the re-read is invisible.
 *
 * What it cannot: jsdom has no layout and starts no real drag. Drag events are
 * dispatched by hand, so the HANDLERS are proven, not the browser's gesture —
 * that is checked in a real browser, by hand (see the PR).
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: true, authHeader: {}, logout: vi.fn() }),
}))
vi.mock('axios', () => {
  const mk = () => ({
    get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  })
  return { default: Object.assign(mk(), { create: mk }) }
})

import { useProjectsStore } from '@/stores/projects'
import ProjectTasks from '@/components/portal/projects/ProjectTasks.vue'
import { tasksViewKey, boardMinWidth } from '@/components/portal/projects/projectsUtils'

const ME = 'eva@example.com'
const byTestId = (id) => document.querySelector(`[data-testid="${id}"]`)
const within = (col, id) => byTestId(`board-column-${col}`).querySelector(`[data-testid="board-card-${id}"]`)

let projects
let observers
let wrapper

const ROWS = () => [
  { id: 'T-001', title: 'Draft the brief', status: 'active', priority: 'p1', owner: ME, agent: null, created_by: ME, updated_at: '2026-11-10T10:00:00Z' },
  { id: 'T-002', title: 'Legal review', status: 'blocked', priority: 'p2', owner: null, agent: 'scout', waiting_on: 'counsel', created_by: 'agent:corbin', updated_at: '2026-11-10T10:00:00Z' },
  { id: 'T-003', title: 'Announce', status: 'pending-verification', priority: 'p2', owner: null, agent: 'scout', created_by: ME, pending_since: '2026-11-10T10:00:00Z', updated_at: '2026-11-10T10:00:00Z' },
  { id: 'T-004', title: 'Cut the tag', status: 'done', priority: 'p2', owner: ME, agent: null, created_by: ME, updated_at: '2026-11-09T10:00:00Z' },
]

function seed(rows = ROWS()) {
  projects.tasks = { prj_1: { rows, loaded: true, error: null } }
}

async function tasksTab(props = {}, { view = 'board' } = {}) {
  if (view) localStorage.setItem(tasksViewKey(ME), view)
  wrapper = mount(ProjectTasks, {
    props: { projectId: 'prj_1', project: { id: 'prj_1', steward: { kind: 'person', ref: ME } }, canContribute: true, myEmail: ME, members: [{ email: ME, label: 'Eva' }], ...props },
    attachTo: document.body,
  })
  await flushPromises()
  return wrapper
}

const fire = (el, type, init = {}) => {
  const e = new Event(type, { bubbles: true, cancelable: true })
  Object.assign(e, { dataTransfer: { setData: vi.fn(), getData: () => 'T-999', effectAllowed: '', dropEffect: '' } }, init)
  el.dispatchEvent(e)
  return e
}
async function drag(id, toColumn) {
  fire(byTestId(`board-card-${id}`), 'dragstart')
  await flushPromises()
  const col = byTestId(`board-column-${toColumn}`)
  fire(col, 'dragenter')
  fire(col, 'dragover')
  fire(col, 'drop')
  await flushPromises()
}
const key = async (el, k) => {
  const e = new KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true })
  el.dispatchEvent(e)
  await flushPromises()
  return e
}
function deferred() {
  let resolve, reject
  const promise = new Promise((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

beforeEach(() => {
  document.body.innerHTML = ''
  localStorage.clear()
  setActivePinia(createPinia())
  vi.clearAllMocks()
  observers = []
  globalThis.ResizeObserver = class {
    constructor(cb) { this.cb = cb; observers.push(this) }
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  projects = useProjectsStore()
  projects.fetchTasks = vi.fn()
  projects.fetchLog = vi.fn()
  projects.fetchTask = vi.fn(async (_p, id) => ({ ...ROWS().find((r) => r.id === id), log: [], context: [] }))
  projects.moveTask = vi.fn().mockResolvedValue({})
  projects.updateTask = vi.fn().mockResolvedValue({})
  seed()
})

afterEach(() => {
  wrapper?.unmount()
  vi.useRealTimers()
})

describe('the view', () => {
  it('a first-time visitor gets the list; Board is one click and is remembered', async () => {
    await tasksTab({}, { view: null })
    expect(byTestId('project-board')).toBeNull()
    expect(byTestId('project-task-T-001')).not.toBeNull()
    byTestId('project-tasks-view').querySelector('[data-segment="board"]').click()
    await flushPromises()
    expect(byTestId('project-board')).not.toBeNull()
    expect(byTestId('project-task-T-001')).toBeNull()
    expect(localStorage.getItem(tasksViewKey(ME))).toBe('board')
  })

  it('too narrow for the columns: the list renders, the choice is kept, and it comes back', async () => {
    await tasksTab()
    expect(byTestId('project-board')).not.toBeNull()
    observers[0].cb([{ contentRect: { width: boardMinWidth() - 1 } }])
    await flushPromises()
    expect(byTestId('project-board')).toBeNull()
    expect(byTestId('project-task-T-001')).not.toBeNull()
    expect(byTestId('project-tasks-view').className).toContain('invisible')
    expect(localStorage.getItem(tasksViewKey(ME))).toBe('board')
    observers[0].cb([{ contentRect: { width: boardMinWidth() } }])
    await flushPromises()
    expect(byTestId('project-board')).not.toBeNull()
  })
})

describe('columns and cards', () => {
  it('one column per step, each with its count; a held task stays in Active with a badge', async () => {
    await tasksTab()
    expect(byTestId('board-count-active').textContent).toBe('2')
    expect(byTestId('board-count-pending-verification').textContent).toBe('1')
    expect(byTestId('board-count-done').textContent).toBe('1')
    expect(byTestId('board-held-active').textContent).toContain('1 blocked')
    expect(within('active', 'T-002')).not.toBeNull()
    expect(byTestId('board-flag-T-002').textContent).toBe('Blocked')
    expect(byTestId('board-flag-T-001')).toBeNull()
    const card = byTestId('board-card-T-002').textContent
    expect(card).toContain('Waiting on: counsel')
    expect(card).toContain('scout')
    expect(card).toContain('Created by corbin (agent)')
    expect(byTestId('board-card-T-001').textContent).toContain('Eva')
    expect(byTestId('board-hint-pending-verification').textContent).toContain('confirm')
  })

  it('an empty project is a board with a next action, never a blank', async () => {
    seed([])
    await tasksTab()
    for (const k of ['active', 'pending-verification', 'done']) expect(byTestId(`board-empty-${k}`)).not.toBeNull()
    byTestId('board-new-task').click()
    await flushPromises()
    expect(document.getElementById('task-create-title')).not.toBeNull()
  })

  it('a status the board does not know is still on it', async () => {
    seed([{ id: 'T-9', title: 'New kind', status: 'triage', priority: 'p2' }])
    await tasksTab()
    expect(within('active', 'T-9')).not.toBeNull()
    expect(byTestId('board-flag-T-9').textContent).toBe('triage')
  })

  it('ent#1232: a todo task, one waiting on another, and a done without a claim each say so', async () => {
    seed([
      { id: 'T-1', title: 'Draft', status: 'todo', priority: 'p2', waiting_for: ['T-2'] },
      { id: 'T-2', title: 'Brief', status: 'done', priority: 'p2', agent: 'scout', closed_without_claim: true, updated_at: '2026-11-10T00:00:00Z' },
    ])
    await tasksTab()
    expect(within('active', 'T-1')).not.toBeNull()
    expect(byTestId('board-flag-T-1').textContent).toBe('To do')
    expect(byTestId('board-shape-T-1').textContent).toContain('Waiting on T-2')
    expect(within('done', 'T-2')).not.toBeNull()
    expect(byTestId('board-unclaimed-T-2').textContent).toBe('Done without a done claim')
    expect(byTestId('board-unclaimed-T-1')).toBeNull()
  })

  it('a long Done column shows the newest and says how many there are', async () => {
    seed(Array.from({ length: 23 }, (_, i) => ({ id: `T-${i + 1}`, title: `Done ${i}`, status: 'done', priority: 'p2', updated_at: `2026-11-${String(i + 1).padStart(2, '0')}T00:00:00Z` })))
    await tasksTab()
    expect(byTestId('board-column-done').querySelectorAll('[data-testid^="board-card-"]').length).toBe(20)
    expect(byTestId('board-count-done').textContent).toBe('23')
    byTestId('board-show-all-done').click()
    await flushPromises()
    expect(byTestId('board-column-done').querySelectorAll('[data-testid^="board-card-"]').length).toBe(23)
  })

  it('opening a card opens the same task dialog the list opens; Confirm does not', async () => {
    await tasksTab()
    byTestId('board-confirm-T-003').click()
    await flushPromises()
    expect(projects.fetchTask).not.toHaveBeenCalled()
    expect(projects.moveTask).toHaveBeenCalledWith('prj_1', 'T-003', 'done')
    byTestId('board-open-T-001').click()
    await flushPromises()
    expect(projects.fetchTask).toHaveBeenCalledWith('prj_1', 'T-001')
    expect(byTestId('project-task-detail')).not.toBeNull()
  })
})

describe('a move', () => {
  it('a drop sends the status and nothing else; the card is drawn where it is going, saving, until the server answers', async () => {
    const d = deferred()
    projects.moveTask = vi.fn(() => d.promise)
    await tasksTab()
    await drag('T-001', 'pending-verification')
    expect(projects.moveTask).toHaveBeenCalledTimes(1)
    expect(projects.moveTask).toHaveBeenCalledWith('prj_1', 'T-001', 'pending-verification')
    expect(within('pending-verification', 'T-001')).not.toBeNull()
    expect(byTestId('board-saving-T-001')).not.toBeNull()
    expect(byTestId('board-card-T-001').getAttribute('draggable')).toBe('false')
    // The server's answer is the row; the card settles there.
    projects.tasks = { prj_1: { rows: ROWS().map((r) => (r.id === 'T-001' ? { ...r, status: 'pending-verification' } : r)), loaded: true, error: null } }
    d.resolve({})
    await flushPromises()
    expect(byTestId('board-saving-T-001')).toBeNull()
    expect(within('pending-verification', 'T-001')).not.toBeNull()
    expect(byTestId('project-board-status').textContent).toContain('T-001 · Active → Awaiting verification')
    expect(byTestId('project-board-evidence')).not.toBeNull()
  })

  it('a refused move puts the card back and says why, until dismissed', async () => {
    projects.moveTask = vi.fn().mockRejectedValue({ response: { status: 403, data: { detail: { code: 'x', message: 'Only a member can change a task.' } } } })
    await tasksTab()
    await drag('T-001', 'pending-verification')
    expect(within('active', 'T-001')).not.toBeNull()
    expect(byTestId('board-saving-T-001')).toBeNull()
    const err = byTestId('project-task-move-error')
    expect(err.textContent).toContain('T-001 was not changed')
    expect(err.textContent).toContain('Only a member can change a task.')
  })

  it('a done task only reopens: any other column sends nothing and names the rule', async () => {
    await tasksTab()
    await drag('T-004', 'pending-verification')
    expect(projects.moveTask).not.toHaveBeenCalled()
    expect(byTestId('project-task-move-error').textContent).toContain('A done task can only be reopened')
    await drag('T-004', 'active')
    expect(projects.moveTask).toHaveBeenCalledWith('prj_1', 'T-004', 'active')
  })

  it('its own column is nothing: a blocked card dropped on Active stays blocked', async () => {
    await tasksTab()
    await drag('T-002', 'active')
    expect(projects.moveTask).not.toHaveBeenCalled()
    expect(byTestId('project-task-move-error')).toBeNull()
  })

  it('Done straight from Active asks first and needs the reason; cancelling sends nothing', async () => {
    await tasksTab()
    await drag('T-002', 'done')
    expect(projects.moveTask).not.toHaveBeenCalled()
    const ask = byTestId('project-task-done-ask')
    expect(ask.textContent).toContain('Mark T-002 done?')
    expect(ask.textContent).toContain('assigned to the agent scout')
    byTestId('project-task-done-cancel').click()
    await flushPromises()
    expect(byTestId('project-task-done-ask')).toBeNull()
    expect(projects.moveTask).not.toHaveBeenCalled()

    await drag('T-002', 'done')
    // No reason, no move: the form says what to write.
    byTestId('project-task-done-ask').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    await flushPromises()
    expect(projects.moveTask).not.toHaveBeenCalled()
    expect(byTestId('project-task-done-ask').textContent).toContain('Say why it is done')
    const note = byTestId('project-task-done-note')
    note.value = '  Counsel signed off by email  '
    note.dispatchEvent(new Event('input'))
    byTestId('project-task-done-ask').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    await flushPromises()
    expect(projects.moveTask).toHaveBeenCalledTimes(1)
    expect(projects.moveTask).toHaveBeenCalledWith('prj_1', 'T-002', 'done', 'Counsel signed off by email')
    expect(byTestId('project-task-done-ask')).toBeNull()
  })

  it('the reason is asked fresh each time', async () => {
    await tasksTab()
    await drag('T-001', 'done')
    const note = byTestId('project-task-done-note')
    note.value = 'left over'
    note.dispatchEvent(new Event('input'))
    byTestId('project-task-done-cancel').click()
    await flushPromises()
    await drag('T-002', 'done')
    expect(byTestId('project-task-done-note').value).toBe('')
  })

  it('Done from Awaiting verification is the confirmation, at once', async () => {
    await tasksTab()
    await drag('T-003', 'done')
    expect(projects.moveTask).toHaveBeenCalledWith('prj_1', 'T-003', 'done')
  })

  it('a drop that did not start on this board moves nothing, whatever its payload says', async () => {
    await tasksTab()
    const col = byTestId('board-column-done')
    const over = fire(col, 'dragover')
    const drop = fire(col, 'drop')
    await flushPromises()
    expect(over.defaultPrevented).toBe(false)
    expect(drop.defaultPrevented).toBe(false)
    expect(projects.moveTask).not.toHaveBeenCalled()
  })

  it('the target column stays lit while the pointer crosses its children', async () => {
    await tasksTab()
    fire(byTestId('board-card-T-001'), 'dragstart')
    const col = byTestId('board-column-done')
    fire(col, 'dragenter')
    fire(byTestId('board-card-T-004'), 'dragenter')
    fire(col, 'dragleave')
    await flushPromises()
    expect(col.getAttribute('data-over')).toBe('true')
    fire(byTestId('board-card-T-004'), 'dragleave')
    await flushPromises()
    expect(col.getAttribute('data-over')).toBeNull()
  })

  it('a second change to a task already in flight is not sent', async () => {
    const d = deferred()
    projects.moveTask = vi.fn(() => d.promise)
    await tasksTab()
    byTestId('board-confirm-T-003').click()
    await flushPromises()
    expect(byTestId('board-saving-T-003')).not.toBeNull()
    await drag('T-003', 'active')
    expect(projects.moveTask).toHaveBeenCalledTimes(1)
    d.resolve({})
    await flushPromises()
  })

  it("the card's own menu holds a task and lets it run again", async () => {
    await tasksTab()
    expect(byTestId('board-actions-T-001')).toBeNull()
    byTestId('board-menu-T-001').click()
    await flushPromises()
    byTestId('board-set-T-001-blocked').click()
    await flushPromises()
    expect(projects.moveTask).toHaveBeenCalledWith('prj_1', 'T-001', 'blocked')
    byTestId('board-menu-T-002').click()
    await flushPromises()
    byTestId('board-set-T-002-active').click()
    await flushPromises()
    expect(projects.moveTask).toHaveBeenCalledWith('prj_1', 'T-002', 'active')
    expect(byTestId('board-menu-T-003')).toBeNull()
    expect(byTestId('board-menu-T-004')).toBeNull()
  })
})

describe('keyboard', () => {
  it('arrows pick, Enter moves and does not also open the task', async () => {
    await tasksTab()
    const title = byTestId('board-open-T-001')
    title.focus()
    await key(title, 'ArrowRight')
    expect(projects.moveTask).not.toHaveBeenCalled()
    expect(byTestId('project-board-armed').textContent).toContain('T-001: move to Awaiting verification')
    const enter = await key(title, 'Enter')
    expect(enter.defaultPrevented).toBe(true)
    expect(projects.moveTask).toHaveBeenCalledWith('prj_1', 'T-001', 'pending-verification')
    expect(projects.fetchTask).not.toHaveBeenCalled()
  })

  it('Escape cancels the pick; Enter with nothing picked opens the task', async () => {
    await tasksTab()
    const title = byTestId('board-open-T-001')
    title.focus()
    await key(title, 'ArrowRight')
    await key(title, 'Escape')
    expect(byTestId('project-board-armed').textContent).toBe('')
    await key(title, 'Enter')
    expect(projects.moveTask).not.toHaveBeenCalled()
    expect(projects.fetchTask).toHaveBeenCalledWith('prj_1', 'T-001')
  })

  it('back to its own column is no pick at all', async () => {
    await tasksTab()
    const title = byTestId('board-open-T-001')
    title.focus()
    await key(title, 'ArrowRight')
    await key(title, 'ArrowLeft')
    expect(byTestId('project-board-armed').textContent).toBe('')
    await key(title, 'ArrowLeft')
    expect(byTestId('project-board-armed').textContent).toBe('')
  })

  it('the task keeps focus when its card changes column', async () => {
    projects.moveTask = vi.fn(async () => {
      projects.tasks = { prj_1: { rows: ROWS().map((r) => (r.id === 'T-001' ? { ...r, status: 'pending-verification' } : r)), loaded: true, error: null } }
      return {}
    })
    await tasksTab()
    const title = byTestId('board-open-T-001')
    title.focus()
    await key(title, 'ArrowRight')
    await key(title, 'Enter')
    await flushPromises()
    expect(within('pending-verification', 'T-001')).not.toBeNull()
    expect(document.activeElement).toBe(byTestId('board-open-T-001'))
  })
})

describe('who may move', () => {
  it.each([
    ['a reader who is not a contributor', { canContribute: false }],
    ['anyone on an archived project', { archived: true }],
  ])('%s sees the board and can open a card, and nothing moves', async (_label, props) => {
    await tasksTab(props)
    expect(byTestId('board-card-T-001').getAttribute('draggable')).toBe('false')
    expect(byTestId('board-confirm-T-003')).toBeNull()
    expect(byTestId('board-menu-T-001')).toBeNull()
    expect(byTestId('project-task-new')).toBeNull()
    const start = fire(byTestId('board-card-T-001'), 'dragstart')
    expect(start.defaultPrevented).toBe(true)
    fire(byTestId('board-column-done'), 'drop')
    const title = byTestId('board-open-T-001')
    await key(title, 'ArrowRight')
    expect(byTestId('project-board-armed').textContent).toBe('')
    expect(projects.moveTask).not.toHaveBeenCalled()
    await key(title, 'Enter')
    expect(projects.moveTask).not.toHaveBeenCalled()
    expect(projects.fetchTask).toHaveBeenCalledWith('prj_1', 'T-001')
  })
})

describe('the re-read', () => {
  beforeEach(() => { vi.useFakeTimers() })

  it('re-reads while visible, swaps rows in place with no skeleton, and stops with the tab', async () => {
    await tasksTab()
    expect(projects.fetchTasks).toHaveBeenCalledTimes(1)
    projects.fetchTasks = vi.fn(async () => {
      projects.tasks = { prj_1: { rows: ROWS().map((r) => (r.id === 'T-002' ? { ...r, status: 'active' } : r)), loaded: true, error: null } }
    })
    const board = byTestId('project-board')
    await vi.advanceTimersByTimeAsync(20000)
    expect(projects.fetchTasks).toHaveBeenCalledTimes(1)
    expect(byTestId('project-board')).toBe(board)
    expect(byTestId('board-flag-T-002')).toBeNull()
    wrapper.unmount()
    wrapper = null
    await vi.advanceTimersByTimeAsync(60000)
    expect(projects.fetchTasks).toHaveBeenCalledTimes(1)
  })

  it('does not re-read while hidden, and does at once when the tab comes back', async () => {
    await tasksTab()
    projects.fetchTasks = vi.fn()
    const state = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
    await vi.advanceTimersByTimeAsync(60000)
    expect(projects.fetchTasks).not.toHaveBeenCalled()
    state.mockReturnValue('visible')
    document.dispatchEvent(new Event('visibilitychange'))
    window.dispatchEvent(new Event('focus'))
    expect(projects.fetchTasks).toHaveBeenCalledTimes(1)
    state.mockRestore()
  })

  it('holds fresh rows while a card is being dragged, and shows them when it ends', async () => {
    await tasksTab()
    projects.fetchTasks = vi.fn()
    fire(byTestId('board-card-T-001'), 'dragstart')
    await flushPromises()
    await vi.advanceTimersByTimeAsync(20000)
    expect(projects.fetchTasks).not.toHaveBeenCalled()
    // A read that was already in flight lands mid-drag.
    projects.tasks = { prj_1: { rows: ROWS().filter((r) => r.id !== 'T-002'), loaded: true, error: null } }
    await flushPromises()
    expect(byTestId('board-card-T-002')).not.toBeNull()
    fire(byTestId('board-card-T-001'), 'dragend')
    await flushPromises()
    expect(byTestId('board-card-T-002')).toBeNull()
  })

  it('one missed re-read keeps the rows and raises no banner; the second says so', async () => {
    await tasksTab()
    projects.fetchTasks = vi.fn(async () => {
      projects.tasks = { prj_1: { ...projects.tasks.prj_1, error: 'Could not reach the server.' } }
    })
    await vi.advanceTimersByTimeAsync(20000)
    expect(byTestId('inline-error')).toBeNull()
    expect(byTestId('board-card-T-001')).not.toBeNull()
    await vi.advanceTimersByTimeAsync(20000)
    expect(byTestId('inline-error').textContent).toContain('Could not reach the server.')
    expect(byTestId('board-card-T-001')).not.toBeNull()
  })

  it('a re-read under the open task dialog leaves what was typed', async () => {
    await tasksTab()
    byTestId('board-open-T-001').click()
    await flushPromises()
    const title = byTestId('project-task-title-edit')
    title.value = 'Draft the brief, v2'
    title.dispatchEvent(new Event('input'))
    projects.fetchTasks = vi.fn(async () => { projects.tasks = { prj_1: { rows: ROWS(), loaded: true, error: null } } })
    await vi.advanceTimersByTimeAsync(20000)
    expect(projects.fetchTasks).toHaveBeenCalled()
    expect(byTestId('project-task-title-edit').value).toBe('Draft the brief, v2')
  })
})
