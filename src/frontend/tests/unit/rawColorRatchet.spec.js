import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { scanRawColors, baselineKey } from '../../scripts/scan-raw-colors.mjs'

/**
 * #2605 — raw-colour RATCHET, actually enforced.
 *
 * `CLAUDE.md` and `docs/memory/design-system-contract.md` both state that raw
 * palette usage is ratcheted — per-file counts may only shrink, measured by
 * `scan-raw-colors.mjs` against `raw-color-baseline.json`. **Nothing ran it.**
 * No workflow invoked the scanner and no spec read the baseline, so the
 * guarantee had been false since it was written, and the tree had drifted: five
 * files grew `raw_nongray`, twenty-four grew `raw_gray`, and ten files created
 * after the freeze carried ~110 `raw_nongray` between them with no entry at all.
 * `views/Portal.vue`'s six were the `amber-*` classes from #2261 — precisely
 * what the ratchet exists to catch.
 *
 * Deliberately modelled on `loadingGateRatchet.spec.js`: it rides
 * `npm run test:unit`, which the frontend job already runs, so the guard needs
 * no new CI wiring to bite.
 *
 * THREE properties, and the third is the one prose could not enforce:
 *
 *   1. no file GROWS past its baseline entry;
 *   2. the baseline is EXACT — a file that improved must lower its entry, or a
 *      stale ceiling silently re-permits regressions back up to it;
 *   3. a file with NO entry is held to ZERO `raw_nongray`. The contract's "new
 *      code starts at zero" rule was prose only, and an absent entry otherwise
 *      reads as "unmeasured" rather than "must be clean" — which is how ten
 *      post-freeze files accumulated raw palette classes unchallenged.
 *
 *   node scripts/scan-raw-colors.mjs . --baseline raw-color-baseline.json
 */

const here = dirname(fileURLToPath(import.meta.url))
const FRONTEND = resolve(here, '../..')
const BASELINE_PATH = resolve(FRONTEND, 'raw-color-baseline.json')
const REGEN = 'node scripts/scan-raw-colors.mjs . --baseline raw-color-baseline.json'

// `raw_gray` is ratcheted but NOT held to zero for new files. Gray is the
// neutral ink ladder; the contract's hard rule is about non-gray palette
// classes, which are the ones that break theming and carry meaning a semantic
// token should own. Holding new files to zero gray would fail every honest new
// component on day one and the guard would be deleted within a week.
const RATCHETED = ['raw_nongray', 'raw_gray', 'hardcoded_colors']

const baseline = JSON.parse(readFileSync(BASELINE_PATH, 'utf8'))
const scan = scanRawColors(FRONTEND)

/** Scanned counts keyed the way the baseline keys them. */
const actual = Object.fromEntries(
  Object.entries(scan.files).map(([rel, c]) => [baselineKey(rel), c])
)

describe('the scanner is callable from a test at all (#2605)', () => {
  it('exports a scan function, so the ratchet can be enforced rather than described', () => {
    expect(typeof scanRawColors).toBe('function')
    expect(Object.keys(scan.files).length).toBeGreaterThan(0)
  })

  it('importing it does not run the CLI', () => {
    // The module parsed `process.argv` at top level and `process.exit(2)`d
    // without a path argument — importing it would have killed the test run.
    expect(scan.totals).toHaveProperty('raw_nongray')
  })

  it('agrees with the committed baseline about how files are keyed', () => {
    const [first] = Object.keys(baseline.files)
    expect(first.startsWith('src/frontend/src/')).toBe(true)
    expect(Object.keys(actual).some(k => k in baseline.files)).toBe(true)
  })
})

describe('ratchet — per-file raw-colour counts may only shrink (#2605)', () => {
  it('baseline file has the expected shape', () => {
    expect(baseline).toHaveProperty('files')
    expect(typeof baseline.files).toBe('object')
  })

  it('no file has MORE raw colours than its baseline entry', () => {
    const grew = []
    for (const [file, counts] of Object.entries(actual)) {
      const allowed = baseline.files[file]
      if (!allowed) continue          // covered by the new-file rule below
      for (const key of RATCHETED) {
        if ((counts[key] ?? 0) > (allowed[key] ?? 0)) {
          grew.push(`${file} — ${key} ${allowed[key] ?? 0} → ${counts[key]}`)
        }
      }
    }
    expect(
      grew,
      'Raw colour usage GREW. Use semantic tokens (design-system-contract.md); ' +
      `if an increase is deliberate, re-freeze in its OWN commit:\n  ${REGEN}\n  ${grew.join('\n  ')}`
    ).toEqual([])
  })

  it('a file with no baseline entry carries zero raw_nongray ("new code starts at zero")', () => {
    const offenders = []
    for (const [file, counts] of Object.entries(actual)) {
      if (baseline.files[file]) continue
      if ((counts.raw_nongray ?? 0) > 0) {
        const where = (scan.samples[file.replace('src/frontend/src/', '')] || [])
          .filter(s => s.kind === 'raw').slice(0, 3)
          .map(s => `:${s.line} ${s.text}`).join(', ')
        offenders.push(`${file} — ${counts.raw_nongray} raw non-gray (${where})`)
      }
    }
    expect(
      offenders,
      'New files must use semantic tokens, not raw palette classes — the ' +
      `contract's "new code starts at zero" rule:\n  ${offenders.join('\n  ')}`
    ).toEqual([])
  })

  it('baseline is exact — an improved file must lower its entry (no stale ceilings)', () => {
    const stale = []
    for (const [file, allowed] of Object.entries(baseline.files)) {
      const counts = actual[file] ?? { raw_nongray: 0, raw_gray: 0, hardcoded_colors: 0 }
      for (const key of RATCHETED) {
        if ((counts[key] ?? 0) < (allowed[key] ?? 0)) {
          stale.push(`${file} — ${key} baseline ${allowed[key] ?? 0}, now ${counts[key] ?? 0}`)
        }
      }
    }
    expect(
      stale,
      `Baseline entries are above reality (colours got paid down — nice). ` +
      `Regenerate so the ratchet keeps biting:\n  ${REGEN}\n  ${stale.join('\n  ')}`
    ).toEqual([])
  })
})

describe('the docs describe what is actually enforced (#2605)', () => {
  const REPO = resolve(FRONTEND, '../..')
  const read = (rel) => readFileSync(resolve(REPO, rel), 'utf8')

  it('CLAUDE.md and the contract name the guard, not just the scanner', () => {
    // The issue's root cause: both documents said "ratcheted" and a contributor
    // reasonably read that as machine-checked. Naming the spec is what makes
    // the claim checkable by the person reading it.
    for (const doc of ['CLAUDE.md', 'docs/memory/design-system-contract.md']) {
      expect(read(doc), doc).toContain('rawColorRatchet.spec.js')
    }
  })
})
