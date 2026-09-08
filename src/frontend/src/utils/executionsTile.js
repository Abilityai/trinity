/**
 * Pure shaping for the fleet Executions tile (ent#96).
 *
 * Extracted from the component for the reason `gridLayout.js` is: the unit
 * suite runs in a node environment with no DOM, so anything that must be
 * TESTED has to be a function over data. What is left in the .vue file is
 * markup and token-driven CSS.
 *
 * The vocabulary is the backend's: bucket names come from `_TRIGGER_BUCKETS`
 * (#1107) and their stack order arrives as `trigger_order` on the response, so
 * this file never holds a second copy that could drift from the tile's legend
 * or the Overview chart (ent#96 AC1).
 */

import { CELL_W } from './gridLayout'

/** Bucket name -> the `--gv-bk-*` token that colours it. */
export const BUCKET_TOKENS = {
  'Chat/Tasks': 'man',
  MCP: 'mcp',
  Channels: 'ext',
  Public: 'public',
  Scheduled: 'sched',
  Loops: 'loops',
  Reminders: 'reminders',
  'Agent-to-agent': 'a2a',
  Voice: 'voice',
  Other: 'other',
}

/** The colour for a bucket, falling back to the catch-all's rather than none. */
export function bucketToken(name) {
  return BUCKET_TOKENS[name] || BUCKET_TOKENS.Other
}

/**
 * The stack order to render: the backend's order first, then any bucket the
 * response carries that the order does not mention.
 *
 * That second clause is the guard AC1 actually needs. A bucket added to
 * `_TRIGGER_BUCKETS` but not to `_BUCKET_ORDER` would otherwise be dropped from
 * the chart while still counting toward the column total — a stack that does
 * not add up to its own bar, which is the failure this tile exists to avoid in
 * the other direction (failures hidden inside totals).
 */
export function stackOrder(triggerOrder, buckets) {
  const order = Array.isArray(triggerOrder) ? [...triggerOrder] : []
  const seen = new Set(order)
  for (const b of buckets || []) {
    for (const name of Object.keys(b?.by_trigger || {})) {
      if (!seen.has(name)) {
        seen.add(name)
        order.push(name)
      }
    }
  }
  return order
}

/** Buckets that actually occur in this window, in stack order — the legend. */
export function presentBuckets(triggerOrder, buckets) {
  const totals = new Map()
  for (const b of buckets || []) {
    for (const [name, entry] of Object.entries(b?.by_trigger || {})) {
      const n = entry?.total || 0
      if (n > 0) totals.set(name, (totals.get(name) || 0) + n)
    }
  }
  return stackOrder(triggerOrder, buckets)
    .filter((name) => totals.has(name))
    .map((name) => ({ name, total: totals.get(name), token: bucketToken(name) }))
}

/* -------------------------------------------------------------------------
 * The tile's vertical budget (#2228)
 *
 * `.it-body` is `overflow: hidden` at a fixed `CELL_H`, so the head, the chart
 * and the legend compete for one fixed number of pixels — measured at 134. The
 * tile shipped needing ~138 of them, which is the SECOND clip behind #2228:
 * even a legend that clamps to whole rows has its last row shaved off by the
 * body if the column above it is too tall. Fixing the legend alone leaves the
 * symptom on screen.
 *
 * So the chart states its budget here, in the same module the legend's does,
 * and `.ex-chart`'s CSS height is exactly CHART_HEIGHT + COL_GAP + RAIL_HEIGHT
 * + 2px padding — the tallest a column can be and nothing more. The container
 * previously stood 4px above that, which was pure dead space paid for at the
 * legend's expense. Pinned by executionsTile.spec.js, because a stylesheet and
 * a JS constant cannot check each other.
 * ------------------------------------------------------------------------- */

