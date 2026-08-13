/**
 * Every `--gv-*` a grid tile consumes must be DEFINED in the cascade, in BOTH
 * themes (ent#325 review).
 *
 * InfoTile's style block opens by asserting the invariant — "tokens come from
 * the --gv-* cascade FleetGrid establishes, so an info tile and an agent tile
 * can never drift apart on colour" — and nothing enforced it. 10 of the 12
 * tokens the two tile components consumed were defined nowhere, so every
 * fallback was permanently live and the tile could not respond to the theme at
 * all. It shipped past a 28-test suite because a comment is not a test.
 *
 * The two tokens that DID resolve are what made it a contrast failure rather
 * than a cosmetic one: `--gv-muted` correctly flips to #9ca3af in dark, and
 * landed on a background hardcoded to #fff — gray-400 on white is ~2.4:1,
 * under the 4.5:1 WCAG AA floor, on a white card sitting among dark AgentTiles.
 *
 * This is a static check by design. The unit suite is node-environment ("pure
 * modules only, no DOM"), and a jsdom test could not catch this anyway: jsdom
 * does not do cascade resolution, so `getComputedStyle` returns the fallback
 * and the broken state looks identical to the fixed one. Parsing the source is
 * what actually distinguishes them.
 *
 * It pays off repeatedly rather than once: ent#94 adds tiles one sub-issue at a
 * time (#95–#101, #259), and each new tile is a fresh chance to consume a token
 * nobody defined — failing silently in exactly one theme.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, dirname, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'src')

/**
 * Components that render inside the grid lattice and inherit its cascade.
 *
 * AUTO-DISCOVERED (ent#100), not hand-listed. It was a two-entry constant, so a
 * new tile was unguarded until someone remembered to add it — and epic #94
 * queues eight of them, each a fresh chance to consume a token nobody defined
 * and fail silently in exactly one theme. A guard whose scope is a hand-written
 * file list validates the fix, not the codebase. The sibling
 * `gridTileLinks.spec.js` already auto-discovers; this matches it, and walks
 * recursively so presentational sub-components under `tiles/parts/` — which
 * carry real tile chrome — are covered too.
 */
function tileComponents() {
  const dir = join(SRC, 'components/tiles')
  const walk = (d) =>
    readdirSync(d).flatMap((n) => {
      const full = join(d, n)
      return statSync(full).isDirectory() ? walk(full) : [full]
    })
  return [
    'components/InfoTile.vue',
    ...walk(dir).filter((f) => f.endsWith('.vue')).map((f) => relative(SRC, f)),
  ].sort()
}

const TILE_COMPONENTS = tileComponents()

/** The two blocks in FleetGrid.vue that establish the cascade. */
const LIGHT_SELECTOR = '.fleet-canvas {'
const DARK_SELECTOR = ':root.dark .fleet-canvas {'

function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const full = join(dir, name)
    return statSync(full).isDirectory() ? walk(full) : [full]
  })
}

/** `--gv-x` names declared anywhere under src/ (any .vue file). */
function definedTokens() {
  const found = new Set()
  for (const file of walk(SRC).filter((f) => f.endsWith('.vue'))) {
    for (const m of readFileSync(file, 'utf8').matchAll(/^\s*(--gv-[a-z0-9-]+)\s*:/gm)) {
      found.add(m[1])
    }
  }
  return found
}

/** `--gv-x` names read via var() in one file. */
function consumedTokens(relPath) {
  const text = readFileSync(join(SRC, relPath), 'utf8')
  return [...text.matchAll(/var\((--gv-[a-z0-9-]+)/g)].map((m) => m[1])
}

/** Token names declared inside one `{ … }` block of FleetGrid.vue. */
function tokensInBlock(selector) {
  const text = readFileSync(join(SRC, 'components/FleetGrid.vue'), 'utf8')
  const start = text.indexOf(`\n${selector}`)
  expect(start, `cascade block "${selector}" not found in FleetGrid.vue`).toBeGreaterThan(-1)
  const end = text.indexOf('\n}', start)
  return new Set(
    [...text.slice(start, end).matchAll(/^\s*(--gv-[a-z0-9-]+)\s*:/gm)].map((m) => m[1]),
  )
}

describe('grid tile design tokens (ent#325)', () => {
  it('discovered the tile components it is meant to police', () => {
    // A guard that reads nothing certifies nothing: if the walk broke, the
    // `it.each` below would iterate an empty list and the whole suite would
    // pass without checking a single file.
    expect(TILE_COMPONENTS.length).toBeGreaterThanOrEqual(3)
    expect(TILE_COMPONENTS).toContain('components/InfoTile.vue')
    expect(TILE_COMPONENTS).toContain('components/tiles/parts/TileRowList.vue')
  })

  it.each(TILE_COMPONENTS)('%s consumes only tokens that exist', (relPath) => {
    const defined = definedTokens()
    const undefinedTokens = [...new Set(consumedTokens(relPath))]
      .filter((t) => !defined.has(t))
      .sort()

    expect(
      undefinedTokens,
      `${relPath} reads ${undefinedTokens.length} token(s) defined nowhere in src/, so their ` +
        'var() fallbacks are permanently live and the component cannot respond to the theme',
    ).toEqual([])
  })

  it('defines every tile token in BOTH the light and dark cascade blocks', () => {
    // A token present in one block only is the same class of bug: it resolves
    // in one theme and silently falls back in the other, which is precisely how
    // a white card ended up on the dark canvas.
    const light = tokensInBlock(LIGHT_SELECTOR)
    const dark = tokensInBlock(DARK_SELECTOR)

    const consumed = new Set(TILE_COMPONENTS.flatMap(consumedTokens))
    const oneThemeOnly = [...consumed]
      .filter((t) => light.has(t) !== dark.has(t))
      .map((t) => `${t} (${light.has(t) ? 'light only' : 'dark only'})`)
      .sort()

    expect(
      oneThemeOnly,
      'these tokens are defined in one theme block only — they resolve in that ' +
        'theme and fall back in the other',
    ).toEqual([])
  })

  it('the light and dark blocks declare the same token set', () => {
    // Broader than the tiles: keeps the two blocks in step as a whole, so a
    // token added for a future tile cannot land in one block by itself.
    const light = tokensInBlock(LIGHT_SELECTOR)
    const dark = tokensInBlock(DARK_SELECTOR)
    expect([...light].filter((t) => !dark.has(t)).sort()).toEqual([])
    expect([...dark].filter((t) => !light.has(t)).sort()).toEqual([])
  })

  it('no tile hardcodes a raw colour where a token exists', () => {
    // The original failure was not only missing tokens — the fallbacks were raw
    // literals (#fff, #111827) chosen to look right in light mode, which is what
    // made the broken state invisible to anyone reading the file in light mode.
    // A bare colour OUTSIDE a var() fallback cannot be themed at all.
    const offenders = []
    for (const relPath of TILE_COMPONENTS) {
      const text = readFileSync(join(SRC, relPath), 'utf8')
      const style = text.slice(text.indexOf('<style'))
      for (const line of style.split('\n')) {
        if (!/^\s*(background|color|border|border-color|box-shadow)\b/.test(line)) continue
        const withoutVars = line.replace(/var\([^;]*\)/g, '')
        if (/#[0-9a-fA-F]{3,8}\b|\brgba?\(/.test(withoutVars)) {
          offenders.push(`${relPath}: ${line.trim()}`)
        }
      }
    }
    expect(
      offenders,
      'raw colour outside a var() fallback — it cannot flip with the theme',
    ).toEqual([])
  })
})
