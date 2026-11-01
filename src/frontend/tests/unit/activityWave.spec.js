import { describe, it, expect } from 'vitest'
import {
  WAVE_W, WAVE_H, WAVE_PAD, BEATS_PER_DAY, SCALE_FLOOR, SEG_EXT, SOFT, FALLBACK_COLOR,
  WAVE_REVEAL_FALLBACK_MS, REVEAL_MS, REVEAL_STAGGER_MS, revealFallbackMs,
  monotone, layoutWave, idleBeats,
} from '@/utils/activityWave'

/**
 * trinity-enterprise#968 — the geometry behind the Workspace band's activity
 * wave (`components/portal/ActivityWave.vue`). Pure on purpose: every rule
 * about how a week becomes beats lives here, where a node test can execute it,
 * and the SFC only binds what comes back.
 */

const COLORS = {
  'Chat/Tasks': '#6366f1', 'MCP': '#a78bfa', 'Channels': '#60a5fa', 'Public': '#38bdf8',
  'Scheduled': '#22d3ee', 'Loops': '#e879f9', 'Agent-to-agent': '#2dd4bf', 'Voice': '#34d399',
  'Other': '#94a3b8',
}
const ORDER = Object.keys(COLORS)

function day(date, by_type = {}) {
  const total = Object.values(by_type).reduce((a, b) => a + b, 0)
  return { date, total, by_type }
}

// The live `7d` payload: the backend walks from the window's start DATE
// (168h back) through today inclusive — EIGHT points, not seven
// (`db/schedules/analytics.py`, the gap-filled timeline).
const WEEK = [
  day('2026-10-15', { 'Chat/Tasks': 3 }),
  day('2026-10-16', { 'Chat/Tasks': 8, 'Scheduled': 4, 'MCP': 2 }),
  day('2026-10-17', { 'Chat/Tasks': 12, 'Scheduled': 6, 'MCP': 3, 'Channels': 1 }),
  day('2026-10-18', { 'Chat/Tasks': 4, 'Scheduled': 4, 'Loops': 1 }),
  day('2026-10-19', { 'Chat/Tasks': 18, 'Scheduled': 9, 'MCP': 4 }),
  day('2026-10-20', {}),
  day('2026-10-21', { 'Chat/Tasks': 9, 'Scheduled': 6, 'MCP': 3 }),
  day('2026-10-22', { 'Scheduled': 4, 'Chat/Tasks': 2 }),
]

describe('constants', () => {
  it('match the band budget and the approved prototype', () => {
    expect(WAVE_W).toBe(416)
    expect(WAVE_H).toBe(24)
    expect(WAVE_PAD).toBe(8)
    expect(BEATS_PER_DAY).toBe(4)
    expect(SCALE_FLOOR).toBe(10)
    expect(SEG_EXT).toBe(4)
    expect(SOFT).toBe(1.8)
    // the reveal is ~1.5s end to end for a week (750ms + 24ms × last beat); the
    // fallback must outlive it, unlike the scanline's 700ms — for ANY beat count
    expect(WAVE_REVEAL_FALLBACK_MS).toBeGreaterThan(1400)
    expect(revealFallbackMs(32)).toBeGreaterThan(REVEAL_MS + REVEAL_STAGGER_MS * 31)
    expect(revealFallbackMs(14 * 4)).toBeGreaterThan(REVEAL_MS + REVEAL_STAGGER_MS * 55)
    expect(revealFallbackMs(0)).toBe(WAVE_REVEAL_FALLBACK_MS)
  })
})

describe('monotone (Fritsch–Carlson) envelope', () => {
  const knots = (ys) => ys.map((y, i) => ({ x: i * 10, y }))

  it('passes through its knots', () => {
    const { at } = monotone(knots([0, 0.5, 1, 0.2]))
    expect(at(0)).toBeCloseTo(0)
    expect(at(10)).toBeCloseTo(0.5)
    expect(at(20)).toBeCloseTo(1)
    expect(at(30)).toBeCloseTo(0.2)
  })

  it('never overshoots: a single spike stays within [0, 1] everywhere', () => {
    const { at } = monotone(knots([0, 0, 0, 1, 0, 0, 0]))
    for (let x = 0; x <= 60; x += 0.5) {
      expect(at(x)).toBeGreaterThanOrEqual(-1e-9)
      expect(at(x)).toBeLessThanOrEqual(1 + 1e-9)
    }
  })

  it('is flat on a flat week', () => {
    const { at, path } = monotone(knots([0, 0, 0, 0]))
    for (let x = 0; x <= 30; x += 3) expect(at(x)).toBeCloseTo(0)
    expect(path.startsWith('M0 0')).toBe(true)
  })

  it('clamps outside the knot range instead of extrapolating', () => {
    const { at } = monotone(knots([0.2, 0.9]))
    expect(at(-50)).toBeCloseTo(0.2)
    expect(at(500)).toBeCloseTo(0.9)
  })

  it('returns a bezier path with one segment per gap', () => {
    const { path } = monotone(knots([0, 1, 0]))
    expect(path.match(/ C/g)).toHaveLength(2)
  })
})

