// @vitest-environment jsdom
/**
 * trinity-enterprise#610 (D8) — the Inbox pane renders ONE ask through the
 * same `PortalAsks`, next to the Work tab's instance of the same row.
 *
 * Two props make that safe, and both are proven by MOUNTING the component:
 *   `askIds`       narrows to those asks, and an ask that ENDED while selected
 *                  stays drawn in place (as ended) instead of vanishing;
 *   `testidPrefix` namespaces EVERY data-testid the component emits, the
 *                  static ones included, while the default keeps each id
 *                  byte-identical to what the existing specs and e2e address.
 * The intersection test is the memory rule "never share a sibling panel's
 * data-testid": both instances are mounted over the same ask, in every state
 * that emits an id (pending with a sync badge, ended, a fresh confirmation),
 * and their id sets must be disjoint.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: true, authHeader: {}, logout: vi.fn() }),
}))
vi.mock('axios', () => {
  const mk = () => ({
    get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  })
  return {
    default: Object.assign(
      { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), create: mk },
      { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
        defaults: { headers: { common: {} } } },
    ),
  }
})

import { useClientPortalStore } from '@/stores/clientPortal'
import PortalAsks from '@/components/portal/PortalAsks.vue'

const ENDED = '2026-09-25T10:00:00.000000Z'
const ask = (id, over = {}) => ({
  id, agent_name: 'scout', kind: 'question', priority: 'medium',
  title: `Ask ${id}`, question: `Ask ${id}`, options: null,
  created_at: '2026-09-20T10:00:00Z', expires_at: null, status: 'pending',
  chat_id: null, sync: 'confirmed', aging: false, ended_at: null, ended_by: null, ...over,
})

let store
beforeEach(() => {
  localStorage.clear()
  setActivePinia(createPinia())
  store = useClientPortalStore()
  store.asksAvailable = true
})

const ids = (w) => w.findAll('[data-testid]').map((n) => n.attributes('data-testid'))

/** Every state that emits an id: the list root, a pending ask with a sync
 *  badge + its input, an approval's option/note/send, an alert's ack, an ended
 *  ask's ending line, and a confirmation after an answer. */
function seed() {
  store.asks = [
    ask('q1', { sync: 'changed' }),
    ask('ap1', { kind: 'approval', options: ['Yes', 'No'] }),
    ask('al1', { kind: 'alert' }),
    ask('x1', { status: 'answered', ended_by: 'you', ended_at: ENDED }),
  ]
}

async function answerFirst(w) {
  await w.find('input[type="text"]').setValue('fine')
  await w.find('form').trigger('submit')
  await flushPromises()
}

describe('testidPrefix — the default keeps every id byte-identical', () => {
  it('emits exactly the ids the existing specs and e2e address', async () => {
    seed()
    store.answerAsk = vi.fn().mockResolvedValue({ ...store.asks[0], status: 'answered' })
    const w = mount(PortalAsks, { props: { agentName: 'scout' } })
    await answerFirst(w)
    expect(new Set(ids(w))).toEqual(new Set([
      'portal-asks', 'portal-ask-confirmation',
      'portal-ask-q1', 'queue-sync-badge', 'portal-ask-input-q1', 'portal-ask-send-q1',
      'portal-ask-ap1', 'portal-ask-option-ap1', 'portal-ask-something-else-ap1', 'portal-ask-note-ap1', 'portal-ask-send-ap1',
      'portal-ask-al1', 'portal-ask-ack-al1',
      'portal-ask-x1', 'portal-ask-ending',
      // #3115: each title renders through AskMarkdown with its own id.
      'portal-ask-title-q1', 'portal-ask-title-ap1', 'portal-ask-title-al1', 'portal-ask-title-x1',
      // trinity-enterprise#747/#748: Discuss and Dismiss on each waiting
      // question and approval (an alert has neither).
      'portal-ask-actions-q1', 'portal-ask-discuss-q1', 'portal-ask-dismiss-q1',
      'portal-ask-actions-ap1', 'portal-ask-discuss-ap1', 'portal-ask-dismiss-ap1',
    ]))
  })

  it('a custom prefix namespaces every id, the static ones included', async () => {
    seed()
    store.answerAsk = vi.fn().mockResolvedValue({ ...store.asks[0], status: 'answered' })
    const w = mount(PortalAsks, { props: { agentName: 'scout', testidPrefix: 'inbox-ask' } })
    await answerFirst(w)
    const got = ids(w)
    expect(got.length).toBeGreaterThan(8)
    for (const id of got) expect(id.startsWith('inbox-ask')).toBe(true)
    expect(got).toEqual(expect.arrayContaining([
      'inbox-asks', 'inbox-ask-confirmation', 'inbox-ask-sync-badge', 'inbox-ask-ending', 'inbox-ask-q1',
    ]))
  })
})

