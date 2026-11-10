// @vitest-environment jsdom
/**
 * ent#1201 — the sidebar while a member's assistant is being set up.
 *
 * An empty roster whose roster read says the viewer's agent is being set up
 * shows that one line, never "No agents shared with you yet — ask whoever
 * invited you". A member's row shows no chip while its assistant is starting,
 * and "Restarting" (never an account) when it is stopped outside a hand-over.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: true, authHeader: {}, userEmail: 'me@example.com', logout: vi.fn() }),
}))
vi.mock('axios', () => {
  const inst = () => ({
    get: vi.fn(async () => ({ data: {} })), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  })
  return { default: Object.assign(inst(), { create: inst }) }
})

import PortalSidebar from '@/components/portal/PortalSidebar.vue'
import { usePortalProgressStore } from '@/stores/portalProgress'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

let router
beforeEach(async () => {
  setActivePinia(createPinia())
  router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/workspace', component: { template: '<div />' } }] })
  await router.push('/workspace')
  await router.isReady()
})

const sidebar = (props) => mount(PortalSidebar, {
  props: { roster: [], threads: [], loadingRoster: false, isPlatformSession: true, workspaceOnly: true, ...props },
  global: { plugins: [router] },
})
const MEMBER = { name: 'bob-assistant', is_member_agent: true, owned: false,
                 owner: 'ops-account@example.com', availability: 'stopped' }

describe('ent#1201 the sidebar while the assistant is being set up', () => {
  it('an empty roster that is being set up says so, and only that', () => {
    const w = sidebar({ setupPending: true })
    const line = w.find('[data-testid="portal-sidebar-setup-pending"]')
    expect(line.exists()).toBe(true)
    expect(line.text()).toBe('Your assistant is being set up')
    expect(w.text()).not.toContain('No agents shared with you yet')
    w.unmount()
  })

  it('past the ceiling it says what the stage says', () => {
    const w = sidebar({ setupPending: true, setupOverdue: true })
    expect(w.find('[data-testid="portal-sidebar-setup-pending"]').text())
      .toBe("This is taking longer than usual — we're looking into it.")
    w.unmount()
  })

  it('without the flag the empty roster is unchanged', () => {
    const w = sidebar({})
    expect(w.text()).toContain('No agents shared with you yet')
    expect(w.find('[data-testid="portal-sidebar-setup-pending"]').exists()).toBe(false)
    w.unmount()
  })

  it("a member's row has no chip while its assistant is starting", async () => {
    usePortalProgressStore().byAgent[MEMBER.name] = { loading: false, loaded: true, error: null,
      steps: [{ key: 'assistant_ready', state: 'in_progress' }] }
    const w = sidebar({ roster: [MEMBER] })
    await flushPromises()
    expect(w.text()).not.toMatch(/Restarting|Stopped|Unavailable/)
    w.unmount()
  })

  it("a member's stopped row reads Restarting and names no account", async () => {
    const w = sidebar({ roster: [MEMBER] })
    await flushPromises()
    expect(w.text()).toContain('Restarting')
    expect(w.html()).not.toMatch(/ops-account|its owner/)
    w.unmount()
  })
})
