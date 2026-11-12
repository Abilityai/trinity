import { describe, it, expect } from 'vitest'
import {
  computeEdges,
  orthogonalPath,
  paintTiers,
  BUS_DROP,
  LANE_X,
  ELBOW_R,
  ARRIVAL_SPREAD,
  ZONE_CHROME,
  GAP_X,
  GAP_Y,
} from '@/utils/gridOrg'
import { CELL_W, CELL_H, cellXY } from '@/utils/gridLayout'

/**
 * #2066: on the Grid org overlay, every reporting line leaves the manager
 * tile at bottom-centre and enters the report tile at top-centre, whatever
 * the tiles' relative lattice positions, and is routed so it reads as an org
 * chart (orthogonal, through the gaps, siblings on one bus).
 */

const agent = (name, tags = []) => ({ name, tags, status: 'running' })

const bottomCentre = (p) => {
  const [x, y] = cellXY(p.c, p.r)
  return { x: x + CELL_W / 2, y: y + CELL_H }
}
const topCentre = (p) => {
  const [x, y] = cellXY(p.c, p.r)
  return { x: x + CELL_W / 2, y }
}

/** Parse our own path dialect (M / L / Q, absolute) into a point list. */
function pathPoints(d) {
  const nums = d.match(/[MLQ]|-?\d+(?:\.\d+)?/g)
  const pts = []
  let i = 0
  while (i < nums.length) {
    const cmd = nums[i++]
    const n = cmd === 'Q' ? 4 : 2
    const vals = nums.slice(i, i + n).map(Number)
    i += n
    for (let k = 0; k < vals.length; k += 2) pts.push({ x: vals[k], y: vals[k + 1], cmd })
  }
  return pts
}

/** Consecutive point pairs. Q control points sit ON the corner, so the
 *  control polygon is the exact orthogonal polyline the curve rounds. */
function segments(d) {
  const pts = pathPoints(d)
  const segs = []
  for (let i = 1; i < pts.length; i++) segs.push([pts[i - 1], pts[i]])
  return segs
}

/** True when an axis-aligned segment enters a rect's open interior. */
function segmentEntersRect([a, b], r) {
  const x0 = Math.min(a.x, b.x)
  const x1 = Math.max(a.x, b.x)
  const y0 = Math.min(a.y, b.y)
  const y1 = Math.max(a.y, b.y)
  return Math.max(x0, r.x0) < Math.min(x1, r.x1) || (x0 === x1 && x0 > r.x0 && x0 < r.x1)
    ? Math.max(y0, r.y0) < Math.min(y1, r.y1) || (y0 === y1 && y0 > r.y0 && y0 < r.y1)
    : false
}

function tileRect(p) {
  const [x, y] = cellXY(p.c, p.r)
  return { x0: x, y0: y, x1: x + CELL_W, y1: y + CELL_H }
}
/** The half-out avatar on a tile's left edge (AgentTile .gtile-avatar). */
function avatarRect(p) {
  const [x, y] = cellXY(p.c, p.r)
  return { x0: x - 26, y0: y + 12, x1: x, y1: y + 64 }
}

function endsOf(e) {
  const pts = pathPoints(e.d)
  return { start: pts[0], end: pts[pts.length - 1] }
}

