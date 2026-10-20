// @vitest-environment jsdom
/**
 * trinity-enterprise#844 (review) — the queue DETAIL panel is one of the
 * surfaces that reports an ask's ending. Before the review fix it rendered
 * `queueEndingText` only for a platform ending, so an approval the clock ended
 * fell through to the "person answered" branch ("by  · …") and the
 * `outcome_unknown` marker never reached the operator there.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { reactive } from 'vue'

const store = reactive({ selectedItem: null, selectedItemId: null })

vi.mock('@/stores/operatorQueue', () => ({ useOperatorQueueStore: () => store }))
vi.mock('@/stores/agents', () => ({ useAgentsStore: () => ({ agents: [], agentRefForSlug: (name) => ({ name }) }) }))

import QueueItemDetail from '@/components/operator/QueueItemDetail.vue'

const expired = (over = {}) => ({
  id: 'e1', agent_name: 'agent-a', type: 'approval', priority: 'high',
  title: 'Merge the canon PR', question: 'Merge it?', options: ['approve', 'reject'],
  created_at: '2026-09-01T00:00:00Z', status: 'expired', disposition: 'expired',
  disposed_by: 'timeout', disposed_at: '2026-09-13T00:00:00Z', response: null,
  responded_by_email: null, responded_at: null, ...over,
})

function mountWith(item) {
  store.selectedItem = item
  store.selectedItemId = item.id
  return mount(QueueItemDetail)
}

describe('QueueItemDetail — an ending the clock wrote', () => {
  beforeEach(() => { store.selectedItem = null })

  it('names the unknown outcome of an unheld approval', () => {
    const w = mountWith(expired({ disposition_reason: 'outcome_unknown' }))
    expect(w.find('[data-testid="queue-detail-ending"]').text())
      .toContain('Expired — nobody answered in time; whether the action went ahead is unknown')
    expect(w.text()).not.toContain('outcome_unknown')
  })

  it('keeps the plain expiry wording for a gate approval, never a person-answer line', () => {
    const w = mountWith(expired({ raised_by: 'gate', disposition_reason: null }))
    const ending = w.find('[data-testid="queue-detail-ending"]')
    expect(ending.text()).toContain('Expired — nobody answered in time')
    expect(ending.text()).not.toContain('unknown')
    expect(w.find('[data-testid="detail-response"]').exists()).toBe(false)
  })
})
