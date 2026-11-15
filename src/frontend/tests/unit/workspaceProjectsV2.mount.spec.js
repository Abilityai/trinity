// @vitest-environment jsdom
/**
 * ent#661 v2 — tasks, the log, files & reports, guests and Wrap up, mounted.
 *
 * Every predicate here gates a store write or what an outside client sees, so
 * each is proven on the rendered component (design-system contract, #2918):
 *   - a done task offers only "Reopen"; saving it sends status `active`;
 *   - the log's add form is absent for anyone who may not contribute;
 *   - a guest's Files & reports shows no decisions and no share/remove controls;
 *   - the chat header gives a guest the badge and Detach, never the Project
 *     button or Wrap up; Wrap up emits the linked project;
 *   - inviting a guest calls the store with the email.
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

import { useClientPortalStore } from '@/stores/clientPortal'
import { useProjectsStore } from '@/stores/projects'
import ProjectTasks from '@/components/portal/projects/ProjectTasks.vue'
import ProjectLog from '@/components/portal/projects/ProjectLog.vue'
import ProjectMaterial from '@/components/portal/projects/ProjectMaterial.vue'
import ProjectChatControls from '@/components/portal/projects/ProjectChatControls.vue'
import ProjectMembersModal from '@/components/portal/projects/ProjectMembersModal.vue'

globalThis.ResizeObserver = globalThis.ResizeObserver || class {
  observe() {}
  unobserve() {}
  disconnect() {}
}

const byTestId = (id) => document.querySelector(`[data-testid="${id}"]`)
let portal
let projects

beforeEach(() => {
  document.body.innerHTML = ''
  localStorage.clear()
  setActivePinia(createPinia())
  vi.clearAllMocks()
  portal = useClientPortalStore()
  projects = useProjectsStore()
})

const DONE = { id: 'T-001', title: 'Draft', status: 'done', priority: 'p2', owner: null, agent: null,
  objective: null, done_definition: null, updated_at: '2026-09-30T10:00:00Z', log: [] }

describe('tasks', () => {
  it('a done task offers only Reopen, and saving sends status active', async () => {
    projects.fetchTasks = vi.fn()
    projects.tasks = { prj_1: { rows: [DONE], loaded: true, error: null } }
    projects.fetchTask = vi.fn().mockResolvedValue(DONE)
    projects.updateTask = vi.fn().mockResolvedValue({})
    mount(ProjectTasks, { props: { projectId: 'prj_1', canContribute: true }, attachTo: document.body })
    await flushPromises()
    byTestId('project-task-T-001').click()
    await flushPromises()
    const select = document.getElementById('task-status')
    expect([...select.options].map((o) => o.value)).toEqual(['done', 'active'])
    select.value = 'active'
    select.dispatchEvent(new Event('change'))
    await flushPromises()
    byTestId('project-task-save').click()
    await flushPromises()
    expect(projects.updateTask).toHaveBeenCalledWith('prj_1', 'T-001', { status: 'active' })
  })
})

// #867 — a contributor changes a task's owner from the task detail. The owner
// is free text (≤320, so a later `role:<id>` form is not refused); a blank
// field clears it; a done task keeps it until reopened; the agent stays read-only.
describe("#867 — a task's owner is editable by contributors", () => {
  const OPEN = { ...DONE, status: 'active', owner: 'a@example.com', agent: 'helper' }

  async function openDetail(task, { after, props } = {}) {
    projects.fetchTasks = vi.fn()
    projects.tasks = { prj_1: { rows: [task], loaded: true, error: null } }
    projects.fetchTask = vi.fn().mockResolvedValueOnce(task).mockResolvedValueOnce(after || task)
    projects.updateTask = vi.fn().mockResolvedValue({})
    // ent#1155: a new owner is picked from the project's own people.
    projects.fetchProjectPeople = vi.fn(async (_id, q) =>
      [{ email: 'b@example.com', label: 'Bee Bo' }].filter((p) => p.email.startsWith(q.trim())))
    mount(ProjectTasks, { props: { projectId: 'prj_1', canContribute: true, ...props }, attachTo: document.body })
    await flushPromises()
    byTestId('project-task-T-001').click()
    await flushPromises()
  }
  async function typeOwner(value) {
    const input = byTestId('project-task-owner')
    input.value = value
    input.dispatchEvent(new Event('input'))
    await flushPromises()
  }
  /** Type part of the owner and pick them from the list (ent#1155: the owner is must-pick). */
  async function pickOwner(value) {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    try {
      await typeOwner(value)
      vi.advanceTimersByTime(250)
      await flushPromises()
    } finally {
      vi.useRealTimers()
    }
    const row = [...document.querySelectorAll('[role="option"]')].find((o) => o.textContent.includes('Bee Bo'))
    row.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }))
    await flushPromises()
  }
  async function save() {
    byTestId('project-task-save').click()
    await flushPromises()
  }
  const dds = () => [...document.querySelectorAll('[data-testid="project-task-detail"] dl > div')]
    .reduce((m, d) => ({ ...m, [d.querySelector('dt').textContent.trim()]: d.querySelector('dd').textContent.trim() }), {})

  it('T1: changing the owner sends exactly the new owner', async () => {
    await openDetail(OPEN, { after: { ...OPEN, owner: 'b@example.com' } })
    expect(byTestId('project-task-owner').value).toBe('a@example.com')
    await pickOwner('  b@exa')
    await save()
    expect(projects.updateTask).toHaveBeenCalledWith('prj_1', 'T-001', { owner: 'b@example.com' })
    expect(byTestId('project-task-owner').value).toBe('b@example.com')
    expect(byTestId('project-task-save').disabled).toBe(true)
  })

  it('T2: clearing the owner (blank or whitespace) sends null, and is enough to enable Save', async () => {
    await openDetail(OPEN, { after: { ...OPEN, owner: null } })
    await typeOwner('   ')
    expect(byTestId('project-task-save').disabled).toBe(false)
    await save()
    expect(projects.updateTask).toHaveBeenCalledWith('prj_1', 'T-001', { owner: null })
    expect(byTestId('project-task-owner').value).toBe('')
  })

  it('T3: a non-email owner such as role:ops is sent as typed, with no field error', async () => {
    await openDetail(OPEN, { after: { ...OPEN, owner: 'role:ops' } })
    await typeOwner('role:ops')
    expect(byTestId('project-task-owner').type).toBe('text')
    expect(byTestId('project-task-owner').getAttribute('aria-invalid')).toBeNull()
    await save()
    expect(projects.updateTask).toHaveBeenCalledWith('prj_1', 'T-001', { owner: 'role:ops' })
  })

  it('T4: a status-only edit sends no owner key', async () => {
    await openDetail(OPEN)
    const select = document.getElementById('task-status')
    select.value = 'blocked'
    select.dispatchEvent(new Event('change'))
    await flushPromises()
    await save()
    expect(projects.updateTask).toHaveBeenCalledTimes(1)
    expect(projects.updateTask.mock.calls[0][2]).not.toHaveProperty('owner')
  })

  it('T5: a done task shows the owner field disabled with the reopen hint, and reopening sends status only', async () => {
    await openDetail({ ...DONE, owner: 'a@example.com' })
    const input = byTestId('project-task-owner')
    expect(input.disabled).toBe(true)
    expect(document.getElementById('task-owner-edit-help').textContent).toContain('Reopen the task to change its owner.')
    const select = document.getElementById('task-status')
    select.value = 'active'
    select.dispatchEvent(new Event('change'))
    await flushPromises()
    await save()
    expect(projects.updateTask).toHaveBeenCalledWith('prj_1', 'T-001', { status: 'active' })
  })

  it.each([
    ['a non-contributor', { canContribute: false }],
    ['an archived project', { archived: true }],
  ])('T6: %s gets no owner field, only the read-only owner', async (_, props) => {
    await openDetail(OPEN, { props })
    expect(byTestId('project-task-owner')).toBeNull()
    expect(dds().Owner).toBe('a@example.com')
  })

  it('T7: the agent stays read-only and is never sent', async () => {
    await openDetail(OPEN, { after: { ...OPEN, owner: 'b@example.com' } })
    expect(dds().Agent).toBe('helper')
    const labels = [...document.querySelectorAll('[data-testid="project-task-detail"] form label')].map((l) => l.textContent.trim())
    expect(labels).toContain('Owner')
    expect(labels.some((l) => /agent/i.test(l))).toBe(false)
    await pickOwner('b@exa')
    await save()
    expect(projects.updateTask.mock.calls[0][2]).not.toHaveProperty('agent')
  })

  it('T8: a server refusal shows its message and keeps the typed owner', async () => {
    await openDetail(OPEN)
    projects.updateTask = vi.fn().mockRejectedValue({ response: { status: 409, data: { detail: { code: 'archived', message: 'archived' } } } })
    await pickOwner('b@exa')
    await save()
    expect(byTestId('inline-error').textContent).toContain('This project is archived. Restore it to add to it.')
    expect(byTestId('project-task-owner').value).toBe('b@example.com')
  })

  it('T9: the field is capped at the server limit of 320 characters', async () => {
    await openDetail(OPEN)
    expect(byTestId('project-task-owner').getAttribute('maxlength')).toBe('320')
  })

  it('T10: a task with no owner and a blank field is not dirty', async () => {
    await openDetail({ ...OPEN, owner: null })
    expect(byTestId('project-task-owner').value).toBe('')
    expect(byTestId('project-task-save').disabled).toBe(true)
  })
})

