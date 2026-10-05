// @vitest-environment jsdom
/**
 * #3242 — the detail panel's "Something else": the store call it makes (the
 * store is mocked here; the POSTed body is proven in operatorQueueSomethingElse
 * .spec.js), the label flip, clear-on-success only, and the resolved label.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { reactive } from 'vue'

const store = reactive({ selectedItem: null, selectedItemId: null, respondToItem: vi.fn() })

vi.mock('@/stores/operatorQueue', () => ({ useOperatorQueueStore: () => store }))
vi.mock('@/stores/agents', () => ({ useAgentsStore: () => ({ agents: [], agentRefForSlug: (name) => ({ name }) }) }))

import QueueItemDetail from '@/components/operator/QueueItemDetail.vue'

const item = (over = {}) => ({
  id: 'd1', request_id: 'approval-1', agent_name: 'a', type: 'approval', status: 'pending',
  priority: 'high', title: 't', question: 'q', options: ['Approve', 'Deny'],
  created_at: '2026-10-05T00:00:00Z', ...over,
})

function mountWith(it) {
  store.selectedItem = it
  store.selectedItemId = it.id
  return mount(QueueItemDetail)
}

describe('QueueItemDetail — Something else (#3242)', () => {
  beforeEach(() => { store.respondToItem = vi.fn() })

  it('typing arms it: the label says the instruction is required, and Submit sends the literal', async () => {
    store.respondToItem.mockResolvedValue(true)
    const w = mountWith(item())
    await w.find('[data-testid="approval-note"]').setValue('Use the blue bucket')
    expect(w.find('[data-testid="detail-note-label"]').text()).toBe('Instruction (required)')
    expect(w.find('[data-testid="approval-send"]').text()).toBe('Submit instruction')
    await w.find('[data-testid="approval-send"]').trigger('click')
    await flushPromises()
    expect(store.respondToItem).toHaveBeenCalledWith('d1', '(something else)', 'Use the blue bucket')
    expect(w.find('[data-testid="approval-note"]').element.value).toBe('')
  })

  it('keeps the typed instruction when the send was not recorded', async () => {
    store.respondToItem.mockResolvedValue(false)
    const w = mountWith(item())
    await w.find('[data-testid="approval-note"]').setValue('Use the blue bucket')
    await w.find('[data-testid="approval-send"]').trigger('click')
    await flushPromises()
    expect(w.find('[data-testid="approval-note"]').element.value).toBe('Use the blue bucket')
  })

  it('hides the chip on a gate approval', () => {
    const w = mountWith(item({ request_id: 'gate-x' }))
    expect(w.find('[data-testid="something-else-chip"]').exists()).toBe(false)
  })

  it('a resolved reserved answer reads "Something else", never the literal', () => {
    const w = mountWith(item({ status: 'responded', response: '(something else)', response_text: 'Use the blue bucket' }))
    expect(w.find('[data-testid="detail-response"]').text()).toBe('Something else')
  })
})
