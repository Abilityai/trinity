// @vitest-environment jsdom
/**
 * #3451 — every Operations list scrolls inside one region, and the page around
 * it holds still.
 *
 * MOUNTED (#2918): `Operations.vue` with the real Notifications and Executions
 * panels over the real stores; only HTTP, the router and the socket are
 * stubbed. jsdom has no layout, so this pins the STRUCTURE the browser test
 * (`e2e/operations-bounded-lists.spec.js`) measures:
 *   - one region per list tab, holding the rows, the states and Load more (last);
 *   - the stated totals outside it, in view;
 *   - the flex chain from the region up to the page root, every link
 *     `grow basis-0` — one block wrapper or one `flex-1` silently regrows the
 *     page — and `w-full` wherever `mx-auto` would stop the stretch;
 *   - the region is the same node, at the same scrollTop, after a poll; a new
 *     query (a filter change) starts at the top;
 *   - nothing that toggles on its own (the bulk actions, the running count, the
 *     stale banner during an outage) stacks above the region and moves it.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { ref, nextTick } from 'vue'

const route = { query: { tab: 'needs-response' } }
vi.mock('vue-router', () => ({
  useRoute: () => route,
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
}))
vi.mock('axios', () => {
  const inst = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), defaults: { headers: { common: {} } },
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } }
  return { default: { ...inst, create: () => inst } }
})
vi.mock('@/api', () => {
  const inst = { get: vi.fn(() => Promise.resolve({ data: [] })), post: vi.fn(), put: vi.fn(), delete: vi.fn() }
  return { default: inst }
})
vi.mock('@/utils/websocket', () => ({ useWebSocket: () => ({ isConnected: ref(false) }) }))

import axios from 'axios'
import Operations from '@/views/Operations.vue'
import { useOperatorQueueStore } from '@/stores/operatorQueue'
import { useNotificationsStore } from '@/stores/notifications'
import { useExecutionsStore } from '@/stores/executions'
import { useAuthStore } from '@/stores/auth'
import { makeQueue, rowsOf } from './helpers/fakeOperatorQueue'

const notification = (i) => ({ id: `n-${i}`, agent_name: 'agent-a', notification_type: 'info', priority: 'normal',
  status: 'pending', title: `Notification ${i}`, message: null, category: null, metadata: null, created_at: '2026-10-23T10:00:00Z' })
const execution = (i) => ({ id: `exec-${String(i).padStart(4, '0')}`, agent_name: 'agent-a', status: 'success',
  triggered_by: 'schedule', started_at: '2026-10-23T10:00:00Z', message: `Scheduled run ${i}` })

let wrapper
let api
function stubApi({ executions = 400, running = 0, notifications = 50, notificationsTotal = 50, queue } = {}) {
  api = { running, queueFail: false }
  const q = queue || makeQueue([...rowsOf(30, { agents: 3 }), ...rowsOf(20, { prefix: 'done', status: 'responded' })])
  axios.get.mockImplementation(async (url, config = {}) => {
    const p = config.params || {}
    if (url === '/api/operator-queue') {
      if (api.queueFail) throw new Error('offline')
      return q.get(p)
    }
    if (url.startsWith('/api/notifications/count')) return { data: { count: notificationsTotal } }
    if (url.startsWith('/api/notifications')) {
      return { data: { count: notificationsTotal, notifications: Array.from({ length: notifications }, (_, i) => notification(i + 1)) } }
    }
    if (url === '/api/executions/stats') {
      return { data: { total: executions, success_rate: 100, failed_count: 0, total_cost: 0, running_count: api.running } }
    }
    if (url === '/api/executions') {
      const n = Math.max(0, Math.min(p.limit, executions - (p.offset || 0)))
      return { data: Array.from({ length: n }, (_, i) => execution((p.offset || 0) + i + 1)) }
    }
    if (url === '/api/agents') return { data: [] }
    return { data: {} }
  })
}

const STUBS = {
  NavBar: true, OverflowTabs: true, QueueCard: { template: '<div data-testid="queue-card" />' },
  ResolvedCard: { template: '<div data-testid="resolved-card" />' }, MonitoringPanel: true,
  ReportsPanelFleet: true, ConfirmDialog: true, RouterLink: { template: '<a><slot /></a>' },
}

async function mountOn(tab, api = {}) {
  route.query.tab = tab
  stubApi(api)
  const pinia = createPinia()
  setActivePinia(pinia)
  useAuthStore().user = { role: 'admin' }
  wrapper = mount(Operations, { global: { plugins: [pinia], stubs: STUBS }, attachTo: document.body })
  await flushPromises()
  return wrapper
}

const region = (w) => w.get('[data-testid="ops-list-region"]').element
const has = (el, ...names) => names.every((n) => el.classList.contains(n))

/** Every element from the region up to the page root, region first. */
function chain(w) {
  const links = []
  for (let el = region(w); el && el !== w.element; el = el.parentElement) links.push(el)
  return links
}

