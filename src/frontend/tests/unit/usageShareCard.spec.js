// @vitest-environment jsdom
/**
 * trinity-enterprise#1059 C — an agent's share of its subscription's usage,
 * as the owner reads and changes it.
 *
 *   - Settings › Guardrails › Usage share: both rolling windows as used vs
 *     budget, where the warning starts, when a window frees, and the owner's
 *     raise through `PUT /api/enterprise/agents/{name}/usage-share` (an empty
 *     field is the platform default; all three empty clears the override).
 *   - Settings › Subscriptions › per-agent breakdown: a "share used" column,
 *     only when the instance is entitled to the usage share.
 *
 * Every predicate here gates a store write or a rendered claim about money,
 * so the real components are mounted (#2918); nothing reads SFC source.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('../../src/api', () => ({
  default: { get: vi.fn(), put: vi.fn(), post: vi.fn(), delete: vi.fn() },
}))
vi.mock('axios', () => ({
  default: { get: vi.fn(), put: vi.fn(), post: vi.fn(), delete: vi.fn() },
}))

import api from '../../src/api'
import axios from 'axios'
import UsageShareCard from '../../src/components/agent-settings/UsageShareCard.vue'
import GuardrailsPane from '../../src/components/agent-settings/GuardrailsPane.vue'
import SubscriptionsPanel from '../../src/components/settings/SubscriptionsPanel.vue'
import { useAgentsStore } from '../../src/stores/agents'
import { useEnterpriseStore } from '../../src/stores/enterprise'

const SHARE_URL = '/api/enterprise/agents/beta/usage-share'

function win(o = {}) {
  return { budget: 100, used: 10, pct: 10, frees_at: null, at_budget: false, ask_filed_at: null, ...o }
}

function shareView(o = {}) {
  return {
    agent_name: 'beta',
    window_5h: win({ budget: 15, used: 3, pct: 20 }),
    window_7d: win({ budget: 100, used: 42, pct: 42 }),
    warn_pct: 80,
    sources: { budget_7d: 'platform', budget_5h: 'platform', warn_pct: 'platform' },
    notice: null,
    ...o,
  }
}

async function mountCard(view = shareView()) {
  api.get.mockResolvedValue({ data: view })
  const notify = vi.fn()
  const w = mount(UsageShareCard, { props: { agentName: 'beta', notify } })
  await flushPromises()
  return { w, notify }
}

const tid = (id) => `[data-testid="${id}"]`

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

describe('Usage share card — what it shows', () => {
  it('reads the share from the enterprise route through the agents store', async () => {
    const spy = vi.spyOn(useAgentsStore(), 'getUsageShare')
    await mountCard()
    expect(spy).toHaveBeenCalledWith('beta')
    expect(api.get).toHaveBeenCalledWith(SHARE_URL)
  })

  it('shows both windows as used vs budget with the percent', async () => {
    const { w } = await mountCard()
    expect(w.get(tid('usage-share-7d')).text()).toMatch(/42\s*\/\s*100/)
    expect(w.get(tid('usage-share-7d')).text()).toMatch(/42%/)
    expect(w.get(tid('usage-share-5h')).text()).toMatch(/3\s*\/\s*15/)
    expect(w.get(tid('usage-share-5h')).text()).toMatch(/20%/)
  })

  it('draws each window as a meter with the warning line at warn_pct', async () => {
    const { w } = await mountCard(shareView({ warn_pct: 70 }))
    const meter = w.get(`${tid('usage-share-7d')} [role="meter"]`)
    expect(meter.attributes('aria-valuenow')).toBe('42')
    const mark = w.get(`${tid('usage-share-7d')} ${tid('usage-share-warn-mark')}`)
    expect(mark.attributes('style')).toMatch(/left:\s*70%/)
    expect(w.text()).toMatch(/warned from 70%/i)
  })

  it('says nothing is wrong below the warning line', async () => {
    const { w } = await mountCard()
    expect(w.find(tid('usage-share-7d-state')).exists()).toBe(false)
  })

  it('marks a window in the warning band and one at the budget, in words', async () => {
    const { w } = await mountCard(shareView({
      window_5h: win({ budget: 15, used: 15, pct: 100, at_budget: true, frees_at: '2026-11-02T09:00:00+00:00' }),
      window_7d: win({ budget: 100, used: 85, pct: 85 }),
    }))
    expect(w.get(tid('usage-share-7d-state')).text()).toMatch(/warning/i)
    expect(w.get(tid('usage-share-5h-state')).text()).toMatch(/stopped/i)
    // At the budget the person is told when the window frees.
    expect(w.get(tid('usage-share-5h')).text()).toMatch(/frees/i)
  })

  it('says a stop ask is pending when the platform filed one', async () => {
    const { w } = await mountCard(shareView({
      window_7d: win({ budget: 100, used: 120, pct: 120, at_budget: true, ask_filed_at: '2026-11-02T05:00:00Z' }),
    }))
    expect(w.get(tid('usage-share-7d')).text()).toMatch(/ask .*pending/i)
    // An over-budget meter never draws past its track.
    expect(w.get(`${tid('usage-share-7d')} ${tid('usage-share-fill')}`).attributes('style')).toMatch(/width:\s*100%/)
  })

  it("shows the line the agent reads on its next turn when there is one", async () => {
    const { w } = await mountCard(shareView({ notice: 'You have used 85% of your 7-day usage share.' }))
    expect(w.get(tid('usage-share-notice')).text()).toMatch(/85% of your 7-day usage share/)
  })

  it('names where each number comes from', async () => {
    const { w } = await mountCard(shareView({ sources: { budget_7d: 'agent', budget_5h: 'platform', warn_pct: 'platform' } }))
    expect(w.get(tid('usage-share-7d')).text()).toMatch(/set for this agent/i)
    expect(w.get(tid('usage-share-5h')).text()).toMatch(/platform default/i)
  })

  it('a failed read renders the failed state, never zero usage', async () => {
    api.get.mockRejectedValue(new Error('boom'))
    const w = mount(UsageShareCard, { props: { agentName: 'beta' } })
    await flushPromises()
    expect(w.find(tid('load-failed')).exists()).toBe(true)
    expect(w.find(tid('usage-share-7d')).exists()).toBe(false)
  })
})

describe('Usage share card — the owner changes it', () => {
  async function openEditor(view) {
    const ctx = await mountCard(view)
    await ctx.w.get(tid('usage-share-edit')).trigger('click')
    return ctx
  }
  const field = (w, id) => w.get(`${tid(`usage-share-input-${id}`)} input`)

  it('opens pre-filled with the agent override and the platform default as placeholder', async () => {
    const { w } = await openEditor(shareView({
      window_7d: win({ budget: 250, used: 42, pct: 16 }),
      sources: { budget_7d: 'agent', budget_5h: 'platform', warn_pct: 'platform' },
    }))
    expect(field(w, 'budget_7d').element.value).toBe('250')
    expect(field(w, 'budget_5h').element.value).toBe('')
    expect(field(w, 'budget_5h').attributes('placeholder')).toMatch(/15/)
  })

  it('raises the share through the PUT and shows the answer', async () => {
    const { w, notify } = await openEditor()
    api.put.mockResolvedValue({ data: shareView({
      window_7d: win({ budget: 300, used: 42, pct: 14 }),
      sources: { budget_7d: 'agent', budget_5h: 'platform', warn_pct: 'platform' },
    }) })
    await field(w, 'budget_7d').setValue('300')
    await w.get(tid('usage-share-save')).trigger('click')
    await flushPromises()
    expect(api.put).toHaveBeenCalledWith(SHARE_URL, { budget_7d: 300, budget_5h: null, warn_pct: null })
    expect(w.get(tid('usage-share-7d')).text()).toMatch(/42\s*\/\s*300/)
    expect(notify).toHaveBeenCalledWith(expect.stringMatching(/next turn/i), 'success')
    expect(w.find(tid('usage-share-save')).exists()).toBe(false)
  })

  it('all fields empty clears the override back to the platform default', async () => {
    const { w } = await openEditor(shareView({
      window_7d: win({ budget: 250 }),
      sources: { budget_7d: 'agent', budget_5h: 'platform', warn_pct: 'platform' },
    }))
    api.put.mockResolvedValue({ data: shareView() })
    await field(w, 'budget_7d').setValue('')
    await w.get(tid('usage-share-save')).trigger('click')
    await flushPromises()
    expect(api.put).toHaveBeenCalledWith(SHARE_URL, { budget_7d: null, budget_5h: null, warn_pct: null })
  })

  it.each([
    ['budget_7d', '0', /above 0/i],
    ['budget_5h', '100001', /100000/],
    ['warn_pct', '49', /50 to 99/],
    ['warn_pct', '100', /50 to 99/],
    ['warn_pct', '80.5', /whole/i],
  ])('refuses %s = %s client-side with a named reason and sends nothing', async (id, value, reason) => {
    const { w } = await openEditor()
    await field(w, id).setValue(value)
    await w.get(tid('usage-share-save')).trigger('click')
    await flushPromises()
    expect(api.put).not.toHaveBeenCalled()
    expect(w.get(tid(`usage-share-input-${id}`)).text()).toMatch(reason)
  })

  it('a refused PUT keeps the form open and says why next to it', async () => {
    const { w } = await openEditor()
    api.put.mockRejectedValue({ response: { status: 403, data: { detail: { code: 'person_required', message: 'Only a person can raise a share.' } } } })
    await field(w, 'budget_7d').setValue('300')
    await w.get(tid('usage-share-save')).trigger('click')
    await flushPromises()
    expect(w.get(tid('inline-error')).text()).toMatch(/Only a person can raise a share/)
    expect(w.find(tid('usage-share-save')).exists()).toBe(true)
  })
})

describe('Guardrails pane — the card is behind the entitlement', () => {
  const stubs = {
    GuardrailsPanel: true, SelfChangePermissionsPanel: true, CrossModelValidationPanel: true,
    ReliabilityPanel: true, RetrievalMemoryToggle: true, RetrievalMemorySearch: true,
    UsageShareCard: { template: '<div data-testid="usage-share-card-stub" />' },
  }

  async function mountPane(features) {
    useEnterpriseStore()._setFeaturesForTest(features)
    const agents = useAgentsStore()
    vi.spyOn(agents, 'getGuardrails').mockResolvedValue({ guardrails: {} })
    api.get.mockResolvedValue({ data: [] })
    axios.get.mockResolvedValue({ data: [] })
    const w = mount(GuardrailsPane, { props: { agentName: 'beta' }, global: { stubs } })
    await flushPromises()
    return w
  }

  it('renders the usage share card when entitled', async () => {
    const w = await mountPane(['usage_share'])
    expect(w.find(tid('usage-share-card-stub')).exists()).toBe(true)
  })

  it('renders nothing of it on a build without the entitlement', async () => {
    const w = await mountPane([])
    expect(w.find(tid('usage-share-card-stub')).exists()).toBe(false)
  })
})

describe('Subscriptions breakdown — share used column', () => {
  const SUB = { id: 'sub-1', name: 'team-max', subscription_type: 'max', agent_count: 2, agents: ['beta', 'gamma'], owner_email: 'owner@example.com' }
  const BREAKDOWN = {
    window_7d: [
      { agent_name: 'beta', cost_usd: 42, output_tokens: 1000, message_count: 3 },
      { agent_name: 'gamma', cost_usd: 5, output_tokens: 10, message_count: 1 },
    ],
    window_5h: [{ agent_name: 'beta', cost_usd: 3 }],
  }

  async function openBreakdown(features, shareFor = {}) {
    useEnterpriseStore()._setFeaturesForTest(features)
    axios.get.mockImplementation(async (url) => {
      if (url === '/api/subscriptions') return { data: [SUB] }
      if (url === '/api/subscriptions/encryption-status') return { data: { configured: true } }
      if (url === '/api/agents') return { data: [] }
      return { data: {} }
    })
    api.get.mockImplementation(async (url) => {
      if (url.endsWith('/usage/breakdown')) return { data: BREAKDOWN }
      const m = url.match(/^\/api\/enterprise\/agents\/([^/]+)\/usage-share$/)
      if (m) {
        if (shareFor[m[1]]) return { data: shareFor[m[1]] }
        throw Object.assign(new Error('not found'), { response: { status: 404 } })
      }
      if (url.endsWith('/usage')) return { data: { window_5h: {}, window_7d: {}, agents: [] } }
      return { data: {} }
    })
    const w = mount(SubscriptionsPanel, { global: { stubs: { RouterLink: true } } })
    await flushPromises()
    const row = w.findAll('tr').find((r) => r.classes().includes('cursor-pointer') && r.text().includes('team-max'))
    await row.trigger('click')
    await flushPromises()
    const toggle = w.findAll('button').find((b) => /per-agent breakdown/.test(b.text()))
    await toggle.trigger('click')
    await flushPromises()
    return w
  }

  it('adds the share used column with both windows when entitled', async () => {
    const w = await openBreakdown(['usage_share'], {
      beta: shareView({ window_7d: win({ pct: 42 }), window_5h: win({ budget: 15, pct: 20 }) }),
    })
    expect(w.text()).toMatch(/Share used/)
    const cell = w.get(tid('share-used-beta'))
    expect(cell.text()).toMatch(/42%/)
    expect(cell.text()).toMatch(/20%/)
    // An agent whose share the reader cannot see renders a dash, not 0%.
    expect(w.get(tid('share-used-gamma')).text()).toBe('—')
  })

  it('has no share column and asks for no share on a build without the entitlement', async () => {
    const w = await openBreakdown([])
    expect(w.text()).not.toMatch(/Share used/)
    expect(api.get.mock.calls.some(([u]) => /usage-share/.test(u))).toBe(false)
  })
})
