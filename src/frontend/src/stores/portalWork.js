import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { useClientPortalStore } from './clientPortal'
import { WORK_POLL_MS, liveItems } from '../components/portal/portalWork'

/**
 * The Workspace rail's Work feed (trinity-enterprise#525): what a chat's
 * participants are doing now, and what they did recently — the executions
 * ledger, projected by `client_portal/work/` for the person who asked.
 *
 * The shape is `stores/portalRailFeeds.js`' — scoped by the shell
 * (`usePortalRailFeeds` is the ONE owner; this store never decides its own
 * participants), a fetch token that drops a stale response after a chat
 * switch, a trailing 2 s debounce on push, and NO timer while idle. What
 * differs is the poll: **12 s, only while something is live**. Live push
 * (`agent_activity`, loop events, the turn ending) degrades to that poll and
 * never to a stuck "running" (AC 1) — and a row the server marks `stale` is
 * not live, so a ghost row after a restart cannot keep the poll alive either.
 *
 * ONE request per refresh for all participants (`GET …/work?agents=a,b`),
 * through the client-portal store's fetcher — the portal auth header and the
 * roster gate are the server's. Platform door only: the shell feeds this
 * store only when the Work tab passes its door, and the route 404s a portal
 * token regardless.
 */
const PUSH_DEBOUNCE_MS = 2000
// ent#533: a stage advance is the one push worth reacting to quickly — the
// agent already wrote the file, and the whole point of the notice is that the
// card stops waiting for the 12 s poll.
const PIPELINE_PUSH_DEBOUNCE_MS = 500
// ...but never faster than this between two push-driven refreshes. The Work
// read is limited to 120/60 s PER VIEWER server-side and its 429 surfaces as
// visible error text on the card, so a pathological writer must be stopped
// here rather than there. It does not bind on the common path (an idle card,
// one advance), only under a burst — which is exactly when it matters.
const PUSH_MIN_GAP_MS = 1500

