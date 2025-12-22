import { defineStore } from 'pinia'
import axios from 'axios'

export const useObservabilityStore = defineStore('observability', {
  state: () => ({
    // Status
    enabled: false,
    available: false,
    error: null,
    loading: false,
    lastUpdated: null,

    // Raw metrics
    metrics: {
      cost_by_model: {},
      tokens_by_model: {},
      lines_of_code: {},
      sessions: 0,
      active_time_seconds: 0,
      commits: 0,
      pull_requests: 0
    },

    // Calculated totals
    totals: {
      total_cost: 0,
      total_tokens: 0,
      tokens_by_type: {},
      total_lines: 0,
      sessions: 0,
      active_time_seconds: 0,
      commits: 0,
      pull_requests: 0
    },

    // Polling
    pollInterval: null,
    pollIntervalMs: 60000 // 60 seconds
  }),

  getters: {
    /**
     * Check if OTel is fully operational (enabled and reachable)
     */
    isOperational() {
      return this.enabled && this.available
    },

    /**
     * Format total cost as currency
     */
    formattedTotalCost() {
      return `$${this.totals.total_cost.toFixed(4)}`
    },

    /**
     * Format total tokens with K/M suffix
     */
    formattedTotalTokens() {
      const tokens = this.totals.total_tokens
      if (tokens >= 1000000) {
        return `${(tokens / 1000000).toFixed(1)}M`
      }
      if (tokens >= 1000) {
        return `${(tokens / 1000).toFixed(1)}K`
      }
      return tokens.toString()
    },

    /**
     * Format active time as human-readable duration
     */
    formattedActiveTime() {
      const seconds = this.totals.active_time_seconds
      if (seconds < 60) {
        return `${Math.round(seconds)}s`
      }
      if (seconds < 3600) {
        return `${Math.round(seconds / 60)}m`
      }
      const hours = Math.floor(seconds / 3600)
      const mins = Math.round((seconds % 3600) / 60)
      return `${hours}h ${mins}m`
    },

    /**
     * Get cost breakdown as sorted array for display
     */
    costBreakdown() {
      return Object.entries(this.metrics.cost_by_model)
        .map(([model, cost]) => ({
          model: this.formatModelName(model),
          cost: cost,
          formattedCost: `$${cost.toFixed(4)}`
        }))
        .sort((a, b) => b.cost - a.cost)
    },

    /**
     * Get token breakdown by model and type
     */
    tokenBreakdown() {
      const breakdown = []
      for (const [model, types] of Object.entries(this.metrics.tokens_by_model)) {
        for (const [type, count] of Object.entries(types)) {
          breakdown.push({
            model: this.formatModelName(model),
            type: type,
            count: count,
            formattedCount: this.formatNumber(count)
          })
        }
      }
      return breakdown.sort((a, b) => b.count - a.count)
    },

    /**
     * Get token totals by type
     */
    tokensByType() {
      return Object.entries(this.totals.tokens_by_type)
        .map(([type, count]) => ({
          type: type,
          count: count,
          formattedCount: this.formatNumber(count)
        }))
        .sort((a, b) => b.count - a.count)
    },

    /**
     * Get lines of code breakdown
     */
    linesBreakdown() {
      return Object.entries(this.metrics.lines_of_code)
        .map(([type, count]) => ({
          type: type,
          count: count
        }))
    },

    /**
     * Check if there's any meaningful data to display
     */
    hasData() {
      return this.totals.total_cost > 0 ||
             this.totals.total_tokens > 0 ||
             this.totals.sessions > 0
    }
  },

  actions: {
    /**
     * Fetch observability metrics from the backend
     */
    async fetchMetrics() {
      this.loading = true

      try {
        const response = await axios.get('/api/observability/metrics')
        const data = response.data

        this.enabled = data.enabled
        this.available = data.available || false
        this.error = data.error || null

        if (data.metrics) {
          this.metrics = data.metrics
        }

        if (data.totals) {
          this.totals = data.totals
        }

        this.lastUpdated = new Date()

      } catch (error) {
        console.error('Failed to fetch observability metrics:', error)
        this.error = error.response?.data?.detail || 'Failed to fetch metrics'
        this.available = false
      } finally {
        this.loading = false
      }
    },

    /**
     * Fetch just the status (lighter weight check)
     */
    async fetchStatus() {
      try {
        const response = await axios.get('/api/observability/status')
        this.enabled = response.data.enabled
        this.available = response.data.collector_reachable || false
        return response.data
      } catch (error) {
        console.error('Failed to fetch observability status:', error)
        return { enabled: false, available: false }
      }
    },

    /**
     * Start polling for metrics updates
     */
    startPolling() {
      if (this.pollInterval) {
        return // Already polling
      }

      // Fetch immediately
      this.fetchMetrics()

      // Then poll every interval
      this.pollInterval = setInterval(() => {
        this.fetchMetrics()
      }, this.pollIntervalMs)
    },

    /**
     * Stop polling for metrics
     */
    stopPolling() {
      if (this.pollInterval) {
        clearInterval(this.pollInterval)
        this.pollInterval = null
      }
    },

    /**
     * Set the polling interval (in milliseconds)
     */
    setPollingInterval(ms) {
      this.pollIntervalMs = ms
      // Restart polling with new interval if currently polling
      if (this.pollInterval) {
        this.stopPolling()
        this.startPolling()
      }
    },

    /**
     * Format a model name for display
     * e.g., "claude-haiku-4-5-20251001" -> "Claude Haiku 4.5"
     */
    formatModelName(model) {
      if (!model) return 'Unknown'

      // Common model name patterns
      if (model.includes('haiku')) return 'Claude Haiku'
      if (model.includes('sonnet')) return 'Claude Sonnet'
      if (model.includes('opus')) return 'Claude Opus'

      // Return as-is if no match
      return model
    },

    /**
     * Format a number with K/M suffix
     */
    formatNumber(num) {
      if (num >= 1000000) {
        return `${(num / 1000000).toFixed(1)}M`
      }
      if (num >= 1000) {
        return `${(num / 1000).toFixed(1)}K`
      }
      return num.toString()
    }
  }
})
