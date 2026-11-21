import { describe, it, expect } from 'vitest'
import {
  DEPT_PREFIX,
  REPORTS_PREFIX,
  ZONE_CHROME,
  GAP_X,
  GAP_Y,
  isOrgTag,
  orgTagFits,
  orgMeta,
  deptOf,
  managersOf,
  deptSlot,
  DEPT_SLOT_COUNT,
  deptInfoByAgent,
  computeZones,
  zoneAt,
  computeEdges,
  arrangeByDept,
  tidyByDept,
  newcomerOrigin,
  orthogonalPath,
  paintTiers,
  BUS_DROP,
  LANE_X,
  ELBOW_R,
  ARRIVAL_SPREAD,
} from '@/utils/gridOrg'
import { CELL_W, CELL_H, cellXY } from '@/utils/gridLayout'

const agent = (name, tags = [], status = 'stopped') => ({ name, tags, status })

describe('org tag parsing', () => {
  it('isOrgTag recognizes both namespaces and nothing else', () => {
    expect(isOrgTag('dept-marketing')).toBe(true)
    expect(isOrgTag('reports-to-boss')).toBe(true)
    expect(isOrgTag('marketing')).toBe(false)
    expect(isOrgTag('department')).toBe(false)
  })

  it('orgMeta: bootstrap iff no explicit dept-* tag anywhere', () => {
    expect(orgMeta([agent('a', ['marketing'])]).bootstrap).toBe(true)
    expect(orgMeta([agent('a', ['dept-x']), agent('b', [])]).bootstrap).toBe(false)
    // Empty-suffix `dept-` does not count as an explicit department.
    expect(orgMeta([agent('a', ['dept-'])]).bootstrap).toBe(true)
  })

  it('deptOf: dept-* wins; plain fallback only in bootstrap; reports-to never', () => {
    const boot = { bootstrap: true }
    const strict = { bootstrap: false }
    expect(deptOf(agent('a', ['zeta', 'dept-mkt']), boot)).toBe('mkt')
    expect(deptOf(agent('a', ['zeta']), boot)).toBe('zeta')
    expect(deptOf(agent('a', ['zeta']), strict)).toBe(null)
    expect(deptOf(agent('a', ['reports-to-x']), boot)).toBe(null)
    expect(deptOf(agent('a', ['dept-']), boot)).toBe(null)
  })

  it('managersOf: multiple allowed, empty suffix dropped', () => {
    expect(managersOf(agent('a', ['reports-to-x', 'reports-to-y', 'plain']))).toEqual([
      'x',
      'y',
    ])
    expect(managersOf(agent('a', ['reports-to-']))).toEqual([])
  })

  it('orgTagFits mirrors the router 50-char cap', () => {
    expect(orgTagFits(REPORTS_PREFIX, 'x'.repeat(50 - REPORTS_PREFIX.length))).toBe(true)
    expect(orgTagFits(REPORTS_PREFIX, 'x'.repeat(51 - REPORTS_PREFIX.length))).toBe(false)
  })
})

describe('palette slots', () => {
  it('slot depends only on the name — adding depts never recolors others', () => {
    const before = deptSlot('marketing')
    // "add" unrelated departments — slot must not move
    void deptSlot('zeta')
    void deptSlot('alpha')
    expect(deptSlot('marketing')).toBe(before)
  })

  it('slots stay in range', () => {
    for (const d of ['a', 'marketing', 'gtm-emea', 'knowledge', 'x'.repeat(40)]) {
      const s = deptSlot(d)
      expect(s).toBeGreaterThanOrEqual(0)
      expect(s).toBeLessThan(DEPT_SLOT_COUNT)
    }
  })
})

describe('zone chrome contract', () => {
  it('frame chrome fits inside the lattice gaps (adjacent zones never collide)', () => {
    // THE spacing contract: if this fails, a gap or chrome edit reintroduced
    // the overlapping-zone-frames bug (#305 design of record).
    expect(ZONE_CHROME.left + ZONE_CHROME.right).toBeLessThanOrEqual(GAP_X)
    expect(ZONE_CHROME.top + ZONE_CHROME.bottom).toBeLessThanOrEqual(GAP_Y)
  })
})

