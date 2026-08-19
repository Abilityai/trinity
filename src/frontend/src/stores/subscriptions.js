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
  },
})
