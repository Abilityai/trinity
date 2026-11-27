// @vitest-environment jsdom
/**
 * trinity-enterprise#1416 — a change made before the dashboard's socket opens
 * is not lost.
 *
 * The Dashboard reads the agent list over REST, THEN opens the collaboration
 * socket. A broadcast sent in between (a tag edit — `agent_tags_changed` — is
 * a thin trigger, the row is only re-read when it arrives) reached nobody, so
 * the change stayed off screen until a later full read. With the two seeded
 * core agents, the mount reads take long enough that a tag written right after
 * page load landed in that gap every time (the `grid-org-overlay` smoke).
 *
 * A socket that opens with no replay cursor (`last-event-id`) cannot be given
 * what it missed, so it re-reads the list once. A reconnect that carries a
 * cursor is replayed by the server and does not re-read.
 *
 * Driven through the store's own WebSocket and `fetchAgents`.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { flushPromises } from '@vue/test-utils'

const { axiosMock } = vi.hoisted(() => ({
  axiosMock: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}))
vi.mock('axios', () => ({
  default: Object.assign(axiosMock, {
    create: () => ({ ...axiosMock, interceptors: { request: { use() {} }, response: { use() {} } }, defaults: { headers: { common: {} } } }),
    interceptors: { request: { use() {} }, response: { use() {} } },
    defaults: { headers: { common: {} } },
  }),
}))

import { useNetworkStore } from '@/stores/network'

let sockets
class FakeWebSocket {
  static OPEN = 1
  constructor(url) { this.url = url; this.readyState = 0; sockets.push(this) }
  send() {}
  close() {}
}

let fleet
const agentListReads = () => axiosMock.get.mock.calls.filter(([url]) => url === '/api/agents').length

beforeEach(() => {
  vi.useFakeTimers()
  setActivePinia(createPinia())
  localStorage.clear()
  localStorage.setItem('token', 'tok')
  sockets = []
  globalThis.WebSocket = FakeWebSocket
  fleet = [{ name: 'trinity-system', is_system: true, tags: [] }]
  axiosMock.get.mockReset(); axiosMock.post.mockReset()
  axiosMock.post.mockResolvedValue({ data: { ticket: 't' } })
  axiosMock.get.mockImplementation(async (url) => (url === '/api/agents' ? { data: fleet.map((a) => ({ ...a })) } : { data: [] }))
})
afterEach(() => { vi.useRealTimers() })

function open(sock) {
  sock.readyState = 1
  sock.onopen()
}

describe('ent#1416 — the socket catches up on what changed before it opened', () => {
  it('a tag written between the list read and the socket opening shows up', async () => {
    const store = useNetworkStore()
    await store.fetchAgents()                      // the Dashboard's mount read
    await store.connectWebSocket()                 // then the socket
    // The tag lands now — its broadcast goes to a socket that is not open yet.
    fleet = [{ name: 'trinity-system', is_system: true, tags: ['dept-e2e-platform'] }]
    open(sockets[0])
    await flushPromises()
    expect(store.agents[0].tags).toEqual(['dept-e2e-platform'])
  })

  it('re-reads once per open, and not at all when the server can replay', async () => {
    const store = useNetworkStore()
    await store.fetchAgents()
    await store.connectWebSocket()
    const before = agentListReads()
    open(sockets[0])
    await flushPromises()
    expect(agentListReads()).toBe(before + 1)

    // An event gives the store a cursor; the reconnect carries it and the
    // server replays the gap, so no second read.
    sockets[0].onmessage({ data: JSON.stringify({ _eid: '1-0', type: 'noop' }) })
    sockets[0].readyState = 3
    sockets[0].onclose({ code: 1006 })
    vi.advanceTimersByTime(5000)
    await flushPromises()
    expect(sockets).toHaveLength(2)
    expect(sockets[1].url).toContain('last-event-id=')
    const afterFirst = agentListReads()
    open(sockets[1])
    await flushPromises()
    expect(agentListReads()).toBe(afterFirst)
  })

  it('does not read the list for a socket opened before anything was read', async () => {
    const store = useNetworkStore()
    await store.connectWebSocket()
    open(sockets[0])
    await flushPromises()
    expect(agentListReads()).toBe(0)
  })
})
