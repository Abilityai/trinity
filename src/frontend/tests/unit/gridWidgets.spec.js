import { describe, it, expect, beforeEach } from 'vitest'
import {
  WIDGET_PREFIX,
  GRID_WIDGETS,
  isWidgetKey,
  widgetKey,
  widgetIdFromKey,
  registerWidget,
  widgetById,
  catalogFor,
  isWidgetEnabled,
  enabledWidgetKeys,
  seedWidgetCells,
} from '@/utils/gridWidgets'
import { normalizeLayout, CELL_W, CELL_H } from '@/utils/gridLayout'
import { tidyByDept, arrangeByDept, computeZones, orgMeta } from '@/utils/gridOrg'

/**
 * ent#325 — the widget chassis's pure half.
 *
 * `utils/gridWidgets.js` is deliberately free of `.vue` imports so it can be
 * tested here under vitest's node environment; the catalog entries (which do
 * import components) live in `components/tiles/catalog.js` instead. That
 * split is what makes the registry testable at all, so the first test pins it.
 */

function resetCatalog() {
  GRID_WIDGETS.length = 0
}

const AGENTS = [
  { name: 'alpha', tags: ['dept-eng'] },
  { name: 'beta', tags: ['dept-eng'] },
  { name: 'gamma', tags: ['dept-sales'] },
]

describe('widget key namespace', () => {
  it('round-trips id <-> key', () => {
    expect(widgetKey('fleet-summary')).toBe('widget:fleet-summary')
    expect(widgetIdFromKey('widget:fleet-summary')).toBe('fleet-summary')
  })

  it('never mistakes an agent name for a widget', () => {
    // `sanitize_agent_name` strips everything outside [A-Za-z0-9_-], so a real
    // agent name can never contain `:` — this is exact, not a heuristic.
    for (const name of ['widget', 'widget-summary', 'my_agent', 'a-b-c', '']) {
      expect(isWidgetKey(name)).toBe(false)
    }
    expect(isWidgetKey(`${WIDGET_PREFIX}x`)).toBe(true)
  })

  it('widgetIdFromKey returns null for a non-widget key', () => {
    expect(widgetIdFromKey('alpha')).toBeNull()
  })
})

describe('registry', () => {
  beforeEach(resetCatalog)

  it('registers with defaults and looks up by id', () => {
    const e = registerWidget({ id: 'x', title: 'X', component: {} })
    expect(e.adminOnly).toBe(false)
    expect(e.defaultOn).toBe(false)
    expect(e.cells).toEqual({ w: 1, h: 1 })
    expect(widgetById('x')).toBe(e)
    expect(widgetById('nope')).toBeNull()
  })

  it('re-registering the same id replaces rather than duplicates', () => {
    registerWidget({ id: 'x', title: 'first', component: {} })
    registerWidget({ id: 'x', title: 'second', component: {} })
    expect(GRID_WIDGETS.filter((w) => w.id === 'x')).toHaveLength(1)
    expect(widgetById('x').title).toBe('second')
  })

  it('hides adminOnly tiles from non-admins', () => {
    registerWidget({ id: 'open', title: 'Open', component: {} })
    registerWidget({ id: 'secret', title: 'Secret', component: {}, adminOnly: true })
    expect(catalogFor(true).map((w) => w.id)).toEqual(['open', 'secret'])
    expect(catalogFor(false).map((w) => w.id)).toEqual(['open'])
  })

  it('an adminOnly tile is never enabled for a non-admin, even if prefs say on', () => {
    // Prefs are per-user and survive a role change; the catalog filter, not
    // the preference, has to be what decides.
    registerWidget({ id: 'secret', title: 'S', component: {}, adminOnly: true, defaultOn: true })
    expect(enabledWidgetKeys(false, { secret: true })).toEqual([])
    expect(enabledWidgetKeys(true, {})).toEqual(['widget:secret'])
  })
})

