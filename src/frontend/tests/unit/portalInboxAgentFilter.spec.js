/**
 * trinity-enterprise#610 PR A2, §3g L6 C2 — Action can be narrowed to one agent.
 *
 * With 21 asks from 7 agents, the reader wants "what does scout need from me".
 * A second, dense strip of agent facets sits under the tabs on ACTION ONLY, when
 * two or more agents are waiting on you; the choice lives in `?from=<agent>`
 * (never `?agent=`, a stage key). Pure rules here; the mount is in
 * portalInbox.mount.spec.js.
 */
import { describe, it, expect } from 'vitest'
import {
  agentFacets, filterByAgent, activeAgentFilter, normalizeFrom, FROM_ALL, listHeadLabel,
} from '@/components/portal/portalInbox'

const row = (id, agent) => ({ key: `ask:${id}`, type: 'ask', id, agent_name: agent })

describe('C2 — agent facets', () => {
  it('none for one agent; "All agents" then each agent, most asks first, with counts', () => {
    expect(agentFacets([row('a', 'scout'), row('b', 'scout')])).toEqual([])
    const f = agentFacets([row('a', 'scout'), row('b', 'relay'), row('c', 'relay')], { relay: 'Relay Bot' })
    expect(f.map((t) => t.id)).toEqual([FROM_ALL, 'relay', 'scout'])
    expect(f[0]).toMatchObject({ label: 'All agents', badge: 3 })
    expect(f[1]).toMatchObject({ label: 'Relay Bot', badge: 2 })
    expect(f[2]).toMatchObject({ label: 'scout', badge: 1 })
  })

  it('an active filter whose agent has no asks left keeps its facet (so it can be cleared)', () => {
    const f = agentFacets([row('a', 'scout')], {}, 'relay')
    expect(f.map((t) => t.id)).toEqual([FROM_ALL, 'scout', 'relay'])
    expect(f[2].badge).toBeNull()
  })

  it('an active filter keeps its strip even when NO asks are left (A2 r1, Codex C5)', () => {
    // relay's last ask was just answered and is still selected: the filter holds
    // (activeAgentFilter), so the strip must too — or it cannot be cleared.
    const f = agentFacets([], {}, 'relay')
    expect(f.map((t) => t.id)).toEqual([FROM_ALL, 'relay'])
    expect(f[0].badge).toBe(0)
  })

  it('facet counts are neutral and name what they count (A2 r1 design P1: green read as "done")', () => {
    const f = agentFacets([row('a', 'scout'), row('b', 'relay'), row('c', 'relay')], { relay: 'Relay Bot' })
    expect(f.every((t) => t.badgeVariant === 'neutral')).toBe(true)
    expect(f[0].badgeLabel).toBe('All agents, 3 asks')
    expect(f[1].badgeLabel).toBe('Relay Bot, 2 asks')
    expect(f[2].badgeLabel).toBe('scout, 1 ask')
  })

  it('the list heading names the agent it is narrowed to (A2 r1: the chip can sit in More)', () => {
    const items = [{ type: 'ask', id: 'a' }, { type: 'ask', id: 'b' }]
    expect(listHeadLabel('action', items)).toBe('2 asks')
    expect(listHeadLabel('action', items, 'Relay Bot')).toBe('2 asks from Relay Bot')
    expect(listHeadLabel('action', [], 'Relay Bot')).toBe('All caught up')
  })

  it('filterByAgent narrows; no filter is the identity', () => {
    const rows = [row('a', 'scout'), row('b', 'relay')]
    expect(filterByAgent(rows, 'relay').map((r) => r.id)).toEqual(['b'])
    expect(filterByAgent(rows, null)).toBe(rows)
  })

  it('the filter holds while its agent has asks, or its ended ask is still selected; then it clears', () => {
    const rows = [row('a', 'scout'), row('b', 'relay')]
    expect(activeAgentFilter('relay', rows, null)).toBe('relay')
    expect(activeAgentFilter('ghost', rows, null)).toBeNull()
    // relay's last ask was just answered: gone from the fresh rows, still in the pane.
    expect(activeAgentFilter('relay', [row('a', 'scout')], { key: 'ask:b', agent_name: 'relay' })).toBe('relay')
    expect(activeAgentFilter('relay', [row('a', 'scout')], { key: 'ask:a', agent_name: 'scout' })).toBeNull()
  })

  it('normalizeFrom: one non-empty string, never the sentinel', () => {
    expect(normalizeFrom('scout')).toBe('scout')
    expect(normalizeFrom(['scout'])).toBeNull()
    expect(normalizeFrom('')).toBeNull()
    expect(normalizeFrom(FROM_ALL)).toBeNull()
  })
})
