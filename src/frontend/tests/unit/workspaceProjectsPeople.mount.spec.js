// @vitest-environment jsdom
/**
 * trinity-enterprise#1155 — a project's person fields pick a person.
 *
 * Members, the steward and a task owner were raw email boxes: a mistyped owner
 * was accepted and the task was owned by nobody; a mistyped member silently
 * failed to add the colleague. They are BasePeopleInput now, over the project's
 * scoped reads (`GET /projects/{id}/people?purpose=`, `GET /projects/people`):
 *   - member, steward, task owner: must-pick — nothing is written for text
 *     that is nobody; a guest: suggest-and-allow — an outside address submits;
 *   - each field asks with its own purpose;
 *   - a task owner may still be written `role:<id>`, and editing a task opens
 *     with its current owner as the chosen row and can clear it;
 *   - member and task rows read the name first.
 * Mounted; the modals teleport, so the DOM is read from `document`.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() }))
vi.mock('@/api', () => ({ default: api }))

import ProjectMembersModal from '@/components/portal/projects/ProjectMembersModal.vue'
import ProjectCreateModal from '@/components/portal/projects/ProjectCreateModal.vue'
import ProjectTasks from '@/components/portal/projects/ProjectTasks.vue'
import { useProjectsStore } from '@/stores/projects'

const BOB = { email: 'bob@example.com', label: 'Bob Baker' }
const CAROL = { email: 'carol@example.com', label: 'Carol Chen' }
const ME = 'alice@example.com'
const MEMBERS = [
  { email: ME, role: 'creator', label: 'Alice Archer' },
  { email: CAROL.email, role: 'member', label: CAROL.label },
]

let projects
const byTestId = (id) => document.querySelector(`[data-testid="${id}"]`)
const options = () => [...document.querySelectorAll('[role="option"]')]

async function typeInto(el, text) {
  el.value = text
  el.dispatchEvent(new Event('input'))
  await flushPromises()            // the field's watcher schedules the lookup
  vi.advanceTimersByTime(250)
  await flushPromises()
}

async function pick(el, text, label) {
  await typeInto(el, text)
  const row = options().find((o) => o.textContent.includes(label))
  if (!row) throw new Error(`no row ${label}: ${options().map((o) => o.textContent)}`)
  row.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }))
  await flushPromises()
}

const submitOf = (el) => el.closest('form').dispatchEvent(new Event('submit', { cancelable: true }))

beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn()
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  document.body.innerHTML = ''
  setActivePinia(createPinia())
  for (const fn of Object.values(api)) fn.mockReset()
  projects = useProjectsStore()
  projects.fetchProjectPeople = vi.fn(async (_id, q, purpose) =>
    (purpose === 'owner' ? [CAROL] : [BOB, CAROL]).filter((p) => p.label.toLowerCase().startsWith(q.toLowerCase())))
  projects.fetchCompanyPeople = vi.fn(async (q) => [BOB].filter((p) => p.label.toLowerCase().startsWith(q.toLowerCase())))
})

afterEach(() => vi.useRealTimers())

// ---------------------------------------------------------------------------
describe('members and guests', () => {
  function openMembers() {
    projects.addMember = vi.fn().mockResolvedValue({})
    projects.addGuest = vi.fn().mockResolvedValue({})
    const project = { id: 'prj_1', can: { manage_members: true }, members: MEMBERS, agents: [], guests: [] }
    return mount(ProjectMembersModal, { props: { modelValue: true, project, myEmail: ME }, attachTo: document.body })
  }

  it('a member is picked, asked for with purpose member, and added by address', async () => {
    openMembers()
    await flushPromises()
    const input = document.getElementById('project-add-member')
    expect(input.getAttribute('role')).toBe('combobox')
    await pick(input, 'bo', 'Bob Baker')
    expect(projects.fetchProjectPeople).toHaveBeenCalledWith('prj_1', 'bo', 'member')
    submitOf(input)
    await flushPromises()
    expect(projects.addMember).toHaveBeenCalledWith('prj_1', BOB.email)
  })

  it('a member typed but not picked is never added, and the field says why', async () => {
    openMembers()
    await flushPromises()
    const input = document.getElementById('project-add-member')
    await typeInto(input, 'bob@exampel.com')
    submitOf(input)
    await flushPromises()
    expect(projects.addMember).not.toHaveBeenCalled()
    expect(input.closest('form').parentElement.textContent).toMatch(/No one at your company matches|Choose a person/)
  })

  it('a guest is free to type an outside address', async () => {
    openMembers()
    await flushPromises()
    const input = document.getElementById('project-add-guest')
    await typeInto(input, 'client@outside.example')
    expect(projects.fetchProjectPeople).toHaveBeenCalledWith('prj_1', 'client@outside.example', 'guest')
    expect(document.body.textContent).toContain('Invite will invite this address')
    submitOf(input)
    await flushPromises()
    expect(projects.addGuest).toHaveBeenCalledWith('prj_1', 'client@outside.example')
  })

  it('a member row reads the name first, the email second', async () => {
    openMembers()
    await flushPromises()
    const row = [...document.querySelectorAll('li')].find((li) => li.textContent.includes('Carol Chen'))
    expect(row.textContent.indexOf('Carol Chen')).toBeLessThan(row.textContent.indexOf(CAROL.email))
  })
})

// ---------------------------------------------------------------------------
describe('the steward', () => {
  it('someone else is picked from the company, and sent as a person', async () => {
    projects.create = vi.fn().mockResolvedValue({ id: 'prj_9' })
    mount(ProjectCreateModal, { props: { modelValue: true, myEmail: ME }, attachTo: document.body })
    await flushPromises()
    const steward = document.getElementById('project-steward')
    steward.value = 'person'
    steward.dispatchEvent(new Event('change'))
    await flushPromises()
    document.getElementById('project-name').value = 'Q4'
    document.getElementById('project-name').dispatchEvent(new Event('input'))
    document.getElementById('project-goal').value = 'Ship it.'
    document.getElementById('project-goal').dispatchEvent(new Event('input'))
    const email = document.getElementById('project-steward-email')
    await pick(email, 'bo', 'Bob Baker')
    expect(projects.fetchCompanyPeople).toHaveBeenCalledWith('bo')
    submitOf(email)
    await flushPromises()
    expect(projects.create).toHaveBeenCalledWith(expect.objectContaining({ steward: { kind: 'person', ref: BOB.email } }))
  })

  it('a steward typed but not picked is not sent', async () => {
    projects.create = vi.fn().mockResolvedValue({ id: 'prj_9' })
    mount(ProjectCreateModal, { props: { modelValue: true, myEmail: ME }, attachTo: document.body })
    await flushPromises()
    const steward = document.getElementById('project-steward')
    steward.value = 'person'
    steward.dispatchEvent(new Event('change'))
    await flushPromises()
    document.getElementById('project-name').value = 'Q4'
    document.getElementById('project-name').dispatchEvent(new Event('input'))
    document.getElementById('project-goal').value = 'Ship it.'
    document.getElementById('project-goal').dispatchEvent(new Event('input'))
    const email = document.getElementById('project-steward-email')
    await typeInto(email, 'bob@exampel.com')
    submitOf(email)
    await flushPromises()
    expect(projects.create).not.toHaveBeenCalled()
  })
})

// ---------------------------------------------------------------------------
describe('a task owner', () => {
  const OPEN = { id: 'T-001', title: 'Draft', status: 'active', priority: 'p2', owner: CAROL.email, agent: null,
    objective: null, done_definition: null, updated_at: '2026-09-30T10:00:00Z', log: [] }

  function renderTasks(task = OPEN) {
    projects.fetchTasks = vi.fn()
    projects.tasks = { prj_1: { rows: [task], loaded: true, error: null } }
    projects.fetchTask = vi.fn().mockResolvedValue(task)
    projects.updateTask = vi.fn().mockResolvedValue({})
    projects.createTask = vi.fn().mockResolvedValue({})
    return mount(ProjectTasks, {
      props: { projectId: 'prj_1', canContribute: true, members: MEMBERS, myEmail: ME },
      attachTo: document.body,
    })
  }

  it('a task row names its owner', async () => {
    renderTasks()
    await flushPromises()
    expect(byTestId('project-task-T-001').textContent).toContain('Carol Chen')
  })

  it('editing opens with the current owner as the chosen person, and asks nothing', async () => {
    renderTasks()
    await flushPromises()
    byTestId('project-task-T-001').click()
    await flushPromises()
    const input = byTestId('project-task-owner')
    expect(input.value).toBe(CAROL.email)
    expect(input.validity.valid).toBe(true)
    expect(document.body.textContent).toContain('Carol Chen · carol@example.com')
    expect(projects.fetchProjectPeople).not.toHaveBeenCalled()
  })

  it('the chosen owner can be cleared, which saves no owner', async () => {
    renderTasks()
    await flushPromises()
    byTestId('project-task-T-001').click()
    await flushPromises()
    await typeInto(byTestId('project-task-owner'), '')
    byTestId('project-task-save').click()
    await flushPromises()
    expect(projects.updateTask).toHaveBeenCalledWith('prj_1', 'T-001', { owner: null })
  })

  it('a new owner is picked from the project with purpose owner', async () => {
    renderTasks({ ...OPEN, owner: null })
    await flushPromises()
    byTestId('project-task-T-001').click()
    await flushPromises()
    await pick(byTestId('project-task-owner'), 'ca', 'Carol Chen')
    expect(projects.fetchProjectPeople).toHaveBeenCalledWith('prj_1', 'ca', 'owner')
    byTestId('project-task-save').click()
    await flushPromises()
    expect(projects.updateTask).toHaveBeenCalledWith('prj_1', 'T-001', { owner: CAROL.email })
  })

  it('an owner typed but not picked is not saved', async () => {
    renderTasks({ ...OPEN, owner: null })
    await flushPromises()
    byTestId('project-task-T-001').click()
    await flushPromises()
    await typeInto(byTestId('project-task-owner'), 'Pat')
    byTestId('project-task-save').click()
    await flushPromises()
    expect(projects.updateTask).not.toHaveBeenCalled()
    expect(byTestId('project-task-owner').getAttribute('aria-invalid')).toBe('true')
  })

  it('a role owner (role:<id>) is still written as typed', async () => {
    renderTasks()
    await flushPromises()
    byTestId('project-task-T-001').click()
    await flushPromises()
    await typeInto(byTestId('project-task-owner'), 'role:ops')
    byTestId('project-task-save').click()
    await flushPromises()
    expect(projects.updateTask).toHaveBeenCalledWith('prj_1', 'T-001', { owner: 'role:ops' })
  })

  it('the create form picks an owner too', async () => {
    renderTasks()
    await flushPromises()
    byTestId('project-task-new').click()
    await flushPromises()
    document.getElementById('task-title').value = 'Brief'
    document.getElementById('task-title').dispatchEvent(new Event('input'))
    await pick(document.getElementById('task-owner'), 'ca', 'Carol Chen')
    submitOf(document.getElementById('task-owner'))
    await flushPromises()
    expect(projects.createTask).toHaveBeenCalledWith('prj_1', expect.objectContaining({ owner: CAROL.email }))
  })
})
