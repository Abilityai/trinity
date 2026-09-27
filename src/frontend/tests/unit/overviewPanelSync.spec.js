// @vitest-environment jsdom
/**
 * Agent Detail → Overview: the footprint's sync line (trinity-enterprise#707) — MOUNTED.
 *
 * It read only `consecutive_failures` and said `ok` for every agent that had
 * not failed a push — including one 15 commits ahead with auto-sync off, and
 * one with no git binding at all. It now renders the backend's numbers from
 * `/git/sync-state` (`↑7 ↓0 · 12 dirty · pushed 3h ago`) in the state colour,
 * with the reason and recommendation on hover (status ink one tier up: it sits
 * on chrome), and `—` when the backend has no
 * observation. The attention count still counts failed syncs (decision D13).
 *
 * axios is mocked per URL; everything else the panel fetches answers empty.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

let syncPayload = null
let syncFails = false

vi.mock('axios', () => {
  const get = vi.fn(async (url) => {
    if (url.endsWith('/git/sync-state')) {
      if (syncFails) throw new Error('network down')
      return { data: syncPayload }
    }
    if (url.includes('/operator-queue/')) return { data: { count: 0 } }
    if (url.includes('/notifications/count')) return { data: { pending_count: 0 } }
    return { data: null }
  })
  const instance = { get, post: vi.fn(), interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } }
  return { default: { ...instance, create: () => instance } }
})

vi.mock('../../src/components/TrendLineChart.vue', () => ({
  default: { name: 'TrendLineChart', template: '<div />' },
}))
vi.mock('../../src/components/StackedBarChart.vue', () => ({
  default: { name: 'StackedBarChart', template: '<div />' },
}))
vi.mock('../../src/components/CompatibilityPanel.vue', () => ({
  default: { name: 'CompatibilityPanel', template: '<div />' },
}))

import OverviewPanel from '../../src/components/OverviewPanel.vue'
import { useAgentsStore } from '../../src/stores/agents'
import { useExecutionsStore } from '../../src/stores/executions'

const NAME = 'overview-agent'
const RAW_ERROR = 'remote: LEAKMARKER fatal: unable to access'

function state(over = {}) {
  const pushed = new Date(Date.now() - 3 * 3600 * 1000 - 60 * 1000).toISOString()
  return {
    agent_name: NAME,
    last_sync_status: 'success',
    consecutive_failures: 0,
    last_error_summary: null,
    ahead_working: 7,
    behind_working: 0,
    dirty_files: 12,
    last_successful_push_at: pushed,
    state: 'red',
    reason: 'diverged 0 behind / 7 ahead for 26h',
    recommendation: 'enable auto-sync',
    ...over,
  }
}

async function mountPanel() {
  const agents = useAgentsStore()
  agents.getAgentInfo = vi.fn(async () => ({}))
  const executions = useExecutionsStore()
  executions.fetchAgentAnalytics = vi.fn(async () => null)
  executions.fetchSchedulesSummary = vi.fn(async () => null)
  const wrapper = mount(OverviewPanel, {
    props: { agent: { name: NAME, status: 'running' } },
    global: { stubs: { 'router-link': { template: '<a><slot /></a>' } } },
  })
  await flushPromises()
  return wrapper
}

const syncLine = (wrapper) => wrapper.find('[data-testid="overview-sync"]')

beforeEach(() => {
  setActivePinia(createPinia())
  syncPayload = null
  syncFails = false
})

describe('Overview sync line', () => {
  it.each([
    ['red', 'text-status-danger-800'],
    ['yellow', 'text-status-warning-800'],
    ['green', 'text-status-success-800'],
  ])('state %s renders the numbers in %s', async (s, cls) => {
    syncPayload = state({ state: s })
    const line = syncLine(await mountPanel())
    expect(line.exists()).toBe(true)
    expect(line.text()).toBe('Sync: ↑7 ↓0 · 12 dirty · pushed 3h ago')
    expect(line.find('[data-testid="overview-sync-value"]').classes()).toContain(cls)
  })

  it('hover gives the reason and recommendation, never the raw error', async () => {
    syncPayload = state({
      last_sync_status: 'failed', consecutive_failures: 3, last_error_summary: RAW_ERROR,
      reason: 'last sync failed (seen on 3 polls)',
      recommendation: 'push via git_sync strategy=pull_first',
    })
    const wrapper = await mountPanel()
    expect(syncLine(wrapper).attributes('title').split('\n')[0]).toBe(
      'last sync failed (seen on 3 polls) — push via git_sync strategy=pull_first')
    expect(wrapper.html()).not.toContain('LEAKMARKER')
  })

  it('an agent with no observation says —, not ok', async () => {
    syncPayload = { agent_name: NAME, last_sync_status: 'never', consecutive_failures: 0,
      state: 'unknown', reason: 'no sync observation yet', recommendation: null }
    const line = syncLine(await mountPanel())
    expect(line.text()).toBe('Sync: —')
    expect(line.text()).not.toMatch(/ok/)
  })

  it('a failed read keeps one footprint and claims nothing', async () => {
    syncFails = true
    const line = syncLine(await mountPanel())
    expect(line.exists()).toBe(true)
    expect(line.text()).toBe('Sync: —')
  })

  it('the attention count still counts failed syncs (D13), not red divergence', async () => {
    syncPayload = state({ state: 'red', consecutive_failures: 0 })
    const quiet = await mountPanel()
    expect(quiet.text()).not.toMatch(/needs? attention/i)

    setActivePinia(createPinia())
    syncPayload = state({ state: 'red', last_sync_status: 'failed', consecutive_failures: 2 })
    const loud = await mountPanel()
    expect(loud.text()).toMatch(/2 items need attention/)
  })
})
