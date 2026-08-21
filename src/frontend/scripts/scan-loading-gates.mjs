#!/usr/bin/env node
/**
 * scan-loading-gates.mjs — design-system p13/p14 "bare loading gate" scanner / ratchet seed (#1927).
 *
 * A BARE LOADING GATE is a `v-if` / `v-else-if` whose ENTIRE expression is a
 * single loading-flag path — `loading`, `loading.queue`, `executionsLoading`,
 * `store.loading`, `isLoading` — with no other operand. Such a branch is gated on
 * "a fetch is in flight", so once the surface HAS data, every background poll
 * swaps the rendered content for the loading branch (the strobe #1634 / #1926 /
 * #1927 each fixed on one surface). The sanctioned shape gates on "no data yet":
 * `loading && !hasLoaded`, `loading && items.length === 0`, or the
 * `utils/loadingState.js::viewState` reducer.
 *
 * This is a measurement tool plus the seed for a RATCHET: the committed
 * `loading-gate-baseline.json` records today's per-file counts; the vitest guard
 * `tests/unit/loadingGateRatchet.spec.js` fails when any file's count GROWS (or a
 * file absent from the baseline gains one). Counts may only shrink — freeze,
 * don't sweep: the ~40 pre-existing gates are frozen where they are, and each
 * sweep PR lowers the baseline as it goes.
 *
 * Known limitations (accepted, best-effort): regex scanner over <template> only;
 * `v-show="loading"` is not counted (it hides rather than unmounts — still a p13
 * smell, tracked separately); flags not containing "loading" (`fetching`, `busy`,
 * `pending`) are invisible; a gate split across template literals is invisible.
 *
 * Usage:
 *   node scan-loading-gates.mjs <path-to-frontend-or-src> [--baseline out.json] [--json]
 *
 * Exit code is always 0 — the ratchet decision lives in the vitest guard.
 */

import { readFileSync, readdirSync, statSync, writeFileSync, existsSync } from 'node:fs'
import { join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { execSync } from 'node:child_process'

// ---------------------------------------------------------------- core

const GATE_RE = /\bv-(?:if|else-if)\s*=\s*(?:"([^"]*)"|'([^']*)')/g
// A bare member path: `loading`, `loading.queue`, `store.state.isLoading`.
const BARE_PATH_RE = /^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*$/

/** True when the whole gate expression is one loading-flag path. */
export function isBareLoadingGate(expr) {
  const e = String(expr ?? '').trim()
  return BARE_PATH_RE.test(e) && /loading/i.test(e)
}

/** The <template> half of an SFC with HTML comments removed. */
export function templateOf(source) {
  const start = source.indexOf('<template')
  const end = source.lastIndexOf('</template>')
  const tpl = start >= 0 && end > start ? source.slice(start, end) : ''
  // Blank comments (keep their newlines) so reported line numbers stay true.
  return tpl.replace(/<!--[\s\S]*?-->/g, (m) => m.replace(/[^\n]/g, ''))
}

/** Count bare loading gates in one SFC source; returns { count, samples:[{line, expr}] }. */
export function countBareLoadingGates(source) {
  const tpl = templateOf(source)
  const samples = []
  for (const m of tpl.matchAll(GATE_RE)) {
    const expr = m[1] ?? m[2] ?? ''
    if (!isBareLoadingGate(expr)) continue
    const line = tpl.slice(0, m.index).split('\n').length + (source.slice(0, source.indexOf('<template')).split('\n').length - 1)
    samples.push({ line, expr: expr.trim() })
  }
  return { count: samples.length, samples }
}

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    if (name === 'node_modules' || name.startsWith('.')) continue
    const p = join(dir, name)
    const st = statSync(p)
    if (st.isDirectory()) walk(p, out)
    else if (name.endsWith('.vue')) out.push(p)
  }
  return out
}

/**
 * Scan a frontend `src` directory. Returns
 * { files: { 'src/frontend/src/<rel>': count }, samples: { key: [{line, expr}] }, total }.
 * Keys use the same repo-relative prefix as raw-color-baseline.json so the two
 * ratchets read alike.
 */
export function scanLoadingGates(srcDir) {
  const root = resolve(srcDir)
  const files = {}
  const samples = {}
  let total = 0
  for (const file of walk(root).sort()) {
    const { count, samples: s } = countBareLoadingGates(readFileSync(file, 'utf8'))
    if (count === 0) continue
    const key = 'src/frontend/src/' + relative(root, file).split('\\').join('/')
    files[key] = count
    samples[key] = s
    total += count
  }
  return { files, samples, total }
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
    console.error('usage: node scan-loading-gates.mjs <path-to-frontend-or-src> [--baseline out.json] [--json]')
    process.exit(2)
  }
  let scanRoot = resolve(rootArg)
  if (existsSync(join(scanRoot, 'src')) && !existsSync(join(scanRoot, 'App.vue'))) scanRoot = join(scanRoot, 'src')
  const result = scanLoadingGates(scanRoot)

  if (baselineOut) {
    let commit = null
    try { commit = execSync('git rev-parse HEAD', { cwd: scanRoot }).toString().trim() } catch { /* not a repo */ }
    const baseline = {
      generated: new Date().toISOString().slice(0, 10),
      commit,
      rule: 'per-file counts of bare v-if/v-else-if loading gates may only shrink; see scripts/scan-loading-gates.mjs',
      files: result.files,
      totals: { bare_loading_gates: result.total, files_with_gates: Object.keys(result.files).length },
    }
    writeFileSync(baselineOut, JSON.stringify(baseline, null, 2) + '\n')
    console.error(`baseline written: ${baselineOut} (${Object.keys(result.files).length} files, ${result.total} gates)`)
  }
  if (printJson) console.log(JSON.stringify(result, null, 2))
  else {
    console.log(JSON.stringify({ total: result.total, files_with_gates: Object.keys(result.files).length }, null, 2))
    for (const [k, n] of Object.entries(result.files).sort((a, b) => b[1] - a[1])) console.log(`${String(n).padStart(3)}  ${k}`)
  }
}
