// @vitest-environment jsdom
/**
 * #4518 — an agent with a display label and no avatar image shows the SAME
 * avatar tile (initials and colour) in the sidebar row as in the briefing.
 * The sidebar used to pass the slug (`agent.name`) to PortalAvatar while the
 * text next to it showed the label.
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
import PortalAvatar from '@/components/portal/PortalAvatar.vue'

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

describe('sidebar agent avatar follows the display label (#4518)', () => {
  it('uses the label for initials and tint, like the conversation header', () => {
    const agent = { name: 'ap-abc-p105', display_label: 'Zeta Desk', avatar_url: null, playbooks: [] }
    const side = mount(PortalSidebar, {
      props: { roster: [agent], threads: [], loadingRoster: false, isPlatformSession: true, activeAgentName: 'ap-abc-p105' },
      global: { plugins: [router] },
    })
    const row = side.findAllComponents(PortalAvatar).find((c) => c.props('avatarUrl') === null && c.props('size') === 26)
    expect(row).toBeTruthy()
    expect(row.props('name')).toBe('Zeta Desk')
    expect(row.text()).toBe('ZD')
    side.unmount()
  })
})
