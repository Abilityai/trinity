// @vitest-environment jsdom
/**
 * trinity-enterprise#527 / #663 — the role card, mounted.
 *
 * The card is a projection of the agent's files, so what the component owns is
 * the honest rendering: no role → nothing; a failed role file says so; stale
 * says stale; readiness shows who/when; the owner's flip goes through a
 * confirm and a refusal lands next to the control. Mounted against the real
 * store with HTTP mocked (#2918 — a regex over the SFC proves none of that).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

vi.hoisted(() => {
  const store = new Map()
  globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    clear: () => store.clear(),
  }
})
vi.mock('@/stores/auth', async () => {
  const { ref } = await import('vue')
  const authed = ref(true)
  return { useAuthStore: () => ({ get isAuthenticated() { return authed.value }, get authHeader() { return { Authorization: 'Bearer jwt' } } }) }
})
vi.mock('axios', () => {
  const inst = {
    get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  }
  return { default: Object.assign(inst, { create: () => inst }) }
})
vi.mock('@/api', () => ({ default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() } }))

import axios from 'axios'
import { useClientPortalStore } from '@/stores/clientPortal'
import PortalAgentRole from '@/components/portal/PortalAgentRole.vue'

const AGENT = 'sales-companion'
const URL = `/api/enterprise/client-portal/agents/${AGENT}/role`
const fresh = new Date(Date.now() - 3600_000).toISOString()

function card(over = {}) {
  return {
    agent_name: AGENT,
    role: { id: 'sales-lead', title: 'Sales Lead', mission: 'Close ICP-fit pipeline.', status: 'active',
            review_by: '2099-12-01', stale: false, path: 'canon/roles/sales-lead.yaml', error: null },
    seat: 'gary@example.com',
    objectives: [{
      id: 'q4-close-rate', statement: 'Raise close rate this quarter.', horizon: 'quarter', status: 'active', owned: true,
      metrics: [
        { name: 'close_rate', direction: 'up', target: 0.3, by: '2026-12-31', value: 0.27, as_of: fresh, stale: false },
        { name: 'reply_rate', direction: 'up', target: 0.2, by: null, value: null, as_of: null, stale: true },
      ],
    }],
    readiness: { status: 'calibrating', changed_at: null, changed_by: null, source: 'template', unstamped_ready: false },
    walkthrough: { asks: 4, target: 10, rated_down: 1, unavailable: false },
    relationship: null,
    can_flip_readiness: true,
    unavailable: null,
    ...over,
  }
}

async function mountWith(payload) {
  axios.get.mockResolvedValueOnce({ data: payload })
  const w = mount(PortalAgentRole, { props: { agentName: AGENT }, attachTo: document.body })
  await flushPromises()
  return w
}

beforeEach(() => { setActivePinia(createPinia()); vi.clearAllMocks(); document.body.innerHTML = '' })

describe('PortalAgentRole (mounted)', () => {
  it('an agent with no role renders nothing at all (AC 5)', async () => {
    const w = await mountWith({ agent_name: AGENT, role: null })
    expect(axios.get).toHaveBeenCalledWith(URL, expect.anything())
    expect(w.find('[data-testid="portal-agent-role"]').exists()).toBe(false)
  })

  it('shows the role, its objectives with stale marked stale, the relationship line, and the walkthrough', async () => {
    const w = await mountWith(card())
    expect(w.get('[data-testid="portal-role-title"]').text()).toBe('Sales Lead')
    expect(w.text()).toContain('Close ICP-fit pipeline.')
    expect(w.text()).toContain('canon/roles/sales-lead.yaml')
    expect(w.findAll('[data-testid="portal-role-objective"]')).toHaveLength(1)
    expect(w.findAll('[data-testid="portal-role-metric"]')).toHaveLength(1)          // close_rate, fresh
    expect(w.findAll('[data-testid="portal-role-metric-stale"]')).toHaveLength(1)    // reply_rate
    expect(w.get('[data-testid="portal-role-metric-stale"]').text()).toContain('stale')
    expect(w.get('[data-testid="portal-role-relationship"]').text()).toContain('no assignment recorded')
    expect(w.get('[data-testid="portal-role-walkthrough"]').text()).toContain('4 of 10 asks')
    expect(w.get('[data-testid="portal-role-walkthrough"]').text()).toContain('1 rated down')
  })

  it('a role file that failed to load says so, never an empty role', async () => {
    const w = await mountWith(card({ role: { id: 'sales-lead', error: 'role_file_not_found', path: 'canon/roles/sales-lead.yaml' }, objectives: [] }))
    expect(w.get('[data-testid="portal-role-title"]').text()).toBe('sales-lead')
    expect(w.get('[data-testid="portal-role-error"]').text()).toContain("not in the agent's canon yet")
  })

  it('a stopped agent says the files live in the container', async () => {
    const w = await mountWith(card({ role: null, unavailable: 'agent_stopped', objectives: [] }))
    expect(w.get('[data-testid="portal-role-unavailable"]').text()).toContain('stopped')
  })

  it('readiness shows who flipped it and when; a template-claimed ready without a stamp is called out', async () => {
    const stamped = await mountWith(card({ readiness: { status: 'ready', changed_at: fresh, changed_by: 'owner@example.com', source: 'owner', unstamped_ready: false } }))
    const r = stamped.get('[data-testid="portal-role-readiness"]').text()
    expect(r).toContain('ready')
    expect(r).toContain('by owner@example.com')
    expect(stamped.find('[data-testid="portal-role-walkthrough"]').exists()).toBe(false)   // only while calibrating

    setActivePinia(createPinia()); document.body.innerHTML = ''
    const unstamped = await mountWith(card({ readiness: { status: 'calibrating', source: 'template', unstamped_ready: true } }))
    expect(unstamped.get('[data-testid="portal-role-unstamped"]').text()).toContain('no owner has stamped it')
  })

  it('the flip is offered to the owner only, goes through a confirm, posts, and re-reads', async () => {
    const w = await mountWith(card())
    await w.get('[data-testid="portal-role-flip"]').trigger('click')
    await flushPromises()
    // The confirm restates the consequence and is explicit that no schedule is switched on.
    expect(document.body.textContent).toContain('Mark this companion ready?')
    expect(document.body.textContent).toContain('does not switch any schedule on')
    expect(axios.post).not.toHaveBeenCalled()

    axios.post.mockResolvedValueOnce({ data: { status: 'ready' } })
    axios.get.mockResolvedValueOnce({ data: card({ readiness: { status: 'ready', changed_at: fresh, changed_by: 'owner@example.com', source: 'owner', unstamped_ready: false } }) })
    const confirm = [...document.querySelectorAll('button')].find((b) => b.textContent.trim() === 'Mark ready' && b.closest('[role="dialog"], .fixed'))
    confirm.click()
    await flushPromises()
    expect(axios.post).toHaveBeenCalledWith(`${URL}/readiness`, { status: 'ready' }, expect.anything())
    expect(w.get('[data-testid="portal-role-readiness"]').text()).toContain('ready')
    expect(w.get('[data-testid="portal-role-flip"]').text()).toBe('Back to calibrating')
  })

  it('a non-owner sees no flip control', async () => {
    const w = await mountWith(card({ can_flip_readiness: false }))
    expect(w.find('[data-testid="portal-role-flip"]').exists()).toBe(false)
  })

  it('a refused flip lands next to the control with the server\'s reason', async () => {
    const w = await mountWith(card())
    const store = useClientPortalStore()
    axios.post.mockRejectedValueOnce({ response: { status: 403, data: { detail: { code: 'readiness_owner_only', message: "Only the agent's owner can change its readiness — the person who created it on this instance, not an assignment." } } } })
    await store.flipAgentReadiness(AGENT, 'ready')
    await flushPromises()
    expect(w.text()).toContain("Only the agent's owner can change its readiness")
    expect(store.roleFlipping).toBe(false)
  })
})
