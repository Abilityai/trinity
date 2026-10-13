/**
 * trinity-enterprise#610 — the two store calls the Inbox adds.
 *
 * `fetchChatState({previews})` — counts and previews from ONE response (D5), so
 * a row's "N new" and its excerpt cannot drift; called with no argument it keeps
 * the old map shape every existing caller reads.
 *
 * `markChatReadStrict` — Mark all read settles many writes and must report how
 * many failed; the fire-and-forget `markChatRead` swallows every error, so over
 * it `Promise.allSettled` would always report zero failures (D11).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

vi.hoisted(() => {
  const mk = () => {
    const mem = new Map()
    return {
      getItem: (k) => (mem.has(k) ? mem.get(k) : null),
      setItem: (k, v) => mem.set(k, String(v)),
      removeItem: (k) => mem.delete(k),
      clear: () => mem.clear(),
    }
  }
  globalThis.localStorage = mk()
  globalThis.sessionStorage = mk()
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

import { useClientPortalStore, portalHttp } from '@/stores/clientPortal'

const URL = '/api/enterprise/client-portal/chat-state'
const latest = { kind: 'message', id: 'm9', at: '2026-09-28T10:00:00Z', excerpt: 'Done.', outcome: 'done' }
const payload = {
  chats: [
    { kind: 'thread', id: 's1', starred: false, unread: 2, latest, first_unread_message_id: 'm7' },
    { kind: 'thread', id: 's2', starred: true, unread: 0 },
    { kind: 'room', id: 'r1', starred: false, unread: 0 },
  ],
}

let store
beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  setActivePinia(createPinia())
  vi.clearAllMocks()
  localStorage.setItem('trinity.portalToken', 'portal-token')
  store = useClientPortalStore()
})

describe('fetchChatState', () => {
  it('with no argument: the old map shape, no previews flag sent', async () => {
    portalHttp.get.mockResolvedValueOnce({ data: payload })
    const out = await store.fetchChatState()
    expect(Object.keys(out).sort()).toEqual(['room:r1', 'thread:s1', 'thread:s2'])
    expect(out['thread:s1'].unread).toBe(2)
    const [url, cfg] = portalHttp.get.mock.calls[0]
    expect(url).toBe(URL)
    expect(cfg.params).toBeUndefined()
  })

  it('with {previews: true}: asks for previews and returns {state, previews} from one response', async () => {
    portalHttp.get.mockResolvedValueOnce({ data: payload })
    const { state, previews } = await store.fetchChatState({ previews: true })
    expect(portalHttp.get).toHaveBeenCalledTimes(1)
    expect(portalHttp.get.mock.calls[0][1].params).toEqual({ previews: true })
    expect(state['thread:s1'].unread).toBe(2)
    expect(previews).toEqual({ 'thread:s1': { latest, first_unread_message_id: 'm7' } })
  })

  it('with {previews: false}: the new shape, no flag, empty previews', async () => {
    portalHttp.get.mockResolvedValueOnce({ data: { chats: [{ kind: 'thread', id: 's1', unread: 1 }] } })
    const out = await store.fetchChatState({ previews: false })
    expect(portalHttp.get.mock.calls[0][1].params).toBeUndefined()
    expect(out).toEqual({ state: { 'thread:s1': { kind: 'thread', id: 's1', unread: 1 } }, previews: {} })
  })

  it('an older backend that ignores the flag yields empty previews, never a throw', async () => {
    portalHttp.get.mockResolvedValueOnce({ data: { chats: [{ kind: 'thread', id: 's1', unread: 3 }] } })
    const { state, previews } = await store.fetchChatState({ previews: true })
    expect(state['thread:s1'].unread).toBe(3)
    expect(previews).toEqual({})
  })

  it('a failed read rejects (the shell keeps its last state), it never returns a synthetic empty', async () => {
    portalHttp.get.mockRejectedValueOnce(Object.assign(new Error('boom'), { response: { status: 500 } }))
    await expect(store.fetchChatState({ previews: true })).rejects.toThrow('boom')
  })
})

describe('markChatReadStrict', () => {
  it('posts the same read write as markChatRead', async () => {
    portalHttp.post.mockResolvedValueOnce({ data: null })
    await store.markChatReadStrict('thread', 'a/b')
    expect(portalHttp.post.mock.calls[0][0]).toBe(`${URL}/thread/a%2Fb/read`)
  })

  it('RETHROWS, where markChatRead swallows — so allSettled can count failures', async () => {
    const err = Object.assign(new Error('nope'), { response: { status: 500 } })
    portalHttp.post.mockRejectedValueOnce(err)
    await expect(store.markChatReadStrict('thread', 's1')).rejects.toThrow('nope')

    portalHttp.post.mockRejectedValueOnce(err)
    await expect(store.markChatRead('thread', 's1')).resolves.toBeUndefined()

    portalHttp.post.mockRejectedValueOnce(err).mockResolvedValueOnce({ data: null })
    const settled = await Promise.allSettled([
      store.markChatReadStrict('thread', 'x'), store.markChatReadStrict('thread', 'y'),
    ])
    expect(settled.filter((s) => s.status === 'rejected')).toHaveLength(1)
  })
})
