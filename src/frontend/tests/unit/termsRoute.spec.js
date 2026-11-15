// @vitest-environment jsdom
/**
 * trinity-enterprise#1054 — the `/terms` route, driven through the app's REAL
 * router (`router/index.js`), so its guard is what runs. Views are stubbed so a
 * navigation does not mount the app.
 *
 *  - a signed-in Workspace-only person STAYS on /terms: the operator-floor
 *    redirect must not send them to the Workspace, whose every call the terms
 *    gate refuses (that would be the loop);
 *  - signed out → the sign-in page, carrying /terms back;
 *  - an operator can open it too (nothing to accept, a Continue);
 *  - it is a standalone page — no help widget over the accept action.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('axios', () => {
  const instance = {
    get: vi.fn(async () => ({ data: {} })),
    post: vi.fn(async () => ({ data: {} })),
    defaults: { headers: { common: {} } },
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
  }
  return { default: { ...instance, create: vi.fn(() => instance) } }
})
vi.mock('../../src/views/Portal.vue', () => ({ default: { render: () => null } }))
vi.mock('../../src/views/Login.vue', () => ({ default: { render: () => null } }))
vi.mock('../../src/views/Dashboard.vue', () => ({ default: { render: () => null } }))
vi.mock('../../src/views/TermsAcceptance.vue', () => ({ default: { name: 'TermsAcceptanceStub', render: () => null } }))

import router from '../../src/router'
import { useAuthStore } from '../../src/stores/auth'

function signedIn(role) {
  const auth = useAuthStore()
  auth.isLoading = false
  auth.isAuthenticated = true
  auth.profileVerified = true
  auth.user = { email: 'x@example.com', role }
  return auth
}

const here = () => router.currentRoute.value

describe('the /terms route', () => {
  beforeEach(async () => {
    setActivePinia(createPinia())
    globalThis.fetch = vi.fn(async () => ({ json: async () => ({ setup_completed: true }) }))
  })

  it('keeps a signed-in Workspace-only person on the terms page', async () => {
    signedIn('user')
    await router.push('/terms?redirect=%2Fworkspace')
    expect(here().path).toBe('/terms')
    expect(here().matched.at(-1).components.default.name).toBe('TermsAcceptanceStub')
  })

  it('sends a signed-out visitor to sign in, carrying /terms back', async () => {
    const auth = useAuthStore()
    auth.isLoading = false
    auth.isAuthenticated = false
    auth.user = null
    await router.push('/terms')
    expect(here().path).toBe('/login')
    expect(here().query.redirect).toBe('/terms')
  })

  it('opens for an operator as well', async () => {
    signedIn('operator')
    await router.push('/terms')
    expect(here().path).toBe('/terms')
  })

  it('is a standalone page with no help widget', async () => {
    signedIn('user')
    await router.push('/terms')
    expect(here().meta.requiresAuth).toBe(true)
    expect(here().meta.hideHelpWidget).toBe(true)
  })
})
