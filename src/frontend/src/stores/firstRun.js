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

// trinity-enterprise#1416: while `fresh_install` is not known (the read failed,
// errored or timed out, or the backend has not recorded its verdict yet) the
// read is asked again on this backing-off schedule, then left alone.
export const INSTALL_KIND_RETRY_MS = [2000, 6000, 18000]

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
    brainAgent: null,     // ent#1408: the seeded company brain (Cornelius), part of the product, not an example
    // trinity-enterprise#1416: the seeded agents that are the person's team from
    // the start (the Company Brain and the Chief of Staff). Still "seeded" (they
    // do not end the first run) but never an example kept off the screen.
    coreAgents: [],
    // trinity-enterprise#1416: this install's first seed ran under the version
    // that seeds the core pair; only then does the Grid open on its new
    // first-load arrangement (an existing install keeps the old placement).
    // null = NOT KNOWN (no answer yet, a failed read, or the backend has not
    // recorded its verdict): never read as "existing install" — the Grid then
    // shows the old placement without saving it, and the read is retried.
    freshInstall: null,
    installKindRetries: 0,
    installKindRetryPending: false,
    install: null,        // a short mark of this install: "setup closed" is remembered against it
    overlayOpen: false,   // the setup overlay has the screen (set by FirstRunOverlay)
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
        this.brainAgent = typeof r.data?.brain_agent === 'string' ? r.data.brain_agent : null
        this.coreAgents = Array.isArray(r.data?.core_agents) ? r.data.core_agents : []
        // A known kind is never reset by a re-read that does not know it
        // (null): only a true/false answer is applied.
        const fresh = r.data?.fresh_install
        if (fresh === true || fresh === false) this.freshInstall = fresh
        this.install = r.data?.install ?? null
      } catch (e) {
        // Fail toward "not first run": a missed nudge is a non-event, whereas a
        // prompt that appears over a mature fleet is noise nobody asked for.
        // `freshInstall` is left as it was: still not known (null) on a first
        // read, and a kind already known is never erased by a failed re-read.
        this.firstRun = false
        console.warn('[firstRun] state unavailable:', e?.message || e)
      } finally {
        this.loading = false
        this.loaded = true
      }
    },

    /**
     * trinity-enterprise#1416: ask again, backing off, while the install kind
     * is not known. Called by the Grid (the one consumer of `freshInstall`)
     * each time it shows an unsaved board for an unknown install; idempotent.
     * A retry applies only the install-kind fields, so no other first-run
     * consumer sees its state change under it.
     */
    ensureInstallKind() {
      if (this.freshInstall !== null || this.installKindRetryPending) return
      if (this.installKindRetries >= INSTALL_KIND_RETRY_MS.length) return
      const delay = INSTALL_KIND_RETRY_MS[this.installKindRetries]
      this.installKindRetries += 1
      this.installKindRetryPending = true
      setTimeout(async () => {
        try {
          const r = await api.get('/api/onboarding/first-run')
          const fresh = r.data?.fresh_install
          if (fresh === true || fresh === false) {
            if (Array.isArray(r.data?.core_agents)) this.coreAgents = r.data.core_agents
            this.freshInstall = fresh
          }
        } catch (e) {
          console.warn('[firstRun] install kind still unavailable:', e?.message || e)
        } finally {
          this.installKindRetryPending = false
        }
        this.ensureInstallKind()
      }, delay)
    },
  },
})
