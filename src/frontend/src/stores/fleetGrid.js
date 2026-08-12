import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import axios from 'axios'
import { useAuthStore } from './auth'
import { useExecutionsStore } from './executions'
import {
  defaultLayout,
  normalizeLayout,
  nearestFreeCell,
  tidyLayout,
  occupantAt,
} from '@/utils/gridLayout'
import {
  isWidgetKey,
  enabledWidgetKeys,
  seedWidgetCells,
  widgetKey,
} from '@/utils/gridWidgets'
import { serverSkewMs } from '@/utils/timestamps'

/**
 * Fleet Grid store (trinity-enterprise#47) — owns the Dashboard Grid view's
 * per-user tile layout and its grid-scoped data hydration.
 *
 * Performance contract (first-class requirement of #47):
 *   - The grid renders skeleton tiles from the cheap `/api/agents` list the
 *     network store already holds; nothing here blocks first paint.
 *   - Per-tile analytics hydrate lazily (only tiles near the viewport ask),
 *     through a small concurrency-capped queue, into the executions store's
 *     existing `(agent, window)` cache — stale data is served instantly and
 *     refreshed in the background (stale-while-revalidate).
 *   - Grid-wide chip data (sync health, operator-queue pending) comes from
 *     two batch endpoints on a slow, visibility-aware poll that only runs
 *     while the Grid is mounted.
 */

// ent#325: layout v2 admits `widget:*` keys alongside agents. The key is
// bumped rather than reused so a v1 client and a v2 client on the same browser
// cannot fight over one blob — and the migration is a one-time COPY, leaving
// v1 in place, so downgrading is not a data-loss event.
const LAYOUT_KEY_V1 = 'trinity-grid-layout-v1'
const LAYOUT_KEY = 'trinity-grid-layout-v2'
// Sparse `{ widgetId: boolean }` OVERRIDE map — see gridWidgets.isWidgetEnabled.
// Its own key: the org overlay (#305) persists Zones/Lines under keys of its
// own and the tile prefs must not be entangled with either, so that "Reset
// tiles" cannot clobber an overlay toggle (ent#325 scope note).
const WIDGET_PREFS_KEY = 'trinity-grid-widgets-v1'
const ANALYTICS_WINDOW = '14d' // one fetch feeds Activity·14d + Context·7d (last 7 entries)
const ANALYTICS_STALE_MS = 5 * 60 * 1000
const HYDRATE_CONCURRENCY = 4
const BATCH_POLL_MS = 60000

