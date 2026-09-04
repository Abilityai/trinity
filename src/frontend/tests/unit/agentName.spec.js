import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { parse as parseSfc } from 'vue/compiler-sfc'
import {
  agentDisplayName,
  hasDistinctLabel,
  agentNameParts,
  agentNameTooltip,
  agentOptionLabel,
} from '../../src/utils/agentName.js'

/**
 * Agent name resolution (ent#181), and the #2358 addition that makes the slug
 * visible again.
 *
 * An agent has two names and they are not interchangeable: the SLUG is the
 * identity (routes, container + volume names, MCP keys, every `agent_name`
 * column) and the LABEL is presentation. Requirements §1.3.1 FR-3 says one
 * helper resolves them everywhere — never a per-site `display_label || name`
 * chain — and FR-4 says the slug stays visible and copyable wherever the label
 * hides it.
 *
 * The Dashboard List and Grid tile shipped the label ALONE with the slug
 * `title`-only, so nothing on screen connected "Delivery Operations Manager"
 * to the `delivery-ops` that every URL and key is written against. #2358 adds
 * `agentNameParts()` as the single decidable rule behind that fix.
 *
 * These are the FIRST unit tests over this module — it had none, on all four
 * exported functions, despite being the resolution point for every agent name
 * the product renders.
 */

describe('agentDisplayName', () => {
  it('prefers the display label', () => {
    expect(agentDisplayName({ name: 'delivery-ops', display_label: 'Delivery Ops' })).toBe(
      'Delivery Ops'
    )
  })

  it('trims the label', () => {
    expect(agentDisplayName({ name: 'a', display_label: '  Padded  ' })).toBe('Padded')
  })

  it('falls back to the slug for an absent, null, empty or whitespace-only label', () => {
    expect(agentDisplayName({ name: 'delivery-ops' })).toBe('delivery-ops')
    expect(agentDisplayName({ name: 'delivery-ops', display_label: null })).toBe('delivery-ops')
    expect(agentDisplayName({ name: 'delivery-ops', display_label: '' })).toBe('delivery-ops')
    expect(agentDisplayName({ name: 'delivery-ops', display_label: '   ' })).toBe('delivery-ops')
  })

  it('falls back to the slug for a non-string label', () => {
    expect(agentDisplayName({ name: 'a', display_label: 42 })).toBe('a')
    expect(agentDisplayName({ name: 'a', display_label: { toString: () => 'x' } })).toBe('a')
  })

  it('passes a bare slug string through (legacy callers)', () => {
    expect(agentDisplayName('delivery-ops')).toBe('delivery-ops')
  })

  it('returns the empty string for null/undefined rather than throwing', () => {
    expect(agentDisplayName(null)).toBe('')
    expect(agentDisplayName(undefined)).toBe('')
  })
})

describe('hasDistinctLabel', () => {
  it('is true only when a label is set AND differs from the slug', () => {
    expect(hasDistinctLabel({ name: 'delivery-ops', display_label: 'Delivery Ops' })).toBe(true)
    expect(hasDistinctLabel({ name: 'delivery-ops', display_label: 'delivery-ops' })).toBe(false)
  })

  it('is false for an absent, empty, whitespace-only or non-string label', () => {
    expect(hasDistinctLabel({ name: 'a' })).toBe(false)
    expect(hasDistinctLabel({ name: 'a', display_label: null })).toBe(false)
    expect(hasDistinctLabel({ name: 'a', display_label: '' })).toBe(false)
    expect(hasDistinctLabel({ name: 'a', display_label: '   ' })).toBe(false)
    expect(hasDistinctLabel({ name: 'a', display_label: 7 })).toBe(false)
    expect(hasDistinctLabel({ name: 'a', display_label: {} })).toBe(false)
  })

  it('is false for a bare string or null input', () => {
    expect(hasDistinctLabel('delivery-ops')).toBe(false)
    expect(hasDistinctLabel(null)).toBe(false)
    expect(hasDistinctLabel(undefined)).toBe(false)
  })
})