describe('#2066 reporting lines leave the manager bottom and enter the report top', () => {
  // Every relative position the lattice allows, manager at (0,0).
  const cases = [
    ['directly below', { c: 0, r: 1 }],
    ['below-right (lateral offset)', { c: 1, r: 1 }],
    ['below-left (lateral offset)', { c: -2, r: 1 }],
    ['same row, right (was a side-to-side arrow)', { c: 1, r: 0 }],
    ['same row, left (was a side-to-side arrow)', { c: -1, r: 0 }],
    ['far right, one row down (was a side anchor)', { c: 4, r: 1 }],
    ['directly above (was flipped: manager top to report bottom)', { c: 0, r: -1 }],
    ['above-right', { c: 2, r: -1 }],
    ['above-left, far', { c: -3, r: -2 }],
    ['two rows down', { c: 0, r: 2 }],
  ]

  it.each(cases)('%s', (_label, repPos) => {
    const layout = { boss: { c: 0, r: 0 }, rep: repPos }
    const [e] = computeEdges(layout, [agent('boss'), agent('rep', ['reports-to-boss'])])
    const { start, end } = endsOf(e)
    expect(start).toMatchObject(bottomCentre(layout.boss))
    expect(end).toMatchObject(topCentre(layout.rep))
    // The arrowhead points DOWN into the report's top edge.
    const ah = pathPoints(e.ah)
    expect(ah[0]).toMatchObject(topCentre(layout.rep))
    expect(ah[1].y).toBe(ah[0].y - 8)
    expect(ah[2].y).toBe(ah[0].y - 8)
    // First move is down out of the manager, last move is down into the report.
    const segs = segments(e.d)
    expect(segs[0][1].x).toBe(start.x)
    expect(segs[0][1].y).toBeGreaterThan(start.y)
    const last = segs[segs.length - 1]
    expect(last[0].x).toBe(end.x)
    expect(last[0].y).toBeLessThan(end.y)
  })

  it('no segment is diagonal: the line is orthogonal with rounded corners', () => {
    const layout = { boss: { c: 0, r: 0 }, a: { c: 2, r: 1 }, b: { c: -1, r: -1 } }
    const edges = computeEdges(layout, [
      agent('boss'),
      agent('a', ['reports-to-boss']),
      agent('b', ['reports-to-boss']),
    ])
    for (const e of edges) {
      for (const cmd of e.d.match(/[A-Z]/g)) expect(['M', 'L', 'Q']).toContain(cmd)
      for (const [p, q] of segments(e.d)) expect(p.x === q.x || p.y === q.y).toBe(true)
    }
  })

  it('reports of one manager share one bus just below the manager', () => {
    const layout = {
      boss: { c: 1, r: 0 },
      a: { c: 0, r: 1 },
      b: { c: 1, r: 1 },
      c: { c: 3, r: 1 },
    }
    const edges = computeEdges(layout, [
      agent('boss'),
      agent('a', ['reports-to-boss']),
      agent('b', ['reports-to-boss']),
      agent('c', ['reports-to-boss']),
    ])
    const busY = bottomCentre(layout.boss).y + BUS_DROP
    for (const e of edges) {
      const ys = pathPoints(e.d).map((p) => p.y)
      if (e.report !== 'b') expect(ys).toContain(busY) // b sits straight below: no bus needed
    }
  })

  it('the bus sits in the corridor between a zone frame and the next row\'s zone header', () => {
    // Bottom chrome of the manager's zone ends at +bottom; the next row's
    // zone frame starts at GAP_Y - top. The bus must be strictly between.
    expect(BUS_DROP).toBeGreaterThan(ZONE_CHROME.bottom)
    expect(BUS_DROP).toBeLessThan(GAP_Y - ZONE_CHROME.top)
    // Lanes: clear of the zone's right chrome and the next column's avatar.
    expect(LANE_X).toBeGreaterThan(ZONE_CHROME.right)
    expect(LANE_X).toBeLessThan(GAP_X - 26)
  })

  it('a tile in the way of a straight drop sends the line down the lane instead', () => {
    const layout = { boss: { c: 0, r: 0 }, mid: { c: 1, r: 1 }, rep: { c: 1, r: 2 } }
    const [e] = computeEdges(layout, [
      agent('boss'),
      agent('mid'),
      agent('rep', ['reports-to-boss']),
    ])
    for (const s of segments(e.d)) expect(segmentEntersRect(s, tileRect(layout.mid))).toBe(false)
    // Without the obstacle the same pair is a straight drop through the empty cell.
    const [clear] = computeEdges(
      { boss: layout.boss, rep: layout.rep },
      [agent('boss'), agent('rep', ['reports-to-boss'])]
    )
    expect(pathPoints(clear.d).length).toBeLessThan(pathPoints(e.d).length)
  })

  it('a report with two managers gets two distinct arrival points', () => {
    const layout = { m1: { c: 0, r: 0 }, m2: { c: 2, r: 0 }, rep: { c: 1, r: 1 } }
    const edges = computeEdges(layout, [
      agent('m1'),
      agent('m2'),
      agent('rep', ['reports-to-m1', 'reports-to-m2']),
    ])
    expect(edges).toHaveLength(2)
    const xs = edges.map((e) => endsOf(e).end.x).sort((a, b) => a - b)
    const centre = topCentre(layout.rep).x
    expect(xs).toEqual([centre - ARRIVAL_SPREAD / 2, centre + ARRIVAL_SPREAD / 2])
    expect(edges[0].hit).not.toBe(edges[1].hit)
  })

  it('the hit target is the line\'s own final drop, so siblings never share one', () => {
    const layout = { boss: { c: 1, r: 0 }, a: { c: 0, r: 1 }, b: { c: 2, r: 1 } }
    const edges = computeEdges(layout, [
      agent('boss'),
      agent('a', ['reports-to-boss']),
      agent('b', ['reports-to-boss']),
    ])
    for (const e of edges) {
      const [p, q] = pathPoints(e.hit)
      expect(p.x).toBe(q.x) // vertical
      expect(q).toMatchObject(topCentre(layout[e.report]))
      expect(e.mid.x).toBe(q.x)
      expect(e.mid.y).toBeGreaterThan(p.y)
      expect(e.mid.y).toBeLessThan(q.y)
    }
    expect(edges[0].hit).not.toBe(edges[1].hit)
  })

  // Property check over random fleets: whatever the user's layout, no line
  // runs through a tile or an avatar, and the #2066 anchors always hold.
  it('random layouts: no line crosses a tile or avatar; anchors always bottom → top', () => {
    let seed = 2066
    const rand = () => {
      seed = (seed * 1103515245 + 12345) & 0x7fffffff
      return seed / 0x7fffffff
    }
    const violations = []
    let edgeCount = 0
    for (let trial = 0; trial < 300; trial++) {
      const n = 3 + Math.floor(rand() * 14)
      const layout = {}
      const used = new Set()
      const agents = []
      for (let i = 0; i < n; i++) {
        let c, r
        do {
          c = Math.floor(rand() * 7) - 3
          r = Math.floor(rand() * 5) - 2
        } while (used.has(`${c},${r}`))
        used.add(`${c},${r}`)
        layout[`a${i}`] = { c, r }
      }
      for (let i = 0; i < n; i++) {
        const tags = []
        if (i > 0 && rand() < 0.8) tags.push(`reports-to-a${Math.floor(rand() * i)}`)
        if (i > 1 && rand() < 0.1) tags.push(`reports-to-a${Math.floor(rand() * i)}`)
        agents.push(agent(`a${i}`, [...new Set(tags)]))
      }
      const edges = computeEdges(layout, agents)
      edgeCount += edges.length
      for (const e of edges) {
        const where = `trial ${trial} ${e.id}`
        const { start, end } = endsOf(e)
        const from = bottomCentre(layout[e.manager])
        const to = topCentre(layout[e.report])
        if (start.x !== from.x || start.y !== from.y) violations.push(`${where}: start off bottom-centre`)
        if (end.y !== to.y || Math.abs(end.x - to.x) > CELL_W / 4) violations.push(`${where}: end off top-centre`)
        for (const s of segments(e.d)) {
          if (s[0].x !== s[1].x && s[0].y !== s[1].y) violations.push(`${where}: diagonal segment`)
          for (const p of Object.values(layout)) {
            if (segmentEntersRect(s, tileRect(p))) violations.push(`${where}: through tile ${p.c},${p.r}`)
            if (segmentEntersRect(s, avatarRect(p))) violations.push(`${where}: through avatar ${p.c},${p.r}`)
          }
        }
      }
    }
    expect(edgeCount).toBeGreaterThan(1000) // the property ran over a real sample
    expect(violations).toEqual([])
  })
})

