// @vitest-environment jsdom
/**
 * ent#1133 — a person edits a task's title, objective and definition of done.
 *
 * Every rule here either enables a store write or decides what reaches the
 * server, so each is proven on the rendered component (design-system contract,
 * #2918) rather than over the SFC's source:
 *   - the three fields mount prefilled, and only the changed ones are sent;
 *   - clearing a long field sends `null`, which is what clears it server-side;
 *   - a bad length is refused inline with nothing sent — the same limit the
 *     create dialog applies, and the same limit `work.py` applies;
 *   - a legacy over-limit stored value does not block an unrelated save;
 *   - a done task shows the fields disabled with the reopen reason, and
 *     Reopen sends the status alone (the server 409s a combined save);
 *   - a server 422 lands under the field it names, keeping what was typed;
 *   - someone who may not contribute is offered no fields at all.
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
import { TASK_TITLE_MAX, TASK_TEXT_MAX } from '@/components/portal/projects/projectsUtils'

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

const OPEN = {
  id: 'T-001', title: 'Draft', status: 'active', priority: 'p2', owner: null, agent: null,
  objective: 'Why this matters', done_definition: 'Two sign-offs.',
  updated_at: '2026-09-30T10:00:00Z', log: [],
}

async function openDetail(task, { after, props } = {}) {
  projects.fetchTasks = vi.fn()
  projects.tasks = { prj_1: { rows: [task], loaded: true, error: null } }
  projects.fetchTask = vi.fn().mockResolvedValueOnce(task).mockResolvedValueOnce(after || task)
  projects.updateTask = vi.fn().mockResolvedValue({})
  projects.fetchProjectPeople = vi.fn().mockResolvedValue([])
  mount(ProjectTasks, { props: { projectId: 'prj_1', canContribute: true, ...props }, attachTo: document.body })
  await flushPromises()
  byTestId(`project-task-${task.id}`).click()
  await flushPromises()
}

async function type(testid, value) {
  const el = byTestId(testid)
  el.value = value
  el.dispatchEvent(new Event('input'))
  await flushPromises()
}

async function save() {
  byTestId('project-task-save').click()
  await flushPromises()
}

describe('ent#1133 — editing a task\'s title, objective and definition of done', () => {
  it('E1: the three fields mount prefilled from the task', async () => {
    await openDetail(OPEN)
    expect(byTestId('project-task-title-edit').value).toBe('Draft')
    expect(byTestId('project-task-objective-edit').value).toBe('Why this matters')
    expect(byTestId('project-task-dod-edit').value).toBe('Two sign-offs.')
    expect(byTestId('project-task-save').disabled).toBe(true)
  })

  it('E2: editing only the title sends exactly the title', async () => {
    await openDetail(OPEN, { after: { ...OPEN, title: 'Draft the launch brief' } })
    await type('project-task-title-edit', 'Draft the launch brief')
    expect(byTestId('project-task-save').disabled).toBe(false)
    await save()
    expect(projects.updateTask).toHaveBeenCalledWith('prj_1', 'T-001', { title: 'Draft the launch brief' })
    expect(byTestId('project-task-title-edit').value).toBe('Draft the launch brief')
    expect(byTestId('project-task-save').disabled).toBe(true)
  })

  it('E3: clearing the objective sends null — what clears it server-side', async () => {
    await openDetail(OPEN, { after: { ...OPEN, objective: null } })
    await type('project-task-objective-edit', '   ')
    expect(byTestId('project-task-save').disabled).toBe(false)
    await save()
    expect(projects.updateTask).toHaveBeenCalledWith('prj_1', 'T-001', { objective: null })
  })

  it('E4: a blank title is refused inline and nothing is sent', async () => {
    await openDetail(OPEN)
    await type('project-task-title-edit', '   ')
    await save()
    expect(projects.updateTask).not.toHaveBeenCalled()
    expect(errorOf('task-title-edit')).toContain('Give the task a title')
  })

  it('E5: an over-limit title or definition of done is refused inline, naming the count', async () => {
    await openDetail(OPEN)
    await type('project-task-title-edit', 'x'.repeat(TASK_TITLE_MAX + 1))
    await save()
    expect(projects.updateTask).not.toHaveBeenCalled()
    expect(errorOf('task-title-edit')).toBe(`Keep the title to ${TASK_TITLE_MAX} characters (it's ${TASK_TITLE_MAX + 1} now).`)

    await type('project-task-title-edit', 'Draft')
    await type('project-task-dod-edit', 'y'.repeat(TASK_TEXT_MAX + 1))
    await save()
    expect(projects.updateTask).not.toHaveBeenCalled()
    expect(errorOf('task-dod-edit')).toContain('definition of done')
  })

  it('E6: a title of 150 emoji is accepted — the limit counts code points, as Python does', async () => {
    await openDetail(OPEN, { after: OPEN })
    await type('project-task-title-edit', '🙂'.repeat(150))
    await save()
    expect(projects.updateTask).toHaveBeenCalledWith('prj_1', 'T-001', { title: '🙂'.repeat(150) })
  })

  it('E7: a legacy over-limit stored value does not block an unrelated save', async () => {
    const legacy = { ...OPEN, done_definition: 'z'.repeat(TASK_TEXT_MAX + 50) }
    await openDetail(legacy, { after: { ...legacy, status: 'blocked' } })
    const select = document.getElementById('task-status')
    select.value = 'blocked'
    select.dispatchEvent(new Event('change'))
    await flushPromises()
    await save()
    expect(projects.updateTask).toHaveBeenCalledWith('prj_1', 'T-001', { status: 'blocked' })
    expect(errorOf('task-dod-edit')).toBe('')
  })

  it('E8: the read-only blocks are hidden while the fields are offered, and shown when they are not', async () => {
    await openDetail(OPEN)
    expect(byTestId('project-task-objective')).toBeNull()

    document.body.innerHTML = ''
    await openDetail(OPEN, { props: { canContribute: false } })
    expect(byTestId('project-task-objective')).not.toBeNull()
    expect(byTestId('project-task-title-edit')).toBeNull()
  })

  it('E9: a done task shows the fields disabled with the reopen reason, and Reopen sends the status alone', async () => {
    const done = { ...OPEN, status: 'done' }
    await openDetail(done, { after: { ...done, status: 'active' } })
    for (const [testid, field] of [['project-task-title-edit', 'title'],
      ['project-task-objective-edit', 'objective'],
      ['project-task-dod-edit', 'definition of done']]) {
      expect(byTestId(testid).disabled).toBe(true)
      const help = document.getElementById(`${byTestId(testid).id}-help`)
      expect(help.textContent).toContain(`Reopen the task to change its ${field}.`)
    }
    expect(byTestId('project-task-objective')).not.toBeNull()   // still readable while locked

    const select = document.getElementById('task-status')
    select.value = 'active'
    select.dispatchEvent(new Event('change'))
    await flushPromises()
    await save()
    expect(projects.updateTask).toHaveBeenCalledWith('prj_1', 'T-001', { status: 'active' })
  })

  it('E10: a server 422 lands under the field it names, keeping what was typed', async () => {
    await openDetail(OPEN)
    projects.updateTask = vi.fn().mockRejectedValue({
      response: { status: 422, data: { detail: { code: 'invalid_done_definition',
        message: 'The definition of done can be at most 4000 characters.' } } },
    })
    await type('project-task-dod-edit', 'Rewritten.')
    await save()
    expect(errorOf('task-dod-edit')).toContain('definition of done')
    expect(byTestId('project-task-dod-edit').value).toBe('Rewritten.')
  })

  it('E11: the create dialog refuses the same lengths, with nothing sent', async () => {
    projects.fetchTasks = vi.fn()
    projects.tasks = { prj_1: { rows: [], loaded: true, error: null } }
    projects.createTask = vi.fn().mockResolvedValue({})
    projects.fetchProjectPeople = vi.fn().mockResolvedValue([])
    mount(ProjectTasks, { props: { projectId: 'prj_1', canContribute: true }, attachTo: document.body })
    await flushPromises()
    byTestId('project-task-new').click()
    await flushPromises()
    const title = document.getElementById('task-title')
    title.value = 'x'.repeat(TASK_TITLE_MAX + 1)
    title.dispatchEvent(new Event('input'))
    const dod = document.getElementById('task-dod')
    dod.value = 'y'.repeat(TASK_TEXT_MAX + 1)
    dod.dispatchEvent(new Event('input'))
    await flushPromises()
    byTestId('project-task-create').click()
    await flushPromises()
    expect(projects.createTask).not.toHaveBeenCalled()
    expect(errorOf('task-title')).toContain(`${TASK_TITLE_MAX} characters`)
    expect(errorOf('task-dod')).toContain('definition of done')
  })
})
