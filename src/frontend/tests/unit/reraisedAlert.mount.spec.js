// @vitest-environment jsdom
/**
 * trinity-enterprise#1367 — a re-raised alert keeps its age and its history.
 *
 * When an agent's alert replaces its own pending alert the server carries
 * `first_raised_at` and `raise_count` on the new row. Every queue surface says
 * "raised N times · first raised …" on the pending row, the oldest-first orders
 * read the first raise, and the newest-first Inbox order and the small time
 * label keep the latest raise (`created_at`).
 *
 * Mounted (#2918): the Operating Room card's line, the Workspace Inbox row's
 * badge (in the place of "Replaces …") and the ask card's line.
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
vi.mock('vue-router', () => ({
  useRoute: () => ({ query: {} }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}))

import { queueSeenLine, queueRaisedLine, queueWaitingSince } from '@/utils/operatorQueue'
import { askUrgencyCompare, askRecencyCompare } from '@/components/portal/portalInbox'
import { useOperatorQueueStore } from '@/stores/operatorQueue'
import { useClientPortalStore } from '@/stores/clientPortal'
import QueueCard from '@/components/operator/QueueCard.vue'
import PortalAsks from '@/components/portal/PortalAsks.vue'
import PortalInboxList from '@/components/portal/PortalInboxList.vue'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

const NOW = Date.parse('2026-10-08T12:00:00Z')
const FIRST = '2026-10-05T12:00:00Z'   // three days before NOW
const LATEST = '2026-10-08T11:55:00Z'  // five minutes before NOW

// An operator-queue row (the Operating Room and `/m`).
const row = (id, over = {}) => ({
  id, agent_name: 'scout', request_id: id, type: 'alert', status: 'pending', priority: 'medium',
  title: `Export failing ${id}`, question: 'The nightly export failed.', raised_by: 'agent',
  created_at: LATEST, context: {}, first_raised_at: null, raise_count: null, ...over,
})
// A Workspace ask (the Inbox).
const ask = (id, over = {}) => ({
  id, agent_name: 'scout', kind: 'alert', priority: 'medium', title: `Export failing ${id}`,
  question: 'The nightly export failed.', options: null, status: 'pending', created_at: LATEST,
  ended_at: null, ended_by: null, replaces: null, replaced_by: null,
  first_raised_at: null, raise_count: null, ...over,
})
const RAISED = { first_raised_at: FIRST, raise_count: 12 }

let wrapper
beforeEach(() => {
  document.body.innerHTML = ''
  setActivePinia(createPinia())
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(NOW)
})
afterEach(() => { wrapper?.unmount(); wrapper = null; vi.useRealTimers() })

describe('the raised line', () => {
  it('says how many times and since when, on a pending row raised more than once', () => {
    expect(queueRaisedLine(row('a', RAISED), NOW)).toBe('raised 12 times · first raised 3d ago')
    expect(queueRaisedLine(row('a', { raise_count: 2 }), NOW)).toBe('raised 2 times')
  })

  it.each([
    ['raised once', {}],
    ['a count of one', { raise_count: 1, first_raised_at: FIRST }],
    ['a count that is not a number', { raise_count: 'many', first_raised_at: FIRST }],
    ['an ended row', { ...RAISED, status: 'cancelled' }],
  ])('says nothing for %s', (_name, over) => {
    expect(queueRaisedLine(row('a', over), NOW)).toBe('')
  })

  it('is the line an agent alert shows where a platform alert shows its seen line', () => {
    expect(queueSeenLine(row('a', RAISED), NOW)).toBe('raised 12 times · first raised 3d ago')
    const platform = row('p', {
      subject: 'sync_diverged:scout', raised_by: null,
      context: { seen_count: 4 }, last_seen_at: LATEST,
    })
    expect(queueSeenLine(platform, NOW)).toBe('seen 4 times · last seen 5m ago')
  })

  it('reads a Workspace ask the same way', () => {
    expect(queueRaisedLine(ask('a', RAISED), NOW)).toBe('raised 12 times · first raised 3d ago')
  })
})

describe('what a pending row has waited since', () => {
  it('is the first raise when there is one, else its own filing time', () => {
    expect(queueWaitingSince(row('a', RAISED))).toBe(FIRST)
    expect(queueWaitingSince(row('a'))).toBe(LATEST)
    expect(queueWaitingSince(null)).toBe('')
  })
})

describe('the Operating Room order (oldest first)', () => {
  it('keeps a re-raised alert ahead of one filed after its first raise', () => {
    const store = useOperatorQueueStore()
    store.items = [
      row('newer', { created_at: '2026-10-07T12:00:00Z' }),
      row('standing', RAISED),                       // filed 5 minutes ago, first raised 3 days ago
      row('urgent', { priority: 'high', created_at: LATEST }),
    ]
    expect(store.openItems.map((i) => i.id)).toEqual(['urgent', 'standing', 'newer'])
  })
})

describe('the Workspace Inbox orders', () => {
  const standing = ask('standing', RAISED)
  const newer = ask('newer', { created_at: '2026-10-07T12:00:00Z' })

  it('urgency (oldest first) reads the first raise', () => {
    expect([newer, standing].sort((a, b) => askUrgencyCompare(a, b, NOW)).map((a) => a.id))
      .toEqual(['standing', 'newer'])
  })

  it('newest first keeps the latest raise, so a standing condition is never buried', () => {
    expect([newer, standing].sort(askRecencyCompare).map((a) => a.id)).toEqual(['standing', 'newer'])
  })
})

describe('QueueCard', () => {
  it('shows the raised line under the title and keeps the latest raise as its time', async () => {
    wrapper = mount(QueueCard, { props: { item: row('a', RAISED) } })
    await flushPromises()
    expect(wrapper.find('[data-testid="queue-seen-line"]').text()).toBe('raised 12 times · first raised 3d ago')
    expect(wrapper.text()).toContain('5m ago')
  })

  it('an alert raised once has no such line', async () => {
    wrapper = mount(QueueCard, { props: { item: row('a') } })
    await flushPromises()
    expect(wrapper.find('[data-testid="queue-seen-line"]').exists()).toBe(false)
  })
})

describe('the Workspace Inbox row', () => {
  const item = (a) => ({ key: `ask:${a.id}`, type: 'ask', id: a.id, agent_name: a.agent_name,
    title: a.title, status: a.status, at: a.created_at, ask: a })

  it('a re-raised alert carries the raised badge in the place of "Replaces"', async () => {
    const again = ask('again', { ...RAISED, replaces: 'export-1' })
    wrapper = mount(PortalInboxList, { props: { items: [item(again)] } })
    await flushPromises()
    const badge = wrapper.find('[data-testid="inbox-row-raised-ask:again"]')
    expect(badge.text()).toBe('Raised 12 times · first raised 3d ago')
    expect(wrapper.find('[data-testid="inbox-row-replaces-ask:again"]').exists()).toBe(false)
    // one badge line, never an extra "Unanswered" beside it
    expect(wrapper.find('[data-testid="inbox-row-pending-ask:again"]').exists()).toBe(false)
  })

  it('the badge needs no visible "Replaces" link: a new addressee reads the history too', async () => {
    const taken = ask('taken', { ...RAISED, replaces: null })
    wrapper = mount(PortalInboxList, { props: { items: [item(taken)] } })
    await flushPromises()
    expect(wrapper.find('[data-testid="inbox-row-raised-ask:taken"]').text()).toBe('Raised 12 times · first raised 3d ago')
    expect(wrapper.find('[data-testid="inbox-row-pending-ask:taken"]').exists()).toBe(false)
  })

  it('a replacement that is not a repeat keeps its "Replaces" badge', async () => {
    const swapped = ask('swapped', { kind: 'approval', replaces: 'payout-1' })
    wrapper = mount(PortalInboxList, { props: { items: [item(swapped)] } })
    await flushPromises()
    expect(wrapper.find('[data-testid="inbox-row-replaces-ask:swapped"]').text()).toBe('Replaces payout-1')
    expect(wrapper.find('[data-testid="inbox-row-raised-ask:swapped"]').exists()).toBe(false)
  })
})

describe('the Workspace ask card', () => {
  it('keeps both lines: what it replaces and how long the condition has stood', async () => {
    const store = useClientPortalStore()
    store.asksAvailable = true
    store.asksLoaded = true
    store.asks = [ask('again', { ...RAISED, replaces: 'export-1' }), ask('plain')]
    wrapper = mount(PortalAsks, { props: { agentName: 'scout' } })
    await flushPromises()
    expect(wrapper.findAll('[data-testid="portal-ask-raised"]').map((l) => l.text()))
      .toEqual(['Raised 12 times · first raised 3d ago'])
    expect(wrapper.findAll('[data-testid="portal-ask-replaces"]').map((l) => l.text()))
      .toEqual(['Replaces export-1'])
  })
})
