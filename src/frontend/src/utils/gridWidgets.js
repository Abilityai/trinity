/**
 * Grid widget registry (trinity-enterprise#325, foundation for epic #94).
 *
 * Info tiles are just another occupant of the same magnetic lattice agent
 * tiles live on: same 384x216 chassis, same `--gv-*` tokens, same drag/swap/
 * tidy/keyboard physics. The ONLY thing that distinguishes them in the layout
 * map is the key namespace.
 *
 * ## Why a `widget:` key prefix rather than a second map
 *
 * The drag machinery in `FleetGrid.vue` is keyed entirely off `layout[name]`
 * and a `data-agent` attribute. Putting widgets in the SAME map means drag,
 * swap-with-preview, keyboard reorder, viewport culling and the snap socket
 * all work for widgets with no second code path — which is also the only way
 * "full parity with agent tiles" stays true as those behaviours evolve.
 *
 * `:` is safe as the separator because it cannot appear in an agent name:
 * `utils/helpers.sanitize_agent_name` strips everything outside
 * `[A-Za-z0-9_-]`, so a `widget:*` key can never collide with a real agent,
 * and `isWidgetKey` is an exact answer rather than a heuristic.
 *
 * ## Registry shape
 *
 * `{ id, title, icon, component, adminOnly, defaultOn, wantsTick, cells }`
 *
 * ## The prop contract the chassis honours (ent#100)
 *
 * `FleetGrid` renders `<component :is="entry.component" :agents :now />`:
 *
 *   - **`agents`** — the UNFILTERED fleet roster (`orgAgents || agents`, the
 *     seam #305 already established). It is a LOOKUP TABLE (display labels,
 *     "can the fleet be enumerated at all"), never a data source: a tile's
 *     numbers come from the store, which fetches on the one batch poll. It is
 *     deliberately not the ent#261 type-to-filter-narrowed list — an info tile
 *     is fleet-scope, so narrowing it would degrade every non-matching row's
 *     label to a raw slug, per keystroke, on a default-on tile.
 *   - **`now`** — the Grid's shared 1s tick, passed ONLY to tiles that declare
 *     `wantsTick: true`. Binding it unconditionally would force a child update
 *     once per second for every tile, forever, on a canvas that also drags at
 *     60fps; epic #94 queues eight tiles, most of which render no clock.
 *
 * A tile needing anything else reads a store — it must NOT fetch in
 * `onMounted`, because viewport culling unmounts it and panning would re-issue.
 *
 * `cells` is declared now and deliberately ignored by v1 rendering: every
 * tile occupies one cell. Multi-cell occupancy is epic #94's stated v2, and
 * declaring the field now means the catalog entries do not have to be rewritten
 * when `gridLayout.js` learns occupancy — the registry is the thing a future
 * "add custom tile" feature appends to, so its shape wants to be stable.
 */

import { COORD_LIMIT } from './gridLayout'

export const WIDGET_PREFIX = 'widget:'

/** True for a layout key that belongs to an info tile rather than an agent. */
export function isWidgetKey(key) {
  return typeof key === 'string' && key.startsWith(WIDGET_PREFIX)
}

/** `widget:fleet-summary` -> `fleet-summary`. */
export function widgetIdFromKey(key) {
  return isWidgetKey(key) ? key.slice(WIDGET_PREFIX.length) : null
}

/** `fleet-summary` -> `widget:fleet-summary`. */
export function widgetKey(id) {
  return `${WIDGET_PREFIX}${id}`
}

/**
 * The tile catalog.
 *
 * v1 ships ONE tile — the chassis needs a real occupant for "default-on tiles
 * render above the constellation" to be a testable claim rather than an
 * assertion about an empty list, and epic #94's own acceptance criteria
 * require it. It is deliberately the cheapest possible tile: it reads the
 * agent array the grid already holds and issues no request, so the chassis
 * carries no data dependency of its own and cannot be blocked by the sibling
 * `GET /api/executions/timeline` work.
 *
 * The real catalog (#95 host resources, #96 executions, #97 Cornelius mind,
 * #98 cost, #99 next schedules, #100 recent failures, #101 tokens, #259
 * subscription pressure) appends here, one entry per sub-issue.
 *
 * `component` is a resolved component, not a name string: an unknown name
 * fails at render as a blank tile, whereas an unresolved import fails at build.
 */
export const GRID_WIDGETS = []

