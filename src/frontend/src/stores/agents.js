import { defineStore } from 'pinia'
import axios from 'axios'
import { useAuthStore } from './auth'
import { agentDisplayName } from '../utils/agentName'
import { dedupe } from '../utils/inflight'
import api from '../api'

// ent#260 composition contract: with the Agents page retired, the Dashboard's
// List panel (AgentListPanel.vue) runs on networkStore as its data spine and
// composes THIS store in for exactly two members — `sortBy`/`setSortBy`
// (session-lived sort state) and `syncHealth`/`fetchSyncHealth` (#389).
// `agents` stays warm via networkStore.fetchAgents' write-through (the #1643
// displayNameForSlug base fetch — gated: a quick-tag-FILTERED fetch never
// clobbers this full-fleet list) plus the utils/websocket.js handlers that
// merge agent_created / agent_label_changed rows in. The sort comparator
// itself lives in utils/agentSort.js (pure function — no store access).
export const useAgentsStore = defineStore('agents', {
  state: () => ({
    agents: [],
    selectedAgent: null,
    loading: false,
    error: null,
    // PERF-269: Stats moved to networkStore (single source of truth, no duplicate polling)
    sortBy: 'created_desc',  // Default sort order
    // Running toggle loading state per agent
    runningToggleLoading: {},  // Map of agent name -> boolean
    // #389 Sync health per agent (populated by fetchSyncHealth)
    syncHealth: {},  // Map of agent name -> { last_sync_status, consecutive_failures, behind_working, ... }
    subscriptionPressure: {}  // #471: agent name -> { auth_mode, subscription_name, failure_events_24h, rate_limited_now, ... }
  }),

  getters: {
    // #1643: slug → human display name, resolved off the loaded agents.
    // Operational payloads (executions, operator queue, monitoring) carry only
    // the slug; this is the single slug→display resolver so those dense surfaces
    // never grow a mutable presentation field of their own. It stays live via
    // the agent_label_changed WS handler that updates the cached agent. Unknown
    // slug (agent not loaded / already gone) → the slug itself.
    displayNameForSlug() {
      return (slug) => agentDisplayName(this.agents.find(a => a.name === slug) || slug)
    },
    // #1643: the agent object for a slug, or the bare slug when not loaded — feed
    // it to agentNameTooltip / hasDistinctLabel where a dense row keeps the slug
    // primary and shows the label on hover.
    agentRefForSlug() {
      return (slug) => this.agents.find(a => a.name === slug) || slug
    },
    // Filter out system agents for regular lists
    userAgents() {
      return this.agents.filter(agent => !agent.is_system)
    },
    // Get the system agent if it exists
    systemAgent() {
      return this.agents.find(agent => agent.is_system) || null
    },
    runningAgents() {
      return this.userAgents.filter(agent => agent.status === 'running')
    },
    stoppedAgents() {
      return this.userAgents.filter(agent => agent.status === 'stopped')
    }
    // ent#260: sortedAgents / sortedAgentsWithSystem / _getSortedAgents were
    // deleted with their sole consumer (views/Agents.vue). The comparator
    // moved to utils/agentSort.js (sortAgents), which the List panel calls
    // with networkStore.executionStats as a parameter.
  },

  actions: {
    async fetchAgents() {
      this.loading = true
      this.error = null
      try {
        const authStore = useAuthStore()
        const response = await axios.get('/api/agents', {
          headers: authStore.authHeader
        })
        this.agents = response.data
      } catch (error) {
        this.error = error.message
        console.error('Failed to fetch agents:', error)
      } finally {
        this.loading = false
      }
    },

    async fetchSyncHealth() {
      // #389: batch endpoint for dashboard sync-health dots.
      try {
        const authStore = useAuthStore()
        const response = await axios.get('/api/agents/sync-health', {
          headers: authStore.authHeader
        })
        const map = {}
        for (const entry of response.data.agents || []) {
          map[entry.agent_name] = entry
        }
        this.syncHealth = map
      } catch (error) {
        // Silent — sync health is advisory; don't block the dashboard.
        console.warn('Failed to fetch sync health:', error.message)
      }
    },

    async fetchSubscriptionPressure() {
      // #471: batch endpoint for subscription-pressure badges. Mirrors the
      // sync-health discipline (AgentListPanel reads THIS store; the grid's
      // fleetGrid store carries its own copy — two stores, one endpoint, the
      // established chassis pattern). Last-known-good kept on error: a failed
      // fetch is never "no pressure".
      try {
        const authStore = useAuthStore()
        const response = await axios.get('/api/agents/subscription-pressure', {
          headers: authStore.authHeader
        })
        const map = {}
        for (const entry of response.data.agents || []) {
          map[entry.agent_name] = entry
        }
        this.subscriptionPressure = map
      } catch (error) {
        console.warn('Failed to fetch subscription pressure:', error.message)
      }
    },

    // #2198: JOINED, not skipped. `onMounted` and `onActivated` both fire on the
    // first mount of a KeepAlive'd AgentDetail (App.vue) and both await this —
    // measured as two requests with an identical timestamp. A joiner must still
    // receive the agent object, so `fleetGrid`'s in-flight *skip* is not usable
    // here (see utils/inflight.js). The entry clears in `finally`, so
    // `waitForAgentStatus`'s sequential polling loop is unaffected.
    async fetchAgent(name) {
      return dedupe(`agent:${name}`, () => this._fetchAgentUncached(name))
    },

    async _fetchAgentUncached(name) {
      this.loading = true
      this.error = null
      try {
        const authStore = useAuthStore()
        const response = await axios.get(`/api/agents/${name}`, {
          headers: authStore.authHeader
        })
        // #526: the circuit-breaker block is embedded in the agent response
        // (no second round-trip). Derive the header-badge fields from it; the
        // block is null when dispatch breaking is off fleet-wide.
        const cb = response.data.circuit_breaker
        this.selectedAgent = {
          ...response.data,
          circuit_breaker_state: cb?.dispatch?.state || 'closed',
          circuit_open: !!cb?.open
        }
        return this.selectedAgent
      } catch (error) {
        // #1914: re-throw. Swallowing here returned `undefined` to the caller,
        // which is indistinguishable from a successful empty fetch — that is
        // how a 404 rendered AgentDetail as a blank page (neither the error
        // banner nor the agent body had a truthy condition). Callers that
        // genuinely tolerate a failure (the post-stop status poll in
        // AgentDetail) already wrap this in their own try/catch.
        this.error = error.message
        console.error('Failed to fetch agent:', error)
        throw error
      } finally {
        this.loading = false
      }
    },

    // #1205: per-agent custom instructions for public & channel chats
    async fetchPublicChannelPrompt(name) {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agents/${name}/public-prompt`, {
        headers: authStore.authHeader
      })
      return response.data.public_channel_system_prompt
    },

    async savePublicChannelPrompt(name, prompt) {
      const authStore = useAuthStore()
      const response = await axios.put(
        `/api/agents/${name}/public-prompt`,
        { public_channel_system_prompt: prompt },
        { headers: authStore.authHeader }
      )
      return response.data.public_channel_system_prompt
    },

    async createAgent(config) {
      this.loading = true
      this.error = null
      try {
        const authStore = useAuthStore()
        const response = await axios.post('/api/agents', config, {
          headers: authStore.authHeader
        })
        // Don't push here - WebSocket 'agent_created' event handles adding to list
        // This prevents duplicate entries from race conditions
        return response.data
      } catch (error) {
        this.error = error.response?.data?.detail || error.message
        throw error
      } finally {
        this.loading = false
      }
    },

    async deleteAgent(name) {
      this.loading = true
      this.error = null
      try {
        const authStore = useAuthStore()
        await axios.delete(`/api/agents/${name}`, {
          headers: authStore.authHeader
        })
        this.agents = this.agents.filter(agent => agent.name !== name)
      } catch (error) {
        this.error = error.response?.data?.detail || error.message
        throw error
      } finally {
        this.loading = false
      }
    },

    async startAgent(name) {
      try {
        const authStore = useAuthStore()
        const response = await axios.post(`/api/agents/${name}/start`, {}, {
          headers: authStore.authHeader
        })
        const agent = this.agents.find(a => a.name === name)
        if (agent) agent.status = 'running'
        return { success: true, message: response.data?.message || `Agent ${name} started` }
      } catch (error) {
        const message = error.response?.data?.detail || error.message || 'Failed to start agent'
        console.error('Start agent error:', message)
        throw new Error(message)
      }
    },

    async stopAgent(name) {
      try {
        const authStore = useAuthStore()
        const response = await axios.post(`/api/agents/${name}/stop`, {}, {
          headers: authStore.authHeader
        })
        const agent = this.agents.find(a => a.name === name)
        if (agent) agent.status = 'stopped'
        return { success: true, message: response.data?.message || `Agent ${name} stopped` }
      } catch (error) {
        const message = error.response?.data?.detail || error.message || 'Failed to stop agent'
        console.error('Stop agent error:', message)
        throw new Error(message)
      }
    },

    /**
     * Toggle agent running state (start/stop)
     * @param {string} name - Agent name
     * @returns {Promise<{success: boolean, status?: string, error?: string}>}
     */
    async toggleAgentRunning(name) {
      const agent = this.agents.find(a => a.name === name)
      if (!agent) return { success: false, error: 'Agent not found' }

      this.runningToggleLoading[name] = true

      try {
        const authStore = useAuthStore()
        if (agent.status === 'running') {
          await axios.post(`/api/agents/${name}/stop`, {}, {
            headers: authStore.authHeader
          })
          agent.status = 'stopped'
        } else {
          await axios.post(`/api/agents/${name}/start`, {}, {
            headers: authStore.authHeader
          })
          agent.status = 'running'
        }
        return { success: true, status: agent.status }
      } catch (error) {
        const message = error.response?.data?.detail || error.message || 'Failed to toggle agent'
        console.error('Toggle agent running error:', message)
        return { success: false, error: message }
      } finally {
        this.runningToggleLoading[name] = false
      }
    },

    /**
     * Check if an agent is in the process of toggling running state
     * @param {string} name - Agent name
     * @returns {boolean}
     */
    isTogglingRunning(name) {
      return this.runningToggleLoading[name] || false
    },

    async getAgentLogs(name, tail = 100) {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agents/${name}/logs?tail=${tail}`, {
        headers: authStore.authHeader
      })
      return response.data.logs
    },

    async sendChatMessage(name, message) {
      const authStore = useAuthStore()
      const response = await axios.post(`/api/agents/${name}/chat`,
        { message },
        { headers: authStore.authHeader }
      )
      return response.data
    },

    async getChatHistory(name) {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agents/${name}/chat/history`, {
        headers: authStore.authHeader
      })
      return response.data
    },

    async getSessionInfo(name) {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agents/${name}/chat/session`, {
        headers: authStore.authHeader
      })
      return response.data
    },

    async clearSession(name) {
      const authStore = useAuthStore()
      const response = await axios.delete(`/api/agents/${name}/chat/history`, {
        headers: authStore.authHeader
      })
      return response.data
    },

    // Simplified Credential System (CRED-002)
    async injectCredentials(name, files, filesB64 = {}) {
      const authStore = useAuthStore()
      const response = await axios.post(`/api/agents/${name}/credentials/inject`,
        { files, files_b64: filesB64 },
        { headers: authStore.authHeader }
      )
      return response.data
    },

    async exportCredentials(name) {
      const authStore = useAuthStore()
      const response = await axios.post(`/api/agents/${name}/credentials/export`,
        {},
        { headers: authStore.authHeader }
      )
      return response.data
    },

    async importCredentials(name) {
      const authStore = useAuthStore()
      const response = await axios.post(`/api/agents/${name}/credentials/import`,
        {},
        { headers: authStore.authHeader }
      )
      return response.data
    },

    // ent#127: written against `api.js` rather than the raw-axios idiom of its
    // neighbours above. `api.js` is the single client (Invariant #7) — it owns
    // the auth interceptor and the 401 -> login redirect — and the neighbours
    // predate it (`Library.vue` and `CreateAgentModal.vue` have already
    // migrated). Copying them would be citing Invariant #7 to violate it.
    async getCredentialRequirements(name) {
      const response = await api.get(`/api/agents/${name}/credential-requirements`)
      return response.data
    },

    async getCredentialStatus(name) {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agents/${name}/credentials/status`, {
        headers: authStore.authHeader
      })
      return response.data
    },

    async getAgentStats(name) {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agents/${name}/stats`, {
        headers: authStore.authHeader
      })
      return response.data
    },

    async getAgentTokenStats(name) {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agents/${name}/token-stats`, {
        headers: authStore.authHeader
      })
      return response.data
    },

    // ent#181: set or clear an agent's human-facing label. `label: null` clears
    // it and the agent renders under its slug again. Goes through the shared
    // axios client + auth interceptor (Invariant #7) — unlike the legacy slug
    // rename in AgentDetail.vue, which hand-rolls fetch + Authorization.
    async setAgentLabel(name, label) {
      const authStore = useAuthStore()
      const response = await axios.put(`/api/agents/${name}/label`, { label }, {
        headers: authStore.authHeader
      })
      // Keep the cached agent in step so every surface re-renders with the new
      // label without a refetch.
      const cached = this.agents.find(a => a.name === name)
      if (cached) cached.display_label = response.data.label
      return response.data
    },

    // #2198: three concurrent readers on one Agent Detail mount —
    // `checkBrainOrbCapability` from both lifecycle hooks, plus OverviewPanel's
    // sidecar batch (Overview is the default tab).
    async getAgentInfo(name) {
      return dedupe(`agentInfo:${name}`, async () => {
        const authStore = useAuthStore()
        const response = await axios.get(`/api/agents/${name}/info`, {
          headers: authStore.authHeader
        })
        return response.data
      })
    },

    // #668 — agent compatibility report. STATIC checks recompute live; pass
    // includeAi=true to force a fresh (cost-incurring) AI evaluation, otherwise
    // the last persisted AI verdicts are returned. The Overview panel fetches
    // STATIC-only first (instant), then AI.
    async getCompatibility(name, { includeAi = false } = {}) {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agents/${name}/compatibility`, {
        params: { include_ai: includeAi },
        headers: authStore.authHeader,
      })
      return response.data
    },

    // #668 — apply an auto-fix for a correctable (gitignore) check. Owner/admin.
    async fixCompatibilityIssue(name, checkId) {
      const authStore = useAuthStore()
      const response = await axios.post(
        `/api/agents/${name}/compatibility/fix`,
        { check_id: checkId },
        { headers: authStore.authHeader },
      )
      return response.data
    },

    async getAgentModel(name) {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agents/${name}/model`, {
        headers: authStore.authHeader
      })
      return response.data
    },

    async setAgentModel(name, model) {
      const authStore = useAuthStore()
      const response = await axios.put(`/api/agents/${name}/model`,
        { model: model || '' },
        { headers: authStore.authHeader }
      )
      return response.data
    },

    // Agent Sharing Actions
    async shareAgent(name, email) {
      const authStore = useAuthStore()
      const response = await axios.post(`/api/agents/${name}/share`,
        { email },
        { headers: authStore.authHeader }
      )
      return response.data
    },

    async unshareAgent(name, email) {
      const authStore = useAuthStore()
      await axios.delete(`/api/agents/${name}/share/${encodeURIComponent(email)}`, {
        headers: authStore.authHeader
      })
    },

    async getAgentShares(name) {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agents/${name}/shares`, {
        headers: authStore.authHeader
      })
      return response.data
    },

    // #17 Access tab: operator (Trinity-user) roster — allow-list emails resolved
    // against `users` (active operator vs pending invite).
    async getAgentAccess(name) {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agents/${name}/access`, {
        headers: authStore.authHeader
      })
      return response.data
    },

    // #1577: toggle the per-recipient allow_proactive flag on an agent_sharing
    // row (#321/#376). Owner/admin only; returns the persisted state.
    async setProactive(name, email, allow) {
      const authStore = useAuthStore()
      const response = await axios.put(
        `/api/agents/${name}/shares/proactive`,
        { email, allow_proactive: allow },
        { headers: authStore.authHeader }
      )
      return response.data
    },

    // Agent Permissions Actions (Phase 9.10)
    async getAgentPermissions(name) {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agents/${name}/permissions`, {
        headers: authStore.authHeader
      })
      return response.data
    },

    async setAgentPermissions(name, permittedAgents) {
      const authStore = useAuthStore()
      const response = await axios.put(`/api/agents/${name}/permissions`,
        { permitted_agents: permittedAgents },
        { headers: authStore.authHeader }
      )
      return response.data
    },

    async addAgentPermission(sourceAgent, targetAgent) {
      const authStore = useAuthStore()
      const response = await axios.post(
        `/api/agents/${sourceAgent}/permissions/${targetAgent}`,
        {},
        { headers: authStore.authHeader }
      )
      return response.data
    },

    async removeAgentPermission(sourceAgent, targetAgent) {
      const authStore = useAuthStore()
      const response = await axios.delete(
        `/api/agents/${sourceAgent}/permissions/${targetAgent}`,
        { headers: authStore.authHeader }
      )
      return response.data
    },

    // ent#84: one gated read for the fleet permissions matrix — returns both
    // axes (accessible, non-system agents) and every caller→target grant edge
    // among them with provenance (granted_by / granted_at). Entitlement-gated
    // enterprise endpoint (permissions_matrix); 404/403 in OSS/unentitled
    // builds — the Settings tab is hidden then, so it's never called there.
    async getPermissionsMatrix() {
      const authStore = useAuthStore()
      const response = await axios.get('/api/enterprise/permissions-matrix', {
        headers: authStore.authHeader
      })
      return {
        agents: response.data.agents || [],
        edges: response.data.edges || []
      }
    },

    // Session Activity Actions
    async getSessionActivity(name) {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agents/${name}/activity`, {
        headers: authStore.authHeader
      })
      return response.data
    },

    async getActivityDetail(name, toolId) {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agents/${name}/activity/${toolId}`, {
        headers: authStore.authHeader
      })
      return response.data
    },

    async clearSessionActivity(name) {
      const authStore = useAuthStore()
      const response = await axios.delete(`/api/agents/${name}/activity`, {
        headers: authStore.authHeader
      })
      return response.data
    },

    // Git Sync Actions (Phase 7)
    async getGitStatus(name) {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agents/${name}/git/status`, {
        headers: authStore.authHeader
      })
      return response.data
    },

    async getGitConfig(name) {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agents/${name}/git/config`, {
        headers: authStore.authHeader
      })
      return response.data
    },

    async syncToGithub(name, { message = null, paths = null, strategy = 'normal' } = {}) {
      const authStore = useAuthStore()
      const payload = { strategy }
      if (message) payload.message = message
      if (paths) payload.paths = paths
      const response = await axios.post(`/api/agents/${name}/git/sync`,
        payload,
        { headers: authStore.authHeader }
      )
      return response.data
    },

    async pullFromGithub(name, { strategy = 'clean' } = {}) {
      const authStore = useAuthStore()
      const response = await axios.post(`/api/agents/${name}/git/pull`,
        { strategy },
        { headers: authStore.authHeader }
      )
      return response.data
    },

    async getGitLog(name, limit = 10) {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agents/${name}/git/log`, {
        params: { limit },
        headers: authStore.authHeader
      })
      return response.data
    },

    async initializeGitHub(name, config) {
      const authStore = useAuthStore()
      const response = await axios.post(`/api/agents/${name}/git/initialize`, config, {
        headers: authStore.authHeader,
        timeout: 120000 // 120 seconds (2 minutes) for git operations
      })
      return response.data
    },

    // Post-creation repo binding (ent#109)
    async bindAgentToOwnRepo(name, payload) {
      const authStore = useAuthStore()
      // Follows the surrounding raw-axios idiom rather than the shared `api.js`
      // instance, whose 30s instance-wide timeout is FAR below this call's
      // worst case (repo create + GitHub visibility poll + a full-history push
      // + a container replacement). Aborting the client mid-bind lands the user
      // after the commit point with no response, which is exactly the case the
      // GET .../bind-to-own-repo/status companion exists to rescue — so don't
      // manufacture it. 300s is generous against `initializeGitHub`'s 120s
      // because binding does strictly more work.
      const response = await axios.post(
        `/api/agents/${name}/git/bind-to-own-repo`,
        payload,
        { headers: authStore.authHeader, timeout: 300000 }
      )
      return response.data
    },

    async getBindToOwnRepoStatus(name) {
      const authStore = useAuthStore()
      const response = await axios.get(
        `/api/agents/${name}/git/bind-to-own-repo/status`,
        { headers: authStore.authHeader }
      )
      return response.data
    },

    // Per-agent GitHub PAT methods (#347)
    async getGitHubPATStatus(name) {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agents/${name}/github-pat`, {
        headers: authStore.authHeader
      })
      return response.data
    },

    async setGitHubPAT(name, pat) {
      const authStore = useAuthStore()
      const response = await axios.put(`/api/agents/${name}/github-pat`, { pat }, {
        headers: authStore.authHeader
      })
      return response.data
    },

    async clearGitHubPAT(name) {
      const authStore = useAuthStore()
      const response = await axios.delete(`/api/agents/${name}/github-pat`, {
        headers: authStore.authHeader
      })
      return response.data
    },

    async listAgentFiles(name, path = '/home/developer', showHidden = false) {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agents/${name}/files`, {
        params: { path, show_hidden: showHidden },
        headers: authStore.authHeader
      })
      return response.data
    },

    async downloadAgentFile(name, filePath) {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agents/${name}/files/download`, {
        params: { path: filePath },
        headers: authStore.authHeader,
        responseType: 'text'
      })
      return response.data
    },

    async deleteAgentFile(name, filePath) {
      const authStore = useAuthStore()
      const response = await axios.delete(`/api/agents/${name}/files`, {
        params: { path: filePath },
        headers: authStore.authHeader
      })
      return response.data
    },

    async updateAgentFile(name, filePath, content) {
      const authStore = useAuthStore()
      const response = await axios.put(`/api/agents/${name}/files`, {
        content
      }, {
        params: { path: filePath },
        headers: authStore.authHeader
      })
      return response.data
    },

    async createAgentFolder(name, folderPath) {
      const authStore = useAuthStore()
      const response = await axios.post(`/api/agents/${name}/files/mkdir`, {
        path: folderPath
      }, {
        headers: authStore.authHeader
      })
      return response.data
    },

    async getFilePreviewBlob(name, filePath) {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agents/${name}/files/preview`, {
        params: { path: filePath },
        headers: authStore.authHeader,
        responseType: 'blob'
      })
      // Return blob URL for media elements
      return {
        url: URL.createObjectURL(response.data),
        type: response.data.type,
        size: response.data.size
      }
    },

    // Custom Metrics Actions (Phase 9.9)
    async getAgentMetrics(name) {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agents/${name}/metrics`, {
        headers: authStore.authHeader
      })
      return response.data
    },

    // Agent Dashboard Actions
    async getAgentDashboard(name) {
      return dedupe(`agentDashboard:${name}`, async () => {
        const authStore = useAuthStore()
        const response = await axios.get(`/api/agent-dashboard/${name}`, {
          headers: authStore.authHeader
        })
        return response.data
      })
    },

    // NOTE: this is the STORE-level probe. `AgentDetail.vue` has a
    // same-named local function that wraps it with the boot retry ladder —
    // they are different functions (#2198 E13).
    async checkDashboardExists(name) {
      return dedupe(`dashboardExists:${name}`, async () => {
        const authStore = useAuthStore()
        const response = await axios.get(`/api/agent-dashboard/${name}/exists`, {
          headers: authStore.authHeader
        })
        return response.data?.has_dashboard === true
      })
    },

    updateAgentStatus(name, status) {
      const agent = this.agents.find(a => a.name === name)
      if (agent) agent.status = status
    },

    // PERF-269: fetchContextStats, fetchExecutionStats, fetchSlotStats removed
    // Stats are now fetched exclusively by networkStore to eliminate duplicate polling

    // Toggle autonomy mode for an agent
    async toggleAutonomy(agentName) {
      try {
        const authStore = useAuthStore()
        const agent = this.agents.find(a => a.name === agentName)
        if (!agent) return { success: false, error: 'Agent not found' }

        const newState = !agent.autonomy_enabled

        const response = await axios.put(`/api/agents/${agentName}/autonomy`, {
          enabled: newState
        }, {
          headers: authStore.authHeader
        })

        // Update local state
        agent.autonomy_enabled = newState

        return {
          success: true,
          enabled: newState,
          // #1945: autonomy is a gate, not a bulk edit — it no longer rewrites
          // per-schedule `enabled`. These are counts, not "how many we changed".
          totalSchedules: response.data.total_schedules,
          enabledSchedules: response.data.enabled_schedules,
          message: response.data.message
        }
      } catch (error) {
        console.error('Failed to toggle autonomy:', error)
        throw error
      }
    },

    // PERF-269: startContextPolling/stopContextPolling removed — use networkStore

    setSortBy(sortBy) {
      this.sortBy = sortBy
    },

    // Outbound File Sharing (FILES-001)
    async getFileSharingStatus(name) {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agents/${name}/file-sharing`, {
        headers: authStore.authHeader
      })
      return response.data
    },

    async setFileSharingStatus(name, enabled) {
      const authStore = useAuthStore()
      const response = await axios.put(`/api/agents/${name}/file-sharing`, { enabled }, {
        headers: authStore.authHeader
      })
      return response.data
    },

    async listSharedFiles(name) {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agents/${name}/shared-files`, {
        headers: authStore.authHeader
      })
      return response.data
    },

    async revokeSharedFile(name, fileId) {
      const authStore = useAuthStore()
      await axios.delete(`/api/agents/${name}/shared-files/${fileId}`, {
        headers: authStore.authHeader
      })
    },

    // MCP Exposure (#846) — expose the agent as a dedicated chat_with_<slug> tool
    async getMcpExposedStatus(name) {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agents/${name}/mcp-exposed`, {
        headers: authStore.authHeader
      })
      return response.data
    },

    async setMcpExposed(name, enabled) {
      const authStore = useAuthStore()
      const response = await axios.put(`/api/agents/${name}/mcp-exposed`, { enabled }, {
        headers: authStore.authHeader
      })
      return response.data
    },

    // A2A control plane (trinity-enterprise#158) — proxies the entitlement-gated
    // enterprise endpoints. The full config (exposure, card URL, allow-list,
    // outbound endpoints) comes from one GET; mutations return the updated config.
    async getA2aConfig(name) {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/enterprise/a2a/${name}/config`, {
        headers: authStore.authHeader
      })
      return response.data
    },

    async setA2aExposure(name, enabled) {
      const authStore = useAuthStore()
      const response = await axios.put(`/api/enterprise/a2a/${name}/exposure`, { enabled }, {
        headers: authStore.authHeader
      })
      return response.data
    },

    // ent#180: choose which skills the agent's A2A card advertises.
    // `skills: null` clears the curation (advertise all — the default);
    // `skills: []` advertises nothing. The two are different on purpose.
    async setA2aSkills(name, skills) {
      const authStore = useAuthStore()
      const response = await axios.put(`/api/enterprise/a2a/${name}/skills`, { skills }, {
        headers: authStore.authHeader
      })
      return response.data
    },

    async updateA2aAllowlist(name, { add = [], remove = [] }) {
      const authStore = useAuthStore()
      const response = await axios.post(`/api/enterprise/a2a/${name}/inbound-allowlist`,
        { add, remove }, { headers: authStore.authHeader })
      return response.data
    },

    async registerA2aEndpoint(name, { name: label, url, credentials }) {
      const authStore = useAuthStore()
      const body = { name: label, url }
      if (credentials) body.credentials = credentials
      const response = await axios.post(`/api/enterprise/a2a/${name}/endpoints`, body, {
        headers: authStore.authHeader
      })
      return response.data
    },

    async removeA2aEndpoint(name, endpointId) {
      const authStore = useAuthStore()
      const response = await axios.delete(`/api/enterprise/a2a/${name}/endpoints/${endpointId}`, {
        headers: authStore.authHeader
      })
      return response.data
    },

    // The owner-visible served Agent Card (#737) — used to show the advertised
    // skills an external A2A caller will see.
    async getA2aCard(name) {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agents/${name}/a2a/agent-card`, {
        headers: authStore.authHeader
      })
      return response.data
    },

    // Shared Folders Actions (Phase 9.11: Agent Shared Folders)
    async getAgentFolders(name) {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agents/${name}/folders`, {
        headers: authStore.authHeader
      })
      return response.data
    },

    async updateAgentFolders(name, config) {
      const authStore = useAuthStore()
      const response = await axios.put(`/api/agents/${name}/folders`, config, {
        headers: authStore.authHeader
      })
      return response.data
    },

    async getAvailableFolders(name) {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agents/${name}/folders/available`, {
        headers: authStore.authHeader
      })
      return response.data.available_folders || []
    },

    async getFolderConsumers(name) {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agents/${name}/folders/consumers`, {
        headers: authStore.authHeader
      })
      return response.data.consumers || []
    },

    // API Key Settings (Per-agent authentication control)
    async getAgentApiKeySetting(name) {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agents/${name}/api-key-setting`, {
        headers: authStore.authHeader
      })
      return response.data
    },

    async updateAgentApiKeySetting(name, usePlatformKey) {
      const authStore = useAuthStore()
      const response = await axios.put(`/api/agents/${name}/api-key-setting`, {
        use_platform_api_key: usePlatformKey
      }, {
        headers: authStore.authHeader
      })
      return response.data
    },

    // Resource Limits (Per-agent memory and CPU allocation)
    async getResourceLimits(name) {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agents/${name}/resources`, {
        headers: authStore.authHeader
      })
      return response.data
    },

    async setResourceLimits(name, memory, cpu) {
      const authStore = useAuthStore()
      const response = await axios.put(`/api/agents/${name}/resources`, {
        memory: memory,
        cpu: cpu
      }, {
        headers: authStore.authHeader
      })
      return response.data
    },

    // Guardrails (GUARD-001 — per-agent max_turns overrides)
    async getGuardrails(name) {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agents/${name}/guardrails`, {
        headers: authStore.authHeader
      })
      return response.data
    },

    async setGuardrails(name, guardrails) {
      const authStore = useAuthStore()
      const response = await axios.put(`/api/agents/${name}/guardrails`, guardrails, {
        headers: authStore.authHeader
      })
      return response.data
    },

    // Capacity (CAPACITY-001 / #506 — per-agent max_parallel_tasks within the fleet ceiling)
    async getAgentCapacity(name) {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agents/${name}/capacity`, {
        headers: authStore.authHeader
      })
      return response.data
    },

    async setAgentCapacity(name, maxParallelTasks) {
      const authStore = useAuthStore()
      const response = await axios.put(`/api/agents/${name}/capacity`, {
        max_parallel_tasks: maxParallelTasks
      }, {
        headers: authStore.authHeader
      })
      return response.data
    }
  }
})