describe('agentNameParts (#2358)', () => {
  it('returns the label as primary and the slug as secondary when a label hides it', () => {
    expect(agentNameParts({ name: 'delivery-ops', display_label: 'Delivery Ops' })).toEqual({
      primary: 'Delivery Ops',
      secondary: 'delivery-ops',
    })
  })

  it('returns no secondary when there is nothing hidden', () => {
    expect(agentNameParts({ name: 'delivery-ops' })).toEqual({
      primary: 'delivery-ops',
      secondary: null,
    })
    expect(agentNameParts({ name: 'delivery-ops', display_label: 'delivery-ops' })).toEqual({
      primary: 'delivery-ops',
      secondary: null,
    })
    expect(agentNameParts({ name: 'delivery-ops', display_label: '   ' })).toEqual({
      primary: 'delivery-ops',
      secondary: null,
    })
    expect(agentNameParts({ name: 'delivery-ops', display_label: null })).toEqual({
      primary: 'delivery-ops',
      secondary: null,
    })
  })

  it('trims the primary but leaves the secondary as the literal slug', () => {
    expect(agentNameParts({ name: 'delivery-ops', display_label: '  Delivery Ops  ' })).toEqual({
      primary: 'Delivery Ops',
      secondary: 'delivery-ops',
    })
  })

  /**
   * The load-bearing property. `secondary` is what a surface renders as the
   * agent's IDENTITY — the string a reader will paste into a URL, a `docker`
   * command or an MCP key lookup. If it could ever be the label, the fix would
   * be worse than the bug it replaces: the row would show two presentation
   * names and still hide the identity.
   */
  it('secondary is always either null or EXACTLY agent.name — never the label', () => {
    const cases = [
      { name: 'delivery-ops', display_label: 'Delivery Ops' },
      { name: 'delivery-ops', display_label: '  Delivery Ops  ' },
      { name: 'delivery-ops', display_label: 'delivery-ops' },
      { name: 'delivery-ops', display_label: '' },
      { name: 'delivery-ops', display_label: null },
      { name: 'delivery-ops', display_label: 42 },
      { name: 'delivery-ops' },
      { name: 'trinity-system', display_label: 'Platform Orchestrator' },
    ]
    for (const agent of cases) {
      const { secondary } = agentNameParts(agent)
      expect(secondary === null || secondary === agent.name).toBe(true)
    }
  })

  it('handles a bare slug string (legacy callers) and null without throwing', () => {
    expect(agentNameParts('delivery-ops')).toEqual({ primary: 'delivery-ops', secondary: null })
    expect(agentNameParts(null)).toEqual({ primary: '', secondary: null })
    expect(agentNameParts(undefined)).toEqual({ primary: '', secondary: null })
  })

  it('agrees with the two helpers it composes', () => {
    const cases = [
      { name: 'a', display_label: 'A Label' },
      { name: 'a', display_label: 'a' },
      { name: 'a' },
      'a',
      null,
    ]
    for (const agent of cases) {
      const parts = agentNameParts(agent)
      expect(parts.primary).toBe(agentDisplayName(agent))
      expect(parts.secondary === null).toBe(!hasDistinctLabel(agent))
    }
  })
})

describe('agentNameTooltip', () => {
  it('carries BOTH names when they differ (a belt for a truncated label)', () => {
    expect(agentNameTooltip({ name: 'delivery-ops', display_label: 'Delivery Ops' })).toBe(
      'Delivery Ops · delivery-ops'
    )
  })

  it('is the bare display name when nothing is hidden', () => {
    expect(agentNameTooltip({ name: 'delivery-ops' })).toBe('delivery-ops')
    expect(agentNameTooltip('delivery-ops')).toBe('delivery-ops')
    expect(agentNameTooltip(null)).toBe('')
  })
})

describe('agentOptionLabel', () => {
  it('disambiguates inline in a picker `<option>` (#1642)', () => {
    expect(agentOptionLabel({ name: 'delivery-ops', display_label: 'Delivery Ops' })).toBe(
      'Delivery Ops (delivery-ops)'
    )
  })

  it('is the bare display name when nothing is hidden', () => {
    expect(agentOptionLabel({ name: 'delivery-ops' })).toBe('delivery-ops')
    expect(agentOptionLabel('delivery-ops')).toBe('delivery-ops')
  })
})

/**
 * Structural guards (#2358) — read from the SFC source.
 *
 * `vitest.config.js` pins `environment: 'node'` with no mount harness, so the
 * rules below are not reachable as behaviour here, and the Playwright specs
 * that WOULD reach them are `ui`-label-gated and do not run on most PRs.
 * Reading the source costs nothing and runs everywhere — the
 * `gridTileLinks.spec.js` pattern.
 *
 * Each rule is pinned TERM BY TERM rather than by one broad match: a guard that
 * only checks "some placement class is present" goes green on the exact typo it
 * exists to catch.
 */
const PANEL = readFileSync(
  resolve(dirname(fileURLToPath(import.meta.url)), '../../src/components/AgentListPanel.vue'),
  'utf8'
)
const TILE = readFileSync(
  resolve(dirname(fileURLToPath(import.meta.url)), '../../src/components/AgentTile.vue'),
  'utf8'
)

