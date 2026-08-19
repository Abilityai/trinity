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
  // #2261 stores its per-tab suppression marker in sessionStorage — same shim,
  // separate map, because the two must not alias (the whole point of the marker
  // is that it is scoped to this tab, not this browser).
  const session = new Map()
  globalThis.sessionStorage = {
    getItem: (k) => (session.has(k) ? session.get(k) : null),
    setItem: (k, v) => session.set(k, String(v)),
    removeItem: (k) => session.delete(k),
    clear: () => session.clear(),
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
  // #2258: `logout()` models the real one — it ends the platform session. It is
  // a spy so a test can assert WHETHER it ran (a portal-only sign-out must not
  // touch a platform session that isn't there) and WHEN (before the portal
  // state clears, never after).
  const logout = vi.fn(async () => { authed.value = false })
  return {
    useAuthStore: () => ({
      get isAuthenticated() { return authed.value },
      get authHeader() { return authed.value ? { Authorization: 'Bearer platform-jwt' } : {} },
      logout,
    }),
    // Test-only handle: mutation must go through the SAME ref the getters read,
    // or Vue never invalidates the computed and the sign-out case passes for
    // the wrong reason.
    __setAuthed: (v) => { authed.value = v },
    __logout: logout,
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
  // #2261: `create()` must return a FRESH instance per call, like real axios.
  // With one shared instance, `api.js`'s PERF-269 dedupe wrapper (which replaces
  // `.get` on the instance it creates) also replaced the workspace store's
  // `.get` — a plain function with no mock helpers — so a stub on it silently
  // was not the code path. Two consumers, two instances.
  const mkInstance = () => ({
    get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  })
  const globalInterceptors = { request: { use: vi.fn() }, response: { use: vi.fn() } }
  return {
    default: Object.assign(
      { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), create: mkInstance },
      { interceptors: globalInterceptors, defaults: { headers: { common: {} } } },
    ),
  }
})

import {
  useClientPortalStore, PLATFORM_LOGIN_ROUTE, portalHttp, setPlatformSessionLostHandler,
} from '@/stores/clientPortal'
import { __setAuthed, __logout } from '@/stores/auth'
import {
  WORKSPACE_ROOT, signOutLabelFor, SIGN_OUT_LABEL_PLATFORM, SIGN_OUT_LABEL_CLIENT,
} from '@/components/portal/portalUtils'

const PORTAL_TOKEN_KEY = 'trinity.portalToken'

