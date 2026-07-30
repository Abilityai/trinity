/**
 * PROTOTYPE — org overlay helpers for the Dashboard Grid view (zones +
 * reporting lines). Pure functions over the existing lattice layout; no DOM,
 * no Vue. The grid's coordinate model is untouched — zones are derived
 * (hull model) from wherever the member tiles sit, never a constraint.
 *
 * Data conventions (reads tolerant, writes namespaced):
 *   department      → first `dept-<name>` tag; falls back to the first plain
 *                     tag so existing fleets (e.g. `marketing`) show zones
 *                     with zero re-tagging
 *   reporting line  → `reports-to-<agent>` tag on the REPORT agent
 * (Hyphen namespaces, not colons — the tags API allows [a-z0-9-] only.)
 */

import { CELL_W, CELL_H, cellXY } from './gridLayout'

export const DEPT_PREFIX = 'dept-'
export const REPORTS_PREFIX = 'reports-to-'

// Categorical palette — distinct from tile state colors (green/yellow/red)
// and from the trigger-bucket chart colors.
const DEPT_PALETTE = [
  '#a78bfa', // violet
  '#f472b6', // pink
  '#2dd4bf', // teal
  '#fbbf24', // amber
  '#38bdf8', // sky
  '#fb923c', // orange
  '#a3e635', // lime
  '#fb7185', // rose
]

/** Department of an agent, or null. */
export function deptOf(agent) {
  const tags = agent.tags || []
  const namespaced = tags.find((t) => t.startsWith(DEPT_PREFIX))
  if (namespaced) return namespaced.slice(DEPT_PREFIX.length) || null
  return tags.find((t) => !t.startsWith(REPORTS_PREFIX)) || null
}

/** Manager names this agent reports to (usually 0 or 1, multiple allowed). */
export function managersOf(agent) {
  return (agent.tags || [])
    .filter((t) => t.startsWith(REPORTS_PREFIX))
    .map((t) => t.slice(REPORTS_PREFIX.length))
    .filter(Boolean)
}

/** All departments present in a fleet, sorted (stable palette assignment). */
export function allDepts(agents) {
  return [...new Set(agents.map(deptOf).filter(Boolean))].sort()
}

export function deptColor(dept, depts) {
  const i = depts.indexOf(dept)
  return DEPT_PALETTE[(i >= 0 ? i : 0) % DEPT_PALETTE.length]
}

/** name → { name: dept, color } for ribbon rendering on tiles. */
export function deptInfoByAgent(agents) {
  const depts = allDepts(agents)
  const map = {}
  for (const a of agents) {
    const d = deptOf(a)
    if (d) map[a.name] = { name: d, color: deptColor(d, depts) }
  }
  return map
}

/**
 * Derived zone hulls: one pixel rect per department, wrapping the bounding
 * box of its placed member tiles. Padding clears the half-out avatar on the
 * left and leaves header room on top.
 *
 * Chrome budget (must stay within the lattice gaps, see gridLayout.js):
 *   left 22 + right 10 ≤ GAP_X (40) with daylight between adjacent frames;
 *   top 34 (header band) + bottom 10 ≤ GAP_Y (50). This is what lets two
 *   departments sit in adjacent rows/columns with no spacer cells.
 */
export function computeZones(layout, agents) {
  const depts = allDepts(agents)
  const groups = new Map()
  for (const a of agents) {
    const d = deptOf(a)
    if (!d || !layout[a.name]) continue
    if (!groups.has(d)) groups.set(d, { members: [], running: 0 })
    const g = groups.get(d)
    g.members.push(a.name)
    if ((a.status || '').toLowerCase() === 'running') g.running++
  }
  const zones = []
  for (const [dept, g] of groups.entries()) {
    let minC = Infinity
    let maxC = -Infinity
    let minR = Infinity
    let maxR = -Infinity
    for (const n of g.members) {
      const p = layout[n]
      if (p.c < minC) minC = p.c
      if (p.c > maxC) maxC = p.c
      if (p.r < minR) minR = p.r
      if (p.r > maxR) maxR = p.r
    }
    const [x0, y0] = cellXY(minC, minR)
    const [x1, y1] = cellXY(maxC, maxR)
    zones.push({
      dept,
      color: deptColor(dept, depts),
      x: x0 - 22,
      y: y0 - 34,
      w: x1 + CELL_W - x0 + 32,
      h: y1 + CELL_H - y0 + 44,
      count: g.members.length,
      running: g.running,
    })
  }
  return zones.sort((a, b) => a.dept.localeCompare(b.dept))
}

