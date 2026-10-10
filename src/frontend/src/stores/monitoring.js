/**
 * Monitoring Store (MON-001)
 *
 * Manages agent health monitoring state including:
 * - Fleet-wide health summary
 * - Individual agent health details
 * - Active alerts
 * - Monitoring configuration
 */

import { defineStore } from 'pinia'
import axios from 'axios'
import { useAuthStore } from './auth'

export const useMonitoringStore = defineStore('monitoring', {
  state: () => ({
    // Service state
    enabled: true,
    loading: false,
    error: null,

    // Fleet status
    lastCheck: null,
    summary: {
      total_agents: 0,
      healthy: 0,
      degraded: 0,
      unhealthy: 0,
      critical: 0,
      unknown: 0
    },
    agents: [],
    // #3452: agent name -> transport-breaker state ({state, failure_count,
    // cooldown_remaining}), as reported by GET /api/monitoring/status. The
    // API sends it to admins only and omits it when there is no history.
    circuitBreakers: {},

    // Active alerts
    alerts: [],
    alertsLoading: false,

    // Configuration
    config: null,
    configLoading: false,

    // Polling
    pollingInterval: null,
    pollingIntervalMs: 30000, // 30 seconds

    // Agent detail cache
    agentDetailCache: {} // Map of agent name -> AgentHealthDetail
  }),

  getters: {
    /**
     * Get agents with unhealthy/critical status
     */
    unhealthyAgents() {
      return this.agents.filter(a =>
        ['unhealthy', 'critical'].includes(a.status)
      )
    },

    /**
     * Get agents with degraded status
     */
    degradedAgents() {
      return this.agents.filter(a => a.status === 'degraded')
    },

    /**
     * Check if there are active alerts
     */
    hasActiveAlerts() {
      return this.alerts.length > 0
    },

    /**
     * Get count of unacknowledged alerts
     */
    unacknowledgedAlertCount() {
      return this.alerts.filter(a => a.status === 'pending').length
    },

    /**
     * Get health status color for an agent
     */
    getStatusColor: () => (status) => {
      switch (status) {
        case 'healthy': return 'green'
        case 'degraded': return 'yellow'
        case 'unhealthy': return 'red'
        case 'critical': return 'red'
        default: return 'gray'
      }
    },

    /**
     * Get health status icon for an agent
     */
    getStatusIcon: () => (status) => {
      switch (status) {
        case 'healthy': return 'check-circle'
        case 'degraded': return 'exclamation-triangle'
        case 'unhealthy': return 'times-circle'
        case 'critical': return 'skull'
        default: return 'question-circle'
      }
    }
  },

  actions: {
    /**
     * Get auth headers for API calls
     */
    getAuthHeaders() {
      const authStore = useAuthStore()
      return authStore.authHeader
    },

    /**
     * Fetch fleet-wide health status
     */
    async fetchStatus() {
      this.loading = true
      this.error = null

      try {
        const response = await axios.get('/api/monitoring/status', {
          headers: this.getAuthHeaders()
        })

        this.enabled = response.data.enabled
        this.lastCheck = response.data.last_check_at
        this.summary = response.data.summary
        this.agents = response.data.agents
        this.circuitBreakers = response.data.circuit_breakers || {}
      } catch (err) {
        console.error('Failed to fetch monitoring status:', err)
        this.error = err.response?.data?.detail || 'Failed to fetch status'
      } finally {
        this.loading = false
      }
    },

    /**
     * Fetch health details for a specific agent
     */
    async fetchAgentHealth(agentName) {
      try {
        const response = await axios.get(`/api/monitoring/agents/${agentName}`, {
          headers: this.getAuthHeaders()
        })

        // Cache the result
        this.agentDetailCache[agentName] = response.data
        return response.data
      } catch (err) {
        console.error(`Failed to fetch health for ${agentName}:`, err)
        throw err
      }
    },

    /**
     * Fetch health history for an agent
     */
    async fetchAgentHistory(agentName, hours = 24, checkType = 'aggregate') {
      try {
        const response = await axios.get(`/api/monitoring/agents/${agentName}/history`, {
          headers: this.getAuthHeaders(),
          params: { hours, check_type: checkType }
        })
        return response.data
      } catch (err) {
        console.error(`Failed to fetch history for ${agentName}:`, err)
        throw err
      }
    },

    /**
     * Trigger immediate health check for an agent
     */
    async triggerCheck(agentName) {
      try {
        const response = await axios.post(
          `/api/monitoring/agents/${agentName}/check`,
          {},
          { headers: this.getAuthHeaders() }
        )

        // Update cache
        this.agentDetailCache[agentName] = response.data

        // Refresh fleet status
        await this.fetchStatus()

        return response.data
      } catch (err) {
        console.error(`Failed to trigger check for ${agentName}:`, err)
        throw err
      }
    },

    /**
     * Fetch active alerts
     */
    async fetchAlerts(status = 'pending') {
      this.alertsLoading = true

      try {
        const response = await axios.get('/api/monitoring/alerts', {
          headers: this.getAuthHeaders(),
          params: { status }
        })

        this.alerts = response.data.alerts || []
      } catch (err) {
        console.error('Failed to fetch alerts:', err)
      } finally {
        this.alertsLoading = false
      }
    },

    /**
     * Fetch monitoring configuration
     */
    async fetchConfig() {
      this.configLoading = true

      try {
        const response = await axios.get('/api/monitoring/config', {
          headers: this.getAuthHeaders()
        })

        this.config = response.data
      } catch (err) {
        console.error('Failed to fetch config:', err)
      } finally {
        this.configLoading = false
      }
    },

    /**
     * Update monitoring configuration
     */
    async updateConfig(config) {
      try {
        const response = await axios.put('/api/monitoring/config', config, {
          headers: this.getAuthHeaders()
        })

        this.config = response.data
        return response.data
      } catch (err) {
        console.error('Failed to update config:', err)
        throw err
      }
    },

    /**
     * Enable monitoring service
     */
    async enableMonitoring() {
      try {
        await axios.post('/api/monitoring/enable', {}, {
          headers: this.getAuthHeaders()
        })
        this.enabled = true
      } catch (err) {
        console.error('Failed to enable monitoring:', err)
        throw err
      }
    },

    /**
     * Disable monitoring service
     */
    async disableMonitoring() {
      try {
        await axios.post('/api/monitoring/disable', {}, {
          headers: this.getAuthHeaders()
        })
        this.enabled = false
      } catch (err) {
        console.error('Failed to disable monitoring:', err)
        throw err
      }
    },

    /**
     * Trigger fleet-wide health check
     */
    async triggerFleetCheck() {
      try {
        const response = await axios.post('/api/monitoring/check-all', {}, {
          headers: this.getAuthHeaders()
        })
        return response.data
      } catch (err) {
        console.error('Failed to trigger fleet check:', err)
        throw err
      }
    },

    /**
     * Start polling for status updates
     */
    startPolling(intervalMs = null) {
      if (intervalMs) {
        this.pollingIntervalMs = intervalMs
      }

      this.stopPolling() // Clear any existing interval

      // Initial fetch
      this.fetchStatus()

      // Set up polling
      this.pollingInterval = setInterval(() => {
        this.fetchStatus()
      }, this.pollingIntervalMs)
    },

    /**
     * Stop polling for status updates
     */
    stopPolling() {
      if (this.pollingInterval) {
        clearInterval(this.pollingInterval)
        this.pollingInterval = null
      }
    },

    /**
     * Handle WebSocket health event
     */
    handleHealthEvent(event) {
      if (event.type === 'agent_health_changed') {
        // Update agent in list
        const index = this.agents.findIndex(a => a.name === event.agent_name)
        if (index >= 0) {
          this.agents[index] = {
            ...this.agents[index],
            status: event.current_status,
            issues: event.issues,
            last_check_at: event.timestamp
          }

          // Update summary counts
          this._recalculateSummary()
        }

        // Invalidate cache
        delete this.agentDetailCache[event.agent_name]
      } else if (event.type === 'monitoring_alert') {
        // Add to alerts list if not already present
        if (!this.alerts.find(a => a.notification_id === event.notification_id)) {
          this.alerts.unshift({
            id: event.notification_id,
            agent_name: event.agent_name,
            title: event.title,
            priority: event.priority,
            status: 'pending',
            created_at: event.timestamp
          })
        }
      }
    },

    /**
     * Recalculate summary from agents list
     */
    _recalculateSummary() {
      const summary = {
        total_agents: this.agents.length,
        healthy: 0,
        degraded: 0,
        unhealthy: 0,
        critical: 0,
        unknown: 0
      }

      for (const agent of this.agents) {
        const status = agent.status || 'unknown'
        if (status in summary) {
          summary[status]++
        } else {
          summary.unknown++
        }
      }

      this.summary = summary
    },

    /**
     * Clear cached agent details
     */
    clearCache() {
      this.agentDetailCache = {}
    }
  }
})
