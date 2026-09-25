// @vitest-environment jsdom
/**
 * trinity-enterprise#641 — the autonomy dial, mounted.
 *
 * The panel's job is to make a verdict explainable: which classes run
 * unprompted, which do not, and EVERY reason why not — including the three
 * that are not about the class at all (the instance ceiling, the agent's hard
 * off, an operator hold). It also has to keep two things visibly apart: what
 * the evidence earned, and what the dial currently permits.
 *
 * And one asymmetry has to be visible in the UI, not only in the API: a person
 * may HOLD a class, and only the owner may RELEASE it. There is no promote.
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
import PortalAgentAutonomy from '@/components/portal/PortalAgentAutonomy.vue'

const AGENT = 'sales-companion'
const URL = `/api/enterprise/client-portal/agents/${AGENT}/autonomy`
const ME = 'gary@example.com'

const BLOCKER_TEXT = {
  level_below_l2: 'the instance dial is below L2, so nothing runs unprompted anywhere',
  agent_autonomy_off: "this agent's autonomy switch is off — the hard off",
  too_few_records: 'fewer than three decisions on record for this kind of ask',
  reversal_in_window: 'a decision in this window was reversed',
  negative_rating_in_window: 'this seat rated the agent down inside the rating window',
  held_by_operator: 'an operator is holding this class on-request',
}

function klass(over = {}) {
  return {
    ask_class: 'vendor-renewal', seat: ME, state: 'graduated', earned_state: 'graduated',
    unprompted: true, blocked_by: [], guard_metric: 'clear', held: false, writable: true,
    evidence_expires_at: '2026-12-31',
    evidence: { count: 3, criteria: ['renewal cost below the switching cost'], reversals: 0 },
    ...over,
  }
}

function page(over = {}) {
  return {
    agent_name: AGENT, my_seat: ME, level: 'L2',
    level_label: 'Delegated classes — unprompted on graduated ask classes; guard-capped',
    ceiling_allows_unprompted: true, agent_autonomy_enabled: true,
    rating_window_days: 30, rule_version: '2026-09-23',
    can_hold: true, can_release: false,
    classes: [klass()], other_seats: [], blocker_text: BLOCKER_TEXT,
    ...over,
  }
}

async function mountWith(payload) {
  axios.get.mockResolvedValueOnce({ data: payload })
  const w = mount(PortalAgentAutonomy, { props: { agentName: AGENT }, attachTo: document.body })
  await flushPromises()
  return w
}

beforeEach(() => { setActivePinia(createPinia()); vi.clearAllMocks(); document.body.innerHTML = '' })

describe('PortalAgentAutonomy (mounted)', () => {
  it('shows the level, the graduated class and the evidence it rests on', async () => {
    const w = await mountWith(page())
    expect(axios.get).toHaveBeenCalledWith(URL, expect.anything())
    expect(w.get('[data-testid="portal-autonomy-level"]').text()).toBe('L2')
    expect(w.get('[data-testid="portal-autonomy-ceiling"]').text()).toContain('Delegated classes')
    const card = w.get('[data-testid="portal-autonomy-class-graduated"]')
    expect(card.text()).toContain('vendor-renewal')
    expect(card.text()).toContain('unprompted')
    expect(card.text()).toContain('renewal cost below the switching cost')
    expect(card.text()).toContain('3 decisions on one criterion')
    expect(card.text()).toContain('2026-12-31')
  })

  it('lists EVERY blocker, not just the first, in the words the server sent', async () => {
    const w = await mountWith(page({
      classes: [klass({
        state: 'on_request', earned_state: 'on_request', unprompted: false,
        blocked_by: ['too_few_records', 'reversal_in_window', 'negative_rating_in_window'],
        evidence: { count: 2, criteria: ['a'], reversals: 1 },
      })],
    }))
    const blockers = w.get('[data-testid="portal-autonomy-blockers-vendor-renewal"]').text()
    expect(blockers).toContain('fewer than three decisions')
    expect(blockers).toContain('was reversed')
    expect(blockers).toContain('rated the agent down')
  })

  it('separates "not earned" from "earned but the dial is down"', async () => {
    const w = await mountWith(page({
      level: 'L1', ceiling_allows_unprompted: false,
      level_label: 'Companion — on-request; brief on for ready seats',
      classes: [klass({ state: 'on_request', earned_state: 'graduated', unprompted: false,
                        blocked_by: ['level_below_l2'] })],
    }))
    const card = w.get('[data-testid="portal-autonomy-class-on_request"]')
    expect(card.text()).toContain('earned — blocked by the dial')
    expect(card.text()).toContain('the instance dial is below L2')
    expect(w.get('[data-testid="portal-autonomy-ceiling"]').text()).toContain('nothing runs unprompted below L2')
  })

  it('says so when the agent hard off is on — whatever a class earned', async () => {
    const w = await mountWith(page({
      agent_autonomy_enabled: false,
      classes: [klass({ state: 'on_request', unprompted: false, blocked_by: ['agent_autonomy_off'] })],
    }))
    expect(w.get('[data-testid="portal-autonomy-hard-off"]').text()).toContain('hard off')
  })

  it('hold posts and re-reads; release is offered to the owner only', async () => {
    const w = await mountWith(page())
    axios.post.mockResolvedValueOnce({ data: { class: klass({ held: true, unprompted: false }) } })
    axios.get.mockResolvedValueOnce({ data: page({
      classes: [klass({ held: true, state: 'on_request', unprompted: false, blocked_by: ['held_by_operator'] })],
    }) })
    await w.get('[data-testid="portal-autonomy-hold-vendor-renewal"]').trigger('click')
    await flushPromises()
    expect(axios.post).toHaveBeenCalledWith(
      `${URL}/classes/vendor-renewal`, { action: 'hold', seat: ME }, expect.anything())
    expect(w.text()).toContain('an operator is holding this class')
    // a non-owner sees why they cannot undo it, rather than a button that 403s
    expect(w.get('[data-testid="portal-autonomy-release-owner-only"]').text()).toContain("owner can release")
    expect(w.find('[data-testid="portal-autonomy-release-vendor-renewal"]').exists()).toBe(false)
  })

  it('the owner gets Release, and there is no promote control anywhere', async () => {
    const w = await mountWith(page({
      can_release: true,
      classes: [klass({ held: true, state: 'on_request', unprompted: false, blocked_by: ['held_by_operator'] })],
    }))
    expect(w.get('[data-testid="portal-autonomy-release-vendor-renewal"]').exists()).toBe(true)
    // No CONTROL promotes — the only verbs are hold and release. (The note says
    // so in words; this asserts the buttons, which is the part that could drift.)
    const verbs = w.findAll('button').map((b) => b.text().toLowerCase())
    expect(verbs.some((v) => v.includes('promote') || v.includes('graduate'))).toBe(false)
    expect(verbs).toContain('release')
    expect(w.get('[data-testid="portal-autonomy-note"]').text()).toContain('no promote button')
  })

  it('a refused action lands on the class it was for', async () => {
    const w = await mountWith(page())
    axios.post.mockRejectedValueOnce({ response: { status: 403, data: { detail: {
      code: 'release_owner_only', message: "Only the agent's owner can let a class run unprompted again",
    } } } })
    await w.get('[data-testid="portal-autonomy-hold-vendor-renewal"]').trigger('click')
    await flushPromises()
    expect(w.text()).toContain("Only the agent's owner")
    expect(useClientPortalStore().autonomyBusy).toBeNull()
  })

  it('other seats are the owner view and collapsed', async () => {
    const w = await mountWith(page({
      can_release: true,
      other_seats: [{ ...klass({ seat: 'ops@example.com', ask_class: 'ops-approval', writable: true }) }],
    }))
    expect(w.get('[data-testid="portal-autonomy-other-seats"]').text()).toContain('Other seats · 1')
  })

  it('renders nothing at all for a seat with no ask classes', async () => {
    const w = await mountWith(page({ classes: [], other_seats: [] }))
    expect(w.find('[data-testid="portal-agent-autonomy"]').exists()).toBe(false)
  })
})

describe('clientPortal store — autonomy slice', () => {
  it('a response that lands after an agent switch is discarded', async () => {
    const store = useClientPortalStore()
    let resolveA
    axios.get.mockImplementationOnce(() => new Promise((r) => { resolveA = r }))
    const a = store.loadAgentAutonomy('agent-a')
    axios.get.mockResolvedValueOnce({ data: page({ agent_name: 'agent-b' }) })
    await store.loadAgentAutonomy('agent-b')
    resolveA({ data: page({ agent_name: 'agent-a' }) })
    await a
    expect(store.autonomyAgent).toBe('agent-b')
    expect(store.autonomy.agent_name).toBe('agent-b')
  })
})