/** Pixel budget for a column's stack. */
export const CHART_HEIGHT = 60
/** Pixel budget for the failure rail beneath it. */
export const RAIL_HEIGHT = 6
/** `.ex-col` gap between the stack and its rail. */
export const COL_GAP = 2
/** `.ex-chart` padding-bottom. */
export const CHART_PAD = 2

/**
 * One rendered column per bucket.
 *
 * `chartHeight` is the pixel budget for the STACK; the failure rail is drawn
 * beneath it and has its own budget, so a column with many failures never
 * steals height from the runs above it.
 *
 * Segments are sized against the tallest column, not against their own total,
 * so the 24 bars are comparable — the point of the chart. A non-zero segment
 * floors at 1px: rounding a real execution to nothing would show an empty hour
 * that was not empty.
 */
export function chartColumns(buckets, { triggerOrder = [], chartHeight = CHART_HEIGHT, railHeight = RAIL_HEIGHT } = {}) {
  const rows = Array.isArray(buckets) ? buckets : []
  const order = stackOrder(triggerOrder, rows)
  const maxTotal = Math.max(1, ...rows.map((b) => b?.total || 0))
  const maxFailed = Math.max(1, ...rows.map((b) => b?.failed || 0))

  return rows.map((b) => {
    const by = b?.by_trigger || {}
    const segments = order
      .map((name) => {
        const entry = by[name] || {}
        const total = entry.total || 0
        if (!total) return null
        return {
          name,
          token: bucketToken(name),
          total,
          failed: entry.failed || 0,
          px: Math.max(1, Math.round((total / maxTotal) * chartHeight)),
        }
      })
      .filter(Boolean)
    const failed = b?.failed || 0
    return {
      bucket: b?.bucket || '',
      hour: hourLabel(b?.bucket),
      total: b?.total || 0,
      failed,
      segments,
      // The failure rail: its own scale, so one failure in a quiet hour is
      // still visible next to a busy hour's five. It encodes PRESENCE and
      // relative size, never a share of the column.
      failPx: failed ? Math.max(2, Math.round((failed / maxFailed) * railHeight)) : 0,
      // A zero hour renders the faint baseline stub the AgentTile charts use,
      // so "no executions" reads as data rather than as a gap (#96 tech notes).
      stub: !(b?.total || 0),
    }
  })
}

/** `2026-08-14T09` -> `09` (UTC hour label; the axis is UTC end to end). */
export function hourLabel(bucket) {
  if (typeof bucket !== 'string') return ''
  const at = bucket.indexOf('T')
  return at >= 0 ? bucket.slice(at + 1, at + 3) : bucket
}

/**
 * Headline numbers for the window.
 *
 * `successRate` is TERMINAL-based (success / (success + failed)) and `null`
 * when nothing terminated — the #1107 convention, so a window of purely
 * still-running work reports "—" rather than a false 0%.
 */
export function headline(buckets) {
  const rows = Array.isArray(buckets) ? buckets : []
  let total = 0
  let success = 0
  let failed = 0
  for (const b of rows) {
    total += b?.total || 0
    success += b?.success || 0
    failed += b?.failed || 0
  }
  const terminal = success + failed
  return {
    total,
    success,
    failed,
    successRate: terminal ? Math.round((success / terminal) * 100) : null,
  }
}

/**
 * The tile's state, in the shape `InfoTile` renders.
 *
 * `loaded` is the only thing that can produce a data state; an error before the
 * first success is an ERROR, not an empty fleet. "No executions in 24h" is a
 * positive claim about the fleet and requires a successful read to make — the
 * manufactured-green rule ent#100 established (principle 15 / #1926).
 */
export function tileState({ loaded, error, buckets }) {
  if (error && !loaded) return 'error'
  if (!loaded) return 'loading'
  const total = headline(buckets).total
  return total ? 'ready' : 'empty'
}

