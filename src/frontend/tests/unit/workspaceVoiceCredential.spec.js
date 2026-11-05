/**
 * trinity-enterprise#595 — the Workspace call's socket credential comes from
 * the start request's OWN credential decision, never from the platform token.
 *
 * #2261 made `clientPortal.authHeader` + the `portalHttp` request interceptor
 * the single decision about which credential a Workspace request carries (a
 * portal session wins; a suppressed platform fallback sends nothing). The voice
 * socket used to step around it and read the platform token for its URL. Now
 * the start response carries a single-use ticket, so whatever authorized the
 * start is all that authorizes the socket.
 *
 * Real Pinia store, real `portalHttp` with its real interceptor, real axios —
 * only the transport adapter is a recorder, and the platform session is a
 * sentinel that must reach no socket. The real composable drives the call.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { FakeVoiceSocket, flush } from './helpers/fakeVoiceSocket'

vi.hoisted(() => {
  const mk = () => {
    const m = new Map()
    return {
      getItem: (k) => (m.has(k) ? m.get(k) : null),
      setItem: (k, v) => m.set(k, String(v)),
      removeItem: (k) => m.delete(k),
      clear: () => m.clear(),
    }
  }
  globalThis.localStorage = mk()
  globalThis.sessionStorage = mk()
})

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({
    token: 'PLATFORM-JWT-SENTINEL',
    isAuthenticated: true,
    authHeader: { Authorization: 'Bearer PLATFORM-JWT-SENTINEL' },
  }),
}))

vi.mock('@/utils/audio', () => ({
  createAudioPlayer: () => ({ play() {}, stop() {}, getAmplitude: () => 0 }),
  startMicCapture: async () => ({ stop() {} }),
}))

import { useClientPortalStore, portalHttp } from '@/stores/clientPortal'
import { useVoiceSession } from '@/composables/useVoiceSession'

const PLATFORM = 'PLATFORM-JWT-SENTINEL'
const PORTAL = 'PORTAL-SESSION-SENTINEL'
const TICKET = 'q'.repeat(43)
const SID = 'vs_wire_595'

let wire
let answer
const realAdapter = portalHttp.defaults.adapter

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  setActivePinia(createPinia())
  FakeVoiceSocket.reset()
  vi.stubGlobal('WebSocket', FakeVoiceSocket)
  vi.stubGlobal('window', { location: { protocol: 'https:', host: 'app.example.com' }, isSecureContext: true })
  vi.stubGlobal('navigator', { mediaDevices: { getUserMedia: () => {} } })
  wire = []
  answer = { status: 200, data: { voice_session_id: SID, websocket_url: `/ws/voice/${SID}`,
    portal_session_id: 'thread-1', max_duration_seconds: 1800,
    websocket_ticket: TICKET, websocket_ticket_expires_in: 30 } }
  portalHttp.defaults.adapter = async (config) => {
    const headers = config.headers?.toJSON ? config.headers.toJSON() : { ...config.headers }
    wire.push({ url: config.url, authorization: headers.Authorization ?? headers.authorization ?? null })
    const response = { data: answer.data, status: answer.status, statusText: String(answer.status),
      headers: {}, config, request: {} }
    if (answer.status >= 400) {
      const err = new Error(`Request failed with status code ${answer.status}`)
      err.response = response
      err.config = config
      throw err
    }
    return response
  }
})

afterEach(() => {
  portalHttp.defaults.adapter = realAdapter
  vi.unstubAllGlobals()
})

async function call() {
  const store = useClientPortalStore()
  const voice = useVoiceSession('scribe')
  const started = voice.startWith(() => store.startWorkspaceVoice('scribe', 'thread-1'), { restStop: false })
  await flush(20)
  return { store, voice, started }
}

function everythingTheSocketCarried() {
  return JSON.stringify(FakeVoiceSocket.instances.map((s) => ({ url: s.url, sent: s.sent })))
}

describe('the Workspace voice socket is authorized by the start request, not the platform token', () => {
  it('a platform session: the start carries the platform bearer; the socket carries only the response ticket', async () => {
    const { voice, started } = await call()
    expect(wire).toEqual([{ url: '/api/enterprise/client-portal/agents/scribe/voice/start',
      authorization: `Bearer ${PLATFORM}` }])
    const sock = FakeVoiceSocket.instances[0]
    expect(sock.url).toBe(`wss://app.example.com/ws/voice/${SID}`)
    sock.open()
    expect(sock.sent).toEqual([{ type: 'auth', ticket: TICKET }])
    expect(everythingTheSocketCarried()).not.toContain(PLATFORM)
    sock.serverSends({ type: 'auth_ok' })
    expect(await started).toBe(true)
    expect(voice.isActive.value).toBe(true)
  })

  it('a portal session wins the start request, and its refused start opens no socket', async () => {
    useClientPortalStore().portalToken = PORTAL
    answer = { status: 404, data: { detail: 'Conversation not found' } }
    const { voice, started } = await call()
    expect(wire.map((w) => w.authorization)).toEqual([`Bearer ${PORTAL}`])
    expect(await started).toBe(false)
    expect(voice.error.value).toBe('Conversation not found')
    expect(FakeVoiceSocket.instances).toEqual([])
  })

  it('a suppressed platform fallback sends no credential, and its refused start opens no socket', async () => {
    useClientPortalStore().platformFallbackSuppressed = true
    answer = { status: 401, data: { detail: 'Could not validate credentials' } }
    const { started } = await call()
    expect(wire.map((w) => w.authorization)).toEqual([null])
    expect(await started).toBe(false)
    expect(FakeVoiceSocket.instances).toEqual([])
    expect(JSON.stringify(wire)).not.toContain(PLATFORM)
  })
})
