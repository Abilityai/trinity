import { defineStore } from 'pinia'
import api from '../api'

// #471 — subscription usage observability reads (SUB-004 extension).
//
// Deliberately READ-scoped: the register/assign/delete writes stay with the
// Settings SubscriptionsPanel (moved verbatim in the #471 extraction; folding
// them in here is the #512 raw-fetch cleanup's job, not this feature's). New
// API surface goes through this store per Invariants #6/#7.
//
// `usageBySub` values are the extended SubscriptionUsage payload:
//   { window_5h, window_7d, agents, failure_events_24h, failure_events_by_kind,
//     rate_limited_now, source: "anthropic"|"observed", headroom|null }
// A failed per-subscription fetch stores { error: true } — a failed fetch is
// never rendered as "no usage" (honest status).
export const useSubscriptionsStore = defineStore('subscriptions', {
  state: () => ({
    usageBySub: {},        // subscription_id -> usage payload | { error: true }
    usageLoading: {},      // subscription_id -> bool
    breakdownBySub: {},    // subscription_id -> breakdown payload | { error: true }
    breakdownLoading: {},  // subscription_id -> bool
    refreshingHeadroom: {},// subscription_id -> bool (click probe in flight)
    headroomAutoRefresh: { enabled: true, refresh_seconds: 900, loaded: false },
    // ent#259 — the roster the Dashboard pressure tile renders. Settings fetches
    // the same list with a raw axios call it owns; this is the store-side read
    // the tile uses, so the Grid never reaches into a view's local state.
    subscriptions: [],
    listLoaded: false,     // latches true on first success, never back
    listError: false,      // this cycle only
  }),

  actions: {
    async fetchUsage(subscriptionId) {
      this.usageLoading = { ...this.usageLoading, [subscriptionId]: true }
      try {
        const { data } = await api.get(`/api/subscriptions/${subscriptionId}/usage`)
        this.usageBySub = { ...this.usageBySub, [subscriptionId]: data }
        return data
      } catch (e) {
        this.usageBySub = { ...this.usageBySub, [subscriptionId]: { error: true } }
        return null
      } finally {
        this.usageLoading = { ...this.usageLoading, [subscriptionId]: false }
      }
    },

    // Called once per Settings load for all visible cards; per-card isolation
    // (one 404/500 never blanks the section).
    async fetchUsageForAll(subscriptionIds) {
      await Promise.allSettled(subscriptionIds.map((id) => this.fetchUsage(id)))
    },

    /**
     * ent#259 — roster + usage for the Dashboard "Subscription pressure" tile.
     *
     * The operator-prescribed shape (ent#259, 2026-08-19): a small build on the
     * endpoints #471 already ships, rather than a third batched route. At the
     * real fleet size (one subscription per Claude account) the fan-out is a
     * handful of requests per poll — fewer than `fetchOpQueuePending`, which
     * already rides the same batch.
     *
     * Honest status, three distinguishable outcomes:
     *  - the LIST fetch fails      → `listError`, last-good roster kept
     *  - the list returns a body of the wrong shape → also a FAULT, never
     *    laundered into "no subscriptions" (the manufactured-empty class: a
     *    200 whose body changed shape would otherwise render as an empty state
     *    that reads "none configured")
     *  - a per-subscription usage fetch fails → `usageBySub[id] = {error:true}`,
     *    and that ROW renders unavailable while its siblings render normally
     *
     * Note this drives #471's ambient headroom refresh from the dashboard: a
     * read is the demand signal, floored server-side at one probe per 15 min
     * per subscription. That is the intended design (an unwatched instance
     * probes nothing) — but it does mean an open dashboard keeps headroom warm,
     * which is the point of the tile.
     */
    async fetchPressureData() {
      let ids = []
      try {
        const { data } = await api.get('/api/subscriptions')
        if (!Array.isArray(data)) {
          this.listError = true
          return
        }
        this.subscriptions = data
        this.listError = false
        this.listLoaded = true
        ids = data.map((s) => s.id).filter(Boolean)
      } catch (e) {
        // Keep the last-known roster: a failed poll is not "no subscriptions".
        this.listError = true
        return
      }
      if (ids.length) await this.fetchUsageForAll(ids)
    },

    async fetchBreakdown(subscriptionId) {
      this.breakdownLoading = { ...this.breakdownLoading, [subscriptionId]: true }
      try {
        const { data } = await api.get(`/api/subscriptions/${subscriptionId}/usage/breakdown`)
        this.breakdownBySub = { ...this.breakdownBySub, [subscriptionId]: data }
        return data
      } catch (e) {
        this.breakdownBySub = { ...this.breakdownBySub, [subscriptionId]: { error: true } }
        return null
      } finally {
        this.breakdownLoading = { ...this.breakdownLoading, [subscriptionId]: false }
      }
    },

    // Click-to-refresh: ONE probe (server floors re-clicks at 60s and then
    // serves the cached snapshot with its honest age). Returns the refreshed
    // usage payload and stores it.
    async refreshHeadroom(subscriptionId) {
      this.refreshingHeadroom = { ...this.refreshingHeadroom, [subscriptionId]: true }
      try {
        const { data } = await api.post(`/api/subscriptions/${subscriptionId}/usage/refresh`)
        this.usageBySub = { ...this.usageBySub, [subscriptionId]: data }
        return data
      } catch (e) {
        return null
      } finally {
        this.refreshingHeadroom = { ...this.refreshingHeadroom, [subscriptionId]: false }
      }
    },

    async fetchHeadroomAutoRefresh() {
      try {
        const { data } = await api.get('/api/subscriptions/settings/headroom-auto-refresh')
        this.headroomAutoRefresh = { ...data, loaded: true }
      } catch (e) {
        // Older backend — leave the default; the toggle stays rendered with
        // its default-ON value and a failed save will surface its own error.
      }
    },

    async setHeadroomAutoRefresh(enabled) {
      await api.put(`/api/subscriptions/settings/headroom-auto-refresh?enabled=${enabled}`)
      this.headroomAutoRefresh = { ...this.headroomAutoRefresh, enabled }
    },

    // ent#434. Refetches rather than patching state locally: `weekly_alert`
    // is a DERIVED block (active / inactive_reason / escalation_pct all follow
    // from the threshold plus live conditions the client cannot see, such as
    // Redis reachability), so echoing back only the value we sent would leave
    // the panel asserting a status the server never computed.
    async setHeadroomAlertThreshold(thresholdPct) {
      await api.put(
        `/api/subscriptions/settings/headroom-alert-threshold?threshold_pct=${thresholdPct}`
      )
      await this.fetchHeadroomAutoRefresh()
    },
  },
})
