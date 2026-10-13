// @vitest-environment jsdom
/**
 * ent#661 — Workspace Projects, mounted.
 *
 * The predicates that gate a destructive verb or a store write, proven on the
 * rendered component (design-system contract, #2918), not on its source:
 *   - removing a member or an agent writes ONLY after the confirm;
 *   - a member sees the lists read-only; an agent's owner may still withdraw
 *     their own agent;
 *   - the chat header offers project controls only outside Main, only to a
 *     platform principal with the capability, and Detach unlinks THIS chat;
 *   - "Work on it" with a new agent creates the chat, links it, then opens it.
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
import ProjectMembersModal from '@/components/portal/projects/ProjectMembersModal.vue'
import ProjectChatControls from '@/components/portal/projects/ProjectChatControls.vue'
import ProjectPage from '@/components/portal/projects/ProjectPage.vue'

const PROJECT = {
  id: 'prj_1', name: 'Q4 launch', goal: 'Ship it.', status: 'active', visibility: 'members',
  created_by: 'eva@example.com', updated_at: '2026-09-29T10:00:00Z', archived_at: null, steward: null,
  members: [{ email: 'eva@example.com', role: 'creator' }, { email: 'bob@example.com', role: 'member' }],
  agents: [{ agent_name: 'scout', state: 'active' }, { agent_name: 'mine', state: 'active' }],
  my_chats: [], rooms: [], others_chat_count: 0,
  can: { manage_members: true, manage_agents: true, edit: true, archive: true, work: true },
}

const byTestId = (id) => document.querySelector(`[data-testid="${id}"]`)
let portal
let projects

// jsdom has no ResizeObserver; OverflowTabs (the project page's tabs) measures
// with one. Same inert stub as portalComposerDraft.spec.js.
globalThis.ResizeObserver = globalThis.ResizeObserver || class {
  observe() {}
  unobserve() {}
  disconnect() {}
}

beforeEach(() => {
  document.body.innerHTML = ''
  localStorage.clear()
  setActivePinia(createPinia())
  vi.clearAllMocks()
  portal = useClientPortalStore()
  projects = useProjectsStore()
})

describe('Members & access', () => {
  function open(project, props = {}) {
    return mount(ProjectMembersModal, {
      props: { modelValue: true, project, myEmail: 'eva@example.com', rosterAgents: [], ownedAgents: [], ...props },
      attachTo: document.body,
    })
  }

  it('removes a member only after the confirm', async () => {
    projects.removeMember = vi.fn().mockResolvedValue()
    open(PROJECT)
    await flushPromises()
    byTestId('remove-member-bob@example.com').click()
    await flushPromises()
    expect(projects.removeMember).not.toHaveBeenCalled()
    expect(byTestId('confirm-dialog-message').textContent).toContain('no longer see this project')
    byTestId('confirm-dialog-confirm').click()
    await flushPromises()
    expect(projects.removeMember).toHaveBeenCalledWith('prj_1', 'bob@example.com')
  })

  it('never offers to remove the creator', async () => {
    open(PROJECT)
    await flushPromises()
    expect(byTestId('remove-member-eva@example.com')).toBe(null)
  })

  it('a member sees it read-only, but may withdraw their own agent', async () => {
    projects.removeAgent = vi.fn().mockResolvedValue()
    open({ ...PROJECT, can: { manage_members: false } }, { myEmail: 'bob@example.com', ownedAgents: ['mine'] })
    await flushPromises()
    expect(byTestId('project-members-readonly')).not.toBe(null)
    expect(byTestId('remove-member-bob@example.com')).toBe(null)
    expect(byTestId('remove-agent-scout')).toBe(null)
    byTestId('remove-agent-mine').click()
    await flushPromises()
    byTestId('confirm-dialog-confirm').click()
    await flushPromises()
    expect(projects.removeAgent).toHaveBeenCalledWith('prj_1', 'mine')
  })
})

describe('chat header controls', () => {
  function header(props = {}) {
    return mount(ProjectChatControls, {
      props: { kind: 'thread', agentName: 'scout', targetId: 's1', sessionTitle: 'Pricing', isMain: false, ...props },
      attachTo: document.body,
    })
  }

  function entitledPlatform() {
    portal.projectsAvailable = true
    portal.portalToken = null
    portal.platformFallbackSuppressed = false
  }

  it('shows nothing in Main, to an outside client, or without the capability', async () => {
    projects.fetchForLink = vi.fn()
    entitledPlatform()
    expect(header({ isMain: true }).find('[data-testid="project-chat-controls"]').exists()).toBe(false)
    portal.projectsAvailable = false
    expect(header().find('[data-testid="project-chat-controls"]').exists()).toBe(false)
    portal.projectsAvailable = true
    portal.portalToken = 'client-token'   // an outside client's session
    expect(header().find('[data-testid="project-chat-controls"]').exists()).toBe(false)
  })

  it('in a linked chat, Detach unlinks this chat from its project', async () => {
    entitledPlatform()
    projects.fetchForLink = vi.fn()
    projects.forLink = vi.fn(() => ({ id: 'prj_1', name: 'Q4 launch' }))
    projects.unlink = vi.fn().mockResolvedValue()
    const w = header()
    await flushPromises()
    expect(w.find('[data-testid="project-chat-badge"]').text()).toContain('Q4 launch')
    await w.find('[data-testid="project-chat-detach"]').trigger('click')
    await flushPromises()
    expect(projects.unlink).toHaveBeenCalledWith('prj_1', 'thread', 's1')
  })
})

describe('Work on it', () => {
  it('starts a chat with the agent, links it, then opens it', async () => {
    portal.agents = [{ name: 'scout' }]
    projects.fetchProject = vi.fn()
    projects.pageState = vi.fn(() => ({ data: PROJECT, loaded: true, loading: false, error: null, notFound: false }))
    portal.createSession = vi.fn().mockResolvedValue({ id: 'new-s' })
    projects.link = vi.fn().mockResolvedValue()
    const w = mount(ProjectPage, { props: { projectId: 'prj_1', myEmail: 'eva@example.com' }, attachTo: document.body })
    await flushPromises()
    await w.find('[data-testid="work-on-scout"] button').trigger('click')
    await flushPromises()
    expect(portal.createSession).toHaveBeenCalledWith('scout')
    expect(projects.link).toHaveBeenCalledWith('prj_1', 'thread', 'new-s')
    expect(w.emitted('open-thread')[0][0]).toEqual({ id: 'new-s', agent_name: 'scout' })
  })

  it('does not offer an agent that is not on my list', async () => {
    portal.agents = []
    projects.fetchProject = vi.fn()
    projects.pageState = vi.fn(() => ({ data: PROJECT, loaded: true, loading: false, error: null, notFound: false }))
    const w = mount(ProjectPage, { props: { projectId: 'prj_1' }, attachTo: document.body })
    await flushPromises()
    expect(w.find('[data-testid="work-on-scout"] button').exists()).toBe(false)
    expect(w.find('[data-testid="work-on-scout"]').text()).toContain('Not on your list')
  })
})