describe('orthogonalPath', () => {
  it('drops repeated and collinear waypoints', () => {
    const d = orthogonalPath([
      { x: 0, y: 0 },
      { x: 0, y: 0 },
      { x: 0, y: 10 },
      { x: 0, y: 20 },
    ])
    expect(d).toBe('M 0 0 L 0 20')
  })

  it('clamps the corner radius to half the shorter neighbouring segment', () => {
    const d = orthogonalPath([
      { x: 0, y: 0 },
      { x: 0, y: 6 },
      { x: 100, y: 6 },
    ])
    // 6px leg → radius 3, not ELBOW_R.
    expect(ELBOW_R).toBeGreaterThan(3)
    expect(d).toBe('M 0 0 L 0 3 Q 0 6 3 6 L 100 6')
  })
})

describe('paintTiers', () => {
  it('paints dimmed, then normal, then highlighted lines; empty tiers dropped', () => {
    const edges = [{ id: 'a' }, { id: 'b' }, { id: 'c' }, { id: 'd' }]
    const cls = { a: 'hot', b: '', c: 'dim', d: '' }
    const tiers = paintTiers(edges, (e) => cls[e.id])
    expect(tiers.map((t) => t.cls)).toEqual(['dim', 'base', 'hot'])
    expect(tiers.map((t) => t.edges.map((e) => e.id))).toEqual([['c'], ['b', 'd'], ['a']])
    expect(paintTiers(edges, () => '').map((t) => t.cls)).toEqual(['base'])
  })
})
