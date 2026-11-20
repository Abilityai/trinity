/**
 * Agent Detail Overview shaping (trinity-enterprise#1317).
 *
 * The rules a new agent's page rests on: a value nobody has measured is `—`,
 * a day with no runs is a gap, a rate carries its denominator.
 */
import { describe, expect, it } from 'vitest'
import {
  CHART_HEIGHT, DASH,
  activityColumns, activityReadout, attentionCount, healthBlocks,
  firstLine, hoverText, linePaths, measureTiles, monotonePath, sourceSplit, nextRun, nowState, recentRows, runTitle, scheduleRows,
} from '../../src/utils/overviewShape'

const NOW = Date.parse('2026-11-20T12:00:00Z')
const iso = (minsAgo) => new Date(NOW - minsAgo * 60000).toISOString()

const EMPTY = {
  total_executions: 0, success_count: 0, failed_count: 0, success_rate: 0,
  duration_ms: { avg: null, p95: null }, context_avg: null, context_pct_avg: null,
  buckets: [], by_type: [],
  timeline: [
    { date: '2026-11-18', total: 0, success: 0, failed: 0, success_rate: null, duration_avg_ms: null, context_avg: null, context_pct_avg: null, by_type: {} },
    { date: '2026-11-19', total: 0, success: 0, failed: 0, success_rate: null, duration_avg_ms: null, context_avg: null, context_pct_avg: null, by_type: {} },
    { date: '2026-11-20', total: 0, success: 0, failed: 0, success_rate: null, duration_avg_ms: null, context_avg: null, context_pct_avg: null, by_type: {} },
  ],
}

const BUSY = {
  total_executions: 11, success_count: 9, failed_count: 1, success_rate: 0.9,
  duration_ms: { avg: 134000, p95: 400000 }, context_avg: 76000, context_pct_avg: 38,
  buckets: ['Chat/Tasks', 'Scheduled'],
  timeline: [
    { date: '2026-11-18', total: 10, success: 9, failed: 1, success_rate: 0.9, duration_avg_ms: 120000, context_avg: 70000, context_pct_avg: 35, by_type: { Scheduled: 6, 'Chat/Tasks': 4 } },
    { date: '2026-11-19', total: 0, success: 0, failed: 0, success_rate: null, duration_avg_ms: null, context_avg: null, context_pct_avg: null, by_type: {} },
    { date: '2026-11-20', total: 1, success: 0, failed: 0, success_rate: null, duration_avg_ms: null, context_avg: 90000, context_pct_avg: 61, by_type: { 'Chat/Tasks': 1 } },
  ],
}

describe('measure tiles', () => {
  it('a window with no runs shows a dash on every tile, never 0%', () => {
    const tiles = measureTiles(EMPTY, { never: true })
    expect(tiles.map((t) => t.value)).toEqual([DASH, DASH, DASH])
    expect(tiles.map((t) => t.sub)).toEqual(['after the first run', 'after the first run', 'after the first run'])
    expect(JSON.stringify(tiles)).not.toMatch(/0%/)
  })

  it('an agent that has run before, with none in this window, is not told it never ran', () => {
    expect(measureTiles(EMPTY).map((t) => t.sub)).toEqual(Array(3).fill('no runs in this window'))
    expect(activityReadout(EMPTY, null).text).toBe('No runs in this window.')
  })

  it('a context share above the window is shown as full, not over 100%', () => {
    const context = measureTiles({ ...BUSY, context_pct_avg: 104, timeline: [{ ...BUSY.timeline[0], context_pct_avg: 131 }] })[2]
    expect(context.value).toBe('100%')
    expect(context.series).toEqual([100])
  })

  it('the finished rate carries its denominator', () => {
    const [finished] = measureTiles(BUSY)
    expect(finished.value).toBe('90%')
    expect(finished.sub).toBe('9 of 10 runs')
  })

  it('runs that have not finished yet give a dash, not 0%', () => {
    const [finished] = measureTiles({ ...EMPTY, total_executions: 2 })
    expect(finished.value).toBe(DASH)
    expect(finished.sub).toBe('no run has finished yet')
  })

  it('context is a percentage with the 80% guide when the API has one', () => {
    const context = measureTiles(BUSY)[2]
    expect(context.value).toBe('38%')
    expect(context.guide).toBe(80)
    expect(context.sub).toContain('busiest day 61%')
    expect(context.series).toEqual([35, null, 61])
  })

  it('context falls back to tokens for rows with no recorded window', () => {
    const context = measureTiles({ ...BUSY, context_pct_avg: null })[2]
    expect(context.value).toBe('76.0k')
    expect(context.guide).toBeNull()
  })

  it('a day with no runs is null in every series, not zero', () => {
    for (const tile of measureTiles(BUSY)) expect(tile.series[1]).toBeNull()
  })
})

