import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  SUBSCRIPTION_SETTINGS_ROUTE,
  SUBSCRIPTION_TILE_MAX_ROWS,
  UTILIZATION_HIGH_PCT,
  UTILIZATION_WARM_PCT,
  barWidthPct,
  formatApproxCost,
  formatTokenCount,
  pressureHeadline,
  resetReading,
  resetText,
  showsReset,
  utilizationLevel,
  warnReason,
  windowReadings,
  rateLimitEventCount,
  rowTooltip,
  subscriptionPressureRows,
  subscriptionPressureTileState,
  subscriptionSeverity,
  subscriptionTileStamp,
  usageLine,
} from '@/utils/subscriptionPressureTile'

/**
 * ent#259 — the Subscription-pressure tile's decisions.
 *
 * vitest is node-environment here (pure modules, no mount harness), so this
 * file IS the tile's test coverage: anything decided inside the SFC is decided
 * where no test can see it. The rules below are the ones whose regression is
 * silent — a wrong number that still renders.
 */

const win = (o = {}) => ({ input_tokens: 0, output_tokens: 0, cost_usd: 0, message_count: 0, ...o })

function usage(o = {}) {
  return {
    window_5h: win(),
    window_7d: win(),
    agents: [],
    failure_events_24h: 0,
    failure_events_by_kind: {},
    rate_limited_now: false,
    source: 'observed',
    headroom: null,
    ...o,
  }
}

/** A provider snapshot recent enough for `headroomIsFresh` (≤ 1800s). */
function fresh(pct, o = {}) {
  return usage({
    source: 'anthropic',
    headroom: {
      five_hour: { utilization_pct: pct, resets_at: null, status: 'allowed' },
      seven_day: null,
      snapshot_age_seconds: 60,
      status: 'ok',
      ...o,
    },
  })
}

describe('rateLimitEventCount — 429s are not "all failures"', () => {
  it('counts only the rate_limit kind', () => {
    const u = usage({ failure_events_24h: 5, failure_events_by_kind: { rate_limit: 2, auth: 3 } })
    expect(rateLimitEventCount(u)).toBe(2)
  })

  it('never promotes auth failures to 429s', () => {
    const u = usage({ failure_events_24h: 4, failure_events_by_kind: { auth: 4 } })
    expect(rateLimitEventCount(u)).toBe(0)
  })

  it('never promotes pre-#471 unclassified rows to 429s', () => {
    // Migration 0040 exists because the table conflated the two; reporting the
    // total under a "429" label would undo that fix in the UI.
    const u = usage({ failure_events_24h: 3, failure_events_by_kind: { unknown: 3 } })
    expect(rateLimitEventCount(u)).toBe(0)
  })

  it('is 0 for an unavailable reading rather than throwing', () => {
    expect(rateLimitEventCount({ error: true })).toBe(0)
    expect(rateLimitEventCount(undefined)).toBe(0)
  })
})

describe('subscriptionSeverity', () => {
  it('crit only from the backend one-gate rate_limited_now', () => {
    expect(subscriptionSeverity(usage({ rate_limited_now: true }))).toBe('crit')
  })

  it('a 24h failure count alone is warn, never crit', () => {
    // The same rule pressureBadge pins for the per-agent chip: a historical
    // count does not claim "limited now".
    const u = usage({ failure_events_24h: 9, failure_events_by_kind: { rate_limit: 9 } })
    expect(subscriptionSeverity(u)).toBe('warn')
  })

  it('a failed usage read is unknown, never ok', () => {
    expect(subscriptionSeverity({ error: true })).toBe('unknown')
    expect(subscriptionSeverity(undefined)).toBe('unknown')
  })

  it('a clean subscription is ok', () => {
    expect(subscriptionSeverity(usage())).toBe('ok')
  })

  it('a window in the HIGH band is warn even with a spotless failure history', () => {
    // The only signal available BEFORE the first 429. Left as `ok`, a
    // subscription at 93% of its weekly limit ranks below one whose usage read
    // merely failed, and gets pushed into the overflow — backwards.
    const nearlyGone = usage({
      source: 'anthropic',
      headroom: {
        five_hour: { utilization_pct: 22 },
        seven_day: { utilization_pct: 93 },
        snapshot_age_seconds: 30,
        status: 'ok',
      },
    })
    expect(subscriptionSeverity(nearlyGone)).toBe('warn')
    expect(warnReason(nearlyGone)).toBe('utilization')
  })

  it('distinguishes "429s happened" from "near the limit"', () => {
    const withEvents = usage({ failure_events_24h: 2, failure_events_by_kind: { rate_limit: 2 } })
    expect(warnReason(withEvents)).toBe('events')
    expect(warnReason(usage())).toBeNull()
  })

  it('does not escalate a merely WARM window', () => {
    // 60-84% is worth colouring amber, not worth ranking as pressure.
    expect(subscriptionSeverity(fresh(70))).toBe('ok')
  })
})

