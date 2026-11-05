// @vitest-environment jsdom
/**
 * #4157 — a platform account that starts at the Workspace sign-in enters ONE code.
 *
 * #3922 sends that account from the Workspace's code page to `/login`. Its first
 * code was correct and already spent, so `/login` used to ask for a second one.
 * Now the backend's 409 carries a one-time hand-off token and:
 *   1. `Portal.vue` keeps it in memory (the auth store), never in the URL or storage;
 *   2. `/login` redeems it at `POST /api/auth/email/handoff` and lands on the page
 *      the person asked for, without the code form;
 *   3. a 2FA account still gets its 2FA step;
 *   4. a refused or missing hand-off (reload, expiry) falls back to the ordinary
 *      code form, with the address filled and no error.
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
      { get: vi.fn(async () => ({ data: { username: 'member', email: 'member@example.com', role: 'user' } })), post: vi.fn(), put: vi.fn(), delete: vi.fn(), create: mk },
      { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
        defaults: { headers: { common: {} } } },
    ),
  }
})

import axios from 'axios'
import { useAuthStore } from '@/stores/auth'
import { useClientPortalStore } from '@/stores/clientPortal'
import Portal from '@/views/Portal.vue'
import Login from '@/views/Login.vue'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

const TOKEN = 'handoff-token-abcdefghijklmnopqrstuvwxyz0123456789'
const Blank = { template: '<div />' }
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
  axios.post.mockReset()
  setActivePinia(createPinia())
})

describe('#4157 — the Workspace keeps the hand-off in memory', () => {
  it('holds the token in the auth store and never puts it in the URL or storage', async () => {
    const router = makeRouter()
    await router.push('/workspace/a/scout')
    await router.isReady()
    const store = useClientPortalStore()
    store.requestCode = vi.fn(async () => {})
    store.verifyCode = vi.fn(async () => {
      const err = new Error('conflict')
      err.response = { status: 409, data: { detail: { reason: 'platform_login_required', handoff: TOKEN } } }
      throw err
    })
    const w = shallowMount(Portal, { global: { plugins: [router], stubs: { PortalCodeInput: CodeInputStub } } })
    await flushPromises()
    await w.get('input[type="email"]').setValue('Member@Example.com')
    await w.get('form').trigger('submit')
    await flushPromises()
    const codeInput = w.findComponent(CodeInputStub)
    codeInput.vm.$emit('update:modelValue', '123456')
    await flushPromises()
    codeInput.vm.$emit('complete')
    await flushPromises()

    expect(useAuthStore().workspaceHandoff).toEqual({ email: 'member@example.com', token: TOKEN })
    expect(router.currentRoute.value.path).toBe('/login')
    expect(router.currentRoute.value.fullPath).not.toContain(TOKEN)
    expect(router.currentRoute.value.query).toEqual({ redirect: '/workspace/a/scout', email: 'member@example.com' })
    const stored = JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage })
    expect(stored).not.toContain(TOKEN)
  })
})

describe('#4157 — /login redeems the hand-off instead of asking for a second code', () => {
  async function bootLogin(query, { held = true } = {}) {
    const router = makeRouter()
    await router.push({ path: '/login', query })
    await router.isReady()
    const auth = useAuthStore()
    auth.modeDetected = true
    auth.isLoading = false
    auth.emailAuthEnabled = true
    auth.emailCodeLoginEnabled = true
    auth.fetchSsoProviders = vi.fn(async () => [])
    if (held) auth.setWorkspaceHandoff('member@example.com', TOKEN)
    const w = mount(Login, { global: { plugins: [router], stubs: { TrinityMark: true } } })
    await flushPromises()
    return { w, router, auth }
  }

  const QUERY = { email: 'member@example.com', redirect: '/workspace/a/scout' }

  it('signs in with the held token and lands on the page the person asked for', async () => {
    axios.post.mockImplementation(async (url) => {
      if (url === '/api/auth/email/handoff') {
        return { data: { access_token: 'platform-jwt', user: { username: 'member', email: 'member@example.com', role: 'user' } } }
      }
      throw new Error(`unexpected POST ${url}`)
    })
    const { router, auth } = await bootLogin(QUERY)

    expect(axios.post).toHaveBeenCalledTimes(1)
    expect(axios.post).toHaveBeenCalledWith('/api/auth/email/handoff', { email: 'member@example.com', token: TOKEN, client: 'web' })
    expect(auth.isAuthenticated).toBe(true)
    expect(auth.workspaceHandoff).toBeNull()
    expect(router.currentRoute.value.fullPath).toBe('/workspace/a/scout')
  })

  it('an account with 2FA still gets its 2FA step', async () => {
    axios.post.mockResolvedValue({ data: { mfa_required: true, mfa_token: 'c', enrolled: true, enrollment_required: false } })
    const { w, router, auth } = await bootLogin(QUERY)

    expect(auth.isAuthenticated).toBe(false)
    expect(auth.mfaChallenge).not.toBeNull()
    expect(w.text()).toContain('Two-factor authentication')
    expect(router.currentRoute.value.path).toBe('/login')
  })

  it('a refused hand-off falls back to the code form, filled in and without an error', async () => {
    axios.post.mockRejectedValue(Object.assign(new Error('401'), { response: { status: 401, data: { detail: 'Invalid or expired verification code' } } }))
    const { w, router, auth } = await bootLogin(QUERY)

    expect(auth.workspaceHandoff).toBeNull()
    expect(auth.authError).toBeNull()
    expect(router.currentRoute.value.path).toBe('/login')
    expect(w.get('#email').element.value).toBe('member@example.com')
    expect(w.find('[data-testid="login-workspace-handoff"]').exists()).toBe(true)
  })

  it('after a reload (nothing held) it sends nothing and shows the code form', async () => {
    const { w } = await bootLogin(QUERY, { held: false })
    expect(axios.post).not.toHaveBeenCalled()
    expect(w.get('#email').element.value).toBe('member@example.com')
  })

  it('a token held for another address is not sent', async () => {
    const { w } = await bootLogin({ ...QUERY, email: 'someone-else@example.com' })
    expect(axios.post).not.toHaveBeenCalled()
    expect(w.get('#email').element.value).toBe('someone-else@example.com')
  })
})