describe('log', () => {
  it('the add form is absent for a non-contributor', async () => {
    projects.fetchLog = vi.fn()
    projects.logs = { prj_1: { rows: [], loaded: true, error: null } }
    const w = mount(ProjectLog, { props: { projectId: 'prj_1', canContribute: false } })
    await flushPromises()
    expect(w.find('[data-testid="project-log-form"]').exists()).toBe(false)
  })

  it('a contributor adds an entry of the chosen kind', async () => {
    projects.fetchLog = vi.fn()
    projects.logs = { prj_1: { rows: [], loaded: true, error: null } }
    projects.addLogEntry = vi.fn().mockResolvedValue({})
    const w = mount(ProjectLog, { props: { projectId: 'prj_1', canContribute: true } })
    await flushPromises()
    await w.find('#log-body').setValue('Three tiers.')
    await w.find('[data-testid="project-log-form"]').trigger('submit')
    await flushPromises()
    expect(projects.addLogEntry).toHaveBeenCalledWith('prj_1', { kind: 'decision', body: 'Three tiers.' })
  })
})

describe('files & reports', () => {
  const project = {
    id: 'prj_1', can: { contribute: true, manage_members: true },
    files: [{ id: 'f1', filename: 'brief.pdf', size_bytes: 10, agent_name: 'scout', audience: 'guests',
      expires_at: '2099-01-01T00:00:00Z', download_url: '/api/files/f1?sig=x&download=1', linked_by: 'me@x.com' }],
    reports: [],
    decisions: [{ id: 'd1', outcome: 'approved', decided: 'Three tiers', agent_name: 'scout', decided_by: 'me@x.com',
      decided_at: '2026-09-30T10:00:00Z', linked_by: 'me@x.com' }],
  }

  it('a guest sees shared files, no decisions, and no share or remove controls', () => {
    const w = mount(ProjectMaterial, { props: { project: { ...project, can: { work: true } }, guest: true } })
    expect(w.text()).toContain('brief.pdf')
    expect(w.text()).not.toContain('Three tiers')
    expect(w.text()).not.toMatch(/Share with guests|Stop sharing|Remove/)
    expect(w.find('[data-testid="project-add-item"]').exists()).toBe(false)
  })

  it('the creator can stop sharing an item with guests', async () => {
    projects.setItemAudience = vi.fn().mockResolvedValue({})
    const w = mount(ProjectMaterial, { props: { project, myEmail: 'me@x.com' } })
    const stop = w.findAll('button').find((b) => b.text() === 'Stop sharing')
    await stop.trigger('click')
    expect(projects.setItemAudience).toHaveBeenCalledWith('prj_1', 'file', 'f1', 'members')
  })
})

