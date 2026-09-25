// @vitest-environment jsdom
/**
 * #2915 — the card RENDERS the badge and the refused-response notice (a mount,
 * per #2918: a regex over the SFC cannot prove a template branch). Copy of the
 * portalThemeSwitch.spec.js shape.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

vi.mock('axios', () => {
  const inst = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), defaults: { headers: { common: {} } } }
  return { default: inst }
})
vi.mock('@/api', () => {
  const inst = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }
  return { default: inst }
})

import QueueCard from '../../src/components/operator/QueueCard.vue'
import { useOperatorQueueStore } from '../../src/stores/operatorQueue'
import { SYNC_BADGE_COPY, QUEUE_RESPONSE_DIVERGED } from '../../src/utils/operatorQueue'

const base = {
  id: 'i1', agent_name: 'agent-a', type: 'approval', status: 'pending', priority: 'high',
  title: 'Approve payout', question: 'Release 500 USDC?', options: ['approve', 'reject'],
  context: {}, created_at: '2026-09-01T10:00:00Z',
}

function mountCard(item) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const wrapper = mount(QueueCard, {
    props: { item },
    global: { plugins: [pinia], stubs: { AgentAvatar: true } },
  })
  return { wrapper, store: useOperatorQueueStore() }
}

describe('QueueCard sync badge (mounted)', () => {
  beforeEach(() => { document.body.innerHTML = '' })

  it('renders no badge for a confirmed, unaged item', () => {
    const { wrapper } = mountCard({ ...base, sync_state: 'confirmed', aging: false })
    expect(wrapper.find('[data-testid="queue-sync-badge"]').exists()).toBe(false)
  })

  it('renders "Changed by the agent" from sync_state, with the field names on hover', () => {
    const { wrapper } = mountCard({ ...base, sync_state: 'changed', sync_detail: 'title,options' })
    const badge = wrapper.find('[data-testid="queue-sync-badge"]')
    expect(badge.exists()).toBe(true)
    expect(badge.text()).toBe(SYNC_BADGE_COPY.changed)
    expect(badge.attributes('title')).toContain('title, options')
  })

  it('renders "Waiting" from the server aging verdict', () => {
    const { wrapper } = mountCard({ ...base, sync_state: 'confirmed', aging: true, aged_since: '2026-09-02T10:00:00Z' })
    expect(wrapper.find('[data-testid="queue-sync-badge"]').text()).toBe(SYNC_BADGE_COPY.aging)
  })

  it('shows the refused-response notice beside the controls once the store marks the card diverged', async () => {
    const { wrapper, store } = mountCard({ ...base, sync_state: 'changed', sync_detail: 'title' })
    store.expandedItemId = 'i1'
    await nextTick()
    expect(wrapper.find('[data-testid="queue-diverged-notice"]').exists()).toBe(false)
    store.divergedItemId = 'i1'
    await nextTick()
    const notice = wrapper.find('[data-testid="queue-diverged-notice"]')
    expect(notice.exists()).toBe(true)
    expect(notice.text()).toContain(QUEUE_RESPONSE_DIVERGED)
  })

  it('a Send on a diverged card reaches the store, which carries the acknowledgement', async () => {
    const { wrapper, store } = mountCard({ ...base, sync_state: 'changed', sync_detail: 'title' })
    const spy = vi.spyOn(store, 'respondToItem').mockResolvedValue(undefined)
    store.expandedItemId = 'i1'
    store.divergedItemId = 'i1'
    await nextTick()
    const option = wrapper.findAll('button').find(b => b.text() === 'approve')
    await option.trigger('click')
    const send = wrapper.findAll('button').find(b => b.text() === 'Send')
    await send.trigger('click')
    expect(spy).toHaveBeenCalledWith('i1', 'approve', '')
  })
})
