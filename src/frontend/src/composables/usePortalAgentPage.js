/**
 * The agent's own page payload, shared by the two surfaces that split it
 * (ent#523).
 *
 * `PortalAgentPage.vue` used to be one component owning one fetch. ent#523
 * splits what it showed across two surfaces that are on screen at DIFFERENT
 * times — the top band (stats + the Activity chart, always visible above the
 * conversation) and the Agent-details panel (chats, what it can do, reports,
 * opened on demand into the rail's place). Both read the same
 * `GET /agents/{name}/page` payload.
 *
 * So the fetch moves here rather than being duplicated or lifted into
 * `Portal.vue`: two components issuing the same request would double every
 * agent page load, and hoisting it into the view would put a per-agent fetch in
 * a shell that is not always showing an agent.
 *
 * The module-level cache is deliberately keyed by `${agent}:${window}` and
 * CLEARED on any agent change (#2160): keyed-by-name alone once served one
 * agent's page under another's name during a fast switch, and a stale band is
 * the same defect one surface along.
 */
import { ref, computed, watch, onMounted } from 'vue'
import { useClientPortalStore } from '@/stores/clientPortal'

// Shared across every consumer in the SPA session — that is the point: the band
// mounts with the conversation and the details panel mounts later, and the
// second one should not re-pay for a payload the first already has.
//
// #2580: entries are `{ payload, fetchedAt }`, not the bare payload, because the
// cache now has to answer two questions and not one — "do we have this?" and
// "is it recent enough not to re-ask?". See `PAGE_FRESH_MS`.
const pageCache = new Map()

// #2580: how long a cached page is served without re-asking.
//
// The band lives inside `PortalConversation`, which is keyed on the thread, so
// every chat-tab switch destroys and rebuilds it. That produced two visible
// faults — a skeleton/scanline flash, and a fresh `GET /agents/{name}/page` per
// switch — for numbers that had not moved. This window removes the second; the
// synchronous seed below removes the first.
//
// 30s, and the number is a judgement rather than a measurement: an agent's
// 7-day task counts do not meaningfully change inside half a minute, and the
// band is not a live surface (in-flight work is the rail's Work tab, which has
// its own feed). Long enough that rapid tab-switching costs nothing; short
// enough that a person who sends a turn and looks up sees it counted.
//
// Deliberately NOT a "never refetch" cache: `reload` and an agent change both
// bypass it, and a plain remount after the window still refetches — the
// staleness ceiling stays bounded rather than becoming "whatever was true when
// this tab was opened".
export const PAGE_FRESH_MS = 30_000

/**
 * #2580 — should `load` go to the network?
 *
 * Exported and pure so the rule can actually be TESTED: `vitest.config.js` pins
 * `environment: 'node'` with no component-mount harness, so a decision left
 * inline in the composable is a decision no test can execute. This is the one
 * piece of `load` that is a judgement rather than plumbing.
 *
 * `force` (the ent#253 retry) always refetches; a miss always refetches; a hit
 * refetches only once it is older than the window. A malformed entry — no
 * `fetchedAt`, a non-numeric one, a clock that jumped backwards — is treated as
 * STALE rather than fresh: the failure mode of an unnecessary request is a
 * duplicate fetch, and of a wrongly-fresh one is a band that never updates
 * again for the life of the tab.
 */
export function shouldRefetchPage({ cached = null, now = Date.now(), force = false } = {}) {
  if (force) return true
  if (!cached) return true
  const at = cached.fetchedAt
  if (typeof at !== 'number' || !Number.isFinite(at)) return true
  const age = now - at
  if (age < 0) return true
  return age >= PAGE_FRESH_MS
}

export function clearPortalAgentPageCache() {
  pageCache.clear()
}

/**
 * @param {import('vue').Ref<string>} agentName
 * @param {import('vue').Ref<string>} timeWindow  one of '7d' | '14d' | '30d'
 */
export function usePortalAgentPage(agentName, timeWindow) {
  const store = useClientPortalStore()
  const page = ref(null)
  const loading = ref(false)
  const error = ref(null)
  // The VERDICT, separate from `loading` (a fetch in flight). #2540/#1927: a
  // skeleton gates on "no data yet", never on a request being open, or a
  // background refresh would blank a band that is already on screen.
  const loaded = ref(false)

  async function load({ force = false } = {}) {
    const name = agentName.value
    if (!name) return
    const key = `${name}:${timeWindow.value}`
    const cached = pageCache.get(key)
    if (cached) {
      page.value = cached.payload
      loaded.value = true
      // #2580: a cache hit inside the freshness window is the WHOLE answer — no
      // request at all. Without this the band re-fetched on every chat-tab
      // switch, which is the "or refetch" half of the defect: the payload was
      // already on screen, correct, and being re-asked for.
      // `force` is what `reload` passes, so the ent#253 retry button still
      // reaches the network on a failed refresh.
      if (!shouldRefetchPage({ cached, now: Date.now(), force })) return
    }
    loading.value = true
    error.value = null
    try {
      const fresh = await store.fetchAgentPage(name, timeWindow.value)
      // The agent may have changed while this was in flight; a late response
      // for the previous one must not paint over the current agent's band.
      if (agentName.value !== name) return
      pageCache.set(key, { payload: fresh, fetchedAt: Date.now() })
      page.value = fresh
      loaded.value = true
    } catch (e) {
      if (agentName.value !== name) return
      error.value = e?.response?.status === 404
        ? "You don't have access to this agent."
        : "Couldn't load this agent right now."
      // A failed refresh keeps the data it has (ent#253): `loaded` is not
      // reset, so the band stays rendered with the banner beside it rather
      // than collapsing back to a skeleton.
    } finally {
      if (agentName.value === name) loading.value = false
    }
  }

  watch(agentName, (next, prev) => {
    if (next === prev) return
    clearPortalAgentPageCache()
    page.value = null
    loaded.value = false
    load()
  })
  watch(timeWindow, () => load())

  // #2580: seed from the cache DURING SETUP, before the first paint.
  //
  // `onMounted(load)` alone is why a chat-tab switch flashed. `load` reads the
  // cache, but `onMounted` runs AFTER the first render — so a remounted band
  // painted one frame with `loaded === false`, which is the skeleton at the top
  // of the band and the scanline over the chart. The data was in memory the
  // whole time; only the ordering made it look absent.
  //
  // This is the #2540/#1927 rule applied one layer up: a placeholder means "no
  // data yet", and a remount holding a warm cache has data. Gating on the
  // VERDICT is not enough on its own if the verdict is computed too late.
  const seedKey = `${agentName.value}:${timeWindow.value}`
  const seed = agentName.value ? pageCache.get(seedKey) : null
  if (seed) {
    page.value = seed.payload
    loaded.value = true
  }
  onMounted(() => load())

  // `reload` forces past the freshness window: it is the ent#253 retry beside a
  // stale-data banner, and a retry that answered from cache would report success
  // without having re-asked anything.
  const reload = () => load({ force: true })

  return {
    page,
    loading,
    loaded,
    error,
    reload,
    stats: computed(() => page.value?.stats || { total_executions: 0, timeline: [] }),
    ratings: computed(() => page.value?.ratings || { up: 0, down: 0, total: 0, unavailable: false }),
    header: computed(() => page.value?.header || null),
    capabilities: computed(() => page.value?.capabilities || []),
    recentWork: computed(() => page.value?.recent_work || []),
  }
}
