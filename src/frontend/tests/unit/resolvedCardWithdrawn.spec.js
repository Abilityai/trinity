// @vitest-environment jsdom
/**
 * trinity-enterprise#901 (T3) — the Operating Room's resolved card shows the
 * agent's withdraw reason as its note; a replace still shows none (its
 * `disposition_reason` is the closed token `replaced`, not anyone's words).
 */
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ResolvedCard from '@/components/operator/ResolvedCard.vue'

const ended = (over = {}) => ({
  id: 'i1', agent_name: 'agent-a', type: 'approval', priority: 'high',
  title: 'Deploy the release?', question: 'Deploy it?', options: ['approve', 'reject'],
  created_at: '2026-10-14T09:00:00Z', status: 'cancelled', disposition: 'cancelled',
  disposed_by: 'agent', disposed_at: '2026-10-14T10:00:00Z', ...over,
})

function mountCard(item) {
  const pinia = createPinia()
  setActivePinia(pinia)
  return mount(ResolvedCard, { props: { item }, global: { plugins: [pinia], stubs: { AgentAvatar: true } } })
}

describe('ResolvedCard (mounted) — an agent withdraw', () => {
  it('says "Withdrawn by the agent" and shows the reason as the note', () => {
    const w = mountCard(ended({ agent_ending: 'withdrawn', disposition_reason: 'The build fixed itself.' }))
    expect(w.find('[data-testid="queue-ending"]').text()).toBe('Withdrawn by the agent')
    expect(w.find('[data-testid="resolved-note"]').text()).toContain('The build fixed itself.')
  })

  it('a replace shows no note', () => {
    const w = mountCard(ended({ agent_ending: 'replaced', disposition_reason: 'replaced', replaced_by: 'u2' }))
    expect(w.find('[data-testid="queue-ending"]').text()).toBe('Replaced by the agent')
    expect(w.find('[data-testid="resolved-note"]').exists()).toBe(false)
  })
})
