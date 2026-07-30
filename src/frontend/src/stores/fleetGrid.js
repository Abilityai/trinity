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
import { DEPT_PREFIX, REPORTS_PREFIX } from '@/utils/gridOrg'

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

const LAYOUT_KEY = 'trinity-grid-layout-v1'
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
      _savedRaw = parsed && typeof parsed === 'object' ? parsed : {}
    } catch {
      _savedRaw = {}
    }
    return _savedRaw
  }

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
  function syncLayout(agentNames, systemNames = new Set()) {
    const saved = { ..._loadSavedRaw(), ...layout.value }
    if (Object.keys(saved).length === 0) {
      layout.value = defaultLayout(agentNames, systemNames)
      _persist()
      return
    }
    const { layout: healed, changed } = normalizeLayout(saved, agentNames)
    layout.value = healed
    if (changed) _persist()
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

  /** Replace the whole layout (org-overlay "Group by department" arrange). */
  function applyLayout(map) {
    layout.value = { ...map }
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

  // --- PROTOTYPE: org-overlay tag writes (departments + reporting lines) ---
  // Departments and reporting lines are plain agent tags (`dept:*`,
  // `reports-to:*`), written through the existing tags API. On success the
  // passed agent object's tags are patched in place so zones/lines/ribbons
  // re-derive without a fleet refetch.

  async function _addTag(agent, tag) {
    const res = await axios.post(
      `/api/agents/${encodeURIComponent(agent.name)}/tags/${encodeURIComponent(tag)}`,
      null,
      { headers: authStore.authHeader }
    )
    agent.tags = res.data.tags || []
  }

  async function _removeTag(agent, tag) {
    const res = await axios.delete(
      `/api/agents/${encodeURIComponent(agent.name)}/tags/${encodeURIComponent(tag)}`,
      { headers: authStore.authHeader }
    )
    agent.tags = res.data.tags || []
  }

  /** Drop-into-zone: set `dept-<dept>`, clearing any other dept-* tag. Plain
   *  tags (the legacy department fallback) are never touched. */
  async function assignDept(agent, dept) {
    try {
      const stale = (agent.tags || []).filter(
        (t) => t.startsWith(DEPT_PREFIX) && t !== `${DEPT_PREFIX}${dept}`
      )
      await _addTag(agent, `${DEPT_PREFIX}${dept}`)
      for (const t of stale) await _removeTag(agent, t)
    } catch (e) {
      console.error('assignDept failed', e)
    }
  }

  async function addReportsTo(agent, manager) {
    try {
      await _addTag(agent, `${REPORTS_PREFIX}${manager}`)
    } catch (e) {
      console.error('addReportsTo failed', e)
    }
  }

  async function removeReportsTo(agent, manager) {
    try {
      await _removeTag(agent, `${REPORTS_PREFIX}${manager}`)
    } catch (e) {
      console.error('removeReportsTo failed', e)
    }
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

  let _pollTimer = null

  function refreshBatchData() {
    return Promise.allSettled([
      fetchSyncHealth(),
      fetchOpQueuePending(),
      fetchSkillRunnerStatus(),
    ])
  }

  /** Visibility-aware slow poll; active only while the Grid view is mounted. */
  function startPolling() {
    stopPolling()
    refreshBatchData()
    _pollTimer = setInterval(() => {
      if (document.hidden) return
      refreshBatchData()
    }, BATCH_POLL_MS)
  }

  function stopPolling() {
    if (_pollTimer) {
      clearInterval(_pollTimer)
      _pollTimer = null
    }
  }

  /** Force refresh (header refresh button): batch data + visible tiles re-ask. */
  function forceRefresh(visibleNames = []) {
    refreshBatchData()
    for (const name of visibleNames) hydrate(name, { force: true })
  }

  return {
    layout,
    syncLayout,
    moveTile,
    tidy,
    resetLayout,
    ensurePlaced,
    applyLayout,
    moveTiles,
    assignDept,
    addReportsTo,
    removeReportsTo,
    analyticsState,
    analyticsFor,
    hydrate,
    syncHealth,
    opQueuePending,
    skillRunnerStatus,
    refreshBatchData,
    startPolling,
    stopPolling,
    forceRefresh,
  }
})
