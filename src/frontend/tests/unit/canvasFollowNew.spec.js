/**
 * #3218 — which canvas a refresh brought that the panel has not seen before.
 *
 * The panel follows a canvas the agent just created; this is the rule that
 * names it, pure so every edge is reachable without a mount (the mounted
 * behaviour is pinned in canvasPanelFollowNew.mount.spec.js).
 */
import { describe, it, expect } from 'vitest'
import { canvasesAppeared } from '../../src/components/canvas/canvasUtils.js'

const row = (id, updated, pinned = false) => ({ canvas_id: id, updated_at: updated, pinned })

describe('canvasesAppeared (#3218)', () => {
  it('names nothing on the first load — every row would otherwise be "new"', () => {
    expect(canvasesAppeared(null, [row('a', '2026-10-01T00:00:00Z')])).toBeNull()
  })

  it('names nothing when the refresh brought no new id', () => {
    const known = new Set(['a', 'b'])
    expect(canvasesAppeared(known, [row('a', '2026-10-02T00:00:00Z'), row('b', '2026-10-01T00:00:00Z')])).toBeNull()
  })

  it('names the id that appeared, not the first row — a pinned canvas sorting ahead does not win', () => {
    const known = new Set(['pinned'])
    const rows = [row('pinned', '2026-10-01T00:00:00Z', true), row('fresh', '2026-09-30T00:00:00Z')]
    expect(canvasesAppeared(known, rows)).toBe('fresh')
  })

  it('picks the most recently updated when several arrive at once', () => {
    const known = new Set(['old'])
    const rows = [row('old', '2026-10-05T00:00:00Z'), row('n1', '2026-10-03T00:00:00Z'),
      row('n2', '2026-10-04T00:00:00Z'), row('n3', '2026-10-02T00:00:00Z')]
    expect(canvasesAppeared(known, rows)).toBe('n2')
  })

  it('a new canvas with no timestamp still counts, and loses to one with a timestamp', () => {
    const known = new Set(['old'])
    expect(canvasesAppeared(known, [row('old', 'x'), row('n1', undefined)])).toBe('n1')
    expect(canvasesAppeared(known, [row('old', 'x'), row('n1', undefined), row('n2', '2026-10-01T00:00:00Z')])).toBe('n2')
  })

  it('names nothing when the whole list was replaced — another agent\'s list is a fresh load, not an arrival', () => {
    // The mounts do not key CanvasPanel per agent, so switching agents swaps
    // the list under the same instance; that must keep "first row", not jump
    // to the most recently updated of the new agent's canvases.
    const known = new Set(['a', 'b'])
    expect(canvasesAppeared(known, [row('x', '2026-10-05T00:00:00Z'), row('y', '2026-10-06T00:00:00Z')])).toBeNull()
  })

  it('is hostile-input safe', () => {
    expect(canvasesAppeared(new Set(), null)).toBeNull()
    expect(canvasesAppeared(new Set(), [null, {}, { canvas_id: '' }])).toBeNull()
  })
})
