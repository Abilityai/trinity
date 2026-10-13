#!/usr/bin/env node
/**
 * Source-text-assertion RATCHET for component specs (#2918).
 *
 * The class: a spec whose subject is a `.vue` file under `src/components/` or
 * `src/views/` and whose assertions are regex/`indexOf` slices over that file's
 * SOURCE (`readFileSync(...Panel.vue)` + `toContain`/`toMatch`). Such a spec
 * proves the code was WRITTEN, not that it RUNS: #2756's `canRemove` with `||`
 * for `&&` and an inverted `toggle()` both passed 36 of them; #2778's
 * `BaseModal.vue` had no executing test at all. Every time, the stated reason
 * was "vitest here is node-only, nothing can mount" — false since
 * `vitest.config.js` grew `plugins: [vue()]` and jsdom for exactly this
 * (precedent: `tests/unit/portalThemeSwitch.spec.js`, `portalComposerDraft.spec.js`).
 *
 * `source-text-baseline.json` freezes today's per-spec counts; the vitest guard
 * (`tests/unit/sourceTextRatchet.spec.js`) fails when any spec GROWS, and holds
 * a spec absent from the baseline to ZERO — unless it declares why a mount
 * cannot prove what it pins, with a `@source-text-pin` marker in its docblock:
 *
 *     // @source-text-pin: AST-shaped call-site guard — every `foo(` in the
 *     //   template must carry `bar`; a mount proves one call, not the set.
 *
 * Legitimate pins exist (a spelling guard whose cascade is proven elsewhere, a
 * route/allowlist table, a parity check) — the marker makes the choice visible
 * to review instead of silent. Counts may only shrink; freeze, then pay down.
 *
 * What counts (deliberately narrow, so the heuristic has no false positives on
 * the mounting specs):
 *   • the spec mentions `readFileSync` / `readFile(` (it reads source at all), AND
 *   • a NON-import line carries a string literal ending in `.vue` whose path
 *     includes `components/` or `views/`. `import X from '@/components/X.vue'`
 *     is a MOUNT import and is never counted.
 *
 * Usage:
 *   node scan-source-text-specs.mjs <path-to-frontend-or-tests/unit> [--baseline out.json] [--json]
 */
import { readdirSync, readFileSync, statSync, writeFileSync, existsSync } from 'node:fs'
import { join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { execSync } from 'node:child_process'

const READS_SOURCE = /\breadFileSync\s*\(|\breadFile\s*\(/
const VUE_LITERAL = /['"`]([^'"`\n]*?(?:components|views)\/[^'"`\n]*?\.vue)['"`]/g
const IMPORT_LINE = /^\s*(?:import\b|export\b.*\bfrom\b|const\s+\w+\s*=\s*(?:await\s+)?import\()/
const PIN_MARKER = /@source-text-pin\s*:?\s*(\S[^\n]{9,})/

/** Lines of `source` that name a component/view SFC by literal path outside an import. */
export function countSourceTextReads(source) {
  if (!READS_SOURCE.test(source)) return { count: 0, samples: [] }
  const samples = []
  const lines = source.split('\n')
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    if (IMPORT_LINE.test(line)) continue
    if (/^\s*(\/\/|\*)/.test(line)) continue   // a comment naming the file is not a read
    VUE_LITERAL.lastIndex = 0
    let m
    while ((m = VUE_LITERAL.exec(line)) !== null) samples.push({ line: i + 1, path: m[1] })
  }
  return { count: samples.length, samples }
}

/** The explicit, reviewable opt-out: a `@source-text-pin: <reason>` marker with a real reason. */
export function pinReason(source) {
  const m = PIN_MARKER.exec(source)
  return m ? m[1].trim() : null
}

function walk(dir) {
  const out = []
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) out.push(...walk(p))
    else if (name.endsWith('.spec.js')) out.push(p)
  }
  return out
}

/**
 * Keys use the same repo-relative prefix as the other two baselines so the
 * three ratchets read alike.
 */
export function scanSourceTextSpecs(specDir) {
  const root = resolve(specDir)
  const files = {}
  const samples = {}
  const pinned = {}
  let total = 0
  for (const file of walk(root).sort()) {
    const source = readFileSync(file, 'utf8')
    const { count, samples: s } = countSourceTextReads(source)
    if (count === 0) continue
    const key = 'src/frontend/tests/unit/' + relative(root, file).split('\\').join('/')
    files[key] = count
    samples[key] = s
    const reason = pinReason(source)
    if (reason) pinned[key] = reason
    total += count
  }
  return { files, samples, pinned, total }
}

// ---------------------------------------------------------------- CLI

const isMain = process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)
if (isMain) {
  const args = process.argv.slice(2)
  const baselineIdx = args.indexOf('--baseline')
  const baselineOut = baselineIdx >= 0 ? args[baselineIdx + 1] : null
  const printJson = args.includes('--json')
  const rootArg = args.find(a => !a.startsWith('--') && a !== baselineOut)
  if (!rootArg) {
    console.error('usage: node scan-source-text-specs.mjs <path-to-frontend-or-tests/unit> [--baseline out.json] [--json]')
    process.exit(2)
  }
  let scanRoot = resolve(rootArg)
  if (existsSync(join(scanRoot, 'tests', 'unit'))) scanRoot = join(scanRoot, 'tests', 'unit')
  const result = scanSourceTextSpecs(scanRoot)

  if (baselineOut) {
    let commit = null
    try { commit = execSync('git rev-parse HEAD', { cwd: scanRoot }).toString().trim() } catch { /* not a repo */ }
    const baseline = {
      generated: new Date().toISOString().slice(0, 10),
      commit,
      rule: 'per-spec counts of source-text reads of component/view SFCs may only shrink; a spec absent here is held to zero unless it carries a @source-text-pin marker; see scripts/scan-source-text-specs.mjs (#2918)',
      files: result.files,
      totals: { source_text_reads: result.total, specs_reading_source: Object.keys(result.files).length },
    }
    writeFileSync(baselineOut, JSON.stringify(baseline, null, 2) + '\n')
    console.error(`baseline written: ${baselineOut} (${Object.keys(result.files).length} specs, ${result.total} reads)`)
  }
  if (printJson) console.log(JSON.stringify(result, null, 2))
  else {
    console.log(JSON.stringify({ total: result.total, specs_reading_source: Object.keys(result.files).length, pinned: Object.keys(result.pinned).length }, null, 2))
    for (const [k, n] of Object.entries(result.files).sort((a, b) => b[1] - a[1])) console.log(`${String(n).padStart(3)}  ${k}${result.pinned[k] ? '  [pinned]' : ''}`)
  }
}
