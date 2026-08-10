/**
 * Org-overlay helpers for the Dashboard Grid view (zones + reporting lines,
 * trinity-enterprise#305). Pure functions over the existing lattice layout;
 * no DOM, no Vue. The grid's coordinate model is untouched — zones are
 * DERIVED (hull model) from wherever the member tiles sit, never a
 * constraint.
 *
 * Data conventions (reads tolerant, writes namespaced):
 *   department      → `dept-<name>` tag on the agent
 *   reporting line  → `reports-to-<agent>` tag on the REPORT agent
 *                     (direction is encoded by which row carries the tag)
 *   bootstrap mode  → while NO agent in the fleet carries a `dept-*` tag,
 *                     an agent's first plain tag counts as its department so
 *                     existing tag-organized fleets see zones on day one.
 *                     Bootstrap zones are READ-ONLY (never drop-assigned) and
 *                     the fallback switches off fleet-wide the moment the
 *                     first explicit `dept-*` tag appears.
 * (Hyphen namespaces, not colons — the tags API allows [a-z0-9-] only.
 *  Server-side these prefixes are reserved against agent-principal writes;
 *  keep in sync with ORG_TAG_PREFIXES in src/backend/db/tags.py.)
 */

import { CELL_W, CELL_H, GAP_X, GAP_Y, cellXY, nearestFreeCell } from './gridLayout'

export const DEPT_PREFIX = 'dept-'
export const REPORTS_PREFIX = 'reports-to-'

/** Mirror of the tags router's cap (routers/tags.py) — client preflight only. */
export const TAG_MAX_LENGTH = 50

/**
 * Zone-frame chrome budget in px. The lattice gaps in gridLayout.js are sized
 * so this chrome fits INSIDE a regular gap — two departments can occupy
 * adjacent rows/columns with no spacer cells and no frame collision. The
 * contract (left+right ≤ GAP_X, top+bottom ≤ GAP_Y) is pinned by a unit test.
 */
export const ZONE_CHROME = { left: 22, right: 10, top: 34, bottom: 10 }

/** True for tags that carry org-overlay facts — generic tag UIs hide these. */
export function isOrgTag(tag) {
  return (
    typeof tag === 'string' &&
    (tag.startsWith(DEPT_PREFIX) || tag.startsWith(REPORTS_PREFIX))
  )
}

/** Client preflight for the router's 50-char tag cap. */
export function orgTagFits(prefix, value) {
  return (prefix + value).length <= TAG_MAX_LENGTH
}

/**
 * Fleet-wide org context. `bootstrap` = no explicit `dept-*` tag anywhere,
 * which is the only state where the plain-tag fallback applies.
 */
export function orgMeta(agents) {
  const bootstrap = !agents.some((a) =>
    (a.tags || []).some((t) => t.startsWith(DEPT_PREFIX) && t.length > DEPT_PREFIX.length)
  )
  return { bootstrap }
}

/** Department of an agent under the given org context, or null. */
export function deptOf(agent, meta) {
  const tags = agent.tags || []
  const namespaced = tags.find(
    (t) => t.startsWith(DEPT_PREFIX) && t.length > DEPT_PREFIX.length
  )
  if (namespaced) return namespaced.slice(DEPT_PREFIX.length)
  const m = meta || { bootstrap: true }
  if (!m.bootstrap) return null
  return tags.find((t) => !isOrgTag(t)) || null
}

/** Manager names this agent reports to (usually 0 or 1, multiple allowed). */
export function managersOf(agent) {
  return (agent.tags || [])
    .filter((t) => t.startsWith(REPORTS_PREFIX) && t.length > REPORTS_PREFIX.length)
    .map((t) => t.slice(REPORTS_PREFIX.length))
}

/** All departments present in a fleet under the given context, sorted. */
export function allDepts(agents, meta) {
  const m = meta || orgMeta(agents)
  return [...new Set(agents.map((a) => deptOf(a, m)).filter(Boolean))].sort()
}

/**
 * Stable palette slot for a department: hash of the NAME, not its position
 * in the current dept list — adding or removing a department never recolors
 * the others. 8 slots (`--gv-dept-0..7` CSS vars themed in FleetGrid.vue);
 * distinct departments may share a slot past 8 — accepted, and zone labels /
 * ribbons always carry the name so identity never rides on hue alone.
 */
export const DEPT_SLOT_COUNT = 8

export function deptSlot(dept) {
  let h = 5381
  for (let i = 0; i < dept.length; i++) {
    h = ((h << 5) + h + dept.charCodeAt(i)) >>> 0
  }
  return h % DEPT_SLOT_COUNT
}

/** name → { name: dept, slot } for ribbon rendering on tiles. */
export function deptInfoByAgent(agents, meta) {
  const m = meta || orgMeta(agents)
  const map = {}
  for (const a of agents) {
    const d = deptOf(a, m)
    if (d) map[a.name] = { name: d, slot: deptSlot(d) }
  }
  return map
}

/**
 * Derived zone hulls: one pixel rect per department, wrapping the bounding
 * box of its placed member tiles, padded by ZONE_CHROME (left pad clears the
 * half-out avatar; top pad is the header band). `readOnly` marks bootstrap
 * (fallback-derived) zones — they render but never accept drop-assign.
 */
