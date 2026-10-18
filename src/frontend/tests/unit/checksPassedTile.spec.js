// @vitest-environment jsdom
/**
 * "Checks passed" beside Completion (trinity-enterprise#935, C3) — MOUNTED.
 *
 * AC: "The Overview and schedules rollup show Completion and Checks passed as
 * two numbers." The backend adds `checks_passed_rate` + `evaluated_count` to the
 * three analytics payloads; this pins the three places they render:
 *
 *   * ScheduleAnalyticsCard — a "Checks passed" tile next to the Completion tile;
 *   * OverviewPanel headline — the agent's rate next to the completion rate;
 *   * OverviewPanel "Schedules performance" rows — a checks column per schedule.
 *
 * At `evaluated_count == 0` the number is "—", never 0% (nothing evaluated is
 * not "every check failed"), and Completion is unchanged in every case.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

let schedulePayload = null

vi.mock('axios', () => {
  const get = vi.fn(async (url) => {
    if (url.endsWith('/analytics')) return { data: schedulePayload }
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

import ScheduleAnalyticsCard from '../../src/components/ScheduleAnalyticsCard.vue'
import OverviewPanel from '../../src/components/OverviewPanel.vue'
import { useAgentsStore } from '../../src/stores/agents'
import { useExecutionsStore } from '../../src/stores/executions'

const NAME = 'tier0-agent'

function scheduleAnalytics(over = {}) {
  return {
    window_hours: 168, total_executions: 5, success_count: 4, failed_count: 1,
    cancelled_count: 0, success_rate: 0.8,
    checks_passed_rate: 0.5, evaluated_count: 4,
    duration_ms: { p50: 1000, p95: 2000, p99: 3000 }, cost: { total: 0.1 },
    tool_calls: { top: [], total_calls: 0 }, timeline: [], sampled: false, sample_size: 4,
    ...over,
  }
}

function agentAnalytics(over = {}) {
  return {
    window_hours: 168, total_executions: 5, success_count: 4, failed_count: 1,
    success_rate: 0.8, checks_passed_rate: 0.75, evaluated_count: 4,
    duration_ms: { avg: 1000, p95: 2000 }, context_avg: null,
    by_type: [{ bucket: 'Scheduled', total: 5 }], buckets: ['Scheduled'],
    timeline: [{ date: '2026-10-17', total: 5, success: 4, failed: 1, success_rate: 0.8, by_type: { Scheduled: 5 } }],
    sampled: false, sample_size: 4,
    ...over,
  }
}

function summaryRow(over = {}) {
  return {
    schedule_id: 's1', name: 'weekly', command: '/report-weekly', cron_expression: '0 * * * *',
    enabled: true, total_executions: 5, success_count: 4, failed_count: 1, cancelled_count: 0,
    success_rate: 0.8, checks_passed_rate: 0.25, evaluated_count: 4,
    avg_duration_ms: 1000, cost_total: 0.1, context_avg: null, tool_call_total: 0,
    last_run_at: null, last_run_status: 'success',
    ...over,
  }
}

function tileValue(wrapper, label) {
  const tile = wrapper.findAll('div').find((d) => d.element.children.length === 2
    && d.element.children[0].textContent.trim() === label)
  return tile ? tile.element.children[1].textContent.trim() : undefined
}

async function mountCard(payload) {
  schedulePayload = payload
  const wrapper = mount(ScheduleAnalyticsCard, { props: { agentName: NAME, scheduleId: 's1' } })
  await flushPromises()
  return wrapper
}

async function mountOverview({ analytics, summary }) {
  const agents = useAgentsStore()
  agents.getAgentInfo = vi.fn(async () => ({}))
  const executions = useExecutionsStore()
  executions.fetchAgentAnalytics = vi.fn(async () => analytics)
  executions.fetchSchedulesSummary = vi.fn(async () => summary)
  const wrapper = mount(OverviewPanel, {
    props: { agent: { name: NAME, status: 'running' } },
    global: { stubs: { 'router-link': { template: '<a><slot /></a>' } } },
  })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  setActivePinia(createPinia())
  localStorage.clear()
})

describe('ScheduleAnalyticsCard — Checks passed tile', () => {
  it('shows the rate as its own number beside an unchanged Completion', async () => {
    const w = await mountCard(scheduleAnalytics())
    expect(tileValue(w, 'Checks passed')).toBe('50.0%')
    expect(tileValue(w, 'Completion')).toBe('80.0%')
    const tile = w.find('[data-testid="checks-passed"]')
    expect(tile.attributes('title')).toContain('4 of 5 runs evaluated')
  })

  it('shows "—", not 0%, when nothing was evaluated', async () => {
    const w = await mountCard(scheduleAnalytics({ checks_passed_rate: null, evaluated_count: 0 }))
    expect(tileValue(w, 'Checks passed')).toBe('—')
    expect(tileValue(w, 'Completion')).toBe('80.0%')
  })

  it('a real 0% (evaluated, none passed) still reads 0%', async () => {
    const w = await mountCard(scheduleAnalytics({ checks_passed_rate: 0, evaluated_count: 3 }))
    expect(tileValue(w, 'Checks passed')).toBe('0.0%')
  })
})

describe('OverviewPanel — Checks passed beside Completion', () => {
  it('headline shows both numbers, and each schedule row carries its own rate', async () => {
    const w = await mountOverview({
      analytics: agentAnalytics(),
      summary: { window_hours: 168, schedule_count: 1, tool_calls_sampled: false, schedules: [summaryRow()] },
    })
    const head = w.find('[data-testid="overview-checks-passed"]')
    expect(head.exists()).toBe(true)
    expect(head.text()).toContain('75%')
    expect(head.attributes('title')).toContain('4 of 5 runs evaluated')
    expect(w.text()).toContain('80%') // Completion, unchanged
    const row = w.find('[data-testid="schedule-checks-passed"]')
    expect(row.text()).toContain('25%')
  })

  it('renders "—" at zero evaluated, in the headline and the rows', async () => {
    const w = await mountOverview({
      analytics: agentAnalytics({ checks_passed_rate: null, evaluated_count: 0 }),
      summary: {
        window_hours: 168, schedule_count: 1, tool_calls_sampled: false,
        schedules: [summaryRow({ checks_passed_rate: null, evaluated_count: 0 })],
      },
    })
    expect(w.find('[data-testid="overview-checks-passed"]').text()).toContain('—')
    expect(w.find('[data-testid="overview-checks-passed"]').text()).not.toContain('0%')
    expect(w.find('[data-testid="schedule-checks-passed"]').text()).toContain('—')
  })
})
