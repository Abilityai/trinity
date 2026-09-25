// @vitest-environment jsdom
/**
 * trinity-enterprise#611 (#627 AC6) — a re-ask and the expired ask it re-raises
 * name each other on the Operating Room cards. The successor carries the link
 * (`supersedes_expired` = the predecessor's platform id); the predecessor's side
 * is found among the loaded items. One rule (utils/operatorQueue.js::
 * queueReaskBadges), rendered by both cards — MOUNTED (#2918).
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

import { queueReaskBadges } from '@/utils/operatorQueue'
import { useOperatorQueueStore } from '@/stores/operatorQueue'
import QueueCard from '@/components/operator/QueueCard.vue'
import ResolvedCard from '@/components/operator/ResolvedCard.vue'

const EXPIRED = {
  id: 'uuid-old', request_id: 'approval-e1-deploy', agent_name: 'agent-a', type: 'approval',
  priority: 'high', title: 'Deploy v2?', question: 'Ship it?', options: ['approve', 'reject'],
  context: {}, created_at: '2026-09-25T08:00:00Z', status: 'expired', disposition: 'expired',
  disposed_by: 'timeout', disposed_at: '2026-09-25T09:00:00Z',
}
const REASK = {
  id: 'uuid-new', request_id: 'approval-e2-deploy', agent_name: 'agent-a', type: 'approval',
  priority: 'high', title: 'Deploy v2 (tests now green)?', question: 'Ship it?',
  options: ['approve', 'reject'], context: {}, created_at: '2026-09-25T09:30:00Z',
  status: 'pending', supersedes_expired: 'uuid-old',
}

describe('queueReaskBadges — the one rule', () => {
  it('the re-ask names the expired ask it re-raises', () => {
    expect(queueReaskBadges(REASK, [EXPIRED, REASK])).toEqual([
      { key: 'reask-of', prefix: 'Re-ask of', id: 'approval-e1-deploy', title: 'Re-asks "Deploy v2?", which expired unanswered.' },
    ])
  })

  it('the expired ask names the ask that re-raised it', () => {
    expect(queueReaskBadges(EXPIRED, [EXPIRED, REASK])).toEqual([
      { key: 'reasked-as', prefix: 'Re-asked as', id: 'approval-e2-deploy', title: 'The agent asked again after this expired: "Deploy v2 (tests now green)?".' },
    ])
  })

  it('a re-ask whose expired ask is not loaded still says it is a re-ask', () => {
    expect(queueReaskBadges(REASK, [REASK])).toEqual([
      { key: 'reask-of', prefix: 'Re-ask of an expired ask', id: null, title: 'Re-asks an earlier ask that expired unanswered.' },
    ])
  })

  it('an ask in a chain carries both links, one fact per badge', () => {
    const third = { ...REASK, id: 'uuid-3', request_id: 'approval-e3-deploy', supersedes_expired: 'uuid-new' }
    const middle = { ...REASK, status: 'expired' }
    expect(queueReaskBadges(middle, [EXPIRED, middle, third]).map((b) => [b.key, b.id]))
      .toEqual([['reask-of', 'approval-e1-deploy'], ['reasked-as', 'approval-e3-deploy']])
  })

  it('an ask with no link, or no ask at all, has no badge', () => {
    expect(queueReaskBadges({ ...EXPIRED, id: 'uuid-lone' }, [EXPIRED, REASK])).toEqual([])
    expect(queueReaskBadges(null, [EXPIRED])).toEqual([])
    expect(queueReaskBadges(REASK, undefined)[0].id).toBeNull()
  })
})

function mountWith(Component, item, items) {
  const pinia = createPinia()
  setActivePinia(pinia)
  useOperatorQueueStore().items = items
  return mount(Component, { props: { item }, global: { plugins: [pinia], stubs: { AgentAvatar: true } } })
}

describe('the cards render the link (mounted)', () => {
  it('a pending re-ask card says what it re-asks, with the id in full on hover', () => {
    const w = mountWith(QueueCard, REASK, [EXPIRED, REASK])
    const badge = w.find('[data-testid="queue-reask-of"]')
    expect(badge.text()).toBe('Re-ask of approval-e1-deploy')
    expect(badge.attributes('title')).toBe('Re-asks "Deploy v2?", which expired unanswered.')
    expect(badge.find('[data-testid="queue-reask-id"]').attributes('title')).toBe('approval-e1-deploy')
    expect(w.find('[data-testid="queue-reasked-as"]').exists()).toBe(false)
  })

  it('the expired card says what it was re-asked as', () => {
    const w = mountWith(ResolvedCard, EXPIRED, [EXPIRED, REASK])
    expect(w.find('[data-testid="queue-reasked-as"]').text()).toBe('Re-asked as approval-e2-deploy')
    expect(w.find('[data-testid="queue-reask-of"]').exists()).toBe(false)
  })

  it('an ended re-ask keeps saying what it re-asked', () => {
    const ended = { ...REASK, status: 'responded', response: 'approve', disposition: 'answered', disposed_at: '2026-09-25T10:00:00Z' }
    const w = mountWith(ResolvedCard, ended, [EXPIRED, ended])
    expect(w.find('[data-testid="queue-reask-of"]').text()).toBe('Re-ask of approval-e1-deploy')
  })

  it('cards with no link render no re-ask badge', () => {
    const lone = { ...REASK, id: 'uuid-lone', supersedes_expired: null }
    const q = mountWith(QueueCard, lone, [lone])
    const r = mountWith(ResolvedCard, { ...EXPIRED, id: 'uuid-x' }, [EXPIRED])
    for (const w of [q, r]) {
      expect(w.find('[data-testid="queue-reask-of"]').exists()).toBe(false)
      expect(w.find('[data-testid="queue-reasked-as"]').exists()).toBe(false)
    }
  })
})