/**
 * The chart zone's `ScanlineReveal` props, derived from the state above
 * (trinity-enterprise#449, design-system §6).
 *
 * `loading` is "no data yet", NEVER "a request is in flight": `tileState` is
 * `'loading'` only before the first successful read, and the store's
 * `execTimelineLoaded` latch is written `= true` on that success and never
 * written false — so the 60s batch refresh can never re-raise the beam.
 *
 * `reveal` is the celebration of DATA arriving, so only `'ready'` earns it: an
 * error or an empty window snaps to loaded with no pass. (Those two terminals
 * also replace the slot with the chassis message, so the zone is unmounted
 * there — the prop is the belt.)
 *
 * Total on unknown input: an unrecognised state renders the loaded face, which
 * is the direction that cannot strand a tile behind a permanent beam.
 */
export function scanlineProps(state) {
  return { loading: state === 'loading', reveal: state === 'ready' }
}

/**
 * The headline's rendered face (trinity-enterprise#449).
 *
 * The headline sits OUTSIDE the scanline zone — the first frame of the reveal
 * wipe clips all slot content to nothing, so a headline inside it would blink
 * at arrival. Being outside, it renders during loading and must therefore have
 * a loading face of its own: `—`, never `0 runs`, which is a claim about the
 * fleet made before anything has been read (the ent#100 manufactured-green
 * rule, applied to the number rather than to the empty state).
 *
 * `failed` stays 0 while loading for the same reason — the segment is rendered
 * only for a non-zero count, and "— failed" would assert failures exist.
 * `ok` keeps `headline`'s terminal-based rule: nothing terminated → `—`, not 0%.
 */
export function headlineFace(head, state) {
  const loading = state === 'loading'
  const rate = head?.successRate
  return {
    total: loading ? '—' : String(head?.total || 0),
    ok: loading || rate === null || rate === undefined ? '—' : `${rate}%`,
    failed: loading ? 0 : head?.failed || 0,
  }
}

/* -------------------------------------------------------------------------
 * Legend fitting (#2228)
 *
 * The legend shipped as a wrapping flex row clamped by `max-height: 26px` with
 * `overflow: hidden`. 26px is not a whole number of legend rows, so the clamp
 * landed *inside* row two and sliced it through the glyphs — and whatever fell
 * past it vanished with no indication, leaving colours in the chart with no
 * decoder anywhere on the tile.
 *
 * Both halves are fixed here rather than in CSS alone, because CSS can clamp
 * but cannot say WHAT it dropped. The clamp pins a whole number of rows; this
 * packer decides which keys occupy them and hands the remainder to a `+N` chip.
 *
 * The width model is deliberately PESSIMISTIC, and the invariant it maintains
 * is per KEY, not in aggregate: the estimate must be >= what the browser
 * actually renders for every name. Only then is the packer's row assignment a
 * subset of what the browser fits, so the browser can never need a row the
 * packer did not plan for. An aggregate-only margin is not enough — greedy
 * packing wastes space at the end of each row, so a model that is generous
 * overall but tight on one name still lets a real third row form, which the
 * clamp then eats silently. That is precisely the defect this removes.
 *
 * The two directions are not symmetric, which is why the bias is one-sided:
 * over-reserving shows a `+N` that was not strictly necessary, under-reserving
 * loses keys with no trace.
 *
 * Calibration, measured in the running app (Chromium, macOS system sans, 10px,
 * 334px rows) rather than estimated: every label renders between 3.4 and 5.5
 * px/char, so the model clears all eleven with 18-59% of headroom — enough to
 * absorb a different platform font. Observed fleets sit around eight keys and
 * fit with room to spare.
 *
 * The browser does in fact wrap the complete ten-bucket vocabulary plus
 * `failed` into two rows; the model, being conservative, shows `+2` there. That
 * gap is accepted rather than tuned away: closing it means calibrating to
 * within a couple of percent of one browser's metrics on one platform, and the
 * penalty for guessing low is a silently eaten row. The keys it gives up are
 * the last in stack order — `Other` and `failed` — which is the right end to
 * lose from: the headline already states the failure count in red just above,
 * and the chip names both.
 * ------------------------------------------------------------------------- */

