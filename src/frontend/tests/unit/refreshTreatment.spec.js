/**
 * ent#253 — one refresh treatment across polled surfaces.
 *
 * #1927 established the rule and fixed four surfaces; the audit on ent#253
 * (issue comment, 2026-08-24) found three that still disturbed on every tick:
 * `MetricsPanel`, `DashboardPanel`, and `ObservabilityPanel` via the
 * observability store. All three shared a worse-than-cosmetic second defect —
 * a FAILED refresh replaced good data with a fabricated empty state, so one
 * transient error made a working agent read as "No Metrics Defined" / "no
 * dashboard" / "collector unavailable". That is a claim about the subject
 * manufactured out of a claim about the request (#1926).
 *
 * The store half is exercised for real (Pinia + a mocked transport), because
 * the store is where the honesty of `hasLoaded` / `available` is decided and a
 * source regex cannot prove a branch. The component half is source-asserted:
 * `vitest.config.js` is `environment: 'node'` with no mount harness, so a rule
 * living in a template has no other guard.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { setActivePinia, createPinia } from 'pinia'

vi.mock('axios', () => {
  const inst = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }
  return { default: inst }
})

import axios from 'axios'
import { useObservabilityStore } from '@/stores/observability'

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8')
const metrics = read('../../src/components/MetricsPanel.vue')
const dashboard = read('../../src/components/DashboardPanel.vue')
const observability = read('../../src/components/ObservabilityPanel.vue')

// Shaped like the real payload (`stores/observability.js` state defaults), so
// the assertions below are about behaviour rather than about a fixture nothing
// resembles.
const GOOD = {
  data: {
    enabled: true,
    available: true,
    metrics: { cost_by_model: { sonnet: 1.25 }, sessions: 3, commits: 2 },
    totals: { total_cost: 1.25, total_tokens: 4200, sessions: 3 },
  },
}

describe('observability store — a failed refresh does not retract the data', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('records that a fetch has succeeded, so the UI can gate on "no data yet"', async () => {
    const store = useObservabilityStore()
    expect(store.hasLoaded).toBe(false)
    axios.get.mockResolvedValueOnce(GOOD)
    await store.fetchMetrics()
    expect(store.hasLoaded).toBe(true)
    expect(store.refreshError).toBeNull()
    expect(store.loading).toBe(false)
  })

  it('keeps metrics, totals and availability when a LATER fetch fails', async () => {
    const store = useObservabilityStore()
    axios.get.mockResolvedValueOnce(GOOD)
    await store.fetchMetrics()

    axios.get.mockRejectedValueOnce(new Error('network down'))
    await store.fetchMetrics()

    // The numbers the collector last reported are still the last thing it
    // reported — a dropped GET does not unsay them.
    expect(store.metrics).toEqual(GOOD.data.metrics)
    expect(store.totals).toEqual(GOOD.data.totals)
    // And a transport failure is not the collector declaring itself down.
    expect(store.available).toBe(true)
    expect(store.hasLoaded).toBe(true)
    // But it IS reported, so nothing presents the stale reading as live.
    expect(store.refreshError).toBeTruthy()
  })

  it('separates a transport failure from the collector’s own message', async () => {
    const store = useObservabilityStore()
    axios.get.mockResolvedValueOnce({ data: { enabled: true, available: true, error: 'collector says: degraded', metrics: [], totals: {} } })
    await store.fetchMetrics()
    expect(store.error).toBe('collector says: degraded')
    expect(store.refreshError).toBeNull()
  })

  it('still fails honestly when the FIRST fetch fails — nothing to protect', async () => {
    const store = useObservabilityStore()
    axios.get.mockRejectedValueOnce(new Error('boom'))
    await store.fetchMetrics()
    expect(store.hasLoaded).toBe(false)
    expect(store.available).toBe(false)
    expect(store.error).toBeTruthy()
    expect(store.refreshError).toBeTruthy()
  })

  it('clears the stale flag once a refresh succeeds again', async () => {
    const store = useObservabilityStore()
    axios.get.mockResolvedValueOnce(GOOD)
    await store.fetchMetrics()
    axios.get.mockRejectedValueOnce(new Error('blip'))
    await store.fetchMetrics()
    expect(store.refreshError).toBeTruthy()
    axios.get.mockResolvedValueOnce(GOOD)
    await store.fetchMetrics()
    expect(store.refreshError).toBeNull()
  })
})

describe('the three fixed surfaces (what only source can answer)', () => {
  it('no longer gates rendered content on the in-flight flag', () => {
    for (const [name, src] of [['MetricsPanel', metrics], ['DashboardPanel', dashboard]]) {
      expect(src, name).not.toContain('<div v-if="loading" class="flex items-center justify-center py-8">')
      expect(src, name).toContain('ScanlineReveal :loading="firstLoad"')
    }
    // The floating chip keeps a placeholder rather than the primitive, but the
    // gate is the same rule: first load, never a poll.
    expect(observability).toContain('v-if="firstLoad"')
    expect(observability).not.toContain('v-if="observabilityStore.loading"')
  })

  it('renders no terminal branch while the first load is still running', () => {
    // Caught in review, live: ScanlineReveal clips its slot only during the
    // REVEAL — during the loading phase the slot renders normally under a
    // 50%-opacity track. Without a first arm, the chain falls through to the
    // empty state while the data ref is still null, so the panel spent the
    // whole first load making the very claim this pass exists to remove.
    for (const [name, src] of [['MetricsPanel', metrics], ['DashboardPanel', dashboard]]) {
      expect(src, name).toMatch(/<div v-if="firstLoad"[^>]*aria-hidden/)
      // ...and the branch that used to be first must now be an else-if, or the
      // placeholder is inert.
      expect(src, name).toContain('v-else-if="loadFailed"')
    }
  })

  it('derives the loading face from "has data", not from the fetch', () => {
    for (const [name, src] of [['MetricsPanel', metrics], ['DashboardPanel', dashboard]]) {
      expect(src, name).toContain("import { viewState, staleBannerMessage } from '../utils/loadingState'")
      expect(src, name).toMatch(/hasLoaded: \w+\.value !== null/)
    }
    expect(observability).toContain('!observabilityStore.hasLoaded')
  })

  it('never fabricates an empty state out of a failed request', () => {
    // The exact regression: the catch used to overwrite the data ref with a
    // synthetic "no metrics" / "no dashboard" object.
    expect(metrics).not.toMatch(/catch[\s\S]{0,200}metricsData\.value = \{/)
    expect(dashboard).not.toMatch(/catch[\s\S]{0,200}dashboardData\.value = \{/)
    expect(metrics).not.toContain("message: 'Failed to load metrics'")
    expect(dashboard).not.toContain("error: 'Failed to load dashboard'")
  })

  it('gives every refresh failure a user-visible home, not console.error', () => {
    for (const [name, src] of [['MetricsPanel', metrics], ['DashboardPanel', dashboard]]) {
      expect(src, name).toContain('<InlineError')
      expect(src, name).toContain('v-if="view.stale"')
      expect(src, name).not.toContain("console.error('Failed to load")
    }
    expect(observability).toContain('v-if="isStale"')
  })

  it('offers a failed FIRST load a retry, distinct from the empty state', () => {
    for (const [name, src] of [['MetricsPanel', metrics], ['DashboardPanel', dashboard]]) {
      expect(src, name).toContain('<LoadFailed')
      // `v-else-if` since the first-load placeholder took the first arm; the
      // rule being pinned is that a failed FIRST load has its own branch, not
      // which keyword introduces it.
      expect(src, name).toMatch(/v-(else-)?if="loadFailed"/)
    }
  })

  it('clears the previous agent’s error when the agent changes', () => {
    // Otherwise agent B's first failure is reported with agent A's detail.
    for (const [name, src] of [['MetricsPanel', metrics], ['DashboardPanel', dashboard]]) {
      expect(src, name).toMatch(/\w+Data\.value = null\n\s*loadError\.value = ''/)
    }
  })

  it('drops the bespoke spinners the pass replaced', () => {
    // Both panels' full-panel spinners are gone; the primitive carries
    // prefers-reduced-motion so the AC is satisfied by construction.
    for (const [name, src] of [['MetricsPanel', metrics], ['DashboardPanel', dashboard]]) {
      expect(src, name).not.toContain('animate-spin rounded-full h-8 w-8')
    }
    // ...and the chip's overlay spinner, which was also a raw-palette color.
    expect(observability).not.toContain('animate-spin h-5 w-5 text-blue-600')
  })
})
