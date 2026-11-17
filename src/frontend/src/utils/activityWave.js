/**
 * Activity wave geometry (trinity-enterprise#968).
 *
 * The Workspace agent band's activity chart is a waveform rather than a bar
 * chart: one row of rounded beats mirrored around the centre line, whose
 * envelope is a smooth curve through the daily totals, each beat filled with
 * the day's execution sources stacked bottom-up. It reads as the agent's
 * signature, not as a plot, and it has to look right whatever the week holds —
 * a quiet week murmurs, one big day blooms, an empty week is a soft dotted
 * line.
 *
 * Everything that decides how a week becomes beats is HERE and pure, so a node
 * test can execute it; `components/portal/ActivityWave.vue` only binds what
 * `layoutWave` returns.
 *
 * Honesty note, stated once: the payload is ONE datum per UTC day. The four
 * beats of a day are texture on that datum — their heights follow the envelope
 * between day centres, they do not sample sub-day activity, and nothing in the
 * UI should read them as hours. The day is the unit the person hovers, the
 * tooltip names, and the accessible label describes.
 */

// The chart's viewBox. The band renders it at 416×23 (the ent#547 budget); the
// one-unit difference is uniform scaling, not a different layout.
export const WAVE_W = 416
export const WAVE_H = 24
export const WAVE_PAD = 8
export const BEATS_PER_DAY = 4

// A week with fewer than this many runs on its busiest day stays low instead
// of filling the chart: the height is a claim about activity, and three runs
// should not look like thirty.
export const SCALE_FLOOR = 10

// Each beat's source segments overshoot the pill by this much on both ends so
// the seam-softening blur never fades the pill's ends into transparency; the
// clip trims the overshoot.
export const SEG_EXT = 4

// Vertical blur (chart units) that melts the colour seams inside a beat. The
// pill outline stays crisp because the blur sits inside the clip.
export const SOFT = 1.8

// Slate, for a bucket the colour map does not know (a stale bundle against a
// newer backend) and for the resting dot of a day with nothing — the same
// fallback `StackedBarChart.vue::colorFor` uses.
export const FALLBACK_COLOR = '#94a3b8'

// The loading face's glint: a slate base the resting dots sit at and an
// indigo core (the `action-primary` family the bucket ramp is led by) that
// sweeps across them. Bound to SVG gradient stops, where a Tailwind class
// cannot reach — the same reason `BUCKET_COLORS` is hex.
export const IDLE_COLORS = Object.freeze({ base: '#64748b', core: '#818cf8' })

// Reveal motion, mirrored by the component's CSS: each beat grows in over
// REVEAL_MS, starting REVEAL_STAGGER_MS after the previous one. The phase
// fallback (lost `animationend`, hidden tab) must outlive the LAST beat, so it
// is derived from the beat count rather than fixed: 750 + 24 × 31 ≈ 1.5s for
// an eight-day week, and longer for a longer window — never the scanline's
// 700ms, which would cut the reveal mid-flight.
export const REVEAL_MS = 750
export const REVEAL_STAGGER_MS = 24
export const WAVE_REVEAL_FALLBACK_MS = 1600 // the floor, for a week or less
export function revealFallbackMs(beatCount) {
  const last = Math.max(0, (beatCount || 0) - 1)
  return Math.max(WAVE_REVEAL_FALLBACK_MS, REVEAL_MS + REVEAL_STAGGER_MS * last + 150)
}

const r2 = (v) => Math.round(v * 100) / 100
const clamp01 = (v) => Math.min(1, Math.max(0, v))

/**
 * Monotone cubic (Fritsch–Carlson) interpolation through `pts` ({x, y}, x
 * ascending). Chosen over Catmull-Rom because it cannot overshoot: a single
 * busy day between empty ones gives a hump, never a dip below the baseline.
 *
 * Returns the SVG bezier `path` through the knots and `at(x)`, which evaluates
 * the same curve (clamped to the knot range, so a beat outside the first or
 * last day centre sits at that day's height).
 */
