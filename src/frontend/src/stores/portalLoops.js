import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import api from '../api'
import { activeLoops, isActive, startPayload } from '../components/portal/portalLoopUtils'

/**
 * Workspace loop state, scoped to a chat's PARTICIPANTS (ent#458).
 *
 * Deliberately a second store rather than a reuse of `stores/loops.js`, which
 * is agent-at-a-time by construction (`setAgent(name)` replaces the list) and
 * is owned by the operator LoopsPanel on Agent Detail. A room has several
 * participants at once, and the two surfaces are mounted independently — a
 * shared singleton would have each clearing the other's list on navigation.
 * Same split, same reason, as `skillsLibrary` vs `skills` (ent#263).
 *
 * Auth: the platform JWT only. ent#458 scopes this surface to the
 * platform-authenticated door (ent#78's auth-path invariant), so there is NO
 * new backend surface here — these are the existing operator endpoints, called
 * with the operator's own credential through the shared api.js client
 * (Invariant #7). An external client holding a portal token never mounts the
 * panel and could not reach these routes if it did.
 */
const POLL_INTERVAL_MS = 12000

export const usePortalLoopsStore = defineStore('portalLoops', () => {
  const loops = ref([])            // across all current participants
  const participants = ref([])
  const loading = ref(false)
  const starting = ref(false)
  const error = ref(null)
  const stoppingIds = ref([])
  const hasLoaded = ref(false)

  let _pollTimer = null
  let _fetchToken = 0

  const active = computed(() => activeLoops(loops.value))
  const hasActive = computed(() => active.value.length > 0)

  function _ensurePolling() {
    // Backstop for a missed WS terminal (AC #4: "live push degrades to poll,
    // never a stuck running"). Only while something is actually active, so an
    // idle Workspace tab issues no traffic at all.
    if (hasActive.value) {
      if (!_pollTimer) _pollTimer = setInterval(() => { fetchLoops() }, POLL_INTERVAL_MS)
    } else {
      stopPolling()
    }
  }

  function stopPolling() {
    if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null }
  }

  function setParticipants(names) {
    const next = (names || []).filter(Boolean)
    const changed = next.length !== participants.value.length ||
      next.some((n, i) => participants.value[i] !== n)
    if (!changed) return
    participants.value = next
    loops.value = []
    hasLoaded.value = false
    error.value = null
  }

  async function fetchLoops() {
    const names = participants.value
    if (!names.length) { loops.value = []; hasLoaded.value = true; return }
    const token = ++_fetchToken
    loading.value = true
    try {
      // One request per participant: the loops API is agent-scoped and this is
      // a chat's participants (typically 1-3), not the roster — the ent#2198
      // N+1 was per-agent across the WHOLE roster on every refresh. A batched
      // route would be a new backend surface for a fleet this small.
      const results = await Promise.allSettled(
        names.map((n) => api.get(`/api/agents/${encodeURIComponent(n)}/loops`, { params: { limit: 20 } }))
      )
      if (token !== _fetchToken) return          // a newer fetch already won
      const collected = []
      let anyOk = false
      for (const r of results) {
        if (r.status !== 'fulfilled') continue
        anyOk = true
        // `GET /api/agents/{name}/loops` answers a BARE list
        // (`response_model=List[LoopStatusResponse]`) — verified, not guessed.
        for (const row of (Array.isArray(r.value?.data) ? r.value.data : [])) collected.push(row)
      }
      if (anyOk) {
        loops.value = collected
        hasLoaded.value = true
        // A partial failure keeps what loaded rather than blanking the panel;
        // the error line says the view may be incomplete (#2382's rule: a
        // failed refresh keeps the data and says so).
        error.value = results.some((r) => r.status === 'rejected')
          ? 'Some agents could not be reached; this list may be incomplete.'
          : null
      } else {
        error.value = 'Could not load loops.'
      }
    } finally {
      if (token === _fetchToken) loading.value = false
      _ensurePolling()
    }
  }

  async function startLoop(agentName, form) {
    starting.value = true
    error.value = null
    try {
      const { data } = await api.post(
        `/api/agents/${encodeURIComponent(agentName)}/loops`, startPayload(form)
      )
      await fetchLoops()
      return { success: true, loopId: data?.loop_id }
    } catch (e) {
      return { success: false, error: e }
    } finally {
      starting.value = false
    }
  }

  async function stopLoop(loopId) {
    if (stoppingIds.value.includes(loopId)) return { success: true }
    stoppingIds.value = [...stoppingIds.value, loopId]
    try {
      await api.post(`/api/loops/${encodeURIComponent(loopId)}/stop`)
      // Stop is cooperative — the current iteration finishes — so the row does
      // NOT go terminal here. Refetch and let WS/poll carry the real end state
      // rather than showing a "stopped" the runtime has not reached.
      await fetchLoops()
      return { success: true }
    } catch (e) {
      return { success: false, error: e }
    } finally {
      stoppingIds.value = stoppingIds.value.filter((id) => id !== loopId)
    }
  }

  /**
   * Loop events are broadcast fleet-wide and unfiltered (#1106), so this store
   * filters to the chat's participants — exactly as the operator store filters
   * to the agent on screen. Both are routed from the one global handler, the
   * `reportsStore` + `fleetReportsStore` shape (#918).
   */
  function handleWebSocketEvent(data) {
    if (data?.type !== 'loop_run_completed' && data?.type !== 'loop_completed') return
    if (!participants.value.length) return
    // Review finding: the agent filter was `if (name && !includes(name)) return`,
    // so a payload with NO `agent_name` fell through and refetched every
    // participant — and a 100-run loop on any agent in the fleet emits 100 of
    // these events, each costing N `GET /loops?limit=20` here (every one of
    // which expands into per-loop run queries server-side). The operator
    // sibling (`stores/loops.js`) requires the field; an event that cannot say
    // which agent it belongs to cannot be shown to belong to this chat, so it
    // is not ours to act on.
    const name = data.agent_name
    if (!name || !participants.value.includes(name)) return
    fetchLoops()
  }

  function clear() {
    stopPolling()
    loops.value = []
    participants.value = []
    hasLoaded.value = false
    error.value = null
    _fetchToken++
  }

  return {
    loops, participants, loading, starting, error, stoppingIds, hasLoaded,
    active, hasActive,
    setParticipants, fetchLoops, startLoop, stopLoop, handleWebSocketEvent,
    stopPolling, clear,
    isActive,
  }
})