describe('utilizationLevel — the colour band', () => {
  it('bands by how much of the limit is spent', () => {
    expect(utilizationLevel(0)).toBe('ok')
    expect(utilizationLevel(59)).toBe('ok')
    expect(utilizationLevel(UTILIZATION_WARM_PCT)).toBe('warm')
    expect(utilizationLevel(84)).toBe('warm')
    expect(utilizationLevel(UTILIZATION_HIGH_PCT)).toBe('high')
    expect(utilizationLevel(100)).toBe('high')
  })

  it('refuses to colour a non-reading', () => {
    // `null` means "no band" — a missing reading must not render as green,
    // which would claim headroom nobody measured.
    expect(utilizationLevel(null)).toBeNull()
    expect(utilizationLevel(undefined)).toBeNull()
    expect(utilizationLevel(Number.NaN)).toBeNull()
  })
})

describe('barWidthPct — geometry is clamped, the claim is not', () => {
  it('passes an in-range reading through', () => {
    expect(barWidthPct(0)).toBe(0)
    expect(barWidthPct(62)).toBe(62)
    expect(barWidthPct(100)).toBe(100)
  })

  it('clamps an overage above 100', () => {
    // An overage plan really can report >100%. An unclamped `width: 137%`
    // overruns the track and shoves the row's siblings out of a tile body that
    // is `overflow: hidden` — content gone, no trace.
    expect(barWidthPct(137)).toBe(100)
  })

  it('clamps a negative or unreadable value to empty', () => {
    expect(barWidthPct(-5)).toBe(0)
    expect(barWidthPct(Number.NaN)).toBe(0)
    expect(barWidthPct(undefined)).toBe(0)
  })

  it('reports the overage NUMBER even though the bar is capped', () => {
    const over = usage({
      source: 'anthropic',
      headroom: { five_hour: { utilization_pct: 137 }, seven_day: null, snapshot_age_seconds: 10, status: 'ok' },
    })
    const [w] = windowReadings(over)
    expect(w.pct).toBe(137)   // the claim — honest
    expect(w.fill).toBe(100)  // the geometry — bounded
    expect(w.level).toBe('high')
  })
})

describe('windowReadings — both limits, spend against each', () => {
  it('reports the 5h AND 7d limits with their own bands', () => {
    const u = usage({
      source: 'anthropic',
      headroom: {
        five_hour: { utilization_pct: 15, resets_at: '2026-08-19T21:00:00Z', status: 'allowed' },
        seven_day: { utilization_pct: 91, resets_at: '2026-08-26T05:00:00Z', status: 'allowed' },
        snapshot_age_seconds: 30,
        status: 'ok',
      },
    })
    expect(windowReadings(u)).toEqual([
      { label: '5h', pct: 15, fill: 15, level: 'ok', resetsAt: '2026-08-19T21:00:00Z' },
      { label: '7d', pct: 91, fill: 91, level: 'high', resetsAt: '2026-08-26T05:00:00Z' },
    ])
  })

  it('rounds to whole percent — the provider reports no finer', () => {
    expect(windowReadings(fresh(62.4))[0].pct).toBe(62)
  })

  it('omits a window the provider did not report rather than showing 0%', () => {
    // An absent window is unknown, not empty; "0% used" would be a claim.
    const r = windowReadings(fresh(15))
    expect(r).toHaveLength(1)
    expect(r[0].label).toBe('5h')
  })

  it('is null when the reading is observed-only', () => {
    // #471's contract: the DB-derived arm is always populated, but it carries
    // consumption — not a utilization denominator. Inventing one is exactly
    // the fake precision the AC forbids.
    expect(windowReadings(usage({ window_5h: win({ cost_usd: 5, output_tokens: 9000 }) }))).toBeNull()
  })

  it('is null when the provider snapshot is stale', () => {
    const stale = fresh(80)
    stale.headroom.snapshot_age_seconds = 7200 // 2h — past the 30 min gate
    expect(windowReadings(stale)).toBeNull()
  })

  it('is null for an unavailable reading', () => {
    expect(windowReadings({ error: true })).toBeNull()
    expect(windowReadings(undefined)).toBeNull()
  })
})