export function computeZones(layout, agents, meta) {
  const m = meta || orgMeta(agents)
  const groups = new Map()
  for (const a of agents) {
    const d = deptOf(a, m)
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
      slot: deptSlot(dept),
      x: x0 - ZONE_CHROME.left,
      y: y0 - ZONE_CHROME.top,
      w: x1 + CELL_W - x0 + ZONE_CHROME.left + ZONE_CHROME.right,
      h: y1 + CELL_H - y0 + ZONE_CHROME.top + ZONE_CHROME.bottom,
      count: g.members.length,
      running: g.running,
      readOnly: m.bootstrap,
      anchor: { c: minC, r: minR },
      extent: { maxC, maxR },
    })
  }
  return zones.sort((a, b) => a.dept.localeCompare(b.dept))
}

/**
 * Point-in-zone test for the drag-to-assign drop affordance. Overlapping
 * hulls (interleaved manual layouts) resolve to the SMALLEST hit — the most
 * specific zone under the cursor, and deterministic.
 */
export function zoneAt(zones, x, y) {
  let best = null
  for (const z of zones) {
    if (x < z.x || x > z.x + z.w || y < z.y || y > z.y + z.h) continue
    if (!best || z.w * z.h < best.w * best.h) best = z
  }
  return best
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

/** Arrowhead polygon at the endpoint, oriented along the arrival axis. */
export function arrowheadPath(bx, by, vertical, s) {
  if (vertical) {
    return `M ${bx} ${by} L ${bx - 4} ${by - s * 8} L ${bx + 4} ${by - s * 8} Z`
  }
  return `M ${bx} ${by} L ${bx - s * 8} ${by - 4} L ${bx - s * 8} ${by + 4} Z`
}

/**
 * Reporting edges as world-space cubic paths, manager → report (arrowhead at
 * the report — direction IS the payload, so every edge carries `ah`).
 * Anchors sit on the facing tile edges along the dominant axis. Edges whose
 * endpoint isn't placed (dangling tag, filtered roster) are skipped.
 */
export function computeEdges(layout, agents) {
  const placed = new Set(Object.keys(layout))
  const edges = []
  for (const a of agents) {
    for (const manager of managersOf(a)) {
      if (manager === a.name) continue
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
      let ax, ay, bx, by, s
      if (vertical) {
        s = ry > my ? 1 : -1
        ax = mx
        ay = my + (s * CELL_H) / 2
        bx = rx
        by = ry - (s * CELL_H) / 2
      } else {
        s = rx > mx ? 1 : -1
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
        ah: arrowheadPath(bx, by, vertical, s),
        mid: { x: (ax + bx) / 2, y: (ay + by) / 2 },
      })
    }
  }
  return edges
}

function blockCols(n) {
  return n <= 2 ? n : n <= 6 ? 2 : 3
}

/**
 * One-shot "Group by department" arrange: department blocks on the same
 * lattice (largest first, unassigned agents last). No spacer cells — the
 * lattice gaps absorb each zone's frame chrome (ZONE_CHROME contract), so
 * blocks pack densely and the grid stays uniform. Output is a normal layout
 * map — fully hand-editable afterwards, exactly like Tidy.
 */
export function arrangeByDept(agents, meta) {
  const m = meta || orgMeta(agents)
  const groups = new Map()
  for (const a of agents) {
    const d = deptOf(a, m) || ''
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
    const cols = blockCols(names.length)
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

/**
 * Zone-aware Tidy: compact each department (and the unassigned group) into a
 * block anchored at its OWN current top-left — "clean up in place", unlike
 * arrangeByDept's canonical packing. Groups are laid down in reading order of
 * their anchors; a collision with an already-placed block resolves to the
 * nearest free cell, so the result is always collision-free and
 * deterministic for a given input.
 */
export function tidyByDept(layout, agents, meta) {
  const m = meta || orgMeta(agents)
  const groups = new Map()
  for (const a of agents) {
    if (!layout[a.name]) continue
    const d = deptOf(a, m) || ''
    if (!groups.has(d)) groups.set(d, [])
    groups.get(d).push(a.name)
  }
  const entries = [...groups.entries()].map(([dept, names]) => {
    let minC = Infinity
    let minR = Infinity
    for (const n of names) {
      const p = layout[n]
      if (p.c < minC) minC = p.c
      if (p.r < minR) minR = p.r
    }
    const ordered = [...names].sort(
      (x, y) => layout[x].r - layout[y].r || layout[x].c - layout[y].c
    )
    return { dept, names: ordered, minC, minR }
  })
  entries.sort((a, b) => a.minR - b.minR || a.minC - b.minC)
  const out = {}
  for (const g of entries) {
    const cols = blockCols(g.names.length)
    g.names.forEach((n, i) => {
      out[n] = nearestFreeCell(out, g.minC + (i % cols), g.minR + Math.floor(i / cols))
    })
  }
  return out
}

/**
 * Placement origin for a newcomer joining an existing department: the first
 * free cell scanning from just right of its zone's hull. Feeds
 * normalizeLayout's `originFor` hook; agents without a placed department
 * keep the default origin.
 */
export function newcomerOrigin(name, layout, agents, meta) {
  const m = meta || orgMeta(agents)
  const agent = agents.find((a) => a.name === name)
  if (!agent) return null
  const d = deptOf(agent, m)
  if (!d) return null
  const zones = computeZones(layout, agents, m)
  const zone = zones.find((z) => z.dept === d)
  if (!zone) return null
  return { c: zone.extent.maxC + 1, r: zone.anchor.r }
}

/* GAP re-export so the chrome-budget unit test pins the contract in one
   place: ZONE_CHROME must fit inside the lattice gaps. */
export { GAP_X, GAP_Y }
