// @vitest-environment jsdom
/**
 * #3450 / #3452 — the Health tab (MonitoringPanel) states what it can show.
 *
 * Both defects are "the panel drops data the API handed it", so both are
 * proven by MOUNTING the panel over a real Pinia and the real monitoring
 * store, with only the HTTP client stubbed:
 *
 *   #3450 — the header counted every alert while the list rendered
 *           `alerts.slice(0, 5)` with no way to reach the rest.
 *   #3452 — `GET /api/monitoring/status` carries a per-agent transport
 *           breaker map (`circuit_breakers`); the store never kept it and the
 *           panel never rendered it, so a DORMANT breaker was invisible.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('axios', () => {
  const interceptors = { request: { use: vi.fn() }, response: { use: vi.fn() } }
  const instance = { get: vi.fn(), post: vi.fn(), put: vi.fn(), interceptors, defaults: { headers: { common: {} } } }
  return { default: { ...instance, create: vi.fn(() => instance) } }
})
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))

import axios from 'axios'
import MonitoringPanel from '../../src/components/MonitoringPanel.vue'
import { useAuthStore } from '../../src/stores/auth'

const agent = (name, status, extra = {}) => ({
  name, status, docker_status: status === 'healthy' ? 'running' : 'exited',
  network_reachable: status === 'healthy', issues: [], last_check_at: '2026-10-09T10:00:00Z', ...extra,
})

const alert = (i) => ({
  id: `notif_${i}`, agent_name: `agent-${i}`, title: `Alert number ${i}`,
  priority: 'high', status: 'pending', created_at: '2026-10-09T10:00:00Z',
})

function stubApi({ alerts = [], agents = [], circuit_breakers = null } = {}) {
  axios.get.mockImplementation(async (url) => {
    if (url === '/api/monitoring/status') {
      return { data: {
        enabled: true, last_check_at: '2026-10-09T10:00:00Z',
        summary: { total_agents: agents.length, healthy: 0, degraded: 0, unhealthy: 0, critical: 0, unknown: 0 },
        agents, circuit_breakers,
      } }
    }
    if (url === '/api/monitoring/alerts') return { data: { count: alerts.length, alerts } }
    throw new Error(`unexpected GET ${url}`)
  })
}

let wrapper
async function mountPanel() {
  const pinia = createPinia()
  setActivePinia(pinia)
  useAuthStore().user = { role: 'admin' }
  wrapper = mount(MonitoringPanel, { global: { plugins: [pinia] } })
  await flushPromises()
  return wrapper
}

beforeEach(() => { vi.clearAllMocks() })
afterEach(() => { wrapper?.unmount(); wrapper = null })

describe('#3450 — every active alert is reachable, inside a bounded viewport', () => {
  it('renders one row per alert, so the stated count is the reachable count', async () => {
    stubApi({ alerts: Array.from({ length: 12 }, (_, i) => alert(i + 1)) })
    const w = await mountPanel()

    const rows = w.findAll('[data-testid="alert-row"]')
    expect(rows).toHaveLength(12)
    expect(rows[11].text()).toContain('Alert number 12')
    expect(w.text()).toContain('Active Alerts (12)')
  })

  it('contains the list in a scrollable, keyboard-reachable viewport instead of growing the page', async () => {
    stubApi({ alerts: Array.from({ length: 12 }, (_, i) => alert(i + 1)) })
    const w = await mountPanel()

    const list = w.get('[data-testid="alerts-list"]')
    // jsdom has no layout: the bound is the max-height + own-axis scroll pair.
    expect(list.classes().some((c) => c.startsWith('max-h-'))).toBe(true)
    expect(list.classes()).toContain('overflow-y-auto')
    expect(list.attributes('tabindex')).toBe('0')
    expect(list.findAll('[data-testid="alert-row"]')).toHaveLength(12)
  })
})

describe('#3452 — a non-closed transport breaker is shown on its agent row', () => {
  const rowFor = (w, name) => w.get(`[data-testid="agent-health-row"][data-agent="${name}"]`)

  it('a DORMANT breaker reads as dormant, with its failure count', async () => {
    stubApi({
      agents: [agent('sleepy', 'critical'), agent('fine', 'healthy')],
      circuit_breakers: {
        sleepy: { state: 'dormant', failure_count: 825, cooldown_remaining: 0 },
        fine: { state: 'closed', failure_count: 0, cooldown_remaining: 0 },
      },
    })
    const w = await mountPanel()

    const badge = rowFor(w, 'sleepy').get('[data-testid="breaker-state"]')
    expect(badge.text()).toBe('Breaker dormant')
    expect(rowFor(w, 'sleepy').text()).toContain('825 failed connection attempts')
  })

  it('an OPEN breaker is shown too; a closed or unreported one adds nothing', async () => {
    stubApi({
      agents: [agent('tripped', 'unhealthy'), agent('fine', 'healthy'), agent('no-history', 'healthy')],
      circuit_breakers: {
        tripped: { state: 'open', failure_count: 1, cooldown_remaining: 12 },
        fine: { state: 'closed', failure_count: 0, cooldown_remaining: 0 },
      },
    })
    const w = await mountPanel()

    expect(rowFor(w, 'tripped').get('[data-testid="breaker-state"]').text()).toBe('Breaker open')
    expect(rowFor(w, 'tripped').text()).toContain('1 failed connection attempt')
    expect(rowFor(w, 'tripped').text()).not.toContain('attempts')
    expect(rowFor(w, 'fine').find('[data-testid="breaker-state"]').exists()).toBe(false)
    expect(rowFor(w, 'no-history').find('[data-testid="breaker-state"]').exists()).toBe(false)
  })

  it('a status payload with no breaker map (non-admin, Redis down) renders rows without a badge', async () => {
    stubApi({ agents: [agent('sleepy', 'critical')], circuit_breakers: null })
    const w = await mountPanel()

    expect(rowFor(w, 'sleepy').text()).toContain('sleepy')
    expect(w.find('[data-testid="breaker-state"]').exists()).toBe(false)
  })
})
