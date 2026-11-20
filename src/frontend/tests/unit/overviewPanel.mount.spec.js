// @vitest-environment jsdom
/**
 * Agent Detail → Overview, rebuilt (trinity-enterprise#1317) — MOUNTED.
 *
 * The page is judged first on the agent with nothing to show: zero runs, no
 * schedules, no declared metrics, no health checks. Every section must still
 * be there at its own footprint, with a dash for anything never measured and
 * an empty state that names what will appear. Then the active agent, the
 * failed read, and the agent switch.
 *
 * axios is mocked per URL; the store reads through `api.js`, an instance of
 * the same mock.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h, KeepAlive, nextTick } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

let routes = {}
let failing = new Set()

const match = (url) => Object.keys(routes).find((k) => url === k || url.endsWith(k))

vi.mock('axios', () => {
  const get = vi.fn(async (url, config) => {
    for (const suffix of failing) if (url.endsWith(suffix)) throw new Error('network down')
    const key = match(url)
    const body = key ? routes[key] : null
    return { data: typeof body === 'function' ? body(config) : body }
  })
  const instance = { get, post: vi.fn(), interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } }
  return { default: { ...instance, create: () => instance } }
})

import axios from 'axios'
import OverviewPanel from '../../src/components/OverviewPanel.vue'
import OverviewHealth from '../../src/components/overview/OverviewHealth.vue'
import OverviewMeasureTile from '../../src/components/overview/OverviewMeasureTile.vue'
import { useAuthStore } from '../../src/stores/auth'

const NAME = 'scout'
const day = (date, over = {}) => ({
  date, total: 0, success: 0, failed: 0, success_rate: null,
  duration_avg_ms: null, context_avg: null, context_pct_avg: null, by_type: {}, ...over,
})

const EMPTY_ANALYTICS = {
  window_hours: 168, total_executions: 0, success_count: 0, failed_count: 0, success_rate: 0,
  duration_ms: { avg: null, p95: null }, context_avg: null, context_pct_avg: null,
  by_type: [], buckets: [],
  timeline: ['2026-11-14', '2026-11-15', '2026-11-16', '2026-11-17', '2026-11-18', '2026-11-19', '2026-11-20'].map((d) => day(d)),
}

const BUSY_ANALYTICS = {
  window_hours: 168, total_executions: 21, success_count: 18, failed_count: 2, success_rate: 0.9,
  duration_ms: { avg: 134000, p95: 400000 }, context_avg: 76000, context_pct_avg: 38,
  by_type: [], buckets: ['Chat/Tasks', 'Scheduled'],
  timeline: [
    day('2026-11-14', { total: 6, success: 6, success_rate: 1, duration_avg_ms: 100000, context_pct_avg: 30, by_type: { Scheduled: 4, 'Chat/Tasks': 2 } }),
    day('2026-11-15'),
    day('2026-11-16', { total: 8, success: 6, failed: 2, success_rate: 0.75, duration_avg_ms: 160000, context_pct_avg: 44, by_type: { Scheduled: 5, 'Chat/Tasks': 3 } }),
    day('2026-11-17', { total: 7, success: 6, success_rate: 1, duration_avg_ms: 140000, context_pct_avg: 40, by_type: { Scheduled: 7 } }),
  ],
}

/** A brand-new agent: every read succeeds and has nothing in it. */
function newAgent() {
  return {
    '/analytics': EMPTY_ANALYTICS,
    '/schedules/analytics-summary': { window_hours: 168, schedule_count: 0, schedules: [] },
    '/schedules': [],
    '/api/executions/stats': { running_count: 0, queued_count: 0 },
    '/api/executions': [],
    '/notifications/count': { pending_count: 0 },
    '/api/operator-queue': { items: [], count: 0, total: 0 },
    '/git/sync-state': { last_sync_status: 'never', consecutive_failures: 0, state: 'unknown' },
    '/history': { checks: [] },
    [`/api/monitoring/agents/${NAME}`]: { agent_name: NAME, aggregate_status: 'unknown' },
    '/metrics': { metrics: [] },
  }
}