describe('pressureHeadline — the qualitative fallback', () => {
  it('shows NO percentage when the reading is observed-only', () => {
    const u = usage({ window_5h: win({ cost_usd: 5, output_tokens: 9000 }) })
    expect(pressureHeadline(u)).toBe('ok')
    expect(pressureHeadline(u)).not.toContain('%')
  })

  it('falls back to the qualitative state when limited without a fresh reading', () => {
    expect(pressureHeadline(usage({ rate_limited_now: true }))).toBe('rate-limited')
  })

  it('reports 429 volume when there is no fresh reading', () => {
    const u = usage({ failure_events_24h: 3, failure_events_by_kind: { rate_limit: 3 } })
    expect(pressureHeadline(u)).toBe('3× 429')
  })

  it('says "failures" when the events are real but none is a 429', () => {
    // A bare "0× 429" beside a warn chip would read as a contradiction.
    const u = usage({ failure_events_24h: 2, failure_events_by_kind: { auth: 2 } })
    expect(pressureHeadline(u)).toBe('failures')
  })

  it('says unavailable for a failed read, never ok', () => {
    expect(pressureHeadline({ error: true })).toBe('unavailable')
  })
})

describe('usageLine — only figures that are what they say', () => {
  it('omits the context-occupancy figure from the row face', () => {
    // `input_tokens` is SUM(schedule_executions.context_used) — context-window
    // occupancy per run, not input tokens consumed; summing it re-counts the
    // whole conversation on every turn. architecture.md made this exact ruling
    // for the sibling ent#101 tile. It belongs in the tooltip, labelled.
    const u = usage({ window_5h: win({ input_tokens: 999999, output_tokens: 1200, cost_usd: 3.12 }) })
    const line = usageLine(u)
    expect(line).not.toContain('999')
    expect(line).toContain('1.2k out')
    expect(line).toContain('≈$3.12')
  })

  it('marks an unavailable reading instead of rendering zeroes', () => {
    expect(usageLine({ error: true })).toBe('usage data unavailable')
  })

  it('never presents cost as a bill', () => {
    const u = usage({ window_5h: win({ cost_usd: 2 }) })
    expect(usageLine(u)).toContain('≈$')
  })
})

describe('rowTooltip', () => {
  it('labels the context figure as an estimate, not tokens', () => {
    const u = usage({ window_5h: win({ input_tokens: 50000 }) })
    const tip = rowTooltip({ id: 's1', name: 'Max #1' }, u)
    expect(tip).toContain('Context estimate')
    expect(tip).toContain('not billed input tokens')
  })

  it('states that 7d contains the 5h window so the two never read as additive', () => {
    const tip = rowTooltip({ id: 's1', name: 'Max #1' }, usage())
    expect(tip).toContain('includes the 5h window')
  })

  it('surfaces an invalid provider token — the most actionable state there is', () => {
    const u = usage({ headroom: { status: 'invalid_token', five_hour: null, seven_day: null } })
    expect(rowTooltip({ id: 's1', name: 'Max #1' }, u)).toContain('re-register')
  })

  it('names the failure-kind split rather than a bare total', () => {
    const u = usage({ failure_events_24h: 4, failure_events_by_kind: { rate_limit: 3, auth: 1 } })
    const tip = rowTooltip({ id: 's1', name: 'Max #1' }, u)
    expect(tip).toContain('3 rate-limit')
    expect(tip).toContain('1 auth')
  })

  it('says so when no agents are assigned', () => {
    expect(rowTooltip({ id: 's1', name: 'Spare' }, usage())).toContain('No agents assigned')
  })

  it('does not pretend to know usage for a failed read', () => {
    const tip = rowTooltip({ id: 's1', name: 'Max #1' }, { error: true })
    expect(tip).toContain('could not be read')
    expect(tip).not.toContain('≈$0')
  })
})

