// @vitest-environment jsdom
/**
 * trinity-enterprise#1132 (part 1) — a person can tell where they stand on a
 * project, and why a control isn't there.
 *
 *   - the header states my role and what it lets me do (creator / member /
 *     company viewer); a guest keeps the guest note and never sees a role line;
 *   - where a control is withheld (New task, the log form, Add from…, member
 *     changes) one line says who can change it, in the control's place;
 *   - the task view says who confirms a finished task: a member, or the steward
 *     agent by name.
 *
 * Ask to join (part 2) waits on the person-request schema ruling (ent#1131 PR-B).
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
import ProjectPage from '@/components/portal/projects/ProjectPage.vue'
import ProjectTasks from '@/components/portal/projects/ProjectTasks.vue'
import ProjectLog from '@/components/portal/projects/ProjectLog.vue'
import ProjectMaterial from '@/components/portal/projects/ProjectMaterial.vue'
import ProjectMembersModal from '@/components/portal/projects/ProjectMembersModal.vue'

globalThis.ResizeObserver = globalThis.ResizeObserver || class {
  observe() {}
  unobserve() {}
  disconnect() {}
}

const EVA = 'eva@example.com'      // the creator
const BOB = 'bob@example.com'      // a member
const DAN = 'dan@example.com'      // a company viewer, not a member

const CAN = {
  creator: { edit: true, manage_members: true, manage_visibility: true, manage_agents: true, archive: true, work: true, contribute: true },
  member: { edit: false, manage_members: false, manage_visibility: false, manage_agents: false, archive: false, work: true, contribute: true },
  viewer: { edit: false, manage_members: false, manage_visibility: false, manage_agents: false, archive: false, work: true, contribute: false },
}

function project(role, over = {}) {
  return {
    id: 'prj_1', name: 'Q4 launch', goal: 'Ship it.', status: 'active', visibility: 'company',
    created_by: EVA, updated_at: '2026-11-09T10:00:00Z', archived_at: null,
    steward: { kind: 'agent', ref: 'scout' },
    members: [{ email: EVA, role: 'creator', label: 'Eva' }, { email: BOB, role: 'member', label: 'Bob' }],
    agents: [], my_chats: [], rooms: [], others_chat_count: 0, files: [], reports: [], decisions: [],
    my_role: role === 'viewer' ? null : role,
    can: CAN[role],
    rollup: { by_status: {}, health: null, health_due: false },
    ...over,
  }
}

const byTestId = (id) => document.querySelector(`[data-testid="${id}"]`)
let projects
let portal

beforeEach(() => {
  document.body.innerHTML = ''
  localStorage.clear()
  setActivePinia(createPinia())
  vi.clearAllMocks()
  projects = useProjectsStore()
  portal = useClientPortalStore()
  projects.fetchTasks = vi.fn()
  projects.fetchLog = vi.fn()
  projects.fetchProjectPeople = vi.fn().mockResolvedValue([])
})

async function page(p, me) {
  portal.agents = []
  projects.fetchProject = vi.fn()
  projects.pageState = vi.fn(() => ({ data: p, loaded: true, loading: false, error: null, notFound: false }))
  const w = mount(ProjectPage, { props: { projectId: p.id, myEmail: me }, attachTo: document.body })
  await flushPromises()
  return w
}

describe('ent#1132 — the header says my role and what it allows', () => {
  it('the creator', async () => {
    await page(project('creator'), EVA)
    expect(byTestId('project-my-role').textContent).toContain('You created this project. You can change its name, goal, steward, members, visibility and tasks')
  })

  it('a member, told who changes the rest', async () => {
    await page(project('member'), BOB)
    const line = byTestId('project-my-role').textContent
    expect(line).toContain("You're a member. You can add and change tasks")
    expect(line).toContain(`Only ${EVA} changes the name, goal, steward, members and visibility.`)
  })

  it('a company viewer, told why they can see it and who can add them', async () => {
    await page(project('viewer'), DAN)
    const line = byTestId('project-my-role').textContent
    expect(line).toContain("You can view this project because it's open to everyone at the company.")
    expect(line).toContain('only members add or change tasks')
    expect(line).toContain(`Ask ${EVA} to add you as a member.`)
  })

  it('an archived project says nothing changes until the creator restores it', async () => {
    await page(project('member', { archived_at: '2026-11-01T00:00:00Z' }), BOB)
    expect(byTestId('project-my-role').textContent).toBe(`This project is archived. Nothing on it changes until ${EVA} restores it.`)
  })

  it('the creator is "you" whatever the email case', async () => {
    await page(project('member', { archived_at: '2026-11-01T00:00:00Z', created_by: 'Bob@Example.com' }), BOB)
    expect(byTestId('project-my-role').textContent).toContain('until you restore it.')
  })

  it('a non-member on a members-only project gets a neutral line, not the company one', async () => {
    await page(project('viewer', { visibility: 'members' }), DAN)
    expect(byTestId('project-my-role').textContent).not.toContain('open to everyone')
  })

  it('a guest keeps the guest note and gets no role line', async () => {
    await page({ ...project('viewer'), guest: true, my_role: 'guest' }, 'client@outside.example')
    expect(byTestId('project-guest-note')).not.toBeNull()
    expect(byTestId('project-my-role')).toBeNull()
  })
})

describe('ent#1132 — a withheld control is said in its place', () => {
  const tasks = (p, me) => mount(ProjectTasks, {
    props: { project: p, projectId: p.id, canContribute: p.can.contribute, archived: Boolean(p.archived_at), myEmail: me, members: p.members },
    attachTo: document.body,
  })

  it('a company viewer reads who can add tasks, where New task would be', async () => {
    projects.tasks = { prj_1: { rows: [], loaded: true, error: null } }
    tasks(project('viewer'), DAN)
    await flushPromises()
    expect(byTestId('project-task-new')).toBeNull()
    expect(byTestId('project-tasks-withheld').textContent).toBe(`Only members add or change tasks. Ask ${EVA} to add you.`)
  })

  it('a member gets New task and no line', async () => {
    projects.tasks = { prj_1: { rows: [], loaded: true, error: null } }
    tasks(project('member'), BOB)
    await flushPromises()
    expect(byTestId('project-task-new')).not.toBeNull()
    expect(byTestId('project-tasks-withheld')).toBeNull()
  })

  it('an archived project says nothing extra (the archive already does)', async () => {
    projects.tasks = { prj_1: { rows: [], loaded: true, error: null } }
    tasks(project('viewer', { archived_at: '2026-11-01T00:00:00Z' }), DAN)
    await flushPromises()
    expect(byTestId('project-tasks-withheld')).toBeNull()
  })

  it('the log form is replaced by the line for a company viewer', async () => {
    projects.logs = { prj_1: { rows: [], loaded: true, error: null } }
    const p = project('viewer')
    mount(ProjectLog, { props: { project: p, projectId: p.id, canContribute: false, archived: false, myEmail: DAN }, attachTo: document.body })
    await flushPromises()
    expect(byTestId('project-log-form')).toBeNull()
    expect(byTestId('project-log-withheld').textContent).toBe(`Only members add or change log entries. Ask ${EVA} to add you.`)
  })

  it('Add from… is replaced by the line for a company viewer, never for a guest', async () => {
    mount(ProjectMaterial, { props: { project: project('viewer'), myEmail: DAN }, attachTo: document.body })
    await flushPromises()
    expect(byTestId('project-add-item')).toBeNull()
    expect(byTestId('project-material-withheld').textContent).toBe(`Only members add or change files and reports. Ask ${EVA} to add you.`)
    document.body.innerHTML = ''
    mount(ProjectMaterial, { props: { project: project('viewer'), guest: true, myEmail: 'client@outside.example' }, attachTo: document.body })
    await flushPromises()
    expect(byTestId('project-material-withheld')).toBeNull()
  })

  it('Members & access keeps its one existing notice — no second line (no repeat)', async () => {
    mount(ProjectMembersModal, {
      props: { modelValue: true, project: project('member'), myEmail: BOB, rosterAgents: [], ownedAgents: [] },
      attachTo: document.body,
    })
    await flushPromises()
    expect(document.querySelectorAll('[data-testid="project-members-readonly"]').length).toBe(1)
    expect(byTestId('project-members-withheld')).toBeNull()
  })

  it('a member whose read says no contribute gets no "ask to be added" line', async () => {
    projects.tasks = { prj_1: { rows: [], loaded: true, error: null } }
    const p = project('member', { can: { ...CAN.member, contribute: false } })
    tasks(p, BOB)
    await flushPromises()
    expect(byTestId('project-tasks-withheld')).toBeNull()
  })
})

describe('ent#1132 — the task view says who confirms a finished task', () => {
  const VERIFYING = { id: 'T-001', title: 'Draft', status: 'pending-verification', priority: 'p2', owner: null, agent: 'scout', updated_at: '2026-11-09T10:00:00Z' }

  it('names the steward agent beside the members when it is active on the project', async () => {
    projects.tasks = { prj_1: { rows: [VERIFYING], loaded: true, error: null } }
    const p = project('member', { agents: [{ agent_name: 'scout', state: 'active' }] })
    mount(ProjectTasks, { props: { project: p, projectId: p.id, canContribute: true, myEmail: BOB, members: p.members }, attachTo: document.body })
    await flushPromises()
    const rule = byTestId('project-tasks-verification-rule').textContent
    expect(rule).toContain('until a member, or the steward agent scout, confirms it.')
    expect(rule).toContain('then Confirm')
  })

  it('an agent steward that is not active on the project is not named', async () => {
    projects.tasks = { prj_1: { rows: [VERIFYING], loaded: true, error: null } }
    const p = project('member', { agents: [{ agent_name: 'scout', state: 'pending' }] })
    mount(ProjectTasks, { props: { project: p, projectId: p.id, canContribute: true, myEmail: BOB, members: p.members }, attachTo: document.body })
    await flushPromises()
    expect(byTestId('project-tasks-verification-rule').textContent).toContain('until a member confirms it.')
  })

  it('with a person steward (or none) it says a member', async () => {
    projects.tasks = { prj_1: { rows: [VERIFYING], loaded: true, error: null } }
    const p = project('viewer', { steward: { kind: 'person', ref: EVA } })
    mount(ProjectTasks, { props: { project: p, projectId: p.id, canContribute: false, myEmail: DAN, members: p.members }, attachTo: document.body })
    await flushPromises()
    const rule = byTestId('project-tasks-verification-rule').textContent
    expect(rule).toContain('until a member confirms it.')
    expect(rule).not.toContain('then Confirm')
  })
})
