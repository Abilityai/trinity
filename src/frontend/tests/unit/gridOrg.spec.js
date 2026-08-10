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
} from '@/utils/gridOrg'

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