describe('computeZones / zoneAt', () => {
  const fleet = [
    agent('a1', ['dept-mkt'], 'running'),
    agent('a2', ['dept-mkt']),
    agent('b1', ['dept-ops']),
    agent('loose', ['plain']),
  ]
  const layout = { a1: { c: 0, r: 0 }, a2: { c: 1, r: 0 }, b1: { c: 0, r: 1 } }

  it('hulls wrap member tiles with the chrome padding; counts are scoped', () => {
    const zones = computeZones(layout, fleet)
    expect(zones.map((z) => z.dept)).toEqual(['mkt', 'ops'])
    const mkt = zones[0]
    expect(mkt.count).toBe(2)
    expect(mkt.running).toBe(1)
    expect(mkt.readOnly).toBe(false)
    expect(mkt.x).toBe(-ZONE_CHROME.left)
    expect(mkt.y).toBe(-ZONE_CHROME.top)
  })

  it('bootstrap zones are read-only', () => {
    const zones = computeZones({ loose: { c: 0, r: 0 } }, [agent('loose', ['plain'])])
    expect(zones).toHaveLength(1)
    expect(zones[0].readOnly).toBe(true)
  })

  it('zoneAt resolves overlap to the smallest hull', () => {
    const zones = [
      { dept: 'big', x: 0, y: 0, w: 1000, h: 1000 },
      { dept: 'small', x: 100, y: 100, w: 200, h: 200 },
    ]
    expect(zoneAt(zones, 150, 150).dept).toBe('small')
    expect(zoneAt(zones, 900, 900).dept).toBe('big')
    expect(zoneAt(zones, 5000, 5000)).toBe(null)
  })
})

describe('computeEdges', () => {
  const layout = { boss: { c: 0, r: 0 }, rep: { c: 0, r: 1 }, side: { c: 1, r: 0 } }

  it('every edge carries a path AND an arrowhead (direction is the payload)', () => {
    const edges = computeEdges(layout, [
      agent('boss'),
      agent('rep', ['reports-to-boss']),
    ])
    expect(edges).toHaveLength(1)
    expect(edges[0].d).toMatch(/^M /)
    expect(edges[0].ah).toMatch(/Z$/)
    expect(edges[0].manager).toBe('boss')
    expect(edges[0].report).toBe('rep')
    expect(edges[0].mid).toBeDefined()
  })

  it('skips dangling refs and self-references', () => {
    const edges = computeEdges(layout, [
      agent('rep', ['reports-to-ghost', 'reports-to-rep']),
    ])
    expect(edges).toHaveLength(0)
  })

  it('stacked tiles connect bottom-of-manager to top-of-report', () => {
    const [e] = computeEdges(layout, [agent('boss'), agent('rep', ['reports-to-boss'])])
    // Manager anchor y = bottom edge of row 0 (CELL_H = 216); path starts there.
    expect(e.d.startsWith('M 192 216')).toBe(true)
  })
})

function assertCollisionFree(map) {
  const cells = Object.values(map).map((p) => `${p.c},${p.r}`)
  expect(new Set(cells).size).toBe(cells.length)
}

