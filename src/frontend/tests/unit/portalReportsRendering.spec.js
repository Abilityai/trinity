/**
 * The Workspace Reports tab renders reports; it does not dump them (#2162).
 *
 * The tab shipped `<pre>{{ JSON.stringify(payload, null, 2) }}</pre>` to
 * external clients. That is a DISCLOSURE defect, not only an ugly one: the
 * payload is free-form agent-authored JSON, the same category as an
 * operator-queue ask's `context`, which `client_portal/agent_page.py` refuses to
 * expose at all because it has been a credential-leak surface before (canary
 * G-04). A typed renderer reads only the keys its hint declares, so routing the
 * tab through the shared set strictly narrows what crosses.
 *
 * This project has no component-mount harness (no `@vue/test-utils`), so the
 * behaviour lives in `portalReportsStore.spec.js` (store fetchers, mocked axios)
 * and `reportSummary.spec.js` (the pure summariser). What is left here is the
 * wiring no unit test can reach: which component the tab mounts, which props it
 * passes, and that the CI-pinned dispatch keys stayed where the pin reads them.
 *
 * Guards check the MECHANISM, not the spelling. Asserting that
 * `:fallback-component` appears at a call site would pass just as happily
 * against a prop that is declared and never used, and the whole point of that
 * prop is that a raw payload is unreachable on this surface.
 *
 * Comments are stripped before every scan — a comment explaining what must not
 * be written necessarily contains the offending string.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'

import { stripComments } from './helpers/stripComments'

const src = (p) => stripComments(readFileSync(fileURLToPath(new URL(p, import.meta.url)), 'utf8'))

const PAGE = '../../src/components/portal/PortalAgentPage.vue'
const RENDERER = '../../src/components/reports/ReportRenderer.vue'
const SUMMARY = '../../src/components/reports/ReportSummary.vue'

/** The Reports tab's template block, isolated from the rest of the page. */
function reportsBlock() {
  const page = src(PAGE)
  const start = page.indexOf("tab === 'reports'")
  expect(start).toBeGreaterThan(-1)
  const end = page.indexOf("tab === 'files'", start)
  expect(end).toBeGreaterThan(start)
  return page.slice(start, end)
}

// ---------------------------------------------------------------------------
// 1. The dump is gone, and the shared renderer is what replaced it
// ---------------------------------------------------------------------------

