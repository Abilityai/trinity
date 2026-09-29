// @vitest-environment jsdom
/**
 * trinity-enterprise#610 PR A2, §3g L7 (E1) — an ask's context in the Inbox
 * pane, BELOW the card (reconcile row 14): the answer controls never move and
 * render whether or not the context read succeeds.
 *
 *   meta      "Asked 8m ago during a scheduled run · 09:00 · expires in 5h ·
 *             High priority" — the run folded into one line (askContextMeta)
 *   sections  Where it came from (the verified thread's messages + "Open the
 *             conversation", anchor=m:) → Delivered in that chat (anchor=d:,
 *             verified threads only) → Your recent answers
 *   fallback  an unverified origin reads "Filed in your Main chat", no excerpt
 *   states    a skeleton while there is no verdict, then LoadFailed dense with a
 *             retry — never an empty context on a failed read
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
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

import { useClientPortalStore, portalHttp } from '@/stores/clientPortal'
import PortalAskContext from '@/components/portal/PortalAskContext.vue'
import PortalInboxPane from '@/components/portal/PortalInboxPane.vue'
import { askContextMeta } from '@/components/portal/portalAskUrgency'

const NOW = Date.parse('2026-09-29T12:00:00Z')
const ago = (ms) => new Date(NOW - ms).toISOString()
const M = 60_000
const H = 60 * M

const ask = (over = {}) => ({
  id: 'a1', agent_name: 'billing', kind: 'approval', priority: 'high',
  title: 'Pay the vendor?', question: 'Release it?', options: ['yes', 'no'],
  created_at: ago(8 * M), expires_at: new Date(NOW + 5 * H + 10 * M).toISOString(), status: 'pending',
  chat_id: 's-main', sync: 'confirmed', aging: false, ended_at: null, ended_by: null, ...over,
})
const RUN = { kind: 'schedule', label: 'Asked during a scheduled run', started_at: '2026-09-29T09:00:00Z' }
const VERIFIED = {
  chat_id: 's-run', title: 'September close', is_main: false, verified: true,
  messages: [
    { id: 'm2', role: 'assistant', at: ago(20 * M), excerpt: 'Invoice found' },
    { id: 'm3', role: 'user', at: ago(15 * M), excerpt: 'check the PO' },
    { id: 'm4', role: 'assistant', at: ago(10 * M), excerpt: 'It matches.' },
  ],
}
const MAIN = { chat_id: 's-main', title: null, is_main: true, verified: false, messages: [] }
const ANSWERS = [{ id: 'old1', title: 'Pay August?', answer: 'yes', ended_at: ago(2 * 24 * H) }]

let store, wrapper
beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(NOW)
  setActivePinia(createPinia())
  store = useClientPortalStore()
  store.asksAvailable = true
  store.fetchSessionDeliverablesStrict = vi.fn(async () => [{ id: 'r1', title: 'September invoice run' }])
})
afterEach(() => { wrapper?.unmount(); wrapper = null; vi.useRealTimers() })

const el = (w, id) => w.find(`[data-testid="${id}"]`)
async function mountCtx(ctx, a = ask()) {
  store.fetchAskContext = typeof ctx === 'function' ? ctx : vi.fn(async () => ctx)
  wrapper = mount(PortalAskContext, { props: { ask: a, agentLabel: 'Billing' }, attachTo: document.body })
  await flushPromises()
  return wrapper
}

describe('askContextMeta — one line', () => {
  it('folds the run, the expiry and the priority into the asked-at line', () => {
    const parts = askContextMeta(ask(), RUN, NOW, (iso) => (iso ? '09:00' : ''))
    expect(parts).toEqual(['Asked 8m ago during a scheduled run · 09:00', 'expires in 5h', 'High priority'])
  })
  it('with no run, no near expiry and a medium priority it is just when', () => {
    expect(askContextMeta(ask({ expires_at: null, priority: 'medium' }), null, NOW)).toEqual(['Asked 8m ago'])
  })
})

describe('the store read', () => {
  it('fetchAskContext rethrows — the pane must tell "no context" from "failed"', async () => {
    portalHttp.get = vi.fn(async () => { throw Object.assign(new Error('boom'), { response: { status: 503 } }) })
    await expect(store.fetchAskContext('a1')).rejects.toThrow('boom')
    portalHttp.get = vi.fn(async () => ({ data: { origin: null, run: null, recent_answers: [] } }))
    expect(await store.fetchAskContext('a1')).toEqual({ origin: null, run: null, recent_answers: [] })
    expect(portalHttp.get).toHaveBeenCalledWith('/api/enterprise/client-portal/asks/a1/context', expect.anything())
  })
})

describe('PortalAskContext (mounted)', () => {
  it('a verified origin: its messages, then what was delivered there, then your answers', async () => {
    const w = await mountCtx({ origin: VERIFIED, run: RUN, recent_answers: ANSWERS })
    expect(el(w, 'inbox-ask-context-meta').text()).toContain('during a scheduled run')
    expect(w.findAll('[data-testid="inbox-ask-context-message"]').map((m) => m.text()))
      .toEqual([expect.stringContaining('Invoice found'), expect.stringContaining('check the PO'), expect.stringContaining('It matches.')])
    expect(store.fetchSessionDeliverablesStrict).toHaveBeenCalledWith('billing', 's-run')
    const origin = el(w, 'inbox-ask-context-origin').element
    const delivered = el(w, 'inbox-ask-context-delivered').element
    const answers = el(w, 'inbox-ask-context-answers').element
    expect(origin.compareDocumentPosition(delivered) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(delivered.compareDocumentPosition(answers) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    await el(w, 'inbox-ask-context-open').trigger('click')
    expect(w.emitted('open-chat')[0]).toEqual(['/workspace/c/s-run?anchor=m:m4'])
    await el(w, 'inbox-ask-context-delivered-r1').trigger('click')
    expect(w.emitted('open-chat')[1]).toEqual(['/workspace/c/s-run?anchor=d:r1'])
  })

  it('an unverified origin says where it was filed, with no excerpt and no deliverables read', async () => {
    const w = await mountCtx({ origin: MAIN, run: null, recent_answers: [] })
    expect(el(w, 'inbox-ask-context-origin').text()).toContain('Filed in your Main chat')
    expect(w.findAll('[data-testid="inbox-ask-context-message"]')).toHaveLength(0)
    expect(store.fetchSessionDeliverablesStrict).not.toHaveBeenCalled()
    expect(el(w, 'inbox-ask-context-answers').exists()).toBe(false)
  })

  it('no verdict yet is a skeleton; a failed read is LoadFailed with a retry — never an empty context', async () => {
    let fail = true
    let release
    const gate = new Promise((r) => { release = r })
    const fetch = vi.fn(async () => { await gate; if (fail) throw new Error('503'); return { origin: MAIN, run: null, recent_answers: [] } })
    store.fetchAskContext = fetch
    wrapper = mount(PortalAskContext, { props: { ask: ask() }, attachTo: document.body })
    await flushPromises()
    expect(el(wrapper, 'inbox-ask-context-loading').exists()).toBe(true)
    release()
    await flushPromises()
    expect(el(wrapper, 'inbox-ask-context-failed').exists()).toBe(true)
    expect(el(wrapper, 'inbox-ask-context-origin').exists()).toBe(false)
    fail = false
    await el(wrapper, 'inbox-ask-context-failed').find('button').trigger('click')
    await flushPromises()
    expect(fetch).toHaveBeenCalledTimes(2)
    expect(el(wrapper, 'inbox-ask-context-origin').exists()).toBe(true)
  })
})

describe('in the Inbox pane', () => {
  const item = (a) => ({ key: `ask:${a.id}`, type: 'ask', id: a.id, agent_name: a.agent_name, title: a.title, status: a.status, ask: a })
  it('the context sits BELOW the card; the controls render even when the context fails; no second thread link', async () => {
    const a = ask()
    store.asks = [a]
    store.fetchAskContext = vi.fn(async () => { throw new Error('503') })
    wrapper = mount(PortalInboxPane, {
      props: { item: item(a), agentLabel: 'Billing' },
      global: { stubs: { 'router-link': true } },
      attachTo: document.body,
    })
    await flushPromises()
    const card = el(wrapper, 'inbox-ask-a1').element
    const ctx = el(wrapper, 'inbox-ask-context').element
    expect(card.compareDocumentPosition(ctx) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(el(wrapper, 'inbox-ask-send-a1').exists()).toBe(true)
    expect(el(wrapper, 'inbox-ask-context-failed').exists()).toBe(true)
    // "Open the conversation" now lives in the context below (bdb2418e7's note).
    expect(el(wrapper, 'inbox-ask-open-thread-a1').exists()).toBe(false)
  })
})