describe('Operations — each list tab scrolls inside one region (#3451)', () => {
  beforeEach(() => { vi.clearAllMocks() })
  afterEach(() => { wrapper?.unmount(); wrapper = null })

  const TABS = [
    ['needs-response', '[data-testid="queue-card"]', 30, 'ops-subtitle'],
    ['resolved', '[data-testid="resolved-card"]', 20, 'ops-subtitle'],
    ['notifications', '[data-testid="notification-row"]', 50, 'notif-stat-shown'],
    ['executions', '[data-testid="execution-row"]', 50, 'executions-shown'],
  ]

  for (const [tab, rowSel, rows, total] of TABS) {
    it(`${tab}: the rows are inside the one region, the stated total outside it`, async () => {
      const w = await mountOn(tab)
      expect(w.findAll('[data-testid="ops-list-region"]')).toHaveLength(1)
      const r = region(w)
      expect(r.querySelectorAll(rowSel)).toHaveLength(rows)
      const totalEl = w.get(`[data-testid="${total}"]`).element
      expect(r.contains(totalEl)).toBe(false)
    })

    it(`${tab}: the page root down to the region is one unbroken flex column`, async () => {
      const w = await mountOn(tab)
      expect(has(w.element, 'min-h-dvh', 'flex', 'flex-col')).toBe(true)
      for (const el of chain(w)) {
        const where = `${el.tagName.toLowerCase()}.${[...el.classList].join('.')}`
        expect(has(el, 'grow', 'basis-0'), `${where} must grow from a 0px basis`).toBe(true)
        expect(el.classList.contains('flex-1'), `${where}: flex-1's 0% basis regrows the page`).toBe(false)
        expect(has(el.parentElement, 'flex', 'flex-col'), `${where}'s parent must be a flex column`).toBe(true)
        if (el.classList.contains('mx-auto')) expect(el.classList.contains('w-full'), `${where} needs w-full`).toBe(true)
      }
    })
  }

  it('Health and Reports keep the page layout they had', async () => {
    for (const tab of ['health', 'reports']) {
      const w = await mountOn(tab)
      expect(w.classes()).toContain('min-h-screen')
      expect(w.classes()).not.toContain('flex')
      wrapper.unmount()
      wrapper = null
    }
  })

  it('Load more is the last thing inside the region (Executions, and Notifications when it pages)', async () => {
    let w = await mountOn('executions')
    let r = region(w)
    const more = w.get('[data-testid="executions-load-more"]').element
    expect(r.contains(more) && r.lastElementChild.contains(more)).toBe(true)
    wrapper.unmount()

    // `GET /api/notifications` cannot produce this today — it takes no offset
    // and its `count` is the page length, so Notifications' Load more never
    // renders (filed with the #3451 follow-ups). Pinned so the button lands in
    // the region once the route pages.
    w = await mountOn('notifications', { notificationsTotal: 120 })
    r = region(w)
    const notifMore = w.get('[data-testid="notifications-load-more"]').element
    expect(r.contains(notifMore) && r.lastElementChild.contains(notifMore)).toBe(true)
  })
})

