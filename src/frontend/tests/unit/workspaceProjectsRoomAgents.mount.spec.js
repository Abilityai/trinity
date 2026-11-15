// @vitest-environment jsdom
/**
 * trinity-enterprise#1131 (PR-A) — a linked room's header says which of its
 * agents aren't on the project, mounted over the real projects store with the
 * Workspace HTTP client mocked (design-system contract, #2918):
 *   - the chip appears only when some room agent is not active;
 *   - the creator's Add writes through the existing add-agent route, then the
 *     room's read is refreshed; an agent not on their list has no Add;
 *   - a member sees states only, and is told who can add;
 *   - a failed Add says why next to the control;
 *   - after the creator links a room with agents not on the project, the list opens;
 *   - the chip's space is held while the room's read is in flight, so the
 *     header does not shift when it lands.
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

import { portalHttp, useClientPortalStore } from '@/stores/clientPortal'
import ProjectChatControls from '@/components/portal/projects/ProjectChatControls.vue'

const byTestId = (id) => document.querySelector(`[data-testid="${id}"]`)
const allByTestId = (id) => [...document.querySelectorAll(`[data-testid="${id}"]`)]

const BY_LINK = '/api/enterprise/projects/by-link/room/r1'
const linked = (over = {}) => ({
  id: 'prj_1', name: 'Q4 launch', status: 'active', archived_at: null,
  room_agents: [
    { agent_name: 'scout', state: 'active', can_add: false },
    { agent_name: 'ledger', state: 'off', can_add: true },
    { agent_name: 'mine', state: 'off', can_add: false, reason: 'not_on_your_list' },
  ],
  can: { manage_agents: true },
  ...over,
})

let portal
let reads   // what each by-link read answers, in order (the last one repeats)

function serve() {
  portalHttp.get.mockImplementation((url) => {
    if (url === BY_LINK) {
      const project = reads.length > 1 ? reads.shift() : reads[0]
      return Promise.resolve({ data: { project } })
    }
    return Promise.resolve({ data: { projects: [] } })
  })
}

function header() {
  return mount(ProjectChatControls, {
    props: { kind: 'room', targetId: 'r1', isMain: false },
    attachTo: document.body,
  })
}

beforeEach(() => {
  document.body.innerHTML = ''
  localStorage.clear()
  setActivePinia(createPinia())
  vi.clearAllMocks()
  portal = useClientPortalStore()
  portal.projectsAvailable = true
  portal.portalToken = null
  portal.platformFallbackSuppressed = false
  reads = [linked()]
  serve()
})

describe('a linked room says which agents are not on the project', () => {
  it("holds the chip's space while the room's read is in flight, then shows the chip in it", async () => {
    let land
    portalHttp.get.mockImplementation((url) => (url === BY_LINK
      ? new Promise((resolve) => { land = () => resolve({ data: { project: linked() } }) })
      : Promise.resolve({ data: { projects: [] } })))
    header()
    await flushPromises()
    const slot = byTestId('project-room-agents-chip-slot')
    expect(slot).not.toBeNull()
    expect(slot.getAttribute('aria-hidden')).toBe('true')
    expect(slot.classList.contains('invisible')).toBe(true)
    expect(slot.querySelector('button, [tabindex]:not([tabindex="-1"])')).toBeNull()
    expect(byTestId('project-room-agents-chip')).toBeNull()
    land()
    await flushPromises()
    expect(byTestId('project-room-agents-chip-slot')).toBeNull()
    expect(byTestId('project-room-agents-chip').textContent).toContain('2 not on it')
  })

  it('holds no chip space on a chat, which has no room agents', async () => {
    portalHttp.get.mockImplementation(() => new Promise(() => {}))
    mount(ProjectChatControls, { props: { kind: 'thread', targetId: 's1', agentName: 'scout', isMain: false },
      attachTo: document.body })
    await flushPromises()
    expect(byTestId('project-chat-controls')).not.toBeNull()
    expect(byTestId('project-room-agents-chip-slot')).toBeNull()
  })

  it('shows no chip when every room agent is on it', async () => {
    reads = [linked({ room_agents: [{ agent_name: 'scout', state: 'active', can_add: false }] })]
    header()
    await flushPromises()
    expect(byTestId('project-chat-badge')).not.toBeNull()
    expect(byTestId('project-room-agents-chip')).toBeNull()
  })

  it('shows no chip when the read carries no agents (a participant who cannot open the project)', async () => {
    reads = [{ id: 'prj_1', name: 'Q4 launch', status: 'active', archived_at: null }]
    header()
    await flushPromises()
    expect(byTestId('project-chat-badge')).not.toBeNull()
    expect(byTestId('project-room-agents-chip')).toBeNull()
  })

  it("lets the creator Add an agent through the existing route, then refreshes the room's read", async () => {
    portalHttp.post.mockResolvedValue({ data: { agent_name: 'ledger', state: 'pending' } })
    header()
    await flushPromises()
    expect(byTestId('project-room-agents-chip').textContent).toContain('2 not on it')
    byTestId('project-room-agents-chip').click()
    await flushPromises()
    const rows = allByTestId('project-room-agent-row')
    expect(rows.map((r) => r.dataset.agent)).toEqual(['scout', 'ledger', 'mine'])
    expect(rows[2].textContent).toContain('Not on your list')
    expect(rows[2].querySelector('[data-testid="project-room-agent-add"]')).toBeNull()
    expect(rows[0].querySelector('[data-testid="project-room-agent-add"]')).toBeNull()

    reads = [linked({ room_agents: [{ agent_name: 'ledger', state: 'pending', can_add: false }] })]
    const before = portalHttp.get.mock.calls.filter(([u]) => u === BY_LINK).length
    rows[1].querySelector('[data-testid="project-room-agent-add"]').click()
    await flushPromises()
    expect(portalHttp.post).toHaveBeenCalledWith('/api/enterprise/projects/prj_1/agents',
      { agent_name: 'ledger' }, expect.anything())
    expect(portalHttp.get.mock.calls.filter(([u]) => u === BY_LINK).length).toBe(before + 1)
    expect(byTestId('project-room-agent-row').textContent).toContain('Waiting for owner')
  })

  it('shows a member the states only, and who can add', async () => {
    reads = [linked({ can: { manage_agents: false },
      room_agents: [{ agent_name: 'ledger', state: 'off', can_add: false }] })]
    header()
    await flushPromises()
    byTestId('project-room-agents-chip').click()
    await flushPromises()
    expect(byTestId('project-room-agent-add')).toBeNull()
    expect(byTestId('project-room-agents-modal').textContent).toContain("Only the project's creator can add agents")
  })

  it('says why an Add failed, next to the list', async () => {
    portalHttp.post.mockRejectedValue({ response: { status: 409, data: { detail: { code: 'owner_unreachable' } } } })
    header()
    await flushPromises()
    byTestId('project-room-agents-chip').click()
    await flushPromises()
    byTestId('project-room-agent-add').click()
    await flushPromises()
    expect(byTestId('project-room-agents-modal').textContent).toContain("This agent's owner can't be asked here")
  })

  it('says the room could not be read', async () => {
    reads = [linked({ room_agents: null, degraded: ['room'] })]
    header()
    await flushPromises()
    byTestId('project-room-agents-chip').click()
    await flushPromises()
    expect(byTestId('project-room-agents-modal').textContent).toContain("Couldn't read this room's agents")
  })

  it('opens the list after the creator links a room whose agents are not on the project', async () => {
    reads = [null, linked()]
    portalHttp.post.mockResolvedValue({ data: {} })
    const list = [{ id: 'prj_1', name: 'Q4 launch', archived_at: null, agents: [] }]
    portalHttp.get.mockImplementation((url) => (url === BY_LINK
      ? Promise.resolve({ data: { project: reads.length > 1 ? reads.shift() : reads[0] } })
      : Promise.resolve({ data: { projects: list } })))
    header()
    await flushPromises()
    byTestId('project-chat-open').click()
    await flushPromises()
    const select = document.querySelector('#project-chat-pick')
    select.value = 'prj_1'
    select.dispatchEvent(new Event('change'))
    await flushPromises()
    select.closest('form').dispatchEvent(new Event('submit'))
    await flushPromises()
    expect(portalHttp.post).toHaveBeenCalledWith('/api/enterprise/projects/prj_1/links',
      { kind: 'room', target_id: 'r1' }, expect.anything())
    expect(byTestId('project-room-agents-modal')).not.toBeNull()
    expect(byTestId('project-room-agent-add')).not.toBeNull()
  })
})
