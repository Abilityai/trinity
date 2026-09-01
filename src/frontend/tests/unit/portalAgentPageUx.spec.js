/**
 * #2161 / #2169 — Workspace agent page UX repairs.
 *
 * #2169 amended the containment block below rather than deleting from it. Two
 * of its guards pinned rules that #2169 deliberately reverses — asks first in
 * DOM order, and a two-column grid conditional on `asks.length` — and a deleted
 * guard leaves no record that a rule was retired on purpose. They now assert the
 * replacement rule and name the old one as the defect, the same way #2161 itself
 * rewrote `workspaceRoomsGate.spec.js` F24. The chart guards and the
 * scroll-region guard are untouched and must keep passing.
 *
 * Two of the four #2161 defects are load-bearing enough to pin, and they fail in
 * opposite ways:
 *
 *   1. **"Start a chat" did nothing.** Not a broken handler — the handler ran
 *      fine and prepared the chat behind an agent page that never yielded the
 *      stage, because the escape guard listed the route params it knew about and
 *      `/workspace/a/:agentName` was added after it was written. That is the
 *      third time the list went stale (#2128 found `roomId` missing from guards
 *      written for `sessionId`), so what is pinned here is not "agentName is in
 *      the list" — it is that there is no list. `shouldEscapeStage` asks about
 *      route SHAPE and therefore fails closed, and a fourth stage route needs no
 *      edit to stay fixed.
 *
 *   2. **The chart legend must not be translated by renaming buckets.** The
 *      `buckets` array entries are the KEYS the chart indexes `by_type` with, so
 *      substituting client-facing wording into that array makes every lookup
 *      miss and renders an empty chart — a silent failure that looks like "this
 *      agent did nothing this week". Translation belongs in a separate `labels`
 *      prop, and these tests keep the two roles apart.
 *
 * There is no component-mount harness in this project (no `@vue/test-utils`), so
 * the testable half is the pure functions, and the parts no unit test can reach
 * (which call sites use the helper, which prop the chart stacks by) are covered
 * by source-structure guards — the `workspaceRoomsGate.spec.js` shape, comments
 * stripped first so prose about the rule is not scanned as code.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'

import { stripComments } from './helpers/stripComments'

import {
  shouldEscapeStage,
  PORTAL_BUCKET_LABELS,
  WORKSPACE_ROOT,
} from '@/components/portal/portalUtils'
import {
  BUCKET_COLORS,
  bucketsForChart,
  hasChartActivity,
} from '@/utils/executionBuckets'

const PORTAL = fileURLToPath(new URL('../../src/views/Portal.vue', import.meta.url))
const PAGE = fileURLToPath(new URL('../../src/components/portal/PortalAgentPage.vue', import.meta.url))
const CHART = fileURLToPath(new URL('../../src/components/StackedBarChart.vue', import.meta.url))

// Comments are stripped so prose about a rule isn't scanned as code. Shared,
// because this was about to become its third copy — and the shared version
// loops the block passes to a fixpoint, which the copies did not.
const portalSource = () => stripComments(readFileSync(PORTAL, 'utf8'))
const pageSource = () => stripComments(readFileSync(PAGE, 'utf8'))
const chartSource = () => stripComments(readFileSync(CHART, 'utf8'))

// The Overview tab's template block only. Every `<template v-else-if="tab ===
// '...'">` sibling — and the tab-agnostic loading skeleton above them all —
// lives in the same file, so a file-wide match answers a question about the
// wrong markup: the skeleton and the Overview row deliberately carry the same
// grid classes, which is exactly why one cannot stand in for the other.
const overviewBlock = (src) => {
  const start = src.indexOf("tab === 'overview'")
  expect(start).toBeGreaterThan(-1)
  const end = src.indexOf("tab === '", start + 1)
  return src.slice(start, end === -1 ? undefined : end)
}

// ---------------------------------------------------------------------------
// 1. The stage escape
// ---------------------------------------------------------------------------

describe('shouldEscapeStage', () => {
  it('escapes the agent page — the #2161 defect itself', () => {
    expect(shouldEscapeStage('/workspace/a/scribe')).toBe(true)
  })

  it('escapes the routes the two earlier misses were about', () => {
    expect(shouldEscapeStage('/workspace/r/room-1')).toBe(true)   // #2128
    expect(shouldEscapeStage('/workspace/c/sess-1')).toBe(true)   // the original
  })

  it('escapes a stage route nobody has written yet', () => {
    // The actual regression guard. A param-enumerating predicate answers false
    // here, which is exactly how this bug shipped twice: the guard keeps working
    // only until someone adds a route, and nothing fails when they do.
    expect(shouldEscapeStage('/workspace/x/whatever')).toBe(true)
    expect(shouldEscapeStage('/workspace/deep/nested/thing')).toBe(true)
  })

  it('does not push when already at the workspace root', () => {
    // A redundant push is not harmless: it is a history entry, so Back would
    // walk through duplicates of the same screen.
    expect(shouldEscapeStage(WORKSPACE_ROOT)).toBe(false)
    expect(shouldEscapeStage('/workspace/')).toBe(false)
  })

  it('does nothing without a route', () => {
    expect(shouldEscapeStage('')).toBe(false)
    expect(shouldEscapeStage(undefined)).toBe(false)
  })

  // A stage can be named by the query as well as the path, and that half is
  // what leaks ACROSS sessions: `?agent=` is re-read by the next bootstrap(),
  // so signing out at /workspace?agent=X shows the NEXT person to sign in on
  // this browser "You don't have access to X".
  it('escapes a stage named by the query at the workspace root', () => {
    expect(shouldEscapeStage('/workspace', { agent: 'acme-billing' })).toBe(true)
    expect(shouldEscapeStage('/workspace', { new: '1' })).toBe(true)
  })

  it('ignores an empty or absent query', () => {
    expect(shouldEscapeStage('/workspace', {})).toBe(false)
    expect(shouldEscapeStage('/workspace', { agent: '' })).toBe(false)
    expect(shouldEscapeStage('/workspace', undefined)).toBe(false)
  })

  it('still escapes a path stage regardless of the query', () => {
    expect(shouldEscapeStage('/workspace/a/scribe', {})).toBe(true)
  })
})

describe('Portal.vue wiring', () => {
  it('routes every stage exit through the shared helper', () => {
    const src = portalSource()
    expect(src).toContain('shouldEscapeStage')
    // Both live callers. `newChatWithAgent` is the "Start a chat" path; the
    // sign-out one carried a room id (#2128) and then an agent name into the
    // next session's address bar.
    expect(src).toMatch(/function newChatWithAgent[\s\S]*?escapeStage\(\)/)
    expect(src).toMatch(/function onSignOut[\s\S]*?escapeStage\(\)/)
  })

  it('passes the query to the escape, not just the path', () => {
    // Dropping the second argument silently reinstates the cross-session
    // `?agent=` leak while every path test still passes.
    expect(portalSource()).toMatch(/shouldEscapeStage\(route\.path,\s*route\.query\)/)
  })

  it('has no hand-rolled param-enumerating escape left', () => {
    // The shape that went stale three times: `route.params.sessionId ||
    // route.params.roomId` as a stand-in for "am I on a stage route?".
    expect(portalSource()).not.toMatch(/route\.params\.sessionId\s*\|\|\s*route\.params\.roomId/)
  })

  it('does not carry the dead startBlankChat function', () => {
    // It had zero callers, and "fix the guard in startBlankChat" was the
    // plausible-looking fix that would have changed nothing.
    expect(portalSource()).not.toContain('function startBlankChat')
  })
})

// ---------------------------------------------------------------------------
// 2. The chart
// ---------------------------------------------------------------------------

describe('bucketsForChart', () => {
  it('prefers the backend order', () => {
    const stats = {
      buckets: ['Chat/Tasks', 'Scheduled'],
      by_type: [{ bucket: 'Scheduled', total: 3 }, { bucket: 'Chat/Tasks', total: 1 }],
    }
    // Not the by_type order — the backend list is canonical (_BUCKET_ORDER).
    expect(bucketsForChart(stats)).toEqual(['Chat/Tasks', 'Scheduled'])
  })

  it('falls back to by_type for a payload that predates the field', () => {
    const stats = { by_type: [{ bucket: 'Scheduled', total: 3 }] }
    expect(bucketsForChart(stats)).toEqual(['Scheduled'])
  })

  it('survives an empty or malformed envelope', () => {
    expect(bucketsForChart({})).toEqual([])
    expect(bucketsForChart(null)).toEqual([])
    expect(bucketsForChart({ by_type: [{ total: 2 }] })).toEqual([])
  })
})

describe('hasChartActivity', () => {
  it('is false for a gap-filled but empty window', () => {
    // The timeline is always full-length — every day is present with total 0 —
    // so a length check would call an idle agent "active" and draw a flat frame.
    expect(hasChartActivity({ timeline: [{ date: '2026-08-01', total: 0 }] })).toBe(false)
  })

  it('is true as soon as one day has work', () => {
    expect(hasChartActivity({
      timeline: [{ date: '2026-08-01', total: 0 }, { date: '2026-08-02', total: 2 }],
    })).toBe(true)
  })

  it('is false for a missing or unavailable envelope', () => {
    expect(hasChartActivity({})).toBe(false)
    expect(hasChartActivity(null)).toBe(false)
  })
})

describe('bucket labels', () => {
  it('translates only the internal-sounding buckets', () => {
    expect(PORTAL_BUCKET_LABELS['MCP']).toBe('Tool call')
    expect(PORTAL_BUCKET_LABELS['Chat/Tasks']).toBe('Chat')
  })

  it('keys the translation off names the palette knows', () => {
    // A typo here is silent: the chart falls back to the raw bucket name, so
    // the legend reads "MCP" on a client's page and nothing errors.
    for (const key of Object.keys(PORTAL_BUCKET_LABELS)) {
      expect(BUCKET_COLORS).toHaveProperty(key)
    }
  })

  it('is presentation only — the page stacks by the untranslated buckets', () => {
    const src = pageSource()
    // If the labels were passed as `:buckets`, every `by_type[bucket]` lookup
    // would miss and the chart would render blank.
    expect(src).toMatch(/:buckets="chartBuckets"/)
    expect(src).toMatch(/:labels="PORTAL_BUCKET_LABELS"/)
    expect(src).not.toMatch(/:buckets="PORTAL_BUCKET_LABELS"/)
  })
})

describe('StackedBarChart labels prop', () => {
  it('defaults to the bucket name so the operator surface is unaffected', () => {
    const src = chartSource()
    expect(src).toMatch(/labels:\s*\{\s*type:\s*Object,\s*default:\s*\(\)\s*=>\s*\(\{\}\)/)
    expect(src).toMatch(/props\.labels\[b\]\s*\|\|\s*b/)
  })

  it('still indexes by_type with the raw bucket key', () => {
    // The lookup the labels must never touch.
    expect(chartSource()).toMatch(/by_type\?\.\[b\]/)
  })
})

// ---------------------------------------------------------------------------
// 3. Containment — the layout rules that keep the Overview readable
// ---------------------------------------------------------------------------

describe('Overview containment', () => {
  it('keeps asks on the Overview rather than a new tab', () => {
    // #2449 repoint: the marker moved, the property did not. The page used to
    // render asks from TWO sources — this section off `page.asks`, and
    // `<PortalAsks>` off the `/asks` store — so every ask appeared twice. The
    // duplicate is gone; `<PortalAsks>` is the rendering, and it is still on the
    // Overview rather than behind a sixth tab.
    const src = pageSource()
    expect(src).toContain('<PortalAsks')
    expect(src).not.toMatch(/id:\s*'asks'/)
  })

  it('does not advertise an asks section for an agent with nothing waiting', () => {
    // Survives the #2449 de-duplication: the surviving mount is guarded on the
    // store list being non-empty, so an agent with nothing waiting renders no
    // section at all — same rule, one source.
    expect(pageSource()).toMatch(
      /v-if="store\.asksForAgent\(agentName\)\.length"[\s\S]{0,160}<PortalAsks/,
    )
  })

  it('does not nest a scroll region inside the asks rendering', () => {
    // #2101 on this surface: the page has one scroll axis, and a pane that
    // scrolls inside a page that scrolls traps the gesture on touch. After
    // #2449 the containment belongs to `PortalAsks`, so assert it there rather
    // than against a section this file no longer owns.
    const ASKS = fileURLToPath(
      new URL('../../src/components/portal/PortalAsks.vue', import.meta.url),
    )
    expect(stripComments(readFileSync(ASKS, 'utf8'))).not.toMatch(
      /overflow-y-auto|overflow-auto/,
    )
  })

  // RETIRED BY #2449 — recorded rather than silently dropped, the way this file
  // records #2161's own reversal above.
  //
  // Three tests lived here that pinned the DELETED section:
  //
  //   'orders the Overview activity -> recent work -> asks'  (#2169)
  //   'puts asks outside the top row, not in it as a third column'  (#2169)
  //   'renders the ask count as a floor when the service truncated it'
  //
  // The first two encoded #2169's instructed rule that asks sit BELOW the top
  // row. That rule was already void before this change: ent#428 added
  // `<PortalAsks>` immediately under the header, ABOVE the stats strip, so the
  // page led with asks and then repeated them lower down. The tests kept
  // passing only because the lower copy still existed. Deleting the duplicate
  // makes the page do one thing; it does not decide where asks belong, and if
  // the answer is still "below the top row" the fix is to move the surviving
  // mount, not to restore a second one.
  //
  // The third pinned `ASKS_CAP = 20`, mirroring the removed reader's server-side
  // `MAX_ASKS`. The surviving list is fetched at `limit=200`, so a "20+" floor
  // would now be a wrong number rather than a rounded one. The badge is a plain
  // count of the pending list, pinned in `portalAskSingleSource.spec.js`.

  it('lets the activity chart fill its column', () => {
    // The chart carried `max-w-2xl` when it sat alone above a full-width row.
    // Inside a half-width column that cap is dead weight below ~1656px, and the
    // section it capped is now one of two equal columns.
    const src = pageSource()
    expect(src).toContain('Activity · last')
    expect(src).not.toMatch(/max-w-2xl/)
  })

  it('does not advertise an asks section for an agent with nothing waiting', () => {
    // #2449 repoint: the guard moved with the rendering. The surviving mount is
    // conditioned on the store list being non-empty, so an agent with nothing
    // waiting renders no section — same rule, one source instead of two.
    expect(pageSource()).toMatch(
      /v-if="store\.asksForAgent\(agentName\)\.length"[\s\S]{0,160}<PortalAsks/,
    )
  })

  it('shapes the loading skeleton like the row it precedes', () => {
    // Layout stability (contract principles #4/#6): a one-column skeleton in
    // front of a two-column row reflows the page on every load.
    expect(pageSource()).toMatch(/loading && !page"[^>]*class="[^"]*\bgrid\b[^"]*\bxl:grid-cols-2\b/)
  })
})