function activeAgent() {
  const soon = new Date(Date.now() + 48 * 60000).toISOString()
  const checks = []
  for (let m = 0; m <= 480; m += 10) checks.push({ checked_at: new Date(Date.now() - m * 60000).toISOString(), reachable: true, latency_ms: 40 })
  return {
    ...newAgent(),
    '/analytics': BUSY_ANALYTICS,
    '/schedules/analytics-summary': { schedules: [
      { schedule_id: 's1', name: 'morning-digest', command: '/digest', cron_expression: '0 9 * * 1-5', enabled: true, total_executions: 10, success_count: 9, failed_count: 1, avg_duration_ms: 112000 },
    ] },
    '/schedules': [{ id: 's1', name: 'morning-digest', enabled: true, next_run_at: soon }],
    '/api/executions/stats': { running_count: 1, queued_count: 0 },
    '/api/executions': [
      { id: 'e1', status: 'running', message: 'Weekly pipeline review', started_at: new Date(Date.now() - 4 * 60000).toISOString(), triggered_by: 'schedule' },
      { id: 'e2', status: 'failed', message: 'Enrich contacts', started_at: new Date(Date.now() - 3 * 3600000).toISOString(), duration_ms: 48000, triggered_by: 'mcp' },
    ],
    '/history': { checks },
    [`/api/monitoring/agents/${NAME}`]: { agent_name: NAME, aggregate_status: 'healthy', uptime_percent_24h: 99.62, avg_latency_24h_ms: 41.7, docker: { restart_count: 0 } },
    '/metrics': { metrics: [
      { name: 'qualified', label: 'Qualified', type: 'counter', latest: { value: 128 } },
      { name: 'reply_rate', label: 'Reply rate', type: 'percentage', latest: { value: 31 } },
      { name: 'old', label: 'Old', type: 'counter', status: 'retired', latest: { value: 9 } },
    ] },
  }
}

// Every mount is unmounted after its test: the panel runs a 1s and a 30s timer.
const mounted = []
async function mountPanel(agent = { name: NAME, status: 'running', can_share: true }) {
  const wrapper = mount(OverviewPanel, {
    props: { agent },
    global: { stubs: { 'router-link': { template: '<a><slot /></a>' } } },
  })
  mounted.push(wrapper)
  await flushPromises()
  return wrapper
}

afterEach(() => {
  while (mounted.length) mounted.pop().unmount()
  vi.useRealTimers()
})

const t = (wrapper, id) => wrapper.find(`[data-testid="${id}"]`)

beforeEach(() => {
  setActivePinia(createPinia())
  axios.get.mockClear()
  routes = newAgent()
  failing = new Set()
})

