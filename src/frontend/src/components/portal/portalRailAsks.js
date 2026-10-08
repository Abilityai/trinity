// trinity-enterprise#836 — the rail's Asks tab, the decidable rules. Pure.
//
// The tab is a door into the Inbox, not a second answer surface (the 2026-09-30
// ruling on ent#610 stands: answering happens in the Inbox). So it invents no
// list of its own: its rows are the Inbox's Action tab — the SAME builder,
// `actionItems`, over the SAME feed, `openAsks` — narrowed to the chat's
// participants, and a row's link is the Inbox's own addressing (`?tab=`,
// `?from=`, `?item=`). Its count is the agent row's "needs you" mark, read
// through `asksByAgent` exactly as the sidebar reads it, so the tab and the
// mark cannot disagree.

import { actionItems, itemKey } from './portalInbox'
import { asksHomeRoute } from './portalUtils'

export const ASKS_TAB_ID = 'asks'

const names = (participants) => (Array.isArray(participants) ? participants : [])
  .filter((p) => typeof p === 'string' && p.trim())
  .map((p) => p.trim())

/**
 * The rows: Action's items (pending only, urgency order — `askUrgencyCompare`)
 * kept only for the participants. A filter over the finished list, never a
 * re-sort, so a 1:1 lists exactly `filterByAgent(actionItems(asks), agent)`.
 */
export function railAskItems(openAsks, participants, now = Date.now()) {
  const keep = new Set(names(participants))
  if (!keep.size) return []
  return actionItems(openAsks, now).filter((it) => keep.has(it.agent_name))
}

/**
 * Where a row goes: the Inbox on Action, filtered to the ask's agent, with the
 * ask selected. Built on `asksHomeRoute` (the agent's asks home, `?from=` —
 * never `?agent=`, which is a stage key) plus the Inbox's `?item=` key.
 */
export function askInboxRoute(item) {
  if (!item || !item.id || !item.agent_name) return null
  const route = asksHomeRoute(item.agent_name)
  return { ...route, query: { ...route.query, item: itemKey('ask', item.id) } }
}

/**
 * The tab's signal: the "updated" dot, the agents waiting, the count in words
 * for the tooltip and the mobile strip, and the count itself for the open
 * strip's badge. `askCounts` is `asksByAgent(openAsks)` — the sidebar's own
 * projection — summed over the participants. Null with nothing waiting, so
 * the shell adds no key to the signals map.
 */
export function asksSignalFrom(askCounts, participants) {
  const counts = askCounts && typeof askCounts === 'object' ? askCounts : {}
  const agents = names(participants).filter((p) => Number.isInteger(counts[p]) && counts[p] > 0)
  if (!agents.length) return null
  const count = agents.reduce((n, p) => n + counts[p], 0)
  return { live: 0, updated: true, agents, note: `${count} ${count === 1 ? 'ask' : 'asks'}`, count }
}

export { asksTabPresent } from './portalRail'