describe('enablement prefs are a sparse override map', () => {
  beforeEach(() => {
    resetCatalog()
    registerWidget({ id: 'on', title: 'On', component: {}, defaultOn: true })
    registerWidget({ id: 'off', title: 'Off', component: {}, defaultOn: false })
  })

  it('absent pref follows the catalog default', () => {
    expect(enabledWidgetKeys(true, {})).toEqual(['widget:on'])
  })

  it('a user can turn a default-on tile off and a default-off tile on', () => {
    expect(enabledWidgetKeys(true, { on: false, off: true })).toEqual(['widget:off'])
  })

  it('reset (empty prefs) restores defaults', () => {
    expect(enabledWidgetKeys(true, {})).toEqual(['widget:on'])
  })

  it('a NEW default-on tile appears for a user with existing prefs', () => {
    // The reason prefs store overrides rather than an allow-list: a stale
    // allow-list would keep every future tile invisible forever.
    const prefs = { on: false }
    registerWidget({ id: 'brandnew', title: 'New', component: {}, defaultOn: true })
    expect(enabledWidgetKeys(true, prefs)).toContain('widget:brandnew')
  })

  it('isWidgetEnabled is false for a missing entry', () => {
    expect(isWidgetEnabled(null, {})).toBe(false)
  })
})

describe('seedWidgetCells — the band above the fleet', () => {
  it('seeds into negative rows so no saved agent position moves', () => {
    const agents = { a: { c: 0, r: 0 }, b: { c: 1, r: 0 }, c: { c: 2, r: 1 } }
    const seeded = seedWidgetCells(agents, ['widget:one', 'widget:two'])
    for (const p of Object.values(seeded)) expect(p.r).toBeLessThan(0)
    // and it did not touch the agents
    expect(agents).toEqual({ a: { c: 0, r: 0 }, b: { c: 1, r: 0 }, c: { c: 2, r: 1 } })
  })

  it('never collides with an existing occupant or with a sibling widget', () => {
    const layout = { a: { c: 0, r: -3 } }
    const seeded = seedWidgetCells(layout, ['widget:one', 'widget:two', 'widget:three'])
    const cells = Object.values(seeded).map((p) => `${p.c},${p.r}`)
    expect(new Set(cells).size).toBe(cells.length)
    expect(cells).not.toContain('0,-3')
  })

  it('honours the isBlocked veto (a department frame reaching into the band)', () => {
    const blocked = (c, r) => r === -3 && c < 2
    const seeded = seedWidgetCells({}, ['widget:one'], blocked)
    expect(blocked(seeded['widget:one'].c, seeded['widget:one'].r)).toBe(false)
  })

  it('returns an empty map for no keys', () => {
    expect(seedWidgetCells({}, [])).toEqual({})
    expect(seedWidgetCells({}, null)).toEqual({})
  })
})

describe('normalizeLayout round-trips widget keys', () => {
  it('DROPS widget keys when not declared — the pre-ent#325 behaviour', () => {
    // Pinned so the regression is visible: this is exactly why a saved
    // layout lost its tiles on the next reconcile.
    const saved = { a: { c: 0, r: 0 }, 'widget:x': { c: 0, r: -1 } }
    const { layout } = normalizeLayout(saved, ['a'])
    expect(layout['widget:x']).toBeUndefined()
  })

  it('keeps widget positions when declared', () => {
    const saved = { a: { c: 0, r: 0 }, 'widget:x': { c: 3, r: -2 } }
    const { layout } = normalizeLayout(saved, ['a'], null, ['widget:x'])
    expect(layout['widget:x']).toEqual({ c: 3, r: -2 })
    expect(layout.a).toEqual({ c: 0, r: 0 })
  })

  it('does not report `changed` on a steady-state board carrying a widget', () => {
    // A false `changed` re-persists localStorage on every reconcile forever.
    const saved = { a: { c: 0, r: 0 }, 'widget:x': { c: 3, r: -2 } }
    const { changed } = normalizeLayout(saved, ['a'], null, ['widget:x'])
    expect(changed).toBe(false)
  })

  it('an agent wins a collision; the widget is re-placed', () => {
    const saved = { a: { c: 1, r: 1 }, 'widget:x': { c: 1, r: 1 } }
    const { layout } = normalizeLayout(saved, ['a'], null, ['widget:x'])
    expect(layout.a).toEqual({ c: 1, r: 1 })
    expect(layout['widget:x']).not.toEqual({ c: 1, r: 1 })
  })

  it('originFor is never consulted for a widget', () => {
    // originFor is the org overlay's zone-aware seeding hook and is agent-only
    // by contract — a widget has no department.
    const seen = []
    normalizeLayout({}, ['a'], (n) => { seen.push(n); return { c: 4, r: 4 } }, ['widget:x'])
    expect(seen).toEqual(['a'])
  })
})

