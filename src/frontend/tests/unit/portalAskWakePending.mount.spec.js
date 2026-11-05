// @vitest-environment jsdom
/**
 * abilityai/trinity-enterprise#1108 AC #4 — an answered ask whose agent was not
 * running says so: "Agent not yet woken — it picks this up when it next runs."
 * The line is a rendered predicate on `wake_state`, so it is MOUNTED, not
 * regexed (trinity#2918); a woken or unmarked ask shows nothing.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('axios', () => {
  const mk = () => ({
    get: vi.fn(() => Promise.resolve({ data: {} })), post: vi.fn(() => Promise.resolve({ data: {} })),
    put: vi.fn(), patch: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  })
  return {
    default: Object.assign(
      { get: vi.fn(() => Promise.resolve({ data: {} })), post: vi.fn(), put: vi.fn(), delete: vi.fn(), create: mk },
      { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
        defaults: { headers: { common: {} } } },
    ),
  }
})

import PortalAsks from '@/components/portal/PortalAsks.vue'
import { useClientPortalStore } from '@/stores/clientPortal'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

const NOW = Date.parse('2026-11-04T12:00:00Z')
const ago = (ms) => new Date(NOW - ms).toISOString()
const M = 60_000

const ask = (id, over = {}) => ({
  id, agent_name: 'scout', kind: 'approval', priority: 'high',
  title: 'Build it?', question: 'Go?', options: ['Build it', 'Not now'],
  status: 'answered', created_at: ago(60 * M), ended_at: ago(M), ended_by: 'you',
  answer: 'Build it', decided_by_options: true, ...over,
})

let wrapper
let store
beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(NOW)
  document.body.innerHTML = ''
  setActivePinia(createPinia())
  store = useClientPortalStore()
  store.asksAvailable = true
  store.asksLoaded = true
})
afterEach(() => { wrapper?.unmount(); wrapper = null; vi.useRealTimers() })

async function mountWith(item) {
  store.asks = [item]
  wrapper = mount(PortalAsks, { props: { askIds: [item.id], testidPrefix: 'inbox-ask' } })
  await flushPromises()
  return wrapper
}

describe('the answered card and the wake (ent#1108 AC #4)', () => {
  it('a pending wake says the agent is not yet woken', async () => {
    const w = await mountWith(ask('p', { wake_state: 'pending' }))
    expect(w.find('[data-testid="inbox-ask-ending"]').text()).toMatch(/^You answered/)
    expect(w.find('[data-testid="inbox-ask-wake-pending"]').text())
      .toBe('Agent not yet woken — it picks this up when it next runs.')
  })

  it('a woken ask, and one with no wake owed, show no such line', async () => {
    const w1 = await mountWith(ask('w', { wake_state: 'woken' }))
    expect(w1.find('[data-testid="inbox-ask-wake-pending"]').exists()).toBe(false)
    w1.unmount(); wrapper = null
    const w2 = await mountWith(ask('n', { wake_state: null }))
    expect(w2.find('[data-testid="inbox-ask-wake-pending"]').exists()).toBe(false)
  })
})
