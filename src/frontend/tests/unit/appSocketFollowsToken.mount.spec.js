// @vitest-environment jsdom
/**
 * #3445 — the app-wide `/ws` socket follows the platform session, not the page load.
 *
 * `App.vue` opened the socket once, on mount, and only if a token was already
 * stored; nothing ever closed it. So a client-side login (which never remounts
 * `App.vue`) left the header dot on Disconnected and live updates off until a
 * reload — and, the other half of the same cause, a logout left the previous
 * session's socket open, still carrying the scope the server fixed for it at
 * connect. A sign-in in the same tab then rode that socket.
 *
 * Now `App.vue` watches the store's token: a token appearing connects, a
 * changed token reconnects (fresh ticket, no replay cursor), and the token
 * going away disconnects — deliberately, with no 5-second reconnect behind it.
 * The decision is the pure `socketActionFor`.
 *
 * Mounted (`App.vue` on a memory router), with the real auth store doing the
 * sign-ins and sign-outs and a fake `WebSocket` standing in for the browser's.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'

vi.hoisted(() => {
  window.matchMedia = window.matchMedia || ((q) => ({
    matches: false, media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
  }))
})

const http = vi.hoisted(() => ({ tickets: 0, ticket: null }))

vi.mock('axios', () => {
  const instance = {
    get: vi.fn(async () => ({ data: {} })),
    post: vi.fn(async (url) => {
      if (url !== '/api/ws/ticket') return { data: {} }
      http.tickets += 1
      return http.ticket ? http.ticket() : { data: { ticket: `t${http.tickets}` } }
    }),
    put: vi.fn(async () => ({ data: {} })),
    delete: vi.fn(async () => ({ data: {} })),
    defaults: { headers: { common: {} } },
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
  }
  return { default: { ...instance, create: vi.fn(() => instance) } }
})
vi.mock('../../src/components/HelpChatWidget.vue', () => ({ default: { render: () => null } }))

import App from '../../src/App.vue'
import { useAuthStore } from '../../src/stores/auth'
import { useWebSocket, reconnectWebSocket, socketActionFor } from '../../src/utils/websocket'

class FakeSocket {
  static all = []
  constructor(url) { this.url = url; this.closed = null; FakeSocket.all.push(this) }
  close(code, reason) { this.closed = { code, reason } }
  send() {}
}
const last = () => FakeSocket.all[FakeSocket.all.length - 1]
const open = () => FakeSocket.all.filter((s) => !s.closed)

let wrapper
async function mountApp() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/', component: { template: '<div />' } }],
  })
  await router.push('/')
  await router.isReady()
  wrapper = mount(App, { global: { plugins: [router] } })
  await flushPromises()
  return useAuthStore()
}

async function signIn(auth, token) {
  await auth._finalizeLogin(token, { username: 'op', email: 'op@example.com' })
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
})
afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  useWebSocket().disconnect()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('socketActionFor — what a token change means for the socket', () => {
  it.each([
    [null, null, 'none'],
    [null, 'A', 'connect'],
    ['A', 'A', 'none'],
    ['A', 'B', 'reconnect'],
    ['A', null, 'disconnect'],
  ])('socket on %s, session on %s → %s', (socketToken, token, action) => {
    expect(socketActionFor({ socketToken, token })).toBe(action)
  })
})

describe('#3445 — the socket follows the platform token', () => {
  it('a client-side login connects, and the header dot reads Connected', async () => {
    const auth = await mountApp()
    const { isConnected } = useWebSocket()
    expect(FakeSocket.all).toHaveLength(0)
    expect(isConnected.value).toBe(false)

    await signIn(auth, 'A')
    expect(FakeSocket.all).toHaveLength(1)
    expect(last().url).toContain('ticket=t1')
    last().onopen()
    expect(isConnected.value).toBe(true)
  })

  it('a page loaded with a stored session connects, as before', async () => {
    localStorage.setItem('token', 'A')
    await mountApp()
    expect(FakeSocket.all).toHaveLength(1)
  })

  it('a logout closes the socket and nothing reopens it', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    const auth = await mountApp()
    await signIn(auth, 'A')
    const sock = last()
    sock.onopen()

    auth.logout()
    await flushPromises()
    expect(sock.closed).not.toBeNull()
    expect(useWebSocket().isConnected.value).toBe(false)

    // The browser delivers the close event afterwards; it is the socket we let go.
    sock.onclose({ code: 1000 })
    vi.advanceTimersByTime(15_000)
    await flushPromises()
    expect(FakeSocket.all).toHaveLength(1)
    expect(http.tickets).toBe(1)
  })

  it('a sign-out in another tab closes it too', async () => {
    const auth = await mountApp()
    await signIn(auth, 'A')
    const sock = last()
    auth.applySessionEndedElsewhere()
    await flushPromises()
    expect(sock.closed).not.toBeNull()
    expect(open()).toHaveLength(0)
  })

  it('a new token reconnects with a fresh ticket and no replay cursor from the old session', async () => {
    const auth = await mountApp()
    await signIn(auth, 'A')
    const first = last()
    first.onopen()
    first.onmessage({ data: JSON.stringify({ _eid: '1700000000000-0', type: 'noop' }) })

    localStorage.setItem('token', 'B')                     // a sibling tab signed in as someone else
    auth.adoptStoredSession()
    await flushPromises()
    expect(first.closed).not.toBeNull()
    expect(FakeSocket.all).toHaveLength(2)
    expect(last().url).toContain('ticket=t2')
    expect(last().url).not.toContain('last-event-id')
    expect(open()).toEqual([last()])
  })

  it("a late close from the old socket does not touch the new one", async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    const auth = await mountApp()
    await signIn(auth, 'A')
    const first = last()
    first.onopen()
    localStorage.setItem('token', 'B')
    auth.adoptStoredSession()
    await flushPromises()
    const second = last()
    second.onopen()

    first.onclose({ code: 1006 })
    expect(useWebSocket().isConnected.value).toBe(true)
    vi.advanceTimersByTime(15_000)
    await flushPromises()
    expect(FakeSocket.all).toHaveLength(2)
  })

  it('a ticket that arrives after the session ended opens no socket', async () => {
    let release
    http.ticket = () => new Promise((resolve) => { release = () => resolve({ data: { ticket: 'late' } }) })
    const auth = await mountApp()
    const login = signIn(auth, 'A')
    await flushPromises()
    expect(http.tickets).toBe(1)

    auth.logout()
    await flushPromises()
    release()
    await login
    await flushPromises()
    expect(FakeSocket.all).toHaveLength(0)
  })
})

describe('what still reconnects on its own', () => {
  it('a dropped connection retries after 5 s', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    const auth = await mountApp()
    await signIn(auth, 'A')
    last().onclose({ code: 1006 })
    vi.advanceTimersByTime(5_000)
    await flushPromises()
    expect(FakeSocket.all).toHaveLength(2)
  })

  it("trinity-enterprise#837's rescope (close 4000) still reconnects", async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    const auth = await mountApp()
    await signIn(auth, 'A')
    const sock = last()
    reconnectWebSocket()
    expect(sock.closed.code).toBe(4000)
    sock.onclose({ code: 4000 })
    vi.advanceTimersByTime(5_000)
    await flushPromises()
    expect(FakeSocket.all).toHaveLength(2)
  })

  it('an auth rejection (4001) does not', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    const auth = await mountApp()
    await signIn(auth, 'A')
    last().onclose({ code: 4001 })
    vi.advanceTimersByTime(15_000)
    await flushPromises()
    expect(FakeSocket.all).toHaveLength(1)
  })
})
