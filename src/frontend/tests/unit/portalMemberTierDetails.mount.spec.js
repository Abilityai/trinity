// @vitest-environment jsdom
/**
 * ent#1201 — the Agent-details panel follows the member-tier rule.
 *
 * It hard-coded the detailed chip, which names the owner ("ask
 * <account> to start it"). A member viewing their own assistant reads
 * "Restarting" with no account named, and nothing while it is starting; an
 * operator's card keeps the detailed #2196 copy.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { shallowMount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: true, authHeader: {}, logout: vi.fn() }),
}))
vi.mock('axios', () => {
  const mk = () => ({
    get: vi.fn(async () => ({ data: {} })), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  })
  return {
    default: Object.assign(
      { get: vi.fn(async () => ({ data: {} })), post: vi.fn(), put: vi.fn(), delete: vi.fn(), create: mk },
      { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
        defaults: { headers: { common: {} } } },
    ),
  }
})

import PortalAgentDetails from '@/components/portal/PortalAgentDetails.vue'
import { usePortalProgressStore } from '@/stores/portalProgress'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

let router
beforeEach(() => {
  setActivePinia(createPinia())
  router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:p(.*)*', component: { template: '<div />' } }] })
})

async function details(agent) {
  const w = shallowMount(PortalAgentDetails, {
    props: { agentName: agent.name, agent, threads: [] },
    global: { plugins: [router] },
  })
  await flushPromises()
  return w
}
const MEMBER = { name: 'bob-assistant', is_member_agent: true, owned: false,
                 owner: 'ops-account@example.com', availability: 'stopped' }

describe('ent#1201 the details panel under a member share', () => {
  it('a stopped member assistant reads Restarting and names no account', async () => {
    const w = await details(MEMBER)
    const head = w.find('[data-testid="portal-agent-details"]')
    expect(head.text()).toContain('Restarting')
    expect(head.html()).not.toMatch(/ops-account|its owner|start it/)
  })

  it('shows nothing while the member assistant is starting', async () => {
    usePortalProgressStore().byAgent[MEMBER.name] = { loading: false, loaded: true, error: null,
      steps: [{ key: 'assistant_ready', state: 'in_progress' }] }
    const w = await details(MEMBER)
    expect(w.text()).not.toMatch(/Restarting|Stopped|Unavailable/)
  })

  it("an operator's card keeps the detailed copy", async () => {
    const w = await details({ name: 'scout', owner: 'alice', availability: 'stopped' })
    const span = w.findAll('span').find((s) => s.text() === 'Stopped')
    expect(span).toBeTruthy()
    expect(span.attributes('title')).toContain('alice')
  })
})
