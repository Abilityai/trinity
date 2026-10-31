// @vitest-environment jsdom
/**
 * trinity-enterprise#74 — an anomaly alarm on the Needs Response card links
 * to the agent's Schedules tab, where the incident's evidence is. Any other
 * ask shows no such link. Copy of the queueCardSyncBadge.spec.js shape.
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

import { nextTick } from 'vue'
import QueueCard from '../../src/components/operator/QueueCard.vue'
import { useOperatorQueueStore } from '../../src/stores/operatorQueue'

const base = {
  id: 'i1', agent_name: 'acme-scout', type: 'alert', status: 'pending', priority: 'medium',
  title: 'Schedule “Daily digest” is running off its baseline', question: 'Run x departed from history.',
  options: [], context: {}, created_at: '2026-10-20T10:00:00Z',
}

const RouterLinkStub = {
  props: ['to'],
  template: '<a :data-to="JSON.stringify(to)"><slot /></a>',
}

// The link sits in the card's expanded body, where the question and context are.
async function mountCard(item) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const w = mount(QueueCard, {
    props: { item },
    global: { plugins: [pinia], stubs: { AgentAvatar: true, RouterLink: RouterLinkStub } },
  })
  useOperatorQueueStore().toggleExpand(item.id)
  await nextTick()
  return w
}

describe('QueueCard anomaly link (mounted)', () => {
  beforeEach(() => { document.body.innerHTML = '' })

  it('an anomaly alarm links to the agent\'s Schedules tab', async () => {
    const w = await mountCard({ ...base, context: { kind: 'schedule_anomaly', incident_id: 'inc-1', schedule_id: 's1', execution_id: 'e1' } })
    const link = w.find('[data-testid="queue-anomaly-link"]')
    expect(link.exists()).toBe(true)
    expect(JSON.parse(link.attributes('data-to'))).toEqual({ path: '/agents/acme-scout', query: { tab: 'schedules' } })
  })

  it('any other ask has no such link', async () => {
    const w = await mountCard({ ...base, context: { kind: 'something_else' } })
    expect(w.find('[data-testid="queue-anomaly-link"]').exists()).toBe(false)
    expect((await mountCard(base)).find('[data-testid="queue-anomaly-link"]').exists()).toBe(false)
  })
})
