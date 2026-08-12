import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { resolve, dirname, relative } from 'node:path'
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
 * This matters more as the catalog grows (epic #94: ent#95-ent#101, ent#259).
 * Each new tile is "one block plus the component" per catalog.js, and every one
 * of them can carry a link.
 *
 * ## ent#100 widened it, because it was blind to the links that matter most
 *
 * The original scan matched ONE shape: a literal `link-to="{…}"` ATTRIBUTE, in
 * a non-recursive `readdirSync` of `components/tiles`. Measured against the
 * first tile to carry per-row links, it saw the static footer link and NOTHING
 * else — not `:link-to="someComputed"`, not the per-row
 * `to: { name: 'ExecutionDetail', … }` objects built in `<script setup>`, and
 * not `components/tiles/parts/` at all.
 *
 * That is the coverage gap pointing directly at the guard's own failure mode: a
 * tile's footer link is written once and eyeballed once, whereas a row link is
 * built per row, from data, on the surface that is hardest to exercise. So the
 * scan now walks `components/tiles/**` recursively and reads any route-target
 * object literal — `link-to=`, `:link-to=`, `:to=` or a bare `to:` key —
 * brace-balanced, with nested objects (`params: { … }`) stripped before the
 * route NAME is read, so an inner `name:` cannot be mistaken for a route.
 *
 * Computed targets (`:link-to="footerLink"`) remain invisible; a static reader
 * cannot resolve them. The mitigation is convention, not regex: build route
 * targets as literals where the guard can see them.
 */

const _dir = dirname(fileURLToPath(import.meta.url))
const SRC = resolve(_dir, '../../src')
const TILES = resolve(SRC, 'components/tiles')

/** Route names declared in the real router. */
function routeNames() {
  const src = readFileSync(resolve(SRC, 'router/index.js'), 'utf8')
  return new Set([...src.matchAll(/name:\s*['"]([A-Za-z][\w-]*)['"]/g)].map((m) => m[1]))
}

/** Every .vue under components/tiles, recursively (parts/ included). */
function tileFiles() {
  const walk = (dir) =>
    readdirSync(dir).flatMap((n) => {
      const full = resolve(dir, n)
      return statSync(full).isDirectory() ? walk(full) : [full]
    })
  return walk(TILES).filter((f) => f.endsWith('.vue'))
}

/** Body of the `{ … }` starting at `open`, brace-balanced. */
function balancedObject(src, open) {
  let depth = 0
  for (let i = open; i < src.length; i++) {
    if (src[i] === '{') depth++
    else if (src[i] === '}' && --depth === 0) return src.slice(open + 1, i)
  }
  return null
}

/** Drop nested `{ … }` groups so only TOP-LEVEL keys remain. */
function topLevelOnly(body) {
  let out = ''
  let depth = 0
  for (const ch of body) {
    if (ch === '{') depth++
    else if (ch === '}') depth--
    else if (depth === 0) out += ch
  }
  return out
}

/** Route-target object literals anywhere in a tile: `{ name: 'X', … }`. */
function tileLinkTargets() {
  const out = []
  // `link-to="{`, `:link-to="{`, `:to="{`, or a bare `to: {` object key.
  const SITE = /(?::?link-to\s*=\s*"\s*|:to\s*=\s*"\s*|\bto:\s*)\{/g
  for (const full of tileFiles()) {
    const file = relative(TILES, full)
    const src = readFileSync(full, 'utf8')
    for (const m of src.matchAll(SITE)) {
      const body = balancedObject(src, m.index + m[0].length - 1)
      if (body === null) continue
      const named = /name:\s*['"]([^'"]+)['"]/.exec(topLevelOnly(body))
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

  it('sees PER-ROW targets built in <script setup>, not just link-to attributes', () => {
    // The ent#100 gap, pinned. Before the widen, this scan matched only a
    // literal `link-to="{…}"` attribute, so every row link in a list tile was
    // invisible — on the guard whose failure mode is a FROZEN DASHBOARD.
    // Asserting a specific per-row route name keeps the widened scan honest:
    // narrowing it back to attributes-only fails here rather than passing green.
    const names = new Set(tileLinkTargets().map((t) => t.name))
    expect(
      names.has('ExecutionDetail'),
      "the scan no longer reads `to: { name: … }` objects built in <script setup>",
    ).toBe(true)
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