describe('line paths', () => {
  it('breaks the line across an unmeasured day', () => {
    const { line, area } = linePaths([10, 20, null, 30, 40])
    expect(line.match(/M/g)).toHaveLength(2)
    expect(area.match(/Z/g)).toHaveLength(2)
  })

  it('has nothing to draw when nothing was measured', () => {
    expect(linePaths([null, null])).toMatchObject({ line: '', area: '', last: null })
  })

  it('puts the end marker on the last measured point', () => {
    const { last } = linePaths([0, 100, null], { lo: 0, hi: 100, height: 48, pad: 4 })
    expect(last.xPct).toBe(50)
    expect(last.y).toBe(4)
  })
})

describe('activity chart', () => {
  // Every y the path visits, control points included.
  const ys = (d) => [...d.matchAll(/(-?[\d.]+) (-?[\d.]+)/g)].map((m) => Number(m[2]))

  it('scales to the busiest day and stays inside the plot', () => {
    const { max, columns, line, area } = activityColumns(BUSY)
    expect(max).toBe(10)
    expect(columns.map((c) => c.y)).toEqual([22, CHART_HEIGHT, expect.any(Number)])
    expect(columns[2].y).toBeGreaterThan(22)
    expect(columns[2].y).toBeLessThan(CHART_HEIGHT)
    expect(Math.min(...ys(line))).toBeGreaterThanOrEqual(22)
    expect(Math.max(...ys(line))).toBeLessThanOrEqual(CHART_HEIGHT)
    expect(area.endsWith('Z')).toBe(true)
    expect(columns[0].segments).toEqual([{ bucket: 'Chat/Tasks', count: 4 }, { bucket: 'Scheduled', count: 6 }])
  })

  it('the curve never overshoots a spike: no dip under zero, no rise above the peak', () => {
    const d = monotonePath([[0, 100], [10, 100], [20, 0], [30, 100], [40, 90], [50, 100]])
    expect(Math.min(...ys(d))).toBe(0)
    expect(Math.max(...ys(d))).toBe(100)
    expect(monotonePath([[0, 1]])).toBe('')
  })

  it('failures sit on the same scale as runs, each day centred in its slot', () => {
    const tl = (total, failed) => ({ date: '2026-11-20', total, failed, by_type: { A: total } })
    const { columns } = activityColumns({ buckets: [], timeline: [tl(10, 0), tl(10, 5), tl(10, 10), tl(4, 1)] })
    expect(columns.map((c) => c.xPct)).toEqual([12.5, 37.5, 62.5, 87.5])
    expect(columns[2].failY).toBe(columns[2].y)
    expect(columns[1].failY).toBe((22 + CHART_HEIGHT) / 2)
    expect(columns[0].failY).toBe(CHART_HEIGHT)
  })

  it('the failure curve is drawn only around failing days, not along a clean baseline', () => {
    const tl = (failed) => ({ date: '2026-11-20', total: 9, failed, by_type: { A: 9 } })
    const starts = (d) => (d.match(/M/g) || []).length
    expect(activityColumns({ buckets: [], timeline: [tl(0), tl(0), tl(0)] }).failLine).toBe('')
    // two separate failing stretches are two strokes; neighbours join into one
    expect(starts(activityColumns({ buckets: [], timeline: [tl(1), tl(0), tl(0), tl(2), tl(1), tl(0)] }).failLine)).toBe(2)
    const one = activityColumns({ buckets: [], timeline: [tl(0), tl(0), tl(2), tl(0), tl(0)] })
    // it starts one day before and ends one day after the failure, at zero
    expect(one.failLine.startsWith(`M300 ${CHART_HEIGHT}`)).toBe(true)
    expect(one.failLine.endsWith(`700 ${CHART_HEIGHT}`)).toBe(true)
  })

  it('a window with no runs has a flat line on the baseline', () => {
    const { max, columns, failLine } = activityColumns(EMPTY)
    expect(max).toBe(0)
    expect(columns.every((c) => c.y === CHART_HEIGHT)).toBe(true)
    expect(failLine).toBe('')
  })

  it('what started the runs is listed largest first with shares of the window', () => {
    const stats = { by_type: [{ bucket: 'Chat/Tasks', total: 6 }, { bucket: 'Scheduled', total: 28 }, { bucket: 'MCP', total: 0 }, { bucket: 'Channels', total: 6 }] }
    expect(sourceSplit(stats)).toEqual([
      { bucket: 'Scheduled', count: 28, pct: 70 },
      { bucket: 'Chat/Tasks', count: 6, pct: 15 },
      { bucket: 'Channels', count: 6, pct: 15 },
    ])
    expect(sourceSplit(EMPTY)).toEqual([])
    expect(sourceSplit(null)).toEqual([])
  })

  it('draws a bucket the backend order did not list', () => {
    const stats = { buckets: [], timeline: [{ date: '2026-11-20', total: 2, by_type: { Mystery: 2 } }] }
    expect(activityColumns(stats).columns[0].segments[0].bucket).toBe('Mystery')
  })

  it('the readout summarises the window at rest and one day on hover', () => {
    expect(activityReadout(BUSY, null)).toEqual({ head: '11', text: 'runs · 1 failed · busiest Wed, Nov 18 (10)' })
    const { columns } = activityColumns(BUSY)
    expect(activityReadout(BUSY, columns[0]).text).toBe('Wed, Nov 18 · 4 Chat/Tasks · 6 Scheduled · 1 failed')
    expect(activityReadout(BUSY, columns[1])).toEqual({ head: '0', text: 'Thu, Nov 19 · no runs' })
  })

  it('an empty window says what will appear', () => {
    expect(activityReadout(EMPTY, null, { never: true }).text).toMatch(/Nothing has run yet/)
  })
})