describe('workspace session', () => {
  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
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

// #2258 — "Sign out" must produce a signed-out state, for BOTH ways of being
// signed in. `signOut()` cleared only the portal token, and because a platform
// session is an implicit workspace session (the block above), removing the
// portal token is exactly what ACTIVATES the platform fallback: a refresh
// re-entered the Workspace as the operator. The fix ends whichever credential
// is live, in an order that never lets the operator identity be derived
// mid-flight — the whole sequence lives in `signOutEverywhere()` so it can be
// pinned here rather than trapped in a component this suite cannot mount.
describe('signing out of the workspace signs out (#2258)', () => {
  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    __setAuthed(false)
    __logout.mockClear()
    setActivePinia(createPinia())
  })

  it('a platform user signing out ends the PLATFORM session and goes to the platform login (AC #1)', async () => {
    __setAuthed(true)
    const store = useClientPortalStore()
    expect(store.isPlatformSession).toBe(true)

    const target = await store.signOutEverywhere()

    expect(__logout).toHaveBeenCalledTimes(1)
    // No half-signed-out shell: NEITHER way of being signed in survives.
    expect(store.isPlatformSession).toBe(false)
    expect(store.isClientSignedIn).toBe(false)
    expect(store.authHeader).toEqual({})
    expect(target).toBe(PLATFORM_LOGIN_ROUTE)
  })

  it('a client signing out on a browser that also holds a platform login lands on the workspace sign-in, not the operator roster (AC #2)', async () => {
    // The reported variant 2: portal token present AND a platform JWT present.
    // Clearing the portal token alone would re-derive the platform identity.
    localStorage.setItem(PORTAL_TOKEN_KEY, 'portal-token')
    setActivePinia(createPinia())
    __setAuthed(true)
    const store = useClientPortalStore()
    expect(store.isClientSignedIn).toBe(true)
    expect(store.isPlatformSession).toBe(false)   // it was a CLIENT session

    const target = await store.signOutEverywhere()

    // The platform credential is gone too — the fallback has nothing to fall
    // back to, on screen AND on the wire.
    expect(__logout).toHaveBeenCalledTimes(1)
    expect(store.isClientSignedIn).toBe(false)
    expect(store.isPlatformSession).toBe(false)
    expect(store.authHeader).toEqual({})
    expect(localStorage.getItem(PORTAL_TOKEN_KEY)).toBeNull()
    // …and they stay on the Workspace (the OTP form), never the operator /login.
    expect(target).toBe(WORKSPACE_ROOT)
  })

  it('a client with NO platform login signs out without touching the platform (nothing to end)', async () => {
    localStorage.setItem(PORTAL_TOKEN_KEY, 'portal-token')
    setActivePinia(createPinia())
    const store = useClientPortalStore()

    const target = await store.signOutEverywhere()

    expect(__logout).not.toHaveBeenCalled()
    expect(store.isClientSignedIn).toBe(false)
    expect(target).toBe(WORKSPACE_ROOT)
  })

  it('the platform credential is ended BEFORE the portal state clears (no window where the operator identity is derived)', async () => {
    localStorage.setItem(PORTAL_TOKEN_KEY, 'portal-token')
    setActivePinia(createPinia())
    __setAuthed(true)
    const store = useClientPortalStore()

    // At the moment the platform logout runs, the portal token must still be
    // held: `portalToken` gone + `isAuthenticated` still true is exactly the
    // state in which `authHeader` would hand an in-flight poll the operator's
    // credential.
    let portalTokenDuringLogout = 'unset'
    __logout.mockImplementationOnce(async () => {
      portalTokenDuringLogout = store.portalToken
      __setAuthed(false)
    })

    await store.signOutEverywhere()
    expect(portalTokenDuringLogout).toBe('portal-token')
  })

  it('the signed-out state survives a reload — no reappearance on refresh (AC #1, #3)', async () => {
    __setAuthed(true)
    localStorage.setItem(PORTAL_TOKEN_KEY, 'portal-token')
    setActivePinia(createPinia())
    await useClientPortalStore().signOutEverywhere()

    // "Reload": a fresh store hydrates from what persisted. Nothing did.
    setActivePinia(createPinia())
    const fresh = useClientPortalStore()
    expect(fresh.isClientSignedIn).toBe(false)
    expect(fresh.isPlatformSession).toBe(false)
    expect(localStorage.getItem(PORTAL_TOKEN_KEY)).toBeNull()
  })

  it('an EXPIRED portal session does not end a platform session — expiry is not a sign-out', () => {
    // `endSession({expired})` is the 401 path, reached with no user act. It
    // must keep calling the plain state-clearing primitive, never the
    // credential-ending one, or an operator working in another tab is logged
    // out by a client's idle timeout.
    __setAuthed(true)
    localStorage.setItem(PORTAL_TOKEN_KEY, 'portal-token')
    setActivePinia(createPinia())
    const store = useClientPortalStore()

    store.endSession({ expired: true, resumePath: '/workspace/c/abc' })

    expect(__logout).not.toHaveBeenCalled()
    expect(store.sessionExpired).toBe(true)
  })

  it('a user who never signed out keeps the implicit entry (AC #6: existing behaviour unchanged)', () => {
    __setAuthed(true)
    const store = useClientPortalStore()
    expect(store.isClientSignedIn).toBe(true)
    expect(store.isPlatformSession).toBe(true)
    expect(__logout).not.toHaveBeenCalled()
  })

  it('the button says what it does for each principal (AC #5)', () => {
    // A platform user's workspace session IS their platform session, so their
    // button ends it and says so. A client's ends only theirs.
    expect(signOutLabelFor(true)).toBe(SIGN_OUT_LABEL_PLATFORM)
    expect(signOutLabelFor(false)).toBe(SIGN_OUT_LABEL_CLIENT)
    expect(SIGN_OUT_LABEL_PLATFORM).toMatch(/Trinity/)
    // Never "Leave" — that would promise navigation while leaving a live
    // credential in a browser the person just asked to leave.
    expect(SIGN_OUT_LABEL_PLATFORM).not.toMatch(/leave/i)
    expect(SIGN_OUT_LABEL_CLIENT).not.toMatch(/leave/i)
  })
})

