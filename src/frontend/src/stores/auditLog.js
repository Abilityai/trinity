/**
 * Audit log dashboard store (#941).
 *
 * Domain state for `views/enterprise/Audit.vue` — the admin-facing
 * dashboard over the platform audit log. Kept separate from the
 * cross-cutting `enterprise.js` store (which only caches the
 * `enterprise_features` flag list) so domain concerns and entitlement
 * lookup don't get tangled.
 *
 * Endpoints consumed (all admin-only, public repo):
 *   GET  /api/audit-log                        — filtered + paginated list
 *   GET  /api/audit-log/{event_id}             — single entry detail
 *   GET  /api/audit-log/distinct/event-types   — filter dropdown values
 *   GET  /api/audit-log/distinct/actor-types   — filter dropdown values
 *   GET  /api/audit-log/stats                  — aggregate counts (#941 v2)
 *   POST /api/audit-log/verify                 — hash chain integrity (#941 v2)
 *   GET  /api/audit-log/export                 — CSV/JSON download (#941 v2)
 *
 * Default time window: last 24h (per #941 acceptance criteria).
 *
 * Backend endpoints are not enterprise-gated — they were shipped OSS
 * via SEC-001 / #20. Only the OSS-side dashboard route is
 * entitlement-gated (via the router guard's `requiresEntitlement: 'audit'`).
 */
import { defineStore } from 'pinia'
import axios from 'axios'
import { useAuthStore } from './auth'

const DEFAULT_LIMIT = 50

function isoMinusHours(hours) {
  const d = new Date(Date.now() - hours * 3600 * 1000)
  return d.toISOString()
}

function emptyFilters() {
  return {
    event_type: '',
    actor_type: '',
    actor_id: '',
    target_type: '',
    start_time: isoMinusHours(24),
    end_time: '',
  }
}

