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
export function chartColumns(buckets, { triggerOrder = [], chartHeight = 64, railHeight = 6 } = {}) {
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
