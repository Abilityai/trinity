/**
 * #2424 — the Workspace said "2 asks are waiting" and gave you no way to find them.
 *
 * Three failures compounded, and they are only worth fixing together: the header
 * badge reported the wrong unit, the agent row carried no ask indicator at all,
 * and the agent could be collapsed out of the roster entirely. A person saw a
 * count, had no row to click, and the agent was not on screen.
 *
 * Everything decidable lives in `portalUtils` because vitest runs
 * `environment: 'node'` with no component-mount harness — a rule that lived
 * inside the SFC would be one no test could reach, which is how all three of
 * these shipped.
 */
import { describe, it, expect } from 'vitest'
import fs from 'fs'
import path from 'path'
import {
  asksByAgent,
  askBadgeTitle,
  agentRowTitle,
  visibleAgentRows,
  AGENT_COLLAPSE_LIMIT,
} from '../../src/components/portal/portalUtils.js'

const SIDEBAR = path.resolve(__dirname, '../../src/components/portal/PortalSidebar.vue')
const sidebarSource = () => fs.readFileSync(SIDEBAR, 'utf8')

// ---------------------------------------------------------------------------
// 1. The unit the badge reports
// ---------------------------------------------------------------------------
describe('#2424 part 1 — the badge counts asks, so it must say asks', () => {
  it('says "asks" for many, never "agents"', () => {
    const title = askBadgeTitle(2)
    expect(title).toContain('2')
    expect(title).toMatch(/asks/i)
    // The reported bug verbatim: two asks on ONE agent rendered as
    // "2 agents are waiting on your answer".
    expect(title).not.toMatch(/agents/i)
  })

  it('is singular for one', () => {
    expect(askBadgeTitle(1)).toMatch(/\b1 ask\b/i)
    expect(askBadgeTitle(1)).not.toMatch(/asks/i)
  })

  it('says nothing at zero — an empty title is worse than no attribute', () => {
    expect(askBadgeTitle(0)).toBe('')
  })

  it('the SFC no longer builds that sentence inline', () => {
    // The old bug was a template literal in the template. If it comes back, the
    // pure function above stops being the single source of the wording.
    expect(sidebarSource()).not.toMatch(/agents are.*waiting on your answer/)
  })
})

