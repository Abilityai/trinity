// @vitest-environment jsdom
/**
 * trinity-enterprise#610 (D11) — the conversation's one-shot `?anchor=`.
 *
 * The rule lives in `composables/useConversationAnchor.js`; PortalConversation
 * supplies its scroll container and stick-to-bottom controls and calls the two
 * hooks. Here the composable is MOUNTED inside a harness that renders a real
 * transcript DOM, with a real (memory-history) router and the real
 * `useStickToBottom`, so what is asserted is what the reader gets: the scroll
 * target, the follow state, the notice, and the URL. `PortalDeliverables` is
 * mounted too, for the `loaded` event and `data-report-id` the `d:` anchor
 * depends on. What jsdom cannot show — the actual scroll position after layout
 * — is left to the live browser check.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { defineComponent, h, ref } from 'vue'
import { createRouter, createMemoryHistory, useRoute, useRouter } from 'vue-router'
import { useStickToBottom } from '../../src/composables/useStickToBottom'
import {
  useConversationAnchor, parseAnchor, ANCHOR_MISSING_NOTICE, ANCHOR_HIGHLIGHT_CLASSES,
} from '../../src/composables/useConversationAnchor'
import PortalDeliverables from '../../src/components/portal/PortalDeliverables.vue'

const { __portal } = vi.hoisted(() => ({ __portal: { stub: null } }))
vi.mock('@/stores/clientPortal', async () => {
  const { reactive } = await import('vue')
  __portal.stub = reactive({
    fetchSessionDeliverables: async () => [],
    fetchAgentReport: async () => ({ payload: {} }),
  })
  return { useClientPortalStore: () => __portal.stub }
})

const Stub = defineComponent({ render: () => h('div') })

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/workspace/c/:id', component: Stub }, { path: '/:p(.*)*', component: Stub }],
  })
}

/** A transcript: message rows, a voice block with a folded turn, a report card. */
function harness({ messageIds = [], turnIds = [], reportIds = [] } = {}) {
  let api
  const Harness = defineComponent({
    setup() {
      const scrollEl = ref(null)
      const stick = useStickToBottom(scrollEl)
      const anchor = useConversationAnchor({
        scrollEl, detach: stick.detach, pinToBottom: stick.pinToBottom,
        route: useRoute(), router: useRouter(),
      })
      api = { stick, anchor, scrollEl }
      return () => h('div', { ref: scrollEl, 'data-testid': 'scroll' }, [
        h('div', [
          ...messageIds.map((id) => h('div', { 'data-message-id': id }, `m ${id}`)),
          h('details', { 'data-testid': 'voice-block' },
            turnIds.map((id) => h('div', { 'data-message-id': id }, `t ${id}`))),
          ...reportIds.map((id) => h('div', { 'data-report-id': id }, `r ${id}`)),
        ]),
        anchor.notice.value ? h('p', { 'data-testid': 'notice' }, anchor.notice.value) : null,
      ])
    },
  })
  return { Harness, get: () => api }
}

let scrolled
beforeEach(() => {
  scrolled = []
  Element.prototype.scrollIntoView = vi.fn(function scrollIntoView() { scrolled.push(this) })
  globalThis.ResizeObserver = class { observe() {} disconnect() {} }
})
afterEach(() => { delete Element.prototype.scrollIntoView; vi.useRealTimers() })

async function mountAt(url, shape) {
  const router = makeRouter()
  await router.push(url)
  await router.isReady()
  const { Harness, get } = harness(shape)
  const wrapper = mount(Harness, { global: { plugins: [router] }, attachTo: document.body })
  await flushPromises()
  return { wrapper, router, api: get() }
}

describe('parseAnchor — the query contract', () => {
  it('reads m:<id> as a message and d:<id> as a deliverable', () => {
    expect(parseAnchor('m:abc')).toEqual({ kind: 'message', id: 'abc' })
    expect(parseAnchor('d:r-1')).toEqual({ kind: 'deliverable', id: 'r-1' })
  })
  it('rejects anything else', () => {
    for (const bad of [undefined, null, '', 'x:1', 'm:', 'abc', 42]) expect(parseAnchor(bad)).toBeNull()
  })
})

