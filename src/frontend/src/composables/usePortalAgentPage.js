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
const pageCache = new Map()

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

  async function load() {
    const name = agentName.value
    if (!name) return
    const key = `${name}:${timeWindow.value}`
    const cached = pageCache.get(key)
    if (cached) {
      page.value = cached
      loaded.value = true
    }
    loading.value = true
    error.value = null
    try {
      const fresh = await store.fetchAgentPage(name, timeWindow.value)
      // The agent may have changed while this was in flight; a late response
      // for the previous one must not paint over the current agent's band.
      if (agentName.value !== name) return
      pageCache.set(key, fresh)
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
  onMounted(load)

  return {
    page,
    loading,
    loaded,
    error,
    reload: load,
    stats: computed(() => page.value?.stats || { total_executions: 0, timeline: [] }),
    ratings: computed(() => page.value?.ratings || { up: 0, down: 0, total: 0, unavailable: false }),
    header: computed(() => page.value?.header || null),
    capabilities: computed(() => page.value?.capabilities || []),
    recentWork: computed(() => page.value?.recent_work || []),
  }
}