export function monotone(pts) {
  const n = pts.length
  if (n === 0) return { path: '', at: () => 0 }
  if (n === 1) return { path: `M${r2(pts[0].x)} ${r2(pts[0].y)}`, at: () => pts[0].y }
  const dx = [], m = [], t = []
  for (let i = 0; i < n - 1; i++) {
    dx[i] = pts[i + 1].x - pts[i].x
    m[i] = (pts[i + 1].y - pts[i].y) / dx[i]
  }
  t[0] = m[0]
  t[n - 1] = m[n - 2]
  for (let i = 1; i < n - 1; i++) t[i] = m[i - 1] * m[i] <= 0 ? 0 : (m[i - 1] + m[i]) / 2
  for (let i = 0; i < n - 1; i++) {
    if (m[i] === 0) { t[i] = 0; t[i + 1] = 0; continue }
    const a = t[i] / m[i], b = t[i + 1] / m[i], s = a * a + b * b
    if (s > 9) {
      const k = 3 / Math.sqrt(s)
      t[i] = k * a * m[i]
      t[i + 1] = k * b * m[i]
    }
  }
  let path = `M${r2(pts[0].x)} ${r2(pts[0].y)}`
  for (let i = 0; i < n - 1; i++) {
    const h = dx[i] / 3
    path += ` C${r2(pts[i].x + h)} ${r2(pts[i].y + t[i] * h)} ${r2(pts[i + 1].x - h)} ${r2(pts[i + 1].y - t[i + 1] * h)} ${r2(pts[i + 1].x)} ${r2(pts[i + 1].y)}`
  }
  const at = (x) => {
    if (x <= pts[0].x) return pts[0].y
    if (x >= pts[n - 1].x) return pts[n - 1].y
    let i = 0
    while (i < n - 2 && x > pts[i + 1].x) i++
    const s = (x - pts[i].x) / dx[i], s2 = s * s, s3 = s2 * s
    return (2 * s3 - 3 * s2 + 1) * pts[i].y
      + (s3 - 2 * s2 + s) * dx[i] * t[i]
      + (-2 * s3 + 3 * s2) * pts[i + 1].y
      + (s3 - s2) * dx[i] * t[i + 1]
  }
  return { path, at }
}

/** A rounded rectangle as a closed SVG path (the beat's clip). */
function pillPath(x, y, w, h, r) {
  const sw = r2(w - 2 * r), sh = r2(h - 2 * r), rr = r2(r)
  return `M${r2(x + r)} ${r2(y)}h${sw}a${rr} ${rr} 0 0 1 ${rr} ${rr}v${sh}a${rr} ${rr} 0 0 1 ${-rr} ${rr}h${-sw}a${rr} ${rr} 0 0 1 ${-rr} ${-rr}v${-sh}a${rr} ${rr} 0 0 1 ${rr} ${-rr}z`
}

const dayTotal = (d) => (Number.isFinite(d?.total) ? d.total : 0)

/** A day's sources in bucket order, plus a slate remainder when the buckets
 *  do not account for the whole total — a trigger type the bundle has never
 *  heard of stays visible rather than vanishing (the backend's own rule for
 *  `_BUCKET_ORDER`). */
function segmentsFor(d, buckets, colors) {
  const byType = d?.by_type || {}
  const segs = []
  let accounted = 0
  for (const b of buckets) {
    const n = byType[b]
    if (!n) continue
    segs.push({ bucket: b, n, color: colors[b] || FALLBACK_COLOR })
    accounted += n
  }
  const rest = dayTotal(d) - accounted
  if (rest > 0) {
    // `Other` is a real bucket too: fold the remainder into it rather than
    // pushing a second segment under the same name (a duplicate tooltip key).
    const other = segs.find((s) => s.bucket === 'Other')
    if (other) other.n += rest
    else segs.push({ bucket: 'Other', n: rest, color: colors.Other || FALLBACK_COLOR })
  }
  return segs
}