describe('now', () => {
  it('a stopped agent is stopped, whatever else is known', () => {
    expect(nowState({ agentStatus: 'stopped', live: { running_count: 2 } }).kind).toBe('stopped')
  })

  it('names the running task and how long it has run', () => {
    const state = nowState({
      agentStatus: 'running', now: NOW, live: { running_count: 3, queued_count: 0 },
      recent: [{ id: 'e1', status: 'running', message: 'Weekly review', started_at: iso(4) }],
    })
    expect(state).toMatchObject({ kind: 'working', title: 'Working on “Weekly review”', detail: '4m 00s · +2 more', executionId: 'e1' })
  })

  it('says only "Working" when the running task is not on the recent list', () => {
    const state = nowState({ agentStatus: 'running', live: { running_count: 1 }, recent: [{ status: 'success', started_at: iso(9) }] })
    expect(state).toMatchObject({ kind: 'working', title: 'Working', detail: '' })
  })

  it('queued work with nothing running', () => {
    expect(nowState({ agentStatus: 'running', live: { running_count: 0, queued_count: 2 } }).title).toBe('2 tasks queued')
  })

  it('a new agent is waiting for its first run', () => {
    expect(nowState({ agentStatus: 'running', live: { running_count: 0, queued_count: 0 }, stats: EMPTY, recent: [] }).kind).toBe('first')
  })

  it('does not claim "first run" before the analytics read has landed', () => {
    expect(nowState({ agentStatus: 'running', stats: null, recent: [] }).kind).toBe('idle')
  })

  it('idle says when it last ran', () => {
    const state = nowState({ agentStatus: 'running', now: NOW, stats: BUSY, recent: [{ status: 'success', started_at: iso(40), completed_at: iso(38) }] })
    expect(state).toMatchObject({ kind: 'idle', detail: 'last ran 38m ago' })
  })
})

describe('next run', () => {
  it('is the soonest enabled schedule', () => {
    const next = nextRun([
      { name: 'later', enabled: true, next_run_at: new Date(NOW + 3 * 3600000).toISOString() },
      { name: 'soon', enabled: true, next_run_at: new Date(NOW + 48 * 60000).toISOString() },
      { name: 'off', enabled: false, next_run_at: new Date(NOW + 60000).toISOString() },
    ], NOW)
    expect(next).toMatchObject({ name: 'soon', label: 'in 48m' })
  })

  it('is null with no schedule to wait for', () => {
    expect(nextRun([], NOW)).toBeNull()
    expect(nextRun([{ name: 'off', enabled: false, next_run_at: iso(-5) }], NOW)).toBeNull()
  })
})

