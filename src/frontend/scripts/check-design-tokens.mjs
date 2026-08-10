#!/usr/bin/env node
/**
 * Verifies the design-system color tokens (#67):
 *   1. Each `status-*` token in tailwind.config.js is a direct alias of the
 *      Tailwind palette it claims to (catches accidental palette swaps).
 *   2. Every `bg-status-*`, `text-status-*`, `focus:ring-status-*`, or
 *      `dark:*-status-*` reference in the migrated source files uses one of
 *      the defined token names (catches typos that Tailwind would silently
 *      drop).
 *   3. The dark ink ladder holds in every SWEPT file (#1922) — see
 *      `checkDarkInkLadder` below.
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
  'action-primary':   'indigo',
}

// Known token families. Reference scanner uses this to flag references like
// `bg-status-foo-500` where `foo` isn't a defined token in the family.
const KNOWN_FAMILIES = {
  status: new Set(['success', 'warning', 'danger', 'info', 'urgent']),
  state:  new Set(['autonomous', 'locked']),
  brand:  new Set(['claude', 'gemini']),
  accent: new Set(['purple']),
  action: new Set(['primary']),
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

// ---------------------------------------------------------------------------
// Dark ink ladder (#1922)
//
// design-system.md: in dark theme, primary is gray-100, secondary gray-300,
// tertiary gray-400. **gray-500 is the floor** — disabled states and pure
// decoration only, never readable meta text (keeps ≥4.5:1 AA on gray-800).
//
// Enforcing that fleet-wide today would fail on hundreds of pre-existing hits,
// so this is a RATCHET BY MEMBERSHIP: it runs only over files a sweep has
// already cleaned. A later sweep adds its files here and they can never regress.
// Membership is the whole mechanism — there is no allowlist of individual
// lines to drift out of date.
//
// Three rules, in descending order of how mechanical they are:
//
//   R1  No `dark:text-gray-{600..900}` at all. Below the floor is wrong even
//       for decoration, so this needs no judgement and no exceptions.
//   R2  No template `text-gray-{500..900}` in a class attribute that has no
//       `dark:text-*` companion. This is the quieter breach and the one the
//       audit found in Audit.vue: with no dark override the light-theme
//       tertiary is simply reused in dark, where it sits at or below the floor.
//   R3  `dark:text-gray-500` is legal (it IS the floor) but capped per file at
//       the count left after the sweep, which is only disabled states, svg
//       glyphs and separator characters. The cap can be lowered, never raised
//       — raising it means new floor-level ink shipped, which is the thing the
//       sweep just removed.
//
// R3's numbers are deliberately literal rather than regenerated: a cap that
// rewrites itself from the current source is not a ratchet, it is a rubber stamp.
const INK_LADDER_SWEPT = {
  'components/FleetGrid.vue': 0,
  // Not in the #1922 audit list, but it renders INTO the Dashboard stats
  // bar: its separators sat next to Dashboard's own. Sweeping one and not
  // the other leaves two inks in a single row.
  'components/HostTelemetry.vue': 3,          // 3 separator glyphs
  'components/NavBar.vue': 0,
  'components/ReplayTimeline.vue': 2,          // 2 separator glyphs
  'components/SystemViewsSidebar.vue': 0,
  'components/TasksPanel.vue': 1,              // empty-state svg
  'components/operator/NotificationsPanel.vue': 2, // spinner svg + inbox svg
  'components/operator/QueueCard.vue': 4,      // separator, dismiss glyph, 2 disabled buttons
  'components/operator/QueueItemDetail.vue': 0,
  // #1848 swept the key-list metadata row, whose inverted ink pair
  // (text-gray-400 dark:text-gray-500) failed AA in BOTH themes at rest.
  'components/settings/McpKeysTab.vue': 1,     // empty-state svg glyph
  'views/Dashboard.vue': 5,                    // 3 separators, search svg, clear glyph
  'views/Login.vue': 0,
  'views/enterprise/Audit.vue': 0,
}

const BELOW_FLOOR_RE = /\bdark:text-gray-(?:600|700|800|900)\b/g
const AT_FLOOR_RE = /\bdark:text-gray-500\b/g
// `(?<!:)` keeps `dark:text-gray-500` / `hover:text-gray-500` out of R2 — those
// carry their own variant prefix and are judged by R1/R3 instead.
const BARE_DARK_UNSAFE_RE = /(?<!:)\btext-gray-(?:500|600|700|800|900)\b/

/** Blank out <script>/<style> blocks, preserving offsets AND line numbers. */
function templateOnly(source) {
  return source.replace(/<(script|style)\b[\s\S]*?<\/\1>/g, (block) =>
    block.replace(/[^\n]/g, ' ')
  )
}

