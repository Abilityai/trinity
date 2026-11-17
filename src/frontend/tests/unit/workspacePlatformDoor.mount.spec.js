// @vitest-environment jsdom
/**
 * #3922 — a platform account gets the same Workspace whichever door it uses.
 *
 * Ruling (option b): the Workspace's code page gives a platform account no
 * client session. When its code verifies, the backend answers 409
 * `platform_login_required` and the Workspace sends the person to `/login`
 * with a return path, where the platform login gives them the team view.
 *
 * Pinned here, each through the code that runs it:
 *   1. every platform login ends a leftover Workspace client sign-in in this
 *      browser, so the Workspace sends the platform JWT, not the stale portal
 *      token (`authHeader` prefers a portal token whenever one is held);
 *   2. `Portal.vue`'s sign-in, on that 409, routes to `/login?redirect=…&email=…`;
 *   3. `/login` fills the email it was handed and says why the person is there.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, shallowMount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'

vi.hoisted(() => {
  window.matchMedia = window.matchMedia || ((q) => ({
    matches: false, media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
  }))
})

vi.mock('axios', () => {
  const mk = () => ({
    get: vi.fn(async () => ({ data: {} })), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  })
  return {
    default: Object.assign(
      { get: vi.fn(async () => ({ data: { username: 'member', role: 'user' } })), post: vi.fn(), put: vi.fn(), delete: vi.fn(), create: mk },
      { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
        defaults: { headers: { common: {} } } },
    ),
  }
})

import { useAuthStore } from '@/stores/auth'
import { useClientPortalStore, PORTAL_TOKEN_KEY } from '@/stores/clientPortal'
import Portal from '@/views/Portal.vue'
import Login from '@/views/Login.vue'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

const Blank = { template: '<div />' }
// The real input focuses its first box after a failed verify; the stub keeps that seam.
const CodeInputStub = {
  name: 'PortalCodeInput', props: ['modelValue'], emits: ['update:modelValue', 'complete'],
  methods: { focusFirst() {} }, template: '<div />',
}
function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: Blank },
      { path: '/workspace', component: Blank },
      { path: '/workspace/a/:agentName', component: Blank },
      { path: '/login', component: Blank },
    ],
  })
}

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  setActivePinia(createPinia())
})

describe('#3922 — a platform login supersedes a leftover Workspace client sign-in', () => {
  it('clears the stale portal token, so the Workspace sends the platform JWT', async () => {
    localStorage.setItem(PORTAL_TOKEN_KEY, 'stale-portal-token')
    const portal = useClientPortalStore()
    expect(portal.portalToken).toBe('stale-portal-token')

    await useAuthStore()._finalizeLogin('platform-jwt', { username: 'member', email: 'member@example.com' })

    expect(localStorage.getItem(PORTAL_TOKEN_KEY)).toBeNull()
    expect(portal.portalToken).toBeNull()
    expect(portal.isPlatformSession).toBe(true)
    expect(portal.authHeader).toEqual({ Authorization: 'Bearer platform-jwt' })
  })
})

describe('#3922 — the Workspace code page sends a platform account to /login', () => {
  async function bootSignIn(path) {
    const router = makeRouter()
    await router.push(path)
    await router.isReady()
    const store = useClientPortalStore()
    store.requestCode = vi.fn(async () => {})
    store.verifyCode = vi.fn(async () => {
      const err = new Error('conflict')
      err.response = { status: 409, data: { detail: { reason: 'platform_login_required' } } }
      throw err
    })
    const w = shallowMount(Portal, { global: { plugins: [router], stubs: { PortalCodeInput: CodeInputStub } } })
    await flushPromises()
    return { w, router, store }
  }

  async function signIn(w) {
    await w.get('input[type="email"]').setValue('Member@Example.com')
    await w.get('form').trigger('submit')
    await flushPromises()
    const codeInput = w.findComponent(CodeInputStub)
    codeInput.vm.$emit('update:modelValue', '123456')
    await flushPromises()
    codeInput.vm.$emit('complete')
    await flushPromises()
  }

  it('routes to /login with the address and a return path to the Workspace', async () => {
    const { w, router, store } = await bootSignIn('/workspace')
    await signIn(w)
    expect(store.verifyCode).toHaveBeenCalledWith('member@example.com', '123456')
    expect(router.currentRoute.value.path).toBe('/login')
    expect(router.currentRoute.value.query).toEqual({ redirect: '/workspace', email: 'member@example.com' })
    expect(store.portalToken).toBeNull()
  })

  it('keeps the page the person asked for as the return path', async () => {
    const { w, router } = await bootSignIn('/workspace/a/scout')
    await signIn(w)
    expect(router.currentRoute.value.query.redirect).toBe('/workspace/a/scout')
  })

  it('a wrong code still reads as a wrong code and stays on the form', async () => {
    const { w, router, store } = await bootSignIn('/workspace')
    store.verifyCode = vi.fn(async () => {
      const err = new Error('nope')
      err.response = { status: 401, data: { detail: 'Invalid or expired code' } }
      throw err
    })
    await signIn(w)
    expect(router.currentRoute.value.path).toBe('/workspace')
    expect(w.text()).toContain('Invalid or expired code.')
  })
})

describe('#3922 — /login takes over the hand-off', () => {
  async function bootLogin(query) {
    const router = makeRouter()
    await router.push({ path: '/login', query })
    await router.isReady()
    const auth = useAuthStore()
    auth.modeDetected = true
    auth.isLoading = false
    auth.emailAuthEnabled = true
    auth.emailCodeLoginEnabled = true
    auth.fetchSsoProviders = vi.fn(async () => [])
    const w = mount(Login, { global: { plugins: [router], stubs: { TrinityMark: true } } })
    await flushPromises()
    return w
  }

  it('fills the address and says why the person is here', async () => {
    const w = await bootLogin({ email: 'member@example.com', redirect: '/workspace' })
    expect(w.get('#email').element.value).toBe('member@example.com')
    expect(w.get('[data-testid="login-workspace-handoff"]').text())
      .toBe('This email has a Trinity account. Sign in here to open your Workspace.')
  })

  it('an ordinary visit shows no hand-off line and an empty field', async () => {
    const w = await bootLogin({})
    expect(w.get('#email').element.value).toBe('')
    expect(w.find('[data-testid="login-workspace-handoff"]').exists()).toBe(false)
  })
})
