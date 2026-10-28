// @vitest-environment jsdom
/**
 * #3445 — the Dashboard's own `/ws` socket follows the platform session too.
 *
 * The SPA holds two `/ws` connections: the app-wide one (`utils/websocket.js`,
 * which now follows `authStore.token` from `App.vue`) and the Dashboard's
 * (`stores/network.js`, opened on mount, closed on unmount). The server fixes a
 * socket's scope when it connects (ent#467), and the Dashboard's socket read
 * the token only then: a Dashboard left mounted while the session ended in
 * another tab kept the earlier session's socket, and kept it under whoever
 * signed in next. Found by the /review of #4069.
 *
 * Mounted (`Dashboard.vue`, shallow), with the real auth store doing the
 * sign-ins and sign-outs and a fake `WebSocket` standing in for the browser's.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { shallowMount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'

vi.hoisted(() => {
  window.matchMedia = window.matchMedia || ((q) => ({
    matches: false, media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
  }))
})
const { axiosMock, http } = vi.hoisted(() => ({
  axiosMock: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), patch: vi.fn() },
  http: { tickets: 0, ticket: null },
}))
vi.mock('axios', () => ({
  default: Object.assign(axiosMock, {
    create: () => ({ ...axiosMock, interceptors: { request: { use() {} }, response: { use() {} } }, defaults: { headers: { common: {} } } }),
    interceptors: { request: { use() {} }, response: { use() {} } },
    defaults: { headers: { common: {} } },
  }),
}))

import Dashboard from '@/views/Dashboard.vue'
import { useAuthStore } from '@/stores/auth'
import { useNetworkStore } from '@/stores/network'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

class FakeSocket {
  static OPEN = 1
  static all = []
  constructor(url) { this.url = url; this.readyState = 0; this.closed = null; FakeSocket.all.push(this) }
  close(code, reason) { this.closed = { code, reason }; this.readyState = 3 }
  send() {}
}
const last = () => FakeSocket.all[FakeSocket.all.length - 1]
const open = () => FakeSocket.all.filter((s) => !s.closed)

let wrapper
async function dashboardSignedInAs(token) {
  localStorage.setItem('token', token)
  localStorage.setItem('auth0_user', JSON.stringify({ email: 'op@example.com', role: 'admin' }))
  const auth = useAuthStore()
  Object.assign(auth, {
    isLoading: false, isAuthenticated: true, token, profileVerified: true,
    user: { email: 'op@example.com', role: 'admin' },
  })
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { template: '<div/>' } }, { path: '/:p(.*)*', component: { template: '<div/>' } }] })
  await router.push('/'); await router.isReady()
  wrapper = shallowMount(Dashboard, { global: { plugins: [router] } })
  await flushPromises()
  return auth
}

async function signInAs(auth, token) {
  await auth._finalizeLogin(token, { username: 'next', email: 'next@example.com' })
  await flushPromises()
}

beforeEach(() => {
  setActivePinia(createPinia())
  localStorage.clear()
  sessionStorage.clear()
  FakeSocket.all = []
  globalThis.WebSocket = FakeSocket
  http.tickets = 0
  http.ticket = null
  for (const verb of ['get', 'post', 'put', 'delete', 'patch']) axiosMock[verb].mockReset()
  axiosMock.get.mockImplementation(async () => ({ data: [] }))
  axiosMock.put.mockImplementation(async () => ({ data: {} }))
  axiosMock.delete.mockImplementation(async () => ({ data: {} }))
  axiosMock.patch.mockImplementation(async () => ({ data: {} }))
  axiosMock.post.mockImplementation(async (url) => {
    if (url !== '/api/ws/ticket') return { data: {} }
    http.tickets += 1
    return http.ticket ? http.ticket() : { data: { ticket: `t${http.tickets}` } }
  })
})
afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('#3445 — the Dashboard socket follows the platform session', () => {
  it('opens on mount, as before', async () => {
    await dashboardSignedInAs('A')
    expect(FakeSocket.all).toHaveLength(1)
    expect(last().url).toContain('ticket=t1')
  })

  it('a sign-out closes it, and nothing reopens it', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    const auth = await dashboardSignedInAs('A')
    const sock = last()
    sock.onopen()
    expect(useNetworkStore().isConnected).toBe(true)

    auth.logout()
    await flushPromises()
    expect(sock.closed).not.toBeNull()
    expect(useNetworkStore().isConnected).toBe(false)

    sock.onclose({ code: 1000 })                           // the browser's close event, afterwards
    vi.advanceTimersByTime(15_000)
    await flushPromises()
    expect(FakeSocket.all).toHaveLength(1)
    expect(http.tickets).toBe(1)
  })

  it('a sign-out in another tab closes it too', async () => {
    const auth = await dashboardSignedInAs('A')
    const sock = last()
    auth.applySessionEndedElsewhere()
    await flushPromises()
    expect(sock.closed).not.toBeNull()
    expect(open()).toHaveLength(0)
  })

  it('whoever signs in next gets a socket of their own: fresh ticket, no replay cursor', async () => {
    const auth = await dashboardSignedInAs('A')
    const first = last()
    first.onopen()
    first.onmessage({ data: JSON.stringify({ _eid: '1700000000000-3', type: 'pong' }) })

    auth.applySessionEndedElsewhere()
    await flushPromises()
    await signInAs(auth, 'B')

    expect(first.closed).not.toBeNull()
    expect(FakeSocket.all).toHaveLength(2)
    expect(last().url).toContain('ticket=t2')
    expect(last().url).not.toContain('last-event-id')
    expect(open()).toEqual([last()])
  })

  it('a new token in another tab reconnects it', async () => {
    const auth = await dashboardSignedInAs('A')
    const first = last()
    localStorage.setItem('token', 'B')
    auth.adoptStoredSession()
    await flushPromises()
    expect(first.closed).not.toBeNull()
    expect(FakeSocket.all).toHaveLength(2)
    expect(open()).toEqual([last()])
  })

  it('a late close from the old socket does not knock the new one over', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    const auth = await dashboardSignedInAs('A')
    const first = last()
    first.onopen()
    localStorage.setItem('token', 'B')
    auth.adoptStoredSession()
    await flushPromises()
    const second = last()
    second.onopen()

    first.onclose({ code: 1006 })
    first.onerror(new Event('error'))
    expect(useNetworkStore().isConnected).toBe(true)
    vi.advanceTimersByTime(15_000)
    await flushPromises()
    expect(FakeSocket.all).toHaveLength(2)
  })

  it('a ticket minted for the session that ended opens no socket', async () => {
    let release
    http.ticket = () => new Promise((resolve) => { release = () => resolve({ data: { ticket: 'late' } }) })
    const auth = await dashboardSignedInAs('A')
    expect(http.tickets).toBe(1)
    auth.logout()
    await flushPromises()
    release()
    await flushPromises()
    expect(FakeSocket.all).toHaveLength(0)
  })

  it('a late open from the old socket does not light the Dashboard dot', async () => {
    const auth = await dashboardSignedInAs('A')
    const first = last()
    localStorage.setItem('token', 'B')
    auth.adoptStoredSession()
    await flushPromises()
    first.onopen()
    expect(useNetworkStore().isConnected).toBe(false)
  })

  it('a late message from the old socket is neither handled nor kept as the replay cursor', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    const auth = await dashboardSignedInAs('A')
    const first = last()
    localStorage.setItem('token', 'B')
    auth.adoptStoredSession()
    await flushPromises()
    const second = last()
    second.onopen()
    const handled = vi.spyOn(useNetworkStore(), 'handleActivityStatusChange')
    first.onmessage({ data: JSON.stringify({ _eid: '1700000000000-9', type: 'agent_activity', agent_name: 'scout', activity_state: 'started' }) })
    expect(handled).not.toHaveBeenCalled()

    second.readyState = 3                                  // a browser marks it CLOSED before the event
    second.onclose({ code: 1006 })                         // the new socket drops; it reconnects by itself
    vi.advanceTimersByTime(5_000)
    await flushPromises()
    expect(FakeSocket.all).toHaveLength(3)
    expect(last().url).not.toContain('last-event-id')
  })

  it('a token change while the first ticket is being minted gets the new session its own socket', async () => {
    let release
    http.ticket = () => (http.tickets === 1
      ? new Promise((resolve) => { release = () => resolve({ data: { ticket: 'for-A' } }) })
      : { data: { ticket: 'for-B' } })
    const auth = await dashboardSignedInAs('A')
    localStorage.setItem('token', 'B')
    auth.adoptStoredSession()
    await flushPromises()
    release()
    await flushPromises()
    expect(http.tickets).toBe(2)
    expect(FakeSocket.all.map((s) => s.url.match(/ticket=([^&]+)/)[1])).toEqual(['for-B'])
  })

  it('a second connect while one is minting or connecting opens nothing more', async () => {
    let release
    http.ticket = () => new Promise((resolve) => { release = () => resolve({ data: { ticket: 'only' } }) })
    localStorage.setItem('token', 'A')
    const store = useNetworkStore()
    store.connectWebSocket()
    store.connectWebSocket()                               // while the ticket is minted
    await flushPromises()
    expect(http.tickets).toBe(1)
    release()
    await flushPromises()
    store.connectWebSocket()                               // while the socket is still connecting
    await flushPromises()
    expect(http.tickets).toBe(1)
    expect(FakeSocket.all).toHaveLength(1)
    store.disconnectWebSocket()
  })

  it('leaving the Dashboard still closes it', async () => {
    await dashboardSignedInAs('A')
    const sock = last()
    wrapper.unmount()
    wrapper = null
    expect(sock.closed).not.toBeNull()
  })
})
