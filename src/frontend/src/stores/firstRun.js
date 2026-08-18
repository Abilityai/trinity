/**
 * First-run front-desk store (ent#319, epic ent#54).
 *
 * Backs the "Start here" card: is this still a seed-only install for the
 * calling user, and which seeded agent should the "Show me" door open.
 *
 * Why a server read rather than counting agents in the browser: before ent#124
 * a fresh install had zero agents, so the client could see freshness directly.
 * Seeding made that permanently false — an out-of-the-box install now comes up
 * with Cornelius and the bundled system's agents already running — and the seed
 * is deployed under the admin account, so nothing client-side can tell a seeded
 * agent from one the operator built. The backend derives the seeded names from
 * the manifest actually in force; the client just asks.
 *
 * Dismissal is per-browser localStorage, matching the existing onboarding
 * wizard's `trinity_onboarding_dismissed` key. This is an ambient, non-gating
 * nudge — the same reason ent#238's checklist persists dismissal server-side
 * (an explicit AC there) does not apply here, and a per-user table for a card
 * that stops appearing the moment you create an agent would be more machinery
 * than the behaviour is worth.
 */
import { defineStore } from 'pinia'
import api from '../api'

export const FRONT_DESK_DISMISSED_KEY = 'trinity_front_desk_dismissed'

export const useFirstRunStore = defineStore('firstRun', {
  state: () => ({
    loaded: false,
    loading: false,
    firstRun: false,
    seededAgents: [],
    ownAgentCount: 0,
    demoAgent: null,
    dismissed: localStorage.getItem(FRONT_DESK_DISMISSED_KEY) === '1',
  }),

  getters: {
    /**
     * The card shows only on a seed-only install the user hasn't waved away,
     * AND only when something was actually seeded.
     *
     * That last clause keeps the two first-run surfaces from stacking. On a
     * truly empty install (seeding disabled via TRINITY_DEFAULT_SYSTEM_MANIFEST,
     * or an older install) the ent#52 wizard still auto-opens exactly as it
     * always has — unchanged behaviour, and a modal over a card offering the
     * same door would be worse than either alone. The front desk exists for the
     * case the wizard cannot reach: a fleet is running, and none of it is yours.
     *
     * `loaded` is part of the condition so it never flashes in before the answer
     * arrives — on an established fleet the honest render is nothing at all,
     * including during the fetch.
     */
    visible: (state) =>
      state.loaded && state.firstRun && state.seededAgents.length > 0 && !state.dismissed,
  },

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
        // card that appears over a mature fleet is noise nobody asked for.
        this.firstRun = false
        console.warn('[firstRun] state unavailable:', e?.message || e)
      } finally {
        this.loading = false
        this.loaded = true
      }
    },

    dismiss() {
      this.dismissed = true
      try {
        localStorage.setItem(FRONT_DESK_DISMISSED_KEY, '1')
      } catch (e) {
        // Private mode / quota — the card is hidden for this session either way.
        console.warn('[firstRun] could not persist dismissal:', e?.message || e)
      }
    },
  },
})