describe('arrangeByDept', () => {
  it('packs dense blocks, no duplicate cells, unassigned last, deterministic', () => {
    const fleet = [
      agent('m1', ['dept-mkt']),
      agent('m2', ['dept-mkt']),
      agent('m3', ['dept-mkt']),
      agent('o1', ['dept-ops']),
      agent('free1', []),
    ]
    const a = arrangeByDept(fleet)
    const b = arrangeByDept(fleet)
    expect(a).toEqual(b)
    expect(Object.keys(a)).toHaveLength(5)
    assertCollisionFree(a)
    // Unassigned block starts after every department block (rightmost column).
    const maxDeptC = Math.max(a.m1.c, a.m2.c, a.m3.c, a.o1.c)
    expect(a.free1.c).toBeGreaterThan(maxDeptC)
  })

  it('wraps into a new band past MAX_COLS', () => {
    const fleet = []
    for (const d of ['aa', 'bb', 'cc', 'dd', 'ee']) {
      for (let i = 0; i < 3; i++) fleet.push(agent(`${d}${i}`, [`dept-${d}`]))
    }
    const map = arrangeByDept(fleet)
    assertCollisionFree(map)
    // 5 blocks × 2 cols = 10 > 8 → at least one block wrapped below band 0.
    expect(Math.max(...Object.values(map).map((p) => p.r))).toBeGreaterThan(1)
    expect(Math.max(...Object.values(map).map((p) => p.c))).toBeLessThan(8)
  })
})

describe('tidyByDept', () => {
  it('compacts each department at its own anchor; collision-free; deterministic', () => {
    const fleet = [
      agent('m1', ['dept-mkt']),
      agent('m2', ['dept-mkt']),
      agent('o1', ['dept-ops']),
      agent('free1', []),
    ]
    // Scattered: mkt anchored at (0,0), ops at (5,3), free at (2,6)
    const layout = {
      m1: { c: 0, r: 0 },
      m2: { c: 3, r: 1 },
      o1: { c: 5, r: 3 },
      free1: { c: 2, r: 6 },
    }
    const out = tidyByDept(layout, fleet)
    const out2 = tidyByDept(layout, fleet)
    expect(out).toEqual(out2)
    assertCollisionFree(out)
    // mkt compacted against its own anchor (0,0): m2 pulled adjacent.
    expect(out.m1).toEqual({ c: 0, r: 0 })
    expect(out.m2).toEqual({ c: 1, r: 0 })
    // ops keeps its own anchor, not dragged to origin.
    expect(out.o1).toEqual({ c: 5, r: 3 })
    // untouched groups keep their anchor too
    expect(out.free1).toEqual({ c: 2, r: 6 })
  })
})

describe('newcomerOrigin', () => {
  it('places a departmental newcomer beside its zone hull', () => {
    const fleet = [
      agent('m1', ['dept-mkt']),
      agent('m2', ['dept-mkt']),
      agent('new', ['dept-mkt']),
    ]
    const layout = { m1: { c: 0, r: 0 }, m2: { c: 1, r: 0 } }
    expect(newcomerOrigin('new', layout, fleet)).toEqual({ c: 2, r: 0 })
  })

  it('returns null for agents without a placed department', () => {
    expect(newcomerOrigin('x', {}, [agent('x', [])])).toBe(null)
  })
})

describe('deptInfoByAgent', () => {
  it('maps name → {name, slot} for ribbon rendering', () => {
    const fleet = [agent('a', ['dept-mkt']), agent('b', [])]
    const info = deptInfoByAgent(fleet)
    expect(info.a).toEqual({ name: 'mkt', slot: deptSlot('mkt') })
    expect(info.b).toBeUndefined()
  })
})

describe('namespace constants', () => {
  it('prefixes match the backend mirror (db/tags.py ORG_TAG_PREFIXES)', () => {
    expect(DEPT_PREFIX).toBe('dept-')
    expect(REPORTS_PREFIX).toBe('reports-to-')
  })
})