export const useAuditLogStore = defineStore('auditLog', {
  state: () => ({
    entries: [],
    total: 0,
    limit: DEFAULT_LIMIT,
    offset: 0,
    filters: emptyFilters(),
    selectedEntry: null,
    distinctEventTypes: [],
    distinctActorTypes: [],
    distinctLoaded: false,
    loading: false,
    detailLoading: false,
    error: '',
    // #941 v2 — dashboard expansion
    stats: null,              // { total, by_event_type: {...}, by_actor_type: {...} }
    statsLoading: false,
    verifyState: 'idle',      // idle | verifying | valid | invalid | error
    verifyResult: null,       // { checked, first_invalid_id?, range?: [start, end] }
    activePreset: '24h',      // '1h' | '24h' | '7d' | '30d' | 'all' | 'custom'
    exporting: false,
    // #941 v3 — heatmap (day-of-week × hour-of-day)
    heatmap: null,            // { cells: [{dow, hour, count}], total, max_count }
    heatmapLoading: false,
    // #941 v3.1 — GitHub-style per-day calendar
    calendar: null,           // { days: [{date, count}], total, max_count }
    calendarLoading: false,
  }),

  getters: {
    page: (state) => Math.floor(state.offset / state.limit) + 1,
    pageCount: (state) =>
      state.total === 0 ? 1 : Math.ceil(state.total / state.limit),
    hasNext: (state) => state.offset + state.limit < state.total,
    hasPrev: (state) => state.offset > 0,
    rangeLabel: (state) => {
      if (state.total === 0) return 'No entries'
      const start = state.offset + 1
      const end = Math.min(state.offset + state.entries.length, state.total)
      return `Showing ${start}–${end} of ${state.total}`
    },
    // #941 v2 — top entry from the stats aggregate, used by the
    // header tiles. Returns null when stats isn't loaded or empty.
    topEventType: (state) => {
      const m = state.stats?.by_event_type
      if (!m) return null
      const entries = Object.entries(m)
      if (entries.length === 0) return null
      const [key, count] = entries.reduce((best, cur) =>
        cur[1] > best[1] ? cur : best
      )
      return { key, count }
    },
    topActorType: (state) => {
      const m = state.stats?.by_actor_type
      if (!m) return null
      const entries = Object.entries(m)
      if (entries.length === 0) return null
      const [key, count] = entries.reduce((best, cur) =>
        cur[1] > best[1] ? cur : best
      )
      return { key, count }
    },
    timeWindowLabel: (state) => {
      const f = state.filters
      if (!f.start_time && !f.end_time) return 'All time'
      const start = f.start_time
        ? new Date(f.start_time).toISOString().replace('T', ' ').slice(0, 16) +
          ' UTC'
        : '—'
      const end = f.end_time
        ? new Date(f.end_time).toISOString().replace('T', ' ').slice(0, 16) +
          ' UTC'
        : 'now'
      return `${start} → ${end}`
    },
  },

  actions: {
    _params() {
      const f = this.filters
      const p = { limit: this.limit, offset: this.offset }
      for (const k of [
        'event_type',
        'actor_type',
        'actor_id',
        'target_type',
        'start_time',
        'end_time',
      ]) {
        if (f[k]) p[k] = f[k]
      }
      return p
    },

    async loadList() {
      const authStore = useAuthStore()
      if (!authStore.isAuthenticated) {
        this.entries = []
        this.total = 0
        return
      }
      this.loading = true
      this.error = ''
      try {
        const r = await axios.get('/api/audit-log', {
          headers: authStore.authHeader,
          params: this._params(),
        })
        this.entries = Array.isArray(r.data?.entries) ? r.data.entries : []
        this.total = Number(r.data?.total) || 0
      } catch (e) {
        this.entries = []
        this.total = 0
        this.error =
          e?.response?.data?.detail ||
          e?.message ||
          'Failed to load audit entries'
      } finally {
        this.loading = false
      }
    },

    async loadDetail(eventId) {
      if (!eventId) {
        this.selectedEntry = null
        return
      }
      const authStore = useAuthStore()
      this.detailLoading = true
      try {
        const r = await axios.get(
          `/api/audit-log/${encodeURIComponent(eventId)}`,
          { headers: authStore.authHeader }
        )
        if (r.data) this.selectedEntry = r.data
      } catch (e) {
        // Leave `selectedEntry` intact — the caller usually set it from
        // the in-list row before calling loadDetail (optimistic display).
        // Wiping it on a transient detail-fetch failure would hide the
        // row the user just clicked.
        this.error =
          e?.response?.data?.detail || e?.message || 'Failed to load detail'
      } finally {
        this.detailLoading = false
      }
    },

    async loadDistinct(force = false) {
      if (this.distinctLoaded && !force) return
      const authStore = useAuthStore()
      if (!authStore.isAuthenticated) return
      try {
        const [evts, actors] = await Promise.all([
          axios.get('/api/audit-log/distinct/event-types', {
            headers: authStore.authHeader,
          }),
          axios.get('/api/audit-log/distinct/actor-types', {
            headers: authStore.authHeader,
          }),
        ])
        this.distinctEventTypes = Array.isArray(evts.data) ? evts.data : []
        this.distinctActorTypes = Array.isArray(actors.data) ? actors.data : []
        this.distinctLoaded = true
      } catch (e) {
        // Distinct endpoints failing shouldn't break the dashboard —
        // fall back to free-text input (dropdowns render empty).
        this.distinctEventTypes = []
        this.distinctActorTypes = []
        this.distinctLoaded = true
      }
    },

    setFilter(key, value) {
      if (!(key in this.filters)) return
      this.filters[key] = value
      this.offset = 0
    },

    resetFilters() {
      this.filters = emptyFilters()
      this.offset = 0
      this.selectedEntry = null
      this.activePreset = '24h'
    },

    setPage(page) {
      const target = Math.max(1, Math.min(page, this.pageCount))
      this.offset = (target - 1) * this.limit
    },

    nextPage() {
      if (this.hasNext) this.offset += this.limit
    },

    prevPage() {
      if (this.hasPrev) this.offset = Math.max(0, this.offset - this.limit)
    },

    selectEntry(entry) {
      this.selectedEntry = entry
    },

    clearSelection() {
      this.selectedEntry = null
    },

    // ─────────────────────────────────────────────────────────────────────
    // #941 v2 — dashboard expansion
    // ─────────────────────────────────────────────────────────────────────

    /** Reload the day-of-week × hour-of-day heatmap over the current window. */
    async loadHeatmap() {
      const authStore = useAuthStore()
      if (!authStore.isAuthenticated) return
      this.heatmapLoading = true
      try {
        const params = {}
        if (this.filters.start_time) params.start_time = this.filters.start_time
        if (this.filters.end_time) params.end_time = this.filters.end_time
        // Honor active filter narrowing so the heatmap matches the table.
        if (this.filters.event_type) params.event_type = this.filters.event_type
        if (this.filters.actor_type) params.actor_type = this.filters.actor_type
        const r = await axios.get('/api/audit-log/heatmap', {
          headers: authStore.authHeader,
          params,
        })
        this.heatmap = r.data || null
      } catch (e) {
        this.heatmap = null
      } finally {
        this.heatmapLoading = false
      }
    },

    /** Reload the GitHub-style per-day calendar over the current window. */
    async loadCalendar() {
      const authStore = useAuthStore()
      if (!authStore.isAuthenticated) return
      this.calendarLoading = true
      try {
        const params = {}
        if (this.filters.start_time) params.start_time = this.filters.start_time
        if (this.filters.end_time) params.end_time = this.filters.end_time
        if (this.filters.event_type) params.event_type = this.filters.event_type
        if (this.filters.actor_type) params.actor_type = this.filters.actor_type
        const r = await axios.get('/api/audit-log/calendar', {
          headers: authStore.authHeader,
          params,
        })
        this.calendar = r.data || null
      } catch (e) {
        this.calendar = null
      } finally {
        this.calendarLoading = false
      }
    },

    /**
     * Narrow the filter to a single UTC day. Used by the calendar
     * heatmap click handler — clicking 2026-05-18 sets the window to
     * 2026-05-18T00:00:00Z..2026-05-18T23:59:59Z, demotes the preset
     * chip to 'custom', and reloads list + stats + heatmap + calendar
     * so the whole dashboard pivots together.
     */
    async drilldownToDay(dateIso) {
      if (!/^\d{4}-\d{2}-\d{2}$/.test(dateIso || '')) return
      this.filters.start_time = `${dateIso}T00:00:00Z`
      this.filters.end_time = `${dateIso}T23:59:59Z`
      this.activePreset = 'custom'
      this.offset = 0
      await Promise.all([
        this.loadList(),
        this.loadStats(),
        this.loadHeatmap(),
        this.loadCalendar(),
      ])
    },

    /** Reload the stats aggregate over the current time window. */
    async loadStats() {
      const authStore = useAuthStore()
      if (!authStore.isAuthenticated) return
      this.statsLoading = true
      try {
        const params = {}
        if (this.filters.start_time) params.start_time = this.filters.start_time
        if (this.filters.end_time) params.end_time = this.filters.end_time
        const r = await axios.get('/api/audit-log/stats', {
          headers: authStore.authHeader,
          params,
        })
        this.stats = r.data || null
      } catch (e) {
        // Stats failure shouldn't blow up the dashboard — keep stale data
        // or null. Pin the error so the tiles can render "—".
        this.stats = null
      } finally {
        this.statsLoading = false
      }
    },

    /**
     * Set a relative time window and reload list + stats.
     * @param {string} key one of '1h' | '24h' | '7d' | '30d' | 'all'
     */
    async applyTimePreset(key) {
      const hoursByKey = { '1h': 1, '24h': 24, '7d': 24 * 7, '30d': 24 * 30 }
      if (key === 'all') {
        this.filters.start_time = ''
        this.filters.end_time = ''
      } else if (hoursByKey[key]) {
        this.filters.start_time = isoMinusHours(hoursByKey[key])
        this.filters.end_time = ''
      } else {
        return
      }
      this.activePreset = key
      this.offset = 0
      await Promise.all([
        this.loadList(),
        this.loadStats(),
        this.loadHeatmap(),
        this.loadCalendar(),
      ])
    },

    /**
     * Drill-down click handler. Sets a single filter, resets paging, and
     * reloads list + stats. Leaves other filters intact (P3 — preserves
     * the user's filter context).
     */
    async drilldownFilter(key, value) {
      if (!(key in this.filters)) return
      this.filters[key] = value || ''
      this.offset = 0
      this.activePreset = 'custom'
      await Promise.all([
        this.loadList(),
        this.loadStats(),
        this.loadHeatmap(),
        this.loadCalendar(),
      ])
    },

    /**
     * Verify the hash chain over the currently-visible id range.
     *
     * Visible-id-range only — full-DB verify is for a compliance audit,
     * not dashboard load. The verify endpoint takes integer id bounds,
     * not event_id UUIDs, so we read `id` from the in-list rows. If the
     * range is empty (no entries), the verify is a trivial "valid 0".
     */
    async verifyChain() {
      const authStore = useAuthStore()
      if (!authStore.isAuthenticated) return
      if (this.entries.length === 0) {
        this.verifyState = 'valid'
        this.verifyResult = { checked: 0, range: null }
        return
      }
      const ids = this.entries.map((e) => Number(e.id)).filter((n) => !isNaN(n))
      if (ids.length === 0) {
        this.verifyState = 'error'
        this.verifyResult = null
        return
      }
      const startId = Math.min(...ids)
      const endId = Math.max(...ids)
      this.verifyState = 'verifying'
      try {
        const r = await axios.post(
          '/api/audit-log/verify',
          null,
          {
            headers: authStore.authHeader,
            params: { start_id: startId, end_id: endId },
          }
        )
        const data = r.data || {}
        this.verifyResult = {
          checked: Number(data.checked) || 0,
          first_invalid_id: data.first_invalid_id ?? null,
          range: [startId, endId],
        }
        this.verifyState = data.valid ? 'valid' : 'invalid'
      } catch (e) {
        this.verifyState = 'error'
        this.verifyResult = null
        this.error =
          e?.response?.data?.detail ||
          e?.message ||
          'Failed to verify hash chain'
      }
    },

    /**
     * Download the current filter view as CSV or JSON.
     *
     * Uses fetch + Blob + object-URL so we can attach the JWT header
     * (a plain <a href> can't carry an Authorization header). Backend
     * endpoint /api/audit-log/export requires start_time + end_time, so
     * we coerce the current filter — falling back to "last 24h → now"
     * if either bound is empty.
     */
    async downloadExport(format = 'json') {
      const authStore = useAuthStore()
      if (!authStore.isAuthenticated) return
      if (!['csv', 'json'].includes(format)) return
      this.exporting = true
      try {
        const params = { format }
        params.start_time = this.filters.start_time || isoMinusHours(24)
        params.end_time = this.filters.end_time || new Date().toISOString()
        const r = await axios.get('/api/audit-log/export', {
          headers: authStore.authHeader,
          params,
          responseType: format === 'csv' ? 'blob' : 'json',
        })

        // Build a Blob whether the response is text/csv or application/json.
        const blob =
          format === 'csv'
            ? r.data
            : new Blob([JSON.stringify(r.data, null, 2)], {
                type: 'application/json',
              })

        const ts = new Date().toISOString().replace(/[:.]/g, '-')
        const filename = `audit-log-${ts}.${format}`
        const url = window.URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url
        a.download = filename
        document.body.appendChild(a)
        a.click()
        a.remove()
        window.URL.revokeObjectURL(url)
      } catch (e) {
        this.error =
          e?.response?.data?.detail || e?.message || 'Export failed'
      } finally {
        this.exporting = false
      }
    },
  },
})
