/**
 * #2791 — the store side: adopting a session, losing one to another tab, and
 * the source guards for the wiring a node-env spec cannot execute.
 *
 * `vitest.config.js` pins `environment: 'node'`: there is no `window`, so the
 * interceptors and the `storage` listener registered in `main.js` cannot be
 * driven here. What CAN be driven is everything they delegate to, which is why
 * #2791 put the verdict in a pure function and the reaction in two store actions
 * instead of inside three closures.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'
import { stripComments } from './helpers/stripComments'

// node has no localStorage; the same hoisted shim `workspaceSignOut.spec.js`
// uses, because `auth.js` reads storage at import time.
vi.hoisted(() => {
  const store = new Map()
  globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    clear: () => store.clear(),
  }
})

vi.mock('axios', () => {
  const mock = {
    get: vi.fn().mockResolvedValue({ data: { email: 'op@example.com', role: 'admin' } }),
    post: vi.fn().mockResolvedValue({ data: {} }),
    put: vi.fn().mockResolvedValue({ data: {} }),
    defaults: { headers: { common: {} } },
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    create: vi.fn(() => ({
      get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
      defaults: { headers: { common: {} } },
      interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    })),
  }
  return { default: mock }
})

const read = (rel) => stripComments(
  readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8'),
)
const MAIN = read('../../src/main.js')
const API = read('../../src/api.js')
const AUTH = read('../../src/stores/auth.js')
const PORTAL = read('../../src/stores/clientPortal.js')

beforeEach(() => {
  setActivePinia(createPinia())
  localStorage.clear()
  vi.clearAllMocks()
})

describe('adopting the session the browser actually holds', () => {
  it('a superseded tab converges on the CURRENT token instead of destroying it', async () => {
    const { useAuthStore } = await import('@/stores/auth')
    const auth = useAuthStore()
    auth.token = 'old-jwt'
    auth.isAuthenticated = true
    localStorage.setItem('token', 'new-jwt')
    localStorage.setItem('auth0_user', JSON.stringify({ email: 'op@example.com' }))

    expect(auth.adoptStoredSession()).toBe(true)
    expect(auth.token).toBe('new-jwt')
    expect(auth.isAuthenticated).toBe(true)
    // The whole point: the new session's credential is still in storage.
    expect(localStorage.getItem('token')).toBe('new-jwt')
  })

  it('role-gated UI stays closed until the new token’s own profile lands (#2198)', async () => {
    const { useAuthStore } = await import('@/stores/auth')
    const auth = useAuthStore()
    auth.token = 'old-jwt'
    auth.profileVerified = true          // verified for the PREVIOUS principal
    localStorage.setItem('token', 'new-jwt')

    auth.adoptStoredSession()
    expect(auth.profileVerified).toBe(false)
  })

  it('adopting an identical token is a no-op, so a storm of 401s costs one fetch', async () => {
    const axios = (await import('axios')).default
    const { useAuthStore } = await import('@/stores/auth')
    const auth = useAuthStore()
    auth.token = 'jwt'
    localStorage.setItem('token', 'jwt')

    expect(auth.adoptStoredSession()).toBe(true)
    expect(axios.get).not.toHaveBeenCalled()
  })

  it('adopting when storage is empty ends the session locally', async () => {
    const { useAuthStore } = await import('@/stores/auth')
    const auth = useAuthStore()
    auth.token = 'jwt'
    auth.isAuthenticated = true

    expect(auth.adoptStoredSession()).toBe(false)
    expect(auth.isAuthenticated).toBe(false)
    expect(auth.token).toBeNull()
  })
})

describe('a sibling tab ending the session', () => {
  it('drops the in-memory mirror without a second server revoke', async () => {
    const axios = (await import('axios')).default
    const { useAuthStore } = await import('@/stores/auth')
    const auth = useAuthStore()
    auth.token = 'jwt'
    auth.isAuthenticated = true
    auth.profileVerified = true

    auth.applySessionEndedElsewhere()

    expect(auth.isAuthenticated).toBe(false)
    expect(auth.profileVerified).toBe(false)
    // `logout()` would POST /api/auth/logout for a token another tab already
    // revoked, and would write to localStorage — so N background tabs reacting
    // to one `storage` event would each clear storage again.
    expect(axios.post).not.toHaveBeenCalled()
  })

  it('does not itself write to storage — the tab that logged out already did', async () => {
    const { useAuthStore } = await import('@/stores/auth')
    localStorage.setItem('auth0_user', JSON.stringify({ email: 'someone@example.com' }))
    const spy = vi.spyOn(localStorage, 'removeItem')
    useAuthStore().applySessionEndedElsewhere()
    expect(spy).not.toHaveBeenCalled()
    // …and the sibling's own key is untouched, so a tab that ended a PORTAL
    // session has not also wiped the platform user record.
    expect(localStorage.getItem('auth0_user')).not.toBeNull()
    spy.mockRestore()
  })
})

describe('one credential source, one handler (source guards)', () => {
  it('nothing writes the axios defaults Authorization copy any more', () => {
    // The second credential source. Only the logout cleanup may still DELETE it
    // (a tab running the previous build still carries one).
    const writes = AUTH.match(/axios\.defaults\.headers\.common\['Authorization'\]\s*=/g) || []
    expect(writes).toEqual([])
    expect(AUTH).toContain("delete axios.defaults.headers.common['Authorization']")
  })

  it('every transport derives the header from the one reader', () => {
    expect(API).toContain('readStoredToken()')
    expect(MAIN).toContain('readStoredToken()')
    // `api.js` must not re-read storage directly any more.
    expect(API).not.toMatch(/localStorage\.getItem\(['"]token['"]\)/)
  })

  it('all three 401 sites report to the single handler', () => {
    expect(API).toContain('notifyPlatformUnauthorized(error)')
    expect(MAIN).toContain('notifyPlatformUnauthorized(error)')
    expect(PORTAL).toContain('notifyPlatformUnauthorized(error)')
    // …and exactly one of them registers the reaction.
    expect(MAIN).toContain('setPlatformUnauthorizedHandler(handlePlatformUnauthorized)')
  })

  it('none of them carries a private copy of the bounce predicate', () => {
    // The duplicated expression that drifted three ways.
    for (const src of [API, MAIN]) {
      expect(src).not.toContain('const internalSession =')
      expect(src).not.toMatch(/!onWorkspace \|\| internalSession/)
    }
  })

  it('the api.js 401 path no longer hard-reloads or half-clears', () => {
    expect(API).not.toContain("window.location.href = '/login'")
    expect(API).not.toContain("localStorage.removeItem('token')")
  })

  it('a logout elsewhere is heard, and only for the platform token', () => {
    expect(MAIN).toContain("window.addEventListener('storage'")
    expect(MAIN).toContain('adoptStoredSession()')
    expect(MAIN).toContain('applySessionEndedElsewhere()')
    // A whole-storage clear (`key === null`) must count as the session ending.
    expect(MAIN).toContain('event.key !== null && event.key !== TOKEN_KEY')
  })

  it('a rejected navigation from the reaction never escapes as an unhandled rejection', async () => {
    const { setPlatformUnauthorizedHandler, notifyPlatformUnauthorized } =
      await import('@/utils/platformSession')
    // Vue Router rejects a redundant navigation, which is exactly what a second
    // 401 arriving while /login is already loading produces.
    setPlatformUnauthorizedHandler(() => Promise.reject(new Error('redundant navigation')))
    expect(() => notifyPlatformUnauthorized({})).not.toThrow()
    await new Promise((r) => setTimeout(r, 0))   // let the rejection settle
    setPlatformUnauthorizedHandler(null)
  })

  it('a throwing reaction never replaces the error being rejected', async () => {
    const { setPlatformUnauthorizedHandler, notifyPlatformUnauthorized } =
      await import('@/utils/platformSession')
    setPlatformUnauthorizedHandler(() => { throw new Error('boom') })
    expect(() => notifyPlatformUnauthorized({})).not.toThrow()
    setPlatformUnauthorizedHandler(null)
  })

  it('the reaction does not hold the user on a dead page for the revoke', () => {
    // `logout()` clears local state synchronously before its first await, so
    // the router guard is satisfied without waiting for the network call.
    expect(MAIN).not.toContain('await authStore.logout()')
  })

  it('the global request interceptor leaves an explicit header alone', () => {
    // The logout revoke depends on it: #2258 clears storage BEFORE the revoke,
    // so the only credential that call can carry is the explicit one.
    expect(MAIN).toContain('if (!headers.Authorization && !headers.authorization)')
    expect(AUTH).toContain('headers: { Authorization: `Bearer ${revoking}` }')
  })
})
