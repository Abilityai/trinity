/**
 * #2199 — the Grid layout storage key must have exactly ONE source of truth.
 *
 * #2042 (ent#325) bumped the layout key v1 -> v2 in `stores/fleetGrid.js`, but
 * `e2e/dashboard-grid-view.spec.js` and `e2e/grid-org-overlay.spec.js` each
 * carried their own hand-copied `'trinity-grid-layout-v1'`. Nothing failed
 * loudly: the read-backs simply resolved `null` and the `removeItem` cleanup
 * silently stopped clearing anything. Two e2e tests went red for a reason that
 * looked like a product bug, and one cleanup went quietly dead.
 *
 * The fix is `src/utils/gridStorageKeys.js`, imported by the store and by the
 * specs. This guard is the ratchet on top of it: the module fixes today's
 * desync, but it cannot stop a NEW spec from hand-copying the literal again.
 *
 * It earns its place because it is the only automatically-enforced part of
 * that change — `.github/workflows/frontend-build.yml` runs `npm run test:unit`
 * on every PR touching `src/frontend/**`, whereas the e2e suite is advisory,
 * smoke-only in CI (#1526), and none of the affected tests are @smoke.
 *
 * Source-structure guard, the same shape as `agentDetailDeepLink.spec.js` and
 * the Python AST guards. Deliberately scoped to the LAYOUT key family: the
 * other hand-copied storage keys in e2e/ currently match their source and a
 * blanket rule would force an unrelated refactor.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'fs'
import { fileURLToPath } from 'url'
import { join } from 'path'

const E2E_DIR = fileURLToPath(new URL('../../e2e', import.meta.url))
const STORE = fileURLToPath(new URL('../../src/stores/fleetGrid.js', import.meta.url))
const MODULE = fileURLToPath(new URL('../../src/utils/gridStorageKeys.js', import.meta.url))

/** The one file allowed to hold the literals. */
const SOURCE_OF_TRUTH = 'gridStorageKeys.js'

/** Any generation of the layout key, as a quoted string literal. */
const LAYOUT_LITERAL = /['"`]trinity-grid-layout-[^'"`]*['"`]/

/** Strip comments so prose naming the key (like this file's own docstring) is not scanned. */
function stripComments(code) {
  return code.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^[ \t]*\/\/.*$/gm, '')
}

function jsFilesUnder(dir) {
  const out = []
  for (const entry of readdirSync(dir)) {
    if (entry === 'node_modules' || entry === '.auth' || entry === 'test-results') continue
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) out.push(...jsFilesUnder(full))
    else if (entry.endsWith('.js')) out.push(full)
  }
  return out
}

describe('#2199 grid layout storage key has one source of truth', () => {
  it('exports every generation, and ALL_LAYOUT_KEYS covers them', async () => {
    const mod = await import('../../src/utils/gridStorageKeys.js')
    expect(mod.LAYOUT_KEY).toBe('trinity-grid-layout-v2')
    expect(mod.LAYOUT_KEY_V1).toBe('trinity-grid-layout-v1')
    // Load-bearing: `_loadSavedRaw` migrates v1 -> v2, so a cleanup that misses
    // a generation lets a stale blob be migrated straight back in.
    expect(mod.ALL_LAYOUT_KEYS).toContain(mod.LAYOUT_KEY)
    expect(mod.ALL_LAYOUT_KEYS).toContain(mod.LAYOUT_KEY_V1)
  })

  it('the constants module imports nothing (Playwright must resolve it)', () => {
    // Playwright reads neither the Vite `@` alias nor a tsconfig `paths` map,
    // so one aliased import here would break every grid spec at once.
    const source = stripComments(readFileSync(MODULE, 'utf8'))
    expect(source).not.toMatch(/^\s*import\s/m)
  })

  it('no e2e spec hand-copies a layout key literal', () => {
    const offenders = jsFilesUnder(E2E_DIR)
      .filter((f) => LAYOUT_LITERAL.test(stripComments(readFileSync(f, 'utf8'))))
      .map((f) => f.slice(E2E_DIR.length + 1))

    expect(
      offenders,
      `import { LAYOUT_KEY, ALL_LAYOUT_KEYS } from '../src/utils/gridStorageKeys.js' ` +
        `instead of hand-copying the literal (#2199)`
    ).toEqual([])
  })

  it('fleetGrid.js imports the keys rather than declaring them', () => {
    const source = stripComments(readFileSync(STORE, 'utf8'))
    expect(source).not.toMatch(LAYOUT_LITERAL)
    expect(source).toMatch(/from\s+['"]@\/utils\/gridStorageKeys['"]/)
  })

  it('the source-of-truth module is the only place holding the literals', () => {
    expect(MODULE.endsWith(SOURCE_OF_TRUTH)).toBe(true)
    expect(readFileSync(MODULE, 'utf8')).toMatch(LAYOUT_LITERAL)
  })
})
