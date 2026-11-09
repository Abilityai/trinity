/**
 * Member onboarding progress card store (ent#1175).
 *
 * Fetches the five-step setup card for a member's agent and handles live
 * ``member_progress_update`` WebSocket events so the card updates without
 * a reload.  The store is keyed by agent name so the same card is shown
 * whether the member opens or refreshes — no skeleton flash on a cache hit.
 */
import { defineStore } from 'pinia'
import api from '../api'

export const usePortalProgressStore = defineStore('portalProgress', {
  state: () => ({
    // keyed by agent_name → { steps, all_done, active, loading, error }
    byAgent: {},
  }),

  actions: {
    async fetchProgress(agentName) {
      if (!agentName) return
      const existing = this.byAgent[agentName]
      // Skip if already loaded or in-flight
      if (existing && (existing.loading || existing.active !== undefined)) return

      this.byAgent[agentName] = { loading: true, steps: [], all_done: false, active: false, error: null }
      try {
        const { data } = await api.get(
          `/api/enterprise/client-portal/agents/${encodeURIComponent(agentName)}/progress`
        )
        this.byAgent[agentName] = {
          loading: false,
          steps: data.steps || [],
          all_done: !!data.all_done,
          active: !!data.active,
          error: null,
        }
      } catch (err) {
        // Soft fail — the enterprise module may not be present (OSS build) or
        // the network may be temporarily unavailable.
        this.byAgent[agentName] = { loading: false, steps: [], all_done: false, active: false, error: null }
      }
    },

    handleWebSocketEvent(event) {
      if (!event || event.type !== 'member_progress_update') return
      const agentName = event.agent_name
      if (!agentName || !Array.isArray(event.steps)) return
      const steps = event.steps
      const all_done = steps.every((s) => s.state === 'done')
      this.byAgent[agentName] = {
        loading: false,
        steps,
        all_done,
        active: true,
        error: null,
      }
    },

    clearProgress(agentName) {
      if (agentName && this.byAgent[agentName]) {
        delete this.byAgent[agentName]
      }
    },
  },

  getters: {
    progressFor: (state) => (agentName) => {
      return state.byAgent[agentName] || { loading: false, steps: [], all_done: false, active: false, error: null }
    },
  },
})
