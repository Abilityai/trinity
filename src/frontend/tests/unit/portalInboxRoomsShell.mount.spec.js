// @vitest-environment jsdom
/**
 * trinity-enterprise#959 — rooms through the Workspace SHELL (`Portal.vue`,
 * shallow-mounted for real, the ent#610 harness).
 *
 *   - a member room's server count reaches BOTH the sidebar and the Inbox from
 *     the one `sidebarThreads` projection, so the pinned row's total and the
 *     Inbox's rows agree (AC 4) with a room in them;
 *   - the room on stage reading itself (`PortalRoom` → `read`) goes through the
 *     shell's `markRead('room', id)` — the store write, kind `room`.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { shallowMount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'

vi.hoisted(() => {
  window.matchMedia = window.matchMedia || ((q) => ({
    matches: false, media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
  }))
})

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: true, authHeader: {}, userEmail: 'me@example.com', logout: vi.fn() }),
}))
vi.mock('axios', () => {
  const mk = () => ({
    get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
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
import Portal from '@/views/Portal.vue'
import { inboxCounts, unreadItems } from '@/components/portal/portalInbox'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

const Blank = { template: '<div />' }
function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/workspace', component: Blank },
      { path: '/workspace/inbox', component: Blank },
      { path: '/workspace/c/:sessionId', component: Blank },
      { path: '/workspace/r/:roomId', component: Blank },
      { path: '/workspace/a/:agentName', component: Blank },
      { path: '/login', component: Blank },
    ],
  })
}

const LIST = [
  { id: 't1', session_id: 't1', agent_name: 'scout', is_main: false, title: 'Chat', last_message_at: '2026-10-21T10:00:00Z' },
  { id: 'room_1', is_room: true, member: true, title: 'Pricing', name: 'Pricing', agent_names: ['scout', 'sage'], last_message_at: '2026-10-21T11:00:00Z' },
  { id: 'room_x', is_room: true, member: false, title: 'Board', name: 'Board', agent_names: ['sage'], last_message_at: '2026-10-21T11:30:00Z' },
]
const STATE = { 'thread:t1': { unread: 1 }, 'room:room_1': { unread: 3 } }

let store
function arm() {
  store = useClientPortalStore()
  store.portalToken = 'tok'
  store.multiAgentChatAvailable = true
  store.fetchRoster = vi.fn(async () => { store.agents = [{ name: 'scout' }, { name: 'sage' }]; store.rosterLoaded = true })
  store.fetchAllSessions = vi.fn(async () => { store.sessionsFailed = false; return LIST })
  store.fetchChatState = vi.fn(async (opts) => (opts ? { state: STATE, previews: {} } : STATE))
  store.fetchSessions = vi.fn(async () => [])
  store.fetchAsks = vi.fn(async () => [])
  store.seedAgentRecency = vi.fn()
  store.ensureBriefing = vi.fn()
  store.loadAgentSuggestions = vi.fn()
  store.markChatRead = vi.fn(async () => {})
  store.markChatReadStrict = vi.fn(async () => {})
}

async function boot(path) {
  const router = makeRouter()
  await router.push(path)
  await router.isReady()
  const w = shallowMount(Portal, { global: { plugins: [router] } })
  await flushPromises(); await flushPromises()
  return { w, router }
}

beforeEach(() => {
  localStorage.clear()
  setActivePinia(createPinia())
})

describe('the shell hands one room count to the sidebar and the Inbox (AC 4)', () => {
  it('the pinned total and the Inbox rows agree, with a room in them', async () => {
    arm()
    const { w } = await boot('/workspace/inbox')
    const inbox = w.findComponent({ name: 'PortalInbox' })
    const sidebar = w.findAllComponents({ name: 'PortalSidebar' }).at(0)
    const inboxThreads = inbox.props('threads')
    expect(sidebar.props('threads')).toEqual(inboxThreads)
    expect(inboxThreads.find((t) => t.id === 'room_1').unread).toBe(3)
    const rows = unreadItems(inboxThreads, {})
    expect(rows.map((r) => r.key)).toEqual(['room:room_1', 'thread:t1'])
    expect(rows.reduce((a, r) => a + r.n, 0)).toBe(inboxCounts(inboxThreads, []).came)
    expect(inboxCounts(inboxThreads, []).came).toBe(4)
  })
})

describe('the room on stage reads itself', () => {
  it("PortalRoom's read is the shell's markRead('room', id)", async () => {
    arm()
    const { w } = await boot('/workspace/r/room_1')
    const room = w.findComponent({ name: 'PortalRoom' })
    expect(room.exists()).toBe(true)
    store.markChatReadStrict.mockClear()
    room.vm.$emit('read', 'room_1')
    await flushPromises()
    expect(store.markChatReadStrict).toHaveBeenCalledWith('room', 'room_1')
  })
})

describe('opening a room from the sidebar does not read it ahead of the room (Codex code review #2)', () => {
  it('no room read is written by the open itself — only PortalRoom\'s complete load reads it', async () => {
    arm()
    const { w, router } = await boot('/workspace')
    store.markChatRead.mockClear()
    store.markChatReadStrict.mockClear()
    const sidebar = w.findAllComponents({ name: 'PortalSidebar' }).at(0)
    sidebar.vm.$emit('open-thread', LIST[1])               // the room row
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/workspace/r/room_1')
    // PortalRoom is stubbed here, so it never loads: nothing may have read it.
    const roomReads = [...store.markChatRead.mock.calls, ...store.markChatReadStrict.mock.calls]
      .filter(([kind]) => kind === 'room')
    expect(roomReads).toEqual([])
  })
})