describe('workspace roster failures are honest (AC: no dead empty state)', () => {
  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    __setAuthed(true)
    setActivePinia(createPinia())
  })

  it('a 404 reports the module as unavailable, not as an empty share list', async () => {
    // #2261: workspace calls run on `portalHttp` now, so the stub goes there —
    // stubbing the global `axios.get` would silently no longer be the code path.
    const { portalHttp: http } = await import('@/stores/clientPortal')
    http.get.mockRejectedValueOnce({ response: { status: 404 } })

    const store = useClientPortalStore()
    await store.fetchRoster()

    expect(store.unavailable).toBe(true)
    expect(store.agents).toEqual([])
    expect(store.error).toMatch(/not available/i)
  })

  it('a transient failure is reported as a failure, and is retryable', async () => {
    // #2261: workspace calls run on `portalHttp` now, so the stub goes there —
    // stubbing the global `axios.get` would silently no longer be the code path.
    const { portalHttp: http } = await import('@/stores/clientPortal')
    http.get.mockRejectedValueOnce({ response: { status: 503, data: { detail: 'upstream down' } } })

    const store = useClientPortalStore()
    await store.fetchRoster()

    expect(store.unavailable).toBe(false)   // NOT "unavailable on this instance"
    expect(store.error).toBe('upstream down')
  })

  it('an empty roster stays an empty roster', async () => {
    // #2261: workspace calls run on `portalHttp` now, so the stub goes there —
    // stubbing the global `axios.get` would silently no longer be the code path.
    const { portalHttp: http } = await import('@/stores/clientPortal')
    http.get.mockResolvedValueOnce({ data: { client_email: 'a@b.c', agents: [] } })

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
    sessionStorage.clear()
    __setAuthed(true)
    setActivePinia(createPinia())
  })

  it('a successful retry clears the "unavailable" verdict', async () => {
    const { portalHttp: http } = await import('@/stores/clientPortal')
    const store = useClientPortalStore()

    http.get.mockRejectedValueOnce({ response: { status: 404 } })
    await store.fetchRoster()
    expect(store.unavailable).toBe(true)

    // The retry button the unavailable state offers.
    http.get.mockResolvedValueOnce({ data: { client_email: 'a@b.c', agents: [{ name: 'scout' }] } })
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


describe('an expired client session does not become the operator (#2261)', () => {
  // The residual #2258 named and left: expiry cannot end the platform
  // credential (that would log an operator out of another tab over a client's
  // idle timeout), so `endSession` → `signOut` cleared only the portal token —
  // and `isPlatformSession` re-derived from the platform JWT still in the
  // browser. The client's tab silently became the operator's.
  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    __setAuthed(false)
    setActivePinia(createPinia())
  })

  const expireOnAPlatformBrowser = () => {
    __setAuthed(true)                                   // operator logged in here too
    localStorage.setItem(PORTAL_TOKEN_KEY, 'portal-token')
    setActivePinia(createPinia())
    const store = useClientPortalStore()
    store.endSession({ expired: true, resumePath: '/workspace/c/abc' })
    return store
  }

  it('shows the expired sign-in state, not the operator roster (AC #1)', () => {
    const store = expireOnAPlatformBrowser()

    expect(store.sessionExpired).toBe(true)
    expect(store.isPlatformSession).toBe(false)
    expect(store.isClientSignedIn).toBe(false)
  })

  it('and a refresh keeps it there (AC #1)', () => {
    expireOnAPlatformBrowser()

    // "Refresh": a fresh store in the SAME tab hydrates from sessionStorage.
    setActivePinia(createPinia())
    const fresh = useClientPortalStore()
    expect(fresh.platformFallbackSuppressed).toBe(true)
    expect(fresh.isClientSignedIn).toBe(false)
  })

  it("leaves the operator's platform session alone (AC #2)", async () => {
    const store = expireOnAPlatformBrowser()

    // The credential is untouched — only this tab's reading of it changed. An
    // operator in another tab keeps working; that is why suppression exists
    // instead of a logout.
    expect(__logout).not.toHaveBeenCalled()
    expect(store.platformFallbackSuppressed).toBe(true)
  })

  it('sends no credential while the UI says signed out — the explicit half (AC #3)', () => {
    const store = expireOnAPlatformBrowser()
    // `authHeader` is what every call site passes. Before this fix it returned
    // the operator's header whenever no portal token existed.
    expect(store.authHeader).toEqual({})
  })

  it('discards any credential on the config and rebuilds from the store — the implicit half (AC #3)', () => {
    // The reason #2258 rejected a suppression flag: `auth.js` installs the
    // platform JWT as `axios.defaults.headers.common.Authorization`. This
    // interceptor's job is that the store is the ONLY source — so a header
    // already on the config, whatever put it there, must not survive when the
    // store's answer is "no credential".
    //
    // The first version preserved whatever it found, which cannot tell
    // store-decided from inherited. It is now unconditional: delete, then set
    // from the store.
    expireOnAPlatformBrowser()
    const handler = portalHttp.interceptors.request.use.mock.calls[0][0]

    const withInherited = handler({ headers: { Authorization: 'Bearer platform-jwt-from-defaults' } })
    const withNothing = handler({ headers: {} })

    expect(withInherited.headers.Authorization).toBeUndefined()
    expect(withNothing.headers.Authorization).toBeUndefined()
  })

  it('rebuilds the credential the store DOES decide (the fix must not break auth)', () => {
    // Fail-closed is only correct when there is nothing to send. A live client
    // session must still authenticate — otherwise the interceptor would trade a
    // disclosure for a workspace nobody can use.
    localStorage.setItem(PORTAL_TOKEN_KEY, 'portal-token')
    setActivePinia(createPinia())
    useClientPortalStore()
    const handler = portalHttp.interceptors.request.use.mock.calls[0][0]

    const out = handler({ headers: { Authorization: 'Bearer something-stale' } })
    expect(out.headers.Authorization).toBe('Bearer portal-token')
  })

  it('a platform session that never expired here still authenticates (ent#357 unchanged)', () => {
    __setAuthed(true)
    const store = useClientPortalStore()

    expect(store.isPlatformSession).toBe(true)
    expect(store.authHeader).toEqual({ Authorization: 'Bearer platform-jwt' })
  })

  it('the operator can continue explicitly, and only explicitly', () => {
    const store = expireOnAPlatformBrowser()
    expect(store.isClientSignedIn).toBe(false)

    store.continueAsPlatform()

    expect(store.isPlatformSession).toBe(true)
    expect(store.sessionExpired).toBe(false)
    expect(sessionStorage.getItem('trinity.workspaceFallbackSuppressed')).toBeNull()
  })

  it('a client signing in again clears the suppression', async () => {
    const store = expireOnAPlatformBrowser()
    portalHttp.post.mockResolvedValueOnce({ data: { token: 'new-portal-token', email: 'c@example.com' } })

    await store.verifyCode('c@example.com', '123456')

    expect(store.platformFallbackSuppressed).toBe(false)
    expect(store.isClientSignedIn).toBe(true)
  })

  it('a user-initiated sign-out leaves no marker for the next login in this tab', async () => {
    const store = expireOnAPlatformBrowser()
    await store.signOutEverywhere()

    // The credential is gone, so there is nothing to suppress; a stale marker
    // would greet the next legitimate platform login with a needless click.
    expect(store.platformFallbackSuppressed).toBe(false)
    expect(sessionStorage.getItem('trinity.workspaceFallbackSuppressed')).toBeNull()
  })

  it('a client\'s 401 does not bounce the operator to /login (the typo hazard)', () => {
    // The hazard `workspace-session-signout.md` lists as objection 3 to a
    // suppression flag, and the reason the bounce discriminator moved off
    // `localStorage['token']`: on THIS browser a client is at the OTP form while
    // an operator's JWT sits in storage. One mistyped digit 401s — and the old
    // predicate would have logged the operator out over it.
    const store = expireOnAPlatformBrowser()
    const onLost = vi.fn()
    setPlatformSessionLostHandler(onLost)

    const reject = portalHttp.interceptors.response.use.mock.calls[0][1]
    // The arm re-rejects (it is an interceptor, not a handler of last resort);
    // the assertion is about the side effect, so swallow the rejection.
    reject({ response: { status: 401 } }).catch(() => {})

    expect(store.isPlatformSession).toBe(false)
    expect(onLost).not.toHaveBeenCalled()
    setPlatformSessionLostHandler(null)
  })

  it('an OPERATOR whose platform session expires on the workspace still bounces (ent#357)', () => {
    // The other half: moving workspace calls onto their own instance took them
    // out of the global 401 interceptor's reach, so without this the operator
    // would sit on "Failed to load your agents" instead of being sent to /login.
    __setAuthed(true)
    setActivePinia(createPinia())
    const store = useClientPortalStore()
    expect(store.isPlatformSession).toBe(true)

    const onLost = vi.fn()
    setPlatformSessionLostHandler(onLost)
    const reject = portalHttp.interceptors.response.use.mock.calls[0][1]
    reject({ response: { status: 401 } }).catch(() => {})

    expect(onLost).toHaveBeenCalledTimes(1)
    setPlatformSessionLostHandler(null)
  })

  it('keeps the resume target across the re-authentication (the notice promises it)', async () => {
    // `endSession` has recorded `resumePath` since ent#375 and nothing read it
    // back, so the expired notice's "pick up where you left off" was a promise
    // the app did not keep. The store must still be holding it after a
    // successful sign-in, because that is when the view spends it.
    const store = expireOnAPlatformBrowser()
    expect(store.resumePath).toBe('/workspace/c/abc')

    portalHttp.post.mockResolvedValueOnce({ data: { token: 'new-token', email: 'c@example.com' } })
    await store.verifyCode('c@example.com', '123456')

    expect(store.resumePath).toBe('/workspace/c/abc')
    expect(store.sessionExpired).toBe(false)
  })

  it('degrades to the pre-fix behaviour when sessionStorage is unavailable', () => {
    // Private mode. A workspace nobody can enter would be worse than the
    // disclosure this fixes, so the read fails to NOT-suppressed.
    const real = globalThis.sessionStorage
    globalThis.sessionStorage = { getItem() { throw new Error('denied') },
                                  setItem() { throw new Error('denied') },
                                  removeItem() { throw new Error('denied') } }
    try {
      __setAuthed(true)
      setActivePinia(createPinia())
      const store = useClientPortalStore()
      expect(store.platformFallbackSuppressed).toBe(false)
      // In-memory suppression still holds for this page-life…
      store.endSession({ expired: true })
      expect(store.isPlatformSession).toBe(false)
    } finally {
      globalThis.sessionStorage = real
    }
  })
})
