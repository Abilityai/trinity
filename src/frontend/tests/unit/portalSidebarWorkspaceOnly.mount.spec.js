// @vitest-environment jsdom
/**
 * trinity-enterprise#837 — an empty Workspace roster names a next step the
 * reader can actually take. A `user` (Workspace-only) cannot make an agent, so
 * "Create one →" (a link into the operator UI the router would bounce them out
 * of) gives way to the ask an external client gets.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: true, authHeader: {}, userEmail: 'me@example.com', logout: vi.fn() }),
}))
vi.mock('axios', () => {
  const inst = () => ({
    get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  })
  return { default: Object.assign(inst(), { create: inst }) }
})

import PortalSidebar from '@/components/portal/PortalSidebar.vue'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

let router
beforeEach(async () => {
  setActivePinia(createPinia())
  router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/workspace', component: { template: '<div />' } }],
  })
  await router.push('/workspace')
  await router.isReady()
})

const emptyRoster = (props) => mount(PortalSidebar, {
  props: { roster: [], threads: [], loadingRoster: false, isPlatformSession: true, ...props },
  global: { plugins: [router] },
})

describe('the empty roster for a Workspace-only member', () => {
  it('asks for a share instead of pointing at the operator UI', () => {
    const w = emptyRoster({ workspaceOnly: true })
    expect(w.text()).toContain('No agents shared with you yet')
    expect(w.findAll('a').some((a) => a.attributes('href') === '/')).toBe(false)
    w.unmount()
  })

  it('still offers an operator the way to make one', () => {
    const w = emptyRoster({ workspaceOnly: false })
    const create = w.findAll('a').find((a) => a.attributes('href') === '/')
    expect(create?.text()).toContain('Create one')
    w.unmount()
  })
})