describe('the Reports tab', () => {
  it('contains no raw payload dump', () => {
    const block = reportsBlock()
    expect(block).not.toContain('<pre')
    expect(block).not.toContain('pretty(')
    expect(block).not.toContain('JSON.stringify')
  })

  it('has no `pretty` helper left behind anywhere on the page', () => {
    // Its only call site was the dump. A dead JSON serialiser sitting in this
    // file is an invitation to wire it back into a template.
    expect(src(PAGE)).not.toContain('const pretty')
  })

  it('mounts the SHARED renderer rather than a portal-local fork', () => {
    const page = src(PAGE)
    expect(page).toContain("from '@/components/reports/ReportRenderer.vue'")
    expect(reportsBlock()).toContain('<ReportRenderer')
  })

  it('forwards the fields the dispatch needs', () => {
    const block = reportsBlock()
    for (const attr of [':report-type=', ':display-hint=', ':payload=']) {
      expect(block).toContain(attr)
    }
  })

  it('overrides the fallback so a client never gets a raw payload', () => {
    // AC #2: stricter than the operator side, deliberately. The operator
    // surfaces pass nothing here and keep the JSON viewer.
    expect(reportsBlock()).toContain(':fallback-component="ReportSummary"')
    expect(src(PAGE)).toContain("from '@/components/reports/ReportSummary.vue'")
  })

  it('passes paging handles only when the server actually windowed', () => {
    // `row_meta` present is the only signal a payload was paged. Passing a
    // load-more callback unconditionally would put a footer under a bounded
    // document that can never satisfy it.
    const block = reportsBlock()
    expect(block).toContain(':meta="reportRowMeta[r.id]"')
    expect(block).toMatch(/:load-more="reportRowMeta\[r\.id\] \?/)
  })

  it('distinguishes loading, empty and failed (contract #15)', () => {
    const block = reportsBlock()
    // Empty copy gates on the LOADED FLAG, never on list length — otherwise a
    // failed fetch renders "this agent hasn't published any reports".
    expect(block).toMatch(/v-else-if="!reports\.length"/)
    expect(block).toContain('reportsLoaded')
    expect(block).toContain('<LoadFailed')
    expect(block).toContain('<InlineError')
  })

  it('never renders a failed fetch through the renderer', () => {
    // The old catch wrote `{error: …}` into the payload map, which the renderer
    // would have presented AS a report — worse than the bug being fixed.
    expect(src(PAGE)).not.toContain("[id]: { error:")
  })

  it('does not introduce a second scroll axis inside the page', () => {
    // The page has ONE scroll axis (#2101). Growth is bounded by the row window
    // plus an explicit "Load more", not by a nested scroll region.
    expect(reportsBlock()).not.toMatch(/overflow-y-(auto|scroll)/)
    expect(reportsBlock()).not.toMatch(/\bmax-h-/)
  })

  it('leaves the Overview block alone (#2169 owns it)', () => {
    const page = src(PAGE)
    expect(page).toContain("tab === 'overview'")
    expect(page).toContain('StackedBarChart')
  })
})

// ---------------------------------------------------------------------------
// 2. The renderer wiring — mechanism, not spelling
// ---------------------------------------------------------------------------

describe('ReportRenderer', () => {
  it('keeps the JSON viewer as the DEFAULT fallback (operator unchanged)', () => {
    // The operator surfaces are debugging an agent's own output, where a raw
    // dump is a feature. AC #2 makes the portal stricter than them, not all
    // three stricter together — a global summary erases the split it asks for.
    const renderer = src(RENDERER)
    expect(renderer).toMatch(/json:\s*ReportJson/)
    expect(renderer).toMatch(/fallbackComponent:\s*\{[^}]*default:\s*null/)
  })

  it('actually renders the override rather than only declaring the prop', () => {
    const renderer = src(RENDERER)
    // Consumed in the dispatch itself — a declared-but-unused prop would pass a
    // call-site scan while the client still got a raw dump.
    expect(renderer).toMatch(/props\.fallbackComponent \|\| ReportJson/)
    expect(renderer).toContain('v-bind="fallbackProps"')
  })

  it('does not import the client-facing summary into the shared renderer', () => {
    // It arrives as a prop from the one surface that wants it; importing it
    // here is how a "portal-only" component quietly becomes global again.
    expect(src(RENDERER)).not.toContain('ReportSummary.vue')
  })

  it('tells the fallback whether it was reached by shape MISMATCH', () => {
    // `json` is a valid agent-chosen display_hint, so "the shape was wrong" and
    // "the agent asked for this" must not read the same to a reader.
    expect(src(RENDERER)).toMatch(/mismatch:\s*true/)
    expect(src(RENDERER)).toMatch(/fallback:\s*resolved\.value\.mismatch/)
  })

  it('routes an agent-chosen `json` hint through the override too', () => {
    // Not only the mismatch path: `display_hint: "json"` is a valid value in the
    // MCP tool's enum, so an override that covered only mismatches would let an
    // agent put a raw dump in front of a client by asking for one.
    expect(src(RENDERER)).toMatch(/hint === 'json'\) return props\.fallbackComponent/)
  })

  it('still reads all five CI-pinned payload keys IN THIS FILE', () => {
    // Belt for `tests/unit/test_1535_report_prompt_guidance.py`, which regexes
    // `payload.X` out of this exact file and asserts the five are present. The
    // tempting refactor — extract `shapeOk` into a shared module — empties that
    // set and breaks the cross-surface drift guard.
    const keys = new Set(
      [...src(RENDERER).matchAll(/payload\.([a-zA-Z_]+)/g)].map((m) => m[1]),
    )
    for (const key of ['columns', 'rows', 'tiles', 'events', 'markdown']) {
      expect(keys).toContain(key)
    }
  })
})

