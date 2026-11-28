/**
 * The Grid on first load (trinity-enterprise#1416): the Chief of Staff with the
 * Company Brain directly below, in one named group, joined by one short arrow,
 * the system agent set apart, the info tiles in one row directly above, and an
 * agent built in the first session next to the pair.
 *
 * `firstLoadLayout` is pure; it is driven here with the shapes a fresh
 * install's roster actually has (the seeder's tags: `dept-headquarters` on both,
 * `reports-to-corbin` on the Brain), and the zone and line are read back
 * through the overlay's own `computeZones` / `computeEdges`.
 */
import { describe, it, expect } from 'vitest'
import { firstLoadLayout, firstLoadFit, firstLoadBoard, firstViewObstacles, FIRST_VIEW_MIN_ZOOM, computeZones, computeEdges, orgMeta } from '@/utils/gridOrg'
import { cellXY, CELL_W, CELL_H, GAP_Y } from '@/utils/gridLayout'

const SYSTEM = { name: 'trinity-system', is_system: true, tags: [] }
const CHIEF = { name: 'corbin', tags: ['dept-headquarters'] }
const BRAIN = { name: 'cornelius', tags: ['dept-headquarters', 'reports-to-corbin'] }
const CORE = ['corbin', 'cornelius']
const TILES = ['widget:fleet-summary', 'widget:capacity', 'widget:subscriptions']

function arrange(agents, tiles = TILES) {
  return firstLoadLayout(agents, tiles, orgMeta(agents), CORE)
}

describe('firstLoadLayout (ent#1416)', () => {
  it('puts the Brain directly below the Chief of Staff', () => {
    const l = arrange([SYSTEM, BRAIN, CHIEF])
    expect(l.corbin).toEqual({ c: 0, r: 0 })
    expect(l.cornelius).toEqual({ c: 0, r: 1 })
  })

  it('joins them with one short straight arrow, Chief of Staff to Brain', () => {
    const agents = [SYSTEM, BRAIN, CHIEF]
    const [e] = computeEdges(arrange(agents), agents)
    // one segment: no elbow, no loop over the tiles
    expect(e.d).toMatch(/^M [\d.-]+ [\d.-]+ L [\d.-]+ [\d.-]+$/)
    const [x0, y0, x1, y1] = e.d.match(/[\d.-]+/g).map(Number)
    const [cx, cy] = cellXY(0, 0)
    expect(x0).toBe(x1)
    expect(y0).toBe(cy + CELL_H)            // leaves the Chief of Staff's bottom
    expect(y1 - y0).toBe(GAP_Y)             // across the gap, nothing more
    expect(x0).toBe(cx + CELL_W / 2)
  })

  it('sets the system agent apart: one empty column after the team', () => {
    const l = arrange([SYSTEM, BRAIN, CHIEF])
    expect(l['trinity-system']).toEqual({ c: 2, r: 0 })
    const taken = new Set(Object.values(l).map((p) => `${p.c},${p.r}`))
    expect(taken.has('1,0')).toBe(false)
  })

  it('lays the info tiles in one row directly above the agents', () => {
    const l = arrange([SYSTEM, BRAIN, CHIEF])
    expect(TILES.map((k) => l[k])).toEqual([{ c: 0, r: -1 }, { c: 1, r: -1 }, { c: 2, r: -1 }])
  })

  it('leaves no empty band: the tiles row, then the agents', () => {
    const l = arrange([SYSTEM, BRAIN, CHIEF])
    const rows = new Set(Object.values(l).map((p) => p.r))
    expect([...rows].sort()).toEqual([-1, 0, 1])
  })

  it('draws one named zone around the pair and the line between them', () => {
    const agents = [SYSTEM, BRAIN, CHIEF]
    const l = arrange(agents)
    const zones = computeZones(l, agents, orgMeta(agents))
    expect(zones.map((z) => z.dept)).toEqual(['headquarters'])
    expect(zones[0].count).toBe(2)
    expect(zones[0].anchor).toEqual({ c: 0, r: 0 })
    expect(zones[0].extent).toEqual({ maxC: 0, maxR: 1 })
    const edges = computeEdges(l, agents)
    expect(edges.map((e) => [e.manager, e.report])).toEqual([['corbin', 'cornelius']])
  })

  it('lands an agent built in the first session next to the pair', () => {
    const l = arrange([SYSTEM, BRAIN, CHIEF, { name: 'lead-finder', tags: [] }])
    expect(l['lead-finder']).toEqual({ c: 1, r: 0 })
    expect(l.corbin).toEqual({ c: 0, r: 0 })
    expect(l.cornelius).toEqual({ c: 0, r: 1 })
    expect(l['trinity-system']).toEqual({ c: 3, r: 0 })
  })

  it('keeps the pair in place when their tags arrive a poll later', () => {
    const untagged = arrange([SYSTEM, { name: 'cornelius', tags: [] }, { name: 'corbin', tags: [] }])
    const tagged = arrange([SYSTEM, BRAIN, CHIEF])
    expect(untagged.corbin).toEqual(tagged.corbin)
    expect(untagged.cornelius).toEqual(tagged.cornelius)
  })

  it('the core group leads even when another department is larger', () => {
    const sales = ['s1', 's2', 's3'].map((name) => ({ name, tags: ['dept-sales'] }))
    const l = arrange([SYSTEM, BRAIN, CHIEF, ...sales])
    expect(l.corbin).toEqual({ c: 0, r: 0 })
    expect(l.cornelius).toEqual({ c: 0, r: 1 })
    expect(Math.min(...sales.map((a) => l[a.name].c))).toBe(1)
  })

  it('places each cell once', () => {
    const agents = [SYSTEM, BRAIN, CHIEF, { name: 'a' }, { name: 'b' }, { name: 'c', tags: ['dept-sales'] }]
    const l = arrange(agents)
    const cells = Object.values(l).map((p) => `${p.c},${p.r}`)
    expect(new Set(cells).size).toBe(cells.length)
    expect(Object.keys(l).length).toBe(agents.length + TILES.length)
  })

  it('without core agents still arranges (an install seeded before ent#1416)', () => {
    const l = firstLoadLayout([SYSTEM, { name: 'mine' }], [], orgMeta([SYSTEM]), [])
    expect(l.mine).toEqual({ c: 0, r: 0 })
    expect(l['trinity-system']).toEqual({ c: 2, r: 0 })
  })
})

