import { defineStore } from 'pinia'
import axios from 'axios'
import { useAuthStore } from './auth'

export const useAgentsStore = defineStore('agents', {
  state: () => ({
    agents: [],
    selectedAgent: null,
    loading: false,
    error: null,
    // Context stats for agents list page
    contextStats: {},  // Map of agent name -> stats
    planStats: {},     // Map of agent name -> plan stats
    contextPollingInterval: null,
    sortBy: 'created_desc'  // Default sort order
  }),

  getters: {
    runningAgents() {
      return this.agents.filter(agent => agent.status === 'running')
    },
    stoppedAgents() {
      return this.agents.filter(agent => agent.status === 'stopped')
    },
    sortedAgents() {
      const sorted = [...this.agents]
      switch (this.sortBy) {
        case 'created_desc':
          return sorted.sort((a, b) => new Date(b.created || 0) - new Date(a.created || 0))
        case 'created_asc':
          return sorted.sort((a, b) => new Date(a.created || 0) - new Date(b.created || 0))
        case 'name_asc':
          return sorted.sort((a, b) => a.name.localeCompare(b.name))
        case 'name_desc':
          return sorted.sort((a, b) => b.name.localeCompare(a.name))
        case 'status':
          return sorted.sort((a, b) => (b.status === 'running' ? 1 : 0) - (a.status === 'running' ? 1 : 0))
        case 'context_desc':
          return sorted.sort((a, b) => {
            const aContext = this.contextStats[a.name]?.contextPercent || 0
            const bContext = this.contextStats[b.name]?.contextPercent || 0
            return bContext - aContext
          })
        default:
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
        this.agents.push(response.data)
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

    async getAgentCredentials(name) {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agents/${name}/credentials`, {
        headers: authStore.authHeader
      })
      return response.data
    },

    async hotReloadCredentials(name, credentialsText) {
      const authStore = useAuthStore()
      const response = await axios.post(`/api/agents/${name}/credentials/hot-reload`,
        { credentials_text: credentialsText },
        { headers: authStore.authHeader }
      )
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

    async syncToGithub(name, message = null, paths = null) {
      const authStore = useAuthStore()
      const payload = {}
      if (message) payload.message = message
      if (paths) payload.paths = paths
      const response = await axios.post(`/api/agents/${name}/git/sync`,
        payload,
        { headers: authStore.authHeader }
      )
      return response.data
    },

    async pullFromGithub(name) {
      const authStore = useAuthStore()
      const response = await axios.post(`/api/agents/${name}/git/pull`, {}, {
        headers: authStore.authHeader
      })
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

    async listAgentFiles(name, path = '/home/developer') {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agents/${name}/files`, {
        params: { path },
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

    // Plans / Task DAG Actions (Phase 9 - Pillar I: Explicit Planning)
    async getAgentPlans(name, status = null) {
      const authStore = useAuthStore()
      const params = status ? { status } : {}
      const response = await axios.get(`/api/agents/${name}/plans`, {
        params,
        headers: authStore.authHeader
      })
      return response.data
    },

    async getAgentPlansSummary(name) {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agents/${name}/plans/summary`, {
        headers: authStore.authHeader
      })
      return response.data
    },

    async getAgentPlan(name, planId) {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agents/${name}/plans/${planId}`, {
        headers: authStore.authHeader
      })
      return response.data
    },

    async createAgentPlan(name, planData) {
      const authStore = useAuthStore()
      const response = await axios.post(`/api/agents/${name}/plans`, planData, {
        headers: authStore.authHeader
      })
      return response.data
    },

    async updateAgentPlan(name, planId, updates) {
      const authStore = useAuthStore()
      const response = await axios.put(`/api/agents/${name}/plans/${planId}`, updates, {
        headers: authStore.authHeader
      })
      return response.data
    },

    async deleteAgentPlan(name, planId) {
      const authStore = useAuthStore()
      const response = await axios.delete(`/api/agents/${name}/plans/${planId}`, {
        headers: authStore.authHeader
      })
      return response.data
    },

    async updateAgentTask(name, planId, taskId, taskUpdate) {
      const authStore = useAuthStore()
      const response = await axios.put(
        `/api/agents/${name}/plans/${planId}/tasks/${taskId}`,
        taskUpdate,
        { headers: authStore.authHeader }
      )
      return response.data
    },

    // Custom Metrics Actions (Phase 9.9)
    async getAgentMetrics(name) {
      const authStore = useAuthStore()
      const response = await axios.get(`/api/agents/${name}/metrics`, {
        headers: authStore.authHeader
      })
      return response.data
    },

    updateAgentStatus(name, status) {
      const agent = this.agents.find(a => a.name === name)
      if (agent) agent.status = status
    },

    // Context Stats Actions (for Agents list page)
    async fetchContextStats() {
      try {
        const authStore = useAuthStore()
        const response = await axios.get('/api/agents/context-stats', {
          headers: authStore.authHeader
        })
        const agentStats = response.data.agents || []

        const newStats = {}
        agentStats.forEach(stat => {
          newStats[stat.name] = {
            contextPercent: stat.contextPercent || 0,
            contextUsed: stat.contextUsed || 0,
            contextMax: stat.contextMax || 200000,
            activityState: stat.activityState || 'offline',
            lastActivityTime: stat.lastActivityTime
          }
        })
        this.contextStats = newStats
      } catch (error) {
        console.error('Failed to fetch context stats:', error)
      }
    },

    async fetchPlanStats() {
      try {
        const authStore = useAuthStore()
        const response = await axios.get('/api/agents/plans/aggregate', {
          headers: authStore.authHeader
        })
        const data = response.data

        // Update per-agent plan stats from agent_summaries
        const newPlanStats = {}
        if (data.agent_summaries) {
          data.agent_summaries.forEach(agentData => {
            const agentName = agentData.agent_name
            const totalTasks = agentData.total_tasks || 0
            const completedTasks = agentData.completed_tasks || 0

            newPlanStats[agentName] = {
              activePlan: agentData.active_plans > 0,
              totalPlans: agentData.total_plans || 0,
              activeTasks: agentData.active_tasks || 0,
              completedTasks: completedTasks,
              totalTasks: totalTasks,
              currentTask: agentData.current_task?.name || null,
              taskProgress: totalTasks > 0 ? Math.round((completedTasks / totalTasks) * 100) : 0
            }
          })
        }
        this.planStats = newPlanStats
      } catch (error) {
        // Don't log 404 errors as they're expected when no plans exist
        if (error.response?.status !== 404) {
          console.error('Failed to fetch plan stats:', error)
        }
      }
    },

    startContextPolling() {
      if (this.contextPollingInterval) {
        clearInterval(this.contextPollingInterval)
      }

      // Fetch immediately
      this.fetchContextStats()
      this.fetchPlanStats()

      // Then poll every 5 seconds
      this.contextPollingInterval = setInterval(() => {
        this.fetchContextStats()
        this.fetchPlanStats()
      }, 5000)

      console.log('[Agents] Started context and plan polling (every 5s)')
    },

    stopContextPolling() {
      if (this.contextPollingInterval) {
        clearInterval(this.contextPollingInterval)
        this.contextPollingInterval = null
        console.log('[Agents] Stopped context polling')
      }
    },

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
    }
  }
})