describe('subscriptionPressureRows — the roster drives the rows', () => {
  const subs = [
    { id: 's-ok', name: 'Beta' },
    { id: 's-crit', name: 'Alpha' },
    { id: 's-warn', name: 'Gamma' },
  ]
  const usageBySub = {
    's-ok': usage(),
    's-crit': usage({ rate_limited_now: true }),
    's-warn': usage({ failure_events_24h: 2, failure_events_by_kind: { rate_limit: 2 } }),
  }

  it('sorts rate-limited to the top (AC#3)', () => {
    const { rows } = subscriptionPressureRows(subs, usageBySub)
    expect(rows.map((r) => r.id)).toEqual(['s-crit', 's-warn', 's-ok'])
  })

  it('keeps a subscription whose usage failed to load', () => {
    // Dropping it would render "not configured" for "could not be read".
    const { rows, totalRows } = subscriptionPressureRows(subs, { 's-ok': { error: true } })
    expect(totalRows).toBe(3)
    expect(rows.find((r) => r.id === 's-ok').severity).toBe('unknown')
  })

  it('keeps a quiet subscription with no usage entry at all', () => {
    const { rows, totalRows } = subscriptionPressureRows([{ id: 'q', name: 'Quiet' }], {})
    expect(totalRows).toBe(1)
    expect(rows[0].primary).toBe('Quiet')
  })

  it('is a TOTAL order — equal rows fall back to name, so polls do not reshuffle', () => {
    const same = [
      { id: 'b', name: 'Bravo' },
      { id: 'a', name: 'Alpha' },
      { id: 'c', name: 'Charlie' },
    ]
    const map = { a: usage(), b: usage(), c: usage() }
    const first = subscriptionPressureRows(same, map).rows.map((r) => r.id)
    const again = subscriptionPressureRows([...same].reverse(), map).rows.map((r) => r.id)
    expect(first).toEqual(['a', 'b', 'c'])
    expect(again).toEqual(first)
  })

  it('carries both limit readings on the row', () => {
    const { rows } = subscriptionPressureRows([{ id: 'a', name: 'A' }], { a: fresh(15) })
    expect(rows[0].windows).toEqual([
      { label: '5h', pct: 15, fill: 15, level: 'ok', resetsAt: null },
    ])
  })

  it('ranks on the FULLEST window, not always the 5h one', () => {
    // A subscription at 30% of its 5h but 95% of its weekly is the one about to
    // stop working; ranking it below a busier-but-fine neighbour buries the risk.
    const list = [{ id: 'busy5h', name: 'Busy5h' }, { id: 'weekly', name: 'WeeklyWall' }]
    const map = {
      busy5h: usage({
        source: 'anthropic',
        headroom: { five_hour: { utilization_pct: 70 }, seven_day: { utilization_pct: 20 }, snapshot_age_seconds: 10, status: 'ok' },
      }),
      weekly: usage({
        source: 'anthropic',
        headroom: { five_hour: { utilization_pct: 30 }, seven_day: { utilization_pct: 95 }, snapshot_age_seconds: 10, status: 'ok' },
      }),
    }
    expect(subscriptionPressureRows(list, map).rows.map((r) => r.id)).toEqual(['weekly', 'busy5h'])
  })

  it('leaves windows null when nothing fresh backs them', () => {
    const { rows } = subscriptionPressureRows([{ id: 'a', name: 'A' }], { a: usage() })
    expect(rows[0].windows).toBeNull()
  })

  it('breaks a severity tie by 429 volume, then by utilization', () => {
    const list = [{ id: 'low', name: 'L' }, { id: 'high', name: 'H' }]
    const map = {
      low: usage({ failure_events_24h: 1, failure_events_by_kind: { rate_limit: 1 } }),
      high: usage({ failure_events_24h: 5, failure_events_by_kind: { rate_limit: 5 } }),
    }
    expect(subscriptionPressureRows(list, map).rows.map((r) => r.id)).toEqual(['high', 'low'])

    const utilList = [{ id: 'a', name: 'A' }, { id: 'b', name: 'B' }]
    const utilMap = { a: fresh(10), b: fresh(90) }
    expect(subscriptionPressureRows(utilList, utilMap).rows.map((r) => r.id)).toEqual(['b', 'a'])
  })

  it('every row carries a resolvable route target', () => {
    // A RouterLink naming a route that does not exist THROWS during render and
    // freezes the whole dashboard (the ent#100 guard exists for this).
    const { rows } = subscriptionPressureRows(subs, usageBySub)
    for (const row of rows) expect(row.to).toEqual(SUBSCRIPTION_SETTINGS_ROUTE)
  })

  it('tolerates a missing roster without throwing', () => {
    expect(subscriptionPressureRows(undefined, undefined).totalRows).toBe(0)
    expect(subscriptionPressureRows(null, null).rows).toEqual([])
  })
})

