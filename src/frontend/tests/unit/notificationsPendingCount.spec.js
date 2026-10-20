// @vitest-environment jsdom
/**
 * #3449 — the global pending-notification count is independent of the list.
 *
 * `pendingCount` backs the Operations header, the Notifications tab badge and
 * the NavBar badge. It used to be recomputed from whatever rows the
 * Notifications tab had loaded, so a status / agent filter (or simply the page
 * limit) rewrote all three. It is a server figure (`GET
 * /api/notifications/count`) — the list never writes it — and the panel's
 * fourth card states what it is: the rows shown, not a total.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('axios', () => {
  const inst = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), defaults: { headers: { common: {} } } }
  return { default: inst }
})
vi.mock('@/api', () => {
  const inst = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }
  return { default: inst }
})
vi.mock('@/utils/platformSession', () => ({ readStoredToken: () => 'test-token' }))

import axios from 'axios'
import { useNotificationsStore } from '@/stores/notifications'
import NotificationsPanel from '@/components/operator/NotificationsPanel.vue'

const GLOBAL_PENDING = 502

function rows(n, status, agent = 'agent-a') {
  return Array.from({ length: n }, (_, i) => ({
    id: `${status}-${agent}-${i}`,
    agent_name: agent,
    notification_type: 'info',
    title: `Notification ${i}`,
    priority: 'normal',
    status,
    created_at: '2026-10-09T08:00:00Z',
    message: null,
    metadata: null,
  }))
}

// The backend as the store sees it: a true pending count, and a list endpoint
// that honours the status filter and caps the page at `limit`.
function serve({ pending = GLOBAL_PENDING, acknowledged = 34 } = {}) {
  axios.get.mockImplementation(async (url) => {
    if (url.startsWith('/api/notifications/count')) {
      return { data: { status: 'pending', count: pending } }
    }
    const q = new URLSearchParams(url.split('?')[1] || '')
    const limit = Number(q.get('limit') || 50)
    const status = q.get('status')
    const all = status === 'acknowledged'
      ? rows(acknowledged, 'acknowledged')
      : rows(pending, 'pending')
    const page = all.slice(0, limit)
    return { data: { count: page.length, notifications: page } }
  })
  axios.post.mockResolvedValue({ data: {} })
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  serve()
})

describe('store: pendingCount is the server count, never the loaded list', () => {
  it('a status filter on the list leaves the global pending count alone', async () => {
    const store = useNotificationsStore()
    await store.fetchPendingCount()
    expect(store.pendingCount).toBe(GLOBAL_PENDING)

    await store.fetchNotifications({ status: 'acknowledged' })
    await flushPromises()

    expect(store.notifications).toHaveLength(34)
    expect(store.pendingCount).toBe(GLOBAL_PENDING)
  })

  it('the page limit does not cap the global pending count', async () => {
    const store = useNotificationsStore()
    await store.fetchPendingCount()

    await store.fetchNotifications() // default filter: status=pending, limit=50
    await flushPromises()

    expect(store.notifications).toHaveLength(50)
    expect(store.pendingCount).toBe(GLOBAL_PENDING)
  })

  it('an agent filter leaves the global pending count alone', async () => {
    const store = useNotificationsStore()
    await store.fetchPendingCount()

    axios.get.mockImplementation(async (url) => (
      url.startsWith('/api/notifications/count')
        ? { data: { status: 'pending', count: GLOBAL_PENDING } }
        : { data: { count: 2, notifications: rows(2, 'pending', 'agent-b') } }
    ))
    await store.fetchNotifications({ agentName: 'agent-b' })
    await flushPromises()

    expect(store.notifications).toHaveLength(2)
    expect(store.pendingCount).toBe(GLOBAL_PENDING)
  })

  it('a list fetch refreshes the count from the count endpoint', async () => {
    const store = useNotificationsStore()
    await store.fetchNotifications({ status: 'acknowledged' })
    await flushPromises()

    expect(axios.get.mock.calls.some(([u]) => u.startsWith('/api/notifications/count'))).toBe(true)
    expect(store.pendingCount).toBe(GLOBAL_PENDING)
  })

  it('acknowledging a pending row takes one off; dismissing an acknowledged row does not', async () => {
    const store = useNotificationsStore()
    await store.fetchNotifications()
    await flushPromises()
    await store.acknowledgeNotification(store.notifications[0].id)
    expect(store.pendingCount).toBe(GLOBAL_PENDING - 1)

    // The row is acknowledged now — dismissing it removes no pending item.
    await store.dismissNotification(store.notifications[0].id)
    expect(store.pendingCount).toBe(GLOBAL_PENDING - 1)

    await store.dismissNotification(store.notifications[0].id)
    expect(store.pendingCount).toBe(GLOBAL_PENDING - 2)
  })

  it('a live pending event counts even when the list filter hides it', async () => {
    const store = useNotificationsStore()
    store.setFilters({ status: 'acknowledged' })
    await flushPromises()
    expect(store.pendingCount).toBe(GLOBAL_PENDING)

    store.addNotification({ id: 'live-1', agent_name: 'agent-a', status: 'pending', priority: 'normal' })

    expect(store.notifications.some(n => n.id === 'live-1')).toBe(false)
    expect(store.pendingCount).toBe(GLOBAL_PENDING + 1)
  })
})

describe('NotificationsPanel: the cards say what they count', () => {
  function cards(wrapper) {
    const out = {}
    wrapper.findAll('[data-testid^="notif-stat-"]').forEach((el) => {
      out[el.attributes('data-testid').replace('notif-stat-', '')] = el.text()
    })
    return out
  }

  it('Pending stays the global figure under a filter, and the row card is "Shown", not "Total"', async () => {
    const wrapper = mount(NotificationsPanel, { global: { stubs: { teleport: true } } })
    await flushPromises()

    expect(cards(wrapper).pending).toBe(`${GLOBAL_PENDING}Pending`)
    expect(cards(wrapper).shown).toBe('50Shown')

    // Agent · Type · Priority · Status — the Status select is the fourth.
    await wrapper.findAll('select')[3].setValue('acknowledged')
    await flushPromises()

    const after = cards(wrapper)
    expect(after.pending).toBe(`${GLOBAL_PENDING}Pending`)
    expect(after.shown).toBe('34Shown')
    expect(wrapper.text()).not.toContain('Total')
  })
})
