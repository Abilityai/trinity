// @vitest-environment jsdom
/**
 * trinity-enterprise#74 — Schedules tab › Anomaly detection, MOUNTED.
 *
 * The watch card shows the agent's opt-in and remediation and saves a change
 * MERGED into the agent's stored row (a PUT replaces it); each schedule gets
 * one baseline badge (on / off / learning with its count; nothing when not
 * watched); an open incident shows its metrics, recent runs and excerpt, and
 * Close posts the resolve. A failed close lands in an InlineError.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { nextTick } from 'vue'

const http = vi.hoisted(() => ({ get: null, put: null, post: null }))
vi.mock('axios', () => {
  const instance = {
    get: vi.fn((...a) => http.get(...a)),
    put: vi.fn((...a) => http.put(...a)),
    post: vi.fn((...a) => http.post(...a)),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
  }
  return { default: { ...instance, create: vi.fn(() => instance) } }
})

import AnomalyWatchCard from '../../src/components/anomaly/AnomalyWatchCard.vue'
import AnomalyBadge from '../../src/components/anomaly/AnomalyBadge.vue'
import AnomalyIncident from '../../src/components/anomaly/AnomalyIncident.vue'
import { useAnomalyDetectionStore, schedulesUrl, settingsUrl, resolveUrl } from '../../src/stores/anomalyDetection'

const INCIDENT = {
  id: 'inc-1', schedule_id: 's1', execution_id: 'e9', status: 'open', occurrences: 3, ask_id: 'ask-1',
  created_at: '2026-10-13T07:00:00Z', remediation: null,
  metrics: [
    { metric: 'duration_ms', value: 600000, median: 61000, ratio: 9.8, direction: 'up' },
    { metric: 'tool_calls', value: 31, median: 4, ratio: 7.8, direction: 'up' },
  ],
  evidence: { trend: { duration_ms: 'rising', tool_calls: 'rising' }, excerpt: 'Retrying CRM export (attempt 7)' },
}
const VIEW = {
  effective: { enabled: true, remediation: 'none', remediate_after: 2, min_samples: 10, lookback_days: 14, resolve_after_normal: 3 },
  schedules: [
    { schedule_id: 's1', name: 'Weekly pipeline digest', state: 'off_baseline', baseline_runs: 12, incident: INCIDENT,
      recent_runs: [
        { id: '5d900000000aa', status: 'success', started_at: '2026-10-20T07:00:00Z', duration_ms: 600000, anomalous: true },
        { id: 'c03a000000051', status: 'success', started_at: '2026-10-06T07:00:00Z', duration_ms: 62000, anomalous: false },
      ] },
    { schedule_id: 's2', name: 'Morning lead sweep', state: 'on_baseline', baseline_runs: 14, incident: null, recent_runs: [] },
    { schedule_id: 's3', name: 'Monthly review', state: 'learning', baseline_runs: 4, incident: null, recent_runs: [] },
  ],
}
const STORED = { enabled: true, remediation: 'none', remediate_after: 2, metrics: { cost: { threshold: 5 } } }

async function flush() {
  for (let i = 0; i < 4; i++) { await nextTick(); await flushPromises() }
}

describe('anomaly panel', () => {
  let store
  beforeEach(async () => {
    setActivePinia(createPinia())
    http.get = vi.fn(async (url) => (url === schedulesUrl('scout') ? { data: VIEW } : { data: { agent: STORED } }))
    http.put = vi.fn(async () => ({ data: {} }))
    http.post = vi.fn(async () => ({ data: {} }))
    store = useAnomalyDetectionStore()
    await store.load('scout')
  })

  it('badges each schedule by its baseline state', () => {
    const badge = (id) => mount(AnomalyBadge, { props: { scheduleId: id } })
    expect(badge('s1').text()).toContain('Off baseline · 3 runs')
    expect(badge('s2').text()).toContain('On baseline')
    expect(badge('s3').text()).toContain('Learning · 4 of 10 runs')
    expect(badge('nope').html()).not.toContain('span')
  })

  it('a change saves merged into the stored row', async () => {
    const w = mount(AnomalyWatchCard, { props: { agentName: 'scout' } })
    await w.find('select[data-testid="anomaly-remediation"]').setValue('pause_schedule')
    await flush()
    expect(http.put).toHaveBeenCalledWith(settingsUrl('scout'),
      { ...STORED, remediation: 'pause_schedule' })
  })

  it('not watching hides the remediation control', async () => {
    store.view = { ...VIEW, effective: { ...VIEW.effective, enabled: false } }
    const w = mount(AnomalyWatchCard, { props: { agentName: 'scout' } })
    expect(w.find('[data-testid="anomaly-remediation"]').exists()).toBe(false)
  })

  it('an open incident shows metrics, runs, excerpt; close resolves it', async () => {
    const w = mount(AnomalyIncident, {
      props: { scheduleId: 's1' },
      global: { stubs: { 'router-link': { template: '<a><slot /></a>' } } },
    })
    const metrics = w.findAll('[data-testid="anomaly-metric"]')
    expect(metrics[0].text()).toContain('Duration')
    expect(metrics[0].text()).toContain('10m 00s')
    expect(metrics[0].text()).toContain('9.8×')
    expect(metrics[0].text()).toContain('rising')
    const runs = w.findAll('[data-testid="anomaly-run"]')
    expect(runs[0].text()).toContain('anomalous')
    expect(runs[1].text()).toContain('normal')
    expect(w.text()).toContain('Retrying CRM export')
    expect(w.find('[data-testid="anomaly-incident-foot"]').text()).toContain('Alert only')
    await w.find('[data-testid="anomaly-close"]').trigger('click')
    await flush()
    expect(http.post.mock.calls[0][0]).toBe(resolveUrl('inc-1'))
  })

  it('a failed close is an InlineError beside the control', async () => {
    http.post = vi.fn(async () => { throw Object.assign(new Error('x'), { response: { status: 409, data: { detail: { message: 'This incident is already resolved.' } } } }) })
    const w = mount(AnomalyIncident, {
      props: { scheduleId: 's1' },
      global: { stubs: { 'router-link': { template: '<a><slot /></a>' } } },
    })
    await w.find('[data-testid="anomaly-close"]').trigger('click')
    await flush()
    expect(w.text()).toContain('already resolved')
  })

  it('no incident renders nothing', () => {
    const w = mount(AnomalyIncident, { props: { scheduleId: 's2' } })
    expect(w.find('[data-testid="anomaly-incident-s2"]').exists()).toBe(false)
  })
})