describe('subscriptionPressureRows — overflow is disclosed, never clipped silently', () => {
  const many = Array.from({ length: 9 }, (_, i) => ({ id: `s${i}`, name: `Sub ${i}` }))

  it('never emits more rows than the tile has tracks', () => {
    // Past the track count a row is clipped by InfoTile's overflow:hidden —
    // no scroll, no ellipsis, it simply disappears.
    const { rows } = subscriptionPressureRows(many, {})
    expect(rows.length).toBeLessThanOrEqual(SUBSCRIPTION_TILE_MAX_ROWS)
  })

  it('counts only real subscriptions as visible, not the overflow link', () => {
    const { visibleRows, totalRows, hiddenRows } = subscriptionPressureRows(many, {})
    expect(totalRows).toBe(9)
    expect(visibleRows).toBe(SUBSCRIPTION_TILE_MAX_ROWS - 1)
    expect(visibleRows + hiddenRows).toBe(totalRows)
  })

  it('the overflow row is reachable, not just a count', () => {
    const { rows } = subscriptionPressureRows(many, {})
    const last = rows[rows.length - 1]
    expect(last.overflow).toBe(true)
    // 9 total, 3 legible (the 4th track is this link) ⇒ 6 hidden.
    expect(last.primary).toBe('+6 more')
    expect(last.to).toEqual(SUBSCRIPTION_SETTINGS_ROUTE)
  })

  it('adds no overflow row when everything fits', () => {
    const { rows, hiddenRows } = subscriptionPressureRows(many.slice(0, 4), {})
    expect(hiddenRows).toBe(0)
    expect(rows.some((r) => r.overflow)).toBe(false)
  })
})

describe('subscriptionPressureTileState — empty needs positive evidence', () => {
  it('is loading before the first fetch lands', () => {
    expect(subscriptionPressureTileState({ loaded: false, error: false, totalRows: 0 })).toBe('loading')
  })

  it('is error when the first fetch failed', () => {
    expect(subscriptionPressureTileState({ loaded: false, error: true, totalRows: 0 })).toBe('error')
  })

  it('is empty only after a fetch that SUCCEEDED and returned zero', () => {
    expect(subscriptionPressureTileState({ loaded: true, error: false, totalRows: 0 })).toBe('empty')
  })

  it('keeps showing rows when a later poll fails (stale-while-revalidate)', () => {
    // Replacing real data with an error panel discards good information; the
    // staleness is disclosed in the stamp instead.
    expect(subscriptionPressureTileState({ loaded: true, error: true, totalRows: 3 })).toBe('ready')
  })

  it('a failed refresh over a previously-empty fleet still reads empty, not error', () => {
    expect(subscriptionPressureTileState({ loaded: true, error: true, totalRows: 0 })).toBe('empty')
  })
})

describe('subscriptionTileStamp', () => {
  it('discloses the overflow', () => {
    expect(subscriptionTileStamp({ visibleRows: 3, totalRows: 9 })).toBe('3 of 9')
  })

  it('counts subscriptions when they all fit', () => {
    expect(subscriptionTileStamp({ visibleRows: 2, totalRows: 2 })).toBe('2 subscriptions')
    expect(subscriptionTileStamp({ visibleRows: 1, totalRows: 1 })).toBe('1 subscription')
  })

  it('marks a stale reading', () => {
    expect(subscriptionTileStamp({ visibleRows: 2, totalRows: 2, stale: true })).toContain('stale')
  })

  it('renders nothing when there is nothing to count', () => {
    expect(subscriptionTileStamp({ visibleRows: 0, totalRows: 0 })).toBe('')
  })
})