function checkDarkInkLadder() {
  const failures = []
  for (const [rel, floorCap] of Object.entries(INK_LADDER_SWEPT)) {
    const full = join(FRONTEND_ROOT, 'src', rel)
    let content
    try {
      content = readFileSync(full, 'utf8')
    } catch {
      // A swept file that moved or vanished must fail loudly: silently dropping
      // it would retire the guard for that file without anyone deciding to.
      failures.push(`${rel}: listed as swept for the dark ink ladder but not readable — update INK_LADDER_SWEPT`)
      continue
    }

    const lineOf = (index) => content.slice(0, index).split('\n').length

    for (const m of content.matchAll(BELOW_FLOOR_RE)) {
      failures.push(`src/${rel}:${lineOf(m.index)}: "${m[0]}" is BELOW the dark ink floor (gray-500). Dark meta text is gray-400 or lighter; decoration stops at gray-500.`)
    }

    const template = templateOnly(content)
    for (const m of template.matchAll(/class="([^"]*)"/g)) {
      const cls = m[1]
      if (BARE_DARK_UNSAFE_RE.test(cls) && !cls.includes('dark:text-')) {
        failures.push(`src/${rel}:${lineOf(m.index)}: light-theme gray with no dark: override — it is reused verbatim in dark, at or below the floor. Add dark:text-gray-400. ("${cls.trim().slice(0, 70)}")`)
      }
    }

    const atFloor = (content.match(AT_FLOOR_RE) || []).length
    if (atFloor > floorCap) {
      failures.push(`src/${rel}: ${atFloor} uses of dark:text-gray-500, cap is ${floorCap}. The floor is for disabled states and pure decoration only — if this is meta text use gray-400; if it is genuinely decoration, raise the cap in INK_LADDER_SWEPT and say why.`)
    }
  }
  failures.push(...checkFleetGridInkVars())
  return failures
}

// FleetGrid states its ink as CSS custom properties rather than classes, so the
// three rules above cannot see it — its whole dark ladder is four hex literals.
// The vars below all resolve to TEXT (`--gv-ghost` reaches the repo label and
// the dimmed half of a stat line in AgentTile), so none may sit at or below the
// gray-500 floor.
const DARK_UNSAFE_INK_HEXES = new Set([
  '#6b7280', // gray-500 — the floor: decoration only, never text
  '#4b5563', // gray-600
  '#374151', // gray-700
  '#1f2937', // gray-800
  '#111827', // gray-900
])
const FLEETGRID_INK_VARS = ['--gv-text', '--gv-muted', '--gv-faint', '--gv-ghost']

function checkFleetGridInkVars() {
  const rel = 'components/FleetGrid.vue'
  let content
  try {
    content = readFileSync(join(FRONTEND_ROOT, 'src', rel), 'utf8')
  } catch {
    return [`${rel}: not readable — the FleetGrid dark ink check cannot run`]
  }
  const darkBlock = content.match(/:root\.dark\s+\.fleet-canvas\s*\{([\s\S]*?)\}/)
  if (!darkBlock) {
    return [`src/${rel}: the ":root.dark .fleet-canvas" block is gone — the dark ink check silently covers nothing, so update it deliberately`]
  }
  const failures = []
  for (const varName of FLEETGRID_INK_VARS) {
    const m = darkBlock[1].match(new RegExp(`${varName}\\s*:\\s*(#[0-9a-fA-F]{3,8})`))
    if (!m) continue
    const hex = m[1].toLowerCase()
    if (DARK_UNSAFE_INK_HEXES.has(hex)) {
      failures.push(`src/${rel}: dark ${varName} is ${hex}, at or below the gray-500 floor. Every --gv-* ink var here renders text; use #9ca3af (gray-400) or lighter.`)
    }
  }
  return failures
}

const paletteFailures = checkPaletteEquivalence()
const referenceFailures = checkTokenReferences()
const inkLadderFailures = checkDarkInkLadder()
const allFailures = [...paletteFailures, ...referenceFailures, ...inkLadderFailures]

if (allFailures.length > 0) {
  console.error('Design-token check FAILED:')
  for (const f of allFailures) console.error('  ' + f)
  process.exit(1)
}

const tokenCount = Object.keys(EXPECTED_ALIASES).length
const sweptCount = Object.keys(INK_LADDER_SWEPT).length
console.log(`Design-token check OK: ${tokenCount} tokens equivalent to source palettes; all references resolve; dark ink ladder holds in ${sweptCount} swept files`)
