/**
 * trinity-enterprise#836 — the rail's Asks tab: a door from the agent's page
 * into the Inbox, for the asks the sidebar already says are waiting.
 *
 * The decidable rules, pure:
 *   - the registry entry: door AGENT (asks are addressed to clients and
 *     platform users alike), a PRESENCE rule so the tab exists only while a
 *     participant has an open ask, last in the fixed order so its coming and
 *     going moves no other tab, the "updated" signal shape with a note and a
 *     COUNT, no empty state (presence makes it unreachable);
 *   - the rows are EXACTLY the Inbox's Action tab narrowed to the participants
 *     — the same items, the same urgency order, nothing re-sorted;
 *   - a row's link opens the Inbox on Action, filtered to the agent, with that
 *     ask selected (`?tab=action&from=<agent>&item=ask:<id>`);
 *   - the count reads the ONE feed the agent row's mark reads (`asksByAgent`
 *     over `openAsks`), so the two cannot disagree;
 *   - `⌥.` reaches it (it walks `visibleTabs`).
 */
import { describe, it, expect } from 'vitest'
import {
  RAIL_TABS, RAIL_TAB_ORDER, RAIL_DOORS, RAIL_SIGNAL_UPDATED,
  tabPassesDoor, visibleTabs, signalFor, railTitle, stripSegments, collapsedSignals,
} from '@/components/portal/portalRail'
import {
  ASKS_TAB_ID, railAskItems, askInboxRoute, asksSignalFrom, asksTabPresent,
} from '@/components/portal/portalRailAsks'
import { actionItems, filterByAgent, itemKey } from '@/components/portal/portalInbox'
import { asksByAgent, WORKSPACE_INBOX } from '@/components/portal/portalUtils'
import { nextRailTab } from '@/components/portal/portalKeymap'

const ask = (id, agent, over = {}) => ({
  id, agent_name: agent, kind: 'question', status: 'pending', title: `Ask ${id}`,
  created_at: '2026-10-07T10:00:00Z', priority: 'medium', ...over,
})
const ASKS = [
  ask('a1', 'scout', { created_at: '2026-10-07T09:00:00Z' }),
  ask('a2', 'scout', { priority: 'high' }),
  ask('a3', 'scout', { expires_at: '2026-10-07T12:00:00Z' }),
  ask('b1', 'bard'),
  ask('done', 'scout', { status: 'answered' }),
  ask('gone', 'bard', { status: 'expired' }),
]
const NOW = Date.parse('2026-10-07T11:00:00Z')
const tab = (id) => RAIL_TABS.find((t) => t.id === id)
const PLATFORM = { isPlatform: true, participants: ['scout'] }
const CLIENT = { isPlatform: false, participants: ['scout'] }
const counts = asksByAgent(ASKS.filter((a) => a.status === 'pending'))

describe('ent#836 — the Asks tab in the registry', () => {
  it('is registered last, AGENT door, "updated" signal, no empty state, its own icon', () => {
    expect(ASKS_TAB_ID).toBe('asks')
    expect(tab('asks')).toMatchObject({ door: RAIL_DOORS.AGENT, signal: RAIL_SIGNAL_UPDATED, icon: 'asks', empty: null })
    expect(typeof tab('asks').presence).toBe('function')
    expect(RAIL_TAB_ORDER[RAIL_TAB_ORDER.length - 1]).toBe('asks')
  })

  it('exists only while a participant has an open ask — the presence rule, on the one door gate', () => {
    const withAsks = { ...PLATFORM, askCounts: counts }
    expect(tabPassesDoor(tab('asks'), withAsks)).toBe(true)
    expect(tabPassesDoor(tab('asks'), PLATFORM)).toBe(false)                       // no counts at all
    expect(tabPassesDoor(tab('asks'), { ...PLATFORM, askCounts: {} })).toBe(false)
    expect(tabPassesDoor(tab('asks'), { ...PLATFORM, askCounts: { quiet: 0 } })).toBe(false)
    // Another agent's asks are not this conversation's business.
    expect(tabPassesDoor(tab('asks'), { ...PLATFORM, participants: ['quiet'], askCounts: counts })).toBe(false)
    // A room: any participant waiting is enough.
    expect(tabPassesDoor(tab('asks'), { ...PLATFORM, participants: ['quiet', 'bard'], askCounts: counts })).toBe(true)
    // Nobody in the chat yet (a room's first beat): no tab.
    expect(tabPassesDoor(tab('asks'), { ...PLATFORM, participants: [], askCounts: counts })).toBe(false)
    // Asks are addressed to clients too: the door is the conversation's shape, not who is asking.
    expect(tabPassesDoor(tab('asks'), { ...CLIENT, askCounts: counts })).toBe(true)
    // Strict: a stringly count is not a count.
    expect(asksTabPresent({ participants: ['scout'], askCounts: { scout: '2' } })).toBe(false)
  })

  it('joins the visible list last, and leaves it the moment the count is gone', () => {
    expect(visibleTabs(RAIL_TABS, { ...PLATFORM, askCounts: counts }).map((t) => t.id))
      .toEqual(['work', 'loops', 'canvas', 'files', 'info', 'asks'])
    expect(visibleTabs(RAIL_TABS, PLATFORM).map((t) => t.id)).toEqual(['work', 'loops', 'canvas', 'files', 'info'])
    expect(visibleTabs(RAIL_TABS, { ...CLIENT, askCounts: counts }).map((t) => t.id)).toEqual(['canvas', 'files', 'info', 'asks'])
  })

  it('⌥. reaches it — it is one more stop on the walk over the visible tabs', () => {
    const visible = visibleTabs(RAIL_TABS, { ...PLATFORM, askCounts: counts })
    expect(nextRailTab(visible, 'info')).toBe('asks')
    expect(nextRailTab(visible, 'asks')).toBe('work')
  })
})