describe('the deep-link target actually resolves', () => {
  /**
   * `gridTileLinks.spec.js` auto-discovers `components/tiles/**` and reads route
   * targets written as OBJECT LITERALS. This tile's targets are a shared
   * constant in `utils/`, which that reader explicitly cannot resolve ("computed
   * targets remain invisible; the mitigation is convention, not regex"). So the
   * check is made here instead — and it covers strictly more, because every ROW
   * carries the same target and rows are the half the sibling guard was blind to
   * before ent#100 widened it.
   *
   * The failure it guards is not cosmetic: a RouterLink naming a route that does
   * not exist THROWS during render, which aborts Vue's update for the whole tree
   * and FREEZES THE DASHBOARD.
   */
  const SRC = resolve(dirname(fileURLToPath(import.meta.url)), '../../src')

  it('names a route the router actually declares', () => {
    const router = readFileSync(resolve(SRC, 'router/index.js'), 'utf8')
    const names = new Set(
      [...router.matchAll(/name:\s*['"]([A-Za-z][\w-]*)['"]/g)].map((m) => m[1]),
    )
    expect(names.has(SUBSCRIPTION_SETTINGS_ROUTE.name)).toBe(true)
  })

  it('names the Settings tab that actually mounts the subscriptions panel', () => {
    // Route names are validated by the sibling guard; the `tab` QUERY is not,
    // so a wrong value lands the operator on an unrelated Settings tab with
    // nothing failing anywhere. Pin it against the real mount condition.
    const settings = readFileSync(resolve(SRC, 'views/Settings.vue'), 'utf8')
    expect(settings).toContain(
      `<SubscriptionsPanel v-if="activeTab === '${SUBSCRIPTION_SETTINGS_ROUTE.query.tab}'"`,
    )
  })
})

describe('formatters', () => {
  it('formats token counts compactly', () => {
    expect(formatTokenCount(0)).toBe('0')
    expect(formatTokenCount(940)).toBe('940')
    expect(formatTokenCount(1200)).toBe('1.2k')
    expect(formatTokenCount(45000)).toBe('45k')
    expect(formatTokenCount(2_400_000)).toBe('2.4M')
  })

  it('treats absent or nonsense counts as zero rather than NaN', () => {
    expect(formatTokenCount(undefined)).toBe('0')
    expect(formatTokenCount(null)).toBe('0')
    expect(formatTokenCount(Number.NaN)).toBe('0')
    expect(formatTokenCount(-5)).toBe('0')
  })

  it('always marks cost as approximate', () => {
    expect(formatApproxCost(0)).toBe('≈$0')
    expect(formatApproxCost(3.125)).toBe('≈$3.13')
    expect(formatApproxCost(0.4)).toBe('≈$0.40')
    expect(formatApproxCost(1500)).toBe('≈$1500')
    expect(formatApproxCost(undefined)).toBe('≈$0')
  })
})

/**
 * #447 — "when does the limit come back".
 *
 * The rules that matter here are the ones whose regression is SILENT: a reset
 * that quietly stops rendering on the one row that needs it, or a stale instant
 * presented as a confident future time.
 */
const T0 = Date.parse('2026-08-20T18:00:00Z')

/** A snapshot the provider returned WITH a 429 — the #447 case. */
function limitedSnapshot(o = {}) {
  return usage({
    // `source` stays 'observed': `decorate_usage` only promotes to 'anthropic'
    // on status 'ok', which a 429 never is. That is exactly why the reset was
    // unreachable through `windowReadings`.
    source: 'observed',
    rate_limited_now: true,
    headroom: {
      five_hour: { utilization_pct: 100, resets_at: '2026-08-20T19:10:00Z', status: 'blocked' },
      seven_day: { utilization_pct: 40, resets_at: '2026-08-26T05:00:00Z', status: 'allowed' },
      representative_claim: 'five_hour',
      snapshot_age_seconds: 30,
      status: 'rate_limited',
      ...o,
    },
  })
}

describe('resetReading — the reset survives the freshness gate that hides the %', () => {
  it('reads a reset off a rate_limited snapshot, where windowReadings gives nothing', () => {
    const u = limitedSnapshot()
    // The precondition this whole feature exists for.
    expect(windowReadings(u)).toBeNull()
    expect(resetReading(u, T0)).toMatchObject({ label: '5h', at: expect.any(String), due: false })
  })

  it('is not gated on snapshot age — an instant does not decay like a percentage', () => {
    const stale = limitedSnapshot({ snapshot_age_seconds: 4000 })
    expect(resetReading(stale, T0)).not.toBeNull()
  })

  it('prefers the window the provider names in representative_claim', () => {
    const u = limitedSnapshot({ representative_claim: 'seven_day' })
    expect(resetReading(u, T0).label).toBe('7d')
  })

  it('falls back to the blocking window when no claim is given', () => {
    const u = limitedSnapshot({ representative_claim: null })
    expect(resetReading(u, T0).label).toBe('5h')
  })

  it('falls back to the fullest window when nothing is blocking and no claim is given', () => {
    const u = limitedSnapshot({
      representative_claim: null,
      five_hour: { utilization_pct: 10, resets_at: '2026-08-20T19:10:00Z', status: 'allowed' },
      seven_day: { utilization_pct: 90, resets_at: '2026-08-26T05:00:00Z', status: 'allowed' },
    })
    expect(resetReading(u, T0).label).toBe('7d')
  })

  it('reports a lapsed instant as due rather than as a future time', () => {
    const u = limitedSnapshot()
    const after = Date.parse('2026-08-20T19:30:00Z')
    expect(resetReading(u, after).due).toBe(true)
    expect(resetText(u, after)).toBe('reset due')
  })

  it('returns nothing for a rejected token — a dead credential has no quota clock', () => {
    const u = limitedSnapshot({ status: 'invalid_token' })
    expect(resetReading(u, T0)).toBeNull()
  })

  it('returns nothing when the payload carries no reset, rather than inventing one', () => {
    const u = limitedSnapshot({
      five_hour: { utilization_pct: 100, resets_at: null, status: 'blocked' },
      seven_day: null,
      representative_claim: 'five_hour',
    })
    expect(resetReading(u, T0)).toBeNull()
    expect(resetText(u, T0)).toBeNull()
  })

  it('returns nothing when there is no snapshot at all (db-predicate-only limit)', () => {
    expect(resetReading(usage({ rate_limited_now: true }), T0)).toBeNull()
  })
})

describe('showsReset — only under pressure', () => {
  it('shows on a rate-limited row', () => {
    expect(showsReset('crit', null)).toBe(true)
  })

  it('shows on a near-limit row', () => {
    expect(showsReset('warn', [{ level: 'high' }])).toBe(true)
  })

  it('stays off a healthy row — a reset at 12% used is noise', () => {
    expect(showsReset('ok', [{ level: 'ok' }])).toBe(false)
    expect(showsReset('warn', [{ level: 'warm' }])).toBe(false)
  })
})

describe('subscriptionPressureRows — where the reset lands on the row face', () => {
  const subs = [{ id: 's1', name: 'Max' }]

  it('joins the headline on the primary line when there are no windows', () => {
    const { rows } = subscriptionPressureRows(subs, { s1: limitedSnapshot() }, { now: T0 })
    expect(rows[0].meta).toContain('rate-limited')
    expect(rows[0].meta).toContain('resets')
    // The second line keeps the spend figures untouched.
    expect(rows[0].sub).not.toContain('resets')
  })

  it('says so when a limited row has no reset to show', () => {
    const u = usage({ rate_limited_now: true })
    const { rows } = subscriptionPressureRows(subs, { s1: u }, { now: T0 })
    expect(rows[0].meta).toContain('reset unknown')
  })

  it('leads the second line when the bars already occupy the first', () => {
    const u = fresh(92, { resets_at: null })
    u.headroom.five_hour = { utilization_pct: 92, resets_at: '2026-08-20T19:10:00Z', status: 'allowed' }
    u.headroom.representative_claim = 'five_hour'
    const { rows } = subscriptionPressureRows(subs, { s1: u }, { now: T0 })
    expect(rows[0].windows).not.toBeNull()
    expect(rows[0].sub.startsWith('resets ')).toBe(true)
    // The bars are the meta; nothing is appended there that could overflow a
    // row that clips silently.
    expect(rows[0].meta).not.toContain('resets')
  })

  it('leaves a healthy row exactly as it was', () => {
    const u = fresh(12)
    const { rows } = subscriptionPressureRows(subs, { s1: u }, { now: T0 })
    expect(rows[0].sub).not.toContain('resets')
    expect(rows[0].meta).not.toContain('resets')
    expect(rows[0].reset).toBeNull()
  })
})
