/**
 * #2791 — the store side: adopting a session, losing one to another tab, and
 * the source guards for the wiring a node-env spec cannot execute.
 *
 * `vitest.config.js` pins `environment: 'node'`: there is no `window`, so the
 * interceptors and the `storage` listener registered in `main.js` cannot be
 * driven here. What CAN be driven is everything they delegate to — and after
 * the merge-train review of this PR, that is the whole reaction, not just the
 * verdict: `reactToPlatformUnauthorized`, `reactToStorageEvent` and
 * `applyRequestCredential` take their collaborators as arguments and are
 * EXECUTED below with fakes. The first version pinned the wiring by regex, and
 * a mutation battery (restore the reported bug on the `stale` branch; invert
 * the storage listener) stayed fully green. `main.js` is now wiring only, and
 * the source guards at the bottom assert exactly that: that it calls these.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { readFileSync, readdirSync } from 'fs'
import { fileURLToPath } from 'url'
import { join, relative } from 'path'
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
const APP = read('../../src/App.vue')

// Every source file under src/frontend/src, comments stripped — for the guards
// that must walk the WHOLE tree. The first version of the defaults-writer guard
// walked `auth.js` only and stayed green over `App.vue:64`, the one writer that
// made the whole mechanism inert (review C2; Invariant #5's "a guard that walks
// only one of the two trees is not a guard").
const SRC_ROOT = fileURLToPath(new URL('../../src/', import.meta.url))
function walk(dir) {
  const out = []
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name)
    if (entry.isDirectory()) out.push(...walk(full))
    else if (/\.(js|vue|ts)$/.test(entry.name)) out.push(full)
  }
  return out
}
const TREE = walk(SRC_ROOT).map((f) => ({
  file: relative(SRC_ROOT, f),
  src: stripComments(readFileSync(f, 'utf8')),
}))

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

describe('the reaction to a 401, executed (review C4 + the CI gap)', () => {
  // Fakes for the collaborators main.js supplies for real.
  function harness({ storedToken = 'NEW', portalTokenPresent = false, path = '/agents' } = {}) {
    const calls = { adopt: 0, logout: 0, login: 0 }
    const deps = {
      path,
      portalTokenPresent,
      readToken: () => storedToken,
      adoptStoredSession: () => { calls.adopt += 1 },
      logout: () => { calls.logout += 1 },
      goToLogin: () => { calls.login += 1; return Promise.resolve('navigated') },
    }
    return { calls, deps }
  }
  const failedWith = (token) => ({ config: { headers: { Authorization: `Bearer ${token}` } } })

  it('the reported bug: a superseded token ADOPTS the current session and never logs out', async () => {
    const { reactToPlatformUnauthorized } = await import('@/utils/platformSession')
    const { calls, deps } = harness({ storedToken: 'NEW' })
    const { verdict } = reactToPlatformUnauthorized(failedWith('OLD'), deps)
    expect(verdict).toBe('stale')
    expect(calls).toEqual({ adopt: 1, logout: 0, login: 0 })
  })

  it('the stored token itself failing logs out and navigates, in that order, without awaiting', async () => {
    const { reactToPlatformUnauthorized } = await import('@/utils/platformSession')
    const order = []
    const { deps } = harness({ storedToken: 'CUR' })
    deps.logout = () => order.push('logout')
    deps.goToLogin = () => { order.push('login'); return Promise.resolve() }
    const { verdict, navigation } = reactToPlatformUnauthorized(failedWith('CUR'), deps)
    expect(verdict).toBe('logout')
    expect(order).toEqual(['logout', 'login'])
    // The navigation promise is RETURNED so notifyPlatformUnauthorized can
    // absorb a rejected redundant navigation (review C4).
    expect(typeof navigation?.then).toBe('function')
  })

  it("a client tab's dead operator JWT on the Workspace is ignored (AC #5)", async () => {
    const { reactToPlatformUnauthorized } = await import('@/utils/platformSession')
    const { calls, deps } = harness({ storedToken: 'CUR', portalTokenPresent: true, path: '/workspace' })
    expect(reactToPlatformUnauthorized(failedWith('CUR'), deps).verdict).toBe('ignore')
    expect(calls).toEqual({ adopt: 0, logout: 0, login: 0 })
  })

  it('an auth route never bounces off itself', async () => {
    const { reactToPlatformUnauthorized } = await import('@/utils/platformSession')
    const { calls, deps } = harness({ storedToken: 'CUR', path: '/login' })
    expect(reactToPlatformUnauthorized(failedWith('CUR'), deps).verdict).toBe('ignore')
    expect(calls.login).toBe(0)
  })

  it('a rejected redundant navigation from the REAL handler shape is absorbed', async () => {
    const { reactToPlatformUnauthorized, setPlatformUnauthorizedHandler, notifyPlatformUnauthorized } =
      await import('@/utils/platformSession')
    const { deps } = harness({ storedToken: 'CUR' })
    deps.goToLogin = () => Promise.reject(new Error('Avoided redundant navigation to /login'))
    let escaped = null
    const onUnhandled = (e) => { escaped = e }
    process.on('unhandledRejection', onUnhandled)
    try {
      // The production handler RETURNS the reaction's navigation — the shape
      // review C4 found missing (it ended `router.push(...)` without `return`).
      setPlatformUnauthorizedHandler((error) => reactToPlatformUnauthorized(error, deps).navigation)
      notifyPlatformUnauthorized(failedWith('CUR'))
      await new Promise((r) => setTimeout(r, 10))
    } finally {
      process.off('unhandledRejection', onUnhandled)
      setPlatformUnauthorizedHandler(null)
    }
    expect(escaped).toBeNull()
  })
})

describe('the storage listener, executed (AC #2)', () => {
  function harness(storedToken) {
    const calls = { adopt: 0, ended: 0 }
    const storage = {}
    const deps = {
      storage,
      readToken: () => storedToken,
      adoptStoredSession: () => { calls.adopt += 1 },
      applySessionEndedElsewhere: () => { calls.ended += 1 },
    }
    return { calls, deps, storage }
  }

  it('a sibling LOGIN adopts; a sibling LOGOUT ends — never the other way round', async () => {
    const { reactToStorageEvent, TOKEN_KEY } = await import('@/utils/platformSession')
    let h = harness('NEW')
    expect(reactToStorageEvent({ key: TOKEN_KEY, storageArea: h.storage }, h.deps)).toBe('adopt')
    expect(h.calls).toEqual({ adopt: 1, ended: 0 })
    h = harness(null)
    expect(reactToStorageEvent({ key: TOKEN_KEY, storageArea: h.storage }, h.deps)).toBe('ended')
    expect(h.calls).toEqual({ adopt: 0, ended: 1 })
  })

  it('a whole-storage clear ends the session too', async () => {
    const { reactToStorageEvent } = await import('@/utils/platformSession')
    const h = harness(null)
    expect(reactToStorageEvent({ key: null, storageArea: h.storage }, h.deps)).toBe('ended')
  })

  it('the user key is heard as well — a sibling login writes it a tick after the token (W6)', async () => {
    const { reactToStorageEvent, USER_KEY } = await import('@/utils/platformSession')
    const h = harness('NEW')
    expect(reactToStorageEvent({ key: USER_KEY, storageArea: h.storage }, h.deps)).toBe('adopt')
  })

  it('other keys and other storage areas are ignored', async () => {
    const { reactToStorageEvent, TOKEN_KEY } = await import('@/utils/platformSession')
    const h = harness('NEW')
    expect(reactToStorageEvent({ key: 'trinity-dashboard-view', storageArea: h.storage }, h.deps)).toBe('ignored')
    expect(reactToStorageEvent({ key: TOKEN_KEY, storageArea: {} }, h.deps)).toBe('ignored')
    expect(h.calls).toEqual({ adopt: 0, ended: 0 })
  })
})

describe('the request credential, executed', () => {
  it('a bare request gets the CURRENT stored token', async () => {
    const { applyRequestCredential } = await import('@/utils/platformSession')
    const cfg = applyRequestCredential({ url: '/api/x' }, () => 'CUR')
    expect(cfg.headers.Authorization).toBe('Bearer CUR')
  })

  it('an explicit header wins — the logout revoke depends on it (#2258)', async () => {
    const { applyRequestCredential } = await import('@/utils/platformSession')
    const cfg = applyRequestCredential({ headers: { Authorization: 'Bearer REVOKING' } }, () => 'CUR')
    expect(cfg.headers.Authorization).toBe('Bearer REVOKING')
    const lower = applyRequestCredential({ headers: { authorization: 'Bearer x' } }, () => 'CUR')
    expect(lower.headers.Authorization).toBeUndefined()
  })

  it('no stored token means no header, not a "Bearer null"', async () => {
    const { applyRequestCredential } = await import('@/utils/platformSession')
    const cfg = applyRequestCredential({}, () => null)
    expect(cfg.headers.Authorization).toBeUndefined()
  })
})

describe('adopting the browser session drops any in-memory credential copy (W2)', () => {
  it('adoptStoredSession and applySessionEndedElsewhere both delete the axios default', async () => {
    const axios = (await import('axios')).default
    const { useAuthStore } = await import('@/stores/auth')
    const auth = useAuthStore()
    axios.defaults.headers.common['Authorization'] = 'Bearer STALE-FROM-A-PREVIOUS-BUILD'
    localStorage.setItem('token', 'NEW')
    auth.adoptStoredSession()
    expect(axios.defaults.headers.common['Authorization']).toBeUndefined()
    axios.defaults.headers.common['Authorization'] = 'Bearer STALE-AGAIN'
    auth.applySessionEndedElsewhere()
    expect(axios.defaults.headers.common['Authorization']).toBeUndefined()
  })
})

describe('one credential source, one handler (source guards — wiring only)', () => {
  it('NOTHING in src/frontend/src writes the axios defaults Authorization copy', () => {
    // Walks the whole tree. Axios merges this default into every request BEFORE
    // the interceptor chain runs, so one writer anywhere makes
    // applyRequestCredential inert for the life of the tab — which is exactly
    // what App.vue:64 did while the previous version of this guard read
    // auth.js alone.
    const writers = TREE
      .filter(({ src }) => /axios\.defaults\.headers\.common\[['"]Authorization['"]\]\s*=/.test(src))
      .map(({ file }) => file)
    expect(writers).toEqual([])
    // Only deletes remain, and they are the belt in the two sync actions + logout.
    expect(AUTH.match(/delete axios\.defaults\.headers\.common\['Authorization'\]/g)?.length).toBeGreaterThanOrEqual(3)
    expect(APP).not.toContain('axios.defaults')
  })

  it('one reader: nothing outside platformSession.js reads the token key from storage directly', () => {
    const readers = TREE
      .filter(({ file }) => file !== 'utils/platformSession.js')
      .filter(({ src }) => /localStorage\.getItem\(['"]token['"]\)/.test(src))
      .map(({ file }) => file)
    expect(readers).toEqual([])
  })

  it('main.js wires the executed reactions and nothing else', () => {
    expect(MAIN).toContain('axios.interceptors.request.use((config) => applyRequestCredential(config))')
    expect(MAIN).toContain('reactToPlatformUnauthorized(error, {')
    expect(MAIN).toContain('reactToStorageEvent(event, {')
    expect(MAIN).toContain('setPlatformUnauthorizedHandler(handlePlatformUnauthorized)')
    // The handler RETURNS the navigation (review C4).
    expect(MAIN).toContain('return navigation')
    // No private copy of the verdict or the reaction survives in main.js.
    expect(MAIN).not.toContain('sessionLostVerdict(')
    expect(MAIN).not.toContain("router.push('/login')\n}")
  })

  it('the Workspace veto reads the per-tab store, not shared storage (W1)', () => {
    expect(MAIN).toContain('useClientPortalStore().portalToken')
    expect(MAIN).not.toContain('localStorage.getItem(PORTAL_TOKEN_KEY)')
  })

  it('all three 401 sites report to the single handler', () => {
    expect(API).toContain('notifyPlatformUnauthorized(error)')
    expect(MAIN).toContain('notifyPlatformUnauthorized(error)')
    expect(PORTAL).toContain('notifyPlatformUnauthorized(error)')
  })

  it('none of them carries a private copy of the bounce predicate', () => {
    for (const src of [API, MAIN]) {
      expect(src).not.toContain('const internalSession =')
      expect(src).not.toMatch(/!onWorkspace \|\| internalSession/)
    }
  })

  it('the api.js 401 path no longer hard-reloads or half-clears', () => {
    expect(API).not.toContain("window.location.href = '/login'")
    expect(API).not.toContain("localStorage.removeItem('token')")
    expect(API).toContain('readStoredToken()')
  })

  it('the reaction does not hold the user on a dead page for the revoke', () => {
    expect(MAIN).not.toContain('await authStore.logout()')
    expect(AUTH).toContain('headers: { Authorization: `Bearer ${revoking}` }')
  })

  it('a throwing reaction never replaces the error being rejected', async () => {
    const { setPlatformUnauthorizedHandler, notifyPlatformUnauthorized } =
      await import('@/utils/platformSession')
    setPlatformUnauthorizedHandler(() => { throw new Error('boom') })
    expect(() => notifyPlatformUnauthorized({})).not.toThrow()
    setPlatformUnauthorizedHandler(null)
  })
})
