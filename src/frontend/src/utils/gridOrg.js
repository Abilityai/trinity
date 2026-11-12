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
import { isWidgetKey } from './gridWidgets'

/**
 * Layout entries that are NOT agents — today, info tiles (ent#325).
 *
 * Both arrange helpers below rebuild a FRESH map keyed from the `agents`
 * array, and `fleetGrid.applyLayout` replaces the layout wholesale, so
 * without this every `widget:*` position was destroyed by a single "Tidy up"
 * or "Group by dept" the moment the org overlay was on. Seeding the output
 * with these keys first both preserves them and makes them obstacles the
 * agent placement flows around, since both helpers place through
 * `nearestFreeCell`.
 */
function nonAgentEntries(layout, agents) {
  const agentNames = new Set((agents || []).map((a) => a.name))
  const out = {}
  for (const [k, p] of Object.entries(layout || {})) {
    if (!agentNames.has(k) && isWidgetKey(k)) out[k] = { c: p.c, r: p.r }
  }
  return out
}

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

/**
 * Reporting-line routing (#2066). Lines are an org chart: they leave a
 * manager at BOTTOM-centre and enter a report at TOP-centre, always, and run
 * only through the lattice gaps:
 *
 *   - BUS_DROP: the horizontal "bus" sits this far below the manager row. It
 *     is the corridor between a zone frame's bottom chrome and the next row's
 *     zone header (ZONE_CHROME contract), so a bus never crosses a zone label.
 *     Every report of one manager shares that bus, so siblings read as one
 *     tree, not a fan of separate curves.
 *   - LANE_X: vertical detours run this far right of a column's right edge:
 *     the corridor between a zone frame's right chrome and the next column's
 *     half-out avatar (AVATAR_OVERHANG).
 *   - ELBOW_R: corner radius of every turn.
 *   - ARRIVAL_SPREAD: a report with several managers gets one arrival point
 *     per manager, this far apart and centred on top-centre, so each line
 *     keeps its own final drop (its own hover/click target).
 */
const AVATAR_OVERHANG = 26 // AgentTile's .gtile-avatar: 52px, centred on the tile's left edge
export const BUS_DROP = Math.round((ZONE_CHROME.bottom + GAP_Y - ZONE_CHROME.top) / 2)
export const LANE_X = Math.round((ZONE_CHROME.right + GAP_X - AVATAR_OVERHANG) / 2)
export const ELBOW_R = 8
export const ARRIVAL_SPREAD = 16

/**
 * SVG path through orthogonal waypoints with rounded corners. Repeated and
 * collinear waypoints are dropped first; each corner's radius is clamped to
 * half of its shorter neighbouring segment so short jogs stay well-formed.
 */
export function orthogonalPath(points, r = ELBOW_R) {
  const pts = []
  for (const p of points) {
    const last = pts[pts.length - 1]
    if (last && last.x === p.x && last.y === p.y) continue
    if (pts.length >= 2) {
      const prev = pts[pts.length - 2]
      if ((prev.x === last.x && last.x === p.x) || (prev.y === last.y && last.y === p.y)) {
        pts[pts.length - 1] = p
        continue
      }
    }
    pts.push(p)
  }
  if (pts.length === 0) return ''
  let d = `M ${pts[0].x} ${pts[0].y}`
  for (let i = 1; i < pts.length - 1; i++) {
    const p0 = pts[i - 1]
    const p1 = pts[i]
    const p2 = pts[i + 1]
    const lenIn = Math.abs(p1.x - p0.x) + Math.abs(p1.y - p0.y)
    const lenOut = Math.abs(p2.x - p1.x) + Math.abs(p2.y - p1.y)
    const k = Math.min(r, lenIn / 2, lenOut / 2)
    const ux = Math.sign(p1.x - p0.x)
    const uy = Math.sign(p1.y - p0.y)
    const vx = Math.sign(p2.x - p1.x)
    const vy = Math.sign(p2.y - p1.y)
    d += ` L ${p1.x - ux * k} ${p1.y - uy * k} Q ${p1.x} ${p1.y} ${p1.x + vx * k} ${p1.y + vy * k}`
  }
  const end = pts[pts.length - 1]
  if (pts.length > 1) d += ` L ${end.x} ${end.y}`
  return d
}

/**
 * Arrowhead polygon at the endpoint, oriented along the arrival axis.
 * Reporting lines always arrive from above (`vertical`, `s = 1`); the other
 * orientations are kept for callers drawing non-org arrows.
 */
export function arrowheadPath(bx, by, vertical, s) {
  if (vertical) {
    return `M ${bx} ${by} L ${bx - 4} ${by - s * 8} L ${bx + 4} ${by - s * 8} Z`
  }
  return `M ${bx} ${by} L ${bx - s * 8} ${by - 4} L ${bx - s * 8} ${by + 4} Z`
}

/** x of the vertical lane just right of column `c` (inside the gap). */
function laneRightOf(c) {
  return cellXY(c, 0)[0] + CELL_W + LANE_X
}

