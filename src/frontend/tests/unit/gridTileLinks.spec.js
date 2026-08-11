import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

/**
 * Every info-tile link must name a route that EXISTS (trinity-enterprise#325).
 *
 * `FleetSummaryTile` shipped `:link-to="{ name: 'Agents' }"`. There is no route
 * named `Agents` — the standalone Agents page was folded into the Dashboard
 * (#1109-era), leaving `/agents` as an UNNAMED redirect to `/?view=list`. So
 * the name resolved to nothing and `InfoTile`'s `<RouterLink :to>` threw during
 * render.
 *
 * The cost was wildly out of proportion to the typo, which is why this is
 * guarded rather than just fixed. A throw inside render aborts Vue's update for
 * the whole tree, so the DASHBOARD FROZE: the type-to-filter e2e failed with
 * the pill's input visibly holding "trinity" while the match-count never
 * appeared and no tile ever filtered — the DOM kept the typed characters and
 * Vue simply stopped re-rendering. Nothing in that symptom points at a link in
 * a tile, and the tile itself renders as an empty box rather than an error.
 *
 * Static on purpose: `vitest.config.js` pins `environment: 'node'` (pure
 * modules only, no DOM), so a mount test is not available here, and the e2e
 * that DID catch it is `ui`-label-gated — it does not run on most PRs. Reading
 * the source costs nothing and runs everywhere.
 *
 * This matters more as the catalog grows (epic #94: #95-#101, #259). Each new
 * tile is "one block plus the component" per catalog.js, and every one of them
 * can carry a link.
 */

const _dir = dirname(fileURLToPath(import.meta.url))
const SRC = resolve(_dir, '../../src')
const TILES = resolve(SRC, 'components/tiles')

/** Route names declared in the real router. */
function routeNames() {
  const src = readFileSync(resolve(SRC, 'router/index.js'), 'utf8')
  return new Set([...src.matchAll(/name:\s*['"]([A-Za-z][\w-]*)['"]/g)].map((m) => m[1]))
}

/** `{ name: 'X' }` targets bound to a `link-to` on any tile component. */
function tileLinkTargets() {
  const out = []
  for (const file of readdirSync(TILES).filter((f) => f.endsWith('.vue'))) {
    const src = readFileSync(resolve(TILES, file), 'utf8')
    for (const m of src.matchAll(/link-to\s*=\s*"\s*\{([^}]*)\}/g)) {
      const named = /name:\s*['"]([^'"]+)['"]/.exec(m[1])
      if (named) out.push({ file, name: named[1] })
    }
  }
  return out
}

describe('grid info-tile links (ent#325)', () => {
  it('the router exposes named routes to check against', () => {
    const names = routeNames()
    // Sanity: a guard that reads nothing certifies nothing.
    expect(names.size).toBeGreaterThan(5)
    expect(names.has('Dashboard')).toBe(true)
  })

  it('finds the link targets it is meant to police', () => {
    // Same reason: if the scan silently matched zero tiles it would pass
    // forever, including on the exact regression it exists to catch.
    expect(tileLinkTargets().length).toBeGreaterThan(0)
  })

  it('every tile link names an existing route', () => {
    const names = routeNames()
    const bad = tileLinkTargets().filter((t) => !names.has(t.name))
    expect(
      bad.map((b) => `${b.file} -> { name: '${b.name}' }`),
      'a RouterLink to an unknown route name THROWS during render, which aborts '
        + "Vue's update for the whole tree and freezes the dashboard — the tile "
        + 'renders as an empty box and gives no hint where the fault is',
    ).toEqual([])
  })

  it("'Agents' specifically is not a route — it folded into the Dashboard", () => {
    // Pins the trap itself. `/agents` still exists as an unnamed redirect to
    // `/?view=list`, so the path works and only the NAME is gone; that is what
    // made the original mistake so easy to make and so hard to spot.
    expect(routeNames().has('Agents')).toBe(false)
  })
})