/**
 * #2066: on the Grid org overlay, every reporting line leaves the manager
 * tile at bottom-centre and enters the report tile at top-centre, whatever
 * the tiles' relative lattice positions, and is routed so it reads as an org
 * chart (orthogonal, through the gaps, siblings on one bus).
 */

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

  it('a widget tile is an obstacle too; a key left by a deleted agent is not', () => {
    const base = { boss: { c: 0, r: 0 }, rep: { c: 1, r: 2 } }
    const agents = [agent('boss'), agent('rep', ['reports-to-boss'])]
    const [clear] = computeEdges(base, agents)
    const [ghost] = computeEdges({ ...base, 'deleted-agent': { c: 1, r: 1 } }, agents)
    expect(ghost.d).toBe(clear.d)
    const [widget] = computeEdges({ ...base, 'widget:clock': { c: 1, r: 1 } }, agents)
    expect(pathPoints(widget.d).length).toBeGreaterThan(pathPoints(clear.d).length)
  })

  it('an only report is hoverable along its whole line', () => {
    const layout = { boss: { c: 0, r: 0 }, rep: { c: 2, r: 1 } }
    const [e] = computeEdges(layout, [agent('boss'), agent('rep', ['reports-to-boss'])])
    expect(e.hit).toBe(e.d)
  })

  it('with siblings, the hit target starts after the last segment another line runs along', () => {
    const layout = { boss: { c: 0, r: 0 }, mid: { c: 1, r: 1 }, rep: { c: 1, r: 2 }, sib: { c: 0, r: 1 } }
    const edges = computeEdges(layout, [
      agent('boss'),
      agent('mid'),
      agent('rep', ['reports-to-boss']),
      agent('sib', ['reports-to-boss']),
    ])
    const e = edges.find((x) => x.report === 'rep')
    const busY = bottomCentre(layout.boss).y + BUS_DROP
    const hit = pathPoints(e.hit)
    // The trunk runs along `sib`'s straight drop, so the hit starts below it,
    // at the bus, and keeps the lane, the approach and the final drop.
    expect(hit[0].y).toBe(busY)
    expect(hit.every((pt) => pt.y >= busY)).toBe(true)
    expect(hit.length).toBeGreaterThan(4)
  })

  it('no two hit targets run along each other, so a hover always names one line', () => {
    // Two reports detouring up the same lane (beside and above the manager)
    // plus straight drops: the lane is shared, so neither hit may include it.
    const layout = {
      boss: { c: 1, r: 1 }, beside: { c: 2, r: 1 }, above: { c: 2, r: 0 },
      below: { c: 1, r: 2 }, far: { c: 3, r: 2 },
    }
    const edges = computeEdges(layout, [
      agent('boss'),
      ...['beside', 'above', 'below', 'far'].map((n) => agent(n, ['reports-to-boss'])),
    ])
    const runs = edges.flatMap((e) => segments(e.hit).map(([a, b]) => ({ e: e.id, a, b })))
      .filter(({ a, b }) => a.x === b.x || a.y === b.y)
    for (const r of runs) {
      for (const o of runs) {
        if (o.e === r.e) continue
        const sameLine = (r.a.x === r.b.x && o.a.x === o.b.x && r.a.x === o.a.x) ||
          (r.a.y === r.b.y && o.a.y === o.b.y && r.a.y === o.a.y)
        if (!sameLine) continue
        const k = r.a.x === r.b.x ? 'y' : 'x'
        const lo = Math.max(Math.min(r.a[k], r.b[k]), Math.min(o.a[k], o.b[k]))
        const hi = Math.min(Math.max(r.a[k], r.b[k]), Math.max(o.a[k], o.b[k]))
        expect(hi - lo, `${r.e} overlaps ${o.e}`).toBeLessThan(1)
      }
    }
    for (const e of edges) expect(segments(e.hit).length).toBeGreaterThan(0)
  })

  it('siblings share the trunk, so each hit target starts at the bus and ends at its report', () => {
    const layout = { boss: { c: 1, r: 0 }, a: { c: 0, r: 1 }, b: { c: 2, r: 1 } }
    const edges = computeEdges(layout, [
      agent('boss'),
      agent('a', ['reports-to-boss']),
      agent('b', ['reports-to-boss']),
    ])
    const junction = { x: bottomCentre(layout.boss).x, y: bottomCentre(layout.boss).y + BUS_DROP }
    for (const e of edges) {
      const pts = pathPoints(e.hit)
      expect(pts[0]).toMatchObject(junction) // the trunk above it is shared
      const q = pts[pts.length - 1]
      expect(q).toMatchObject(topCentre(layout[e.report]))
      expect(e.mid.x).toBe(q.x)
      expect(e.mid.y).toBeGreaterThan(junction.y)
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