describe('ent#836 — the rows are the Inbox Action list, narrowed', () => {
  it('a 1:1 lists exactly what Action shows filtered to that agent, in that order', () => {
    const expected = filterByAgent(actionItems(ASKS, NOW), 'scout')
    const rows = railAskItems(ASKS, ['scout'], NOW)
    expect(rows.map((r) => r.key)).toEqual(expected.map((r) => r.key))
    // Urgency order, not insertion: the ask expiring within the day leads.
    expect(rows[0].id).toBe('a3')
    // Pending only — the answered one is in no row.
    expect(rows.some((r) => r.id === 'done')).toBe(false)
    expect(rows.every((r) => r.type === 'ask' && r.agent_name === 'scout')).toBe(true)
  })

  it('a room lists every participant\'s asks and nobody else\'s, still in Action order', () => {
    const rows = railAskItems(ASKS, ['bard', 'scout'], NOW)
    const expected = actionItems(ASKS, NOW).filter((r) => ['bard', 'scout'].includes(r.agent_name))
    expect(rows.map((r) => r.key)).toEqual(expected.map((r) => r.key))
    expect(railAskItems(ASKS, ['quiet'], NOW)).toEqual([])
    expect(railAskItems(null, ['scout'], NOW)).toEqual([])
    expect(railAskItems(ASKS, [], NOW)).toEqual([])
  })

  it('a row opens the Inbox on Action, filtered to the agent, with that ask selected', () => {
    const [row] = railAskItems(ASKS, ['bard'], NOW)
    expect(askInboxRoute(row)).toEqual({
      path: WORKSPACE_INBOX,
      query: { tab: 'action', from: 'bard', item: itemKey('ask', 'b1') },
    })
    // Never `?agent=` — that is a stage key and would open the agent's page instead.
    expect(Object.keys(askInboxRoute(row).query)).not.toContain('agent')
    expect(askInboxRoute(null)).toBeNull()
  })
})

describe('ent#836 — the count is the agent row\'s mark', () => {
  it('reads the same feed, summed over the participants, and is null with nothing waiting', () => {
    expect(asksSignalFrom(counts, ['scout'])).toEqual({ live: 0, updated: true, agents: ['scout'], note: '3 asks', count: 3 })
    expect(asksSignalFrom(counts, ['bard'])).toEqual({ live: 0, updated: true, agents: ['bard'], note: '1 ask', count: 1 })
    expect(asksSignalFrom(counts, ['scout', 'bard', 'quiet'])).toMatchObject({ agents: ['scout', 'bard'], count: 4, note: '4 asks' })
    expect(asksSignalFrom(counts, ['quiet'])).toBeNull()
    expect(asksSignalFrom({}, ['scout'])).toBeNull()
    expect(asksSignalFrom(null, ['scout'])).toBeNull()
  })

  it('the rail\'s signal reader carries the count through, and the strip says it in words', () => {
    const signals = { asks: asksSignalFrom(counts, ['scout']) }
    const sig = signalFor(signals, tab('asks'))
    expect(sig.count).toBe(3)
    expect(sig.note).toBe('3 asks')
    expect(railTitle(tab('asks'), sig)).toBe('Asks · 3 asks')
    const visible = visibleTabs(RAIL_TABS, { ...PLATFORM, askCounts: counts })
    expect(stripSegments(signals, visible)).toEqual([{ id: 'asks', shape: 'updated', text: 'Asks · 3 asks' }])
    expect(collapsedSignals(signals, visible).find((s) => s.id === 'asks')).toMatchObject({ shape: 'updated', note: '3 asks', title: 'Asks · 3 asks' })
    // A signal with no count keeps its exact prior shape — nothing else grows a key.
    expect(signalFor({ work: { live: 1, agents: ['scout'] } }, 'work')).toEqual({ live: 1, updated: false, agents: ['scout'] })
    expect(signalFor({ asks: { updated: true, count: 0 } }, 'asks')).not.toHaveProperty('count')
    expect(signalFor({ asks: { updated: true, count: '3' } }, 'asks')).not.toHaveProperty('count')
  })
})
