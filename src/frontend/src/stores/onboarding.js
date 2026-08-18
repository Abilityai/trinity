/**
 * Activation-checklist store (ent#238, epic ent#54).
 *
 * Holds the calling user's four first-value milestones and their dismissal
 * state, served by the entitlement-gated enterprise endpoint
 * `GET /api/enterprise/onboarding/checklist`.
 *
 * Degrades to silence, deliberately: on an OSS build the router is never
 * mounted (404) and on an unentitled build it answers 403. Neither is an error
 * worth showing a user — `available` simply stays false and the component
 * renders nothing. That is what lets the checklist ship in the OSS bundle
 * (Invariant: enterprise Vue is gated by server-driven flags, not by a
 * separate build).
 *
 * Milestones are never cached across sessions or optimistically ticked — every
 * read is derived server-side from verified rows, so a user who deletes the
 * agent behind a milestone sees it untick. Honest completion is the point.
 */
import { defineStore } from 'pinia'
import api from '../api'

export const useOnboardingStore = defineStore('onboarding', {
  state: () => ({
    loaded: false,
    available: false,   // false on OSS/unentitled (404/403) — render nothing
    loading: false,
    items: [],
    completedCount: 0,
    totalCount: 0,
    complete: false,
    dismissed: false,
  }),

  getters: {
    /**
     * Show only when there is something left to do. A complete checklist hides
     * itself — the reward for finishing is that it stops asking (ent#238: never
     * a mandatory tour, never a permanent nag).
     */
    visible: (state) =>
      state.available && !state.dismissed && !state.complete && state.items.length > 0,
  },

  actions: {
    _apply(data) {
      this.items = Array.isArray(data?.items) ? data.items : []
      this.completedCount = data?.completed_count ?? 0
      this.totalCount = data?.total_count ?? this.items.length
      this.complete = !!data?.complete
      this.dismissed = !!data?.dismissed
      this.available = true
    },

    async fetchChecklist(force = false) {
      if (this.loaded && !force) return
      this.loading = true
      try {
        const r = await api.get('/api/enterprise/onboarding/checklist')
        this._apply(r.data)
      } catch (e) {
        // 404 = OSS build (router never mounted); 403 = mounted-but-unentitled.
        // Anything else is a real failure, but the checklist is ambient guidance
        // — it must never surface an error banner over the app it decorates.
        this.available = false
        if (![403, 404].includes(e?.response?.status)) {
          console.warn('[onboarding] checklist unavailable:', e?.message || e)
        }
      } finally {
        this.loading = false
        this.loaded = true
      }
    },

    async dismiss() {
      // Hide first, persist second: dismissal must feel instant, and a failed
      // write costs the user nothing worse than seeing the checklist again next
      // load. (The reverse order makes a dismissal feel broken on a slow link.)
      this.dismissed = true
      try {
        const r = await api.post('/api/enterprise/onboarding/checklist/dismiss')
        this._apply(r.data)
      } catch (e) {
        console.warn('[onboarding] dismiss failed:', e?.message || e)
      }
    },

    async restore() {
      try {
        const r = await api.delete('/api/enterprise/onboarding/checklist/dismiss')
        this._apply(r.data)
      } catch (e) {
        console.warn('[onboarding] restore failed:', e?.message || e)
      }
    },
  },
})
