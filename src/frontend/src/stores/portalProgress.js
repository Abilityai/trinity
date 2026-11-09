/**
 * Member onboarding progress card store (ent#1175).
 *
 * Fetches the five-step setup card for a member's agent through the Workspace
 * client (`portalHttp` + the portal auth header, like every client-portal
 * read). A ``member_progress_update`` WebSocket event is a thin trigger — it
 * carries the agent name only (#918) — so the store refetches through the
 * access-controlled GET instead of trusting a broadcast payload.
 *
 * Keyed by agent name, so a remount reuses the last good card (no skeleton
 * flash). A failed read is NOT cached: the next mount, WS trigger or
 * ``resync_required`` reads again.
 */
import { defineStore } from 'pinia'
import { portalHttp, useClientPortalStore } from './clientPortal'

const EMPTY = Object.freeze({ loading: false, steps: [], all_done: false, active: false, error: null })

export const usePortalProgressStore = defineStore('portalProgress', {
  state: () => ({
    // keyed by agent_name → { steps, all_done, active, loading, error }
    byAgent: {},
  }),

  actions: {
    async fetchProgress(agentName, { force = false } = {}) {
      if (!agentName) return
      const existing = this.byAgent[agentName]
      if (existing?.loading) return
      // A good read is reused on remount; a failed one (error set) is retried.
      if (!force && existing?.loaded && !existing.error) return

      // Keep showing the previous card while re-reading — no layout jump.
      this.byAgent[agentName] = { ...(existing || EMPTY), loading: true }
      try {
        const { data } = await portalHttp.get(
          `/api/enterprise/client-portal/agents/${encodeURIComponent(agentName)}/progress`,
          { headers: useClientPortalStore().authHeader },
        )
        this.byAgent[agentName] = {
          loading: false,
          loaded: true,
          steps: data.steps || [],
          all_done: !!data.all_done,
          active: !!data.active,
          error: null,
        }
      } catch (err) {
        // Soft fail — keep whatever card was shown, mark the error so the
        // next trigger retries rather than serving a cached failure.
        this.byAgent[agentName] = { ...(existing || EMPTY), loading: false, error: err?.message || 'failed' }
      }
    },

    handleWebSocketEvent(event) {
      if (!event || event.type !== 'member_progress_update') return
      if (!event.agent_name) return
      return this.fetchProgress(event.agent_name, { force: true })
    },

    // The server trimmed our replay cursor: any update in the gap is lost, so
    // re-read every card this tab is holding.
    refetchAll() {
      return Promise.all(Object.keys(this.byAgent).map((name) => this.fetchProgress(name, { force: true })))
    },

    clearProgress(agentName) {
      if (agentName && this.byAgent[agentName]) {
        delete this.byAgent[agentName]
      }
    },
  },

  getters: {
    progressFor: (state) => (agentName) => state.byAgent[agentName] || EMPTY,
  },
})
