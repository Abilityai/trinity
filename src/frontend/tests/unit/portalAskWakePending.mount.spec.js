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

// #4502 — the confirmation above the card said "Sent — <agent> is picking this
// up." for an agent that was not running, beside the card's own (true) "Agent
// not yet woken". `resume_requested` only says a wake was REQUESTED, and the
// answer response cannot say it was deferred, so the card re-reads the roster
// and the sentence needs a running agent.
describe('the "Sent" line and an agent that is not running (#4502)', () => {
  const pendingAsk = (id) => ask(id, { status: 'pending', ended_at: null, ended_by: null, answer: null })
  const confirmation = (w) => w.find('[data-testid="inbox-ask-confirmation"]')

  async function answer({ rosterAfter, resume = true, rosterFails = false, before = [] } = {}) {
    const item = pendingAsk('q')
    store.asks = [item]
    store.agents = before
    store.answerAsk = vi.fn(async (id) => {
      const answered = { ...item, status: 'answered', ended_by: 'you', ended_at: ago(0), answer: 'Build it', resume_requested: resume }
      store.asks = store.asks.map((a) => (a.id === id ? answered : a))
      return answered
    })
    store.refreshRoster = vi.fn(async () => {
      if (rosterFails) throw new Error('roster read failed')
      store.agents = rosterAfter
    })
    wrapper = mount(PortalAsks, { props: { askIds: [item.id], testidPrefix: 'inbox-ask' } })
    await flushPromises()
    await wrapper.findAll('[data-testid="inbox-ask-option-q"]')[0].trigger('click')
    await flushPromises()
    expect(store.answerAsk).toHaveBeenCalledTimes(1)
    return wrapper
  }

  it('a stopped agent gets "Sent." only, read from a roster fetched AFTER the answer', async () => {
    // The roster in hand still says ready: it was read when the page loaded.
    const w = await answer({ before: [{ name: 'scout', availability: 'ready' }], rosterAfter: [{ name: 'scout', availability: 'stopped' }] })
    expect(store.refreshRoster).toHaveBeenCalledTimes(1)
    expect(confirmation(w).text()).toBe('Sent.')
    expect(w.text()).not.toContain('is picking this up')
  })

  it('an agent whose container is gone gets "Sent." only', async () => {
    const w = await answer({ rosterAfter: [{ name: 'scout', availability: 'unavailable' }] })
    expect(confirmation(w).text()).toBe('Sent.')
  })

  it('a running agent keeps the sentence', async () => {
    const w = await answer({ rosterAfter: [{ name: 'scout', availability: 'ready' }] })
    expect(confirmation(w).text()).toBe('Sent — scout is picking this up.')
  })

  it('a roster read that fails falls back on the last roster, never on a guess', async () => {
    const stopped = await answer({ before: [{ name: 'scout', availability: 'stopped' }], rosterFails: true })
    expect(confirmation(stopped).text()).toBe('Sent.')
    stopped.unmount(); wrapper = null
    const ready = await answer({ before: [{ name: 'scout', availability: 'ready' }], rosterFails: true })
    expect(confirmation(ready).text()).toBe('Sent — scout is picking this up.')
  })

  it('an answer that requested no wake says "Sent." and reads no roster', async () => {
    const w = await answer({ resume: false, rosterAfter: [{ name: 'scout', availability: 'ready' }] })
    expect(store.refreshRoster).not.toHaveBeenCalled()
    expect(confirmation(w).text()).toBe('Sent.')
  })

  it('a surface that takes the answer over (the Inbox pane) shows no "Sent" line and reads no roster', async () => {
    const item = pendingAsk('q')
    store.asks = [item]
    store.answerAsk = vi.fn(async (id) => {
      const answered = { ...item, status: 'answered', ended_by: 'you', ended_at: ago(0), resume_requested: true }
      store.asks = store.asks.map((a) => (a.id === id ? answered : a))
      return answered
    })
    store.refreshRoster = vi.fn(async () => {})
    const notifyAnswered = vi.fn()
    wrapper = mount(PortalAsks, { props: { askIds: [item.id], testidPrefix: 'inbox-ask', notifyAnswered } })
    await flushPromises()
    await wrapper.findAll('[data-testid="inbox-ask-option-q"]')[0].trigger('click')
    await flushPromises()
    expect(notifyAnswered).toHaveBeenCalledTimes(1)
    expect(notifyAnswered).toHaveBeenCalledWith('q', expect.objectContaining({ status: 'answered' }))
    expect(confirmation(wrapper).exists()).toBe(false)
    expect(store.refreshRoster).not.toHaveBeenCalled()
  })

  it('a roster that does not answer does not hold the line back: after 1.5 s the last roster decides', async () => {
    vi.useRealTimers()
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    const item = pendingAsk('q')
    store.asks = [item]
    store.agents = [{ name: 'scout', availability: 'stopped' }]
    store.answerAsk = vi.fn(async (id) => {
      const answered = { ...item, status: 'answered', ended_by: 'you', ended_at: ago(0), resume_requested: true }
      store.asks = store.asks.map((a) => (a.id === id ? answered : a))
      return answered
    })
    store.refreshRoster = vi.fn(() => new Promise(() => {})) // never settles
    wrapper = mount(PortalAsks, { props: { askIds: [item.id], testidPrefix: 'inbox-ask' } })
    await flushPromises()
    await wrapper.findAll('[data-testid="inbox-ask-option-q"]')[0].trigger('click')
    await flushPromises()
    expect(confirmation(wrapper).exists()).toBe(false) // still waiting on the read
    vi.advanceTimersByTime(1500)
    await flushPromises()
    expect(confirmation(wrapper).text()).toBe('Sent.')
  })

  it('an answer confirmed after the card unmounted reads no roster and starts nothing', async () => {
    const item = pendingAsk('q')
    store.asks = [item]
    let release
    store.answerAsk = vi.fn(() => new Promise((resolve) => {
      release = () => resolve({ ...item, status: 'answered', ended_by: 'you', ended_at: ago(0), resume_requested: true })
    }))
    store.refreshRoster = vi.fn(async () => {})
    wrapper = mount(PortalAsks, { props: { askIds: [item.id], testidPrefix: 'inbox-ask' } })
    await flushPromises()
    await wrapper.findAll('[data-testid="inbox-ask-option-q"]')[0].trigger('click')
    await flushPromises()
    wrapper.unmount()
    wrapper = null
    release()
    await flushPromises()
    expect(store.refreshRoster).not.toHaveBeenCalled()
  })

  it('a refused answer tells nobody: the hook is on the success path only', async () => {
    const item = pendingAsk('q')
    store.asks = [item]
    store.answerAsk = vi.fn(async () => {
      throw Object.assign(new Error('refused'), { response: { status: 409, data: { detail: { code: 'already_resolved', message: 'This ask was just answered elsewhere.' } } } })
    })
    const notifyAnswered = vi.fn()
    wrapper = mount(PortalAsks, { props: { askIds: [item.id], testidPrefix: 'inbox-ask', notifyAnswered } })
    await flushPromises()
    await wrapper.findAll('[data-testid="inbox-ask-option-q"]')[0].trigger('click')
    await flushPromises()
    expect(notifyAnswered).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('This ask was just answered elsewhere.')
  })
})
