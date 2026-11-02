// @vitest-environment jsdom
/**
 * #3466 — the Workspace sign-in card names the platform login.
 *
 * An operator whose platform session ends while they work in the Workspace
 * (signed out in another tab, or back after the session ended) sees the
 * Workspace's own sign-in: "Sign in to your agents", an email and a 6-digit
 * code. That is the door for people an agent was shared with; an operator
 * cannot always get through it (an account with no email on it, an install
 * with no email provider), and nothing on the card pointed anywhere else.
 *
 * Ruled: no automatic navigation (#2791 — a background tab pushing `/login` is
 * noise), and the card gains one line under both steps, "Have a Trinity
 * account? Sign in", to `/login?redirect=<this page>` — or to the shared canvas
 * a visitor came from — shown only while no platform session is held. When
 * one is held, #2261's "Continue as" card is the way back instead.
 *
 * Mounted, with the link clicked through a real router.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { shallowMount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { createRouter, createMemoryHistory, RouterLink } from 'vue-router'

vi.hoisted(() => {
  window.matchMedia = window.matchMedia || ((q) => ({
    matches: false, media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
  }))
})

vi.mock('axios', () => {
  const mk = () => ({
    get: vi.fn(async () => ({ data: {} })), post: vi.fn(async () => ({ data: {} })), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  })
  return {
    default: Object.assign(
      { get: vi.fn(async () => ({ data: {} })), post: vi.fn(async () => ({ data: {} })), put: vi.fn(), delete: vi.fn(), create: mk },
      { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
        defaults: { headers: { common: {} } } },
    ),
  }
})

import { useAuthStore } from '@/stores/auth'
import { useClientPortalStore } from '@/stores/clientPortal'
import Portal from '@/views/Portal.vue'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

const COPY = 'Have a Trinity account? Sign in'
const LINK = '[data-testid="workspace-platform-login"]'
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
      { path: '/login', component: Blank },
      { path: '/workspace', component: Blank },
      { path: '/workspace/c/:sessionId', component: Blank },
      { path: '/workspace/a/:agentName', component: Blank },
    ],
  })
}

async function boot(path) {
  const router = makeRouter()
  await router.push(path)
  await router.isReady()
  const store = useClientPortalStore()
  store.requestCode = vi.fn(async () => {})
  const w = shallowMount(Portal, {
    global: { plugins: [router], stubs: { PortalCodeInput: CodeInputStub, RouterLink } },
  })
  await flushPromises()
  return { w, router, store }
}

const lineText = (w) => w.get(LINK).element.parentElement.textContent.replace(/\s+/g, ' ').trim()

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  setActivePinia(createPinia())
})

describe('#3466 — with no platform session, the card offers the platform login', () => {
  it('says so in one line, and the link returns to this Workspace page', async () => {
    const { w, router } = await boot('/workspace/a/scout')
    expect(w.text()).toContain('Sign in to your agents')
    expect(lineText(w)).toBe(COPY)
    expect(w.get(LINK).text()).toBe('Sign in')

    await w.get(LINK).trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/login')
    expect(router.currentRoute.value.query).toEqual({ redirect: '/workspace/a/scout' })
  })

  it('a visitor who came from a shared canvas is sent back to the canvas', async () => {
    const { w, router } = await boot('/workspace?redirect=%2Fcanvas%2Fs%2Ftok_1')
    await w.get(LINK).trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.query).toEqual({ redirect: '/canvas/s/tok_1' })
  })

  it('stays on the code step, for an operator waiting on a code that will not come', async () => {
    const { w } = await boot('/workspace/c/s_1')
    await w.get('input[type="email"]').setValue('op@example.com')
    await w.get('form').trigger('submit')
    await flushPromises()
    expect(w.text()).toContain('Enter your code')
    expect(lineText(w)).toBe(COPY)
  })

  it('appears when the platform session ends in another tab, without navigating', async () => {
    const auth = useAuthStore()
    auth.isAuthenticated = true
    auth.token = 'jwt'
    auth.user = { email: 'op@example.com', role: 'admin' }
    const { w, router } = await boot('/workspace/c/s_1')
    expect(w.find(LINK).exists()).toBe(false)              // working in the Workspace

    auth.applySessionEndedElsewhere()                      // the `storage` reaction
    await flushPromises()
    expect(w.text()).toContain('Sign in to your agents')
    expect(lineText(w)).toBe(COPY)
    expect(router.currentRoute.value.fullPath).toBe('/workspace/c/s_1')
  })
})

describe('#3466 — with a platform session held, the card keeps #2261 and shows no link', () => {
  it('a client tab whose session expired, on a browser with a live operator login', async () => {
    const auth = useAuthStore()
    auth.isAuthenticated = true
    auth.token = 'jwt'
    auth.user = { email: 'op@example.com', role: 'admin' }
    const store = useClientPortalStore()
    store.platformFallbackSuppressed = true
    const { w } = await boot('/workspace')
    expect(w.text()).toContain('Sign in to your agents')
    expect(w.find('[data-testid="workspace-continue-as-operator"]').exists()).toBe(true)
    expect(w.find(LINK).exists()).toBe(false)
  })
})