/** Point-in-zone test for the drag-to-assign drop affordance. */
export function zoneAt(zones, x, y) {
  for (const z of zones) {
    if (x >= z.x && x <= z.x + z.w && y >= z.y && y <= z.y + z.h) return z
  }
  return null
}

function edgePath(ax, ay, bx, by, vertical) {
  const k = Math.min(120, Math.max(28, (vertical ? Math.abs(by - ay) : Math.abs(bx - ax)) * 0.4))
  if (vertical) {
    const s = by > ay ? 1 : -1
    return `M ${ax} ${ay} C ${ax} ${ay + s * k}, ${bx} ${by - s * k}, ${bx} ${by}`
  }
  const s = bx > ax ? 1 : -1
  return `M ${ax} ${ay} C ${ax + s * k} ${ay}, ${bx - s * k} ${by}, ${bx} ${by}`
}

/**
 * Reporting edges as world-space cubic paths, manager → report (arrowhead at
 * the report). Anchors sit on the facing tile edges along the dominant axis.
 */
export function computeEdges(layout, agents) {
  const placed = new Set(Object.keys(layout))
  const edges = []
  for (const a of agents) {
    for (const manager of managersOf(a)) {
      if (!placed.has(a.name) || !placed.has(manager)) continue
      const mp = layout[manager]
      const rp = layout[a.name]
      const [mx0, my0] = cellXY(mp.c, mp.r)
      const [rx0, ry0] = cellXY(rp.c, rp.r)
      const mx = mx0 + CELL_W / 2
      const my = my0 + CELL_H / 2
      const rx = rx0 + CELL_W / 2
      const ry = ry0 + CELL_H / 2
      const vertical = Math.abs(ry - my) >= Math.abs(rx - mx)
      let ax, ay, bx, by
      if (vertical) {
        const s = ry > my ? 1 : -1
        ax = mx
        ay = my + (s * CELL_H) / 2
        bx = rx
        by = ry - (s * CELL_H) / 2
      } else {
        const s = rx > mx ? 1 : -1
        ax = mx + (s * CELL_W) / 2
        ay = my
        bx = rx - (s * CELL_W) / 2
        by = ry
      }
      edges.push({
        id: `${a.name}→${manager}`,
        manager,
        report: a.name,
        d: edgePath(ax, ay, bx, by, vertical),
      })
    }
  }
  return edges
}

/**
 * One-shot "Group by department" arrange: department blocks on the same
 * lattice (largest first, unassigned agents last). No spacer cells — the
 * lattice gaps are sized to absorb each zone's frame chrome (see the budget
 * above), so blocks pack densely and the grid stays uniform. Output is a
 * normal layout map — fully hand-editable afterwards, exactly like Tidy.
 */
export function arrangeByDept(agents) {
  const groups = new Map()
  for (const a of agents) {
    const d = deptOf(a) || ''
    if (!groups.has(d)) groups.set(d, [])
    groups.get(d).push(a.name)
  }
  for (const list of groups.values()) list.sort()
  const order = [...groups.entries()].sort((x, y) => {
    if (!x[0]) return 1
    if (!y[0]) return -1
    return y[1].length - x[1].length || x[0].localeCompare(y[0])
  })
  const MAX_COLS = 8
  const layout = {}
  let blockC = 0
  let blockR = 0
  let bandRows = 0
  for (const [, names] of order) {
    const cols = names.length <= 2 ? names.length : names.length <= 6 ? 2 : 3
    const rows = Math.ceil(names.length / cols)
    if (blockC > 0 && blockC + cols > MAX_COLS) {
      blockC = 0
      blockR += bandRows
      bandRows = 0
    }
    names.forEach((n, i) => {
      layout[n] = { c: blockC + (i % cols), r: blockR + Math.floor(i / cols) }
    })
    blockC += cols
    bandRows = Math.max(bandRows, rows)
  }
  return layout
}
