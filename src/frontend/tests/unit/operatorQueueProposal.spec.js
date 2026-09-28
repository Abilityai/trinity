// @vitest-environment jsdom
/**
 * trinity-enterprise#611 — an approval shows the exact action it asks a person
 * to approve.
 *
 * The prompt tells agents to put the action and its parameters in `proposal`
 * "so the operator can verify what they are approving". Until this, nothing in
 * the UI rendered `proposal`, so an agent that followed the prompt moved the one
 * thing the approver has to check out of view (review on #3028). One rule
 * (utils/operatorQueue.js::proposalRows), one read-only block
 * (components/operator/QueueProposal.vue), on every card that offers the
 * decision — MOUNTED (#2918).
 */
import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('axios', () => {
  const inst = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), defaults: { headers: { common: {} } } }
  return { default: inst }
})
vi.mock('@/api', () => {
  const inst = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }
  return { default: inst }
})

import { proposalRows } from '@/utils/operatorQueue'
import { useOperatorQueueStore } from '@/stores/operatorQueue'
import QueueCard from '@/components/operator/QueueCard.vue'
import ResolvedCard from '@/components/operator/ResolvedCard.vue'
import QueueProposal from '@/components/operator/QueueProposal.vue'

const PROPOSAL = { action: 'pay_invoice', amount: 500, currency: 'USDC', to: 'vendor-7',
                   dry_run: false, lines: [{ sku: 'a', qty: 2 }] }

const APPROVAL = {
  id: 'uuid-1', request_id: 'deploy-1', agent_name: 'scout', type: 'approval',
  priority: 'high', status: 'pending', title: 'Pay the vendor',
  question: 'Release the payment?', options: ['approve', 'reject'],
  created_at: '2026-09-28T10:00:00Z', context: {}, proposal: PROPOSAL,
}

describe('proposalRows — the one rule', () => {
  it('lists every top-level field, in the order the agent wrote them', () => {
    expect(proposalRows(PROPOSAL).map((r) => r.key))
      .toEqual(['action', 'amount', 'currency', 'to', 'dry_run', 'lines'])
  })

  it('shows text verbatim and every other value as compact JSON', () => {
    const byKey = Object.fromEntries(proposalRows(PROPOSAL).map((r) => [r.key, r.value]))
    expect(byKey).toEqual({
      action: 'pay_invoice', amount: '500', currency: 'USDC', to: 'vendor-7',
      dry_run: 'false', lines: '[{"sku":"a","qty":2}]',
    })
  })

  it('has no rows for no proposal, an empty one, or a value that is not an object', () => {
    for (const p of [null, undefined, {}, [], 'pay 500', 42]) {
      expect(proposalRows(p), JSON.stringify(p)).toEqual([])
    }
  })

  it('says a null field is null rather than dropping it', () => {
    expect(proposalRows({ target: null })).toEqual([{ key: 'target', value: 'null' }])
  })
})

function mountCard(item) {
  const pinia = createPinia()
  setActivePinia(pinia)
  useOperatorQueueStore().items = [item]
  return mount(QueueCard, { props: { item }, global: { plugins: [pinia], stubs: { AgentAvatar: true } } })
}

describe('the Operations card shows the proposal (mounted)', () => {
  it('shows every field once the card is open, without opening "Show details"', async () => {
    const w = mountCard(APPROVAL)
    await w.find('[data-testid="queue-card"]').trigger('click')   // open it, as an operator does
    const block = w.find('[data-testid="queue-proposal"]')
    expect(block.exists()).toBe(true)
    const rows = block.findAll('[data-testid="queue-proposal-row"]').map((r) => r.text())
    expect(rows).toHaveLength(6)
    expect(rows[1]).toContain('amount')
    expect(rows[1]).toContain('500')
    expect(rows[3]).toContain('vendor-7')
  })

  it('shows nothing when the ask carries no proposal', async () => {
    const w = mountCard({ ...APPROVAL, id: 'uuid-2', proposal: null })
    await w.find('[data-testid="queue-card"]').trigger('click')
    expect(w.find('[data-testid="queue-card"]').attributes('aria-expanded')).toBe('true')
    expect(w.find('[data-testid="queue-proposal"]').exists()).toBe(false)
  })
})

describe('the Resolved card keeps the proposal, so it says what was decided (mounted)', () => {
  const ENDED = { ...APPROVAL, id: 'uuid-9', status: 'cancelled', disposition: 'cancelled',
                  disposed_by: 'person', disposed_at: '2026-09-28T11:00:00Z' }

  function mountResolved(item) {
    const pinia = createPinia()
    setActivePinia(pinia)
    useOperatorQueueStore().items = [item]
    return mount(ResolvedCard, { props: { item }, global: { plugins: [pinia], stubs: { AgentAvatar: true } } })
  }

  it('shows every field on an ended ask', () => {
    const block = mountResolved(ENDED).find('[data-testid="queue-proposal"]')
    expect(block.exists()).toBe(true)
    expect(block.findAll('[data-testid="queue-proposal-row"]')).toHaveLength(6)
    expect(block.text()).toContain('vendor-7')
  })

  it('shows nothing when the ended ask carried no proposal', () => {
    expect(mountResolved({ ...ENDED, id: 'uuid-10', proposal: null }).find('[data-testid="queue-proposal"]').exists()).toBe(false)
  })
})

describe('the proposal is agent-authored text (mounted)', () => {
  it('renders markup as text and never as elements', () => {
    const w = mount(QueueProposal, { props: { proposal: { note: '<img src=x onerror="alert(1)">' } } })
    expect(w.find('img').exists()).toBe(false)
    expect(w.text()).toContain('<img src=x onerror="alert(1)">')
  })
})