/** Every `class="…"` / `:class="…"` attribute value containing `token`. */
function classAttrsContaining(source, token) {
  const attrs = source.match(/:?class="[^"]*"/g) || []
  return attrs.filter((a) => a.includes(token))
}

describe('structural: name resolution goes through the helper, never a chain', () => {
  it.each([
    ['AgentListPanel.vue', PANEL],
    ['AgentTile.vue', TILE],
  ])('%s has no `display_label ||` / `??` / ternary chain', (_name, source) => {
    // Ternary included on purpose: `display_label ? display_label : name` is
    // the same defect wearing different punctuation (§1.3.1 FR-3 — resolution
    // goes through a single helper, not per-site chains).
    expect(source).not.toMatch(/display_label\s*(\|\||\?\?|\?)/)
  })
})

describe('structural: the lg list is ONE column sizing context (#2358)', () => {
  it('declares the track template exactly once — on the list container', () => {
    // Two copies of a template string is the defect itself: each grid resolves
    // its own `auto` tracks and the `1fr` name track absorbs the difference.
    const templates = PANEL.match(/lg:grid-cols-\[/g) || []
    expect(templates).toHaveLength(1)
    expect(PANEL).toContain(
      'lg:grid-cols-[auto_auto_auto_1fr_46px_22rem_180px_auto_auto_auto]'
    )
  })

  it('makes the header AND the row subgrid items spanning every track', () => {
    expect((PANEL.match(/lg:grid-cols-subgrid/g) || [])).toHaveLength(2)
    const header = classAttrsContaining(PANEL, 'lg:grid-cols-subgrid').find((a) =>
      a.includes('hidden lg:grid')
    )
    expect(header, 'the header is a subgrid item').toBeTruthy()
    expect(header).toContain('lg:col-span-full')
    const row = classAttrsContaining(PANEL, 'lg:grid lg:grid-cols-subgrid')[0]
    expect(row, 'the row is a subgrid item').toBeTruthy()
    expect(row).toContain('lg:col-span-full')
  })

  it('places the CapacityMeter definitely in BOTH axes — track 10, rows 1–2', () => {
    // Sparse auto-placement would put it in rows 2–3 (the secondary line's
    // definite column bumps the cursor to row 2 first), hanging an implicit
    // third row below every line. `row-start-1` after `row-span-2` is what
    // makes it `grid-row: 1 / span 2` — Tailwind emits gridRow before
    // gridRowStart.
    const meters = PANEL.match(/<CapacityMeter[\s\S]*?\/>/g) || []
    const placed = meters.filter((m) => m.includes('lg:col-start-10'))
    expect(placed).toHaveLength(1)
    expect(placed[0]).toContain('lg:row-start-1')
    expect(placed[0]).toContain('lg:row-span-2')
  })

  it('places the secondary line definitely on row 2, under the name column', () => {
    const lines = classAttrsContaining(PANEL, 'lg:row-start-2')
    expect(lines).toHaveLength(1)
    expect(lines[0]).toContain('lg:col-start-4')
    expect(lines[0]).toContain('lg:col-end-10')
    // No-wrap contract: a long slug + badges + tags must never open a third row.
    expect(lines[0]).toContain('flex-nowrap')
    expect(lines[0]).toContain('overflow-hidden')
    expect(lines[0]).toContain('min-h-[1.375rem]')
  })

  it('gives no subgrid item horizontal padding — insets are item margins', () => {
    // A subgrid item's own horizontal padding is laid out INSIDE its first and
    // last tracks (CSS Grid L2 §7.1); re-adding `pl-8` here would silently make
    // alignment depend on that corner again. `lg:ml-8` / `lg:mr-4` on the first
    // and last cells are ordinary L1 margins and contribute identically on the
    // header and every row.
    const subgridItems = classAttrsContaining(PANEL, 'lg:grid-cols-subgrid')
    for (const attr of subgridItems) {
      expect(attr).not.toMatch(/lg:p[xlr]-/)
    }
    expect(PANEL).toContain('lg:ml-8')
    expect(PANEL).toContain('lg:mr-4')
  })
})

/**
 * The lg row's SHAPE, read from the parsed template rather than from a regex.
 *
 * The subgrid contract is arithmetic: ten tracks, nine children that
 * auto-place across row 1, and two placed by hand (the secondary line on row
 * 2, the meter in track 10 spanning both). Every one of those numbers is load
 * bearing and none of them is visible in a class string, so a source regex
 * cannot see them — and neither could the rest of this file: adding a twelfth
 * child to the `lg:contents` wrapper leaves the whole suite green while the
 * new cell auto-places into row 2, column 1, in the row's left gutter, and
 * widens track 1 for the header and every row with it.
 *
 * `vue/compiler-sfc` is the parser Vite already uses on this file, reached
 * through the declared `vue` dependency. Parsing rather than matching also
 * makes the guard immune to re-indentation and to comments, which a
 * whitespace- or line-based count is not.
 */
function templateAst(source) {
  const { descriptor, errors } = parseSfc(source)
  expect(errors, 'the SFC parses').toHaveLength(0)
  expect(descriptor.template, 'the SFC has a <template>').toBeTruthy()
  return descriptor.template.ast
}

/** Depth-first element walk over a template AST. */
function eachElement(node, fn) {
  if (node && node.type === 1) fn(node)
  for (const child of node.children || []) {
    if (child && typeof child === 'object') eachElement(child, fn)
  }
}

/** Every class this element declares, static `class` and bound `:class` alike. */
function declaredClasses(node) {
  return (node.props || [])
    .map((p) => {
      if (p.type === 6 && p.name === 'class') return p.value ? p.value.content : ''
      if (p.type === 7 && p.name === 'bind' && p.arg && p.arg.content === 'class') {
        return p.exp ? p.exp.content : ''
      }
      return ''
    })
    .join(' ')
}

function staticAttr(node, name) {
  const p = (node.props || []).find((p) => p.type === 6 && p.name === name)
  return p && p.value ? p.value.content : null
}

function directiveNames(node) {
  return (node.props || []).filter((p) => p.type === 7).map((p) => p.name)
}

function findElement(source, predicate) {
  let found = null
  eachElement(templateAst(source), (n) => {
    if (found === null && predicate(n)) found = n
  })
  return found
}

const elementChildren = (node) => (node.children || []).filter((c) => c.type === 1)

describe('structural: the lg row is ten tracks and eleven items (#2358)', () => {
  const container = () =>
    findElement(PANEL, (n) => declaredClasses(n).includes('lg:grid-cols-['))
  const header = () => findElement(PANEL, (n) => staticAttr(n, 'data-testid') === 'list-header')
  const lgCells = () => findElement(PANEL, (n) => declaredClasses(n).includes('lg:contents'))

  it('gives the header exactly one cell per declared track', () => {
    // The header's spacer widths no longer decide alignment, but its CELL
    // COUNT still does: an eleventh header cell auto-places onto a second
    // header row instead of erroring, and a tenth track with only nine cells
    // leaves the last column unlabelled and unmeasured by the e2e.
    const tracks = declaredClasses(container()).match(/lg:grid-cols-\[([^\]]+)\]/)
    expect(tracks, 'the container declares the track template').toBeTruthy()
    expect(tracks[1].split('_')).toHaveLength(10)
    expect(elementChildren(header())).toHaveLength(10)
  })

  it('gives the row nine auto-placed cells plus the two it places by hand', () => {
    const children = elementChildren(lgCells())
    expect(children, 'the lg wrapper holds exactly eleven items').toHaveLength(11)

    const placed = children.filter((c) => /lg:(row|col)-start-/.test(declaredClasses(c)))
    // Exactly two: the secondary line (row 2, columns 4-9) and the meter
    // (column 10, rows 1-2). A third would mean someone placed a cell by hand
    // instead of letting the tracks do it; a first-through-ninth that auto-
    // places past track 9 is what opens a phantom row.
    expect(placed.map((c) => c.tag)).toEqual(['div', 'CapacityMeter'])
    expect(children.length - placed.length, 'nine cells fill row 1, tracks 1-9').toBe(9)
  })

  it('makes every auto-placed cell unconditional', () => {
    // Auto-placement is positional: a `v-if` on cell 5 shifts cells 6-9 one
    // track left on THAT ROW ONLY, which is the per-row misalignment this
    // whole change exists to remove. Reserve with `invisible` (the system
    // row's Run toggle) or an empty box, never `v-if`, on a cell that owns a
    // track. Definitely-placed items are exempt — their track is named, so a
    // `v-if` there leaves a hole rather than a shift.
    for (const cell of elementChildren(lgCells())) {
      if (/lg:(row|col)-start-/.test(declaredClasses(cell))) continue
      expect(
        directiveNames(cell),
        `the ${cell.tag} cell must not be conditional or repeated`
      ).not.toEqual(expect.arrayContaining(['if']))
      expect(directiveNames(cell)).not.toEqual(expect.arrayContaining(['for']))
      expect(directiveNames(cell)).not.toEqual(expect.arrayContaining(['else-if']))
      expect(directiveNames(cell)).not.toEqual(expect.arrayContaining(['else']))
    }
  })
})

