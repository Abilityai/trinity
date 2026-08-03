#!/usr/bin/env node
/**
 * scan-raw-colors.mjs — design-system raw-color scanner / ratchet seed.
 *
 * Scans a Vue frontend for classes and literals that bypass the semantic
 * design-token layer defined in tailwind.config.js (status-*, state-*,
 * brand-*, accent-*, action-* — see src/frontend/scripts/check-design-tokens.mjs
 * in Abilityai/trinity for the token families this scanner is aligned with).
 *
 * Counts, per file:
 *   raw_nongray       — raw Tailwind palette classes (bg-green-500, hover:text-red-300, …)
 *                       for every palette family EXCEPT gray. Hard violations.
 *   raw_gray          — raw gray-* palette classes (chrome; partially sanctioned,
 *                       tracked separately — includes the custom gray-750).
 *   hardcoded_colors  — hex literals and rgb()/rgba() literals in .vue
 *                       <template>/<style> blocks (comments excluded).
 *   semantic_tokens   — semantic token-class usages (for ratio comparison).
 *
 * Also runs two best-effort spot-checks: overflow-x-auto strips (OverflowTabs
 * adoption candidates) and <textarea> resize constraints.
 *
 * Semantic token classes (e.g. bg-status-success-500, text-action-primary-600)
 * are never counted as raw: the palette word must directly follow the utility,
 * so status-/state-/brand-/accent-/action- prefixed families cannot match.
 *
 * Known limitations (accepted, best-effort): dynamically built class strings
 * (`bg-${color}-500`) are invisible; whole-line // comments are stripped but
 * inline // comments are not; template/style comment stripping is regex-based.
 *
 * Usage:
 *   node scan-raw-colors.mjs <path-to-frontend-or-src> [--baseline out.json] [--json]
 *
 *   <path>           directory containing src/ (e.g. trinity/src/frontend), or a src dir itself
 *   --baseline FILE  write the ratchet-seed JSON (only files with nonzero counts)
 *   --json           print the full result JSON (files, samples, spot-checks) to stdout
 *
 * Exit code is always 0 — this is a measurement tool; the future CI ratchet
 * compares totals against the committed baseline.
 */

import { readFileSync, readdirSync, statSync, writeFileSync, existsSync } from 'node:fs'
import { join, relative, resolve } from 'node:path'
import { execSync } from 'node:child_process'

// ---------------------------------------------------------------- config

const PALETTE_FAMILIES = [
  'red', 'orange', 'amber', 'yellow', 'lime', 'green', 'emerald', 'teal',
  'cyan', 'sky', 'blue', 'indigo', 'violet', 'purple', 'fuchsia', 'pink',
  'rose', 'slate', 'zinc', 'neutral', 'stone', 'gray',
]

const SEMANTIC_TOKENS = [
  'status-success', 'status-warning', 'status-danger', 'status-info', 'status-urgent',
  'state-autonomous', 'state-locked',
  'brand-claude', 'brand-gemini',
  'accent-purple', 'action-primary',
]

// Color-bearing utility prefixes (superset of check-design-tokens.mjs's list).
const UTIL = '(?:bg|text|border(?:-[trblxyse])?|ring-offset|ring|divide|fill|stroke|from|to|via|placeholder|caret|accent|decoration|outline|shadow)'
const SHADE = '(?:50|100|200|300|400|500|600|700|750|800|900|950)'
const VARIANT = '(?:[a-zA-Z0-9-]+:)*' // hover:, dark:, focus:, group-hover:, md:, chained…

// A raw palette class: optional variant chain, utility, palette family, shade.
// The (?<![\w-]) guard means the palette word cannot be the tail of a semantic
// token (bg-accent-purple-500 → "purple" is preceded by "-", rejected).
const RAW_RE = new RegExp(
  `(?<![\\w-])(${VARIANT})${UTIL}-(${PALETTE_FAMILIES.join('|')})-${SHADE}(?:\\/\\d{1,3})?(?![\\w-])`, 'g')