export const usePortalWorkStore = defineStore('portalWork', () => {
  const participants = ref([])
  const chatId = ref(null)
  const now = ref([])
  const earlier = ref([])
  const earlierTotal = ref(0)
  const earlierLimit = ref(30)
  const windowDays = ref(30)
  const hasLoaded = ref(false)
  const loading = ref(false)
  const error = ref(null)
  const fetchedAt = ref(null)     // ms — the instant `elapsed_seconds` was true
  const stoppingIds = ref([])
  const version = ref(0)

  let _fetchToken = 0
  let _debounceTimer = null
  let _debounceDue = 0
  let _lastRefreshStartedAt = 0
  let _pollTimer = null

  const live = computed(() => liveItems(now.value))
  const hasLive = computed(() => live.value.length > 0)

  function _sameList(a, b) {
    return a.length === b.length && a.every((n, i) => b[i] === n)
  }

  function _ensurePolling() {
    if (hasLive.value) {
      if (!_pollTimer) _pollTimer = setInterval(() => { refresh() }, WORK_POLL_MS)
    } else {
      stopPolling()
    }
  }

  function stopPolling() {
    if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null }
  }

  /** Scope to a chat: its participants and, in a 1:1, the open thread. */
  function setScope(names, nextChatId = null) {
    const next = (names || []).filter(Boolean)
    const chat = typeof nextChatId === 'string' && nextChatId ? nextChatId : null
    const sameNames = _sameList(next, participants.value)
    if (sameNames && chat === chatId.value) return
    if (!sameNames) {
      now.value = []
      earlier.value = []
      earlierTotal.value = 0
      hasLoaded.value = false
      error.value = null
      fetchedAt.value = null
      stopPolling()
    }
    participants.value = next
    chatId.value = chat
    _fetchToken++
  }

  /** Read the ledger for the current scope. Resolves after the store is updated; never throws. */
  async function refresh() {
    const names = participants.value
    if (!names.length) return
    const portal = useClientPortalStore()
    const token = ++_fetchToken
    loading.value = true
    try {
      const data = await portal.fetchWork(names, chatId.value)
      if (token !== _fetchToken) return
      now.value = Array.isArray(data?.now) ? data.now : []
      earlier.value = Array.isArray(data?.earlier) ? data.earlier : []
      earlierTotal.value = Number.isFinite(data?.earlier_total) ? data.earlier_total : earlier.value.length
      earlierLimit.value = Number.isFinite(data?.earlier_limit) ? data.earlier_limit : earlierLimit.value
      windowDays.value = Number.isFinite(data?.window_days) ? data.window_days : windowDays.value
      fetchedAt.value = Date.now()
      hasLoaded.value = true
      error.value = null
      version.value++
    } catch (e) {
      if (token !== _fetchToken) return
      // A failed refresh keeps what loaded and says so (#1926 / ent#253); a
      // failed FIRST load is `failed`, never `empty`.
      error.value = e?.response?.data?.detail?.message
        || (typeof e?.response?.data?.detail === 'string' ? e.response.data.detail : null)
        || "Couldn't load what's running."
    } finally {
      if (token === _fetchToken) loading.value = false
      _ensurePolling()
    }
  }

  /**
   * Trailing-edge debounce for push-driven refreshes, under two rules.
   *
   * **The earlier deadline wins.** A plain re-arm lets a later, slower push
   * postpone a pending faster one — a 2 s `agent_activity` landing 400 ms
   * after a 500 ms stage advance would drag the refetch out to 2.4 s, so the
   * store would defeat ent#533 by itself. `Portal.vue`'s own
   * `scheduleRefresh(1500)` benefits identically.
   *
   * **A floor under the burst.** Because deadlines only ever shorten, a
   * stream of pushes becomes a `delay`-length throttle rather than a
   * coalescing debounce. A push-driven refresh therefore never *starts*
   * within `PUSH_MIN_GAP_MS` of the previous one's start, which keeps a burst
   * clear of the server's per-viewer read limiter (120/60 s) whose 429 would
   * render as an error banner on the card.
   */
  function scheduleRefresh(delay = PUSH_DEBOUNCE_MS) {
    const now = Date.now()
    const due = Math.max(now + delay, _lastRefreshStartedAt + PUSH_MIN_GAP_MS)
    if (_debounceTimer && due >= _debounceDue) return
    if (_debounceTimer) clearTimeout(_debounceTimer)
    _debounceDue = due
    _debounceTimer = setTimeout(() => {
      _debounceTimer = null
      _debounceDue = 0
      _lastRefreshStartedAt = Date.now()
      refresh()
    }, Math.max(0, due - now))
  }

  /**
   * Stop one of the caller's OWN turns — the existing terminate route, whose
   * gates (`roster` + `started by this caller`) are what `can_stop` mirrors.
   * The row does NOT go terminal here: CANCELLED is the platform's CAS write
   * and the refetch carries it back.
   */
  async function stopItem(item) {
    if (!item || !item.can_stop || !item.agent_name) return { success: false }
    if (stoppingIds.value.includes(item.id)) return { success: true }
    stoppingIds.value = [...stoppingIds.value, item.id]
    const portal = useClientPortalStore()
    try {
      await portal.cancelPortalTurn(item.agent_name, item.id)
      await refresh()
      return { success: true }
    } catch (e) {
      // A 404 is the lost race (the row went terminal), not a refusal.
      if (e?.response?.status === 404) { await refresh(); return { success: true } }
      return { success: false, error: e }
    } finally {
      stoppingIds.value = stoppingIds.value.filter((id) => id !== item.id)
    }
  }

  /**
   * Platform sessions: the fleet-wide `/ws`. `agent_activity` for a
   * participant — STARTED as well as terminal, so *Now* cold-starts when a
   * schedule or a loop begins in an idle chat (review E1) — and the loop
   * progress events. Thin triggers; the data comes back through the
   * access-controlled read (#918). An event that cannot name its agent is not
   * ours to act on.
   */
  function handleWebSocketEvent(data) {
    if (!data || typeof data !== 'object') return
    if (!participants.value.length) return
    const name = data.agent_name
    if (!name || !participants.value.includes(name)) return
    // ent#533: the agent wrote a new pipeline-state file. Same thin-trigger
    // contract — ids and a stage, nothing read from the payload; the steps
    // come back through the access-controlled read, just sooner.
    if (data.type === 'pipeline_state_changed') {
      scheduleRefresh(PIPELINE_PUSH_DEBOUNCE_MS)
      return
    }
    const isLoop = data.type === 'loop_run_completed' || data.type === 'loop_completed'
    const isActivity = data.type === 'agent_activity'
    if (isLoop || isActivity) scheduleRefresh()
  }

  function clear() {
    if (_debounceTimer) { clearTimeout(_debounceTimer); _debounceTimer = null }
    _debounceDue = 0
    _lastRefreshStartedAt = 0
    stopPolling()
    participants.value = []
    chatId.value = null
    now.value = []
    earlier.value = []
    earlierTotal.value = 0
    hasLoaded.value = false
    loading.value = false
    error.value = null
    fetchedAt.value = null
    stoppingIds.value = []
    _fetchToken++
  }

  return {
    participants, chatId, now, earlier, earlierTotal, earlierLimit, windowDays,
    hasLoaded, loading, error, fetchedAt, stoppingIds, version,
    live, hasLive,
    setScope, refresh, scheduleRefresh, stopItem, handleWebSocketEvent, stopPolling, clear,
  }
})
