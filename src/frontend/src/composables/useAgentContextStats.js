import { computed } from 'vue'

/**
 * trinity-enterprise#950 — keep this agent's context-window reading fresh for
 * the agent header chart while the agent page is shown.
 *
 * Reuses the fleet store the dashboard already fills
 * (`stores/network.js::fetchContextStats`, `GET /api/agents/context-stats`),
 * so the header and the fleet tile read one number. It does NOT call the
 * store's `startContextPolling` / `stopContextPolling`: that pair is the
 * Dashboard's, also polls execution and slot stats, and a stop from here would
 * end the Dashboard's poll too.
 *
 * Lifecycle is the caller's. AgentDetail is KeepAlive-cached, so `onUnmounted`
 * does not fire on navigation — start/stop ride the page's
 * startAllPolling/stopAllPolling (onActivated / onDeactivated / agent switch)
 * and its status watcher (running only).
 *
 * A failed fetch keeps the previous reading: the store only logs on error and
 * never overwrites the map with an empty one (design-system: a failed refresh
 * keeps the data).
 */
export const CONTEXT_POLL_MS = 15000

export function useAgentContextStats(agentRef, networkStore) {
  let timer = null
  let inFlight = false

  const stats = computed(() => {
    const name = agentRef.value?.name
    return (name && networkStore.contextStats?.[name]) || null
  })

  async function refresh() {
    // The endpoint is fleet-wide; on a slow backend a new tick must not stack
    // a second request behind the first.
    if (inFlight) return
    inFlight = true
    try {
      await networkStore.fetchContextStats()
    } finally {
      inFlight = false
    }
  }

  function start() {
    stop()
    refresh()
    timer = setInterval(() => {
      if (typeof document !== 'undefined' && document.hidden) return // PERF-269
      refresh()
    }, CONTEXT_POLL_MS)
  }

  function stop() {
    if (timer) {
      clearInterval(timer)
      timer = null
    }
  }

  return { stats, start, stop, refresh }
}
