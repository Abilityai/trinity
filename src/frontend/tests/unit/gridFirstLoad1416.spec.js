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
import { firstLoadLayout, firstLoadFit, computeZones, computeEdges, orgMeta } from '@/utils/gridOrg'
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
 * The automatic fit (ent#1416 fix round). Measured against the 1440x900
 * screenshots: the Grid canvas is about 1377x788 with the assistant closed and
 * about 1042x788 with it open. Four fleet tiles in one row are wider than the
 * three-agent board; fitting all of them landed at 58% with the assistant open.
 */
describe('firstLoadFit (ent#1416)', () => {
  const FOUR = [...TILES, 'widget:executions']
  const agents = [SYSTEM, BRAIN, CHIEF]
  const names = agents.map((a) => a.name)
  const toScreen = (f, c, r) => {
    const [x, y] = cellXY(c, r)
    return { x: x * f.z + f.tx, y: y * f.z + f.ty }
  }

  it('places the board from the top with normal padding, no empty band', () => {
    const l = arrange(agents, FOUR)
    for (const [w, h] of [[1377, 788], [1042, 788]]) {
      const f = firstLoadFit(l, names, w, h)
      expect(toScreen(f, 0, -1).y).toBe(28)
    }
  })

  it('stays readable with the assistant open: the tiles row does not set the zoom', () => {
    const l = arrange(agents, FOUR)
    const f = firstLoadFit(l, names, 1042, 788)
    expect(f.z).toBeGreaterThanOrEqual(0.75)
    // every agent tile is fully in view
    for (const n of names) {
      const tl = toScreen(f, l[n].c, l[n].r)
      expect(tl.x).toBeGreaterThanOrEqual(0)
      expect(tl.x + CELL_W * f.z).toBeLessThanOrEqual(1042)
      expect(tl.y + CELL_H * f.z).toBeLessThanOrEqual(788)
    }
    // the fourth tile is reached by panning to the right
    expect(toScreen(f, 3, -1).x).toBeGreaterThan(1042 - CELL_W * f.z)
  })

  it('fits the whole board when it is small enough anyway', () => {
    const l = arrange(agents, TILES.slice(0, 2))
    const f = firstLoadFit(l, names, 1377, 788)
    expect(f.z).toBeLessThanOrEqual(1.1)
    expect(toScreen(f, 2, 0).x + CELL_W * f.z).toBeLessThanOrEqual(1377)
  })
})
