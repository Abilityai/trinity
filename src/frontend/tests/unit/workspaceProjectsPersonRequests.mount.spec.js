// @vitest-environment jsdom
/**
 * The person-request lane, mounted: Ask to join (trinity-enterprise#1132 part 2)
 * and Ask to add a linked room's agent (trinity-enterprise#1131 part 2).
 *
 *   - a company viewer reads "Ask to join"; after asking, "Waiting on <creator>";
 *     a decline shows the creator's reason and "Ask again"; a member and a guest
 *     see nothing; an older backend (no `my_request`) changes nothing;
 *   - the role line and the withheld lines point at Ask to join when it is offered;
 *   - in a linked room, a member who can't add an agent gets "Ask to add", a
 *     pending request reads as waiting, a decline offers "Ask again".
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
import { useProjectsStore } from '@/stores/projects'
import ProjectPage from '@/components/portal/projects/ProjectPage.vue'
import ProjectChatControls from '@/components/portal/projects/ProjectChatControls.vue'
import { joinRequestState, roleLine, roomAgentRows, withheldLine } from '@/components/portal/projects/projectsUtils'

globalThis.ResizeObserver = globalThis.ResizeObserver || class {
  observe() {}
  unobserve() {}
  disconnect() {}
}

const EVA = 'eva@example.com'   // the creator
const DAN = 'dan@example.com'   // a company viewer
const byTestId = (id) => document.querySelector(`[data-testid="${id}"]`)

function project(over = {}) {
  return {
    id: 'prj_1', name: 'Q4 launch', goal: 'Ship it.', status: 'active', visibility: 'company',
    created_by: EVA, updated_at: '2026-11-09T10:00:00Z', archived_at: null, steward: null,
    members: [{ email: EVA, role: 'creator', label: 'Eva' }], agents: [], my_chats: [], rooms: [],
    others_chat_count: 0, files: [], reports: [], decisions: [], my_role: null,
    can: { work: true, contribute: false }, rollup: { by_status: {} },
    my_request: { can_ask_to_join: true, last: null },
    ...over,
  }
}

let projects
let portal

beforeEach(() => {
  document.body.innerHTML = ''
  localStorage.clear()
  setActivePinia(createPinia())
  vi.clearAllMocks()
  projects = useProjectsStore()
  portal = useClientPortalStore()
  portal.agents = []
})

async function page(p, me = DAN) {
  projects.fetchProject = vi.fn()
  projects.pageState = vi.fn(() => ({ data: p, loaded: true, loading: false, error: null, notFound: false }))
  mount(ProjectPage, { props: { projectId: p.id, myEmail: me }, attachTo: document.body })
  await flushPromises()
}

describe('ent#1132 part 2 — Ask to join', () => {
  it('a company viewer asks in one click', async () => {
    projects.requestToJoin = vi.fn().mockResolvedValue({})
    await page(project())
    expect(byTestId('project-join-ask').textContent.trim()).toBe('Ask to join')
    byTestId('project-join-ask').click()
    await flushPromises()
    expect(projects.requestToJoin).toHaveBeenCalledWith('prj_1')
  })

  it('a pending request says who it waits on, with no button', async () => {
    await page(project({ my_request: { can_ask_to_join: true, last: { request: 'join', state: 'pending', waiting_on: EVA } } }))
    expect(byTestId('project-join-pending').textContent).toBe(`Asked to join. Waiting on ${EVA}.`)
    expect(byTestId('project-join-ask')).toBeNull()
  })

  it("a decline shows the creator's reason and Ask again", async () => {
    await page(project({ my_request: { can_ask_to_join: true, last: { request: 'join', state: 'declined', reason: 'Small team for now.' } } }))
    expect(byTestId('project-join-declined').textContent).toContain(`${EVA} declined your request: Small team for now.`)
    expect(byTestId('project-join-ask').textContent.trim()).toBe('Ask again')
  })

  it('the server refusal renders next to the button', async () => {
    projects.requestToJoin = vi.fn().mockRejectedValue({
      response: { status: 503, data: { detail: { code: 'requests_unavailable', message: "Requests can't be sent on this Trinity right now." } } },
    })
    await page(project())
    byTestId('project-join-ask').click()
    await flushPromises()
    expect(byTestId('project-join').querySelector('[role="alert"]').textContent).toBe("Requests can't be sent on this Trinity right now.")
  })

  it('a member, a guest and an older backend see nothing', async () => {
    await page(project({ my_role: 'member', can: { contribute: true }, my_request: { can_ask_to_join: false, last: null } }))
    expect(byTestId('project-join')).toBeNull()
    document.body.innerHTML = ''
    await page(project({ guest: true, my_role: 'guest', my_request: undefined }))
    expect(byTestId('project-join')).toBeNull()
    document.body.innerHTML = ''
    await page(project({ my_request: undefined }))
    expect(byTestId('project-join')).toBeNull()
  })

  it('the role line and the withheld lines point at Ask to join when it is offered', () => {
    expect(roleLine(project(), DAN)).toContain(`Ask to join, and ${EVA} decides.`)
    expect(withheldLine({ project: project(), myEmail: DAN, canContribute: false, archived: false }, 'tasks'))
      .toBe('Only members add or change tasks. Use Ask to join at the top of the page.')
    // An older backend keeps the earlier wording.
    expect(roleLine(project({ my_request: undefined }), DAN)).toContain(`Ask ${EVA} to add you as a member.`)
  })

  it('joinRequestState: an approved request offers nothing more', () => {
    expect(joinRequestState(project({ my_role: 'member', my_request: { can_ask_to_join: false, last: { request: 'join', state: 'applied' } } }))).toBeNull()
    expect(joinRequestState(project({ my_request: { unknown: true } }))).toBeNull()
  })
})

describe('ent#1131 part 2 — Ask to add a room agent', () => {
  const BY_LINK = '/api/enterprise/projects/by-link/room/r1'
  const linked = (agents) => ({
    id: 'prj_1', name: 'Q4 launch', status: 'active', archived_at: null,
    room_agents: agents, can: { manage_agents: false },
  })

  it('the rows: Ask to add, waiting, and Ask again after a decline', () => {
    const view = roomAgentRows(linked([
      { agent_name: 'scout', state: 'off', can_add: false, can_request: true, my_request: null },
      { agent_name: 'ledger', state: 'off', can_add: false, can_request: true, my_request: 'pending' },
      { agent_name: 'mine', state: 'declined', can_add: false, can_request: true, my_request: 'declined' },
      { agent_name: 'done', state: 'active', can_add: false },
      { agent_name: 'other', state: 'off', can_add: false, can_request: true, my_request: 'blocked' },
    ]))
    const by = Object.fromEntries(view.rows.map((r) => [r.name, r]))
    expect(by.scout.action).toBe('request')
    expect([by.ledger.action, by.ledger.note]).toEqual([null, 'Asked — waiting on the creator'])
    expect([by.mine.action, by.mine.requested]).toEqual(['request', 'declined'])
    expect(by.done.action).toBeNull()
    // One request at a time: beside a pending one, no second Ask (it would 409).
    expect([by.other.action, by.other.note]).toEqual([null, 'You have a request waiting on the creator'])
  })

  it('a member clicks Ask to add and the request is filed for that agent', async () => {
    portal.projectsAvailable = true
    portalHttp.get.mockImplementation((url) => Promise.resolve(url === BY_LINK
      ? { data: { project: linked([{ agent_name: 'scout', state: 'off', can_add: false, can_request: true, my_request: null }]) } }
      : { data: { projects: [] } }))
    portalHttp.post.mockResolvedValue({ data: {} })
    mount(ProjectChatControls, { props: { kind: 'room', targetId: 'r1', isMain: false }, attachTo: document.body })
    await flushPromises()
    byTestId('project-room-agents-chip').click()
    await flushPromises()
    const ask = byTestId('project-room-agent-request')
    expect(ask.textContent.trim()).toBe('Ask to add')
    ask.click()
    await flushPromises()
    expect(portalHttp.post).toHaveBeenCalledWith('/api/enterprise/projects/prj_1/agent-requests',
      { agent_name: 'scout' }, expect.anything())
  })
})
