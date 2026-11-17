/**
 * trinity-enterprise#1046 — the store↔backend contract of a Direct call.
 *
 * Each side's wiring was pinned on its own, but not the HTTP seam between them:
 * dropping `mode` from the start body silently started a Live call, dropping
 * `voice_call_id` untied spoken turns from their call, and dropping the roster's
 * `direct_voice` parse left the Direct button permanently off — all with every
 * test green (merge-train 2026-11-02). These drive the real store actions over a
 * faked axios and assert the wire shape the backend models read.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

vi.hoisted(() => {
  const store = new Map()
  globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    clear: () => store.clear(),
  }
  globalThis.window = globalThis.window || { location: { pathname: '/workspace' } }
})

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: false, authHeader: {} }),
}))

vi.mock('axios', () => {
  const inst = {
    get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  }
  return { default: Object.assign(inst, { create: () => inst }) }
})

import axios from 'axios'
import { useClientPortalStore } from '@/stores/clientPortal'

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

const lastBody = () => axios.post.mock.calls.at(-1)[1]

describe('ent#1046 Direct call: the wire contract', () => {
  it('a Direct start sends mode=direct (the backend reads body.mode)', async () => {
    const store = useClientPortalStore()
    store.portalToken = 't'
    axios.post.mockResolvedValueOnce({ data: { session_id: 'vd_x' } })
    await store.startWorkspaceVoice('scout', 'ps1', null, 'direct')
    expect(axios.post.mock.calls.at(-1)[0]).toMatch(/\/agents\/scout\/voice\/start$/)
    expect(lastBody()).toMatchObject({ portal_session_id: 'ps1', mode: 'direct' })
  })

  it('a start with no mode is a Live call', async () => {
    const store = useClientPortalStore()
    store.portalToken = 't'
    axios.post.mockResolvedValueOnce({ data: {} })
    await store.startWorkspaceVoice('scout', 'ps1')
    expect(lastBody().mode).toBe('live')
  })

  it('a spoken turn carries its call id as voice_call_id', async () => {
    const store = useClientPortalStore()
    store.portalToken = 't'
    axios.post.mockResolvedValueOnce({ data: { execution_id: 'e1', session_id: 's1' } })
    await store.startPortalChat('scout', 'hello', 's1', { voiceCallId: 'vd_abc' })
    expect(axios.post.mock.calls.at(-1)[0]).toMatch(/\/agents\/scout\/chat\/stream$/)
    expect(lastBody().voice_call_id).toBe('vd_abc')
  })

  it('a typed turn sends voice_call_id null', async () => {
    const store = useClientPortalStore()
    store.portalToken = 't'
    axios.post.mockResolvedValueOnce({ data: { execution_id: 'e1', session_id: 's1' } })
    await store.startPortalChat('scout', 'hello', 's1')
    expect(lastBody().voice_call_id).toBeNull()
  })

  it("the roster's direct_voice reaches store.directVoice", async () => {
    const store = useClientPortalStore()
    store.portalToken = 't'
    axios.get.mockResolvedValue({
      data: { agents: [], direct_voice: { available: true, reason: null, provider: 'gemini' } },
    })
    await store.fetchRoster()
    expect(store.directVoice).toEqual({ available: true, reason: null, provider: 'gemini' })
  })

  it('an older backend without direct_voice reads as unavailable', async () => {
    const store = useClientPortalStore()
    store.portalToken = 't'
    axios.get.mockResolvedValue({ data: { agents: [] } })
    await store.fetchRoster()
    expect(store.directVoice.available).toBe(false)
  })
})