describe('layoutWave — days', () => {
  it('takes the day count from the payload (7d is eight points)', () => {
    const w = layoutWave({ data: WEEK, buckets: ORDER, colors: COLORS })
    expect(w.n).toBe(8)
    expect(w.days).toHaveLength(8)
    expect(w.beats).toHaveLength(8 * BEATS_PER_DAY)
    expect(w.empty).toBe(false)
  })

  it('gives every day a full-height hit band that tiles the chart edge to edge', () => {
    const w = layoutWave({ data: WEEK, buckets: ORDER, colors: COLORS })
    expect(w.days[0].x).toBe(WAVE_PAD)
    const last = w.days[7]
    expect(last.x + last.w).toBeCloseTo(WAVE_W - WAVE_PAD)
    for (let i = 1; i < 8; i++) expect(w.days[i].x).toBeCloseTo(w.days[i - 1].x + w.days[i - 1].w)
  })

  it('lists a day’s sources in bucket order with their counts and colours', () => {
    const w = layoutWave({ data: WEEK, buckets: ORDER, colors: COLORS })
    expect(w.days[4].segments.map((s) => [s.bucket, s.n, s.color])).toEqual([
      ['Chat/Tasks', 18, '#6366f1'], ['MCP', 4, '#a78bfa'], ['Scheduled', 9, '#22d3ee'],
    ])
    expect(w.days[4].total).toBe(31)
    expect(w.days[5].segments).toEqual([])
    expect(w.days[5].total).toBe(0)
  })

  it('falls back to slate for a bucket the colour map does not know', () => {
    const w = layoutWave({ data: [day('2026-10-22', { 'Telepathy': 5 })], buckets: ['Telepathy'], colors: COLORS })
    expect(w.days[0].segments[0].color).toBe(FALLBACK_COLOR)
  })

  it('keeps runs the buckets do not account for visible as a slate remainder', () => {
    const d = { date: '2026-10-22', total: 10, by_type: { 'Chat/Tasks': 6 } }
    const w = layoutWave({ data: [d], buckets: ORDER, colors: COLORS })
    expect(w.days[0].segments.map((s) => [s.bucket, s.n])).toEqual([['Chat/Tasks', 6], ['Other', 4]])
    expect(w.days[0].segments[1].color).toBe(COLORS.Other)
  })

  it('folds the remainder into an existing Other bucket rather than listing Other twice', () => {
    const d = { date: '2026-10-22', total: 10, by_type: { 'Chat/Tasks': 6, 'Other': 1 } }
    const w = layoutWave({ data: [d], buckets: ORDER, colors: COLORS })
    expect(w.days[0].segments.map((s) => [s.bucket, s.n])).toEqual([['Chat/Tasks', 6], ['Other', 4]])
  })

  it('renders a resting wave, never nothing, for an empty or missing payload', () => {
    for (const data of [[], undefined, null, WEEK.map((d) => day(d.date))]) {
      const w = layoutWave({ data, buckets: ORDER, colors: COLORS })
      expect(w.empty).toBe(true)
      expect(w.beats.length).toBeGreaterThan(0)
      for (const b of w.beats) expect(b.h).toBeCloseTo(b.w) // every beat is a dot
    }
  })
})