export const useFleetGridStore = defineStore('fleetGrid', () => {
  const authStore = useAuthStore()
  const executionsStore = useExecutionsStore()

  // --- layout: agent → {c, r} on the unbounded lattice ---
  // `layout` holds ONLY the agents currently shown. `_savedRaw` is the full
  // persisted map, including agents hidden by an owner/tag filter — persisting
  // merges over it so toggling a filter never erases the positions of the
  // tiles it hides (filtering is indistinguishable from deletion here, so we
  // never treat absence as deletion; a truly deleted agent's entry just
  // lingers harmlessly in localStorage until Reset).
  const layout = ref({})
  let _savedRaw = null // lazily loaded full persisted map

  function _loadSavedRaw() {
    if (_savedRaw) return _savedRaw
    try {
      const raw = localStorage.getItem(LAYOUT_KEY)
      const parsed = raw ? JSON.parse(raw) : null
      if (parsed && typeof parsed === 'object') {
        _savedRaw = parsed
        return _savedRaw
      }
      // One-time v1 -> v2 migration. v1 holds agent keys only, so the copy is
      // straight; widgets are seeded afterwards by `syncLayout` into the band
      // above the fleet, which is why an existing constellation does not move.
      const legacy = localStorage.getItem(LAYOUT_KEY_V1)
      const legacyParsed = legacy ? JSON.parse(legacy) : null
      _savedRaw =
        legacyParsed && typeof legacyParsed === 'object' ? { ...legacyParsed } : {}
      if (Object.keys(_savedRaw).length) {
        try {
          localStorage.setItem(LAYOUT_KEY, JSON.stringify(_savedRaw))
        } catch {
          /* private mode */
        }
      }
    } catch {
      _savedRaw = {}
    }
    return _savedRaw
  }

  // --- info-tile show/hide preferences (ent#325) ---
  const widgetPrefs = ref({})
  try {
    const raw = localStorage.getItem(WIDGET_PREFS_KEY)
    const parsed = raw ? JSON.parse(raw) : null
    if (parsed && typeof parsed === 'object') widgetPrefs.value = parsed
  } catch {
    /* defaults */
  }

  function _persistWidgetPrefs() {
    try {
      localStorage.setItem(WIDGET_PREFS_KEY, JSON.stringify(widgetPrefs.value))
    } catch {
      /* private mode — session-local */
    }
  }

  function setWidgetEnabled(id, enabled) {
    widgetPrefs.value = { ...widgetPrefs.value, [id]: !!enabled }
    _persistWidgetPrefs()
    // Fill a newly-enabled data tile immediately rather than at the next 60s
    // tick. Guarded on `enabled` because `FleetGrid` passes
    // `$event.target.checked` straight through: an unguarded call would fire
    // the whole batch every time a box is UNCHECKED. It re-runs ALL batch
    // fetches rather than just the toggled tile's — accepted, since this is a
    // manual toggle and a per-widget fetch registry buys nothing.
    if (enabled) refreshBatchData()
  }

  /** Back to catalog defaults (an empty override map IS "defaults"). */
  function resetWidgets() {
    widgetPrefs.value = {}
    _persistWidgetPrefs()
  }

  const isAdmin = computed(() => authStore.user?.role === 'admin')

  /** Layout keys of the tiles that should be on the board for this viewer. */
  const activeWidgetKeys = computed(() =>
    enabledWidgetKeys(isAdmin.value, widgetPrefs.value)
  )

  function _persist() {
    _savedRaw = { ..._loadSavedRaw(), ...layout.value }
    try {
      localStorage.setItem(LAYOUT_KEY, JSON.stringify(_savedRaw))
    } catch {
      /* private mode — layout stays session-local */
    }
  }

  /**
   * Reconcile the layout with the live agent list (self-healing, #47 AC):
   * new agents take the first free cell near the origin, deleted agents
   * leave their gap, invalid/colliding saved positions resolve to the
   * nearest free cell. Falls back to the deterministic default layout when
   * nothing usable is saved.
   */
  function syncLayout(
    agentNames,
    systemNames = new Set(),
    originFor = null,
    isCellBlocked = null
  ) {
    const widgetKeys = activeWidgetKeys.value
    let saved = { ..._loadSavedRaw(), ...layout.value }
    if (Object.keys(saved).length === 0) {
      saved = defaultLayout(agentNames, systemNames)
    }
    // Seed any enabled tile that has never been placed into the band ABOVE the
    // fleet, consulting the caller's veto for cells inside a department frame
    // (#305 interlock D). Seeding BEFORE normalize means the tile arrives with
    // a real position rather than being treated as a homeless newcomer and
    // dropped at the board origin, on top of the agents.
    const unplaced = widgetKeys.filter((k) => !saved[k])
    if (unplaced.length) {
      saved = { ...saved, ...seedWidgetCells(saved, unplaced, isCellBlocked) }
    }
    const { layout: healed, changed } = normalizeLayout(
      saved,
      agentNames,
      originFor,
      widgetKeys
    )
    layout.value = healed
    if (changed || unplaced.length) _persist()
  }

  /** Drop a tile on `(c, r)`; an occupied target swaps with the dragged tile. */
  function moveTile(name, c, r) {
    const from = layout.value[name]
    if (!from) return
    const occ = occupantAt(layout.value, c, r, name)
    const next = { ...layout.value, [name]: { c, r } }
    if (occ) next[occ] = { c: from.c, r: from.r }
    layout.value = next
    _persist()
  }

  function tidy() {
    layout.value = tidyLayout(layout.value)
    _persist()
  }

  function resetLayout(agentNames, systemNames = new Set()) {
    try {
      localStorage.removeItem(LAYOUT_KEY)
      // v1 too, or the next _loadSavedRaw migrates the stale blob straight
      // back in and "Reset layout" appears not to have worked (ent#325).
      localStorage.removeItem(LAYOUT_KEY_V1)
    } catch {
      /* ignore */
    }
    _savedRaw = {} // reset drops hidden/stale entries too
    layout.value = defaultLayout(agentNames, systemNames)
    _persist()
  }

  /** Place an agent missing from the layout (e.g. created mid-session). */
  function ensurePlaced(name) {
    if (layout.value[name]) return
    layout.value = { ...layout.value, [name]: nearestFreeCell(layout.value, 0, 0) }
    _persist()
  }

  /**
   * Replace the layout (org-overlay "Group by department" / zone tidy).
   *
   * Belt to `gridOrg`'s braces (ent#325 interlock A): those helpers now carry
   * `widget:*` keys through themselves, but this was a blind wholesale replace
   * and a single caller forgetting to pass the current layout would silently
   * delete every info tile. Two independent guards, because one of them being
   * the only guard is exactly how the bug existed in the first place.
   */
  function applyLayout(map) {
    const carried = {}
    for (const [k, p] of Object.entries(layout.value)) {
      if (isWidgetKey(k) && !(k in map)) carried[k] = p
    }
    layout.value = { ...carried, ...map }
    _persist()
  }

  /** Translate a set of tiles by a cell delta (org-overlay block move). The
   *  caller validates the target cells are free; this just commits. */
  function moveTiles(names, dc, dr) {
    const next = { ...layout.value }
    for (const n of names) {
      const p = next[n]
      if (p) next[n] = { c: p.c + dc, r: p.r + dr }
    }
    layout.value = next
    _persist()
  }

  // --- per-tile analytics hydration (lazy, capped, stale-while-revalidate) ---
  const analyticsState = ref({}) // agent → 'loading' | 'done' | 'error'
  const _fetchedAt = {} // agent → epoch ms of last successful fetch (non-reactive)
  const _queue = []
  const _inFlightNames = new Set() // dedup guard incl. the stale-revalidate path
  let _inFlight = 0

  const analyticsFor = computed(() => (name) =>
    executionsStore.analyticsCache[`${name}:${ANALYTICS_WINDOW}`] || null
  )

  function _pump() {
    while (_inFlight < HYDRATE_CONCURRENCY && _queue.length > 0) {
      const { name, force } = _queue.shift()
      _inFlight++
      _inFlightNames.add(name)
      executionsStore
        .fetchAgentAnalytics(name, ANALYTICS_WINDOW, { force })
        .then(() => {
          _fetchedAt[name] = Date.now()
          analyticsState.value = { ...analyticsState.value, [name]: 'done' }
        })
        .catch(() => {
          // A failed per-agent fetch degrades that one tile only (#47 AC).
          // Keep any cached payload usable; mark error only when we have none.
          const has = executionsStore.analyticsCache[`${name}:${ANALYTICS_WINDOW}`]
          analyticsState.value = {
            ...analyticsState.value,
            [name]: has ? 'done' : 'error',
          }
        })
        .finally(() => {
          _inFlight--
          _inFlightNames.delete(name)
          _pump()
        })
    }
  }

  /**
   * Ask for an agent's analytics. Called by tiles when they come near the
   * viewport. Cached data renders immediately; a background refresh is
   * queued when the cache is stale.
   */
  function hydrate(name, { force = false } = {}) {
    const cached = executionsStore.analyticsCache[`${name}:${ANALYTICS_WINDOW}`]
    const stale = !_fetchedAt[name] || Date.now() - _fetchedAt[name] > ANALYTICS_STALE_MS
    if (cached && !stale && !force) {
      if (analyticsState.value[name] !== 'done') {
        analyticsState.value = { ...analyticsState.value, [name]: 'done' }
      }
      return
    }
    if (_inFlightNames.has(name) || _queue.some((q) => q.name === name)) return
    if (!cached) {
      analyticsState.value = { ...analyticsState.value, [name]: 'loading' }
    }
    _queue.push({ name, force: force || (!!cached && stale) })
    _pump()
  }

  // --- grid-wide chip data: sync health + operator-queue pending ---
  const syncHealth = ref({}) // agent → sync-health entry (#389 batch endpoint)
  const opQueuePending = ref({}) // agent → { count, oldestCreatedAt, hasApproval }

  async function fetchSyncHealth() {
    try {
      const res = await axios.get('/api/agents/sync-health', {
        headers: authStore.authHeader,
      })
      const map = {}
      for (const entry of res.data.agents || []) {
        map[entry.agent_name] = entry
      }
      syncHealth.value = map
    } catch {
      // chip data is non-critical — keep last known state
    }
  }

  async function fetchOpQueuePending() {
    try {
      const res = await axios.get('/api/operator-queue', {
        params: { status: 'pending', limit: 200 },
        headers: authStore.authHeader,
      })
      const map = {}
      for (const item of res.data.items || []) {
        const cur = map[item.agent_name] || {
          count: 0,
          oldestCreatedAt: null,
          hasApproval: false,
        }
        cur.count++
        if (!cur.oldestCreatedAt || item.created_at < cur.oldestCreatedAt) {
          cur.oldestCreatedAt = item.created_at
        }
        if (item.type === 'approval') cur.hasApproval = true
        map[item.agent_name] = cur
      }
      opQueuePending.value = map
    } catch {
      // non-critical
    }
  }

  // ent#139 — skill-runner status for the runner's tile variant. Rides the
  // existing visibility-aware batch refresh (no new polling loop), and the
  // endpoint is admin-only, so a non-admin simply keeps `null` and the tile
  // falls back to its standard rendering. Never surfaces an error: absent
  // status is a legitimate state (OSS build, unentitled, non-admin viewer).
  const skillRunnerStatus = ref(null)

  async function fetchSkillRunnerStatus() {
    try {
      const res = await axios.get('/api/enterprise/skill-runner/status', {
        headers: authStore.authHeader,
      })
      skillRunnerStatus.value = res.data
    } catch {
      skillRunnerStatus.value = null
    }
  }

  // ent#100 — "Recent failures" tile data. Two GETs, and they are deliberately
  // NOT collapsed into one state pair: a bounded page cannot yield a true 24h
  // total, so `/executions` supplies the rows and `/executions/stats` the
  // count. Each owns its own {data, loaded, error} triple, because sharing one
  // pair manufactures a false green in whichever direction is unguarded — the
  // green "No failures in 24h ✓" is a positive claim about the fleet and
  // requires BOTH to have succeeded on this cycle (`utils/executionFailure.js`
  // owns that rule; principle 15 / #1926).
  //
  // `loaded` flips true only after a SUCCESS and never back to false, so a
  // background-refresh failure keeps the last good rows on screen
  // (stale-while-revalidate) while `error` still records that this cycle failed.
  const FAILURE_ROWS = 4
  const recentFailures = ref([])
  const failuresListLoaded = ref(false)
  const failuresListError = ref(false)
  const failures24h = ref(null) // null = UNKNOWN; never inferred from rows.length
  const failuresStatsLoaded = ref(false)
  const failuresStatsError = ref(false)
  // Server-minus-browser clock offset, from the response's HTTP `Date` header
  // (no backend change, no extra request). Ages are otherwise measured between
  // two different clocks — see `utils/timestamps.js::serverSkewMs`.
  const serverClockSkewMs = ref(0)

  async function fetchRecentFailures() {
    try {
      const res = await axios.get('/api/executions', {
        params: { status: 'failed', hours: 24, limit: FAILURE_ROWS },
        headers: authStore.authHeader,
      })
      // NOTE: this endpoint answers a BARE JSON ARRAY
      // (`response_model=List[FleetExecutionSummary]`), unlike every other
      // fetcher in this store, which reads `res.data.agents` / `res.data.items`.
      // Reading `.items` here would yield a permanently empty tile with no error.
      //
      // A 200 whose body is NOT that array is a FAULT, not "zero failures".
      // Coercing it to `[]` while flipping `loaded` true and `error` false hands
      // `failuresTileState` the exact shape of a healthy empty fleet — the
      // manufactured-green class this tile exists to refuse, arriving through
      // the STORE, where the pure function's exhaustive sweep cannot see it.
      // The sibling `/stats` fetcher already degrades this way (an unreadable
      // count becomes `null`, i.e. not a confirmed all-clear); this matches it,
      // and keeps the last known-good rows on screen rather than blanking them.
      if (!Array.isArray(res.data)) {
        failuresListError.value = true
        return
      }
      recentFailures.value = res.data
      serverClockSkewMs.value = serverSkewMs(res.headers?.date, Date.now())
      failuresListLoaded.value = true
      failuresListError.value = false
    } catch {
      failuresListError.value = true
    }
  }

  async function fetchFailureStats() {
    try {
      const res = await axios.get('/api/executions/stats', {
        params: { hours: 24 },
        headers: authStore.authHeader,
      })
      const n = res.data?.failed_count
      failures24h.value = typeof n === 'number' ? n : null
      failuresStatsLoaded.value = true
      failuresStatsError.value = false
    } catch {
      failuresStatsError.value = true
    }
  }

  let _pollTimer = null
  let _visibilityHandler = null

  function refreshBatchData() {
    const tasks = [fetchSyncHealth(), fetchOpQueuePending(), fetchSkillRunnerStatus()]
    // Don't pay for a tile that is switched OFF. This reads `activeWidgetKeys`,
    // which resolves through `GRID_WIDGETS` — a registry populated by
    // `components/tiles/catalog.js`'s SIDE-EFFECT import from `FleetGrid.vue`.
    // It is correct here only because `startPolling()` is called from
    // `FleetGrid.onMounted`, i.e. after that import has run. Noted rather than
    // left as folklore.
    if (activeWidgetKeys.value.includes(widgetKey('recent-failures'))) {
      tasks.push(fetchRecentFailures(), fetchFailureStats())
    }
    return Promise.allSettled(tasks)
  }

  /** Visibility-aware slow poll; active only while the Grid view is mounted. */
  function startPolling() {
    stopPolling()
    refreshBatchData()
    _pollTimer = setInterval(() => {
      if (document.hidden) return
      refreshBatchData()
    }, BATCH_POLL_MS)
    // Refresh on return-to-tab rather than waiting out the interval. The poll
    // above skips every tick while hidden, so backgrounding the tab for half an
    // hour and coming back would otherwise show up to 60s of data that is 30
    // minutes old — tolerable for a chip, not for "No failures in 24h ✓", which
    // is a positive all-clear. An EVENT LISTENER, not a second timer: the Grid
    // still runs exactly one poll and one tick.
    if (typeof document !== 'undefined' && document.addEventListener) {
      _visibilityHandler = () => {
        if (!document.hidden) refreshBatchData()
      }
      document.addEventListener('visibilitychange', _visibilityHandler)
    }
  }

  function stopPolling() {
    if (_pollTimer) {
      clearInterval(_pollTimer)
      _pollTimer = null
    }
    if (_visibilityHandler) {
      document.removeEventListener('visibilitychange', _visibilityHandler)
      _visibilityHandler = null
    }
  }

  /** Force refresh (header refresh button): batch data + visible tiles re-ask. */
  function forceRefresh(visibleNames = []) {
    refreshBatchData()
    for (const name of visibleNames) hydrate(name, { force: true })
  }

  return {
    layout,
    widgetPrefs,
    activeWidgetKeys,
    isAdmin,
    setWidgetEnabled,
    resetWidgets,
    syncLayout,
    moveTile,
    tidy,
    resetLayout,
    ensurePlaced,
    applyLayout,
    moveTiles,
    analyticsState,
    analyticsFor,
    hydrate,
    syncHealth,
    opQueuePending,
    skillRunnerStatus,
    recentFailures,
    failuresListLoaded,
    failuresListError,
    failures24h,
    failuresStatsLoaded,
    failuresStatsError,
    serverClockSkewMs,
    refreshBatchData,
    startPolling,
    stopPolling,
    forceRefresh,
  }
})