const SEMANTIC_RE = new RegExp(
  `(?<![\\w-])(${VARIANT})${UTIL}-(${SEMANTIC_TOKENS.join('|')})-${SHADE}(?:\\/\\d{1,3})?(?![\\w-])`, 'g')

// Hex literal (not an HTML entity like &#160;) and rgb()/rgba() with a numeric body.
const HEX_RE = /(?<!&)#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})\b/g
const RGB_RE = /\brgba?\(\s*\d[^)]*\)/g

// ---------------------------------------------------------------- helpers

function* walk(dir) {
  for (const entry of readdirSync(dir)) {
    if (entry === 'node_modules' || entry === 'dist' || entry.startsWith('.')) continue
    const full = join(dir, entry)
    const stat = statSync(full)
    if (stat.isDirectory()) yield* walk(full)
    else if (/\.(vue|js)$/.test(entry)) yield full
  }
}

/** Replace a span with spaces, preserving newlines so line numbers survive. */
function blank(text) {
  return text.replace(/[^\n]/g, ' ')
}

/** Strip comments best-effort, preserving offsets/line numbers. */
function stripComments(content) {
  return content
    .replace(/<!--[\s\S]*?-->/g, blank)          // HTML comments
    .replace(/\/\*[\s\S]*?\*\//g, blank)         // block comments (JS + CSS)
    .replace(/^\s*\/\/[^\n]*/gm, blank)          // whole-line // comments
}

function lineOf(content, index) {
  let line = 1
  for (let i = 0; i < index; i++) if (content.charCodeAt(i) === 10) line++
  return line
}

/** Extract [start, end) offset ranges of <template> and <style> blocks of a .vue file. */
function vueTemplateStyleRanges(content) {
  const ranges = []
  const t = content.match(/<template[^>]*>[\s\S]*<\/template>/) // greedy: nested </template> safe
  if (t) ranges.push([t.index, t.index + t[0].length])
  for (const s of content.matchAll(/<style[^>]*>[\s\S]*?<\/style>/g)) {
    ranges.push([s.index, s.index + s[0].length])
  }
  return ranges
}

function collect(content, re, file, samples, kind) {
  let n = 0
  for (const m of content.matchAll(re)) {
    n++
    if (samples) samples.push({ file, line: lineOf(content, m.index), text: m[0], kind })
  }
  return n
}

// ---------------------------------------------------------------- main

const args = process.argv.slice(2)
const baselineIdx = args.indexOf('--baseline')
const baselineOut = baselineIdx >= 0 ? args[baselineIdx + 1] : null
const printJson = args.includes('--json')
const rootArg = args.find(a => !a.startsWith('--') && a !== baselineOut)
if (!rootArg) {
  console.error('usage: node scan-raw-colors.mjs <path-to-frontend-or-src> [--baseline out.json] [--json]')
  process.exit(2)
}
let scanRoot = resolve(rootArg)
if (existsSync(join(scanRoot, 'src')) && !existsSync(join(scanRoot, 'App.vue'))) scanRoot = join(scanRoot, 'src')

const files = {}          // repo-relative path → counts
const samples = {}        // path → sample violations (raw classes + hardcoded)
const spot = { overflowXAuto: [], textareas: [] }
const totals = { raw_nongray: 0, raw_gray: 0, hardcoded_colors: 0, semantic_tokens: 0 }

for (const file of walk(scanRoot)) {
  const rel = relative(scanRoot, file)
  const raw = readFileSync(file, 'utf8')
  const content = stripComments(raw)
  const fileSamples = []

  // 1. raw palette + semantic classes (whole file — class maps live in <script> too)
  let rawNongray = 0, rawGray = 0
  for (const m of content.matchAll(RAW_RE)) {
    const family = m[2]
    if (family === 'gray') rawGray++
    else {
      rawNongray++
      fileSamples.push({ line: lineOf(content, m.index), text: m[0], kind: 'raw' })
    }
  }
  const semantic = collect(content, SEMANTIC_RE)

  // 2. hardcoded colors — .vue template/style blocks only
  let hardcoded = 0
  if (file.endsWith('.vue')) {
    for (const [start, end] of vueTemplateStyleRanges(content)) {
      const block = content.slice(start, end)
      for (const re of [HEX_RE, RGB_RE]) {
        for (const m of block.matchAll(re)) {
          hardcoded++
          fileSamples.push({ line: lineOf(content, start + m.index), text: m[0], kind: 'hardcoded' })
        }
      }
    }
  }

  // 3. spot-checks
  for (const m of content.matchAll(/overflow-x-auto/g)) {
    const line = lineOf(content, m.index)
    const lineText = content.split('\n')[line - 1] ?? ''
    const ctx = content.slice(Math.max(0, m.index - 400), m.index + 400)
    spot.overflowXAuto.push({
      file: rel, line,
      tabNavCandidate: /tab|nav/i.test(rel) || /tab|nav/i.test(ctx),
      lineText: lineText.trim().slice(0, 160),
    })
  }
  for (const m of content.matchAll(/<textarea[\s\S]*?>/g)) {
    const tag = m[0]
    spot.textareas.push({
      file: rel, line: lineOf(content, m.index),
      resize: tag.includes('resize-none') ? 'none'
        : tag.includes('resize-y') ? 'vertical'
        : tag.includes('resize-x') ? 'horizontal'
        : /\bresize\b/.test(tag) ? 'both' : 'unconstrained',
    })
  }

  if (rawNongray || rawGray || hardcoded || semantic) {
    files[rel] = { raw_nongray: rawNongray, raw_gray: rawGray, hardcoded_colors: hardcoded, semantic_tokens: semantic }
    totals.raw_nongray += rawNongray
    totals.raw_gray += rawGray
    totals.hardcoded_colors += hardcoded
    totals.semantic_tokens += semantic
    if (fileSamples.length) samples[rel] = fileSamples
  }
}

// ---------------------------------------------------------------- output

const count = (key) => Object.values(files).filter(f => f[key] > 0).length
const summary = {
  scanned_root: scanRoot,
  totals,
  file_counts: {
    with_raw_nongray: count('raw_nongray'),
    with_raw_gray: count('raw_gray'),
    with_raw_any: Object.values(files).filter(f => f.raw_nongray + f.raw_gray > 0).length,
    with_hardcoded: count('hardcoded_colors'),
    with_semantic: count('semantic_tokens'),
  },
}

if (baselineOut) {
  let commit = null
  try { commit = execSync('git rev-parse HEAD', { cwd: scanRoot }).toString().trim() } catch { /* not a repo */ }
  const baselineFiles = {}
  for (const [rel, c] of Object.entries(files)) {
    if (c.raw_nongray + c.raw_gray + c.hardcoded_colors === 0) continue
    baselineFiles['src/frontend/src/' + rel] = {
      raw_nongray: c.raw_nongray, raw_gray: c.raw_gray, hardcoded_colors: c.hardcoded_colors,
    }
  }
  const baseline = {
    generated: new Date().toISOString().slice(0, 10),
    branch: 'dev',
    commit,
    files: baselineFiles,
    totals: {
      raw_nongray: totals.raw_nongray,
      raw_gray: totals.raw_gray,
      hardcoded_colors: totals.hardcoded_colors,
      semantic_tokens: totals.semantic_tokens,
      files_with_violations: Object.keys(baselineFiles).length,
    },
  }
  writeFileSync(baselineOut, JSON.stringify(baseline, null, 2) + '\n')
  console.error(`baseline written: ${baselineOut} (${Object.keys(baselineFiles).length} files)`)
}

if (printJson) {
  console.log(JSON.stringify({ ...summary, files, samples, spot }, null, 2))
} else {
  console.log(JSON.stringify(summary, null, 2))
  const top = Object.entries(files)
    .sort((a, b) => b[1].raw_nongray - a[1].raw_nongray)
    .slice(0, 15)
  console.log('\nTop offenders by raw_nongray:')
  for (const [rel, c] of top) {
    console.log(`  ${String(c.raw_nongray).padStart(4)} nongray  ${String(c.raw_gray).padStart(4)} gray  ${String(c.hardcoded_colors).padStart(3)} hardcoded  ${rel}`)
  }
}
