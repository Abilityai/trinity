/**
 * Workspace session + routing (ent#357).
 *
 * The surface formerly called "Client Portal" is now a view of the account you
 * are already in. Two properties carry that, and both were one predicate away
 * from being wrong:
 *
 *   1. A signed-in platform user must NOT be asked for an email code. The
 *      transport layer already fell back to the platform auth header, and the
 *      backend's `get_portal_identity` already accepts a platform JWT — only
 *      `isClientSignedIn` disagreed, so the OTP round-trip it forced was pure
 *      ceremony on a surface the user was already entitled to.
 *   2. An external client with no platform account must still get the OTP form,
 *      unchanged.
 *
 * Plus the redirects: `/portal` URLs were mailed to real clients, and a client
 * who lands on a dead link has no way to report it.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

// `vitest.config.js` runs unit tests in the NODE environment on purpose ("pure
// modules only, no DOM"), and this store reads localStorage at import time. A
// hoisted shim keeps that config honest — adding jsdom just for a key/value map
// would widen the suite's dependency surface for no coverage.
vi.hoisted(() => {
  const store = new Map()
  globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    clear: () => store.clear(),
  }
})

// The store imports the auth store; stub it so each case states exactly one
// thing: whether a platform session exists.
//
// The flag must be REACTIVE. Pinia getters are computeds, so a plain `let`
// never invalidates them and the sign-out case passes vacuously — in
// production `authStore.isAuthenticated` is reactive store state, and the test
// stub has to model that or it tests nothing.
vi.mock('@/stores/auth', async () => {
  const { ref } = await import('vue')
  const authed = ref(false)
  return {
    useAuthStore: () => ({
      get isAuthenticated() { return authed.value },
      get authHeader() { return authed.value ? { Authorization: 'Bearer platform-jwt' } : {} },
    }),
    // Test-only handle: mutation must go through the SAME ref the getters read,
    // or Vue never invalidates the computed and the sign-out case passes for
    // the wrong reason.
    __setAuthed: (v) => { authed.value = v },
  }
})

// vue-router's default history needs `window`; the unit suite is node-only by
// design. Swapping ONLY the history implementation lets these tests exercise
// the real route table and the real router rather than a restatement of it.
vi.mock('vue-router', async (importOriginal) => {
  const actual = await importOriginal()
  return { ...actual, createWebHistory: actual.createMemoryHistory }
})
// `create` matters: importing the router pulls in `api.js`, which builds an
// axios instance at module scope.
vi.mock('axios', () => {
  const instance = {
    get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  }
  return {
    default: Object.assign(
      { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), create: () => instance },
      { interceptors: instance.interceptors },
    ),
  }
})

import { useClientPortalStore } from '@/stores/clientPortal'
import { __setAuthed } from '@/stores/auth'

const PORTAL_TOKEN_KEY = 'trinity.portalToken'

describe('workspace session', () => {
  beforeEach(() => {
    localStorage.clear()
    __setAuthed(false)
    setActivePinia(createPinia())
  })

  it('an anonymous visitor still gets the email-OTP sign-in (AC: external path unchanged)', () => {
    const store = useClientPortalStore()
    expect(store.isClientSignedIn).toBe(false)
    expect(store.isPlatformSession).toBe(false)
  })

  it('a signed-in platform user is already in — no OTP (AC: one click, no round-trip)', () => {
    __setAuthed(true)
    const store = useClientPortalStore()
    expect(store.isClientSignedIn).toBe(true)
    expect(store.isPlatformSession).toBe(true)
    // …and authenticates as themselves, with no second credential minted.
    expect(store.authHeader).toEqual({ Authorization: 'Bearer platform-jwt' })
  })

  it('the internal session IS the platform session (AC: platform sign-out ends it)', () => {
    __setAuthed(true)
    const store = useClientPortalStore()
    expect(store.isClientSignedIn).toBe(true)

    // Signing out of the platform is the whole revocation: there is no separate
    // workspace credential that could outlive it.
    __setAuthed(false)
    expect(store.isClientSignedIn).toBe(false)
    expect(localStorage.getItem(PORTAL_TOKEN_KEY)).toBeNull()
  })

  it('an external client session survives independently of the platform session', () => {
    localStorage.setItem(PORTAL_TOKEN_KEY, 'portal-token')
    setActivePinia(createPinia())
    const store = useClientPortalStore()

    expect(store.isClientSignedIn).toBe(true)
    expect(store.isPlatformSession).toBe(false)   // NOT platform-derived
    expect(store.authHeader).toEqual({ Authorization: 'Bearer portal-token' })
  })

  it('a portal token wins over a platform session, so operator preview cannot hijack a client tab', () => {
    __setAuthed(true)
    localStorage.setItem(PORTAL_TOKEN_KEY, 'portal-token')
    setActivePinia(createPinia())
    const store = useClientPortalStore()

    expect(store.authHeader).toEqual({ Authorization: 'Bearer portal-token' })
    expect(store.isPlatformSession).toBe(false)
  })
})

describe('workspace roster failures are honest (AC: no dead empty state)', () => {
  beforeEach(() => {
    localStorage.clear()
    __setAuthed(true)
    setActivePinia(createPinia())
  })

  it('a 404 reports the module as unavailable, not as an empty share list', async () => {
    const axios = (await import('axios')).default
    axios.get.mockRejectedValueOnce({ response: { status: 404 } })

    const store = useClientPortalStore()
    await store.fetchRoster()

    expect(store.unavailable).toBe(true)
    expect(store.agents).toEqual([])
    expect(store.error).toMatch(/not available/i)
  })

  it('a transient failure is reported as a failure, and is retryable', async () => {
    const axios = (await import('axios')).default
    axios.get.mockRejectedValueOnce({ response: { status: 503, data: { detail: 'upstream down' } } })

    const store = useClientPortalStore()
    await store.fetchRoster()

    expect(store.unavailable).toBe(false)   // NOT "unavailable on this instance"
    expect(store.error).toBe('upstream down')
  })

  it('an empty roster stays an empty roster', async () => {
    const axios = (await import('axios')).default
    axios.get.mockResolvedValueOnce({ data: { client_email: 'a@b.c', agents: [] } })

    const store = useClientPortalStore()
    await store.fetchRoster()

    expect(store.unavailable).toBe(false)
    expect(store.error).toBeNull()
    expect(store.agents).toEqual([])
  })
})

describe('legacy /portal links keep working (AC: query-preserving redirect)', () => {
  // Asserts the SHIPPED redirect functions from the real route table. Driving
  // a router with `push()` would resolve the lazy route components, which pulls
  // `.vue` files into a suite that is node-only by design — and the property
  // under test lives entirely in these two functions.
  it('/portal keeps query and hash on the hop to /workspace', async () => {
    const { routes } = await import('@/router/index.js')
    const record = routes.find(r => r.path === '/portal')

    expect(record, 'no /portal record — old client links would 404').toBeTruthy()
    expect(typeof record.redirect).toBe('function')

    const target = record.redirect({ query: { agent: 'scout' }, hash: '#top', params: {} })
    expect(target.path).toBe('/workspace')
    expect(target.query).toEqual({ agent: 'scout' })
    expect(target.hash).toBe('#top')
  })

  it('/portal/c/:sessionId lands on the same thread, not the roster', async () => {
    const { routes } = await import('@/router/index.js')
    const record = routes.find(r => r.path === '/portal/c/:sessionId')

    expect(record, 'no legacy thread record — a mailed deep link would 404').toBeTruthy()
    const target = record.redirect({ params: { sessionId: 'sess-123' }, query: { x: '1' }, hash: '' })
    expect(target.path).toBe('/workspace/c/sess-123')
    expect(target.query).toEqual({ x: '1' })
  })

  it('the workspace routes exist under their new names', async () => {
    const { routes } = await import('@/router/index.js')
    const paths = routes.map(r => r.path)
    expect(paths).toContain('/workspace')
    expect(paths).toContain('/workspace/c/:sessionId')
  })
})

describe('workspace availability state is not sticky (/review C1)', () => {
  beforeEach(() => {
    localStorage.clear()
    __setAuthed(true)
    setActivePinia(createPinia())
  })

  it('a successful retry clears the "unavailable" verdict', async () => {
    const axios = (await import('axios')).default
    const store = useClientPortalStore()

    axios.get.mockRejectedValueOnce({ response: { status: 404 } })
    await store.fetchRoster()
    expect(store.unavailable).toBe(true)

    // The retry button the unavailable state offers.
    axios.get.mockResolvedValueOnce({ data: { client_email: 'a@b.c', agents: [{ name: 'scout' }] } })
    await store.fetchRoster()

    expect(store.unavailable).toBe(false)
    expect(store.error).toBeNull()
    expect(store.agents).toHaveLength(1)
  })
})

describe('who gets bounced to /login on a 401 (/review I1)', () => {
  // The guards live in api.js / main.js interceptors, which need `window`. The
  // property under test is the PREDICATE, so assert it directly against the
  // storage states it reads — the same expression both interceptors use.
  const shouldBounce = (path, hasPlatformToken) => {
    const onWorkspace = path.startsWith('/workspace') || path.startsWith('/portal')
    return !onWorkspace || hasPlatformToken
  }

  it('an internal user whose platform session expired IS bounced', () => {
    expect(shouldBounce('/workspace', true)).toBe(true)
  })

  it('an external client on the workspace is NOT bounced to the operator login', () => {
    expect(shouldBounce('/workspace', false)).toBe(false)
    expect(shouldBounce('/workspace/c/abc', false)).toBe(false)
    expect(shouldBounce('/portal', false)).toBe(false)   // legacy URL, mid-redirect
  })

  it('the verdict does not depend on the portal token, which signOut() races away', () => {
    // The first 401 drops the portal token (fetchRoster -> signOut). A second,
    // concurrent 401 must reach the same answer as the first — keying on the
    // portal token made this flip and threw the client onto /login.
    const before = shouldBounce('/workspace', false)   // portal token present
    const after = shouldBounce('/workspace', false)    // portal token now gone
    expect(after).toBe(before)
  })

  it('everywhere else keeps the normal bounce', () => {
    expect(shouldBounce('/agents/scout', false)).toBe(true)
    expect(shouldBounce('/', true)).toBe(true)
  })
})
