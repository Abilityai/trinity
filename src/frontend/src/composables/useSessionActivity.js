import { ref, computed, onUnmounted } from 'vue'
import { activityLine } from '../utils/workActivity'

/**
 * Composable for session info and activity polling
 * Manages context window, token usage, cost tracking, and activity status
 *
 * =============================================================================
 * REUSABLE COST & CONTEXT TRACKING LOGIC
 * =============================================================================
 *
 * This composable contains the core logic for tracking session costs and context
 * window usage. While currently not displayed in the Terminal tab, this data is
 * still collected and can be reused in other UI components.
 *
 * DATA AVAILABLE (via sessionInfo ref):
 * - context_tokens: Current token count used in the session
 * - context_window: Maximum context window size (e.g., 200000 for Claude)
 * - context_percent: Percentage of context window used (0-100)
 * - total_cost_usd: Cumulative session cost in USD
 * - message_count: Number of messages in the session
 *
 * BACKEND API:
 * - Endpoint: GET /api/agents/{name}/chat/session
 * - Returns session data from the agent's internal API at http://agent-{name}:8000/api/chat/session
 *
 * USAGE EXAMPLE (for future UI integration):
 * ```vue
 * <template>
 *   <div class="cost-display">
 *     <span>Cost: ${{ sessionInfo.total_cost_usd.toFixed(4) }}</span>
 *     <span>Context: {{ sessionInfo.context_percent.toFixed(1) }}%</span>
 *   </div>
 * </template>
 *
 * <script setup>
 * import { useSessionActivity } from '../composables/useSessionActivity'
 * const { sessionInfo, loadSessionInfo } = useSessionActivity(agentRef, agentsStore)
 * </script>
 * ```
 *
 * RELATED FILES:
 * - src/backend/routers/chat.py - Backend endpoint for session data
 * - src/backend/services/scheduler_service.py - Uses cost data for scheduled executions
 * - src/frontend/src/components/SchedulesPanel.vue - Shows cost in execution history
 * - src/frontend/src/stores/agents.js - getSessionInfo() store method
 *
 * TODO: Consider integrating cost/context display into:
 * - Dashboard overview
 * - Agent metrics panel
 * - Activity timeline
 * =============================================================================
 */
export function useSessionActivity(agentRef, agentsStore) {
  /**
   * Session info containing cost and context window tracking data.
   * This data is fetched from the agent's internal chat session API.
   *
   * @property {number} context_tokens - Current tokens used in context
   * @property {number} context_window - Max context window size (model-dependent)
   * @property {number} context_percent - Percentage of context used (0-100)
   * @property {number} total_cost_usd - Cumulative session cost in USD
   * @property {number} message_count - Number of messages exchanged
   */
  const sessionInfo = ref({
    context_tokens: 0,
    context_window: 200000,
    context_percent: 0,
    total_cost_usd: 0,
    message_count: 0
  })

  const sessionActivity = ref({
    status: 'idle',
    active_tool: null,
    tool_counts: {},
    timeline: [],
    totals: { calls: 0, duration_ms: 0, started_at: null }
  })

  let activityRefreshInterval = null

  // trinity-enterprise#620: the same words the Workspace card and the Chat
  // tab use, from the same two facts (`active_tool.name` is the agent's
  // display tool name, `input_summary` its bounded input summary).
  const currentToolDisplay = computed(() => {
    const active = sessionActivity.value?.active_tool
    if (active) {
      const line = activityLine({ tool: active.name, summary: active.input_summary })
      return line ? `${line.text}...` : `${active.name}...`
    }
    return 'Processing...'
  })

  const loadSessionInfo = async () => {
    if (!agentRef.value || agentRef.value.status !== 'running') return
    try {
      const info = await agentsStore.getSessionInfo(agentRef.value.name)
      sessionInfo.value = {
        context_tokens: info.context_tokens || 0,
        context_window: info.context_window || 200000,
        context_percent: info.context_percent || 0,
        total_cost_usd: info.total_cost_usd || 0,
        message_count: info.message_count || 0
      }
    } catch (err) {
      console.error('Failed to load session info:', err)
    }
  }

  const loadSessionActivity = async () => {
    if (!agentRef.value || agentRef.value.status !== 'running') return
    try {
      sessionActivity.value = await agentsStore.getSessionActivity(agentRef.value.name)
    } catch (err) {
      // Don't log errors - activity endpoint may fail during startup
      console.debug('Failed to load session activity:', err)
    }
  }

  const startActivityPolling = () => {
    if (activityRefreshInterval) clearInterval(activityRefreshInterval)  // PERF-269: guard double-registration
    loadSessionActivity() // Load immediately
    activityRefreshInterval = setInterval(() => {
      if (!document.hidden) loadSessionActivity()  // PERF-269: skip when tab backgrounded
    }, 5000) // Then every 5 seconds
  }

  const stopActivityPolling = () => {
    if (activityRefreshInterval) {
      clearInterval(activityRefreshInterval)
      activityRefreshInterval = null
    }
  }

  const clearActivity = async () => {
    if (!agentRef.value) return
    try {
      await agentsStore.clearSessionActivity(agentRef.value.name)
      sessionActivity.value = {
        status: 'idle',
        active_tool: null,
        tool_counts: {},
        timeline: [],
        totals: { calls: 0, duration_ms: 0, started_at: null }
      }
    } catch (err) {
      console.error('Failed to clear activity:', err)
    }
  }

  const resetSessionInfo = () => {
    sessionInfo.value = {
      context_tokens: 0,
      context_window: 200000,
      context_percent: 0,
      total_cost_usd: 0,
      message_count: 0
    }
  }

  const resetSessionActivity = () => {
    sessionActivity.value = {
      status: 'idle',
      active_tool: null,
      tool_counts: {},
      timeline: [],
      totals: { calls: 0, duration_ms: 0, started_at: null }
    }
  }

  // Cleanup on unmount
  onUnmounted(() => {
    stopActivityPolling()
  })

  return {
    sessionInfo,
    sessionActivity,
    currentToolDisplay,
    loadSessionInfo,
    loadSessionActivity,
    startActivityPolling,
    stopActivityPolling,
    clearActivity,
    resetSessionInfo,
    resetSessionActivity
  }
}
