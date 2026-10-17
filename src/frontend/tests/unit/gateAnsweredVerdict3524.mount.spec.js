// @vitest-environment jsdom
/**
 * trinity#3524 — an answered approval says which answer it was.
 *
 * The projection carries the viewer's OWN answer (`answer`, `answer_note`,
 * only when `ended_by` is `you`). The card reads "You approved" / "You
 * rejected" plus the note; the Inbox row reads "Approved · you" /
 * "Rejected · you"; anyone else's ending keeps its coarse line. The relative
 * time agrees with its count ("1 minute ago").
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
import PortalInboxList from '@/components/portal/PortalInboxList.vue'
import { useClientPortalStore } from '@/stores/clientPortal'
import { workspaceOwnAnswer } from '@/utils/operatorQueue'
import { formatRelativeTime } from '@/utils/timestamps'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

const NOW = Date.parse('2026-10-12T12:00:00Z')
const ago = (ms) => new Date(NOW - ms).toISOString()
const M = 60_000

const ask = (id, over = {}) => ({
  id, agent_name: 'scout', kind: 'approval', priority: 'high',
  title: 'Approve the skill /deploy on scout', question: 'Go?',
  options: ['Approve', 'Reject'], status: 'answered', created_at: ago(60 * M),
  ended_at: ago(M), ended_by: 'you', decided_by_options: true, ...over,
})
const APPROVED = ask('ok', { answer: 'Approve', answer_note: null, ended_at: ago(7 * 60 * M) })
const REJECTED = ask('no', { answer: 'Reject', answer_note: 'wrong customer' })

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

describe('workspaceOwnAnswer', () => {
  it('reads an approval as a verdict and quotes any other answer', () => {
    expect(workspaceOwnAnswer(APPROVED)).toEqual({ past: 'approved', text: 'You approved' })
    expect(workspaceOwnAnswer(REJECTED)).toEqual({ past: 'rejected', text: 'You rejected' })
    expect(workspaceOwnAnswer(ask('q', { kind: 'question', answer: 'Tuesday' })))
      .toEqual({ past: null, text: 'You answered “Tuesday”' })
  })

  it('is null for an ending that was not the viewer\'s, or with no answer', () => {
    expect(workspaceOwnAnswer(ask('x', { ended_by: 'operator', answer: 'Approve' }))).toBeNull()
    expect(workspaceOwnAnswer(ask('y', { answer: null }))).toBeNull()
  })
})

describe('the answered card', () => {
  it('states the viewer\'s own verdict and note', async () => {
    store.asks = [REJECTED]
    wrapper = mount(PortalAsks, { props: { askIds: ['no'], testidPrefix: 'inbox-ask' } })
    await flushPromises()
    const line = wrapper.find('[data-testid="inbox-ask-ending"]').text()
    expect(line).toMatch(/^You rejected · 1 minute ago/)
    expect(wrapper.find('[data-testid="inbox-ask-answer-note-no"]').text()).toBe('Your note: “wrong customer”')
  })

  it('an approval without a note shows no note line', async () => {
    store.asks = [APPROVED]
    wrapper = mount(PortalAsks, { props: { askIds: ['ok'], testidPrefix: 'inbox-ask' } })
    await flushPromises()
    expect(wrapper.find('[data-testid="inbox-ask-ending"]').text()).toBe('You approved · 7 hours ago')
    expect(wrapper.find('[data-testid="inbox-ask-answer-note-ok"]').exists()).toBe(false)
  })

  it('an ask the viewer did not answer keeps the coarse line', async () => {
    store.asks = [ask('op', { ended_by: 'operator', answer: null })]
    wrapper = mount(PortalAsks, { props: { askIds: ['op'], testidPrefix: 'inbox-ask' } })
    await flushPromises()
    expect(wrapper.find('[data-testid="inbox-ask-ending"]').text()).toMatch(/^Answered by the operator/)
  })
})

describe('the Inbox row', () => {
  const item = (a) => ({ key: `ask:${a.id}`, type: 'ask', id: a.id, agent_name: a.agent_name,
    title: a.title, status: a.status, at: a.ended_at || a.created_at, ask: a })

  it('an answered approval shows its verdict', async () => {
    wrapper = mount(PortalInboxList, { props: { items: [item(APPROVED), item(REJECTED)] } })
    await flushPromises()
    expect(wrapper.find('[data-testid="inbox-row-ended-ask:ok"]').text()).toBe('Approved · you')
    expect(wrapper.find('[data-testid="inbox-row-ended-ask:no"]').text()).toBe('Rejected · you')
  })

  it('a question the viewer answered keeps "Answered · you"', async () => {
    wrapper = mount(PortalInboxList, { props: { items: [item(ask('q', { kind: 'question', answer: 'Tuesday' }))] } })
    await flushPromises()
    expect(wrapper.find('[data-testid="inbox-row-ended-ask:q"]').text()).toBe('Answered · you')
  })
})

describe('formatRelativeTime', () => {
  it('agrees with its count', () => {
    expect(formatRelativeTime(ago(M))).toBe('1 minute ago')
    expect(formatRelativeTime(ago(5 * M))).toBe('5 minutes ago')
    expect(formatRelativeTime(ago(60 * M))).toBe('1 hour ago')
    expect(formatRelativeTime(ago(2 * 24 * 60 * M))).toBe('2 days ago')
  })
})
