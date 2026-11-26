/**
 * trinity-enterprise#1233 — the milestone row's rules, as pure functions.
 *
 * The server derives the state and reads each target through the platform's
 * one stale rule; these decide only what the row shows. The rule that matters
 * most: a reading that is overdue is never drawn as a current number.
 */
import { describe, it, expect } from 'vitest'
import {
  milestoneState, milestoneNumberLine, isOverdueReading, milestoneTasksLine, milestoneNeedsYou,
  metricsOutsideMilestones, tasksForMilestone, milestoneLanes, validateMilestone,
} from '@/components/portal/projects/projectsUtils'

const target = (over = {}) => ({
  agent_name: 'scout', metric: 'partners', label: 'Design partners', unit: '', target: 10,
  direction: 'up_good', actual: 4, actual_at: '2026-11-20T10:00:00Z', stale: false, freshness: 'fresh',
  agent_inactive: false, met: false, ...over,
})
const ms = (over = {}) => ({ id: 'ms_1', name: 'Ten partners', state: 'in_progress', targets: [target()], worst: target(), tasks: { total: 0, done: 0, attention: 0 }, ...over })

describe('milestoneState', () => {
  it('maps each server state to a label and a badge', () => {
    expect(milestoneState({ state: 'reached' })).toMatchObject({ key: 'reached', label: 'Reached', badge: 'success' })
    expect(milestoneState({ state: 'in_progress' })).toMatchObject({ key: 'in_progress', badge: 'info' })
    expect(milestoneState({ state: 'not_started' })).toMatchObject({ key: 'not_started', badge: 'neutral' })
  })
  it('an unknown state never reads as reached', () => {
    expect(milestoneState({ state: 'bogus' }).key).toBe('not_started')
    expect(milestoneState(null).key).toBe('not_started')
  })
})

describe('isOverdueReading', () => {
  it('is true for a stale flag or a stale freshness, false for a fresh one', () => {
    expect(isOverdueReading(target({ stale: true }))).toBe(true)
    expect(isOverdueReading(target({ stale: null, freshness: 'stale' }))).toBe(true)
    expect(isOverdueReading(target())).toBe(false)
    expect(isOverdueReading(null)).toBe(false)
  })
})

describe('milestoneNumberLine', () => {
  it('a fresh reading is current: value, relation and target', () => {
    const line = milestoneNumberLine(ms())
    expect(line).toMatchObject({ value: '4', target: '10', relation: '≥', reading: 'current', greyed: false, met: false, note: '' })
  })
  it('a stale reading is greyed and says it is stale, never current', () => {
    const t = target({ stale: true, freshness: 'stale' })
    const line = milestoneNumberLine(ms({ worst: t, targets: [t] }))
    expect(line.reading).toBe('stale')
    expect(line.greyed).toBe(true)
    expect(line.met).toBe(null)
    expect(line.note).toMatch(/stale/i)
  })
  it('no point yet, an inactive agent and an unreadable metric each say so', () => {
    const none = target({ actual: null, actual_at: null, freshness: 'never' })
    expect(milestoneNumberLine(ms({ worst: none })).reading).toBe('none')
    const gone = target({ agent_inactive: true, actual: null, freshness: 'unknown' })
    expect(milestoneNumberLine(ms({ worst: gone }))).toMatchObject({ reading: 'inactive', value: '—', greyed: true })
    const unread = target({ actual: null, freshness: 'unknown' })
    expect(milestoneNumberLine(ms({ worst: unread })).reading).toBe('unknown')
  })
  it('down and hold directions use their relation; extra targets are counted', () => {
    const t = target({ direction: 'down_good' })
    expect(milestoneNumberLine(ms({ worst: t, targets: [t, target(), target()] }))).toMatchObject({ relation: '≤', more: 2 })
    expect(milestoneNumberLine(ms({ worst: target({ direction: 'neutral' }) })).relation).toBe('=')
  })
  it('a milestone without a target has no number', () => {
    expect(milestoneNumberLine(ms({ worst: null, targets: [] }))).toBe(null)
  })
})

describe('milestoneTasksLine and milestoneNeedsYou', () => {
  it('counts done of total, empty with no task', () => {
    expect(milestoneTasksLine(ms({ tasks: { total: 5, done: 3 } }))).toBe('3 of 5 tasks done')
    expect(milestoneTasksLine(ms({ tasks: { total: 1, done: 0 } }))).toBe('0 of 1 task done')
    expect(milestoneTasksLine(ms())).toBe('')
  })
  const proj = (can) => ({ can, archived_at: null })
  it('suggests marking reached only to an editor', () => {
    const m = ms({ suggest_reached: true })
    expect(milestoneNeedsYou(m, proj({ edit: true, contribute: true }))).toMatch(/mark it reached/)
    expect(milestoneNeedsYou(m, proj({ edit: false, contribute: true }))).toBe('')
  })
  it('names tasks needing a look to a contributor; nothing once reached or archived', () => {
    const m = ms({ tasks: { total: 3, done: 1, attention: 2 } })
    expect(milestoneNeedsYou(m, proj({ contribute: true }))).toBe('2 tasks need a look')
    expect(milestoneNeedsYou({ ...m, state: 'reached' }, proj({ contribute: true }))).toBe('')
    expect(milestoneNeedsYou(m, { can: { contribute: true }, archived_at: '2026-01-01' })).toBe('')
  })
})

describe('filters and lanes', () => {
  const tasks = [{ id: 'T-1', milestone: 'ms_1' }, { id: 'T-2', milestone: null }, { id: 'T-3', milestone: 'ms_2' }, { id: 'T-4', milestone: 'ms_gone' }]
  it('filters tasks by milestone, by none, or not at all', () => {
    expect(tasksForMilestone(tasks, 'ms_1').map((t) => t.id)).toEqual(['T-1'])
    expect(tasksForMilestone(tasks, 'none').map((t) => t.id)).toEqual(['T-2'])
    expect(tasksForMilestone(tasks, '')).toHaveLength(4)
  })
  it('lanes follow the goal order, then everything under no known milestone', () => {
    const lanes = milestoneLanes(tasks, [{ id: 'ms_2', name: 'B' }, { id: 'ms_1', name: 'A' }])
    expect(lanes.map((l) => [l.key, l.rows.map((t) => t.id)])).toEqual([
      ['ms_2', ['T-3']], ['ms_1', ['T-1']], ['none', ['T-2', 'T-4']],
    ])
  })
  it('the Metrics card keeps only metrics no milestone targets', () => {
    const metrics = [{ agent_name: 'scout', name: 'partners' }, { agent_name: 'scout', name: 'churn' }]
    expect(metricsOutsideMilestones(metrics, [ms()]).map((m) => m.name)).toEqual(['churn'])
    expect(metricsOutsideMilestones(metrics, [])).toHaveLength(2)
  })
})

describe('validateMilestone', () => {
  it('names a missing name, a non-number target and a duplicate target', () => {
    expect(validateMilestone({ name: ' ', targets: [] }).name).toMatch(/Name the milestone/)
    const errs = validateMilestone({ name: 'A', targets: [
      { agent_name: 'scout', metric: 'p', target: 'ten' },
      { agent_name: 'scout', metric: 'q', target: '3' },
      { agent_name: 'scout', metric: 'q', target: '4' },
    ] })
    expect(errs.target_0).toMatch(/number/)
    expect(errs.target_1).toBeUndefined()
    expect(errs.target_2).toMatch(/already/)
  })
})