describe('askIds — one ask, and it stays in place when it ends', () => {
  it('renders only the listed asks', () => {
    seed()
    const w = mount(PortalAsks, { props: { askIds: ['ap1'], testidPrefix: 'inbox-ask' } })
    expect(w.findAll('[data-status]').map((n) => n.attributes('data-testid'))).toEqual(['inbox-ask-ap1'])
  })

  it('wins over pendingOnly: an ended ask in askIds is drawn ended, not dropped', () => {
    seed()
    const w = mount(PortalAsks, { props: { askIds: ['x1'], pendingOnly: true, testidPrefix: 'inbox-ask' } })
    const card = w.find('[data-testid="inbox-ask-x1"]')
    expect(card.exists()).toBe(true)
    expect(card.attributes('data-status')).toBe('answered')
    expect(w.find('[data-testid="inbox-ask-ending"]').exists()).toBe(true)
  })

  it('an ask answered while selected stays on screen as answered (store replaces, never removes)', async () => {
    seed()
    store.answerAsk = vi.fn(async (id) => {
      const answered = { ...store.asks.find((a) => a.id === id), status: 'answered', ended_by: 'you', ended_at: ENDED }
      store.asks = store.asks.map((a) => (a.id === id ? answered : a))
      return answered
    })
    const w = mount(PortalAsks, { props: { askIds: ['q1'], testidPrefix: 'inbox-ask' } })
    await answerFirst(w)
    expect(w.find('[data-testid="inbox-ask-q1"]').attributes('data-status')).toBe('answered')
    expect(w.find('[data-testid="inbox-ask-confirmation"]').exists()).toBe(true)
  })

  it('without askIds the default list is unchanged', () => {
    seed()
    const w = mount(PortalAsks, { props: { agentName: 'scout', pendingOnly: true } })
    expect(w.findAll('[data-status]').map((n) => n.attributes('data-status'))).toEqual(['pending', 'pending', 'pending'])
  })
})

describe('the Inbox pane next to the Work tab — no shared address', () => {
  it('the two instances over the same ask have disjoint data-testid sets, static ids included', async () => {
    seed()
    store.answerAsk = vi.fn().mockResolvedValue({ ...store.asks[0], status: 'answered' })
    // The Work tab's instance: default prefix, as PortalWork mounts it.
    const work = mount(PortalAsks, { props: { agentNames: ['scout'] } })
    // The Inbox pane's instance: the same asks, prefixed.
    const pane = mount(PortalAsks, { props: { askIds: ['q1', 'ap1', 'al1', 'x1'], testidPrefix: 'inbox-ask' } })
    await answerFirst(work)
    await answerFirst(pane)
    const a = new Set(ids(work))
    const b = new Set(ids(pane))
    expect(a.has('portal-asks') && a.has('queue-sync-badge') && a.has('portal-ask-ending')).toBe(true)
    expect(b.has('inbox-asks') && b.has('inbox-ask-sync-badge') && b.has('inbox-ask-ending')).toBe(true)
    expect([...a].filter((id) => b.has(id))).toEqual([])
  })
})
