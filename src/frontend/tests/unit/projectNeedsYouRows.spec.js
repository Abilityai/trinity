/**
 * #3574 — a project's "Needs you" rows, the pure rule (projectsUtils).
 *   - only the project's own ids, only pending, in urgency order;
 *   - a row the person opened stays after its ask ends; one in its Undo
 *     window is forced open; a non-pending ask nobody opened is dropped;
 *   - the title falls back to the question (portalInbox.askItem's rule).
 */
import { describe, it, expect } from 'vitest'
import { projectNeedsYouRows } from '@/components/portal/projects/projectsUtils'

const ask = (over = {}) => ({
  id: 'q1', agent_name: 'scout', kind: 'question', title: 'Mine here', question: 'Long body',
  status: 'pending', created_at: '2026-09-30T10:00:00Z', ...over,
})

describe('projectNeedsYouRows', () => {
  it('keeps only the project ids that are pending, in urgency order', () => {
    const asks = [
      ask({ id: 'later', created_at: '2026-09-30T12:00:00Z' }),
      ask({ id: 'elsewhere' }),
      ask({ id: 'urgent', priority: 'high', created_at: '2026-09-30T13:00:00Z' }),
      ask({ id: 'earlier', created_at: '2026-09-30T09:00:00Z' }),
      ask({ id: 'ended', status: 'answered' }),
    ]
    const rows = projectNeedsYouRows(asks, ['later', 'urgent', 'earlier', 'ended', 'missing'])
    expect(rows.map((r) => r.id)).toEqual(['urgent', 'earlier', 'later'])
    expect(rows.every((r) => r.open === false)).toBe(true)
  })

  it('an opened row stays after its ask ends; an Undo-window row is forced open', () => {
    const asks = [ask({ id: 'a', status: 'answered' }), ask({ id: 'b', status: 'dismissed' }), ask({ id: 'c' })]
    const rows = projectNeedsYouRows(asks, ['a', 'b', 'c'], { expanded: { a: true }, dismissals: { b: { until: 1 } } })
    const byId = Object.fromEntries(rows.map((r) => [r.id, r.open]))
    expect(byId).toEqual({ a: true, b: true, c: false })
  })

  it('an ended ask nobody opened is dropped; the title falls back to the question', () => {
    const asks = [ask({ id: 'x', title: '', question: 'Just a question' }), ask({ id: 'y', status: 'expired' })]
    const rows = projectNeedsYouRows(asks, ['x', 'y'])
    expect(rows.map((r) => r.id)).toEqual(['x'])
    expect(rows[0].title).toBe('Just a question')
  })

  it('no ids, no asks, or malformed input yields no rows', () => {
    expect(projectNeedsYouRows([ask()], [])).toEqual([])
    expect(projectNeedsYouRows(null, ['q1'])).toEqual([])
    expect(projectNeedsYouRows([null, ask()], [null, 'q1'])).toHaveLength(1)
  })
})
