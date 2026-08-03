// Agent list sort comparator (trinity-enterprise#260) — extracted from the
// retired stores/agents.js `_getSortedAgents` getter so the Dashboard List
// panel can sort an arbitrary agent array without a store getter (and so
// agents.js no longer needs its `useNetworkStore` import, breaking the
// agents↔network module cycle).
//
// INPUT CONTRACT: `list` is the FULL visible agent list *including* system
// agents. The function partitions system rows out, sorts the rest, and
// re-pins the system rows first — callers must NOT pre-strip or pre-pin
// system agents (a naive "pin flag" over an already-pinned list would
// duplicate the row).
//
// `executionStats` is the networkStore `executionStats` map (agent name →
// { taskCount, successRate, ... }); it is a plain parameter — the caller
// reads it inside a computed so reactivity tracks the polling updates —
// instead of the old per-comparison `useNetworkStore()` access.
import { agentDisplayName } from './agentName'

export function sortAgents(list, sortBy, executionStats = {}) {
  const system = list.filter(a => a.is_system)
  const rest = list.filter(a => !a.is_system)

  switch (sortBy) {
    case 'created_desc':
      rest.sort((a, b) => new Date(b.created || 0) - new Date(a.created || 0))
      break
    case 'created_asc':
      rest.sort((a, b) => new Date(a.created || 0) - new Date(b.created || 0))
      break
    // #1642: "Name (A-Z/Z-A)" sorts by what the user sees — the display name
    // when set, else the slug (agentDisplayName). Sorting by the slug while
    // the row renders the label would order the list by an invisible key.
    case 'name_asc':
      rest.sort((a, b) => agentDisplayName(a).localeCompare(agentDisplayName(b)))
      break
    case 'name_desc':
      rest.sort((a, b) => agentDisplayName(b).localeCompare(agentDisplayName(a)))
      break
    case 'status':
      rest.sort((a, b) => (b.status === 'running' ? 1 : 0) - (a.status === 'running' ? 1 : 0))
      break
    case 'success_desc':
      rest.sort((a, b) => {
        const aStats = executionStats[a.name]
        const bStats = executionStats[b.name]
        // No-data-to-bottom tiebreak: agents with zero recorded tasks sink to
        // the bottom instead of floating on an implicit 0% success rate.
        const aHas = (aStats?.taskCount || 0) > 0 ? 1 : 0
        const bHas = (bStats?.taskCount || 0) > 0 ? 1 : 0
        if (aHas !== bHas) return bHas - aHas
        return (bStats?.successRate || 0) - (aStats?.successRate || 0)
      })
      break
  }

  return [...system, ...rest]
}
