#!/usr/bin/env node
/**
 * Verifies the design-system color tokens (#67):
 *   1. Each `status-*` token in tailwind.config.js is a direct alias of the
 *      Tailwind palette it claims to (catches accidental palette swaps).
 *   2. Every `bg-status-*`, `text-status-*`, `focus:ring-status-*`, or
 *      `dark:*-status-*` reference in the migrated source files uses one of
 *      the defined token names (catches typos that Tailwind would silently
 *      drop).
 *
 * Run via `npm run check:tokens` or directly: `node scripts/check-design-tokens.mjs`.
 */

import { readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const FRONTEND_ROOT = resolve(__dirname, '..')

// Tailwind config uses `export default` but the frontend package.json is not
// "type": "module", so Node can't import it directly here. Tailwind has its
// own loader. We read the config as text and assert each token aliases the
// expected palette via a literal `colors.<name>` reference — that's the only
// invariant this PR commits to.
const EXPECTED_ALIASES = {
  'status-success':   'green',
  'status-warning':   'yellow',
  'status-danger':    'red',
  'status-info':      'blue',
  'status-urgent':    'orange',
  'state-autonomous': 'amber',
  'state-locked':     'rose',
  'brand-claude':     'orange',
  'brand-gemini':     'blue',
  'accent-purple':    'purple',
}

// Known token families. Reference scanner uses this to flag references like
// `bg-status-foo-500` where `foo` isn't a defined token in the family.
const KNOWN_FAMILIES = {
  status: new Set(['success', 'warning', 'danger', 'info', 'urgent']),
  state:  new Set(['autonomous', 'locked']),
  brand:  new Set(['claude', 'gemini']),
  accent: new Set(['purple']),
}

function checkPaletteEquivalence() {
  const failures = []
  const configText = readFileSync(join(FRONTEND_ROOT, 'tailwind.config.js'), 'utf8')
  for (const [tokenName, paletteName] of Object.entries(EXPECTED_ALIASES)) {
    const aliasRe = new RegExp(`['"]${tokenName}['"]\\s*:\\s*colors\\.${paletteName}\\b`)
    if (!aliasRe.test(configText)) {
      failures.push(`${tokenName}: expected alias of colors.${paletteName} not found in tailwind.config.js`)
    }
  }
  return failures
}

const FAMILY_RE = Object.keys(KNOWN_FAMILIES).join('|')
const TOKEN_REFERENCE_RE = new RegExp(
  `(?:bg|text|border|ring|fill|stroke|from|to|via|focus:ring|focus:bg|focus:text|focus:border|hover:bg|hover:text|hover:border|hover:ring|dark:bg|dark:text|dark:border|dark:ring|dark:hover:bg|dark:hover:text)-(${FAMILY_RE})-([a-z]+)-(?:50|100|200|300|400|500|600|700|800|900|950)\\b`,
  'g'
)

function* walkVueAndJs(dir) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry)
    if (entry === 'node_modules' || entry === 'dist' || entry.startsWith('.')) continue
    const stat = statSync(full)
    if (stat.isDirectory()) yield* walkVueAndJs(full)
    else if (/\.(vue|js|ts|jsx|tsx)$/.test(entry)) yield full
  }
}

function checkTokenReferences() {
  const failures = []
  for (const file of walkVueAndJs(join(FRONTEND_ROOT, 'src'))) {
    const content = readFileSync(file, 'utf8')
    for (const match of content.matchAll(TOKEN_REFERENCE_RE)) {
      const [whole, family, variant] = match
      const variants = KNOWN_FAMILIES[family]
      if (!variants?.has(variant)) {
        const line = content.slice(0, match.index).split('\n').length
        failures.push(`${file.replace(FRONTEND_ROOT + '/', '')}:${line}: unknown ${family}-* token "${variant}" in "${whole}"`)
      }
    }
  }
  return failures
}

const paletteFailures = checkPaletteEquivalence()
const referenceFailures = checkTokenReferences()
const allFailures = [...paletteFailures, ...referenceFailures]

if (allFailures.length > 0) {
  console.error('Design-token check FAILED:')
  for (const f of allFailures) console.error('  ' + f)
  process.exit(1)
}

const tokenCount = Object.keys(EXPECTED_ALIASES).length
console.log(`Design-token check OK: ${tokenCount} tokens equivalent to source palettes; all references resolve`)
