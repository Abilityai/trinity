/**
 * Pure lattice-layout helpers for the Dashboard Grid view (trinity-enterprise#47).
 *
 * The Grid view places agent tiles on a sparse, unbounded integer lattice
 * (negative coordinates included). Layout = map `agentName → {c, r}`.
 * These functions own all coordinate math so the store and components stay
 * free of layout edge cases. No DOM, no Vue — plain data in, data out.
 */

// Interaction constants from the approved design of record (issue #47 mockup).
// GAP_X exceeds the 26px avatar overhang, so a protruding avatar always
// floats in clear space between columns.
export const CELL_W = 384
export const CELL_H = 216
export const GAP_X = 34
export const GAP_Y = 18
export const COORD_LIMIT = 60 // sanity bound, ~unbounded in practice
export const Z_MIN = 0.25
export const Z_MAX = 1.6

export function cellXY(c, r) {
  return [c * (CELL_W + GAP_X), r * (CELL_H + GAP_Y)]
}

export function cellFromCenter(cx, cy) {
  const c = Math.round(cx / (CELL_W + GAP_X))
  const r = Math.round(cy / (CELL_H + GAP_Y))
  return {
    c: Math.max(-COORD_LIMIT, Math.min(COORD_LIMIT, c)),
    r: Math.max(-COORD_LIMIT, Math.min(COORD_LIMIT, r)),
  }
}

function key(c, r) {
  return `${c},${r}`
}

function occupiedSet(layout, exceptName = null) {
  const occ = new Set()
  for (const [name, p] of Object.entries(layout)) {
    if (name === exceptName) continue
    occ.add(key(p.c, p.r))
  }
  return occ
}

function isValidPos(p) {
  return (
    p &&
    Number.isInteger(p.c) &&
    Number.isInteger(p.r) &&
    Math.abs(p.c) <= COORD_LIMIT &&
    Math.abs(p.r) <= COORD_LIMIT
  )
}

/**
 * Walk lattice cells in growing rings around `(oc, or_)` and return the first
 * free one. Ring order scans top-to-bottom, left-to-right within each ring so
 * placement reads naturally. Always terminates: the lattice is bounded by
 * COORD_LIMIT and callers never hold more tiles than cells.
 */
export function nearestFreeCell(layout, oc = 0, or_ = 0, exceptName = null) {
  const occ = occupiedSet(layout, exceptName)
  if (!occ.has(key(oc, or_))) return { c: oc, r: or_ }
  for (let radius = 1; radius <= COORD_LIMIT * 2; radius++) {
    for (let dr = -radius; dr <= radius; dr++) {
      for (let dc = -radius; dc <= radius; dc++) {
        if (Math.max(Math.abs(dc), Math.abs(dr)) !== radius) continue
        const c = oc + dc
        const r = or_ + dr
        if (Math.abs(c) > COORD_LIMIT || Math.abs(r) > COORD_LIMIT) continue
        if (!occ.has(key(c, r))) return { c, r }
      }
    }
  }
  return { c: oc, r: or_ } // unreachable in practice
}

/**
 * Default layout for a fleet: reading-order grid near the origin, system
 * agent first. Deterministic for a given agent list.
 */
export function defaultLayout(agentNames, systemNames = new Set()) {
  const sorted = [...agentNames].sort((a, b) => {
    const sa = systemNames.has(a) ? 0 : 1
    const sb = systemNames.has(b) ? 0 : 1
    return sa - sb || a.localeCompare(b)
  })
  const cols = Math.max(3, Math.ceil(Math.sqrt(sorted.length)))
  const layout = {}
  sorted.forEach((name, i) => {
    layout[name] = { c: i % cols, r: Math.floor(i / cols) }
  })
  return layout
}

/**
 * Self-healing pass (#47 AC): reconcile a saved layout against the live
 * agent list.
 *   - new agents take the first free cell near the origin
 *   - deleted agents leave their gap (their entries are dropped)
 *   - an invalid or colliding saved position resolves to the nearest free cell
 * Returns `{ layout, changed }` — `changed` signals the caller to re-persist.
 */
export function normalizeLayout(saved, agentNames) {
  const layout = {}
  let changed = false
  const names = [...agentNames]
  const source = saved && typeof saved === 'object' ? saved : {}

  // Drop entries for agents that no longer exist (gap remains free).
  if (Object.keys(source).some((n) => !agentNames.includes(n))) changed = true

  // First pass: keep valid, non-colliding saved positions.
  const pending = []
  for (const name of names) {
    const p = source[name]
    if (isValidPos(p) && !occupiedSet(layout).has(key(p.c, p.r))) {
      layout[name] = { c: p.c, r: p.r }
    } else {
      pending.push(name)
      if (p !== undefined) changed = true
    }
  }

  // Second pass: place newcomers / evicted entries near the origin.
  for (const name of pending) {
    layout[name] = nearestFreeCell(layout, 0, 0)
    changed = true
  }
  return { layout, changed }
}

/**
 * Tidy up (#47): compact row-by-row preserving reading order, anchored at
 * the layout's own top-left. Column count fixed at 3 per the design of
 * record. Returns a new layout map.
 */
export function tidyLayout(layout) {
  const names = Object.keys(layout)
  if (names.length === 0) return {}
  let minC = Infinity
  let minR = Infinity
  for (const p of Object.values(layout)) {
    if (p.c < minC) minC = p.c
    if (p.r < minR) minR = p.r
  }
  // Clamp the anchor so the compacted block stays inside COORD_LIMIT —
  // otherwise the next normalize pass would evict the out-of-bounds tiles.
  const rows = Math.ceil(names.length / 3)
  minC = Math.max(-COORD_LIMIT, Math.min(minC, COORD_LIMIT - 2))
  minR = Math.max(-COORD_LIMIT, Math.min(minR, COORD_LIMIT - rows + 1))
  const sorted = names.sort(
    (x, y) => layout[x].r - layout[y].r || layout[x].c - layout[y].c
  )
  const out = {}
  sorted.forEach((name, i) => {
    out[name] = { c: minC + (i % 3), r: minR + Math.floor(i / 3) }
  })
  return out
}

/**
 * World-space bounding box of the current constellation (used by fit-view).
 */
export function layoutBBox(layout) {
  const positions = Object.values(layout)
  if (positions.length === 0) {
    return { x: 0, y: 0, w: CELL_W, h: CELL_H }
  }
  let minC = Infinity
  let maxC = -Infinity
  let minR = Infinity
  let maxR = -Infinity
  for (const p of positions) {
    if (p.c < minC) minC = p.c
    if (p.c > maxC) maxC = p.c
    if (p.r < minR) minR = p.r
    if (p.r > maxR) maxR = p.r
  }
  return {
    x: minC * (CELL_W + GAP_X),
    y: minR * (CELL_H + GAP_Y),
    w: (maxC - minC) * (CELL_W + GAP_X) + CELL_W,
    h: (maxR - minR) * (CELL_H + GAP_Y) + CELL_H,
  }
}

/** Name of the agent occupying `(c, r)`, or null. */
export function occupantAt(layout, c, r, exceptName = null) {
  for (const [name, p] of Object.entries(layout)) {
    if (name === exceptName) continue
    if (p.c === c && p.r === r) return name
  }
  return null
}
