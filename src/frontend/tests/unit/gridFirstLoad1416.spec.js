/**
 * The Grid on first load (trinity-enterprise#1416): the Chief of Staff and the
 * Company Brain side by side in one named group with the line between them,
 * the system agent set apart, the info tiles in one row directly above, and an
 * agent built in the first session next to the pair.
 *
 * `firstLoadLayout` is pure; it is driven here with the shapes a fresh
 * install's roster actually has (the seeder's tags: `dept-headquarters` on both,
 * `reports-to-corbin` on the Brain), and the zone and line are read back
 * through the overlay's own `computeZones` / `computeEdges`.
 */
import { describe, it, expect } from 'vitest'
import { firstLoadLayout, computeZones, computeEdges, orgMeta } from '@/utils/gridOrg'
import { layoutBBox, CELL_W, GAP_X } from '@/utils/gridLayout'

const SYSTEM = { name: 'trinity-system', is_system: true, tags: [] }
const CHIEF = { name: 'corbin', tags: ['dept-headquarters'] }
const BRAIN = { name: 'cornelius', tags: ['dept-headquarters', 'reports-to-corbin'] }
const CORE = ['corbin', 'cornelius']
const TILES = ['widget:fleet-summary', 'widget:capacity', 'widget:subscriptions']

function arrange(agents, tiles = TILES) {
  return firstLoadLayout(agents, tiles, orgMeta(agents), CORE)
}

describe('firstLoadLayout (ent#1416)', () => {
  it('puts the Chief of Staff and the Brain side by side, Chief first', () => {
    const l = arrange([SYSTEM, BRAIN, CHIEF])
    expect(l.corbin).toEqual({ c: 0, r: 0 })
    expect(l.cornelius).toEqual({ c: 1, r: 0 })
  })

  it('sets the system agent apart: one empty column after the team', () => {
    const l = arrange([SYSTEM, BRAIN, CHIEF])
    expect(l['trinity-system']).toEqual({ c: 3, r: 0 })
    const taken = new Set(Object.values(l).map((p) => `${p.c},${p.r}`))
    expect(taken.has('2,0')).toBe(false)
  })

  it('lays the info tiles in one row directly above the agents', () => {
    const l = arrange([SYSTEM, BRAIN, CHIEF])
    expect(TILES.map((k) => l[k])).toEqual([{ c: 0, r: -1 }, { c: 1, r: -1 }, { c: 2, r: -1 }])
  })

  it('leaves no empty band: the board is two rows tall', () => {
    const l = arrange([SYSTEM, BRAIN, CHIEF])
    const rows = new Set(Object.values(l).map((p) => p.r))
    expect([...rows].sort()).toEqual([-1, 0])
    // and four columns wide (team, gap, system)
    expect(layoutBBox(l).w).toBe(4 * CELL_W + 3 * GAP_X)
  })

  it('draws one named zone around the pair and the line between them', () => {
    const agents = [SYSTEM, BRAIN, CHIEF]
    const l = arrange(agents)
    const zones = computeZones(l, agents, orgMeta(agents))
    expect(zones.map((z) => z.dept)).toEqual(['headquarters'])
    expect(zones[0].count).toBe(2)
    expect(zones[0].anchor).toEqual({ c: 0, r: 0 })
    expect(zones[0].extent).toEqual({ maxC: 1, maxR: 0 })
    const edges = computeEdges(l, agents)
    expect(edges.map((e) => [e.manager, e.report])).toEqual([['corbin', 'cornelius']])
  })

  it('lands an agent built in the first session next to the pair', () => {
    const l = arrange([SYSTEM, BRAIN, CHIEF, { name: 'lead-finder', tags: [] }])
    expect(l['lead-finder']).toEqual({ c: 2, r: 0 })
    expect(l.corbin).toEqual({ c: 0, r: 0 })
    expect(l.cornelius).toEqual({ c: 1, r: 0 })
    expect(l['trinity-system']).toEqual({ c: 4, r: 0 })
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
    expect(l.cornelius).toEqual({ c: 1, r: 0 })
    expect(Math.min(...sales.map((a) => l[a.name].c))).toBe(2)
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