// ---------------------------------------------------------------------------
// 2. Per-agent counts, and the row that shows them
// ---------------------------------------------------------------------------
describe('#2424 part 2 — asks are attributable to an agent', () => {
  it('groups by agent', () => {
    expect(asksByAgent([
      { agent_name: 'ws-sage' },
      { agent_name: 'ws-sage' },
      { agent_name: 'scout' },
    ])).toEqual({ 'ws-sage': 2, scout: 1 })
  })

  it('survives junk without throwing — the sidebar must not blank on a bad row', () => {
    expect(asksByAgent(null)).toEqual({})
    expect(asksByAgent(undefined)).toEqual({})
    expect(asksByAgent([null, {}, { agent_name: '' }, { agent_name: 'a' }])).toEqual({ a: 1 })
  })

  it('the row title names the pending decision', () => {
    const t = agentRowTitle({ label: 'ws-sage', name: 'ws-sage', askCount: 2 })
    expect(t).toMatch(/2 asks/i)
    // The observed title was the bare "Open ws-sage" while two asks waited.
    expect(t).not.toBe('Open ws-sage')
  })

  it('is singular for one ask', () => {
    expect(agentRowTitle({ label: 'a', name: 'a', askCount: 1 })).toMatch(/\b1 ask\b/i)
  })

  it('keeps unread replies as a SEPARATE fact, never summed', () => {
    const t = agentRowTitle({ label: 'a', name: 'a', askCount: 2, unread: 3 })
    expect(t).toMatch(/2 asks/i)
    expect(t).toMatch(/3 unread/i)
    // "5" would mean the two counts were added — the exact conflation
    // PortalSidebar.vue's own comment forbids.
    expect(t).not.toMatch(/\b5\b/)
  })

  it('still renders the display label with its slug, and the availability chip', () => {
    const t = agentRowTitle({
      label: 'Sage', name: 'ws-sage', askCount: 0, unread: 0, chipTitle: 'This agent is stopped',
    })
    expect(t).toContain('Sage')
    expect(t).toContain('ws-sage')
    expect(t).toContain('This agent is stopped')
  })

  it('falls back to "Open <name>" when nothing is pending', () => {
    expect(agentRowTitle({ label: 'a', name: 'a' })).toBe('Open a')
  })

  it('the row renders an ask badge, tokenised and distinct from the unread pill', () => {
    const src = sidebarSource()
    expect(src).toMatch(/askCountFor\(/)
    // status-urgent is the platform's "waiting on you" token — the same one the
    // operator NavBar's pending-operator-queue badge uses. Not amber: that maps
    // to `state-autonomous`, which is an operating mode, not a pending decision.
    expect(src).toContain('bg-status-urgent-500')
    // Raw palette classes are ratcheted to zero for new code (design contract).
    expect(src).not.toContain('bg-amber-500')
  })
})

// ---------------------------------------------------------------------------
// 3. A blocked agent is never collapsed out of view
// ---------------------------------------------------------------------------
describe('#2424 part 3 — the agent you need is on screen', () => {
  const roster = Array.from({ length: 12 }, (_, i) => ({ name: `a${String(i).padStart(2, '0')}` }))

  it('collapsed, shows the first N in roster order when nothing is pending', () => {
    const out = visibleAgentRows(roster, { expanded: false, askCounts: {} })
    expect(out).toHaveLength(AGENT_COLLAPSE_LIMIT)
    expect(out.map((a) => a.name)).toEqual(['a00', 'a01', 'a02', 'a03', 'a04'])
  })

  it('lifts an ask-bearing agent that would otherwise be hidden', () => {
    // The reported case: ws-sage was 11th of 12, so on a fresh load the one row
    // the person needed was behind the "show more" toggle.
    const out = visibleAgentRows(roster, { expanded: false, askCounts: { a10: 2 } })
    expect(out.map((a) => a.name)).toContain('a10')
  })

  it('keeps roster order rather than floating the ask to the top', () => {
    // Re-sorting on a transient count makes rows move under the cursor between
    // refreshes — the layout-stability rule the availability chip already obeys.
    const out = visibleAgentRows(roster, { expanded: false, askCounts: { a10: 2 } })
    const names = out.map((a) => a.name)
    expect(names.indexOf('a10')).toBe(names.length - 1)
    expect(names.slice(0, 5)).toEqual(['a00', 'a01', 'a02', 'a03', 'a04'])
  })

  it('does not duplicate an ask-bearing agent already inside the slice', () => {
    const out = visibleAgentRows(roster, { expanded: false, askCounts: { a02: 1 } })
    expect(out.filter((a) => a.name === 'a02')).toHaveLength(1)
    expect(out).toHaveLength(AGENT_COLLAPSE_LIMIT)
  })

  it('expanded, shows everything', () => {
    expect(visibleAgentRows(roster, { expanded: true, askCounts: { a10: 2 } })).toHaveLength(12)
  })

  it('short rosters are untouched', () => {
    const three = roster.slice(0, 3)
    expect(visibleAgentRows(three, { expanded: false, askCounts: {} })).toHaveLength(3)
  })

  it('tolerates a missing roster', () => {
    expect(visibleAgentRows(null, { expanded: false, askCounts: {} })).toEqual([])
    expect(visibleAgentRows(roster, {})).toHaveLength(AGENT_COLLAPSE_LIMIT)
  })

  it('the SFC drives its rows through the shared rule', () => {
    const src = sidebarSource()
    expect(src).toMatch(/visibleAgentRows\(/)
    // The raw slice was the bug; it must not survive alongside the fix.
    expect(src).not.toMatch(/roster\.slice\(0,\s*AGENT_COLLAPSE_LIMIT\)/)
  })

  it('the "show more" affordance still keys off the full roster', () => {
    // With ask-lifting, the visible count can exceed the limit — so a toggle
    // gated on `shown.length` would vanish exactly when an ask is pending.
    expect(sidebarSource()).toMatch(/roster\.length > AGENT_COLLAPSE_LIMIT/)
  })
})