/**
 * Lay a timeline out as beats.
 *
 * @param {object} o
 * @param {Array<{date, total, by_type}>} o.data   the backend's gap-filled daily timeline
 * @param {string[]} o.buckets                      stack order (`bucketsForChart`)
 * @param {Record<string,string>} o.colors          bucket → colour (`BUCKET_COLORS`)
 * @returns {{ n, empty, scaleMax, beatW, days, beats }}
 *   days  — one per timeline point: { i, date, total, x, w, segments } — `x`/`w`
 *           is the full-height hit band the person hovers
 *   beats — perDay per day: { day, cx, x, y, w, h, r, clip, segs:[{y,h,color}] }
 */
export function layoutWave({
  data,
  buckets = [],
  colors = {},
  perDay = BEATS_PER_DAY,
  width = WAVE_W,
  height = WAVE_H,
  pad = WAVE_PAD,
} = {}) {
  const points = Array.isArray(data) && data.length
    ? data
    : Array.from({ length: 7 }, () => ({ date: null, total: 0, by_type: {} }))
  const n = points.length
  const totals = points.map(dayTotal)
  const scaleMax = Math.max(SCALE_FLOOR, ...totals)
  const amps = totals.map((v) => v / scaleMax)
  const empty = totals.every((v) => v <= 0)
  const dayW = (width - 2 * pad) / n
  const step = dayW / perDay
  const beatW = step * 0.5
  const r = beatW / 2

  const days = points.map((d, i) => ({
    i,
    date: d?.date ?? null,
    total: totals[i],
    x: r2(pad + i * dayW),
    w: r2(dayW),
    segments: segmentsFor(d, buckets, colors),
  }))

  // Envelope knots sit at each day's column centre, so a day's beats take that
  // day's height and the curve between two days is just the curve between
  // their totals.
  const envelope = monotone(points.map((_, i) => ({ x: pad + (i + 0.5) * dayW, y: amps[i] })))

  const beats = []
  for (let i = 0; i < n; i++) {
    for (let j = 0; j < perDay; j++) {
      const cx = pad + (i * perDay + j + 0.5) * step
      const a = clamp01(envelope.at(cx))
      const h = Math.max(beatW, a * (height - 2))
      const x = cx - r
      const y = height / 2 - h / 2
      const hh = h + 2 * SEG_EXT
      const segs = []
      if (totals[i] <= 0) {
        segs.push({ y: r2(y - SEG_EXT), h: r2(hh), color: FALLBACK_COLOR })
      } else {
        let yy = y + h + SEG_EXT
        for (const s of days[i].segments) {
          const sh = hh * s.n / totals[i]
          yy -= sh
          segs.push({ y: r2(yy), h: r2(sh), color: s.color })
        }
      }
      beats.push({
        day: i,
        cx: r2(cx), x: r2(x), y: r2(y), w: r2(beatW), h: r2(h), r: r2(r),
        clip: pillPath(x, y, beatW, h, r),
        segs,
      })
    }
  }

  return { n, empty, scaleMax, beatW: r2(beatW), days, beats }
}

/**
 * The loading face: resting dots at the positions the loaded beats will take,
 * so the reveal grows each beat in place rather than shuffling the row. `n` is
 * the day count when it is already known, else EIGHT — the band's window is
 * fixed at `7d`, and the backend's gap-filled timeline for it runs from the
 * window's start date through today inclusive, which is eight points.
 */
export function idleBeats({ n = 8, perDay = BEATS_PER_DAY, width = WAVE_W, height = WAVE_H, pad = WAVE_PAD } = {}) {
  const dayW = (width - 2 * pad) / Math.max(1, n)
  const step = dayW / perDay
  const w = step * 0.5
  const h = w * 1.15
  const out = []
  for (let k = 0; k < n * perDay; k++) {
    const cx = pad + (k + 0.5) * step
    out.push({ cx: r2(cx), x: r2(cx - w / 2), y: r2(height / 2 - h / 2), w: r2(w), h: r2(h), r: r2(w / 2) })
  }
  return out
}

// Per-instance SVG id prefix for the component. Module-level on purpose: a
// `<script setup>` variable is per instance, and `useId()` restarts per app
// (so repeats across the apps a test harness mounts). `w` first so `url(#w…)`
// can never parse as a hex colour in the raw-colour scanner.
let instanceSeq = 0
export function nextWaveId() {
  return `w${instanceSeq++}`
}
