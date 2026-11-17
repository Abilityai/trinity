// @vitest-environment jsdom
/**
 * trinity#3894 — a platform account with no email (the built-in `admin` on a
 * default install) gets the remedy, not a dead Workspace.
 *
 * The Workspace finds a person's agents by email, so the backend refuses an
 * email-less account on every Workspace call (403, `X-Trinity-Error-Code:
 * account_has_no_email`). Before, the shell rendered anyway: "Couldn't load
 * your agents" with a Try again that could never succeed, beside "No agents
 * yet. Create one" for an owner who just had. Pinned:
 *  - `fetchRoster` reads the code from the header (not the message text);
 *  - Portal renders the "add an email" state, not the shell, and its buttons
 *    open Settings › General and retry;
 *  - any other refusal is not mistaken for it, and a later success clears it.
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
import { useClientPortalStore, portalHttp } from '@/stores/clientPortal'
import Portal from '@/views/Portal.vue'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

const NO_EMAIL = {
  response: {
    status: 403,
    headers: { 'x-trinity-error-code': 'account_has_no_email' },
    data: { detail: 'This Trinity account has no email address, and the Workspace identifies people by email.' },
  },
}
const OTHER_403 = { response: { status: 403, headers: {}, data: { detail: 'Forbidden' } } }
const Blank = { template: '<div />' }

function router() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/workspace', component: Blank },
      { path: '/settings', component: Blank },
    ],
  })
}

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  setActivePinia(createPinia())
  const auth = useAuthStore()
  auth.isAuthenticated = true          // a platform session: the one-click path
  auth.token = 'jwt'
  portalHttp.get.mockReset()
})

describe('the store reads the refusal code', () => {
  it('sets accountHasNoEmail from the header', async () => {
    portalHttp.get.mockRejectedValue(NO_EMAIL)
    const store = useClientPortalStore()
    await store.fetchRoster()
    expect(store.accountHasNoEmail).toBe(true)
  })

  it('another 403 is not that case', async () => {
    portalHttp.get.mockRejectedValue(OTHER_403)
    const store = useClientPortalStore()
    await store.fetchRoster()
    expect(store.accountHasNoEmail).toBe(false)
  })

  it('a later successful load clears it', async () => {
    const store = useClientPortalStore()
    portalHttp.get.mockRejectedValueOnce(NO_EMAIL)
    await store.fetchRoster()
    portalHttp.get.mockResolvedValue({ data: { agents: [], client_email: 'admin@example.com' } })
    await store.fetchRoster()
    expect(store.accountHasNoEmail).toBe(false)
  })

  it('signing out clears it, so the next session does not inherit the page', async () => {
    portalHttp.get.mockRejectedValue(NO_EMAIL)
    const store = useClientPortalStore()
    await store.fetchRoster()
    expect(store.accountHasNoEmail).toBe(true)
    store.signOut()
    expect(store.accountHasNoEmail).toBe(false)
  })
})

describe('Portal shows the remedy, not the shell', () => {
  async function boot() {
    const r = router()
    await r.push('/workspace')
    await r.isReady()
    const store = useClientPortalStore()
    store.fetchRoster = vi.fn(async () => {})
    store.accountHasNoEmail = true
    const w = shallowMount(Portal, { global: { plugins: [r], stubs: { RouterLink } } })
    await flushPromises()
    return { w, r, store }
  }

  it('renders the state and none of the dead-end shell', async () => {
    const { w } = await boot()
    const state = w.get('[data-testid="workspace-account-has-no-email"]')
    expect(state.text()).toContain('Add an email to use the Workspace')
    expect(state.text()).toContain('Settings → General')
    expect(w.text()).not.toContain('Try again')
    expect(w.find('[data-testid="sidebar-inbox"]').exists()).toBe(false)
  })

  it('Open Settings goes to Settings › General; Reload retries', async () => {
    const { w, r, store } = await boot()
    await w.get('[data-testid="workspace-add-email"]').trigger('click')
    await flushPromises()
    expect(r.currentRoute.value.path).toBe('/settings')
    expect(r.currentRoute.value.query).toEqual({ tab: 'general' })
    await w.get('[data-testid="workspace-no-email-reload"]').trigger('click')
    expect(store.fetchRoster).toHaveBeenCalled()
  })
})
