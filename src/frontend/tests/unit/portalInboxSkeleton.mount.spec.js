// @vitest-environment jsdom
/**
 * #3060 — the Inbox's skeleton is drawn in the ready Inbox's footprint.
 *
 * The stage skeleton on the Inbox route was the CONVERSATION's (header,
 * thread, composer), so the Inbox landed on a different frame. Its own
 * skeleton must hand over to the Inbox's loading state without moving a row or
 * a column. jsdom cannot lay out, so this pins what decides the geometry — the
 * frame's class strings and the tab strip — by mounting BOTH and comparing
 * them; the pixel proof is the e2e arm in `workspace-inbox.spec.js`.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'

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
import PortalInbox from '@/components/portal/PortalInbox.vue'
import PortalInboxSkeleton from '@/components/portal/PortalInboxSkeleton.vue'

// A ResizeObserver that reports a chosen width, so both components run the
// same `inboxLayout` over the same number.
let reportWidth = 0
globalThis.ResizeObserver = class {
  constructor(cb) { this.cb = cb }
  observe(el) { if (reportWidth) this.cb([{ contentRect: { width: reportWidth } }], this) }
  unobserve() {}
  disconnect() {}
}
let phone = false
function stubMatchMedia() {
  window.matchMedia = (q) => ({
    matches: phone, media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
  })
}

let router
const mounted = []
beforeEach(async () => {
  phone = false
  reportWidth = 0
  stubMatchMedia()
  setActivePinia(createPinia())
  const store = useClientPortalStore()
  store.portalToken = 'tok'
  store.asksAvailable = true
  store.fetchAsks = vi.fn(async () => store.asks)
  router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/workspace/inbox', component: { template: '<div />' } }],
  })
  await router.push('/workspace/inbox')
  await router.isReady()
})
afterEach(() => { while (mounted.length) mounted.pop().unmount(); document.body.innerHTML = '' })

async function both(props = {}) {
  const inbox = mount(PortalInbox, {
    // No verdict on either source: the Inbox's own loading state.
    props: { threads: [], previews: {}, threadsLoaded: false, threadsFailed: false, labels: {}, ...props },
    global: { plugins: [router] },
    attachTo: document.body,
  })
  const skel = mount(PortalInboxSkeleton, {
    props: { part: 'shell', ...props },
    global: { plugins: [router] },
    attachTo: document.body,
  })
  mounted.push(inbox, skel)
  await flushPromises()
  return { inbox, skel }
}
const cls = (w, sel) => w.find(sel).attributes('class')
const tabText = (w, sel) => w.find(sel).findAll('[role="tab"]').map((t) => t.text().replace(/\s+/g, ' ').trim())

describe('#3060 the skeleton is the Inbox frame', () => {
  it('the header and the tab-strip wrapper carry the Inbox\'s own classes', async () => {
    const { inbox, skel } = await both()
    expect(cls(skel, '[data-testid="inbox-skeleton-header"]')).toBe(cls(inbox, '[data-testid="inbox-header"]'))
    expect(cls(skel, '[data-testid="inbox-skeleton-tabs"]')).toBe(cls(inbox, '[data-testid="inbox-tabs"]'))
  })

  it('the tab strip is the SAME strip — labels, order, the selected tab, and the reserved badge slots', async () => {
    const { inbox, skel } = await both()
    expect(tabText(skel, '[data-testid="inbox-skeleton-tabs"]')).toEqual(tabText(inbox, '[data-testid="inbox-tabs"]'))
    expect(tabText(skel, '[data-testid="inbox-skeleton-tabs"]')).toEqual(['Action', 'Unread', 'All'])
    const selected = (w, sel) => w.find(sel).find('[aria-selected="true"]').text().trim()
    expect(selected(skel, '[data-testid="inbox-skeleton-tabs"]')).toBe(selected(inbox, '[data-testid="inbox-tabs"]'))
    for (const w of [inbox, skel]) expect(w.findAll('[data-badge-slot]')).toHaveLength(2)
  })

  it.each([
    ['unmeasured', 0, false, 'w-80'],
    ['split, 320 list', 900, false, 'w-80'],
    ['split, 384 list', 1300, false, 'w-96'],
    ['stacked', 600, false, 'max-w-3xl'],
    ['phone', 0, true, 'max-w-3xl'],
  ])('the list column is the width the Inbox picks (%s)', async (_n, width, isPhone, expected) => {
    reportWidth = width
    phone = isPhone
    stubMatchMedia()
    const { inbox, skel } = await both()
    expect(skel.find('[data-testid="inbox-skeleton"]').attributes('data-layout'))
      .toBe(inbox.find('[data-testid="inbox"]').attributes('data-layout'))
    expect(cls(skel, '[data-testid="inbox-skeleton-list"]')).toBe(cls(inbox, '[data-testid="inbox-list-column"]'))
    // Not vacuous: the width really reached the layout rule.
    expect(skel.find('[data-testid="inbox-skeleton-list"]').classes()).toContain(expected)
  })

  it('the rows the stage skeleton draws are the rows the Inbox\'s loading arm draws', async () => {
    const { inbox, skel } = await both()
    const rows = (w, sel) => w.find(sel).findAll('[data-testid="inbox-skeleton-row"]').map((r) => r.attributes('class'))
    expect(rows(skel, '[data-testid="inbox-skeleton-list"]')).toEqual(rows(inbox, '[data-testid="inbox-list-loading"]'))
    expect(rows(inbox, '[data-testid="inbox-list-loading"]').length).toBeGreaterThan(0)
  })

  it('split, the pane block is on screen in both — never "Pick something" before there is anything to pick', async () => {
    reportWidth = 900
    const { inbox, skel } = await both()
    expect(skel.find('[data-testid="inbox-skeleton-pane"]').exists()).toBe(true)
    expect(inbox.find('[data-testid="inbox-skeleton-pane"]').exists()).toBe(true)
    expect(inbox.find('[data-testid="inbox-pane-none"]').exists()).toBe(false)
  })

  it('a tab picked on the skeleton is written to ?tab=, where the Inbox reads it', async () => {
    const { skel } = await both()
    await skel.find('[data-testid="inbox-skeleton-tabs"]').findAll('[role="tab"]')[1].trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.query.tab).toBe('unread')
  })

  it('announces loading once and is reduced-motion safe', async () => {
    const { skel } = await both()
    expect(skel.find('[data-testid="inbox-skeleton"]').attributes('aria-busy')).toBe('true')
    expect(skel.findAll('.sr-only').filter((s) => /Loading/.test(s.text()))).toHaveLength(1)
    for (const b of skel.findAll('.animate-pulse')) expect(b.classes()).toContain('motion-reduce:animate-none')
  })
})