// ---------------------------------------------------------------------------
// 3. Tokens and themes (AC #4)
// ---------------------------------------------------------------------------

// Mirrors `scan-raw-colors.mjs`'s own list. Gray is deliberately EXCLUDED:
// `tailwind.config.js` keeps gray as the raw palette and defines semantic
// families only for status/state/brand/accent/action, so there is no neutral
// token and the design contract's own ink rules are written in gray classes.
// The meaningful gate is therefore zero NON-gray raw classes and zero hexes.
const NONGRAY_FAMILIES = [
  'red', 'orange', 'amber', 'yellow', 'lime', 'green', 'emerald', 'teal',
  'cyan', 'sky', 'blue', 'indigo', 'violet', 'purple', 'fuchsia', 'pink',
  'rose', 'slate', 'zinc', 'neutral', 'stone',
]
const NONGRAY_RE = new RegExp(`\\b(?:${NONGRAY_FAMILIES.join('|')})-(?:50|\\d{3})\\b`, 'g')
const HEX_RE = /#[0-9a-fA-F]{3,8}\b/g

describe('token discipline', () => {
  it('ReportSummary.vue exposes no raw-payload escape hatch', () => {
    // Portal-only, so there is nothing to disclose behind. A `<details>Show raw
    // JSON</details>` here would hand the client the dump this issue removes.
    const content = src(SUMMARY)
    expect(content).not.toContain('ReportJson.vue')
    expect(content).not.toContain('<details')
    expect(content).not.toContain('JSON.stringify')
  })

  it.each([
    ['ReportSummary.vue', SUMMARY],
    ['ReportRenderer.vue', RENDERER],
  ])('%s uses no raw non-gray palette class and no hex', (_name, path) => {
    const content = src(path)
    expect(content.match(NONGRAY_RE) || []).toEqual([])
    expect(content.match(HEX_RE) || []).toEqual([])
  })

  it('the Reports tab block uses no raw non-gray palette class and no hex', () => {
    const block = reportsBlock()
    expect(block.match(NONGRAY_RE) || []).toEqual([])
    expect(block.match(HEX_RE) || []).toEqual([])
  })

  it('every gray in ReportSummary.vue pairs with a dark variant on the ink ladder', () => {
    // AC #4. The contract: dark meta text is gray-300/400, gray-500 is the
    // floor and decoration-only. `check:tokens` will NOT catch a regression
    // here — its INK_LADDER_SWEPT list covers 13 files and includes neither
    // components/reports/ nor components/portal/.
    const content = src(SUMMARY)
    const lightMeta = [...content.matchAll(/(?<!dark:)\btext-gray-500\b/g)]
    expect(lightMeta.length).toBeGreaterThan(0)
    // Each occurrence of the light tertiary must be followed by its dark pair
    // in the same class attribute.
    for (const m of lightMeta) {
      const tail = content.slice(m.index, m.index + 120)
      expect(tail).toMatch(/dark:text-gray-[34]00/)
    }
    expect(content).not.toMatch(/dark:text-gray-500/)
  })

  it.each([
    ['ReportTable.vue', '../../src/components/reports/ReportTable.vue'],
    ['ReportKpiTiles.vue', '../../src/components/reports/ReportKpiTiles.vue'],
  ])('%s no longer ships bare gray-500 meta text into dark mode', (_name, path) => {
    // These are REUSED verbatim on the client-facing surface, so "we didn't
    // edit those files" does not make AC #4 free — it makes their meta text
    // render visibly darker than everything around it, in the theme the AC
    // names. `ReportTimeline.vue` already paired correctly, which is what
    // proves these two were oversights rather than a house style.
    const content = src(path)
    for (const m of content.matchAll(/\btext-gray-500\b/g)) {
      const tail = content.slice(m.index, m.index + 120)
      expect(tail).toMatch(/dark:text-gray-[34]00/)
    }
  })

  it('ReportTimeline.vue uses the semantic token, not a raw blue', () => {
    const content = src('../../src/components/reports/ReportTimeline.vue')
    expect(content).toContain('bg-status-info-500')
    expect(content.match(NONGRAY_RE) || []).toEqual([])
  })
})