describe('layoutWave — beats', () => {
  const w = layoutWave({ data: WEEK, buckets: ORDER, colors: COLORS })

  it('places beats strictly left to right inside the padding, four per day', () => {
    let prev = -Infinity
    for (const b of w.beats) {
      expect(b.cx).toBeGreaterThan(prev)
      expect(b.x).toBeGreaterThanOrEqual(WAVE_PAD)
      expect(b.x + b.w).toBeLessThanOrEqual(WAVE_W - WAVE_PAD + 1e-9)
      prev = b.cx
    }
    expect(w.beats.filter((b) => b.day === 4)).toHaveLength(4)
  })

  it('mirrors every beat around the centre line and keeps it inside the chart', () => {
    for (const b of w.beats) {
      expect(b.y + b.h / 2).toBeCloseTo(WAVE_H / 2, 1) // y and h are each rounded to 0.01
      expect(b.y).toBeGreaterThanOrEqual(1 - 1e-9)
      expect(b.y + b.h).toBeLessThanOrEqual(WAVE_H - 1 + 1e-9)
      expect(b.h).toBeGreaterThanOrEqual(b.w - 1e-9) // never thinner than a dot
      expect(b.r).toBeCloseTo(b.w / 2)
      expect(b.clip.startsWith('M')).toBe(true)
      expect(b.clip.endsWith('z')).toBe(true)
    }
  })

  it('is tallest on the busiest day and a dot on the empty one', () => {
    const tallest = Math.max(...w.beats.map((b) => b.h))
    expect(w.beats.filter((b) => b.day === 4).some((b) => b.h === tallest)).toBe(true)
    for (const b of w.beats.filter((b) => b.day === 5)) expect(b.h).toBeLessThan(tallest / 3)
  })

  it('stacks a beat’s sources bottom-up in bucket order, overshooting the clip by SEG_EXT', () => {
    const b = w.beats.find((x) => x.day === 4)
    expect(b.segs.map((s) => s.color)).toEqual(['#6366f1', '#a78bfa', '#22d3ee'])
    const sum = b.segs.reduce((a, s) => a + s.h, 0)
    expect(sum).toBeCloseTo(b.h + 2 * SEG_EXT)
    // bottom segment ends SEG_EXT below the clip, top segment starts SEG_EXT above it
    const bottom = b.segs[0], top = b.segs[b.segs.length - 1]
    expect(bottom.y + bottom.h).toBeCloseTo(b.y + b.h + SEG_EXT)
    expect(top.y).toBeCloseTo(b.y - SEG_EXT)
    // shares follow the counts: 18 : 4 : 9
    expect(bottom.h / top.h).toBeCloseTo(18 / 9)
  })

  it('gives an empty day one slate segment so the dot still paints', () => {
    const b = w.beats.find((x) => x.day === 5)
    expect(b.segs).toHaveLength(1)
    expect(b.segs[0].color).toBe(FALLBACK_COLOR)
  })
})

describe('layoutWave — scale', () => {
  it('holds a quiet week low: the floor is ten runs, not the week’s own max', () => {
    const quiet = WEEK.map((d, i) => day(d.date, i === 3 ? { 'Chat/Tasks': 3 } : {}))
    const w = layoutWave({ data: quiet, buckets: ORDER, colors: COLORS })
    expect(w.scaleMax).toBe(SCALE_FLOOR)
    const tallest = Math.max(...w.beats.map((b) => b.h))
    expect(tallest).toBeLessThan((WAVE_H - 2) * 0.45)
  })

  it('scales to the busiest day once that passes the floor', () => {
    const w = layoutWave({ data: WEEK, buckets: ORDER, colors: COLORS })
    expect(w.scaleMax).toBe(31)
    const tallest = Math.max(...w.beats.map((b) => b.h))
    expect(tallest).toBeGreaterThan((WAVE_H - 2) * 0.9)
  })

  it('survives a nine-bucket day without a segment going negative', () => {
    const nine = Object.fromEntries(ORDER.map((k) => [k, 1]))
    const w = layoutWave({ data: [day('2026-10-22', nine)], buckets: ORDER, colors: COLORS })
    const b = w.beats[0]
    expect(b.segs).toHaveLength(9)
    for (const s of b.segs) expect(s.h).toBeGreaterThan(0)
  })
})

describe('idleBeats — the loading face', () => {
  it('lays out the same number of resting dots the loaded wave will replace', () => {
    const idle = idleBeats({ n: 8 })
    expect(idle).toHaveLength(8 * BEATS_PER_DAY)
    const loaded = layoutWave({ data: WEEK, buckets: ORDER, colors: COLORS })
    for (let i = 0; i < idle.length; i++) {
      expect(idle[i].cx).toBeCloseTo(loaded.beats[i].cx)
      expect(idle[i].w).toBeCloseTo(loaded.beats[i].w)
    }
  })

  it('defaults to the eight points a 7d window carries, so the reveal lands in place', () => {
    expect(idleBeats()).toHaveLength(8 * BEATS_PER_DAY)
  })
})
