/**
 * First-run state store (ent#319, epic ent#54).
 *
 * Is this still a seed-only install for the calling user, and which seeded
 * agent should the "Show me" door open. Read by the first-run overlay
 * (ent#581), whose `agent` step absorbed the ent#319 front-desk card: the
 * `first_run` flag is both that step's auto-open signal and its completion.
 *
 * Why a server read rather than counting agents in the browser: before ent#124
 * a fresh install had zero agents, so the client could see freshness directly.
 * Seeding made that permanently false — an out-of-the-box install now comes up
 * with Cornelius and the bundled system's agents already running — and the seed
 * is deployed under the admin account, so nothing client-side can tell a seeded
 * agent from one the operator built. The backend derives the seeded names from
 * the manifest actually in force; the client just asks.
 */
import { defineStore } from 'pinia'
import api from '../api'

export const useFirstRunStore = defineStore('firstRun', {
  state: () => ({
    // `loaded` is a required term for every consumer: it is true only once the
    // answer has arrived, so nothing flashes in over an established fleet
    // during the fetch.
    loaded: false,
    loading: false,
    firstRun: false,
    seededAgents: [],
    ownAgentCount: 0,
    demoAgent: null,
  }),

  actions: {
    async fetchState(force = false) {
      if (this.loaded && !force) return
      this.loading = true
      try {
        const r = await api.get('/api/onboarding/first-run')
        this.firstRun = !!r.data?.first_run
        this.seededAgents = Array.isArray(r.data?.seeded_agents) ? r.data.seeded_agents : []
        this.ownAgentCount = r.data?.own_agent_count ?? 0
        this.demoAgent = r.data?.demo_agent ?? null
      } catch (e) {
        // Fail toward "not first run": a missed nudge is a non-event, whereas a
        // prompt that appears over a mature fleet is noise nobody asked for.
        this.firstRun = false
        console.warn('[firstRun] state unavailable:', e?.message || e)
      } finally {
        this.loading = false
        this.loaded = true
      }
    },
  },
})
