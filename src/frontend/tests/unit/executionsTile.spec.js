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
import {
  BUCKET_TOKENS,
  bucketToken,
  chartColumns,
  headline,
  hourLabel,
  presentBuckets,
  stackOrder,
  tileState,
} from '@/utils/executionsTile'

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
