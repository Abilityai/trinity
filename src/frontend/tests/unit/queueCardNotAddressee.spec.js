// @vitest-environment jsdom
/**
 * trinity-enterprise#751 — a gated-skill approval is decided only by the person
 * it was addressed to. Anyone else's answer is refused (403 `not_addressee`) and
 * nothing is recorded. The card must say so beside its controls and keep saying
 * it (design-system principle 18). Before this, the refusal fell through to the
 * queue's refresh banner — "Couldn't refresh the queue" — and the next poll's
 * `fetchItems` wiped it, so a second click looked identical (eyeball 2026-10-03).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
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

import axios from 'axios'
import QueueCard from '../../src/components/operator/QueueCard.vue'
import { useOperatorQueueStore } from '../../src/stores/operatorQueue'
import { QUEUE_RESPONSE_NOT_ADDRESSEE } from '../../src/utils/operatorQueue'

const item = {
  id: 'i1', agent_name: 'testfix', request_id: 'gate-abc', type: 'approval', status: 'pending',
  priority: 'high', raised_by: 'gate', title: 'Approve the skill pay-invoice on testfix',
  question: 'Someone asked testfix to run the skill pay-invoice.', options: ['Approve', 'Reject'],
  context: {}, created_at: '2026-10-03T10:00:00Z',
}

const REFUSED = {
  response: {
    status: 403,
    data: { detail: { code: 'not_addressee', message: 'This approval was addressed to someone else; only they can decide it.' } },
  },
}

async function mountExpanded() {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = useOperatorQueueStore()
  store.items = [{ ...item }]
  store.expandedItemId = item.id
  const wrapper = mount(QueueCard, {
    props: { item: store.items[0] },
    global: { plugins: [pinia], stubs: { AgentAvatar: true } },
  })
  await nextTick()
  return { wrapper, store }
}

async function approve(wrapper) {
  const buttons = () => wrapper.findAll('button')
  await buttons().find(b => b.text() === 'Approve').trigger('click')
  await buttons().find(b => b.text() === 'Send').trigger('click')
  await flushPromises()
}

const notice = wrapper => wrapper.find('[data-testid="queue-not-addressee-notice"]')

describe('QueueCard — an answer refused because it was not addressed to you (mounted)', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
    vi.clearAllMocks()
  })

  it('says why on the card, not in the refresh banner, and records nothing', async () => {
    const { wrapper, store } = await mountExpanded()
    axios.post.mockRejectedValueOnce(REFUSED)
    await approve(wrapper)

    expect(axios.post).toHaveBeenCalledTimes(1)
    expect(notice(wrapper).exists()).toBe(true)
    expect(notice(wrapper).text()).toContain(QUEUE_RESPONSE_NOT_ADDRESSEE)
    expect(store.error).toBeNull()
    expect(store.items[0].status).toBe('pending')
  })

  it('survives the next queue refresh', async () => {
    const { wrapper, store } = await mountExpanded()
    axios.post.mockRejectedValueOnce(REFUSED)
    await approve(wrapper)

    axios.get.mockResolvedValueOnce({ data: { items: [{ ...item }], count: 1 } })
    await store.fetchItems()
    await nextTick()
    expect(notice(wrapper).exists()).toBe(true)
  })

  it('is cleared when dismissed', async () => {
    const { wrapper } = await mountExpanded()
    axios.post.mockRejectedValueOnce(REFUSED)
    await approve(wrapper)

    await notice(wrapper).find('button[aria-label]').trigger('click')
    await nextTick()
    expect(notice(wrapper).exists()).toBe(false)
  })
})
