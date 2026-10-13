/**
 * `utils/agentTabs.js::buildTabs` — the Dashboard tab's gate (ent#479).
 *
 * `buildTabs` used to live inside `AgentDetail.vue` and be tested by pulling
 * its TEXT out of the SFC and `new Function`-ing it. That executed the real
 * code, but only because the spec could not import an SFC; ent#479 moved the
 * function into its own module, so both specs now import the shipped thing.
 *
 * What is new here is the gate itself. Before ent#479 the Dashboard tab
 * appeared only for an agent with a cached `dashboard.yaml`, which hid it from
 * exactly the agents "declared metrics are the default tiles" targets: an
 * agent that declares `metrics:` and records points has numbers to show and no
 * dashboard file to show them in.
 */
import { describe, expect, it } from 'vitest'
import { buildTabs } from '../../src/utils/agentTabs'

const ids = (flags) => buildTabs(flags).map((t) => t.id)

describe('ent#479 the Dashboard tab appears for EITHER gate', () => {
  it('appears for a dashboard.yaml with no declared metrics (the old behaviour)', () => {
    expect(ids({ hasDashboardFlag: true })).toContain('dashboard')
  })

  it('appears for declared metrics with NO dashboard.yaml — the point of the issue', () => {
    expect(ids({ hasDeclaredMetrics: true })).toContain('dashboard')
  })

  it('appears once, not twice, when both are true', () => {
    const shown = ids({ hasDashboardFlag: true, hasDeclaredMetrics: true })
    expect(shown.filter((id) => id === 'dashboard')).toHaveLength(1)
  })

  it('stays hidden when neither is true — the tab is still gated', () => {
    expect(ids({})).not.toContain('dashboard')
    expect(ids({ hasDashboardFlag: false, hasDeclaredMetrics: false })).not.toContain('dashboard')
  })

  it('keeps its position: after Tasks/Chat, before Reports', () => {
    const shown = ids({ hasDeclaredMetrics: true })
    expect(shown.indexOf('dashboard')).toBeGreaterThan(shown.indexOf('chat'))
    expect(shown.indexOf('dashboard')).toBeLessThan(shown.indexOf('reports'))
  })

  it('is not owner-gated — a shared viewer sees the numbers too', () => {
    expect(ids({ hasDeclaredMetrics: true, canShare: false })).toContain('dashboard')
  })

  it('is not hidden for the system agent', () => {
    expect(ids({ hasDeclaredMetrics: true, isSystem: true })).toContain('dashboard')
  })
})

describe('buildTabs stays a pure, defaulted builder', () => {
  it('runs with no arguments at all (the pre-load frame)', () => {
    expect(ids()).toContain('overview')
  })

  it('returns a fresh array each call — no shared mutable tab list', () => {
    const a = buildTabs({ hasDeclaredMetrics: true })
    const b = buildTabs({ hasDeclaredMetrics: true })
    expect(a).not.toBe(b)
    expect(a).toEqual(b)
  })
})
