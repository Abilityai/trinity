// @vitest-environment jsdom
/**
 * #3109 — the dashboard store hears agents being created and deleted.
 *
 * `stores/network.js` (what the Dashboard and Timeline read) dispatches on
 * `data.type`. It had no `agent_created` branch at all, and the backend sent
 * that event — and `agent_deleted` — with `event` but no `type`, so even the
 * existing delete branch could never fire (and it read `agent_name` off the
 * envelope, where the name is not). A new agent therefore appeared only on the
 * 30 s poll.
 *
 * Fed the backend's real envelope shape through the store's own WebSocket.
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

import { useNetworkStore, AGENT_CREATED_COALESCE_MS } from '@/stores/network'

let socket
class FakeWebSocket {
  static OPEN = 1
  constructor() { socket = this; this.readyState = 0 }
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
  globalThis.WebSocket = FakeWebSocket
  fleet = [{ name: 'alpha', status: 'running' }]
  axiosMock.get.mockReset(); axiosMock.post.mockReset()
  axiosMock.post.mockResolvedValue({ data: { ticket: 't' } })
  axiosMock.get.mockImplementation(async (url) => (url === '/api/agents' ? { data: fleet.map((a) => ({ ...a })) } : { data: [] }))
})
afterEach(() => { vi.useRealTimers() })

async function connected() {
  const store = useNetworkStore()
  await store.connectWebSocket()
  expect(socket).toBeTruthy()
  return store
}
const push = (msg) => socket.onmessage({ data: JSON.stringify(msg) })

describe('#3109 — agent_created reaches the dashboard store', () => {
  it('refetches the access-controlled list, once per burst', async () => {
    const store = await connected()
    const before = agentListReads()
    fleet = [...fleet, { name: 'b' }, { name: 'c' }, { name: 'd' }, { name: 'e' }]
    // Setup seeds four agents: four events in a row.
    for (const name of ['b', 'c', 'd', 'e']) {
      push({ event: 'agent_created', type: 'agent_created', data: { name, status: 'running' } })
    }
    expect(agentListReads()).toBe(before)                 // gathered, not yet fetched
    vi.advanceTimersByTime(AGENT_CREATED_COALESCE_MS)
    await flushPromises()
    expect(agentListReads()).toBe(before + 1)             // ONE refetch for the burst
    expect(store.agents.map((a) => a.name)).toEqual(['alpha', 'b', 'c', 'd', 'e'])
  })

  it('never inserts the broadcast payload itself — the REST list decides (#918 thin trigger)', async () => {
    const store = await connected()
    push({ event: 'agent_created', type: 'agent_created', data: { name: 'not-yours', status: 'running' } })
    vi.advanceTimersByTime(AGENT_CREATED_COALESCE_MS)
    await flushPromises()
    // The REST list (access-controlled, tag-filtered) does not include it, so neither does the store.
    expect(store.agents.map((a) => a.name)).toEqual(['alpha'])
  })
})

describe('#3109 — agent_deleted is no longer a dead branch', () => {
  it('removes the agent named in data.data', async () => {
    const store = await connected()
    await store.fetchAgents()
    expect(store.agents.map((a) => a.name)).toEqual(['alpha'])
    push({ event: 'agent_deleted', type: 'agent_deleted', data: { name: 'alpha' } })
    expect(store.agents).toEqual([])
  })
})
