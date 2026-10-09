// @vitest-environment jsdom
/**
 * trinity-enterprise#837 — the REAL router guard sends a Workspace-only member
 * on an operator URL to the Workspace (an agent page to that agent's
 * conversation), and leaves operators and a still-loading role alone.
 *
 * Driven through `router.push` on the app's own router, so the wiring in
 * `router/index.js` is what runs; the views are stubbed so a navigation does
 * not mount the app.
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
vi.mock('../../src/views/AgentDetail.vue', () => ({ default: { render: () => null } }))
vi.mock('../../src/views/Dashboard.vue', () => ({ default: { render: () => null } }))
vi.mock('../../src/views/Operations.vue', () => ({ default: { render: () => null } }))
vi.mock('../../src/views/Login.vue', () => ({ default: { render: () => null } }))
vi.mock('../../src/views/SharedCanvas.vue', () => ({ default: { render: () => null } }))

import router from '../../src/router'
import { useAuthStore } from '../../src/stores/auth'

function signedIn(role) {
  const auth = useAuthStore()
  auth.isLoading = false
  auth.isAuthenticated = true
  auth.user = role === undefined ? { email: 'x@example.com' } : { email: 'x@example.com', role }
  return auth
}

const here = () => router.currentRoute.value.fullPath

describe('the router guard and the Workspace-only rung', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    globalThis.fetch = vi.fn(async () => ({ json: async () => ({ setup_completed: true }) }))
  })

  it("sends a member on an agent page to that agent's Workspace conversation", async () => {
    signedIn('user')
    await router.push('/agents/scout')
    expect(here()).toBe('/workspace?agent=scout')
  })

  it('sends a member on any other operator page to the Workspace', async () => {
    signedIn('user')
    await router.push('/operations')
    expect(here()).toBe('/workspace')
  })

  it('lets a member open the Workspace and a shared canvas', async () => {
    signedIn('user')
    await router.push('/workspace/inbox')
    expect(here()).toBe('/workspace/inbox')
    await router.push('/canvas/s/tok_1')
    expect(here()).toBe('/canvas/s/tok_1')
  })

  it('sends a signed-in member who opens /login to the Workspace', async () => {
    signedIn('user')
    await router.push('/login')
    expect(here()).toBe('/workspace')
  })

  it('leaves an operator on the agent page', async () => {
    signedIn('operator')
    await router.push('/agents/atlas')
    expect(here()).toBe('/agents/atlas')
  })

  it('confirms a cached `user` role with the server before redirecting', async () => {
    const auth = signedIn('user')
    auth.profileVerified = false
    vi.spyOn(auth, 'fetchUserProfile').mockImplementation(async () => {
      auth.user = { ...auth.user, role: 'operator' }   // raised since the last sign-in
      auth.profileVerified = true
    })
    await router.push('/agents/delta')
    expect(auth.fetchUserProfile).toHaveBeenCalledTimes(1)
    expect(here()).toBe('/agents/delta')
  })

  it('confirms a cached `user` role before sending a signed-in visitor of /login away', async () => {
    const auth = signedIn('user')
    auth.profileVerified = false
    vi.spyOn(auth, 'fetchUserProfile').mockImplementation(async () => {
      auth.user = { ...auth.user, role: 'operator' }
      auth.profileVerified = true
    })
    await router.push('/workspace')
    await router.push('/login')
    expect(auth.fetchUserProfile).toHaveBeenCalledTimes(1)
    expect(here()).toBe('/')
  })

  it('leaves a role that has not loaded yet alone — the server is the boundary', async () => {
    signedIn(undefined)
    await router.push('/agents/beacon')
    expect(here()).toBe('/agents/beacon')
  })
})