/** Register a tile. Exported so sub-issues add catalog entries in one line. */
export function registerWidget(entry) {
  const existing = GRID_WIDGETS.findIndex((w) => w.id === entry.id)
  const normalized = {
    adminOnly: false,
    defaultOn: false,
    wantsTick: false,
    cells: { w: 1, h: 1 },
    ...entry,
  }
  if (existing >= 0) GRID_WIDGETS[existing] = normalized
  else GRID_WIDGETS.push(normalized)
  return normalized
}

/** Catalog entry by id, or null. */
export function widgetById(id) {
  return GRID_WIDGETS.find((w) => w.id === id) || null
}

/**
 * The tiles this viewer may see: catalog minus `adminOnly` for non-admins.
 *
 * Visibility is a SEPARATE question from access. This only decides what is
 * offered in the "Tiles" menu and rendered; every tile's data still comes
 * from an access-scoped endpoint, so hiding is a UI courtesy and never the
 * control (epic #94 AC: "all tile data inherits endpoint access scoping").
 */
export function catalogFor(isAdmin) {
  return GRID_WIDGETS.filter((w) => !w.adminOnly || isAdmin)
}

/**
 * Is this tile on, given the user's stored preferences?
 *
 * `prefs` is a sparse `{ id: boolean }` OVERRIDE map, not a list of enabled
 * ids. Absent means "follow the catalog's `defaultOn`", which is what makes a
 * tile added in a later release appear for existing users instead of staying
 * invisible behind a stale allow-list — and equally lets a user keep a
 * default-on tile switched off. "Reset" is therefore just `prefs = {}`.
 */
export function isWidgetEnabled(entry, prefs = {}) {
  if (!entry) return false
  const override = prefs[entry.id]
  return typeof override === 'boolean' ? override : !!entry.defaultOn
}

/** Keys of the tiles that should currently be on the board, in catalog order. */
export function enabledWidgetKeys(isAdmin, prefs = {}) {
  return catalogFor(isAdmin)
    .filter((w) => isWidgetEnabled(w, prefs))
    .map((w) => widgetKey(w.id))
}

/**
 * Default placement band for info tiles: the rows directly ABOVE the agent
 * block, filled left-to-right.
 *
 * Seeding above rather than into the agent area is what makes epic #94's
 * "default-on tiles appear without moving any saved agent position" true by
 * construction — `defaultLayout` starts agents at r=0 and grows downward, so
 * negative rows are unclaimed on every existing install.
 *
 * `isBlocked(c, r)` lets the caller veto a cell. The org overlay (#305) is the
 * reason it exists: rows -3..-1 are empty of TILES, but a department whose
 * members were dragged upward has a zone hull that reaches into them, and a
 * widget seeded inside that frame would read as a member of a department it
 * has nothing to do with. Optional — without it the band is used as-is.
 */
export function seedWidgetCells(layout, keys, isBlocked = null, band = [-3, -1]) {
  const out = {}
  if (!keys || keys.length === 0) return out
  const occupied = new Set()
  for (const p of Object.values(layout || {})) {
    if (p && Number.isInteger(p.c) && Number.isInteger(p.r)) occupied.add(`${p.c},${p.r}`)
  }
  const [rTop, rBottom] = band
  let c = 0
  let r = rTop
  for (const key of keys) {
    // Walk the band left-to-right, row by row, skipping taken/vetoed cells.
    let guard = 0
    while (guard++ < COORD_LIMIT * 4) {
      const taken = occupied.has(`${c},${r}`)
      const vetoed = isBlocked ? isBlocked(c, r) : false
      if (!taken && !vetoed) break
      c++
      if (c > COORD_LIMIT) {
        c = 0
        r = r < rBottom ? r + 1 : rTop - 1 // spill upward, never into the agents
      }
    }
    out[key] = { c, r }
    occupied.add(`${c},${r}`)
    c++
    if (c > COORD_LIMIT) {
      c = 0
      r = r < rBottom ? r + 1 : rTop - 1
    }
  }
  return out
}

// --- catalog registration lives ELSEWHERE, on purpose --------------------
//
// This module stays PURE (no `.vue` imports) so `vitest.config.js`'s node
// environment can import it directly — the registry, the key namespace and
// the seeding band are exactly the logic that needs unit tests, and they
// would be untestable here if importing this file dragged in an SFC.
// `gridOrg.js` also imports `isWidgetKey` from here, so a component import
// would break the existing `gridOrg.spec.js` too.
//
// Catalog entries therefore live in `components/tiles/catalog.js`, which
// `FleetGrid.vue` imports for its side effect. Sub-issues add their tile
// there, in one `registerWidget({...})` block.