describe('chat header', () => {
  function header(props = {}) {
    return mount(ProjectChatControls, {
      props: { kind: 'thread', agentName: 'scout', targetId: 's1', sessionTitle: 'Pricing', isMain: false, ...props },
      attachTo: document.body,
    })
  }

  it('a guest gets the badge and Detach, not the Project button or Wrap up', async () => {
    portal.projectsAvailable = true
    portal.portalToken = 'client-token'
    projects.fetchForLink = vi.fn()
    projects.forLink = vi.fn(() => ({ id: 'prj_1', name: 'Q4', guest: true }))
    const w = header()
    await flushPromises()
    expect(w.find('[data-testid="project-chat-badge"]').exists()).toBe(true)
    expect(w.find('[data-testid="project-chat-detach"]').exists()).toBe(true)
    expect(w.find('[data-testid="project-chat-wrapup"]').exists()).toBe(false)
    projects.forLink = vi.fn(() => null)
    const unlinked = header({ targetId: 's2' })
    await flushPromises()
    expect(unlinked.find('[data-testid="project-chat-open"]').exists()).toBe(false)
  })

  it('Wrap up emits the linked project for a platform user', async () => {
    portal.projectsAvailable = true
    portal.portalToken = null
    portal.platformFallbackSuppressed = false
    projects.fetchForLink = vi.fn()
    projects.forLink = vi.fn(() => ({ id: 'prj_1', name: 'Q4' }))
    const w = header()
    await flushPromises()
    await w.find('[data-testid="project-chat-wrapup"]').trigger('click')
    expect(w.emitted('wrap-up')[0][0]).toMatchObject({ id: 'prj_1', name: 'Q4' })
  })
})

describe('guests', () => {
  it('the creator invites a guest by email', async () => {
    projects.addGuest = vi.fn().mockResolvedValue({})
    const project = { id: 'prj_1', can: { manage_members: true }, members: [], agents: [], guests: [] }
    mount(ProjectMembersModal, { props: { modelValue: true, project }, attachTo: document.body })
    await flushPromises()
    const input = document.getElementById('project-add-guest')
    input.value = 'client@outside.example'
    input.dispatchEvent(new Event('input'))
    await flushPromises()
    input.closest('form').dispatchEvent(new Event('submit'))
    await flushPromises()
    expect(projects.addGuest).toHaveBeenCalledWith('prj_1', 'client@outside.example')
  })
})
