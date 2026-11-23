// @vitest-environment jsdom
/**
 * trinity-enterprise#1327 — accepting an instance's terms of use re-reads what
 * start-up was refused.
 *
 * Until a person accepts the current terms, every route they call answers 403
 * `terms_required`. The app's start-up reads — the profile (`/api/users/me`),
 * the entitlements (`/api/settings/feature-flags`), the build info
 * (`/api/version`), the first-run state, the assistant's status and the
 * getting-started checklist —
 * are all refused on a first visit, and each one caches that refusal for the
 * page load. Accepting used to leave every one of them as it was, so the page
 * the person landed on had no edition, no Enterprise tab and no assistant until
 * a reload.
 *
 * Mounted: `App.vue` on a memory router, the real NavBar on the landing page,
 * the real terms page, the real stores; only HTTP is faked (and the two corner
 * widgets, whose own behaviour has its own specs, are markers).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'
import { defineComponent, h } from 'vue'

vi.hoisted(() => {
  window.matchMedia = window.matchMedia || ((q) => ({
    matches: false, media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
  }))
  globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }
})

// One fake server behind both HTTP clients: every route but the two terms
// routes is refused with `terms_required` until the terms are accepted.
const server = vi.hoisted(() => {
  // `hang`: a URL that, once the terms are accepted, never answers.
  const state = { accepted: false, calls: [], hang: null }
  const refuse = () => {
    const e = new Error('HTTP 403')
    e.response = { status: 403, data: { detail: { code: 'terms_required' } } }
    return Promise.reject(e)
  }
  const answers = {
    '/api/users/me': { username: 'admin', email: 'admin@example.com', role: 'admin' },
    '/api/settings/feature-flags': { enterprise_features: ['assistant', 'sso'] },
    '/api/version': { version: '9.9.9', git_commit_short: 'abc1234', edition: 'enterprise' },
    '/api/enterprise/assistant/status': { available: true, contacted: true, reachable: true },
    '/api/enterprise/onboarding/checklist': { items: [], completed_count: 0, total_count: 0 },
    '/api/onboarding/first-run': { first_run: true, seeded_agents: ['demo'], own_agent_count: 0, demo_agent: 'demo', install: 'inst-1' },
    '/api/terms/current': { required: true, version: '2026-11', title: 'Terms of use', text: 'Be kind.', accepted: false },
  }
  async function respond(method, url) {
    state.calls.push(`${method} ${url}`)
    if (method === 'post' && url === '/api/terms/accept') {
      state.accepted = true
      return { data: { version: '2026-11', accepted: true } }
    }
    if (url === '/api/terms/current') return { data: answers[url] }
    if (!state.accepted) return refuse()
    if (url === state.hang) return new Promise(() => {})
    if (url in answers) return { data: answers[url] }
    if (url.startsWith('/api/notifications')) return { data: { count: 0 } }
    return { data: {} }
  }
  const client = {
    get: (url) => respond('get', url),
    post: (url) => respond('post', url),
    put: (url) => respond('put', url),
    delete: (url) => respond('delete', url),
    defaults: { headers: { common: {} } },
    interceptors: { request: { use() {} }, response: { use() {} } },
  }
  return { state, client }
})

vi.mock('axios', () => ({ default: { ...server.client, create: () => server.client } }))
vi.mock('@/api', () => ({ default: server.client }))
vi.mock('@/utils/websocket', async () => {
  const { ref } = await import('vue')
  return { useWebSocket: () => ({ isConnected: ref(true), followToken() {} }), reconnectWebSocket() {} }
})
vi.mock('@/components/assistant/AssistantWidget.vue', () => ({
  default: { render: () => h('div', { 'data-testid': 'assistant' }) },
}))
vi.mock('@/components/HelpChatWidget.vue', () => ({
  default: { render: () => h('div', { 'data-testid': 'help-widget' }) },
}))

import App from '@/App.vue'
import NavBar from '@/components/NavBar.vue'
import TermsAcceptance from '@/views/TermsAcceptance.vue'
import { useAuthStore } from '@/stores/auth'
import { useEnterpriseStore } from '@/stores/enterprise'
import { useOnboardingStore } from '@/stores/onboarding'
import { useAssistantStore } from '@/stores/assistant'
import { useFirstRunStore } from '@/stores/firstRun'
import { resetInFlight } from '@/utils/inflight'
import { useBuildInfo } from '@/composables/useBuildInfo'

const Landing = defineComponent({ render: () => h('div', [h(NavBar), h('main', 'dashboard')]) })

let wrapper

async function startUpRefused() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: Landing, meta: { requiresAuth: true } },
      { path: '/terms', component: TermsAcceptance, meta: { requiresAuth: true, hideHelpWidget: true } },
      { path: '/login', component: { render: () => h('div', 'login') } },
      { path: '/:rest(.*)*', component: { render: () => h('div') } },
    ],
  })
  // The person signed in; the stored role is not trusted until the server says it.
  const auth = useAuthStore()
  auth.token = 'tok'
  auth.isAuthenticated = true
  auth.user = { username: 'admin', email: 'admin@example.com', role: 'admin' }

  await router.push('/')
  await router.isReady()
  wrapper = mount(App, {
    global: { plugins: [router], stubs: { BaseModal: true, ThemeChoice: true, NavSheet: true } },
  })
  // The start-up reads main.js and the page fire, every one refused.
  await auth.fetchUserProfile()
  await useOnboardingStore().fetchChecklist()
  await useFirstRunStore().fetchState()   // the first-run overlay's own read on the landing page
  await flushPromises()
  // The refusal handler opens the terms page, carrying the page to come back to.
  await router.push({ path: '/terms', query: { redirect: '/' } })
  await flushPromises()
  return router
}

async function accept() {
  await wrapper.find('[data-testid="terms-agree"]').setValue(true)
  await wrapper.find('[data-testid="terms-accept"]').trigger('click')
  await flushPromises()
  await flushPromises()
}

beforeEach(() => {
  setActivePinia(createPinia())
  resetInFlight()
  useBuildInfo().info.value = null   // a module singleton: each test is its own page load
  server.state.accepted = false
  server.state.calls.length = 0
  server.state.hang = null
})
afterEach(() => { vi.useRealTimers(); wrapper?.unmount(); wrapper = null })

describe('accepting the terms re-reads what start-up was refused (trinity-enterprise#1327)', () => {
  it('lands on a page with the edition, the Enterprise tab and the assistant — no reload', async () => {
    const router = await startUpRefused()
    // Before acceptance: the refusals are cached as "nothing".
    expect(useEnterpriseStore().hasAnyEnterprise).toBe(false)
    expect(useAuthStore().profileVerified).toBe(false)

    await accept()

    expect(router.currentRoute.value.path).toBe('/')
    const text = wrapper.text()
    expect(text).toContain('Enterprise')                            // the tab
    expect(text).toContain('9.9.9')                                 // the build/edition chip
    expect(wrapper.find('[data-testid="assistant"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="help-widget"]').exists()).toBe(false)
    expect(server.state.calls).toContain('get /api/enterprise/assistant/status')
  })

  it('every refused start-up read is asked again once the terms are accepted', async () => {
    await startUpRefused()
    const refused = ['/api/users/me', '/api/settings/feature-flags', '/api/version', '/api/enterprise/onboarding/checklist', '/api/onboarding/first-run']
    for (const url of refused) expect(server.state.calls).toContain(`get ${url}`)
    const before = server.state.calls.length

    await accept()

    const after = server.state.calls.slice(before)
    for (const url of refused) expect(after).toContain(`get ${url}`)
    expect(useOnboardingStore().available).toBe(true)
    expect(useAssistantStore().available).toBe(true)
  })

  it('the first-run state is the real answer after acceptance and after the landing page remounts', async () => {
    await startUpRefused()
    const firstRun = useFirstRunStore()
    expect(firstRun.firstRun).toBe(false)   // the refusal, cached as "not a first run"

    await accept()
    expect(firstRun.firstRun).toBe(true)
    expect(firstRun.install).toBe('inst-1')
    expect(firstRun.demoAgent).toBe('demo')

    // The overlay remounts with the landing page and asks without `force`.
    await firstRun.fetchState()
    await flushPromises()
    expect(firstRun.firstRun).toBe(true)
    expect(firstRun.install).toBe('inst-1')
  })

  it('a re-read that never answers does not keep the person on the terms page', async () => {
    const router = await startUpRefused()
    server.state.hang = '/api/enterprise/onboarding/checklist'
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })

    await accept()
    expect(router.currentRoute.value.path).toBe('/terms')   // still waiting on the re-reads

    await vi.advanceTimersByTimeAsync(10_000)
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/')
    // The reads that did answer are in; the hung one lands whenever it lands.
    expect(useEnterpriseStore().hasAnyEnterprise).toBe(true)
    expect(useFirstRunStore().firstRun).toBe(true)
  })

  it('the landing page never shows the open-source shell first: the reads are in before it moves on', async () => {
    const router = await startUpRefused()
    const seen = []
    router.afterEach(() => {
      seen.push({ enterprise: useEnterpriseStore().hasAnyEnterprise, assistant: useAssistantStore().available })
    })
    await accept()
    expect(seen).toEqual([{ enterprise: true, assistant: true }])
  })

  it('declining still signs the person out', async () => {
    const router = await startUpRefused()
    await wrapper.find('[data-testid="terms-decline"]').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/login')
    expect(useAuthStore().isAuthenticated).toBe(false)
    expect(server.state.calls).not.toContain('post /api/terms/accept')
  })
})
