// @vitest-environment jsdom
/**
 * trinity-enterprise#611 — how an ask ENDED, as ONE rule every surface renders
 * (ResolvedCard, /m, PortalAsks), plus the surfaces that consume it, MOUNTED
 * (#2918: a regex over the SFC cannot prove a template branch).
 *
 * The rule's two promises: who + when come from the endings ledger
 * (`disposition` / `disposed_by*` / `disposed_at`), and a row that ended
 * before the ledger never presents its FILING time as its ending time.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
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

import axios from 'axios'
import api from '@/api'
import {
  queueEnding, queueEndingText, queueEndingSortTime, recentlyEnded, ENDING_LABELS,
} from '@/utils/operatorQueue'
import { triggerLabel } from '@/utils/executionFailure'
import { useOperatorQueueStore } from '@/stores/operatorQueue'
import ResolvedCard from '@/components/operator/ResolvedCard.vue'
import ReliabilityPanel from '@/components/ReliabilityPanel.vue'

const FILED = '2026-01-01T00:00:00Z'
const ENDED = '2026-09-25T10:00:00.000000Z'

const row = (over = {}) => ({
  id: 'i1', agent_name: 'agent-a', type: 'approval', priority: 'high',
  title: 'Approve payout', question: 'Release 500 USDC?', options: ['approve', 'reject'],
  created_at: FILED, status: 'pending', ...over,
})

describe('queueEnding — the one rule', () => {
  it('is null while the ask is still pending', () => {
    expect(queueEnding(row())).toBeNull()
    expect(queueEnding(null)).toBeNull()
  })

  it.each([
    [{ status: 'responded', disposition: 'answered', disposed_by: 'person', disposed_by_email: 'op@example.com', disposed_at: ENDED },
      { kind: 'answered', who: 'op@example.com', when: ENDED }],
    [{ status: 'acknowledged', disposition: 'answered', disposed_by_email: 'op@example.com', disposed_at: ENDED },
      { kind: 'answered', who: 'op@example.com', when: ENDED }],
    [{ status: 'cancelled', disposition: 'cancelled', disposed_by_email: 'op@example.com', disposed_at: ENDED },
      { kind: 'cancelled', who: 'op@example.com', when: ENDED }],
    [{ status: 'expired', disposition: 'expired', disposed_by: 'timeout', disposed_at: ENDED },
      { kind: 'expired', who: 'timeout', when: ENDED }],
  ])('reads who and when from the ledger: %o', (over, expected) => {
    const e = queueEnding(row(over))
    expect({ kind: e.kind, who: e.who, when: e.when }).toEqual(expected)
    expect(e.label).toBe(ENDING_LABELS[expected.kind])
  })

  it.each([
    [{ status: 'responded', responded_by_email: 'op@example.com', responded_at: ENDED },
      { kind: 'answered', who: 'op@example.com', when: ENDED }],
    [{ status: 'cancelled' }, { kind: 'cancelled', who: null, when: null }],
    [{ status: 'expired' }, { kind: 'expired', who: 'timeout', when: null }],
  ])('a row that ended before the ledger never borrows its filing time: %o', (over, expected) => {
    const e = queueEnding(row(over))
    expect({ kind: e.kind, who: e.who, when: e.when }).toEqual(expected)
    expect(e.when).not.toBe(FILED)
  })

  it.each([
    [{ status: 'answered', ended_by: 'you', ended_at: ENDED }, { kind: 'answered', who: 'you' }],
    [{ status: 'answered', ended_by: 'operator', ended_at: ENDED }, { kind: 'answered', who: 'the operator' }],
    [{ status: 'cancelled', ended_by: 'operator', ended_at: ENDED }, { kind: 'cancelled', who: 'the operator' }],
    [{ status: 'expired', ended_by: 'timeout', ended_at: ENDED }, { kind: 'expired', who: 'timeout' }],
  ])('reads the Workspace projection’s coarse who: %o', (over, expected) => {
    const e = queueEnding(row(over))
    expect({ kind: e.kind, who: e.who }).toEqual(expected)
    expect(e.when).toBe(ENDED)
  })

  it('says the ending in words — who ended it, or that time did', () => {
    expect(queueEndingText(queueEnding(row({ status: 'cancelled', disposition: 'cancelled', disposed_by_email: 'op@example.com' }))))
      .toBe('Cancelled by op@example.com')
    expect(queueEndingText(queueEnding(row({ status: 'answered', ended_by: 'you' })))).toBe('Answered by you')
    expect(queueEndingText(queueEnding(row({ status: 'expired', disposition: 'expired' }))))
      .toBe('Expired — nobody answered in time')
    expect(queueEndingText(queueEnding(row({ status: 'cancelled' })))).toBe('Cancelled')
    expect(queueEndingText(null)).toBe('')
  })

  it('sorts ended rows by when they ended, a legacy answer by its answer time, else by filing time', () => {
    expect(queueEndingSortTime(row({ status: 'cancelled', disposed_at: ENDED }))).toBe(ENDED)
    expect(queueEndingSortTime(row({ status: 'responded', responded_at: ENDED }))).toBe(ENDED)
    expect(queueEndingSortTime(row({ status: 'cancelled' }))).toBe(FILED)
  })

  it('the /m strip takes the most recent endings, pending rows never', () => {
    const items = [
      row({ id: 'p' }),
      row({ id: 'old', status: 'cancelled', disposition: 'cancelled', disposed_at: '2026-09-20T00:00:00Z' }),
      row({ id: 'new', status: 'expired', disposition: 'expired', disposed_at: '2026-09-25T00:00:00Z' }),
      row({ id: 'mid', status: 'responded', disposition: 'answered', disposed_at: '2026-09-22T00:00:00Z' }),
    ]
    expect(recentlyEnded(items, 2).map((i) => i.id)).toEqual(['new', 'mid'])
    expect(recentlyEnded(items).map((i) => i.id)).toEqual(['new', 'mid', 'old'])
    expect(recentlyEnded(null)).toEqual([])
  })
})

describe('the wake triggers render under their own names', () => {
  it('operator_response and operator_ending are known triggers, not "other"', () => {
    expect(triggerLabel('operator_response')).toBe('operator_response')
    expect(triggerLabel('operator_ending')).toBe('operator_ending')
  })
})

describe('the Operating Room store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('the resolved feed is ordered by when each ask ended (#627 AC6)', () => {
    const store = useOperatorQueueStore()
    store.items = [
      row({ id: 'filed-late-ended-early', created_at: '2026-09-24T00:00:00Z', status: 'cancelled', disposed_at: '2026-09-24T01:00:00Z' }),
      row({ id: 'filed-early-ended-late', created_at: '2026-01-01T00:00:00Z', status: 'cancelled', disposed_at: '2026-09-25T09:00:00Z' }),
    ]
    expect(store.resolvedItems.map((i) => i.id)).toEqual(['filed-early-ended-late', 'filed-late-ended-early'])
  })

  it.each(['operator_queue_responded', 'operator_queue_cancelled'])(
    'a thin %s trigger refetches the access-controlled list', (type) => {
      axios.get.mockResolvedValue({ data: { items: [], count: 0 } })
      const store = useOperatorQueueStore()
      store.handleWebSocketEvent({ type, data: { id: 'i1', agent_name: 'agent-a' } })
      expect(axios.get).toHaveBeenCalledTimes(1)
    },
  )
})

describe('ResolvedCard renders the ending (mounted)', () => {
  function mountCard(item) {
    const pinia = createPinia()
    setActivePinia(pinia)
    return mount(ResolvedCard, { props: { item }, global: { plugins: [pinia], stubs: { AgentAvatar: true } } })
  }

  it('a cancellation says who cancelled it, why, and when — absolute time on hover', () => {
    const w = mountCard(row({
      status: 'cancelled', disposition: 'cancelled', disposed_by: 'person',
      disposed_by_email: 'op@example.com', disposed_at: ENDED, disposition_reason: 'duplicate of #2',
    }))
    expect(w.find('[data-testid="queue-ending"]').text()).toBe('Cancelled by op@example.com')
    expect(w.text()).toContain('duplicate of #2')
    const when = w.find('[data-testid="queue-ending-when"]')
    expect(when.text()).not.toBe('')
    expect(when.attributes('title')).toContain('2026')
  })

  it('an expiry says nobody answered in time', () => {
    const w = mountCard(row({ status: 'expired', disposition: 'expired', disposed_by: 'timeout', disposed_at: ENDED }))
    expect(w.find('[data-testid="queue-ending"]').text()).toBe('Expired — nobody answered in time')
  })

  it('an answer names who answered', () => {
    const w = mountCard(row({
      status: 'responded', response: 'approve', responded_at: ENDED, responded_by_email: 'op@example.com',
      disposition: 'answered', disposed_by_email: 'op@example.com', disposed_at: ENDED,
    }))
    expect(w.text()).toContain('approve')
    expect(w.find('[data-testid="queue-ending-when"]').text()).toContain('by op@example.com')
  })

  it('a row that ended before the ledger shows no invented ending time', () => {
    const w = mountCard(row({ status: 'cancelled' }))
    expect(w.find('[data-testid="queue-ending"]').text()).toBe('Cancelled')
    const when = w.find('[data-testid="queue-ending-when"]')
    expect(when.text()).toBe('')
    expect(when.attributes('title')).toBeUndefined()
  })
})

describe('the wake setting says what it spends on (mounted)', () => {
  it('names every ending, not only an answer', async () => {
    api.get.mockResolvedValue({ data: {} })
    const w = mount(ReliabilityPanel, { props: { agentName: 'agent-a' } })
    await new Promise((r) => setTimeout(r, 0))
    expect(w.text()).toContain('when an ask it raised ends — answered, cancelled or expired')
    expect(w.text()).not.toContain('Each answer then costs one agent turn')
  })
})
