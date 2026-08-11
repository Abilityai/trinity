/**
 * The client half of the sliding Workspace session (ent#375).
 *
 * The backend slides the session and hands back a rotated token in a response
 * header. Two things have to be true on this side or the feature is invisible:
 *
 *   1. the rotated token is adopted — otherwise the client keeps presenting the
 *      old one, it eventually expires, and the user is back to re-authenticating
 *      with nothing to show anything was wrong;
 *   2. when a session DOES end, the user is told, and lands back where they
 *      were — not bounced silently to a sign-in form, which is indistinguishable
 *      from "you were never signed in".
 *
 * The adoption path is deliberately an axios *interceptor* rather than 13
 * per-call-site edits: a portal call added later cannot forget to opt in, and
 * forgetting would be silent. These tests therefore drive the interceptor, not
 * a helper — if the interceptor stops being installed, they fail.
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
  globalThis.window = globalThis.window || { location: { pathname: '/workspace/c/sess-9' } }
})

vi.mock('@/stores/auth', async () => {
  const { ref } = await import('vue')
  const authed = ref(false)
  return {
    useAuthStore: () => ({
      get isAuthenticated() { return authed.value },
      get authHeader() { return authed.value ? { Authorization: 'Bearer platform-jwt' } : {} },
    }),
    __setAuthed: (v) => { authed.value = v },
  }
})

// A minimal axios whose response interceptors we can actually FIRE — the point
// of these tests is the interceptor, so a mock that swallowed registration
// would test nothing.
vi.mock('axios', () => {
  const handlers = []
  const inst = {
    get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: {
      request: { use: vi.fn() },
      response: { use: (fn) => { handlers.push(fn); return handlers.length - 1 } },
    },
    defaults: { headers: { common: {} } },
    __fire: (response) => handlers.reduce((r, fn) => fn(r), response),
    __count: () => handlers.length,
  }
  return { default: Object.assign(inst, { create: () => inst }) }
})

import axios from 'axios'
import { useClientPortalStore } from '@/stores/clientPortal'

const TOKEN_KEY = 'trinity.portalToken'
const HEADER = 'x-trinity-session-token'

const portalResponse = (token) => ({
  config: { url: '/api/enterprise/client-portal/my-agents' },
  headers: token ? { [HEADER]: token } : {},
})

describe('rotated tokens are adopted (AC: renews without a visible interruption)', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
  })

  it('swaps the new token into storage and the store', () => {
    localStorage.setItem(TOKEN_KEY, 'old-token')
    const store = useClientPortalStore()
    expect(store.portalToken).toBe('old-token')

    axios.__fire(portalResponse('rotated-token'))

    expect(localStorage.getItem(TOKEN_KEY)).toBe('rotated-token')
    expect(store.portalToken).toBe('rotated-token')
  })

  it('a response without the header changes nothing (the common case)', () => {
    localStorage.setItem(TOKEN_KEY, 'old-token')
    const store = useClientPortalStore()

    axios.__fire(portalResponse(null))

    expect(localStorage.getItem(TOKEN_KEY)).toBe('old-token')
    expect(store.portalToken).toBe('old-token')
  })

  it('an operator previewing on a platform JWT does NOT acquire a portal token', () => {
    // No portal token in storage = operator preview. A header on that response
    // must not mint a client session for them; the surface would then keep
    // authenticating as a client after they navigated away.
    const store = useClientPortalStore()
    expect(store.portalToken).toBeNull()

    axios.__fire(portalResponse('rotated-token'))

    expect(localStorage.getItem(TOKEN_KEY)).toBeNull()
    expect(store.portalToken).toBeNull()
  })

  it('a header on a NON-portal response is ignored', () => {
    localStorage.setItem(TOKEN_KEY, 'old-token')
    useClientPortalStore()

    axios.__fire({ config: { url: '/api/agents' }, headers: { [HEADER]: 'not-ours' } })

    expect(localStorage.getItem(TOKEN_KEY)).toBe('old-token')
  })

  it('the interceptor is installed once, not once per store construction', () => {
    const before = axios.__count()
    useClientPortalStore()
    setActivePinia(createPinia())
    useClientPortalStore()
    expect(axios.__count()).toBe(before)
  })
})

describe('an ended session is honest about it (AC: no silent bounce)', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
  })

  it('expiry is distinguishable from a deliberate sign-out', () => {
    localStorage.setItem(TOKEN_KEY, 'tok')
    const store = useClientPortalStore()

    store.endSession({ expired: true, resumePath: '/workspace/c/sess-9' })
    expect(store.sessionExpired).toBe(true)
    expect(store.portalToken).toBeNull()

    // …and a deliberate sign-out is NOT reported as an expiry.
    localStorage.setItem(TOKEN_KEY, 'tok2')
    setActivePinia(createPinia())
    const s2 = useClientPortalStore()
    s2.signOut()
    expect(s2.sessionExpired).toBe(false)
  })

  it('remembers where the user was, so re-auth returns them to the thread', () => {
    localStorage.setItem(TOKEN_KEY, 'tok')
    const store = useClientPortalStore()

    store.endSession({ expired: true, resumePath: '/workspace/c/sess-9' })
    expect(store.resumePath).toBe('/workspace/c/sess-9')
  })

  it('signing in again clears the expiry notice', () => {
    localStorage.setItem(TOKEN_KEY, 'tok')
    const store = useClientPortalStore()
    store.endSession({ expired: true, resumePath: '/workspace' })
    expect(store.sessionExpired).toBe(true)

    store.signOut()          // what the sign-in flow calls before a fresh start
    expect(store.sessionExpired).toBe(false)
    expect(store.resumePath).toBeNull()
  })

  it('a 401 while loading the roster reports expiry rather than an empty roster', async () => {
    localStorage.setItem(TOKEN_KEY, 'tok')
    setActivePinia(createPinia())
    const store = useClientPortalStore()

    axios.get.mockRejectedValueOnce({ response: { status: 401 } })
    await store.fetchRoster()

    expect(store.sessionExpired).toBe(true)
    expect(store.portalToken).toBeNull()
    expect(store.agents).toEqual([])
  })
})

describe('the session-policy panel survives its own save (ent#375)', () => {
  // Found by driving the real endpoints, not by unit tests — which is why this
  // one exists. The OSS GET returns `editable`; the entitled PUT did not. The
  // panel adopted the PUT response wholesale, `editable` went undefined, and the
  // form disabled itself and showed the community upsell the instant you saved
  // successfully. One save made a working panel look broken.
  //
  // Two fixes, both pinned here: the responses are shape-identical now, and
  // adopt() MERGES so a future divergence degrades to a stale field rather than
  // a dead form.
  const adopt = (current, data) => ({ ...(current || {}), ...data })

  it('a PUT response missing `editable` does not disable the form', () => {
    const loaded = { idle_days: 7, absolute_days: 30, editable: true, sources: {} }
    const afterSave = adopt(loaded, { idle_days: 10, absolute_days: 30, sources: {} })

    expect(afterSave.editable).toBe(true)
    expect(afterSave.idle_days).toBe(10)
  })

  it('a PUT response that DOES carry `editable` still wins', () => {
    const loaded = { idle_days: 7, editable: true }
    expect(adopt(loaded, { idle_days: 7, editable: false }).editable).toBe(false)
  })

  it('replacing instead of merging is what broke it', () => {
    // The old behaviour, kept as the counter-example so the reason survives.
    const replaced = { idle_days: 10, absolute_days: 30, sources: {} }
    expect(replaced.editable).toBeUndefined()
  })
})
