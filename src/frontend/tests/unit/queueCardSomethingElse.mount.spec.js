// @vitest-environment jsdom
/**
 * #3242 — the desktop queue card's "Something else" chip, mounted over the real
 * store (axios mocked), so the assertions are on the body that is POSTed.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

vi.mock('axios', () => {
  const inst = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), defaults: { headers: { common: {} } } }
  return { default: inst }
})
vi.mock('@/api', () => ({ default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() } }))

import axios from 'axios'
import QueueCard from '../../src/components/operator/QueueCard.vue'
import { useOperatorQueueStore } from '../../src/stores/operatorQueue'

const base = {
  id: 'i1', agent_name: 'a', request_id: 'approval-1', type: 'approval', status: 'pending',
  priority: 'high', title: 'Deploy?', question: 'Deploy to prod?', options: ['Approve', 'Deny'],
  context: {}, created_at: '2026-10-05T10:00:00Z',
}

async function mountCard(over = {}) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = useOperatorQueueStore()
  store.items = [{ ...base, ...over }]
  store.expandedItemId = 'i1'
  const wrapper = mount(QueueCard, {
    props: { item: store.items[0] },
    global: { plugins: [pinia], stubs: { AgentAvatar: true } },
  })
  await nextTick()
  return wrapper
}

const chip = (w) => w.find('[data-testid="something-else-chip"]')
const send = (w) => w.find('[data-testid="approval-send"]')
const note = (w) => w.find('[data-testid="approval-note"]')

describe('QueueCard — Something else (#3242)', () => {
  beforeEach(() => { vi.clearAllMocks(); axios.post.mockResolvedValue({ data: {} }) })

  it('offers the chip after the agent options, and Send is disabled with nothing typed', async () => {
    const w = await mountCard()
    expect(chip(w).exists()).toBe(true)
    expect(chip(w).text()).toBe('Something else')
    await chip(w).trigger('click')
    expect(send(w).attributes('disabled')).toBeDefined()
  })

  it('typing with no pick arms it: label flips and Send posts the literal + instruction', async () => {
    const w = await mountCard()
    await note(w).setValue('Ship to staging first')
    expect(chip(w).attributes('aria-pressed')).toBe('true')
    expect(send(w).text()).toBe('Send instruction')
    expect(send(w).attributes('disabled')).toBeUndefined()
    await send(w).trigger('click')
    await flushPromises()
    expect(axios.post).toHaveBeenCalledTimes(1)
    expect(axios.post.mock.calls[0][1]).toEqual({ response: '(something else)', response_text: 'Ship to staging first' })
  })

  it('Enter never sends an auto-armed state', async () => {
    const w = await mountCard()
    await note(w).setValue('Ship to staging first')
    await note(w).trigger('keydown', { key: 'Enter' })
    await flushPromises()
    expect(axios.post).not.toHaveBeenCalled()
  })

  it('Enter sends after an explicit pick of the chip', async () => {
    const w = await mountCard()
    await chip(w).trigger('click')
    await note(w).setValue('Ship to staging first')
    await note(w).trigger('keydown', { key: 'Enter' })
    await flushPromises()
    expect(axios.post.mock.calls[0][1].response).toBe('(something else)')
  })

  it('a picked option keeps the note a note', async () => {
    const w = await mountCard()
    await w.findAll('button').find(b => b.text() === 'Approve').trigger('click')
    await note(w).setValue('looks good')
    expect(send(w).text()).toBe('Send')
    await send(w).trigger('click')
    await flushPromises()
    expect(axios.post.mock.calls[0][1]).toEqual({ response: 'Approve', response_text: 'looks good' })
  })

  it('a gate approval has no chip and typing alone never arms anything', async () => {
    const w = await mountCard({ request_id: 'gate-abc' })
    expect(chip(w).exists()).toBe(false)
    await note(w).setValue('anything')
    expect(send(w).attributes('disabled')).toBeDefined()
  })

  it('an agent-offered twin of the literal is not rendered as a second chip', async () => {
    const w = await mountCard({ options: ['Approve', '(something else)'] })
    const labels = w.findAll('button').map(b => b.text())
    expect(labels.filter(t => /something else/i.test(t))).toEqual(['Something else'])
  })
})
