/**
 * #2540 — the scanline reveal is for CHART loading only.
 *
 * Operator ruling 2026-09-06 (Workspace review of the v0.9.5 design): the
 * horizontal loading beam is the chart-loading motion and nothing else; pages,
 * panels, lists and message threads load with skeleton placeholders keyed on
 * "no data yet". This amends design-system principle 12 and reverses #2163's
 * first-load criterion, which had wrapped the Workspace's stage, thread and
 * briefing in `ScanlineReveal`.
 *
 * Three things are pinned, in the shape of `workspaceRoomsGate.spec.js`
 * (there is no component-mount harness here — a rule inside an SFC is a rule
 * only a source guard can reach):
 *
 *   1. WHO may import the primitive — an ALLOWLIST, not "the three portal
 *      files no longer do". The review of this change found two non-chart
 *      consumers the issue never named (`LibrarySkillsSection`, a skills LIST;
 *      `FinishSetupCard`, a JSON preview). A guard that only asserted chart
 *      consumers still import it could not catch the next non-chart adoption;
 *      an exact set can. Those two are recorded on #1921's sweep as holdovers
 *      and listed here as such — converting one shrinks HOLDOVERS, never
 *      grows CHART.
 *   2. HOW the three Workspace zones load now: a `PortalSkeleton` keyed on a
 *      VERDICT (`stage.state`, `!historyLoaded`, `zone.state`) — never a bare
 *      `<x>.loading` path, which the #1927 ratchet counts — inside a wrapper
 *      that owns the footprint for both faces (principle 4).
 *   3. That the rule lives in the DOC, not in a skill: `/audit-design-system`
 *      reads `docs/memory/design-system.md` on its next run, so the amended
 *      principle 12 must be in the doc's own words.
 */
import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync, statSync } from 'fs'
import { fileURLToPath } from 'url'
import { join, relative } from 'path'
import { stripComments } from './helpers/stripComments'

const SRC = fileURLToPath(new URL('../../src', import.meta.url))
const DOCS = fileURLToPath(new URL('../../../../docs/memory', import.meta.url))

const read = (rel) => readFileSync(join(SRC, rel), 'utf8')
const code = (rel) => stripComments(read(rel))

function walkVue(dir, out = []) {
  for (const name of readdirSync(dir)) {
    if (name === 'node_modules' || name.startsWith('.')) continue
    const p = join(dir, name)
    if (statSync(p).isDirectory()) walkVue(p, out)
    else if (name.endsWith('.vue')) out.push(p)
  }
  return out
}

// The chart consumers — the surfaces the ruling names, and only those.
const CHART = [
  'components/AgentTile.vue',            // grid tile chart zones (ent#245)
  'components/DashboardPanel.vue',       // agent-defined dashboard charts (ent#253)
  'components/tiles/ExecutionsTile.vue', // the Executions grid tile (ent#449)
]
// Non-chart consumers that pre-date the ruling, re-pointed to #1921's sweep
// (skeletons, not scanlines). Shrinking this list is the sweep; growing it is
// a regression.
const HOLDOVERS = [
  'components/LibrarySkillsSection.vue',
  'components/onboarding/FinishSetupCard.vue',
]

describe('#2540 — the scanline primitive is imported by chart surfaces only', () => {
  it('has exactly the allowlisted importers', () => {
    const importers = walkVue(SRC)
      .filter((p) => /import\s+ScanlineReveal\s+from/.test(stripComments(readFileSync(p, 'utf8'))))
      .map((p) => relative(SRC, p).split('\\').join('/'))
      .sort()
    expect(importers).toEqual([...CHART, ...HOLDOVERS].sort())
  })

  it('no Workspace surface imports it', () => {
    for (const rel of ['views/Portal.vue', 'components/portal/PortalConversation.vue',
                       'components/portal/PortalBriefing.vue']) {
      expect(code(rel), rel).not.toMatch(/import\s+ScanlineReveal/)
      expect(code(rel), rel).not.toContain('<ScanlineReveal')
    }
  })
})

