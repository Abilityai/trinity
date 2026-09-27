import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import {
  scanSourceTextSpecs,
  countSourceTextReads,
  pinReason,
} from '../../scripts/scan-source-text-specs.mjs'

/**
 * #2918 — source-text-assertion RATCHET for component specs.
 *
 * The class: safety-critical UI logic (a delete confirmation's `canRemove`, a
 * modal's Esc/focus-trap contract) lands in a `.vue` file whose only "test" is
 * a regex over the file's source. That proves the code was WRITTEN; an
 * inverted predicate passes it byte-identically (#2756, #2778 — both ejected
 * from the 2026-09-20 train with green CI). Every instance justified itself
 * with "this vitest is node-only, nothing can mount", which has been false
 * since `vitest.config.js` grew `plugins: [vue()]` + a per-file DOM opt-in
 * for exactly this. (The opt-in's spelling is deliberately NOT written in
 * this comment: vitest reads the marker from any leading comment, and would
 * run THIS spec under jsdom.)
 *
 * So, the same shape as the raw-colour and loading-gate ratchets:
 * `source-text-baseline.json` freezes today's per-spec counts of source-text
 * reads of component/view SFCs; this test fails when a spec GROWS, holds a
 * spec absent from the baseline to ZERO, and pins the baseline exact so a
 * paid-down spec must lower its entry. The one escape is explicit and
 * reviewable: a `@source-text-pin: <reason>` marker in the spec's docblock,
 * for the legitimate pins (an AST-shaped call-site guard, a spelling whose
 * cascade is proven elsewhere, a parity/allowlist table).
 *
 * The failure message names the mount harness at the point of decision —
 * that is the whole fix for #2918: the capability existed, it was invisible.
 *
 *   node scripts/scan-source-text-specs.mjs . --baseline source-text-baseline.json
 */

const here = dirname(fileURLToPath(import.meta.url))
const FRONTEND = resolve(here, '../..')
const BASELINE_PATH = resolve(FRONTEND, 'source-text-baseline.json')
const REGEN = `node scripts/scan-source-text-specs.mjs . --baseline source-text-baseline.json`
const HOW_TO_MOUNT =
  'The harness mounts components: put `// @vitest-' + 'environment jsdom` on line 1, ' +
  '`import { mount } from \'@vue/test-utils\'`, and assert on the rendered DOM / emitted ' +
  'events / store calls — copy tests/unit/portalThemeSwitch.spec.js. If this spec is a ' +
  'deliberate pin that a mount cannot prove (an AST-shaped call-site guard, a parity table), ' +
  'say so with `// @source-text-pin: <reason>` in its docblock.'

describe('scanner — what counts as a source-text read of an SFC', () => {
  it('counts a non-import .vue literal under components/ or views/ when the spec reads files', () => {
    const spec = `
import { readFileSync } from 'node:fs'
const SRC = readFileSync(new URL('../../src/components/SystemTeardownPanel.vue', import.meta.url), 'utf8')
const shell = read('../../src/views/Portal.vue')
expect(SRC).toContain('canRemove')
`
    const { count, samples } = countSourceTextReads(spec)
    expect(count).toBe(2)
    expect(samples.map(s => s.path)).toEqual([
      '../../src/components/SystemTeardownPanel.vue',
      '../../src/views/Portal.vue',
    ])
  })

  it('never counts a mount import, a comment, or a spec that reads no files', () => {
    const mounting = `
// @vitest-${'environment'} jsdom
import { mount } from '@vue/test-utils'
import Panel from '../../src/components/SystemTeardownPanel.vue'
// the old version read '../../src/components/SystemTeardownPanel.vue' as text
const w = mount(Panel)
`
    expect(countSourceTextReads(mounting).count).toBe(0)
    const noRead = `const p = '../../src/components/X.vue'; expect(p).toBeTruthy()`
    expect(countSourceTextReads(noRead).count).toBe(0)
    const utilOnly = `import { readFileSync } from 'node:fs'; const s = readFileSync('../../src/utils/x.js', 'utf8')`
    expect(countSourceTextReads(utilOnly).count).toBe(0)
  })

  it('reads the pin marker only when it carries a reason', () => {
    expect(pinReason('/**\n * @source-text-pin: every template call site must pass :agent — a mount proves one call, not the set\n */')).toMatch(/^every template call site/)
    expect(pinReason('// @source-text-pin: because')).toBeNull()   // too short to be a reason
    expect(pinReason('// nothing here')).toBeNull()
  })
})

describe('ratchet — per-spec source-text reads may only shrink', () => {
  const baseline = JSON.parse(readFileSync(BASELINE_PATH, 'utf8'))
  const scan = scanSourceTextSpecs(resolve(FRONTEND, 'tests/unit'))

  it('baseline file has the expected shape', () => {
    expect(baseline).toHaveProperty('files')
    expect(typeof baseline.files).toBe('object')
  })

  it('no spec has MORE source-text reads than its baseline entry', () => {
    const grew = []
    for (const [file, count] of Object.entries(scan.files)) {
      if (!(file in baseline.files)) continue          // new specs: next case
      if (count > baseline.files[file]) {
        const where = (scan.samples[file] || []).map(s => `:${s.line} ${s.path}`).join(', ')
        grew.push(`${file} — ${count} > ${baseline.files[file]} (${where})`)
      }
    }
    expect(
      grew,
      `Source-text reads of component/view SFCs GREW (#2918). ${HOW_TO_MOUNT}\n  ${grew.join('\n  ')}`
    ).toEqual([])
  })

  it('a spec with no baseline entry reads no SFC source — or says why with @source-text-pin', () => {
    const offenders = []
    for (const [file, count] of Object.entries(scan.files)) {
      if (file in baseline.files) continue
      if (scan.pinned[file]) continue
      const where = (scan.samples[file] || []).map(s => `:${s.line} ${s.path}`).join(', ')
      offenders.push(`${file} — ${count} read(s) (${where})`)
    }
    expect(
      offenders,
      `A NEW spec asserts over a component's SOURCE TEXT (#2918): that proves the code was written, not that it runs — an inverted predicate passes it byte-identically. ${HOW_TO_MOUNT}\n  ${offenders.join('\n  ')}`
    ).toEqual([])
  })

  it('baseline is exact — a paid-down spec must lower its entry (no stale ceilings)', () => {
    const stale = []
    for (const [file, allowed] of Object.entries(baseline.files)) {
      const actual = scan.files[file] ?? 0
      if (actual < allowed) stale.push(`${file}: baseline ${allowed}, now ${actual}`)
    }
    expect(
      stale,
      `Baseline entries are above reality (a spec now mounts — nice). Regenerate so the ratchet keeps biting:\n  ${REGEN}\n  ${stale.join('\n  ')}`
    ).toEqual([])
  })

  it('the two mounting precedents the failure message points at still exist and still mount', () => {
    for (const name of ['portalThemeSwitch.spec.js', 'portalComposerDraft.spec.js']) {
      const src = readFileSync(resolve(FRONTEND, 'tests/unit', name), 'utf8')
      expect(src, name).toMatch(new RegExp('^// @vitest-' + 'environment jsdom', 'm'))
      expect(src, name).toMatch(/from '@vue\/test-utils'/)
    }
  })
})
