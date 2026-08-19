/**
 * The Workspace asks store contract (ent#364).
 *
 * The feature's central claim is "one row, three renderings, cleared everywhere".
 * In the client that reduces to: all three surfaces read ONE list, and answering
 * removes the row from it. So these tests are about the list — the three components
 * are template work over what is pinned here.
 *
 * Also pinned: the module is enterprise-gated, so an OSS or unentitled build gets
 * 404/403 on every poll. That has to be silent, or every OSS install logs a warning
 * every 20 seconds for a feature it does not have.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

vi.hoisted(() => {
  const mem = new Map()
  globalThis.localStorage = {
    getItem: (k) => (mem.has(k) ? mem.get(k) : null),
    setItem: (k, v) => mem.set(k, String(v)),
    removeItem: (k) => mem.delete(k),
    clear: () => mem.clear(),
  }
  const session = new Map()
  globalThis.sessionStorage = {
    getItem: (k) => (session.has(k) ? session.get(k) : null),
    setItem: (k, v) => session.set(k, String(v)),
    removeItem: (k) => session.delete(k),
    clear: () => session.clear(),
  }
  globalThis.window = globalThis.window || { location: { pathname: '/workspace' } }
})

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: false, authHeader: {}, logout: vi.fn() }),
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

import axios from 'axios'
import { useClientPortalStore } from '@/stores/clientPortal'

const ask = (id, over = {}) => ({
  id, agent_name: 'scout', kind: 'question', priority: 'medium',
  title: 'Ship it?', question: 'Ship it?', options: ['yes', 'no'],
  created_at: '2026-08-19T10:00:00Z', expires_at: null, status: 'pending',
  chat_id: null, ...over,
})

let store

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  setActivePinia(createPinia())
  vi.clearAllMocks()
  localStorage.setItem('trinity.portalToken', 'portal-token')   // a signed-in client
  store = useClientPortalStore()
})

describe('one list, three renderings', () => {
  it('the count, the agent filter and the full list all come from one fetch', async () => {
    axios.get.mockResolvedValueOnce({
      data: [ask('a1'), ask('a2', { agent_name: 'sage' })],
    })

    await store.fetchAsks()

    expect(axios.get).toHaveBeenCalledTimes(1)
    expect(store.askCount).toBe(2)                       // sidebar
    expect(store.asksForAgent('scout').map((a) => a.id)).toEqual(['a1'])  // agent page
    expect(store.asks).toHaveLength(2)                   // inline / global
  })

  it('answering removes the row, so every surface clears with no second call', async () => {
    axios.get.mockResolvedValueOnce({ data: [ask('a1'), ask('a2')] })
    await store.fetchAsks()

    axios.post.mockResolvedValueOnce({ data: { ...ask('a1'), status: 'responded' } })
    await store.answerAsk('a1', { response: 'yes' })

    expect(store.askCount).toBe(1)
    expect(store.asksForAgent('scout').map((a) => a.id)).toEqual(['a2'])
    expect(axios.get).toHaveBeenCalledTimes(1)      // no refetch needed
  })

  it('an expired ask still renders but is not counted', async () => {
    // #1142 deletes terminal rows, so expiry must be VISIBLE while it exists — but
    // it is not something to nag about: the person cannot answer it any more.
    axios.get.mockResolvedValueOnce({
      data: [ask('a1'), ask('a2', { status: 'expired' })],
    })

    await store.fetchAsks()

    expect(store.asks).toHaveLength(2)
    expect(store.askCount).toBe(1)
    expect(store.asksForAgent('scout')).toHaveLength(2)
  })

  it('narrows server-side when an agent is named', async () => {
    axios.get.mockResolvedValueOnce({ data: [ask('a1')] })
    await store.fetchAsks('scout')

    const [, config] = axios.get.mock.calls[0]
    expect(config.params).toEqual({ agent_name: 'scout' })
  })
})

describe('an unentitled build says nothing', () => {
  it('404 leaves the feature unavailable and logs nothing', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    axios.get.mockRejectedValueOnce({ response: { status: 404 } })

    await store.fetchAsks()

    expect(store.asksAvailable).toBe(false)
    expect(store.asks).toEqual([])
    expect(warn).not.toHaveBeenCalled()
    warn.mockRestore()
  })

  it('403 is equally silent — the module is present but not entitled', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    axios.get.mockRejectedValueOnce({ response: { status: 403 } })

    await store.fetchAsks()

    expect(store.asksAvailable).toBe(false)
    expect(warn).not.toHaveBeenCalled()
    warn.mockRestore()
  })

  it('a real failure is reported once, and clears the list rather than showing stale asks', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    axios.get.mockResolvedValueOnce({ data: [ask('a1')] })
    await store.fetchAsks()

    axios.get.mockRejectedValueOnce({ response: { status: 500 } })
    await store.fetchAsks()

    expect(warn).toHaveBeenCalledTimes(1)
    expect(store.asks).toEqual([])
    expect(store.asksAvailable).toBe(false)
    warn.mockRestore()
  })

  it('does not poll at all when nobody is signed in', async () => {
    localStorage.clear()
    setActivePinia(createPinia())
    const anon = useClientPortalStore()

    await anon.fetchAsks()

    expect(axios.get).not.toHaveBeenCalled()
  })
})

describe('answering surfaces the backend refusal', () => {
  it('propagates the error and keeps the ask, so nothing looks resolved that is not', async () => {
    axios.get.mockResolvedValueOnce({ data: [ask('a1')] })
    await store.fetchAsks()

    axios.post.mockRejectedValueOnce({
      response: { status: 409, data: { detail: { code: 'expired', message: 'This ask expired before it was answered.' } } },
    })

    await expect(store.answerAsk('a1', { response: 'yes' })).rejects.toBeTruthy()
    expect(store.askCount).toBe(1)
  })
})