describe('#2540 — the three Workspace zones load with a skeleton keyed on a verdict', () => {
  it('the stage: a stage skeleton ahead of the branch chain, keyed on stage.state', () => {
    const src = code('views/Portal.vue')
    expect(src).toContain(`<PortalSkeleton v-else-if="stage.state === 'loading'" variant="stage" />`)
    // The chain is the placeholder's v-else, so no terminal arm can render
    // under a placeholder (the ent#253 lesson).
    const at = src.indexOf(`variant="stage"`)
    expect(src.slice(at, at + 200)).toContain('<template v-else>')
    // A bare `stage.loading` gate is exactly what the #1927 ratchet counts.
    expect(src).not.toContain('v-if="stage.loading"')
    expect(src).not.toContain('v-else-if="stage.loading"')
  })

  it('the thread: a thread skeleton keyed on the history VERDICT, inside the footprint wrapper', () => {
    const src = code('components/portal/PortalConversation.vue')
    const wrapAt = src.indexOf('<div class="max-w-4xl mx-auto min-h-[10rem]">')
    expect(wrapAt, 'the wrapper must own the footprint for both faces').toBeGreaterThan(-1)
    const zone = src.slice(wrapAt, wrapAt + 200)
    expect(zone).toContain('<PortalSkeleton v-if="!historyLoaded" variant="thread" />')
    expect(zone).toContain('<div v-else class="space-y-6">')
    // Never the in-flight flag: the adoption path re-runs loadThread with the
    // transcript on screen (#2163's own rule, kept).
    expect(src).not.toContain('<PortalSkeleton v-if="loadingHistory"')
  })

  it('the briefing: a briefing skeleton keyed on zone.state, inside the footprint wrapper', () => {
    const src = code('components/portal/PortalBriefing.vue')
    const wrapAt = src.indexOf('<div class="mt-1.5 mx-auto max-w-2xl min-h-[6.5rem]">')
    expect(wrapAt, 'the wrapper must own the footprint for both faces').toBeGreaterThan(-1)
    const zone = src.slice(wrapAt, wrapAt + 160)
    expect(zone).toContain(`<PortalSkeleton v-if="zone.state === 'pending'" variant="briefing" />`)
    expect(zone).toContain('<template v-else>')
    expect(src).not.toContain('v-if="zone.loading"')
    // The Work tab's "See what you can ask" scrolls here (ent#474).
    expect(src).toContain('id="portal-briefing"')
  })

  it('the skeleton recipe carries its own rules', () => {
    const src = read('components/portal/PortalSkeleton.vue')
    for (const variant of ['stage', 'thread', 'briefing']) {
      expect(src).toContain(`variant === '${variant}'`)
      expect(src).toContain(`data-testid="portal-skeleton-${variant}"`)
    }
    // Static under prefers-reduced-motion (§6); busy + one sr-only line.
    expect(src).toContain('animate-pulse motion-reduce:animate-none')
    expect(src).not.toMatch(/import\s+ScanlineReveal/)
    expect((src.match(/aria-busy/g) || []).length).toBeGreaterThanOrEqual(3)
    expect((src.match(/sr-only/g) || []).length).toBeGreaterThanOrEqual(3)
  })
})

describe('#2540 — the rule lives in the design-system doc, not in a skill', () => {
  const doc = readFileSync(join(DOCS, 'design-system.md'), 'utf8')
  const contract = readFileSync(join(DOCS, 'design-system-contract.md'), 'utf8')

  it('principle 12 says the scanline is for chart surfaces and everything else is a skeleton', () => {
    const p12 = doc.match(/^12\. .*$/m)?.[0] || ''
    expect(p12).toMatch(/chart/i)
    expect(p12).toMatch(/skeleton/i)
    expect(p12).not.toMatch(/no bespoke spinners or skeletons/i)
    const c12 = contract.match(/^12\. .*$/m)?.[0] || ''
    expect(c12).toMatch(/chart/i)
    expect(c12).toMatch(/skeleton/i)
  })

  it('the do/don\'t table keeps the chart row and adds the page/list row', () => {
    expect(doc).toContain('| A new skeleton/spinner for a loading chart | The scanline primitive, keyed off store state |')
    expect(doc).toMatch(/\| A scanline beam over a page, list or thread \|/)
  })

  it('no longer claims the Workspace zones as scanline adopters', () => {
    expect(doc).not.toMatch(/the Workspace's three zones \(the stage/)
    expect(contract).not.toMatch(/No new spinners or skeletons\./)
  })
})
