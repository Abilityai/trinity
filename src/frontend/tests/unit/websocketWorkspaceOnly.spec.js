// @vitest-environment jsdom
/**
 * trinity-enterprise#837 — `/ws` delivers a Workspace-only member the events of
 * the agents they can see (ent#467), and `utils/websocket.js` used to answer
 * several of them by refetching OPERATOR stores: the agent list, the
 * notification count, the operator queue, the executions table. Each of those
 * 403s for a `user`, on every ask raised. For that role the operator stores do
 * not follow the stream; the Workspace stores still do.
 *
 * Driven through the real `connect()` with a fake socket, asserting on the
 * requests that actually go out.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

const http = vi.hoisted(() => ({ get: null, post: null }))

vi.mock('axios', () => {
  const instance = {
    get: (...args) => http.get(...args),
    post: (...args) => http.post(...args),
    put: vi.fn(async () => ({ data: {} })),
    delete: vi.fn(async () => ({ data: {} })),
    defaults: { headers: { common: {} } },
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
  }
  return { default: { ...instance, create: vi.fn(() => instance) } }
})

import { useWebSocket, reconnectWebSocket } from '../../src/utils/websocket'
import { useAuthStore } from '../../src/stores/auth'
import { useLoopsStore } from '../../src/stores/loops'
import { useSkillsStore } from '../../src/stores/skills'

class FakeSocket {
  static last = null
  constructor(url) { this.url = url; FakeSocket.last = this; this.closed = null }
  close(code, reason) { this.closed = { code, reason } }
  send() {}
}

const OPERATOR_EVENTS = [
  { type: 'resync_required', reason: 'trimmed' },
  { event: 'agent_notification', notification_id: 'n1', agent_name: 'scout', title: 'Heads up' },
  { type: 'operator_queue_new', data: { id: 'q1', agent_name: 'scout' } },
  { type: 'notifications_cleared' },
  { type: 'agent_activity', activity_type: 'schedule_start', agent_name: 'scout' },
]
const OPERATOR_READS = ['/api/agents', '/api/notifications', '/api/operator-queue', '/api/executions']

async function streamAs(role) {
  const auth = useAuthStore()
  auth.isAuthenticated = true
  auth.token = 'jwt'
  auth.user = { email: 'x@example.com', role }
  const socket = useWebSocket()
  await socket.connect()
  const sock = FakeSocket.last
  http.get.mockClear()
  for (const event of OPERATOR_EVENTS) sock.onmessage({ data: JSON.stringify(event) })
  await new Promise((r) => setTimeout(r, 0))
  const reads = http.get.mock.calls.map(([url]) => String(url))
  socket.disconnect()
  return reads
}

describe('/ws events and the Workspace-only rung', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    http.get = vi.fn(async () => ({ data: [] }))
    http.post = vi.fn(async (url) => (url === '/api/ws/ticket' ? { data: { ticket: 't' } } : { data: {} }))
    globalThis.WebSocket = FakeSocket
    localStorage.setItem('token', 'jwt')
  })
  afterEach(() => {
    localStorage.clear()
    vi.restoreAllMocks()
  })

  it('a member’s stream refetches no operator store', async () => {
    const reads = await streamAs('user')
    for (const prefix of OPERATOR_READS) {
      expect(reads.filter((u) => u.startsWith(prefix))).toEqual([])
    }
  })

  it('an operator’s stream still does', async () => {
    const reads = await streamAs('operator')
    expect(reads.some((u) => u.startsWith('/api/operator-queue'))).toBe(true)
    expect(reads.some((u) => u.startsWith('/api/notifications'))).toBe(true)
  })

  // Review nit: the loop and skills events feed an operator store too
  // (Agent Detail's), which must not follow a member's stream either.
  async function storeHitsAs(role) {
    const auth = useAuthStore()
    auth.isAuthenticated = true
    auth.token = 'jwt'
    auth.user = { email: 'x@example.com', role }
    const loops = vi.spyOn(useLoopsStore(), 'handleWebSocketEvent')
    const skills = vi.spyOn(useSkillsStore(), 'noteSkillsChanged')
    const socket = useWebSocket()
    await socket.connect()
    FakeSocket.last.onmessage({ data: JSON.stringify({ type: 'loop_run_completed', agent_name: 'scout', loop_id: 'l1' }) })
    FakeSocket.last.onmessage({ data: JSON.stringify({ type: 'agent_skills_changed', agent_name: 'scout' }) })
    socket.disconnect()
    return { loops: loops.mock.calls.length, skills: skills.mock.calls.length }
  }

  it('a member’s loop and skills events reach no operator store', async () => {
    expect(await storeHitsAs('user')).toEqual({ loops: 0, skills: 0 })
  })

  it('an operator’s loop and skills events still do', async () => {
    expect(await storeHitsAs('operator')).toEqual({ loops: 1, skills: 1 })
  })

  // trinity-enterprise#837 review: a socket's scope is fixed when it connects.
  it('reconnectWebSocket drops the open socket so it reconnects with a fresh scope', async () => {
    const auth = useAuthStore()
    auth.isAuthenticated = true
    auth.token = 'jwt'
    auth.user = { email: 'x@example.com', role: 'user' }
    const socket = useWebSocket()
    await socket.connect()
    const sock = FakeSocket.last
    reconnectWebSocket()
    expect(sock.closed).not.toBeNull()
    expect(sock.closed.code).not.toBe(4001)  // 4001 means "do not reconnect"
    socket.disconnect()
  })
})
