// @vitest-environment jsdom
/**
 * trinity-enterprise#844 — an approval the clock ended without the platform
 * holding its action (`disposition_reason = 'outcome_unknown'`) says so on every
 * surface that renders an ending (ResolvedCard, the queue detail, /m), through
 * the one rule `queueEndingText`. A gate approval (the platform held it), a
 * question and a legacy row keep the plain "nobody answered in time".
 */
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { queueEnding, queueEndingText, TIMEOUT_ENDING_REASONS } from '@/utils/operatorQueue'
import ResolvedCard from '@/components/operator/ResolvedCard.vue'

const expired = (over = {}) => ({
  id: 'i1', agent_name: 'agent-a', type: 'approval', priority: 'high',
  title: 'Merge the canon PR', question: 'Merge it?', options: ['approve', 'reject'],
  created_at: '2026-09-01T00:00:00Z', status: 'expired', disposition: 'expired',
  disposed_by: 'timeout', disposed_at: '2026-09-13T00:00:00Z', ...over,
})

describe('queueEndingText — an expiry that cannot vouch for the action', () => {
  it('names the unknown outcome', () => {
    expect(queueEndingText(queueEnding(expired({ disposition_reason: 'outcome_unknown' }))))
      .toBe('Expired — nobody answered in time; whether the action went ahead is unknown')
  })

  it.each([
    [{ disposition_reason: null }],
    [{ raised_by: 'gate', disposition_reason: null }],
    [{ type: 'question', disposition_reason: null }],
    [{ disposition_reason: 'some_token_from_the_future' }],
  ])('keeps the plain wording otherwise: %o', (over) => {
    expect(queueEndingText(queueEnding(expired(over)))).toBe('Expired — nobody answered in time')
  })

  it('names every reason the backend writes on a timeout', () => {
    expect(Object.keys(TIMEOUT_ENDING_REASONS)).toEqual(['outcome_unknown'])
  })
})

describe('ResolvedCard (mounted)', () => {
  it('words the marker and never prints the raw token as a note', () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const w = mount(ResolvedCard, {
      props: { item: expired({ disposition_reason: 'outcome_unknown' }) },
      global: { plugins: [pinia], stubs: { AgentAvatar: true } },
    })
    expect(w.find('[data-testid="queue-ending"]').text())
      .toBe('Expired — nobody answered in time; whether the action went ahead is unknown')
    expect(w.text()).not.toContain('outcome_unknown')
  })
})
