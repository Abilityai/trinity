/**
 * #2536 — structural guards for the Dashboard view-mode switcher.
 *
 * Two invariants that no behavioural test enforces in CI:
 *
 *  1. The mode list has exactly ONE home, `src/utils/viewModes.js`. Before
 *     #2536 it lived twice in different orders — `['grid','timeline','list']`
 *     in the store (whitelist only) and `['timeline','grid','list']` in the
 *     template — so a cycle built on the store copy would have run against
 *     the buttons. The module fixes today's desync; this guard stops the NEXT
 *     hand-copy (the #2199 `gridStorageKeys.spec.js` lesson, same shape).
 *
 *  2. The `v` hotkey and the `/` hotkey share ONE document keydown listener,
 *     so guards 2–5 (chords, IME, editable targets, modals) are one code path.
 *     A second listener would fork the ladder — and would also need its own
 *     #2200 mount-slot proof.
 *
 * This is the REQUIRED CI gate for the change: `.github/workflows/
 * frontend-build.yml` runs `npm run test:unit` on every PR touching
 * `src/frontend/**`, whereas the e2e suite is advisory, smoke-only in CI
 * (#1526), and `dashboard-stats-overflow.spec.js` is `@interactive` (never
 * runs in CI). Source-structure guard: asserts counts and structure, never
 * exact lines (learnings: brittle string pins).
 */
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'fs'
import { fileURLToPath } from 'url'
import { join, relative } from 'path'

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url))
const SRC_DIR = join(FRONTEND_ROOT, 'src')
const E2E_DIR = join(FRONTEND_ROOT, 'e2e')
const MODULE = join(SRC_DIR, 'utils/viewModes.js')
const STORE = join(SRC_DIR, 'stores/network.js')
const VIEW = join(SRC_DIR, 'views/Dashboard.vue')

/** The one file allowed to hold the mode-list literal. */
const SOURCE_OF_TRUTH = 'src/utils/viewModes.js'

/**
 * Any 3-element array literal of the three mode names, in ANY order — the
 * store copy was ordered differently from the template copy, so an order-
 * specific regex would have missed exactly the desync this guards against.
 */
const MODE_LIST_LITERAL =
  /\[\s*(['"](?:timeline|grid|list)['"]\s*,\s*){2}['"](?:timeline|grid|list)['"]\s*,?\s*\]/

/**
 * Strip `/* *\/`, whole-line `//` AND `<!-- -->` comments so prose naming the
 * list (this file's own docstring, the module's header, the switcher's
 * template comment in Dashboard.vue) is never scanned.
 */
function stripComments(code) {
  return code
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^[ \t]*\/\/.*$/gm, '')
}

const SKIP_DIRS = new Set(['node_modules', '.auth', 'test-results', 'playwright-report'])

function sourceFilesUnder(dir) {
  const out = []
  for (const entry of readdirSync(dir)) {
    if (SKIP_DIRS.has(entry)) continue
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) out.push(...sourceFilesUnder(full))
    else if (entry.endsWith('.js') || entry.endsWith('.vue')) out.push(full)
  }
  return out
}

/** `src/` + `e2e/` only — `tests/unit/` legitimately holds the literal as an expectation. */
const SCANNED = [...sourceFilesUnder(SRC_DIR), ...sourceFilesUnder(E2E_DIR)]

describe('#2536 view-mode list has one home and the hotkeys share one listener', () => {
  it('finds source files to scan (the guard is not vacuously green)', () => {
    expect(SCANNED.length).toBeGreaterThan(100)
    expect(SCANNED).toContain(MODULE)
    expect(SCANNED).toContain(STORE)
    expect(SCANNED).toContain(VIEW)
  })

  it('the constants module imports nothing (Playwright must resolve it)', () => {
    // Playwright reads neither the Vite `@` alias nor a tsconfig `paths` map,
    // so one aliased import here would break every consuming spec at once.
    const source = stripComments(readFileSync(MODULE, 'utf8'))
    expect(source).not.toMatch(/^\s*import\s/m)
  })

  it('the mode-list literal appears in exactly one file under src/ and e2e/ — the module', () => {
    const holders = SCANNED.filter((f) => MODE_LIST_LITERAL.test(stripComments(readFileSync(f, 'utf8')))).map(
      (f) => relative(FRONTEND_ROOT, f)
    )
    expect(
      holders,
      `import { VIEW_MODES } from '@/utils/viewModes' (or '../src/utils/viewModes.js' ` +
        `from an e2e spec) instead of hand-copying the list (#2536)`
    ).toEqual([SOURCE_OF_TRUTH])
  })

  it('the store and the view import the list rather than declaring it', () => {
    for (const file of [STORE, VIEW]) {
      const source = stripComments(readFileSync(file, 'utf8'))
      expect(source, relative(FRONTEND_ROOT, file)).toMatch(/from\s+['"]@\/utils\/viewModes['"]/)
      expect(source, relative(FRONTEND_ROOT, file)).not.toMatch(MODE_LIST_LITERAL)
    }
    // The template iterates the import — a `<script setup>` binding is usable directly.
    expect(stripComments(readFileSync(VIEW, 'utf8'))).toMatch(/v-for="mode in VIEW_MODES"/)
  })

  it('Dashboard.vue registers exactly ONE document keydown listener (the hotkeys share one guard ladder)', () => {
    const source = stripComments(readFileSync(VIEW, 'utf8'))
    const registrations = source.match(/document\.addEventListener\(\s*['"]keydown['"]/g) || []
    expect(
      registrations.length,
      'Add a key to the dispatch inside handleDashboardKeydown — never a second ' +
        'document keydown listener. Guards 2–5 (chords, IME, editable targets, ' +
        'modals) must stay one code path, and a second listener would also need ' +
        'its own #2200 mount-slot proof (#2536).'
    ).toBe(1)
  })

  it('the source-of-truth module actually holds the literal (guards a vacuous one-home test)', () => {
    expect(relative(FRONTEND_ROOT, MODULE)).toBe(SOURCE_OF_TRUTH)
    // On COMMENT-STRIPPED source: the header's prose mentions both old copies,
    // and a match there would let the real export drift to a different shape.
    expect(stripComments(readFileSync(MODULE, 'utf8'))).toMatch(MODE_LIST_LITERAL)
  })
})
