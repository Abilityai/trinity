import { describe, it, expect } from 'vitest'
import {
  HEALTH_STATES, HEALTH_BADGE, healthLabel, rollupLine, attentionLines, trendPoints, metricStateLabel, logKindLabel,
} from '@/components/portal/projects/projectsUtils'

describe('ent#661 v3 — health', () => {
  it('every state has words and a badge; none reads as a state', () => {
    expect(HEALTH_STATES.map(healthLabel)).toEqual(['On track', 'At risk', 'Off track'])
    expect(HEALTH_STATES.map((s) => HEALTH_BADGE[s])).toEqual(['success', 'warning', 'danger'])
    expect(healthLabel(null)).toBe('No health yet')
  })
  it('a health update reads as Status in the log', () => {
    expect(logKindLabel('status')).toBe('Status')
  })
})

describe('rollupLine', () => {
  it('lists non-zero statuses in lattice order', () => {
    expect(rollupLine({ done: 2, active: 3, blocked: 1 })).toBe('3 active · 1 blocked · 2 done')
  })
  it('says so when there are no tasks', () => {
    expect(rollupLine({})).toBe('No tasks yet')
    expect(rollupLine(undefined)).toBe('No tasks yet')
  })
})

describe('attentionLines', () => {
  it('orders what a steward acts on, singular and plural', () => {
    const lines = attentionLines({
      awaiting_verification: [{ id: 'T-1' }], stuck: [{ id: 'T-2' }, { id: 'T-3' }], stale: [],
      open_asks: 1, health_due: true, health: null, quiet: true,
    })
    expect(lines.map((l) => l.text)).toEqual([
      '1 task to verify', '2 tasks blocked or waiting on a decision', '1 open ask', 'No health update yet',
      'Quiet for two weeks',
    ])
  })
  it('an overdue update, once one exists, says "due"', () => {
    expect(attentionLines({ health_due: true, health: { state: 'on-track' } })[0].text).toBe('Health update due')
  })
  it('nothing to do is an empty list', () => {
    expect(attentionLines({ stuck: [], stale: [], awaiting_verification: [], open_asks: 0 })).toEqual([])
  })
})

describe('trendPoints', () => {
  it('scales min..max into the box, low values at the bottom', () => {
    expect(trendPoints([0, 10], 96, 24, 2)).toBe('2,22 94,2')
  })
  it('a flat line sits in the middle', () => {
    expect(trendPoints([5, 5, 5], 96, 24, 2)).toBe('2,12 48,12 94,12')
  })
  it('fewer than two numbers draws nothing', () => {
    expect(trendPoints([7])).toBe('')
    expect(trendPoints(null)).toBe('')
    expect(trendPoints(['x', 3])).toBe('')
  })
})

describe('metricStateLabel — words the one stale rule, never re-decides it', () => {
  it.each([
    [{ declared: false, stale: false, latest: { value: 1 } }, 'No longer declared'],
    [{ declared: true, freshness: 'no_points', latest: null }, 'Not measured yet'],
    [{ declared: true, stale: true, latest: { value: 1 } }, 'Stale'],
    [{ declared: true, stale: false, freshness: 'fresh', latest: { value: 1 } }, ''],
    [{ declared: true, stale: null, freshness: 'no_cadence', latest: { value: 1 } }, ''],
  ])('%o → %s', (tile, label) => {
    expect(metricStateLabel(tile)).toBe(label)
  })
})
