// @vitest-environment jsdom
/**
 * #3444 / #3466 — the way to /login carries the page, and signing in returns there.
 *
 * `Login.vue` has honoured `?redirect=` since #3406 (through `safeRedirect` and
 * the role-aware `landingAfterSignIn`), but nothing that SENDS a person to the
 * login page wrote one: the router guard returned a bare `'/login'` for a
 * logged-out deep link, and the one 401 reaction pushed a bare `'/login'` for a
 * session that died mid-use — on an operator page and on the Workspace alike.
 * Both now build the location with `loginLocation()`. And a signed-in visit to
 * `/login?redirect=X` used to land on `/` with X discarded.
 *
 * Driven through the app's REAL router (`router/index.js`, its guard included)
 * and the real `Login.vue`: `loginRedirect.spec.js` mocks `vue-router`, so its
 * "already signed in goes straight there" never meets the guard. The 401
 * reaction is composed here exactly as `main.js` composes it (that wiring is
 * pinned in `platformSessionSync.spec.js`, beside the other `main.js` pins),
 * with the real auth store doing the logout.
 *
 * The real cases, not the issue's synthetic repro (deleting only `token` from
 * storage): a token that is dead on the server, a reload after the session
 * ended, a deep link while signed out. And #3406's rule is pinned beside them:
 * a public link's visitor is never sent to /login.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { START_LOCATION } from 'vue-router'

vi.mock('axios', () => {
  const instance = {
    get: vi.fn(async () => ({ data: {} })),
    post: vi.fn(async () => ({ data: {} })),
    defaults: { headers: { common: {} } },
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
  }
  return { default: { ...instance, create: vi.fn(() => instance) } }
})
const Blank = { default: { render: () => null } }
vi.mock('../../src/views/Portal.vue', () => Blank)
vi.mock('../../src/views/AgentDetail.vue', () => Blank)
vi.mock('../../src/views/Dashboard.vue', () => Blank)
vi.mock('../../src/views/Library.vue', () => Blank)
vi.mock('../../src/views/Settings.vue', () => Blank)
vi.mock('../../src/views/PublicChat.vue', () => Blank)
vi.mock('../../src/views/SharedCanvas.vue', () => Blank)

import router from '../../src/router'
import Login from '../../src/views/Login.vue'
import { useAuthStore } from '../../src/stores/auth'
import {
  fullPathForVerdict, loginLocation, pathForVerdict, reactToPlatformUnauthorized,
} from '../../src/utils/platformSession'

const here = () => router.currentRoute.value.fullPath
const failedWith = (token, headers = { 'www-authenticate': 'Bearer' }) => ({
  config: { headers: { Authorization: `Bearer ${token}` } },
  response: { status: 401, headers },
})

function signedIn(role = 'admin', token = 'CUR') {
  localStorage.setItem('token', token)
  localStorage.setItem('auth0_user', JSON.stringify({ email: 'op@example.com', role }))
  const auth = useAuthStore()
  auth.isLoading = false
  auth.isAuthenticated = true
  auth.token = token
  auth.user = { email: 'op@example.com', role }
  auth.profileVerified = true
  return auth
}

function signedOut() {
  localStorage.clear()
  const auth = useAuthStore()
  auth.isLoading = false
  auth.isAuthenticated = false
  auth.token = null
  auth.user = null
  return auth
}

// The handler main.js registers, with the same collaborators — the real
// router, the real store — so what runs is the reaction and the guard together.
function report401(error, { portalTokenPresent = false } = {}) {
  const auth = useAuthStore()
  return reactToPlatformUnauthorized(error, {
    path: pathForVerdict(router, START_LOCATION, window.location.pathname),
    returnPath: fullPathForVerdict(router, START_LOCATION, window.location),
    portalTokenPresent,
    adoptStoredSession: () => auth.adoptStoredSession(),
    logout: () => auth.logout(),
    goToLogin: (location) => router.push(location),
  })
}

let loginPage = null
async function signInOnLoginPage(role = 'admin') {
  const auth = useAuthStore()
  auth.modeDetected = true
  vi.spyOn(auth, 'fetchSsoProviders').mockResolvedValue([])
  const w = mount(Login, { global: { plugins: [router], stubs: { QrCode: true } } })
  await flushPromises()
  vi.spyOn(auth, 'loginWithCredentials').mockImplementation(async () => {
    signedIn(role, 'FRESH')
    return true
  })
  await w.vm.handleAdminLogin()
  await flushPromises()
  // Unmounted in afterEach, after the assertions: unmounting the last app that
  // installed the router resets `currentRoute` to START_LOCATION ("/").
  loginPage = w
}

beforeEach(async () => {
  setActivePinia(createPinia())
  globalThis.fetch = vi.fn(async () => ({ json: async () => ({ setup_completed: true }) }))
  // A neutral public page between tests: the router is the app's singleton.
  signedOut()
  await router.push('/workspace')
})
afterEach(() => {
  loginPage?.unmount()
  loginPage = null
  vi.restoreAllMocks()
  localStorage.clear()
})

describe('loginLocation — the one place a way to /login is built', () => {
  it.each([
    ['/agents/test-echo?tab=files'],
    ['/library'],
    ['/settings?tab=general#keys'],
    ['/workspace/c/s_1'],
    ['/workspace/a/scout?panel=work'],
    ['/?view=list'],
    ['/?tags=team-a'],
  ])('carries %s', (path) => {
    expect(loginLocation(path)).toEqual({ path: '/login', query: { redirect: path } })
  })

  it.each([
    ['the dashboard', '/'],
    ['nothing', ''],
    ['undefined', undefined],
    ['the login page', '/login'],
    ['the login page with its own redirect', '/login?redirect=/library'],
    ['the setup page', '/setup'],
    ['the mobile admin', '/m'],
    ['the mobile admin with a query', '/m?tab=queue'],
    ['an off-site address', '//evil.example/x'],
    ['an absolute URL', 'https://evil.example/'],
  ])('leaves out %s', (_label, path) => {
    expect(loginLocation(path)).toEqual({ path: '/login' })
  })
})

describe('fullPathForVerdict — the page a 401 happened on, query and hash included', () => {
  const location = { pathname: '/agents/scout', search: '?tab=files', hash: '#log' }

  it('reads the address bar until the first navigation settles (START_LOCATION reads "/")', () => {
    const router = { currentRoute: { value: START_LOCATION } }
    expect(fullPathForVerdict(router, START_LOCATION, location)).toBe('/agents/scout?tab=files#log')
  })

  it('reads the router once it has settled', () => {
    const router = { currentRoute: { value: { path: '/library', fullPath: '/library?tab=skills' } } }
    expect(fullPathForVerdict(router, START_LOCATION, location)).toBe('/library?tab=skills')
  })
})

describe('#3444 — a logged-out deep link', () => {
  it('reaches /login with the page it asked for, and signing in returns there', async () => {
    await router.push('/agents/test-echo?tab=files')
    expect(router.currentRoute.value.path).toBe('/login')
    expect(router.currentRoute.value.query).toEqual({ redirect: '/agents/test-echo?tab=files' })

    await signInOnLoginPage()
    expect(here()).toBe('/agents/test-echo?tab=files')
  })

  // /review of #4069 (reviewer's nit): a dashboard link with a query
  // (`?view=`, `?tags=` — ent#126/ent#260 deep links) is a destination;
  // only the bare dashboard is where a sign-in lands anyway.
  it('a dashboard link with a query keeps it through the sign-in', async () => {
    await router.push('/?view=list')
    expect(router.currentRoute.value.path).toBe('/login')
    expect(router.currentRoute.value.query).toEqual({ redirect: '/?view=list' })

    await signInOnLoginPage()
    expect(here()).toBe('/?view=list')
  })

  it('the dashboard itself reaches a bare /login, as before', async () => {
    await router.push('/')
    expect(here()).toBe('/login')
  })
})

describe('#3444 — a session that ends mid-use (a 401 on the stored token)', () => {
  it('on an operator page: /login carries the page, and signing in returns there', async () => {
    signedIn('admin', 'CUR')
    await router.push('/library?tab=skills')

    const { verdict, navigation } = report401(failedWith('CUR'))
    await navigation
    expect(verdict).toBe('logout')
    expect(router.currentRoute.value.path).toBe('/login')
    expect(router.currentRoute.value.query).toEqual({ redirect: '/library?tab=skills' })
    expect(localStorage.getItem('token')).toBeNull()

    await signInOnLoginPage()
    expect(here()).toBe('/library?tab=skills')
  })

  it('a 401 that lands before the first navigation settles still names the page being loaded', () => {
    const goToLogin = vi.fn()
    signedIn('admin', 'CUR')
    reactToPlatformUnauthorized(failedWith('CUR'), {
      path: pathForVerdict({ currentRoute: { value: START_LOCATION } }, START_LOCATION, '/settings'),
      returnPath: fullPathForVerdict(
        { currentRoute: { value: START_LOCATION } }, START_LOCATION,
        { pathname: '/settings', search: '?tab=general', hash: '' },
      ),
      adoptStoredSession: vi.fn(),
      logout: vi.fn(),
      goToLogin,
    })
    expect(goToLogin).toHaveBeenCalledWith({ path: '/login', query: { redirect: '/settings?tab=general' } })
  })
})

describe('#3466 — an operator whose platform session ends inside the Workspace', () => {
  it('a dead token is sent to /login with the Workspace page, and signing in returns there', async () => {
    signedIn('admin', 'CUR')
    await router.push('/workspace/c/s_42')

    const { verdict, navigation } = report401(failedWith('CUR'))
    await navigation
    expect(verdict).toBe('logout')
    expect(router.currentRoute.value.path).toBe('/login')
    expect(router.currentRoute.value.query).toEqual({ redirect: '/workspace/c/s_42' })

    await signInOnLoginPage()
    expect(here()).toBe('/workspace/c/s_42')
  })

  it("a client's tab (a portal session) is still left alone, as #2791 rules", async () => {
    signedIn('admin', 'CUR')
    await router.push('/workspace/c/s_42')
    const goToLogin = vi.spyOn(router, 'push')
    const { verdict } = report401(failedWith('CUR'), { portalTokenPresent: true })
    expect(verdict).toBe('ignore')
    expect(goToLogin).not.toHaveBeenCalled()
    expect(here()).toBe('/workspace/c/s_42')
  })
})

describe('#3406 stands — a public link visitor is never sent to /login', () => {
  it('an expired link session (a bare 401) on /chat/:token stays on the page', async () => {
    await router.push('/chat/tok_1')
    const push = vi.spyOn(router, 'push')
    const { verdict } = report401(failedWith('LINK', { 'content-type': 'application/json' }))
    expect(verdict).toBe('ignore')
    expect(push).not.toHaveBeenCalled()
    expect(here()).toBe('/chat/tok_1')
  })

  it("a dead operator token on the visitor's browser ends in place, without navigating", async () => {
    signedIn('admin', 'CUR')
    await router.push('/chat/tok_1')
    const push = vi.spyOn(router, 'push')
    const { verdict } = report401(failedWith('CUR'))
    expect(verdict).toBe('logout-in-place')
    expect(push).not.toHaveBeenCalled()
    expect(here()).toBe('/chat/tok_1')
    expect(localStorage.getItem('token')).toBeNull()
  })

  it('the same on a shared canvas', async () => {
    signedIn('admin', 'CUR')
    await router.push('/canvas/s/tok_2')
    const push = vi.spyOn(router, 'push')
    expect(report401(failedWith('CUR')).verdict).toBe('logout-in-place')
    expect(push).not.toHaveBeenCalled()
    expect(here()).toBe('/canvas/s/tok_2')
  })
})

describe('a signed-in visit to /login?redirect=X (the guard used to drop X)', () => {
  it('lands on X', async () => {
    signedIn('admin')
    await router.push({ path: '/login', query: { redirect: '/agents/scout?tab=files' } })
    expect(here()).toBe('/agents/scout?tab=files')
  })

  it('lands on the dashboard when X is off-site', async () => {
    signedIn('admin')
    await router.push({ path: '/login', query: { redirect: '//evil.example' } })
    expect(here()).toBe('/')
  })

  it("a Workspace-only member asking for an operator page opens that agent's Workspace conversation", async () => {
    signedIn('user')
    await router.push({ path: '/login', query: { redirect: '/agents/scout' } })
    expect(here()).toBe('/workspace?agent=scout')
  })
})