describe('m: — a message anchor', () => {
  it('scrolls to the [data-message-id] target, detaches follow, outlines it and strips the key', async () => {
    const { wrapper, router, api } = await mountAt('/workspace/c/s1?anchor=m:m2&tab=x', { messageIds: ['m1', 'm2', 'm3'] })
    expect(api.stick.following.value).toBe(true)

    await api.anchor.afterHistory()
    await flushPromises()

    const target = wrapper.find('[data-message-id="m2"]').element
    expect(scrolled).toEqual([target])
    expect(api.stick.following.value).toBe(false)     // the observer will not re-pin
    for (const c of ANCHOR_HIGHLIGHT_CLASSES) expect(target.classList.contains(c)).toBe(true)
    expect(router.currentRoute.value.query).toEqual({ tab: 'x' })   // anchor stripped, rest kept
    expect(router.currentRoute.value.path).toBe('/workspace/c/s1')
    expect(wrapper.find('[data-testid="notice"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('opens a collapsed voice-call block when the target is a spoken turn inside it', async () => {
    const { wrapper, api } = await mountAt('/workspace/c/s1?anchor=m:t1', { turnIds: ['t1'] })
    await api.anchor.afterHistory()
    expect(wrapper.find('[data-testid="voice-block"]').element.open).toBe(true)
    expect(scrolled).toHaveLength(1)
    wrapper.unmount()
  })

  it('a missing target falls back to the bottom with a notice, and still strips the key', async () => {
    const { wrapper, router, api } = await mountAt('/workspace/c/s1?anchor=m:gone', { messageIds: ['m1'] })
    const el = api.scrollEl.value
    Object.defineProperty(el, 'scrollHeight', { configurable: true, value: 900 })
    el.scrollTop = 10
    api.stick.detach()

    await api.anchor.afterHistory()
    await flushPromises()

    expect(scrolled).toEqual([])
    expect(el.scrollTop).toBe(900)                        // the bottom
    expect(api.stick.following.value).toBe(true)          // pinToBottom re-arms
    expect(wrapper.find('[data-testid="notice"]').text()).toBe(ANCHOR_MISSING_NOTICE)
    expect(router.currentRoute.value.query.anchor).toBeUndefined()
    wrapper.unmount()
  })

  it('is one-shot: a second history load does not jump again', async () => {
    const { wrapper, api } = await mountAt('/workspace/c/s1?anchor=m:m1', { messageIds: ['m1'] })
    await api.anchor.afterHistory()
    await flushPromises()
    await api.anchor.afterHistory()
    expect(scrolled).toHaveLength(1)
    wrapper.unmount()
  })

  it('does nothing without an anchor', async () => {
    const { wrapper, api } = await mountAt('/workspace/c/s1', { messageIds: ['m1'] })
    await api.anchor.afterHistory()
    await api.anchor.afterDeliverables()
    expect(scrolled).toEqual([])
    expect(api.stick.following.value).toBe(true)
    wrapper.unmount()
  })
})

describe('d: — a deliverable anchor waits for "Delivered here"', () => {
  it('ignores the history hook and resolves on the deliverables hook', async () => {
    const { wrapper, router, api } = await mountAt('/workspace/c/s1?anchor=d:r7', { messageIds: ['m1'], reportIds: ['r7'] })
    await api.anchor.afterHistory()
    expect(scrolled).toEqual([])
    expect(router.currentRoute.value.query.anchor).toBe('d:r7')   // still pending

    await api.anchor.afterDeliverables({ sessionId: 's1', ids: ['r7'] })
    await flushPromises()
    expect(scrolled).toEqual([wrapper.find('[data-report-id="r7"]').element])
    expect(router.currentRoute.value.query.anchor).toBeUndefined()
    wrapper.unmount()
  })
})

describe('PortalDeliverables — the hooks the d: anchor needs', () => {
  it('emits `loaded` with the chat and its ids, and stamps data-report-id on each card', async () => {
    __portal.stub.fetchSessionDeliverables = async () => ([
      { id: 'r1', title: 'One', report_type: 'summary', created_at: '2026-09-28T10:00:00Z' },
      { id: 'r2', title: 'Two', report_type: 'summary', created_at: '2026-09-28T11:00:00Z' },
    ])
    const w = mount(PortalDeliverables, {
      props: { agentName: 'acme', sessionId: 's1' },
      global: { stubs: { PortalRating: true, ReportRenderer: true } },
    })
    await flushPromises()
    expect(w.emitted('loaded')).toEqual([[{ sessionId: 's1', ids: ['r1', 'r2'] }]])
    expect(w.findAll('[data-report-id]').map((n) => n.attributes('data-report-id'))).toEqual(['r1', 'r2'])
    w.unmount()
  })

  it('emits `loaded` with no ids for a chat that has no session yet', async () => {
    const w = mount(PortalDeliverables, { props: { agentName: 'acme', sessionId: null } })
    await flushPromises()
    expect(w.emitted('loaded')).toEqual([[{ sessionId: null, ids: [] }]])
    w.unmount()
  })
})