/** The SFC's `<template>` region — everything before `<script setup>`. */
function templateOf(source) {
  const i = source.indexOf('<script setup>')
  expect(i, 'SFC has a <script setup> block').toBeGreaterThan(0)
  return source.slice(0, i)
}

describe('structural: all three List breakpoints render the same names (#2358 AC #7)', () => {
  it('renders the primary name via agentNameParts at every name site', () => {
    const tpl = templateOf(PANEL)
    // lg, md and base each have their own name-rendering site; the bug was
    // fixed at one of them and left at the other two more than once in this
    // component's history.
    expect((tpl.match(/agentNameParts\(agent\)\.primary/g) || [])).toHaveLength(3)
    // `agentDisplayName` is still imported — the name filter and the toasts use
    // it — but nothing in the markup may resolve a name a second way.
    expect(tpl).not.toContain('agentDisplayName(')
  })

  it('renders the slug as real text at every breakpoint, selectable, outside the link', () => {
    const tpl = templateOf(PANEL)
    for (const id of ['agent-slug-lg', 'agent-slug-md', 'agent-slug-base']) {
      const el = tpl.match(new RegExp(`<code[\\s\\S]{0,400}?${id}[\\s\\S]{0,400}?</code>`))
      expect(el, `${id} renders`).toBeTruthy()
      // FR-4 says visible AND copyable: a `title` is invisible on touch,
      // unreachable by keyboard and impossible to copy from, and `select-all`
      // is what makes ONE click take the whole hyphenated slug (plain text
      // selection takes a single segment on double-click).
      expect(el[0], `${id} is selectable`).toContain('select-all')
      expect(el[0], `${id} shows the slug`).toContain('agentNameParts(agent).secondary')
    }
    // Never inside the router-link: the slug is a copy target, not a nav target.
    expect(tpl).not.toMatch(/<router-link[\s\S]{0,600}?agent-slug-/)
  })

  it('gates every List runtime badge on the non-default rule, and none at base', () => {
    const tpl = templateOf(PANEL)
    const badges = tpl.match(/<RuntimeBadge[\s\S]*?\/>/g) || []
    expect(badges.length, 'lg + md secondary lines only').toBe(2)
    for (const b of badges) expect(b).toContain('showsRuntimeBadgeInList(agent)')
  })
})