/** Rows the legend may occupy. The CSS clamp MUST agree; pinned by spec. */
export const LEGEND_ROWS = 2

/**
 * Usable legend width in px.
 *
 * Derived from the lattice cell rather than hardcoded, so a change to CELL_W
 * moves the packer with the tile it describes instead of leaving a stale
 * literal behind.
 *
 * 50 = InfoTile's horizontal padding (30 left + 16 right) plus its 1px borders,
 * rounded up. The padding alone gives 46, which measures 4px WIDER than the
 * element really is — an error in the one direction the model must never take,
 * since believing there is more room than exists is how a third row forms. The
 * live tile reports a 334px content box, which is exactly CELL_W - 50.
 */
export const LEGEND_WIDTH = CELL_W - 50

// Per-key geometry, matching the `.ex-key` rule in ExecutionsTile.vue.
const DOT_W = 6 //     `.ex-dot` width
const DOT_GAP = 3 //   `.ex-key` gap between dot and label
const KEY_GAP = 8 //   `.ex-legend` column-gap
const CHIP_W = 50 //   the `+N` chip at its widest (three dots + count + padding)
const CHAR_PX = 5.7 // per-char estimate at font-size 10px
const KEY_PAD = 4 //   flat per-key allowance, see below

/**
 * Estimated rendered width of one legend key at `font-size: 10px`.
 *
 * 5.7px/char sits above the measured per-char cost of every name in the
 * vocabulary, which ranges from 3.4 (`failed`) to 5.5 (`MCP`). `KEY_PAD` covers
 * the direction a flat per-char figure is weakest in: short all-caps names,
 * where every glyph is a wide one and there are too few characters for the
 * per-char margin to absorb the difference. Paying that as a constant is
 * cheaper than inflating CHAR_PX, which would tax every long name for a problem
 * only the short ones have.
 *
 * Zoom is a transform on the whole lattice, so the model is zoom-invariant.
 */
export function keyWidth(name) {
  return DOT_W + DOT_GAP + KEY_PAD + String(name || '').length * CHAR_PX
}

/** The full legend key list: present buckets in stack order, then `failed`. */
export function legendKeys(triggerOrder, buckets) {
  const keys = presentBuckets(triggerOrder, buckets)
  const failed = headline(buckets).failed
  if (failed) keys.push({ name: 'failed', total: failed, token: null, fail: true })
  return keys
}

function pack(keys, { width, rows, reserveChip }) {
  const shown = []
  let row = 0
  let used = 0
  let i = 0

  while (i < keys.length) {
    const key = keys[i]
    const lastRow = row === rows - 1
    // The chip shares the last row, so its width comes out of that budget only.
    const budget = lastRow && reserveChip ? Math.max(0, width - KEY_GAP - CHIP_W) : width
    const w = keyWidth(key.name)
    const need = used ? used + KEY_GAP + w : w

    // `used === 0` force-places a key wider than a whole row: CSS clips it, and
    // a key that fits nowhere must never spin the packer.
    if (need <= budget || used === 0) {
      shown.push(key)
      used = need
      i += 1
      continue
    }
    if (lastRow) return { shown, hidden: keys.slice(i) }
    row += 1
    used = 0
  }
  return { shown, hidden: [] }
}

/**
 * Split the legend into what fits and what does not.
 *
 * Two passes on purpose: the chip only exists if something is hidden, so
 * reserving room for it up front would truncate sets that fit without it.
 */
export function legendFit(keys, { width = LEGEND_WIDTH, rows = LEGEND_ROWS } = {}) {
  const list = Array.isArray(keys) ? keys : []
  const first = pack(list, { width, rows, reserveChip: false })
  if (!first.hidden.length) return first
  return pack(list, { width, rows, reserveChip: true })
}