describe('org overlay preserves widget positions (ent#325 interlock A)', () => {
  const meta = orgMeta(AGENTS)

  it('tidyByDept carries widget keys through', () => {
    const layout = {
      alpha: { c: 0, r: 0 },
      beta: { c: 1, r: 0 },
      gamma: { c: 0, r: 2 },
      'widget:x': { c: 0, r: -2 },
    }
    const out = tidyByDept(layout, AGENTS, meta)
    expect(out['widget:x']).toEqual({ c: 0, r: -2 })
  })

  it('arrangeByDept carries widget keys through', () => {
    const layout = { alpha: { c: 0, r: 0 }, 'widget:x': { c: 5, r: -1 } }
    const out = arrangeByDept(AGENTS, meta, layout)
    expect(out['widget:x']).toEqual({ c: 5, r: -1 })
  })

  it('arrangeByDept is byte-identical to the old behaviour with no widgets', () => {
    // The safety argument for adding nearestFreeCell to arrangeByDept: with
    // no widgets every requested cell is free, so placement cannot change.
    const withNothing = arrangeByDept(AGENTS, meta)
    const withEmptyLayout = arrangeByDept(AGENTS, meta, {})
    expect(withEmptyLayout).toEqual(withNothing)
    // three agents, two departments, packed from the origin
    expect(Object.keys(withNothing).sort()).toEqual(['alpha', 'beta', 'gamma'])
    for (const p of Object.values(withNothing)) {
      expect(p.r).toBeGreaterThanOrEqual(0)
    }
  })

  it('a widget sitting in the agent area displaces the department block, not itself', () => {
    const layout = { 'widget:x': { c: 0, r: 0 } }
    const out = arrangeByDept(AGENTS, meta, layout)
    expect(out['widget:x']).toEqual({ c: 0, r: 0 })
    for (const name of ['alpha', 'beta', 'gamma']) {
      expect(out[name]).not.toEqual({ c: 0, r: 0 })
    }
  })

  it('non-widget stray keys are NOT carried (only widgets are)', () => {
    // Guard against turning the carry-through into "preserve anything unknown",
    // which would resurrect a deleted agent's position forever.
    const layout = { alpha: { c: 0, r: 0 }, 'ghost-agent': { c: 9, r: 9 } }
    const out = arrangeByDept(AGENTS, meta, layout)
    expect(out['ghost-agent']).toBeUndefined()
  })
})

describe('widgets stay out of department zone hulls (interlock B)', () => {
  it('a widget parked beside a department does not stretch its frame', () => {
    const withoutWidget = computeZones(
      { alpha: { c: 0, r: 0 }, beta: { c: 1, r: 0 }, gamma: { c: 0, r: 3 } },
      AGENTS,
      orgMeta(AGENTS)
    )
    const withWidget = computeZones(
      {
        alpha: { c: 0, r: 0 },
        beta: { c: 1, r: 0 },
        gamma: { c: 0, r: 3 },
        'widget:x': { c: 8, r: 0 },
      },
      AGENTS,
      orgMeta(AGENTS)
    )
    expect(withWidget).toEqual(withoutWidget)
  })

  it('sanity: the hull does cover its own members', () => {
    const zones = computeZones(
      { alpha: { c: 0, r: 0 }, beta: { c: 1, r: 0 } },
      AGENTS,
      orgMeta(AGENTS)
    )
    const eng = zones.find((z) => z.dept === 'eng')
    expect(eng).toBeTruthy()
    expect(eng.w).toBeGreaterThan(CELL_W)
    expect(eng.h).toBeGreaterThanOrEqual(CELL_H)
  })
})
