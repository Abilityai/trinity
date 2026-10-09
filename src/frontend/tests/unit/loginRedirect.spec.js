// @vitest-environment jsdom
/**
 * #3406 — "Sign in" on a page that offers its own sign-in comes back to that page.
 *
 * A shared canvas behind an `authorized` link answers 401 to a signed-out
 * visitor and shows "Sign in to view this canvas", whose link carries
 * `/login?redirect=<the canvas>`. `Login.vue` read no such query: every
 * success path pushed `/`, so the visitor signed in and landed on the
 * dashboard with the canvas lost.
 *
 * The value arrives in the URL, so anyone can craft it. `safeRedirect` honours
 * an in-app path only and falls back to the dashboard for everything else —
 * executed below as a table, and through the mounted page's real success paths.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

const nav = vi.hoisted(() => ({ query: {}, push: null }))

vi.mock('axios', () => {
  const instance = {
    get: vi.fn(async () => ({ data: {} })),
    post: vi.fn(async () => ({ data: {} })),
    defaults: { headers: { common: {} } },
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
  }
  return { default: { ...instance, create: vi.fn(() => instance) } }
})
vi.mock('vue-router', () => ({
  useRoute: () => ({ query: nav.query, params: {} }),
  useRouter: () => ({ push: nav.push, replace: vi.fn() }),
}))

import Login from '../../src/views/Login.vue'
import { useAuthStore } from '../../src/stores/auth'
import { safeRedirect } from '../../src/utils/safeRedirect'

const CANVAS = '/canvas/s/tok_1'

describe('safeRedirect — only an in-app path is honoured', () => {
  it.each([
    [CANVAS, CANVAS],
    [`${CANVAS}?print=1#block-2`, `${CANVAS}?print=1#block-2`],
    ['/agents/scout', '/agents/scout'],
  ])('keeps %s', (raw, expected) => {
    expect(safeRedirect(raw)).toBe(expected)
  })

  it.each([
    ['nothing', undefined],
    ['an empty value', ''],
    ['a repeated parameter (an array)', ['/agents/a', '/agents/b']],
    ['an absolute URL', 'https://evil.example/'],
    ['a scheme', 'javascript:alert(1)'],
    ['a protocol-relative URL', '//evil.example'],
    ['a backslash variant browsers read as protocol-relative', '/\\evil.example'],
    ['a backslash anywhere', '/agents\\..\\x'],
    ['a control character', '/agents/\tx'],
    ['a relative path', 'canvas/s/tok_1'],
    ['the login page itself', '/login'],
    ['the login page with a query', '/login?redirect=/x'],
  ])('falls back to the dashboard for %s', (_label, raw) => {
    expect(safeRedirect(raw)).toBe('/')
  })
})

describe('Login sends the person back where they were', () => {
  let wrapper
  let authStore

  async function mountLogin({ redirect, authenticated = false } = {}) {
    nav.query = redirect === undefined ? {} : { redirect }
    authStore = useAuthStore()
    authStore.modeDetected = true
    authStore.isLoading = false
    authStore.isAuthenticated = authenticated
    vi.spyOn(authStore, 'fetchSsoProviders').mockResolvedValue([])
    wrapper = mount(Login, { global: { stubs: { QrCode: true } } })
    for (let i = 0; i < 3; i++) { await nextTick(); await flushPromises() }
    return wrapper
  }

  beforeEach(() => {
    setActivePinia(createPinia())
    nav.push = vi.fn()
  })
  afterEach(() => {
    wrapper?.unmount()
    wrapper = null
    vi.restoreAllMocks()
  })

  it('a password sign-in returns to the canvas', async () => {
    const w = await mountLogin({ redirect: CANVAS })
    vi.spyOn(authStore, 'loginWithCredentials').mockResolvedValue(true)
    await w.vm.handleAdminLogin()
    expect(nav.push).toHaveBeenCalledWith(CANVAS)
  })

  it('an email-code sign-in returns to the canvas', async () => {
    const w = await mountLogin({ redirect: CANVAS })
    vi.spyOn(authStore, 'verifyEmailCode').mockResolvedValue(true)
    await w.vm.handleVerifyCode()
    expect(nav.push).toHaveBeenCalledWith(CANVAS)
  })

  it('a second factor completed on the same page returns to the canvas', async () => {
    const w = await mountLogin({ redirect: CANVAS })
    vi.spyOn(authStore, 'verifyMfaCode').mockResolvedValue(true)
    await w.vm.handleMfaVerify()
    expect(nav.push).toHaveBeenCalledWith(CANVAS)
  })

  it('a forced 2FA enrollment with no recovery codes to show returns to the canvas', async () => {
    const w = await mountLogin({ redirect: CANVAS })
    vi.spyOn(authStore, 'confirmMfaEnrollment').mockResolvedValue({ ok: true, recoveryCodes: [] })
    await w.vm.handleMfaEnrollConfirm()
    expect(nav.push).toHaveBeenCalledWith(CANVAS)
  })

  it('after the recovery codes are shown, Continue returns to the canvas', async () => {
    const w = await mountLogin({ redirect: CANVAS })
    vi.spyOn(authStore, 'confirmMfaEnrollment').mockResolvedValue({ ok: true, recoveryCodes: ['a1b2', 'c3d4'] })
    await w.vm.handleMfaEnrollConfirm()
    expect(nav.push).not.toHaveBeenCalled()      // the codes are on screen first
    w.vm.finishMfa()
    expect(nav.push).toHaveBeenCalledWith(CANVAS)
  })

  it('someone already signed in goes straight there', async () => {
    await mountLogin({ redirect: CANVAS, authenticated: true })
    expect(nav.push).toHaveBeenCalledWith(CANVAS)
  })

  it('a crafted off-site target lands on the dashboard instead', async () => {
    const w = await mountLogin({ redirect: '//evil.example' })
    vi.spyOn(authStore, 'loginWithCredentials').mockResolvedValue(true)
    await w.vm.handleAdminLogin()
    expect(nav.push).toHaveBeenCalledWith('/')
    expect(nav.push).not.toHaveBeenCalledWith('//evil.example')
  })

  it('no redirect is the dashboard, as before', async () => {
    const w = await mountLogin()
    vi.spyOn(authStore, 'loginWithCredentials').mockResolvedValue(true)
    await w.vm.handleAdminLogin()
    expect(nav.push).toHaveBeenCalledWith('/')
  })

  it('a failed sign-in goes nowhere', async () => {
    const w = await mountLogin({ redirect: CANVAS })
    vi.spyOn(authStore, 'loginWithCredentials').mockResolvedValue(false)
    await w.vm.handleAdminLogin()
    expect(nav.push).not.toHaveBeenCalled()
  })
})

describe('A Workspace-only member lands in the Workspace (trinity-enterprise#837)', () => {
  let wrapper
  let authStore

  async function mountAs(role, { redirect, authenticated = false } = {}) {
    nav.query = redirect === undefined ? {} : { redirect }
    authStore = useAuthStore()
    authStore.modeDetected = true
    authStore.isLoading = false
    authStore.isAuthenticated = authenticated
    authStore.user = { email: 'member@example.com', role }
    vi.spyOn(authStore, 'fetchSsoProviders').mockResolvedValue([])
    wrapper = mount(Login, { global: { stubs: { QrCode: true } } })
    for (let i = 0; i < 3; i++) { await nextTick(); await flushPromises() }
    return wrapper
  }

  beforeEach(() => {
    setActivePinia(createPinia())
    nav.push = vi.fn()
  })
  afterEach(() => {
    wrapper?.unmount()
    wrapper = null
    vi.restoreAllMocks()
  })

  it('an email-code sign-in by a `user` goes to the Workspace, not the dashboard', async () => {
    const w = await mountAs('user')
    vi.spyOn(authStore, 'verifyEmailCode').mockResolvedValue(true)
    await w.vm.handleVerifyCode()
    expect(nav.push).toHaveBeenCalledWith({ path: '/workspace' })
  })

  it("an operator page in ?redirect= opens that agent's Workspace conversation instead", async () => {
    const w = await mountAs('user', { redirect: '/agents/scout' })
    vi.spyOn(authStore, 'loginWithCredentials').mockResolvedValue(true)
    await w.vm.handleAdminLogin()
    expect(nav.push).toHaveBeenCalledWith({ path: '/workspace', query: { agent: 'scout' } })
  })

  it('a shared canvas in ?redirect= is still honoured for a member', async () => {
    const w = await mountAs('user', { redirect: CANVAS })
    vi.spyOn(authStore, 'verifyEmailCode').mockResolvedValue(true)
    await w.vm.handleVerifyCode()
    expect(nav.push).toHaveBeenCalledWith(CANVAS)
  })

  it('a member already signed in goes straight to the Workspace', async () => {
    await mountAs('user', { authenticated: true })
    expect(nav.push).toHaveBeenCalledWith({ path: '/workspace' })
  })

  it('an operator still lands on the dashboard', async () => {
    const w = await mountAs('operator')
    vi.spyOn(authStore, 'verifyEmailCode').mockResolvedValue(true)
    await w.vm.handleVerifyCode()
    expect(nav.push).toHaveBeenCalledWith('/')
  })

  it('the sign-in page points a person an agent was shared with to the Workspace', async () => {
    const w = await mountAs(undefined)
    const link = w.find('[data-testid="login-workspace-pointer"]')
    expect(link.exists()).toBe(true)
    expect(link.attributes('href')).toBe('/workspace')
  })
})
