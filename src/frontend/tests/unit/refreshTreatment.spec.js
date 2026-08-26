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
      expect(src, name).toContain('ScanlineReveal :loading="awaitingFirstLoad"')
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
      expect(src, name).toMatch(/<div v-if="awaitingFirstLoad"[^>]*aria-hidden/)
      // ...and the branch that used to be first must now be an else-if, or the
      // placeholder is inert.
      expect(src, name).toContain('v-else-if="loadFailed"')
    }
  })

  it('does not let the placeholder swallow "Agent Not Running"', () => {
    // Review of this PR: `viewState` ignores `loading` deliberately, so on a
    // STOPPED agent — where onMounted takes the `else { loading = false }`
    // branch and never fetches — the data ref stays null and `firstLoad` is
    // true for the entire life of the mount. Gated on that alone, the
    // placeholder arm wins forever, the not-running arm below it is never
    // evaluated, and the operator gets a blank dimmed panel under a track
    // stuck at :loading. Reachable on the default path: the tab is gated on
    // the DB-backed /exists probe, not on run state.
    //
    // The rule, not the spelling: whatever gates the placeholder must also
    // consider run state, and the primitive must take the same gate or it
    // animates behind the not-running copy.
    for (const [name, src] of [['MetricsPanel', metrics], ['DashboardPanel', dashboard]]) {
      expect(src, name).toMatch(
        /const awaitingFirstLoad = computed\(\(\) => firstLoad\.value && props\.agentStatus === 'running'\)/,
      )
      expect(src, name).not.toMatch(/<div v-if="firstLoad"[^>]*aria-hidden/)
      expect(src, name).not.toContain('ScanlineReveal :loading="firstLoad"')
      // The not-running arm still exists and is still downstream of the two
      // request-shaped arms.
      expect(src, name).toContain(`v-else-if="agentStatus !== 'running'"`)
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

  it('lets go of the error when the agent stops', () => {
    // Review finding: with the request-shaped arms now ahead of it, a failed
    // FIRST load outranks "Agent Not Running" — and nothing cleared
    // `loadError` on the way to stopped. So an agent that 502'd while booting
    // kept showing "Couldn't load the dashboard" with a Retry that can only
    // fail again, where the not-running copy belongs.
    for (const [name, src] of [['MetricsPanel', metrics], ['DashboardPanel', dashboard]]) {
      expect(src, name).toMatch(/stopRefresh\(\)[\s\S]{0,700}loadError\.value = ''/)
    }
  })

  it('does not play the celebratory wipe over an empty state', () => {
    // `viewState`'s `count` defaults to 1, so `state` was 'ready' for every
    // successful response including an empty one — against ScanlineReveal's
    // documented contract.
    expect(dashboard).toMatch(/count: dashboardData\.value\?\.has_dashboard \? 1 : 0/)
    expect(metrics).toMatch(/count: metricsData\.value\?\.has_metrics \? 1 : 0/)
  })

  it('never calls an old reading "just now" in the stale banner', () => {
    // The relative helper's only reactive dependency is `lastUpdated`, which a
    // FAILED refresh does not change — so it kept serving its cached string for
    // the whole outage, which is stale-as-fresh in the one line that exists to
    // deny it. "Updated …" keeps the relative form; the BANNER must not.
    //
    // Pinned as the RULE, not as the sentence: the banner now comes from the
    // shared `staleBannerMessage`, which prints an absolute time by
    // construction, and an assertion on the old wording failed for a change
    // that made the guarantee stronger. What has to stay true is that the
    // banner's own time is not the relative computed.
    expect(observability).toMatch(/v-if="isStale"[\s\S]{0,160}staleReadingTime/)
    expect(observability).not.toMatch(/v-if="isStale"[\s\S]{0,200}formatLastUpdated/)
    expect(observability).toMatch(/staleReadingTime = computed[\s\S]{0,200}staleBannerMessage\(/)
  })

  it('shows a failed FIRST fetch instead of a claim about the platform config', () => {
    // `enabled` sits at its `false` default after a dropped request, and that
    // arm used to be first — so a transport failure rendered as
    // "OTel not enabled. Set OTEL_ENABLED=1." The store recorded the real
    // reason and no arm could display it.
    const errArm = observability.indexOf('!observabilityStore.hasLoaded && observabilityStore.error')
    const cfgArm = observability.indexOf('!observabilityStore.enabled')
    expect(errArm).toBeGreaterThan(-1)
    expect(errArm).toBeLessThan(cfgArm)
    expect(observability).toContain("v-else-if=\"!observabilityStore.enabled\"")
  })

  it('does not claim a reading it never had', () => {
    // `hasLoaded` means "a request succeeded", not "we have data" — it is set
    // on `{enabled:false}` too, so a later transport failure asserted
    // "showing the reading from …" on an install that never had one.
    expect(observability).toMatch(/isStale = computed[\s\S]{0,600}hasData/)
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

describe('ent#253 review — the chip tells the truth about what it has', () => {
  const panel = read('../../src/components/ObservabilityPanel.vue')
  const store = read('../../src/stores/observability.js')

  it('derives hasLoaded from the payload, not from the status code', () => {
    // An OTel-disabled install answers 200 with `{enabled: false}` and no
    // metrics. Setting `hasLoaded` there made the next transport failure render
    // "showing the reading from …" when there had never been a reading.
    expect(store).toMatch(/this\.hasLoaded = this\.hasData/)
    expect(store).not.toMatch(/this\.hasLoaded = true/)
  })

  it('uses the shared banner helper rather than a local sentence', () => {
    // The local version interpolated a relative time whose only reactive
    // dependency was `lastUpdated` — so during an outage it kept serving
    // "just now", which is the stale-as-fresh claim the banner exists to stop.
    expect(panel).toMatch(/staleBannerMessage\(/)
    expect(panel).toMatch(/from '@\/utils\/loadingState'/)
  })

  it('announces the stale banner', () => {
    expect(panel).toMatch(/v-if="isStale" role="alert"/)
  })
})
