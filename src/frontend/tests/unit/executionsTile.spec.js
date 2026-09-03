/**
 * Fleet Executions tile shaping (ent#96).
 *
 * The tile stacks 24 hourly columns by trigger bucket with failures on their
 * own rail. Everything a node-environment suite can reach lives in
 * `utils/executionsTile.js`; these pin the properties the chart's honesty rests
 * on:
 *
 *   - a column's segments sum to the column total (a stack that does not add up
 *     to its own bar is the defect the split endpoint exists to prevent);
 *   - failures are never folded INTO a segment, so no segment silently means
 *     "succeeded" (AC2);
 *   - the bucket vocabulary and its order come from the backend, and a bucket
 *     the order does not mention is appended rather than dropped (AC1);
 *   - "no executions in 24h" requires a successful read — the manufactured-green
 *     rule ent#100 established.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  BUCKET_TOKENS,
  CHART_HEIGHT,
  CHART_PAD,
  COL_GAP,
  bucketToken,
  chartColumns,
  headline,
  hourLabel,
  keyWidth,
  LEGEND_ROWS,
  LEGEND_WIDTH,
  legendFit,
  legendKeys,
  presentBuckets,
  headlineFace,
  RAIL_HEIGHT,
  scanlineProps,
  stackOrder,
  tileState,
} from '@/utils/executionsTile'
import { CELL_W } from '@/utils/gridLayout'
import {
  initialPhase,
  onLoadingChange,
  onRevealEnd,
  PHASE_LOADED,
  PHASE_LOADING,
  PHASE_REVEALING,
} from '@/utils/scanlinePhase'

const ORDER = [
  'Chat/Tasks', 'MCP', 'Channels', 'Public',
  'Scheduled', 'Loops', 'Reminders', 'Agent-to-agent', 'Voice', 'Other',
]

function bucket(hour, by = {}, extra = {}) {
  const total = Object.values(by).reduce((n, e) => n + e.total, 0)
  const failed = Object.values(by).reduce((n, e) => n + (e.failed || 0), 0)
  const success = Math.max(0, total - failed)
  return { bucket: `2026-08-14T${hour}`, total, failed, success, by_trigger: by, ...extra }
}

describe('#96 stack order', () => {
  it('follows the backend order, so Other stays last', () => {
    expect(stackOrder(ORDER, [])).toEqual(ORDER)
  })

  it('appends a bucket the order does not mention rather than dropping it', () => {
    const rows = [bucket('09', { Newcomer: { total: 3, failed: 0 } })]
    const order = stackOrder(ORDER, rows)
    expect(order).toContain('Newcomer')
    expect(order.indexOf('Newcomer')).toBe(order.length - 1)
  })

  it('never drops a bucket that carries runs, so the stack matches its column', () => {
    const rows = [bucket('09', { Newcomer: { total: 3, failed: 1 }, MCP: { total: 2, failed: 0 } })]
    const [col] = chartColumns(rows, { triggerOrder: ORDER })
    const stacked = col.segments.reduce((n, s) => n + s.total, 0)
    expect(stacked).toBe(col.total)
  })

  it('colours an unknown bucket with the catch-all rather than nothing', () => {
    expect(bucketToken('Newcomer')).toBe(BUCKET_TOKENS.Other)
    expect(bucketToken('Scheduled')).toBe('sched')
  })
})

describe('#96 columns', () => {
  const rows = [
    bucket('08', {}),
    bucket('09', { Scheduled: { total: 60, failed: 0 } }),
    bucket('10', { Scheduled: { total: 10, failed: 2 }, 'Chat/Tasks': { total: 5, failed: 1 } }),
  ]

  it('segments sum to the column total in every column', () => {
    for (const col of chartColumns(rows, { triggerOrder: ORDER })) {
      expect(col.segments.reduce((n, s) => n + s.total, 0)).toBe(col.total)
    }
  })

  it('keeps failures OUT of the stack and on their own rail', () => {
    const [, , busy] = chartColumns(rows, { triggerOrder: ORDER })
    // The trigger segment still carries ALL its runs, failed ones included...
    const sched = busy.segments.find((s) => s.name === 'Scheduled')
    expect(sched.total).toBe(10)
    expect(sched.failed).toBe(2)
    // ...and the rail encodes the failures separately.
    expect(busy.failed).toBe(3)
    expect(busy.failPx).toBeGreaterThan(0)
  })

  it('gives a zero hour a baseline stub, not a gap', () => {
    const [quiet] = chartColumns(rows, { triggerOrder: ORDER })
    expect(quiet.stub).toBe(true)
    expect(quiet.segments).toEqual([])
    expect(quiet.failPx).toBe(0)
  })

  it('scales against the tallest column so the bars stay comparable', () => {
    const [, tall, short] = chartColumns(rows, { triggerOrder: ORDER, chartHeight: 60 })
    const tallPx = tall.segments.reduce((n, s) => n + s.px, 0)
    const shortPx = short.segments.reduce((n, s) => n + s.px, 0)
    expect(tallPx).toBeGreaterThan(shortPx)
    expect(tallPx).toBeLessThanOrEqual(60)
  })

  it('never rounds a real execution away to nothing', () => {
    const lopsided = [
      bucket('09', { Scheduled: { total: 5000, failed: 0 } }),
      bucket('10', { MCP: { total: 1, failed: 0 } }),
    ]
    const [, tiny] = chartColumns(lopsided, { triggerOrder: ORDER, chartHeight: 60 })
    expect(tiny.segments[0].px).toBeGreaterThanOrEqual(1)
  })

  it('labels the UTC hour', () => {
    expect(hourLabel('2026-08-14T09')).toBe('09')
    expect(hourLabel('2026-08-14')).toBe('2026-08-14')
    expect(hourLabel(undefined)).toBe('')
  })

  it('tolerates a malformed response shape without throwing', () => {
    expect(chartColumns(null)).toEqual([])
    expect(chartColumns([{}], { triggerOrder: ORDER })[0].total).toBe(0)
  })
})

describe('#96 legend', () => {
  it('lists only buckets that occurred, in stack order', () => {
    const rows = [
      bucket('09', { Scheduled: { total: 3, failed: 0 } }),
      bucket('10', { MCP: { total: 1, failed: 0 }, Scheduled: { total: 2, failed: 0 } }),
    ]
    expect(presentBuckets(ORDER, rows).map((b) => b.name)).toEqual(['MCP', 'Scheduled'])
  })

  it('sums a bucket across the window', () => {
    const rows = [
      bucket('09', { Scheduled: { total: 3, failed: 0 } }),
      bucket('10', { Scheduled: { total: 2, failed: 1 } }),
    ]
    expect(presentBuckets(ORDER, rows)[0].total).toBe(5)
  })
})

describe('#96 headline', () => {
  it('reports terminal success rate, not runs-over-total', () => {
    const rows = [bucket('09', { Scheduled: { total: 10, failed: 2 } })]
    // 8 success / 10 terminal
    expect(headline(rows)).toMatchObject({ total: 10, failed: 2, successRate: 80 })
  })

  it('reports null rather than 0% when nothing has terminated', () => {
    const rows = [{ bucket: '2026-08-14T09', total: 4, failed: 0, success: 0, by_trigger: {} }]
    expect(headline(rows).successRate).toBeNull()
  })

  it('is zero-safe on an empty window', () => {
    expect(headline([])).toEqual({ total: 0, success: 0, failed: 0, successRate: null })
  })
})

describe('#96 tile state — a green claim needs positive evidence', () => {
  it('is loading before the first successful read', () => {
    expect(tileState({ loaded: false, error: false, buckets: [] })).toBe('loading')
  })

  it('is error, never empty, when the first read failed', () => {
    expect(tileState({ loaded: false, error: true, buckets: [] })).toBe('error')
  })

  it('keeps showing data when a background refresh fails (stale-while-revalidate)', () => {
    const rows = [bucket('09', { Scheduled: { total: 1, failed: 0 } })]
    expect(tileState({ loaded: true, error: true, buckets: rows })).toBe('ready')
  })

  it('only claims "no executions" after a successful read', () => {
    expect(tileState({ loaded: true, error: false, buckets: [] })).toBe('empty')
  })
})

/**
 * Legend fitting (#2228).
 *
 * The legend clamped itself with `max-height: 26px` — about 1.7 rows — so
 * `overflow: hidden` sliced row two through its glyphs, and every key past the
 * clamp disappeared with no `+N` and no tooltip: a colour rendered in the chart
 * with no decoder anywhere on the tile.
 *
 * These pin the two halves of the fix and, critically, that they AGREE. The
 * packer decides what fits; the CSS decides how much is shown. If the two
 * disagree about the row count the bug returns silently in whichever direction
 * drifted, which is why the last test reads the component's own stylesheet
 * rather than trusting the comment in it.
 */