/*
 * The automatic fit (ent#1416 fix rounds). Measured against the 1440x900
 * screenshots: the Grid canvas is about 1377x789 with the assistant closed and
 * about 1041x789 with it open (1217x689 and 881x689 at 1280x800). The view keeps
 * every card clear of the canvas overlays (corners, not bands), and on a canvas
 * too narrow for four tiles in a row at 70% the tiles go two to a row.
 */
describe('firstLoadFit (ent#1416)', () => {
  const FOUR = [...TILES, 'widget:executions']
  const agents = [SYSTEM, BRAIN, CHIEF]
  const box = (f, p) => {
    const [x, y] = cellXY(p.c, p.r)
    return { l: (x - 28) * f.z + f.tx, t: y * f.z + f.ty, r: (x + CELL_W) * f.z + f.tx, b: (y + CELL_H) * f.z + f.ty }
  }
  const SIZES = [[1377, 789], [1041, 789], [1217, 689], [881, 689]]

  it('keeps every card inside the canvas and off every overlay, the fourth tile included', () => {
    for (const tileCols of [null, 2]) {
      const l = firstLoadLayout(agents, FOUR, orgMeta(agents), CORE, { tileCols })
      for (const [w, h] of SIZES) {
        const f = firstLoadFit(l, w, h)
        for (const p of Object.values(l)) {
          const b = box(f, p)
          expect(b.l).toBeGreaterThanOrEqual(0)
          expect(b.t).toBeGreaterThanOrEqual(0)
          expect(b.r).toBeLessThanOrEqual(w)
          expect(b.b).toBeLessThanOrEqual(h)
          for (const o of firstViewObstacles(w, h)) {
            expect(b.r <= o.x || b.l >= o.x + o.w || b.b <= o.y || b.t >= o.y + o.h).toBe(true)
          }
        }
      }
    }
  })

  it('places the board at the top when nothing is in the way there', () => {
    const l = firstLoadLayout(agents, FOUR, orgMeta(agents), CORE, { tileCols: 2 })
    const f = firstLoadFit(l, 1041, 789)
    expect(box(f, { c: 0, r: -2 }).t).toBeLessThan(30)
  })

  it('never zooms past 110% on a small board', () => {
    const l = arrange(agents, TILES.slice(0, 2))
    const f = firstLoadFit(l, 2400, 1400)
    expect(f.z).toBe(1.1)
  })
})

describe('firstLoadBoard (ent#1416): one row of tiles while it can be read', () => {
  const FOUR = [...TILES, 'widget:executions']
  const agents = [SYSTEM, BRAIN, CHIEF]
  const build = (tileCols) => firstLoadLayout(agents, FOUR, orgMeta(agents), CORE, { tileCols })
  const rows = (l) => new Set(FOUR.map((k) => l[k].r))

  it('one row on a wide canvas', () => {
    const l = firstLoadBoard(build, 1377, 789)
    expect([...rows(l)]).toEqual([-1])
    expect(firstLoadFit(l, 1377, 789).z).toBeGreaterThanOrEqual(FIRST_VIEW_MIN_ZOOM)
  })

  it('two rows of two, ending directly above the agents, where one row is too small to read', () => {
    const l = firstLoadBoard(build, 1041, 789)
    expect(l['widget:fleet-summary']).toEqual({ c: 0, r: -2 })
    expect(l['widget:capacity']).toEqual({ c: 1, r: -2 })
    expect(l['widget:subscriptions']).toEqual({ c: 0, r: -1 })
    expect(l['widget:executions']).toEqual({ c: 1, r: -1 })
    expect(l.corbin).toEqual({ c: 0, r: 0 })
    expect(firstLoadFit(l, 1041, 789).z).toBeGreaterThanOrEqual(FIRST_VIEW_MIN_ZOOM)
  })

  it('keeps one row when two to a row would not be larger', () => {
    expect([...rows(firstLoadBoard(build, 1217, 689))]).toEqual([-1])
  })

  it('one row before the canvas has a size', () => {
    expect([...rows(firstLoadBoard(build, 0, 0))]).toEqual([-1])
  })
})
