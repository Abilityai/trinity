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
  'status-success': 'green',
  'status-warning': 'yellow',
  'status-danger':  'red',
  'status-info':    'blue',
  'status-urgent':  'orange',
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

const TOKEN_REFERENCE_RE = /(?:bg|text|border|ring|fill|stroke|from|to|via|focus:ring|focus:bg|focus:text|focus:border|hover:bg|hover:text|hover:border|hover:ring|dark:bg|dark:text|dark:border|dark:ring|dark:hover:bg|dark:hover:text)-status-([a-z]+)-(?:50|100|200|300|400|500|600|700|800|900|950)\b/g

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
  const knownTokens = new Set(Object.keys(EXPECTED_ALIASES).map(t => t.replace(/^status-/, '')))
  const failures = []
  for (const file of walkVueAndJs(join(FRONTEND_ROOT, 'src'))) {
    const content = readFileSync(file, 'utf8')
    for (const match of content.matchAll(TOKEN_REFERENCE_RE)) {
      const family = match[1]
      if (!knownTokens.has(family)) {
        const line = content.slice(0, match.index).split('\n').length
        failures.push(`${file.replace(FRONTEND_ROOT + '/', '')}:${line}: unknown status family "${family}" in "${match[0]}"`)
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
