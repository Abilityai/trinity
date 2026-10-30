/**
 * trinity-enterprise#1010 — the read write covers only what the client held.
 *
 * The cursor used to move to the server's now, so an arrival after the client's
 * last fetch was swallowed. The client now sends `through`: the later of the
 * last chat-state `as_of` and the newest row its view rendered, in the spelling
 * the server sent (the server stores it verbatim and compares as strings).
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
import { heldThrough, laterInstant, newestAt, READ_UP_TO_NOW } from '@/utils/readThrough'

const READ = (kind, id) => `/api/enterprise/client-portal/chat-state/${kind}/${id}/read`

describe('laterInstant / newestAt', () => {
  it('compares as instants and keeps the winner spelled as it came', () => {
    // Lexically "…10:00:00Z" sorts AFTER "…10:00:00.500000Z"; as instants it is earlier.
    expect(laterInstant('2026-10-01T10:00:00Z', '2026-10-01T10:00:00.500000Z'))
      .toBe('2026-10-01T10:00:00.500000Z')
    expect(laterInstant('2026-10-01T11:00:00Z', '2026-10-01T10:00:00.500000Z'))
      .toBe('2026-10-01T11:00:00Z')
  })

  it('ignores a missing or unparsable side', () => {
    expect(laterInstant(null, '2026-10-01T10:00:00Z')).toBe('2026-10-01T10:00:00Z')
    expect(laterInstant('2026-10-01T10:00:00Z', 'junk')).toBe('2026-10-01T10:00:00Z')
    expect(laterInstant(null, undefined)).toBe(null)
  })

  it('finds the newest row whatever the order', () => {
    expect(newestAt([{ created_at: '2026-10-01T10:00:02Z' }, { created_at: '2026-10-01T10:00:01Z' }]))
      .toBe('2026-10-01T10:00:02Z')
    expect(newestAt([])).toBe(null)
  })
})

describe('heldThrough', () => {
  it('is the newest rendered instant', () => {
    const rows = [{ at: '2026-10-01T10:00:00Z' }, { at: '2026-10-01T10:05:00Z' }]
    expect(heldThrough(rows, 'at')).toBe('2026-10-01T10:05:00Z')
  })

  it('says "up to now" when the newest row carries no instant (a live reply)', () => {
    expect(heldThrough([{ at: '2026-10-01T10:00:00Z' }, { at: null }], 'at')).toBe(READ_UP_TO_NOW)
  })

  it('says nothing for an empty view', () => {
    expect(heldThrough([], 'at')).toBe(null)
  })
})

describe('the store sends what was held', () => {
  let store
  beforeEach(() => {
    setActivePinia(createPinia())
    store = useClientPortalStore()
    portalHttp.get = vi.fn(async () => ({ data: { chats: [], as_of: '2026-10-01T10:00:05.000000Z' } }))
    portalHttp.post = vi.fn(async () => ({}))
  })

  it("remembers the chat state's as_of", async () => {
    await store.fetchChatState()
    expect(store.chatStateAsOf).toBe('2026-10-01T10:00:05.000000Z')
  })

  it('an older as_of never replaces a newer one (an overtaken poll)', async () => {
    await store.fetchChatState()
    portalHttp.get = vi.fn(async () => ({ data: { chats: [], as_of: '2026-10-01T09:00:00.000000Z' } }))
    await store.fetchChatState()
    expect(store.chatStateAsOf).toBe('2026-10-01T10:00:05.000000Z')
  })

  it('a read with nothing newer rendered sends the as_of', async () => {
    await store.fetchChatState()
    await store.markChatReadStrict('room', 'r1')
    expect(portalHttp.post).toHaveBeenCalledWith(
      READ('room', 'r1'), { through: '2026-10-01T10:00:05.000000Z' }, expect.anything())
  })

  it('a read sends the newest rendered row when that is later', async () => {
    await store.fetchChatState()
    await store.markChatReadStrict('thread', 's1', '2026-10-01T10:00:30Z')
    expect(portalHttp.post).toHaveBeenCalledWith(
      READ('thread', 's1'), { through: '2026-10-01T10:00:30Z' }, expect.anything())
  })

  it('"up to now" sends no body', async () => {
    await store.fetchChatState()
    await store.markChatReadStrict('thread', 's1', READ_UP_TO_NOW)
    expect(portalHttp.post).toHaveBeenCalledWith(READ('thread', 's1'), null, expect.anything())
  })

  it('before any state was read and with nothing rendered, no body', async () => {
    await store.markChatRead('thread', 's1')
    expect(portalHttp.post).toHaveBeenCalledWith(READ('thread', 's1'), null, expect.anything())
  })
})