describe('structural: the Grid tile shows the same two names (#2358 AC #3)', () => {
  it('renders the primary via agentNameParts and no second resolution', () => {
    const tpl = templateOf(TILE)
    expect(tpl).toContain('agentNameParts(agent).primary')
    expect(tpl).not.toContain('agentDisplayName(')
  })

  it('puts the slug on the EXISTING meta line, copyable, and not draggable', () => {
    const tpl = templateOf(TILE)
    const el = tpl.match(/<code[\s\S]{0,400}?agent-slug-tile[\s\S]{0,400}?<\/code>/)
    expect(el, 'the tile slug renders').toBeTruthy()
    expect(el[0]).toContain('agentNameParts(agent).secondary')
    // `.gtile` sets `user-select: none` and FleetGrid.onTilePointerDown starts a
    // drag unless the target is inside `.nodrag` — without both of these the
    // slug can be neither selected nor copied, and a click drags the tile.
    expect(el[0]).toContain('nodrag')
    expect(TILE).toContain('user-select: all')
    // It rides `.t-repo`, which is always rendered: a third identity line would
    // compress the zone rhythm of labelled tiles only, inside a fixed cell.
    expect(tpl).toMatch(/class="t-repo"[\s\S]{0,900}?agent-slug-tile/)
  })

  it('gives the slug its own ellipsis and ink rather than inheriting the line', () => {
    // `.t-repo span` targets `span`, so a `code` gets no ellipsis from it; and
    // `.t-repo.local` ghosts the whole line for a repo-less agent, which the
    // identity must not be.
    const rule = TILE.match(/\.t-slug\s*\{[\s\S]*?\}/)
    expect(rule, '.t-slug rule exists').toBeTruthy()
    expect(rule[0]).toContain('text-overflow: ellipsis')
    expect(rule[0]).toContain('color: var(--gv-muted)')
    expect(rule[0]).toContain('max-width')
  })
})