describe('what a run is called', () => {
  const PROMPT = '\n\n  Review the Q4 pipeline  \nYou are a careful analyst.\n\n- step one\n- step two'

  it('is the first non-empty line of a long multi-line prompt', () => {
    expect(firstLine(PROMPT)).toBe('Review the Q4 pipeline')
    expect(runTitle({ message: PROMPT })).toMatchObject({ title: 'Review the Q4 pipeline', detail: '' })
  })

  it('keeps the whole prompt for hover, on one line and capped', () => {
    expect(hoverText(PROMPT)).toBe('Review the Q4 pipeline You are a careful analyst. - step one - step two')
    const long = hoverText('x'.repeat(900))
    expect(long).toHaveLength(400)
    expect(long.endsWith('…')).toBe(true)
  })

  it('a scheduled run is known by its schedule, with the command beneath', () => {
    const schedules = [{ id: 's1', name: 'morning-digest' }]
    expect(runTitle({ message: '/digest --full', schedule_id: 's1' }, schedules)).toMatchObject({ title: 'morning-digest', detail: '/digest --full' })
    // an id that matches no schedule (a manual run's placeholder) falls back to the prompt
    expect(runTitle({ message: 'Hello', schedule_id: '__manual__' }, schedules).title).toBe('Hello')
  })

  it('a run with no message says so', () => {
    expect(runTitle({ message: '' }).title).toBe('(no message)')
  })

  it('the Now row uses the same title, with the whole prompt kept for hover', () => {
    const state = nowState({ agentStatus: 'running', recent: [{ id: 'e', status: 'running', message: PROMPT, started_at: iso(1) }] })
    expect(state.title).toBe('Working on “Review the Q4 pipeline”')
    expect(state.full).toMatch(/careful analyst/)
  })
})

describe('lists', () => {
  it('recent rows carry status, age, duration and what started them', () => {
    const rows = recentRows([
      { id: 'a', status: 'running', message: 'Now', started_at: iso(4), triggered_by: 'schedule' },
      { id: 'b', status: 'failed', message: '', started_at: iso(180), duration_ms: 48000, triggered_by: 'mcp' },
    ], NOW)
    expect(rows[0]).toMatchObject({ status: 'running', when: '4m ago', took: 'in progress', via: 'schedule' })
    expect(rows[1]).toMatchObject({ status: 'failed', title: '(no message)', when: '3h ago', took: '48.0s' })
  })

  it('a schedule row states its outcome over the window, with the denominator', () => {
    const [row, idle, paused] = scheduleRows(
      { schedules: [
        { schedule_id: 's1', name: 'digest', cron_expression: '0 9 * * 1-5', enabled: true, success_count: 9, failed_count: 1, avg_duration_ms: 112000 },
        { schedule_id: 's2', name: 'new', cron_expression: '0 * * * *', enabled: true, success_count: 0, failed_count: 0, avg_duration_ms: null },
        { schedule_id: 's3', name: 'off', cron_expression: '0 * * * *', enabled: false, success_count: 0, failed_count: 0, avg_duration_ms: null },
      ] },
      [{ id: 's1', enabled: true, next_run_at: new Date(NOW + 48 * 60000).toISOString() }, { id: 's3', enabled: false, next_run_at: null }],
      { now: NOW, windowLabel: '14d' },
    )
    expect(row).toMatchObject({ next: 'in 48m', score: '9 of 10 finished in 14d', okPct: 90, failed: 1, hasOutcome: true, avg: '1m 52s' })
    expect(idle).toMatchObject({ next: DASH, score: 'no finished runs yet', hasOutcome: false, avg: DASH })
    expect(paused.next).toBe('paused')
  })
})

describe('health blocks', () => {
  it('is null with no checks, so the card can say monitoring is off', () => {
    expect(healthBlocks([])).toBeNull()
  })

  it('covers the span the checks cover and labels it', () => {
    const checks = []
    for (let m = 0; m <= 480; m += 10) checks.push({ checked_at: iso(m), reachable: !(m >= 200 && m < 230) })
    const out = healthBlocks(checks, { blocks: 24, now: NOW })
    expect(out.spanLabel).toBe('8h ago')
    expect(out.blocks).toHaveLength(24)
    const states = new Set(out.blocks.map((b) => b.state))
    expect(states.has('ok')).toBe(true)
    expect(states.has('down') || states.has('degraded')).toBe(true)
  })
})

it('a monitor that has only just started gets one block per check, packed against now', () => {
  const out = healthBlocks([{ checked_at: iso(1), reachable: true }, { checked_at: iso(2), reachable: false }], { blocks: 24, now: NOW })
  expect(out.blocks.map((b) => b.state)).toEqual([...Array(22).fill('none'), 'down', 'ok'])
  expect(out.spanLabel).toBe('2m ago')
})

it('attention counts notifications, asks and failed syncs', () => {
  expect(attentionCount({ notifications: 2, asks: 140, syncFailures: 0 })).toBe(142)
  expect(attentionCount()).toBe(0)
})
