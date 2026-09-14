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
 * #2492: `MetricsPanel.vue` and `ObservabilityPanel.vue` were deleted as
 * unreferenced (no importer anywhere in the tree) — their source assertions
 * left with them. `DashboardPanel` is the live surface this spec still pins,
 * and the store half stays: `stores/observability.js` is where the honesty of
 * `hasLoaded` / `available` was fixed.
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
const dashboard = read('../../src/components/DashboardPanel.vue')

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

describe('the fixed surfaces (what only source can answer)', () => {
  it('no longer gates rendered content on the in-flight flag', () => {
    for (const [name, src] of [['DashboardPanel', dashboard]]) {
      expect(src, name).not.toContain('<div v-if="loading" class="flex items-center justify-center py-8">')
      expect(src, name).toContain('ScanlineReveal :loading="awaitingFirstLoad"')
    }
  })

  it('renders no terminal branch while the first load is still running', () => {
    // Caught in review, live: ScanlineReveal clips its slot only during the
    // REVEAL — during the loading phase the slot renders normally under a
    // 50%-opacity track. Without a first arm, the chain falls through to the
    // empty state while the data ref is still null, so the panel spent the
    // whole first load making the very claim this pass exists to remove.
    for (const [name, src] of [['DashboardPanel', dashboard]]) {
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
    for (const [name, src] of [['DashboardPanel', dashboard]]) {
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
    for (const [name, src] of [['DashboardPanel', dashboard]]) {
      expect(src, name).toContain("import { viewState, staleBannerMessage } from '../utils/loadingState'")
      expect(src, name).toMatch(/hasLoaded: \w+\.value !== null/)
    }
  })

  it('never fabricates an empty state out of a failed request', () => {
    // The exact regression: the catch used to overwrite the data ref with a
    // synthetic "no metrics" / "no dashboard" object.
    expect(dashboard).not.toMatch(/catch[\s\S]{0,200}dashboardData\.value = \{/)
    expect(dashboard).not.toContain("error: 'Failed to load dashboard'")
  })

  it('gives every refresh failure a user-visible home, not console.error', () => {
    for (const [name, src] of [['DashboardPanel', dashboard]]) {
      expect(src, name).toContain('<InlineError')
      expect(src, name).toContain('v-if="view.stale"')
      expect(src, name).not.toContain("console.error('Failed to load")
    }
  })

  it('offers a failed FIRST load a retry, distinct from the empty state', () => {
    for (const [name, src] of [['DashboardPanel', dashboard]]) {
      expect(src, name).toContain('<LoadFailed')
      // `v-else-if` since the first-load placeholder took the first arm; the
      // rule being pinned is that a failed FIRST load has its own branch, not
      // which keyword introduces it.
      expect(src, name).toMatch(/v-(else-)?if="loadFailed"/)
    }
  })

  it('clears the previous agent’s error when the agent changes', () => {
    // Otherwise agent B's first failure is reported with agent A's detail.
    for (const [name, src] of [['DashboardPanel', dashboard]]) {
      expect(src, name).toMatch(/\w+Data\.value = null\n\s*loadError\.value = ''/)
    }
  })

  it('lets go of the error when the agent stops', () => {
    // Review finding: with the request-shaped arms now ahead of it, a failed
    // FIRST load outranks "Agent Not Running" — and nothing cleared
    // `loadError` on the way to stopped. So an agent that 502'd while booting
    // kept showing "Couldn't load the dashboard" with a Retry that can only
    // fail again, where the not-running copy belongs.
    for (const [name, src] of [['DashboardPanel', dashboard]]) {
      expect(src, name).toMatch(/stopRefresh\(\)[\s\S]{0,700}loadError\.value = ''/)
    }
  })

  it('does not play the celebratory wipe over an empty state', () => {
    // `viewState`'s `count` defaults to 1, so `state` was 'ready' for every
    // successful response including an empty one — against ScanlineReveal's
    // documented contract.
    expect(dashboard).toMatch(/count: dashboardData\.value\?\.has_dashboard \? 1 : 0/)
  })

  it('drops the bespoke spinners the pass replaced', () => {
    // Both panels' full-panel spinners are gone; the primitive carries
    // prefers-reduced-motion so the AC is satisfied by construction.
    for (const [name, src] of [['DashboardPanel', dashboard]]) {
      expect(src, name).not.toContain('animate-spin rounded-full h-8 w-8')
    }
  })
})

describe('ent#253 re-review — an error must not outlive its request', () => {
  const dash = read('../../src/components/DashboardPanel.vue')
    const store = read('../../src/stores/observability.js')

  it.each([['DashboardPanel', dash]])(
    '%s drops a failure that landed after the agent stopped', (_name, sfc) => {
      // The watcher clears `loadError` synchronously on the stop transition,
      // but the catch lands LATER and used to write unconditionally — so
      // stopping an agent mid-fetch reproduced the blocking symptom by
      // ordering alone, on exactly the wedged-agent case an operator hits.
      expect(sfc).toMatch(/catch \(error\) \{[\s\S]{0,1200}?agentStatus !== 'running'\) return[\s\S]{0,400}?loadError\.value = error/)
    })

  it('keeps hasLoaded monotonic, so the placeholder cannot re-arm each poll', () => {
    // `firstLoad` is `loading && !hasLoaded`. A flag derived from the payload
    // goes false again on every no-data poll, re-arming the first-load
    // placeholder every 60s — the "disturbs on every tick" gate this branch
    // deletes. The stale banner is guarded by `hasData` in the panel instead,
    // which is where the claim about data is actually made.
    expect(store).toMatch(/this\.hasLoaded = true/)
    expect(store).not.toMatch(/this\.hasLoaded = this\.hasData/)
  })
})
