// @vitest-environment jsdom
/**
 * #3302 — the agent Overview's "needs attention" count is the agent's real
 * pending count.
 *
 * The panel read `/operator-queue/agents/{name}?status=pending&limit=100` and
 * used its `count` — the page's LENGTH — so an agent with 140 open asks showed
 * 100. The list route answers the count as `total` for a one-row page
 * (`?agent_name&status=pending&limit=1`; the user's scope decision). MOUNTED:
 * the number is a template sum over three reads.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

let queueBody = null
const NAME = 'overview-agent'

vi.mock('axios', () => {
  const get = vi.fn(async (url) => {
    if (url === '/api/operator-queue') return { data: queueBody }
    if (url === `/api/operator-queue/agents/${NAME}`) {
      // the old per-agent route: `count` is the page length, capped at the limit
      return { data: { agent_name: NAME, items: [], count: Math.min(queueBody?.total ?? 0, 100) } }
    }
    if (url.includes('/notifications/count')) return { data: { pending_count: 2 } }
    if (url.endsWith('/git/sync-state')) return { data: { consecutive_failures: 0, state: 'green' } }
    return { data: null }
  })
  const instance = { get, post: vi.fn(), interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } }
  return { default: { ...instance, create: () => instance } }
})
vi.mock('../../src/components/TrendLineChart.vue', () => ({ default: { name: 'TrendLineChart', template: '<div />' } }))
vi.mock('../../src/components/StackedBarChart.vue', () => ({ default: { name: 'StackedBarChart', template: '<div />' } }))
vi.mock('../../src/components/CompatibilityPanel.vue', () => ({ default: { name: 'CompatibilityPanel', template: '<div />' } }))

import axios from 'axios'
import OverviewPanel from '../../src/components/OverviewPanel.vue'
import { useAgentsStore } from '../../src/stores/agents'
import { useExecutionsStore } from '../../src/stores/executions'

async function mountPanel() {
  const agents = useAgentsStore()
  agents.getAgentInfo = vi.fn(async () => ({}))
  const executions = useExecutionsStore()
  executions.fetchAgentAnalytics = vi.fn(async () => null)
  executions.fetchSchedulesSummary = vi.fn(async () => null)
  const wrapper = mount(OverviewPanel, {
    props: { agent: { name: NAME, status: 'running' } },
    global: { stubs: { 'router-link': { template: '<a data-testid="needs-attention"><slot /></a>' } } },
  })
  await flushPromises()
  return wrapper
}

describe('Overview — needs attention counts every pending ask (#3302)', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    axios.get.mockClear()
  })

  it('reads the agent\'s pending total from the list route, not a page length', async () => {
    queueBody = { items: [{ id: 'one' }], count: 1, total: 140, has_more: true }
    const wrapper = await mountPanel()
    expect(axios.get).toHaveBeenCalledWith('/api/operator-queue', expect.objectContaining({
      params: { agent_name: NAME, status: 'pending', limit: 1 },
    }))
    expect(wrapper.find('[data-testid="needs-attention"]').text()).toContain('142 items need attention')
  })

  it('an unverified total falls back to the rows the page holds — a floor, never another agent\'s count', async () => {
    queueBody = { items: [{ id: 'one' }], count: 1, total: 140, has_more: true }
    const wrapper = await mountPanel()
    expect(wrapper.find('[data-testid="needs-attention"]').text()).toContain('142 items need attention')
    // the panel is reused when the page switches agents
    queueBody = { items: [{ id: 'two' }], count: 1, total: null, has_more: true }
    await wrapper.setProps({ agent: { name: 'other-agent', status: 'running' } })
    await flushPromises()
    expect(wrapper.find('[data-testid="needs-attention"]').text()).toContain('3 items need attention')
  })
})