/**
 * Waypoints for one reporting line, manager cell `m` → report cell `r`,
 * arriving at `bx` on the report's top edge. Three shapes:
 *
 *   1. Report one row below, or further below with every cell in between in
 *      its column empty: trunk down to the bus, along the bus, straight drop.
 *   2. Report further below with a tile in the way: trunk, bus, then down the
 *      lane beside the report's column to the gap above its row, then in.
 *   3. Report on the same row or above (the layout is the user's, so this
 *      happens): trunk, bus, up the lane beside the manager's column to the
 *      gap above the report's row, then in from above. The line loops, which
 *      is the honest picture of an upside-down pair; Tidy / Group by dept
 *      keep it rare.
 */
function routePoints(m, r, bx, occupied) {
  const [mx0, my0] = cellXY(m.c, m.r)
  const [, ry0] = cellXY(r.c, r.r)
  const ax = mx0 + CELL_W / 2
  const ay = my0 + CELL_H
  const by = ry0
  const busY = ay + BUS_DROP
  const approachY = by - GAP_Y + BUS_DROP
  const start = [{ x: ax, y: ay }, { x: ax, y: busY }]

  if (r.r > m.r) {
    let blocked = false
    for (let row = m.r + 1; row < r.r; row++) {
      if (occupied.has(`${r.c},${row}`)) {
        blocked = true
        break
      }
    }
    if (!blocked) return [...start, { x: bx, y: busY }, { x: bx, y: by }]
    // Lane on the side of the report's column that faces the manager.
    const lane = m.c <= r.c ? laneRightOf(r.c - 1) : laneRightOf(r.c)
    return [...start, { x: lane, y: busY }, { x: lane, y: approachY }, { x: bx, y: approachY }, { x: bx, y: by }]
  }

  // Same row or above: detour up the lane on the report's side of the manager.
  const lane = r.c < m.c ? laneRightOf(m.c - 1) : laneRightOf(m.c)
  return [...start, { x: lane, y: busY }, { x: lane, y: approachY }, { x: bx, y: approachY }, { x: bx, y: by }]
}

/**
 * Reporting edges as world-space orthogonal paths, manager → report
 * (arrowhead at the report: direction IS the payload, so every edge carries
 * `ah`). Every edge leaves the manager's bottom-centre and enters the
 * report's top-centre (#2066); see `routePoints` for the three shapes.
 *
 * `d` is the full line. `hit` is only the part no other edge of the same
 * manager shares (the final drop into the report), so a hover or a
 * click-to-remove on a shared trunk or bus can never pick an arbitrary
 * sibling. `mid` sits on that same final drop, for the hover chip.
 * Edges whose endpoint isn't placed (dangling tag, filtered roster) are skipped.
 */
export function computeEdges(layout, agents) {
  const placed = new Set(Object.keys(layout))
  const occupied = new Set(Object.values(layout).map((p) => `${p.c},${p.r}`))
  const edges = []
  for (const a of agents) {
    if (!placed.has(a.name)) continue
    const managers = managersOf(a).filter((m) => m !== a.name && placed.has(m))
    managers.forEach((manager, i) => {
      const rp = layout[a.name]
      const [rx0, ry0] = cellXY(rp.c, rp.r)
      const bx = rx0 + CELL_W / 2 + (i - (managers.length - 1) / 2) * ARRIVAL_SPREAD
      const by = ry0
      const pts = routePoints(layout[manager], rp, bx, occupied)
      // The final drop: from the last turn into the report's top edge.
      const turn = pts[pts.length - 2]
      edges.push({
        id: `${a.name}→${manager}`,
        manager,
        report: a.name,
        d: orthogonalPath(pts),
        hit: `M ${turn.x} ${turn.y} L ${bx} ${by}`,
        ah: arrowheadPath(bx, by, true, 1),
        mid: { x: bx, y: (turn.y + by) / 2 },
      })
    })
  }
  return edges
}

/**
 * Edges bucketed for painting: dimmed first, then normal, then highlighted,
 * so a highlighted line is never painted over by a sibling that shares its
 * trunk or bus. Each tier is drawn as ONE translucent group, which keeps a
 * segment shared by several edges the same tone as an unshared one (per-path
 * opacity would stack and darken every shared trunk).
 * `classOf(edge)` returns 'hot', 'dim' or '' (the overlay's edgeClass).
 */
export function paintTiers(edges, classOf) {
  const tiers = { dim: [], base: [], hot: [] }
  for (const e of edges) {
    const cls = classOf(e)
    tiers[cls === 'hot' || cls === 'dim' ? cls : 'base'].push(e)
  }
  return ['dim', 'base', 'hot']
    .filter((cls) => tiers[cls].length > 0)
    .map((cls) => ({ cls, edges: tiers[cls] }))
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
export function arrangeByDept(agents, meta, layout = null) {
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
  // Seed with the info tiles so their positions survive the arrange and the
  // department blocks flow around them (ent#325 AC). With no widgets on the
  // board `nearestFreeCell` returns every requested cell unchanged, so the
  // department layout is byte-identical to the pre-ent#325 behaviour — pinned
  // by a test, because that equivalence is the whole reason this is safe.
  const out = nonAgentEntries(layout, agents)
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
      out[n] = nearestFreeCell(out, blockC + (i % cols), blockR + Math.floor(i / cols))
    })
    blockC += cols
    bandRows = Math.max(bandRows, rows)
  }
  return out
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
  // Seed with the info tiles (ent#325): preserved, and treated as obstacles
  // by the `nearestFreeCell` placement below.
  const out = nonAgentEntries(layout, agents)
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