describe('#2228 legend fitting', () => {
  const FULL = [
    'Chat/Tasks', 'MCP', 'Channels', 'Public', 'Scheduled',
    'Loops', 'Reminders', 'Agent-to-agent', 'Voice', 'Other',
  ]

  function keys(names) {
    return names.map((name) => ({ name, total: 1, token: bucketToken(name) }))
  }

  it('shows every key when the set fits', () => {
    const fit = legendFit(keys(['Chat/Tasks', 'Scheduled']))
    expect(fit.shown.map((k) => k.name)).toEqual(['Chat/Tasks', 'Scheduled'])
    expect(fit.hidden).toEqual([])
  })

  it('fits a real fleet without a chip — truncation is the tail case, not the norm', () => {
    // The set from the #2228 report: eight keys, which is what the clamp used
    // to slice. If this ever starts truncating, ordinary fleets have begun
    // seeing `+N` and the width model needs re-deriving — not this test
    // relaxing.
    const observed = [
      ...keys(['Chat/Tasks', 'MCP', 'Channels', 'Public', 'Scheduled', 'Agent-to-agent', 'Other']),
      { name: 'failed', total: 1, token: null, fail: true },
    ]
    const fit = legendFit(observed)
    expect(fit.hidden).toEqual([])
    expect(fit.shown).toHaveLength(8)
  })

  it('surrenders only a short announced tail on the complete vocabulary', () => {
    // All ten buckets plus `failed` sit past two rows under a deliberately
    // pessimistic model, and the chip has to occupy the last row itself, which
    // costs one key beyond the true overflow. Both effects are accepted: the
    // result is a `+2` that names what it hides, not a silent slice. What must
    // NOT drift is the size of that tail — a model change that starts hiding
    // half the vocabulary is a regression even though nothing is lost.
    const fit = legendFit([...keys(FULL), { name: 'failed', total: 3, token: null, fail: true }])
    expect(fit.hidden.length).toBeLessThanOrEqual(3)
    // The tail comes off the END, so the buckets a fleet uses most — which the
    // backend orders first — always keep their key.
    expect(fit.shown.map((k) => k.name)).toEqual(FULL.slice(0, fit.shown.length))
  })

  it('never estimates a key narrower than the browser renders it', () => {
    // The invariant the whole model rests on. These are measured upper bounds
    // for 10px system sans; the estimate must sit above every one of them, or
    // the browser can form a row the packer did not plan for and the clamp
    // eats it silently.
    const MEASURED_MAX = {
      'Chat/Tasks': 51, MCP: 20, Channels: 42, Public: 32, Scheduled: 47,
      Loops: 26, Reminders: 47, 'Agent-to-agent': 68, Voice: 27, Other: 28,
      failed: 30,
    }
    for (const [name, px] of Object.entries(MEASURED_MAX)) {
      // keyWidth includes the 6px dot and its 3px gap, which the label figures
      // above do not.
      expect(keyWidth(name), name).toBeGreaterThan(px + 9)
    }
  })

  it('never silently drops a key: what does not fit is returned as hidden', () => {
    const many = keys(Array.from({ length: 40 }, (_, i) => `Bucket-number-${i}`))
    const fit = legendFit(many)
    expect(fit.hidden.length).toBeGreaterThan(0)
    // The union is the input, in order — nothing is lost between the two lists.
    expect([...fit.shown, ...fit.hidden].map((k) => k.name)).toEqual(many.map((k) => k.name))
  })

  it('keeps the shown keys in stack order, so legend and chart agree', () => {
    const fit = legendFit(keys(FULL.concat(FULL.map((n) => n + '-x'))))
    const names = fit.shown.map((k) => k.name)
    expect(names).toEqual(FULL.concat(FULL.map((n) => n + '-x')).slice(0, names.length))
  })

  it('reserves room for the +N chip only when it truncates', () => {
    // A set that fits exactly without the chip must not be truncated to make
    // space for a chip that would then have nothing to report.
    const exact = legendFit(keys(FULL))
    expect(exact.hidden).toEqual([])
    // ...and one that overflows loses at least the overflowing key.
    const over = legendFit(keys(FULL), { rows: 1 })
    expect(over.hidden.length).toBeGreaterThan(0)
  })

  it('honours the row budget: one row holds strictly less than two', () => {
    const one = legendFit(keys(FULL), { rows: 1 })
    const two = legendFit(keys(FULL), { rows: 2 })
    expect(one.shown.length).toBeLessThan(two.shown.length)
  })

  it('force-places a key wider than an entire row rather than spinning', () => {
    const huge = keys(['x'.repeat(500), 'Scheduled'])
    const fit = legendFit(huge)
    expect(fit.shown[0].name).toHaveLength(500)
    expect([...fit.shown, ...fit.hidden]).toHaveLength(2)
  })

  it('is total on junk input', () => {
    expect(legendFit(null)).toEqual({ shown: [], hidden: [] })
    expect(legendFit(undefined)).toEqual({ shown: [], hidden: [] })
    expect(() => legendFit([{ total: 1 }])).not.toThrow()
  })

  it('appends `failed` last and flags it, so it never takes a bucket colour', () => {
    const rows = [bucket('09', { Scheduled: { total: 4, failed: 1 } })]
    const built = legendKeys(ORDER, rows)
    expect(built.at(-1)).toMatchObject({ name: 'failed', total: 1, fail: true })
  })

  it('omits `failed` entirely when nothing failed', () => {
    const rows = [bucket('09', { Scheduled: { total: 4 } })]
    expect(legendKeys(ORDER, rows).some((k) => k.fail)).toBe(false)
  })

  it('derives its width from the lattice cell, and never wider than the real box', () => {
    // A tile that grows must widen the legend budget with it; a hardcoded
    // literal would leave the packer describing a tile that no longer exists.
    expect(LEGEND_WIDTH).toBe(CELL_W - 50)
    // 334px is the content box the live tile reports. Counting only InfoTile's
    // padding and forgetting its 1px borders gives 338 — four px WIDER than the
    // element is, which is the one direction that manufactures a third row.
    expect(LEGEND_WIDTH).toBeLessThanOrEqual(334)
  })

  // The stylesheet and these constants describe one shared budget and cannot
  // check each other at runtime, so the source is parsed. jsdom would not help:
  // it does no cascade resolution, so a mid-row clamp looks identical to a
  // clean one under getComputedStyle. Same rationale as gridTokens.spec.js.
  const TILE_CSS = readFileSync(
    join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'src',
         'components', 'tiles', 'ExecutionsTile.vue'),
    'utf8',
  )

  it('sizes the legend to exactly LEGEND_ROWS whole rows', () => {
    const clamp = TILE_CSS.match(
      /\n\s*height:\s*calc\(var\(--ex-row\)\s*\*\s*(\d+)\s*\+\s*var\(--ex-row-gap\)\)/,
    )
    expect(clamp, 'legend must size with the row-arithmetic calc, not a raw px value').toBeTruthy()
    expect(Number(clamp[1])).toBe(LEGEND_ROWS)
    // The pitch the arithmetic assumes has to be asserted on `.ex-key`, not
    // inherited from whatever the cascade happens to supply.
    expect(TILE_CSS).toMatch(/line-height:\s*var\(--ex-row\)/)
  })

  it('fixes the legend height rather than capping it, so the chart never reflows', () => {
    // `max-height` grows and shrinks with the key count, which resizes the
    // chart above whenever a bucket appears or goes quiet. That is the layout
    // instability the design contract forbids, and it is one character away.
    expect(TILE_CSS).not.toMatch(/max-height:\s*calc\(var\(--ex-row\)/)
  })

  it('gives the chart exactly the height a full column needs — no dead space', () => {
    // `.it-body` is `overflow: hidden` at a fixed CELL_H, so a chart container
    // taller than its tallest possible column does not merely waste space: it
    // takes those px off the bottom of the legend. Four px of dead space here
    // is the second clip behind #2228, and it survived fixing the legend.
    const h = TILE_CSS.match(/\.ex-chart\s*\{[^}]*?\n\s*height:\s*(\d+)px/s)
    expect(h, '.ex-chart must declare an explicit height').toBeTruthy()
    expect(Number(h[1])).toBe(CHART_HEIGHT + COL_GAP + RAIL_HEIGHT + CHART_PAD)
  })

  it('renders the failure rail only when there is one, so every column ends on the baseline', () => {
    // `.ex-col` spaces stack and rail with `gap`, and flex gap applies to a
    // 0px-tall item exactly as to a 6px one. An always-present empty rail
    // therefore lifted every failure-free stack 2px above the chart baseline
    // while a real rail reached it — the red read as hanging BELOW the chart.
    // The rail has to be absent, not merely 0px, for the columns to align.
    expect(TILE_CSS).toMatch(/<i\s+v-if="col\.failPx"\s+class="ex-rail"/)
  })

  it('draws the chart against the same budget the CSS reserves', () => {
    // The defaults are what the component relies on; a column taller than the
    // container it is drawn into overflows into the legend's rows.
    const tallest = chartColumns([bucket('09', { Scheduled: { total: 9, failed: 9 } })])[0]
    expect(tallest.segments[0].px).toBeLessThanOrEqual(CHART_HEIGHT)
    expect(tallest.failPx).toBeLessThanOrEqual(RAIL_HEIGHT)
  })
})