describe('a new agent with nothing to show', () => {
  it('renders every section, at its footprint', async () => {
    const wrapper = await mountPanel()
    for (const id of [
      'overview-now', 'overview-activity', 'overview-recent',
      'overview-tile-finished', 'overview-tile-time-per-run', 'overview-tile-context-used',
      'overview-reported', 'overview-schedules', 'overview-health',
    ]) expect(t(wrapper, id).exists(), id).toBe(true)
  })

  it('draws ghost charts, not blank space and not data', async () => {
    const wrapper = await mountPanel()
    expect(t(wrapper, 'overview-activity-ghost').exists()).toBe(true)
    expect(t(wrapper, 'overview-activity-plot').exists()).toBe(false)
    expect(wrapper.findAll('[data-testid="overview-tile-ghost"]')).toHaveLength(3)
    expect(wrapper.findAll('[data-testid="overview-tile-line"]')).toHaveLength(0)
    expect(t(wrapper, 'overview-health-strip').findAll('i')).toHaveLength(24)
  })

  it('shows a dash for everything never measured, and never 0%', async () => {
    const wrapper = await mountPanel()
    expect(wrapper.findAll('[data-testid="overview-tile-value"]').map((n) => n.text())).toEqual(['—', '—', '—'])
    expect(t(wrapper, 'overview-health-uptime').text()).toBe('—')
    expect(t(wrapper, 'overview-sync').text()).toBe('Sync: —')
    expect(wrapper.text()).not.toMatch(/\b0%/)
  })

  it('every empty list says what will appear and offers one next step', async () => {
    const wrapper = await mountPanel()
    expect(t(wrapper, 'overview-now-state').text()).toBe('Idle, waiting for its first run')
    expect(t(wrapper, 'overview-now-next').text()).toContain('Nothing scheduled')
    expect(t(wrapper, 'overview-activity-readout').text()).toMatch(/Nothing has run yet/)
    expect(t(wrapper, 'overview-recent-empty').text()).toMatch(/No work yet.*Give it a first task/s)
    expect(t(wrapper, 'overview-schedules-empty').text()).toMatch(/No schedules yet.*Add a schedule/s)
    expect(t(wrapper, 'overview-reported-empty').text()).toMatch(/reports no numbers of its own yet/)
    expect(t(wrapper, 'overview-health-off').text()).toMatch(/No health checks recorded/)
  })

  it('has no attention chip and nothing that reads as an error', async () => {
    const wrapper = await mountPanel()
    expect(t(wrapper, 'needs-attention').exists()).toBe(false)
    expect(wrapper.text()).not.toMatch(/couldn't load/i)
  })

  it('the empty-state actions go where they say', async () => {
    const wrapper = await mountPanel()
    await t(wrapper, 'overview-recent-empty').find('button').trigger('click')
    await t(wrapper, 'overview-schedules-empty').find('button').trigger('click')
    await t(wrapper, 'overview-reported-empty').find('button').trigger('click')
    await t(wrapper, 'overview-new-task').trigger('click')
    expect(wrapper.emitted('navigate-tab').map(([tab]) => tab)).toEqual(['tasks', 'schedules', 'dashboard', 'tasks'])
  })

  it('a viewer who cannot declare metrics is not offered the door', async () => {
    const wrapper = await mountPanel({ name: NAME, status: 'running', can_share: false })
    expect(t(wrapper, 'overview-reported-empty').find('button').exists()).toBe(false)
  })

  it('a stopped new agent says so, and still has the whole page', async () => {
    const wrapper = await mountPanel({ name: NAME, status: 'stopped', can_share: true })
    expect(t(wrapper, 'overview-now-state').text()).toContain('Stopped')
    expect(t(wrapper, 'overview-activity-ghost').exists()).toBe(true)
  })
})

describe('the compatibility report', () => {
  it('is not on the Overview and is not read for it', async () => {
    routes = activeAgent()
    const wrapper = await mountPanel()
    expect(wrapper.text()).not.toMatch(/compatib/i)
    expect(axios.get.mock.calls.some(([url]) => url.includes('compatibility'))).toBe(false)
  })
})

describe('an active agent', () => {
  beforeEach(() => { routes = activeAgent() })

  it('says what it is doing and when it runs next', async () => {
    const wrapper = await mountPanel()
    expect(t(wrapper, 'overview-now-state').text()).toMatch(/^Working on “Weekly pipeline review” · 4m \d\ds$/)
    expect(t(wrapper, 'overview-now-next').text()).toMatch(/^in 4[78]m · morning-digest$/)
  })

  it('opens the running task from the Now row and a past one from the list', async () => {
    const wrapper = await mountPanel()
    await t(wrapper, 'overview-now-state').find('button').trigger('click')
    await wrapper.findAll('[data-testid="overview-recent-row"]')[1].trigger('click')
    expect(wrapper.emitted('open-task')).toEqual([['e1'], ['e2']])
  })

  it('a long multi-line prompt shows its first line, clamped, with the rest on hover', async () => {
    routes['/api/executions'] = [
      { id: 'p1', status: 'success', message: 'Review the Q4 pipeline\nYou are a careful analyst.\n- one\n- two', started_at: new Date(Date.now() - 60000).toISOString(), duration_ms: 9000, triggered_by: 'manual', schedule_id: '__manual__' },
      { id: 'p2', status: 'success', message: '/digest', started_at: new Date(Date.now() - 120000).toISOString(), duration_ms: 9000, triggered_by: 'schedule', schedule_id: 's1' },
    ]
    const wrapper = await mountPanel()
    const rows = wrapper.findAll('[data-testid="overview-recent-row"]')
    const title = rows[0].find('[data-testid="overview-recent-title"]')
    expect(title.text()).toBe('Review the Q4 pipeline')
    expect(title.classes()).toContain('line-clamp-2')
    expect(rows[0].attributes('title')).toBe('Review the Q4 pipeline You are a careful analyst. - one - two')
    expect(rows[1].find('[data-testid="overview-recent-title"]').text()).toBe('morning-digest')
    expect(rows[1].find('[data-testid="overview-recent-detail"]').text()).toBe('/digest')
  })

  it('stacks runs per day, stubs the empty day and rails the failures', async () => {
    const wrapper = await mountPanel()
    expect(wrapper.findAll('[data-testid="overview-activity-col"]')).toHaveLength(4)
    expect(wrapper.findAll('[data-testid="overview-activity-stub"]')).toHaveLength(1)
    expect(wrapper.findAll('[data-testid="overview-activity-fail"]')).toHaveLength(1)
    expect(t(wrapper, 'overview-activity-readout').text()).toMatch(/^21\s*runs · 2 failed · busiest Mon, Nov 16 \(8\)$/)
  })

  it('reads one day into the reserved line on hover, and puts the summary back', async () => {
    const wrapper = await mountPanel()
    await wrapper.findAll('[data-testid="overview-activity-col"]')[2].trigger('mouseenter')
    expect(t(wrapper, 'overview-activity-readout').text()).toMatch(/^8\s*Mon, Nov 16 · 3 Chat\/Tasks · 5 Scheduled · 2 failed$/)
    await t(wrapper, 'overview-activity-plot').trigger('mouseleave')
    expect(t(wrapper, 'overview-activity-readout').text()).toMatch(/^21\s*runs/)
  })

  it('the same reading is reachable from the keyboard', async () => {
    const wrapper = await mountPanel()
    const plot = t(wrapper, 'overview-activity-plot')
    expect(plot.attributes('tabindex')).toBe('0')
    await plot.trigger('keydown', { key: 'ArrowRight' })
    expect(t(wrapper, 'overview-activity-readout').text()).toMatch(/Sat, Nov 14/)
    await plot.trigger('keydown', { key: 'ArrowRight' })
    expect(t(wrapper, 'overview-activity-readout').text()).toMatch(/Sun, Nov 15 · no runs/)
    await plot.trigger('keydown', { key: 'End' })
    expect(t(wrapper, 'overview-activity-readout').text()).toMatch(/Tue, Nov 17/)
    await plot.trigger('keydown', { key: 'Escape' })
    expect(t(wrapper, 'overview-activity-readout').text()).toMatch(/^21\s*runs/)
  })

  it('the tiles carry values, a denominator, and real lines', async () => {
    const wrapper = await mountPanel()
    expect(wrapper.findAll('[data-testid="overview-tile-value"]').map((n) => n.text())).toEqual(['90%', '2m 14s', '38%'])
    expect(t(wrapper, 'overview-tile-finished').find('[data-testid="overview-tile-sub"]').text()).toBe('18 of 20 runs')
    expect(wrapper.findAll('[data-testid="overview-tile-line"]')).toHaveLength(3)
    expect(wrapper.findAll('[data-testid="overview-tile-guide"]')).toHaveLength(1)
  })

  it('lists what the agent reports about itself, without retired metrics', async () => {
    const wrapper = await mountPanel()
    const tiles = wrapper.findAll('[data-testid="overview-reported-tile"]')
    expect(tiles.map((n) => n.text())).toEqual(['Qualified128', 'Reply rate31%'])
  })

  it('a schedule row states next run and outcome with its denominator', async () => {
    const wrapper = await mountPanel()
    const row = t(wrapper, 'overview-schedule-row')
    expect(row.text()).toMatch(/\/digest/)
    expect(row.text()).toMatch(/in 4[78]m/)
    expect(row.text()).toMatch(/9 of 10 finished in 7d/)
  })

  it('health shows the verdict, 24h figures and a strip labelled with its real span', async () => {
    const wrapper = await mountPanel()
    expect(t(wrapper, 'overview-health-verdict').text()).toBe('Healthy')
    expect(t(wrapper, 'overview-health-uptime').text()).toBe('99.6%')
    expect(t(wrapper, 'overview-health-latency').text()).toBe('42 ms')
    expect(t(wrapper, 'overview-health').text()).toMatch(/8h ago\s*Now/)
  })

  it('switching the window refetches the charts without going back to loading', async () => {
    const wrapper = await mountPanel()
    let release
    const gate = new Promise((r) => { release = r })
    const first = routes['/analytics']
    routes['/analytics'] = (config) => (config?.params?.window === '30d' ? { ...first, total_executions: 77 } : first)
    const realGet = axios.get.getMockImplementation()
    axios.get.mockImplementation(async (url, config) => {
      if (url.endsWith('/analytics') && config?.params?.window === '30d') await gate
      return realGet(url, config)
    })
    const thirty = wrapper.findAll('[role="radio"]').find((b) => b.text() === '30d')
    await thirty.trigger('click')
    await flushPromises()
    // the 30d read is still out: the 7d chart is still on screen
    expect(t(wrapper, 'overview-activity-plot').exists()).toBe(true)
    expect(t(wrapper, 'overview-activity-readout').text()).toMatch(/^21\s*runs/)
    release()
    await flushPromises()
    expect(t(wrapper, 'overview-activity-readout').text()).toMatch(/^77\s*runs/)
    expect(axios.get.mock.calls.filter(([url, c]) => url.endsWith('/analytics') && c?.params?.window === '30d')).toHaveLength(1)
    axios.get.mockImplementation(realGet)
  })
})

describe('failed reads', () => {
  it('a failed read is a failed section, never the empty copy', async () => {
    failing = new Set(['/analytics', '/api/executions'])
    const wrapper = await mountPanel()
    expect(t(wrapper, 'overview-activity').text()).toMatch(/Couldn't load activity/)
    expect(t(wrapper, 'overview-activity-ghost').exists()).toBe(false)
    expect(t(wrapper, 'overview-recent').text()).toMatch(/Couldn't load recent work/)
    expect(t(wrapper, 'overview-recent-empty').exists()).toBe(false)
    expect(t(wrapper, 'overview-tile-finished').find('[data-testid="overview-tile-sub"]').text()).toBe("couldn't load")
    // the rest of the page is unaffected
    expect(t(wrapper, 'overview-schedules-empty').exists()).toBe(true)
  })

  it('retry reads the failed section again', async () => {
    failing = new Set(['/api/executions'])
    const wrapper = await mountPanel()
    failing = new Set()
    await t(wrapper, 'overview-recent').find('button').trigger('click')
    await flushPromises()
    expect(t(wrapper, 'overview-recent-empty').exists()).toBe(true)
  })

  it("switching agents never leaves the previous agent's figures on a section whose read failed (#3815)", async () => {
    routes = activeAgent()
    const wrapper = await mountPanel()
    expect(wrapper.text()).toContain('Weekly pipeline review')
    expect(wrapper.text()).toContain('99.6%')

    routes = newAgent()
    failing = new Set(['/analytics', '/api/executions', '/api/monitoring/agents/other', '/metrics', '/schedules/analytics-summary'])
    await wrapper.setProps({ agent: { name: 'other', status: 'running', can_share: true } })
    await flushPromises()
    const text = wrapper.text()
    for (const leaked of ['Weekly pipeline review', '99.6%', 'Qualified', '/digest', '18 of 20', '21']) {
      expect(text, leaked).not.toContain(leaked)
    }
    expect(t(wrapper, 'overview-recent').text()).toMatch(/Couldn't load recent work/)
    expect(t(wrapper, 'overview-health').text()).toMatch(/Couldn't load health/)
  })

  it('a refresh that fails keeps the data on screen', async () => {
    routes = activeAgent()
    const wrapper = await mountPanel()
    failing = new Set(['/analytics'])
    await wrapper.findAll('[role="radio"]').find((b) => b.text() === '14d').trigger('click')
    await flushPromises()
    expect(t(wrapper, 'overview-activity-plot').exists()).toBe(true)
    expect(t(wrapper, 'overview-activity-stale').exists()).toBe(true)
    // the numbers are still 7d's, so nothing on the page may say 14d
    const checked = wrapper.findAll('[role="radio"]').find((b) => b.attributes('aria-checked') === 'true')
    expect(checked.text()).toBe('7d')
    expect(t(wrapper, 'overview-tile-finished').text()).toContain('7d')
    expect(t(wrapper, 'overview-schedule-row').text()).toMatch(/finished in 7d/)
  })
})

describe('responses that arrive out of order', () => {
  it("a slow answer for a window the user has left never lands under the new window's label", async () => {
    routes = activeAgent()
    const wrapper = await mountPanel()
    const base = routes['/analytics']
    let release30
    const gate30 = new Promise((r) => { release30 = r })
    const realGet = axios.get.getMockImplementation()
    axios.get.mockImplementation(async (url, config) => {
      const win = config?.params?.window
      if (url.endsWith('/analytics') && win === '30d') { await gate30; return { data: { ...base, total_executions: 3000 } } }
      if (url.endsWith('/analytics') && win === '14d') return { data: { ...base, total_executions: 1400 } }
      return realGet(url, config)
    })
    const pick = (label) => wrapper.findAll('[role="radio"]').find((b) => b.text() === label).trigger('click')
    await pick('30d')
    await pick('14d')
    await flushPromises()
    expect(t(wrapper, 'overview-activity-readout').text()).toMatch(/^1400\s*runs/)
    release30() // 30d answers last
    await flushPromises()
    expect(t(wrapper, 'overview-activity-readout').text()).toMatch(/^1400\s*runs/)
    expect(t(wrapper, 'overview-tile-finished').text()).toContain('14d')
    axios.get.mockImplementation(realGet)
  })
})

describe('a brand-new agent versus a quiet window', () => {
  it('an agent with past work and nothing in this window is not told it never ran', async () => {
    routes['/api/executions'] = [{ id: 'old', status: 'success', message: 'Earlier', started_at: new Date(Date.now() - 20 * 86400000).toISOString(), duration_ms: 1000, triggered_by: 'manual' }]
    const wrapper = await mountPanel()
    expect(t(wrapper, 'overview-activity-ghost').exists()).toBe(true)
    expect(t(wrapper, 'overview-activity-readout').text()).toMatch(/No runs in this window/)
    expect(wrapper.text()).not.toMatch(/Nothing has run yet|after the first run|first run/)
  })
})

describe('inside the cached agent page', () => {
  it('stops polling when the page is put away and resumes when it comes back', async () => {
    vi.useFakeTimers()
    routes = activeAgent()
    const Page = defineComponent({ name: 'Page', setup: () => () => h(OverviewPanel, { agent: { name: NAME, status: 'running' } }) })
    const Elsewhere = defineComponent({ name: 'Elsewhere', setup: () => () => h('div') })
    const Host = defineComponent({
      props: { on: { type: Boolean, default: true } },
      setup: (props) => () => h(KeepAlive, null, () => h(props.on ? Page : Elsewhere)),
    })
    const wrapper = mount(Host, { global: { stubs: { 'router-link': { template: '<a><slot /></a>' } } } })
    mounted.push(wrapper)
    await flushPromises()
    const polls = () => axios.get.mock.calls.filter(([url]) => url === '/api/executions/stats').length
    expect(polls()).toBe(1)
    await vi.advanceTimersByTimeAsync(30000)
    expect(polls()).toBe(2)

    await wrapper.setProps({ on: false }) // the agent page is cached, not destroyed
    await vi.advanceTimersByTimeAsync(90000)
    expect(polls()).toBe(2)

    await wrapper.setProps({ on: true })
    await nextTick()
    await flushPromises()
    expect(polls()).toBe(3) // refreshed on return
    await vi.advanceTimersByTimeAsync(30000)
    expect(polls()).toBe(4) // and polling again
    const before = t(wrapper, 'overview-now-state').text()
    await vi.advanceTimersByTimeAsync(5000)
    expect(t(wrapper, 'overview-now-state').text()).not.toBe(before) // the clock is running
  })
})

describe('sections on their own', () => {
  it('health: an admin is offered the switch, anyone else is told who has it', async () => {
    const base = { state: 'ready', health: null, historyLoaded: true, checks: [], sync: null }
    const stubs = { 'router-link': { template: '<a data-testid="turn-on"><slot /></a>' } }
    const admin = mount(OverviewHealth, { props: { ...base, isAdmin: true }, global: { stubs } })
    expect(t(admin, 'turn-on').text()).toBe('Turn monitoring on in Operations')
    const member = mount(OverviewHealth, { props: { ...base, isAdmin: false }, global: { stubs } })
    expect(t(member, 'turn-on').exists()).toBe(false)
    expect(t(member, 'overview-health-off').text()).toMatch(/An admin can turn monitoring on/)
  })

  it('health: a current reading with no history yet is not "no health checks"', () => {
    const wrapper = mount(OverviewHealth, {
      props: { state: 'ready', historyLoaded: true, checks: [], sync: null, health: { aggregate_status: 'healthy', uptime_percent_24h: 100, avg_latency_24h_ms: 17 } },
      global: { stubs: { 'router-link': true } },
    })
    expect(t(wrapper, 'overview-health-uptime').text()).toBe('100%')
    expect(t(wrapper, 'overview-health-off').exists()).toBe(false)
  })

  it('health: alarm states are shown beside the verdict', () => {
    const wrapper = mount(OverviewHealth, {
      props: {
        state: 'ready', historyLoaded: true, checks: [], sync: null,
        health: { aggregate_status: 'unhealthy', circuit_breaker: { open: true }, docker: { oom_killed: true, restart_count: 3 } },
      },
      global: { stubs: { 'router-link': true } },
    })
    expect(t(wrapper, 'overview-health-verdict').text()).toBe('Unhealthy')
    expect(wrapper.text()).toMatch(/Circuit open/)
    expect(wrapper.text()).toMatch(/Out of memory/)
  })

  it('health: a history read that failed does not claim there are no checks', () => {
    const wrapper = mount(OverviewHealth, {
      props: { state: 'ready', health: null, historyLoaded: false, historyFailed: true, checks: [], sync: null },
      global: { stubs: { 'router-link': true } },
    })
    expect(t(wrapper, 'overview-health-off').exists()).toBe(false)
    expect(t(wrapper, 'overview-health-history-failed').exists()).toBe(true)
  })

  it('tile: nothing is drawn in the chart zone while the first read is out', () => {
    const wrapper = mount(OverviewMeasureTile, { props: { state: 'loading', label: 'Finished', value: '90%', series: [1, 2] } })
    expect(t(wrapper, 'overview-tile-value').text()).toBe('—')
    expect(t(wrapper, 'overview-tile-line').exists()).toBe(false)
    expect(t(wrapper, 'overview-tile-ghost').exists()).toBe(false)
  })
})

it('reads through the store once per source on open, and admins are read from the auth store', async () => {
  routes = activeAgent()
  const auth = useAuthStore()
  auth.user = { role: 'admin' }
  routes['/history'] = { checks: [] }
  routes[`/api/monitoring/agents/${NAME}`] = { agent_name: NAME, aggregate_status: 'unknown' }
  const wrapper = await mountPanel()
  const count = (suffix) => axios.get.mock.calls.filter(([url]) => url.endsWith(suffix)).length
  for (const suffix of ['/analytics', '/schedules/analytics-summary', '/schedules', '/api/executions', '/api/executions/stats', '/history', '/git/sync-state', '/metrics']) {
    expect(count(suffix), suffix).toBe(1)
  }
  expect(t(wrapper, 'overview-health-off').text()).toMatch(/Turn monitoring on in Operations/)
})
