// @vitest-environment jsdom
/**
 * ent#1232 — a task's shape on the project page: `todo`, subtasks, blocked-by,
 * tasks for people, and who made a task and who it is for.
 *
 * The rules are the server's; the page decides what to OFFER and what to send,
 * so each of those is proven on the rendered component (#2918):
 *   - `todo` is a status with its own words, and the default the list shows;
 *   - a subtask sits under its parent, and a parent shows its done/total;
 *   - waiting on another task reads as "Waiting on", never as the Blocked status;
 *   - the row and the detail name who created the task and who it is for;
 *   - a task for a person offers no Start;
 *   - the edit form sends `parent` (null clears it) and the blocked-by change
 *     as add/remove; a bad id is refused with nothing sent; the server's
 *     circle refusal shows its path under the field.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
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
  return {
    default: Object.assign(
      { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), create: mk },
      { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
        defaults: { headers: { common: {} } } },
    ),
  }
})

import { useProjectsStore } from '@/stores/projects'
import ProjectTasks from '@/components/portal/projects/ProjectTasks.vue'
import {
  TASK_STATUSES, assignedLabel, creatorLabel, groupTasks, parseTaskIds, subtaskLabel, taskStatusLabel,
  waitingLabel,
} from '@/components/portal/projects/projectsUtils'

globalThis.ResizeObserver = globalThis.ResizeObserver || class {
  observe() {}
  unobserve() {}
  disconnect() {}
}

const byTestId = (id) => document.querySelector(`[data-testid="${id}"]`)
const errorOf = (id) => document.getElementById(`${id}-error`)?.textContent.trim() || ''
let projects

beforeEach(() => {
  document.body.innerHTML = ''
  localStorage.clear()
  setActivePinia(createPinia())
  vi.clearAllMocks()
  projects = useProjectsStore()
})

const base = (over) => ({
  title: 'x', status: 'todo', priority: 'p2', owner: null, agent: null, objective: '', done_definition: '',
  updated_at: '2026-11-10T10:00:00Z', log: [], parent: null, for_person: false,
  subtasks: { ids: [], total: 0, done: 0 }, blocked_by: [], waiting_for: [], unblocked: true,
  creator: { kind: 'person', name: 'me@example.com' }, created_by: 'me@example.com', ...over,
})

const PARENT = base({ id: 'T-001', title: 'Launch', status: 'active', subtasks: { ids: ['T-003'], total: 1, done: 0 } })
const OTHER = base({ id: 'T-002', title: 'Pricing page' })
const CHILD = base({
  id: 'T-003', title: 'Sign the contract', parent: 'T-001', for_person: true, owner: 'alex@example.com',
  blocked_by: ['T-002'], waiting_for: ['T-002'], unblocked: false,
  creator: { kind: 'agent', name: 'scout' }, created_by: 'agent:scout',
})
const MEMBERS = [{ email: 'alex@example.com', label: 'Alex' }]

function mountTasks(rows, { detail, after, props } = {}) {
  projects.fetchTasks = vi.fn()
  projects.tasks = { prj_1: { rows, loaded: true, error: null } }
  projects.fetchTask = vi.fn().mockResolvedValueOnce(detail).mockResolvedValue(after || detail)
  projects.updateTask = vi.fn().mockResolvedValue({})
  projects.createTask = vi.fn().mockResolvedValue({})
  projects.fetchProjectPeople = vi.fn().mockResolvedValue([])
  return mount(ProjectTasks, {
    props: { projectId: 'prj_1', canContribute: true, myEmail: 'me@example.com', members: MEMBERS, ...props },
    attachTo: document.body,
  })
}

async function openDetail(task, opts = {}) {
  mountTasks([task], { detail: task, ...opts })
  await flushPromises()
  byTestId(`project-task-${task.id}`).click()
  await flushPromises()
}

async function type(testid, value) {
  const el = byTestId(testid) || document.getElementById(testid)
  el.value = value
  el.dispatchEvent(new Event('input'))
  await flushPromises()
}

async function save() {
  byTestId('project-task-save').click()
  await flushPromises()
}

describe('ent#1232 — the rules in words', () => {
  it('todo is the first status and reads "To do"', () => {
    expect(TASK_STATUSES[0]).toBe('todo')
    expect(taskStatusLabel('todo')).toBe('To do')
  })

  it('a subtask follows its parent in the same group', () => {
    const g = groupTasks([CHILD, OTHER, PARENT])
    expect(g.open.map((t) => t.id)).toEqual(['T-001', 'T-003', 'T-002'])
  })

  it('names the creator, the assignee, the progress and the wait', () => {
    expect(creatorLabel({ kind: 'agent', name: 'scout' }, 'me@example.com')).toBe('scout (agent)')
    expect(creatorLabel({ kind: 'person', name: 'me@example.com' }, 'me@example.com')).toBe('You')
    expect(creatorLabel({ kind: 'person', name: 'alex@example.com' }, '', () => 'Alex')).toBe('Alex')
    expect(creatorLabel(null, '')).toBe('')
    expect(assignedLabel({ owner: 'alex@example.com', agent: 'scout' }, () => 'Alex')).toBe('Alex · scout (agent)')
    expect(assignedLabel({ owner: null, agent: null })).toBe('')
    expect(subtaskLabel(PARENT)).toBe('0/1 subtasks')
    expect(subtaskLabel(OTHER)).toBe('')
    expect(waitingLabel(CHILD)).toBe('Waiting on T-002')
    expect(waitingLabel(OTHER)).toBe('')
  })

  it('reads task ids typed with commas or spaces, and names a bad one', () => {
    expect(parseTaskIds('t-2, T-004  T-010')).toEqual({ ids: ['T-002', 'T-004', 'T-010'], bad: [] })
    expect(parseTaskIds('T-2 nope')).toEqual({ ids: ['T-002'], bad: ['nope'] })
    expect(parseTaskIds('')).toEqual({ ids: [], bad: [] })
  })
})

describe('ent#1232 — the list', () => {
  it('nests the subtask, shows progress, the wait, the creator and who it is for', async () => {
    mountTasks([PARENT, OTHER, CHILD])
    await flushPromises()
    const ids = [...document.querySelectorAll('[data-testid^="project-task-T-"]')].map((b) => b.dataset.testid)
    expect(ids).toEqual(['project-task-T-001', 'project-task-T-003', 'project-task-T-002'])
    expect(byTestId('project-task-T-001').textContent).toContain('0/1 subtasks')
    const child = byTestId('project-task-T-003').textContent
    expect(child).toContain('Waiting on T-002')
    expect(child).not.toContain('Blocked')
    expect(child).toContain('Alex')
    expect(child).toContain('by scout (agent)')
    expect(byTestId('project-task-T-002').textContent).toContain('To do')
  })
})

describe('ent#1232 — one task', () => {
  it('a task for a person names its creator and offers no Start', async () => {
    await openDetail(CHILD)
    const detail = byTestId('project-task-detail').textContent
    expect(detail).toContain('scout (agent)')
    expect(detail).toContain('T-001')
    expect(byTestId('project-task-for-person').textContent).toMatch(/for a person/)
    expect(byTestId('project-task-start')).toBeNull()
    expect(byTestId('project-task-waiting').textContent).toContain('T-002')
  })

  it('an agent task still offers Start', async () => {
    await openDetail(OTHER)
    expect(byTestId('project-task-for-person')).toBeNull()
    expect(document.querySelector('[data-testid^="project-task-start"]')).not.toBeNull()
  })

  it('clearing the parent sends null; the blocked-by change goes as add and remove', async () => {
    await openDetail(CHILD)
    await type('project-task-parent-edit', '')
    await type('project-task-blocked-by-edit', 'T-004')
    await save()
    expect(projects.updateTask).toHaveBeenCalledWith('prj_1', 'T-003', {
      parent: null, blocked_by_add: ['T-004'], blocked_by_remove: ['T-002'],
    })
  })

  it('a bad id is refused under the field with nothing sent', async () => {
    await openDetail(OTHER)
    await type('project-task-blocked-by-edit', 'T-001, soon')
    await save()
    expect(projects.updateTask).not.toHaveBeenCalled()
    expect(errorOf('task-blocked-by-edit')).toContain('soon')
  })

  it("the server's circle refusal shows its path under the blocked-by field", async () => {
    await openDetail(OTHER)
    projects.updateTask = vi.fn().mockRejectedValue({ response: { status: 422, data: { detail: {
      code: 'blocker_cycle', message: 'That link would make a circle: T-002 → T-005 → T-002.' } } } })
    await type('project-task-blocked-by-edit', 'T-005')
    await save()
    expect(errorOf('task-blocked-by-edit')).toContain('T-002 → T-005 → T-002')
    expect(byTestId('project-task-blocked-by-edit').value).toBe('T-005')
  })
})

describe('ent#1232 — a new task', () => {
  it('sends parent, blocked_by and for_person when given', async () => {
    mountTasks([OTHER])
    await flushPromises()
    byTestId('project-task-new').click()
    await flushPromises()
    await type('task-title', 'Sign it')
    await type('project-task-parent-new', 't-1')
    await type('project-task-blocked-by-new', 'T-002')
    byTestId('project-task-for-person-new').click()
    await flushPromises()
    byTestId('project-task-create').click()
    await flushPromises()
    expect(projects.createTask).toHaveBeenCalledWith('prj_1', {
      title: 'Sign it', priority: 'p2', parent: 'T-001', blocked_by: ['T-002'], for_person: true,
    })
  })
})
