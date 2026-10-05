// @vitest-environment jsdom
/**
 * #3130 review I2 — the detail panel must treat the budgeted flood alarm
 * (`queue_flood`) exactly like an `alert`: the same type badge and the same
 * shared label as the card (`utils/operatorQueue.js::queueTypeLabel`), never
 * the raw machine string with no badge colour.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { reactive } from 'vue'

const store = reactive({ selectedItem: null, selectedItemId: null })

vi.mock('@/stores/operatorQueue', () => ({ useOperatorQueueStore: () => store }))
vi.mock('@/stores/agents', () => ({ useAgentsStore: () => ({ agents: [], agentRefForSlug: (name) => ({ name }) }) }))

import QueueItemDetail from '@/components/operator/QueueItemDetail.vue'

const item = (type) => ({
  id: `x-${type}`,
  agent_name: 'a',
  type,
  status: 'pending',
  priority: 'high',
  title: 't',
  question: 'q',
  created_at: '2026-10-05T00:00:00Z',
})

function typeBadge(type) {
  store.selectedItem = item(type)
  store.selectedItemId = `x-${type}`
  const w = mount(QueueItemDetail)
  // The type badge is the first pill in the header row.
  return w.find('span.rounded.text-xs')
}

describe('QueueItemDetail type badge (#3130 I2)', () => {
  beforeEach(() => {
    store.selectedItem = null
  })

  it('renders queue_flood with the alert badge and label', () => {
    const alert = typeBadge('alert')
    const flood = typeBadge('queue_flood')
    expect(flood.classes().sort()).toEqual(alert.classes().sort())
    expect(flood.classes()).toContain('bg-state-autonomous-100')
    expect(flood.text()).toBe('Heads up')
    expect(alert.text()).toBe('Heads up')
  })

  it('never shows the raw queue_flood string', () => {
    expect(typeBadge('queue_flood').text()).not.toContain('queue_flood')
  })
})