/**
 * ent#449 — the chart zone's one loading motion.
 *
 * vitest runs node-environment here, so the phase↔DOM wiring is out of reach:
 * what CAN be proven is (a) the pure rules the component binds, driven through
 * the real phase machine over the exact inputs the store emits, and (b) the
 * markup/stylesheet invariants the motion rests on, parsed from source (the
 * #2228 pin style — a stylesheet and a JS constant cannot check each other).
 *
 * The invariants worth naming:
 *   - ONE persistent ScanlineReveal, with the loaded/loading branch INSIDE its
 *     slot. Sibling v-if branches around the component remount it, it re-inits
 *     from loading=false, and the reveal never plays (the primitive's own usage
 *     contract; learnings 2026-08-24 for the terminal-under-the-track half);
 *   - the headline sits OUTSIDE the zone, so the em-dash is visible while
 *     loading and the first frame of the wipe (`inset(0 100% 0 0)`) cannot
 *     blink it away;
 *   - loading is "no data yet", never "fetch in flight": a 60s background
 *     refresh — success OR failure — can never re-raise the beam.
 */
describe('ent#449 chart zone — one loading motion', () => {
  const SRC_DIR = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'src')
  const TILE_SRC = readFileSync(join(SRC_DIR, 'components', 'tiles', 'ExecutionsTile.vue'), 'utf8')
  const INFOTILE_SRC = readFileSync(join(SRC_DIR, 'components', 'InfoTile.vue'), 'utf8')
  const TEMPLATE = TILE_SRC.slice(0, TILE_SRC.indexOf('<script setup>'))

  /** The store's own refs, in the shape the component reads them. */
  const zoneFor = (store) => scanlineProps(tileState(store))
  const DATA = [bucket('09', { Scheduled: { total: 4, failed: 1 } })]

  it('derives the zone props from the pure state, and is total on junk', () => {
    expect(scanlineProps('loading')).toEqual({ loading: true, reveal: false })
    expect(scanlineProps('ready')).toEqual({ loading: false, reveal: true })
    // A data-less terminal must SNAP: the reveal is a celebration of data
    // arriving, and there is none (design-system §6).
    expect(scanlineProps('empty')).toEqual({ loading: false, reveal: false })
    expect(scanlineProps('error')).toEqual({ loading: false, reveal: false })
    expect(scanlineProps(undefined)).toEqual({ loading: false, reveal: false })
    expect(scanlineProps('nonsense')).toEqual({ loading: false, reveal: false })
  })

  it('first load with data plays exactly one reveal', () => {
    const cold = zoneFor({ loaded: false, error: false, buckets: [] })
    expect(cold.loading).toBe(true)
    let phase = initialPhase(cold.loading)
    expect(phase).toBe(PHASE_LOADING)

    const arrived = zoneFor({ loaded: true, error: false, buckets: DATA })
    phase = onLoadingChange(phase, arrived.loading, { reveal: arrived.reveal })
    expect(phase).toBe(PHASE_REVEALING)
    expect(onRevealEnd(phase)).toBe(PHASE_LOADED)
  })

  it('the 60s background refresh is invisible — success AND failure', () => {
    // The property AC4 rests on: `execTimelineLoaded` latches true on the first
    // success and is never set false, so no later refresh can re-raise the beam.
    let phase = onLoadingChange(initialPhase(true), false, { reveal: true })
    phase = onRevealEnd(phase)
    expect(phase).toBe(PHASE_LOADED)

    const refreshes = [
      { loaded: true, error: false, buckets: DATA },
      { loaded: true, error: true, buckets: DATA }, // refresh failed → `24h · stale`
      { loaded: true, error: false, buckets: [...DATA, bucket('10', { MCP: { total: 2 } })] },
    ]
    for (const store of refreshes) {
      expect(tileState(store)).not.toBe('loading')
      const zone = zoneFor(store)
      expect(zone.loading).toBe(false)
      phase = onLoadingChange(phase, zone.loading, { reveal: zone.reveal })
      expect(phase).toBe(PHASE_LOADED)
    }
  })

  it('an error terminal snaps instead of celebrating', () => {
    const store = { loaded: false, error: true, buckets: [] }
    expect(tileState(store)).toBe('error')
    const zone = zoneFor(store)
    expect(zone.reveal).toBe(false)
    expect(onLoadingChange(PHASE_LOADING, zone.loading, { reveal: zone.reveal })).toBe(PHASE_LOADED)
  })

  it('an empty terminal snaps instead of celebrating', () => {
    const store = { loaded: true, error: false, buckets: [] }
    expect(tileState(store)).toBe('empty')
    const zone = zoneFor(store)
    expect(zone.reveal).toBe(false)
    expect(onLoadingChange(PHASE_LOADING, zone.loading, { reveal: zone.reveal })).toBe(PHASE_LOADED)
  })

  it('KNOWN GAP: a retry after a first-read error snaps, it does not re-beam', () => {
    // The store never clears `execTimelineError` before a retry fetch (only on
    // success), so 'error' → 'ready' skips 'loading' entirely and an explicit
    // Retry gets no in-progress feedback. Recorded here rather than asserted as
    // correct: the honest fix (clear the error on a RETRY-initiated fetch only,
    // never on the background poll, which would blink the stale stamp every
    // 60s) is a store change outside this issue.
    const seq = [
      { loaded: false, error: true, buckets: [] },
      { loaded: true, error: false, buckets: DATA },
    ]
    const states = seq.map(tileState)
    expect(states).toEqual(['error', 'ready'])
    expect(states).not.toContain('loading')
    expect(seq.map((s) => zoneFor(s).loading)).toEqual([false, false])
  })

  it('holds the headline at an em-dash until the first read lands', () => {
    const loading = headlineFace(headline([]), 'loading')
    expect(loading.total).toBe('—')
    expect(loading.ok).toBe('—')
    // Never "— failed": rendering a failure segment while loading asserts that
    // failures exist before anything has been read.
    expect(loading.failed).toBe(0)

    const ready = headlineFace(headline(DATA), 'ready')
    expect(ready).toEqual({ total: '4', ok: '75%', failed: 1 })

    // The existing terminal-based rule survives: nothing terminated → '—', not 0%.
    const running = headlineFace({ total: 3, failed: 0, successRate: null }, 'ready')
    expect(running).toEqual({ total: '3', ok: '—', failed: 0 })

    const empty = headlineFace(headline([]), 'empty')
    expect(empty).toEqual({ total: '0', ok: '—', failed: 0 })
  })

  it('mounts exactly ONE ScanlineReveal, with the branch inside its slot', () => {
    // A second instance, or a branch AROUND the component, remounts it: the
    // machine re-inits from loading=false and the reveal never plays.
    expect(TEMPLATE.match(/<ScanlineReveal\b/g)).toHaveLength(1)

    const open = TEMPLATE.indexOf('<ScanlineReveal')
    const close = TEMPLATE.indexOf('</ScanlineReveal>')
    expect(close).toBeGreaterThan(open)
    const branch = TEMPLATE.indexOf('<template v-if=', open)
    expect(branch, 'the loaded branch must live INSIDE the slot').toBeGreaterThan(open)
    expect(branch).toBeLessThan(close)
    expect(TEMPLATE.slice(branch, close)).toMatch(/<template v-if="!zone\.loading">/)
  })

  it('keeps the headline outside the zone and the legend after it', () => {
    // Order is load-bearing twice over: the first frame of `scan-wipe` clips
    // ALL slot content to nothing, so a headline inside the zone blinks; and
    // the `.ex-head ~ .ex-zone` theming selector needs the head to precede it.
    const head = TEMPLATE.indexOf('class="ex-head"')
    const open = TEMPLATE.indexOf('<ScanlineReveal')
    const close = TEMPLATE.indexOf('</ScanlineReveal>')
    const legend = TEMPLATE.indexOf('class="ex-legend"')
    expect(head).toBeGreaterThan(-1)
    expect(legend).toBeGreaterThan(-1)
    expect(head).toBeLessThan(open)
    expect(open).toBeLessThan(close)
    expect(close).toBeLessThan(legend)
  })

  it('binds the zone to the pure rule, never to a fetch-in-flight flag', () => {
    expect(TEMPLATE).toMatch(/:loading="zone\.loading"/)
    expect(TEMPLATE).toMatch(/:reveal="zone\.reveal"/)
    // "loading" means NO DATA YET (§6). Binding it to a request flag makes the
    // beam replay on every 60s poll — the strobe #1927 exists to prevent.
    const bound = [...TEMPLATE.matchAll(/:loading="([^"]*)"/g)].map((m) => m[1])
    expect(bound).toEqual(['zone.loading'])
    for (const expr of bound) {
      expect(expr).not.toMatch(/Loaded|fetching|Fetching|pending/)
    }
  })

  it('renders the headline through the face, so the em-dash cannot leak', () => {
    expect(TEMPLATE).toMatch(/\{\{\s*face\.total\s*\}\}/)
    expect(TEMPLATE).toMatch(/face\.ok/)
    expect(TEMPLATE).toMatch(/v-if="face\.failed"/)
    // A surviving `head.*` read in the template is a value that bypasses the
    // loading face and shows "0 runs" before anything has been read.
    expect(TEMPLATE).not.toMatch(/head\.total|head\.successRate|head\.failed/)
  })

  it('gives the zone and the chart ONE footprint, and stops the margin collapse', () => {
    // The primitive's content wrapper is auto-height and EMPTY while loading,
    // so the zone root must be sized or the track has no height at all; the
    // inner box must be sized too, because a percentage height does not resolve
    // inside that wrapper. One literal, so they cannot drift.
    const shared = TILE_SRC.match(/\.ex-zone,\s*\n\s*\.ex-chart\s*\{[^}]*?\n\s*height:\s*(\d+)px/s)
    expect(shared, 'zone and chart must share one height block').toBeTruthy()
    expect(Number(shared[1])).toBe(CHART_HEIGHT + COL_GAP + RAIL_HEIGHT + CHART_PAD)

    // `.scanline` is `position: relative` but not a BFC root, and `.scan-content`
    // carries `margin: -4px`. That negative top margin collapses THROUGH the
    // zone root and then with `.ex-head`'s 6px bottom margin: measured in
    // Chromium the head→zone gap drops to 2px and the chart renders 4px BELOW
    // the track for the whole 550ms wipe. `flow-root` makes the zone a BFC root.
    // Never `overflow: hidden` — that clips the primitive's deliberate bleed.
    expect(TILE_SRC).toMatch(/\.ex-zone\s*\{[^}]*display:\s*flow-root/s)
    expect(TILE_SRC).not.toMatch(/\.ex-zone\s*\{[^}]*overflow:\s*hidden/s)
  })

  it('themes the beam from the grid palette, at a specificity that cannot lose', () => {
    // The primitive's own `.dark .scanline` is (0,3,0); a two-class override
    // ties with it and wins only by stylesheet injection order.
    const rule = TILE_SRC.match(/([^}\n][^}]*\.ex-zone\.scanline[^{]*)\{([^}]*)\}/s)
    expect(rule, 'the override must be scoped to the zone').toBeTruthy()
    expect(rule[2]).toMatch(/--scan-core:\s*var\(--gv-blue\)/)
    expect(rule[2]).toMatch(/--scan-track:\s*var\(--gv-bar-track\)/)
    // Three classes + the scoped attribute = (0,4,0), order-independent.
    expect(rule[1]).toContain('.ex-head ~')
    // Theming belongs in the token layer, never a `.dark` selector in a
    // component (design-system contract).
    expect(TILE_SRC).not.toMatch(/\.dark\s+\.ex-zone/)
  })

  it('takes the chassis handoff explicitly rather than lying about its state', () => {
    expect(TEMPLATE).toMatch(/:owns-loading="true"/)
    // The tile must not fake `ready` to get its slot rendered — that is an
    // invisible contract the chassis cannot honour or check.
    expect(TEMPLATE).toMatch(/:state="state"/)
  })

  it('the chassis renders the slot for a tile that owns its loading face', () => {
    // Term-by-term, not one frozen literal: a pin on the exact expression text
    // goes red on any reformat, and a pin on `.it-skel` existing would go red
    // on the very follow-up meant to delete it.
    const gate = INFOTILE_SRC.match(/v-if="([^"]*)"\s+class="it-skel"/)
    expect(gate, 'the skeleton branch must still be a v-if').toBeTruthy()
    expect(gate[1]).toContain("state === 'loading'")
    expect(gate[1]).toContain('!ownsLoading')
    // error / empty still replace the slot, so a tile owns its LOADING face only.
    expect(INFOTILE_SRC).toMatch(/v-else-if="state === 'error'"/)
    expect(INFOTILE_SRC).toMatch(/v-else-if="state === 'empty'"/)
    expect(INFOTILE_SRC).toMatch(/<slot v-else><\/slot>/)
    // Default off: every tile that has not adopted keeps the chassis skeleton.
    expect(INFOTILE_SRC).toMatch(/ownsLoading:\s*\{\s*type:\s*Boolean,\s*default:\s*false\s*\}/)
  })
})
