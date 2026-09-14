import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

/**
 * #1925 — a RATCHET, not the feature's coverage.
 *
 * What the strips actually DO is driven for real by `overflowFit.spec.js` (the
 * fit arithmetic) and `navLinks.spec.js` (the link set and its active/badge
 * rules). Those execute the shipped code. This file exists for the one thing a
 * unit test cannot execute in a `environment: 'node'` suite — that the three
 * audited surfaces are still WIRED to the primitive and have not drifted back
 * to a hidden-scrollbar scroller, which is how #1789 left NavBar hiding links
 * with no signal that any existed.
 */
const ROOT = join(dirname(fileURLToPath(import.meta.url)), '../../src')
const read = (rel) => readFileSync(join(ROOT, rel), 'utf8')

// The surfaces this issue converted. A strip here may not scroll horizontally.
const CONVERTED = [
  'components/NavBar.vue',
  'views/Settings.vue',
  'views/Operations.vue',
]

describe('#1925 tab-strip adoption', () => {
  it.each(CONVERTED)('%s has no horizontally-scrolling nav strip', (rel) => {
    const src = read(rel)
    // Strip HTML comments first: this very file's rationale mentions the class
    // by name, and a guard satisfied by its own prose guards nothing.
    const code = src.replace(/<!--[\s\S]*?-->/g, '')
    expect(code).not.toMatch(/overflow-x-auto/)
  })

  it('NavBar no longer suppresses a scrollbar to hide the overflow it created', () => {
    const src = read('components/NavBar.vue')
    expect(src).not.toMatch(/scrollbar-width/)
    expect(src).not.toMatch(/::-webkit-scrollbar/)
    expect(src).not.toMatch(/nav-links-scroll/)
  })

  it('Settings and Operations render their tabs through the primitive', () => {
    for (const rel of ['views/Settings.vue', 'views/Operations.vue']) {
      const src = read(rel)
      expect(src).toMatch(/import OverflowTabs from/)
      expect(src).toMatch(/<OverflowTabs/)
    }
  })

  it('NavBar consumes the shared fit rule instead of a second copy of it', () => {
    // The failure this catches is the one a green source-text suite hides: a
    // helper that is written, imported and then never called.
    const src = read('components/NavBar.vue')
    expect(src).toMatch(/from '\.\.\/utils\/overflowFit'/)
    expect(src).toMatch(/from '\.\.\/utils\/navLinks'/)
    expect(src).toMatch(/computeInlineCount\(\{/)
    expect(src).toMatch(/buildNavLinks\(\{/)
  })

  it('OverflowTabs shares that rule rather than keeping its own', () => {
    const src = read('components/OverflowTabs.vue')
    expect(src).toMatch(/from '\.\.\/utils\/overflowFit'/)
    expect(src).toMatch(/computeInlineCount\(\{/)
  })

  it('every collapsible strip measures a hidden mirror row', () => {
    // Without the mirror, a link living in the overflow menu has no measurable
    // width, so the strip can never decide to bring it back inline on a resize.
    for (const rel of ['components/NavBar.vue', 'components/OverflowTabs.vue']) {
      const src = read(rel)
      expect(src).toMatch(/visibility: hidden/)
      expect(src).toMatch(/ResizeObserver/)
      expect(src).toMatch(/document\.fonts/)
    }
  })

  // The nine `overflow-x-auto` candidates the 2026-07-31 audit listed on #1925.
  // Scoped to that list on purpose: a repo-wide sweep would fail on a dozen
  // containers nobody has looked at, and passing it would mean either marking
  // them with a verdict this issue never reached or allowlisting them, which
  // records "untriaged" as if it were a decision.
  const AUDIT_CANDIDATES = [
    'components/DashboardPanel.vue',
    'components/FileSharingPanel.vue',
    'components/LoopsPanel.vue',
    'components/NeverminedPanel.vue',
    'components/SharingPanel.vue',
    'components/process/RoleMatrix.vue',
    'components/reports/ReportTable.vue',
    'components/settings/McpKeysTab.vue',
    'views/enterprise/Audit.vue',
  ]

  it.each(AUDIT_CANDIDATES)('%s carries a written triage verdict on every scroll container', (rel) => {
    const lines = read(rel).split('\n')
    const hits = lines
      .map((line, i) => (line.includes('overflow-x-auto') ? i : -1))
      .filter((i) => i >= 0)
    // These files were named BECAUSE they hold one; a file with none means the
    // list has drifted and this guard is now checking nothing.
    expect(hits.length).toBeGreaterThan(0)
    const untriaged = hits.filter(
      (i) => !/#1925 triage/.test(lines.slice(Math.max(0, i - 12), i).join('\n'))
    )
    expect(untriaged.map((i) => `${rel}:${i + 1}`)).toEqual([])
  })

  it('records a verdict on the audit\'s advisory two-pill strip', () => {
    // Not a defect to fix — a segmented control is a different primitive — but
    // an unexplained divergence gets re-flagged by the next audit.
    const src = read('views/enterprise/Audit.vue')
    expect(src).toMatch(/#1925 triage[\s\S]{0,400}SEGMENTED\s+CONTROL/)
  })
})
