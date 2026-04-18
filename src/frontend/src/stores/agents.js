import { defineStore } from 'pinia'
import axios from 'axios'
import { useAuthStore } from './auth'
import { useNetworkStore } from './network'

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
    syncHealth: {}  // Map of agent name -> { last_sync_status, consecutive_failures, behind_working, ... }
  }),

  getters: {
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
    },
    // Sorted agents excluding system agent (for regular users)
    sortedAgents() {
      return this._getSortedAgents(false)
    },
    // Sorted agents including system agent pinned at top (for admins)
    sortedAgentsWithSystem() {
      return this._getSortedAgents(true)
    },
    // Internal getter for sorted agents with optional system agent inclusion
    _getSortedAgents() {
      return (includeSystem) => {
        const sorted = [...this.userAgents]
        switch (this.sortBy) {
          case 'created_desc':
            sorted.sort((a, b) => new Date(b.created || 0) - new Date(a.created || 0))
            break
          case 'created_asc':
            sorted.sort((a, b) => new Date(a.created || 0) - new Date(b.created || 0))
            break
          case 'name_asc':
            sorted.sort((a, b) => a.name.localeCompare(b.name))
            break
          case 'name_desc':
            sorted.sort((a, b) => b.name.localeCompare(a.name))
            break
          case 'status':
            sorted.sort((a, b) => (b.status === 'running' ? 1 : 0) - (a.status === 'running' ? 1 : 0))
            break
          case 'success_desc':
            sorted.sort((a, b) => {
              const networkStore = useNetworkStore()
              const aRate = networkStore.executionStats[a.name]?.successRate || 0
              const bRate = networkStore.executionStats[b.name]?.successRate || 0
              return bRate - aRate
            })
            break
        }
        // Pin system agent at top if requested and exists
        if (includeSystem && this.systemAgent) {
          return [this.systemAgent, ...sorted]
        }
        return sorted
      }
    }
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

    async fetchAgent(name) {
      this.loading = true
      this.error = null
      try {
        const authStore = useAuthStore()
        const response = await axios.get(`/api/agents/${name}`, {
          headers: authStore.authHeader
        })
        this.selectedAgent = response.data
        return response.data
      } catch (error) {
        this.error = error.message
        console.error('Failed to fetch agent:', error)
      } finally {
        this.loading = false
      }
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
    async injectCredentials(name, files) {
      const authStore = useAuthStore()
      const response = await axios.post(`/api/agents/${name}/credentials/inject`,
        { files },
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

    async getAgentInfo(name) {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agents/${name}/info`, {
        headers: authStore.authHeader
      })
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
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agent-dashboard/${name}`, {
        headers: authStore.authHeader
      })
      return response.data
    },

    async checkDashboardExists(name) {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agent-dashboard/${name}/exists`, {
        headers: authStore.authHeader
      })
      return response.data?.has_dashboard === true
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
          schedulesUpdated: response.data.schedules_updated
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
    }
  }
})