describe('Operations — the reader keeps their place (#3451, p5)', () => {
  beforeEach(() => { vi.clearAllMocks() })
  afterEach(() => { wrapper?.unmount(); wrapper = null })

  it('Needs Response: a poll keeps the region node and its scrollTop', async () => {
    const w = await mountOn('needs-response')
    const before = region(w)
    before.scrollTop = 300
    await useOperatorQueueStore().fetchItems()
    await flushPromises()
    expect(region(w)).toBe(before)
    expect(before.scrollTop).toBe(300)
  })

  it('Executions: a poll keeps the place; a filter change starts at the top', async () => {
    const w = await mountOn('executions')
    const r = region(w)
    r.scrollTop = 300
    await useExecutionsStore().refresh()
    await flushPromises()
    expect(region(w)).toBe(r)
    expect(r.scrollTop).toBe(300)

    await w.findAll('select')[1].setValue('failed')
    await flushPromises()
    expect(r.scrollTop).toBe(0)
  })

  it('Notifications: a refresh keeps the place; a filter change starts at the top', async () => {
    const w = await mountOn('notifications')
    const r = region(w)
    r.scrollTop = 300
    await useNotificationsStore().fetchNotifications()
    await flushPromises()
    expect(region(w)).toBe(r)
    expect(r.scrollTop).toBe(300)

    await w.findAll('select')[2].setValue('urgent')
    await flushPromises()
    expect(r.scrollTop).toBe(0)
  })
})

describe('Operations — nothing that toggles on its own stacks above the region (#3451, p30)', () => {
  beforeEach(() => { vi.clearAllMocks() })
  afterEach(() => { wrapper?.unmount(); wrapper = null })

  /** The elements a list region's top edge depends on: every preceding sibling of every link. */
  const above = (w) => chain(w).flatMap((el) => {
    const out = []
    for (let s = el.previousElementSibling; s; s = s.previousElementSibling) out.push(s)
    return out
  })
  /** Same nodes, same order — identity, not `toEqual` (which compares DOM nodes by structure). */
  const sameNodes = (a, b) => a.length === b.length && a.every((el, i) => el === b[i])

  it('Notifications: selecting a row puts the bulk actions in the list header, not above it', async () => {
    const w = await mountOn('notifications')
    const header = w.get('[data-testid="notifications-list-header"]')
    const stackBefore = above(w)
    await w.get('[data-testid="notification-row"] input[type="checkbox"]').setValue(true)
    expect(header.text()).toContain('Acknowledge Selected')
    expect(sameNodes(above(w), stackBefore)).toBe(true)
  })

  it('Executions: runs starting put "running now" in the count row, not a strip above the list', async () => {
    const w = await mountOn('executions')
    const stackBefore = above(w)
    api.running = 2
    await useExecutionsStore().refresh()
    await flushPromises()
    expect(w.get('[data-testid="executions-running"]').text()).toContain('2 running now')
    expect(sameNodes(above(w), stackBefore)).toBe(true)
  })

  it('Needs Response: the stale banner holds through the next poll instead of blinking', async () => {
    const w = await mountOn('needs-response')
    const store = useOperatorQueueStore()
    api.queueFail = true
    await store.fetchItems()
    await flushPromises()
    expect(w.find('[data-testid="inline-error"]').exists()).toBe(true)

    // A write clears `error` in its prologue with no read running (bulkCancel,
    // clearResolved): nothing was re-read, so the feed is still stale.
    store.error = null
    await nextTick()
    expect(w.find('[data-testid="inline-error"]').exists()).toBe(true)

    // The next poll starts: the store clears `error` as the read begins.
    store.loading = true
    await nextTick()
    expect(w.find('[data-testid="inline-error"]').exists()).toBe(true)

    // It succeeds: now the feed is fresh again.
    store.loading = false
    await nextTick()
    expect(w.find('[data-testid="inline-error"]').exists()).toBe(false)
  })
})
