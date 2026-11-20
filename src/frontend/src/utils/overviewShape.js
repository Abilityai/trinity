/**
 * Shaping for the Agent Detail Overview (trinity-enterprise#1317).
 *
 * Everything the Overview draws is decided here, as pure functions of the API
 * payloads: chart columns, the hover readout, line paths with gaps, the tile
 * faces, the "now" state, health blocks. The components only place what these
 * return, so the rules that matter are unit-testable without a DOM:
 *
 *   - a value that has never been measured is `—`, never `0` or `0%`;
 *   - a day with no runs is a stub in the bar chart and a GAP in a line, never
 *     a point at zero;
 *   - a rate is always shown with its denominator (trinity-enterprise#227).
 */
import { parseUTC, formatCompactAge } from './timestamps'

export const DASH = '—'

/** Tallest a stacked column may be, in px. The chart zone reserves this. */
export const CHART_HEIGHT = 126
/** The failure rail under the baseline: 3px for one failure, up to this for the window's worst day. */
export const RAIL_MAX = 8
const RAIL_MIN = 3
/** Gap between stacked segments, in px (the surface shows through). */
export const SEGMENT_GAP = 2

// The outline of a chart to come: a gentle rise, plainly not data. Same idea as
// the dashboard tile's GHOST_BARS, at this chart's height.
const GHOST_HEIGHTS = [18, 26, 22, 34, 28, 42, 34, 50, 40, 60, 48, 70, 58, 80]

/** `n` ghost-bar heights, resampled so 7, 14 and 30 days share one silhouette. */
export function ghostColumns(n) {
  const count = Math.max(1, n | 0)
  const out = []
  for (let i = 0; i < count; i++) {
    const at = count === 1 ? GHOST_HEIGHTS.length - 1 : Math.round((i * (GHOST_HEIGHTS.length - 1)) / (count - 1))
    out.push(GHOST_HEIGHTS[at])
  }
  return out
}

/** The ghost curve for an empty line tile, in a 200×48 box. */
export const GHOST_LINE = 'M0 40 C 28 38, 44 26, 68 28 S 116 38, 140 20 S 182 10, 200 13'

/** `2026-11-17` → `Tue, Nov 17`. The API buckets by UTC day, so format in UTC. */
export function dayLabel(iso) {
  if (!iso) return ''
  const d = new Date(`${iso}T00:00:00Z`)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric', timeZone: 'UTC' })
}

export function formatDuration(ms) {
  if (ms == null || Number.isNaN(ms)) return DASH
  if (ms < 1000) return `${Math.round(ms)}ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  const m = Math.floor(ms / 60000)
  const s = Math.round((ms % 60000) / 1000)
  return `${m}m ${s}s`
}

export function formatTokens(n) {
  if (n == null) return DASH
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : `${n}`
}

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`

/** `3h ago`, and `just now` rather than "now ago" inside the first second. */
export function agoLabel(msSince) {
  const age = formatCompactAge(msSince)
  return age === 'now' ? 'just now' : age === DASH ? DASH : `${age} ago`
}

/** Did anything run in this window? */
export function hasRuns(stats) {
  return (stats?.total_executions || 0) > 0
}

const MIN_SEGMENT = 3

/**
 * Columns for the stacked activity chart.
 *
 * A column's height is proportional to the busiest day. Inside it every
 * segment first gets a 3px minimum (so one run is visible beside a busy
 * bucket) and the gaps are set aside; only what is left is shared by count.
 * So segments + gaps never exceed the column, and the column never exceeds
 * CHART_HEIGHT, however lopsided the day.
 */
export function activityColumns(stats, { height = CHART_HEIGHT } = {}) {
  const timeline = stats?.timeline || []
  const order = Array.isArray(stats?.buckets) ? stats.buckets : []
  const max = timeline.reduce((m, d) => Math.max(m, d?.total || 0), 0)
  const unit = max > 0 ? height / max : 0
  // The rail's height says HOW MANY failed that day, relative to the window's
  // worst day; with a single failure anywhere it is the minimum mark.
  const maxFailed = timeline.reduce((m, d) => Math.max(m, d?.failed || 0), 0)
  const railPx = (failed) => {
    if (!failed) return 0
    if (maxFailed <= 1) return RAIL_MIN
    return Math.round(RAIL_MIN + ((RAIL_MAX - RAIL_MIN) * (failed - 1)) / (maxFailed - 1))
  }
  const columns = timeline.map((d) => {
    const byType = d?.by_type || {}
    // Backend order first; anything it did not list still gets drawn.
    const names = [...order.filter((b) => byType[b]), ...Object.keys(byType).filter((b) => !order.includes(b) && byType[b])]
    const counted = names.reduce((sum, b) => sum + byType[b], 0) || 1
    const column = Math.min(height, (d?.total || 0) * unit)
    const shared = Math.max(0, column - SEGMENT_GAP * (names.length - 1) - MIN_SEGMENT * names.length)
    return {
      date: d.date,
      label: dayLabel(d.date),
      total: d?.total || 0,
      failed: d?.failed || 0,
      failPx: railPx(d?.failed || 0),
      segments: names.map((bucket) => ({
        bucket,
        count: byType[bucket],
        px: MIN_SEGMENT + Math.floor((shared * byType[bucket]) / counted),
      })),
    }
  })
  return { max, columns }
}

/**
 * The line under the chart title: window summary at rest, one day on hover.
 * `never` = the agent has no runs at all (not merely none in this window); only
 * then does the copy speak of a first run.
 */
export function activityReadout(stats, column, { never = false } = {}) {
  if (column) {
    if (!column.total) return { head: '0', text: `${column.label} · no runs` }
    const parts = column.segments.map((s) => `${s.count} ${s.bucket}`)
    parts.push(column.failed ? `${column.failed} failed` : 'none failed')
    return { head: String(column.total), text: `${column.label} · ${parts.join(' · ')}` }
  }
  if (!hasRuns(stats)) {
    return {
      head: '0',
      text: never ? 'Nothing has run yet. Runs will stack here by what started them.' : 'No runs in this window.',
    }
  }
  const timeline = stats.timeline || []
  const busiest = timeline.reduce((best, d) => ((d?.total || 0) > (best?.total || 0) ? d : best), null)
  const bits = ['runs', `${stats.failed_count || 0} failed`]
  if (busiest?.total) bits.push(`busiest ${dayLabel(busiest.date)} (${busiest.total})`)
  return { head: String(stats.total_executions), text: bits.join(' · ') }
}

/**
 * SVG paths for a small line chart in a `width`×`height` box.
 *
 * `null` values break the line: each run of measured days is its own subpath
 * (and its own closed area), so a day with no runs is a gap rather than a line
 * drawn through a value nobody measured.
 */
export function linePaths(values, { lo = 0, hi = 100, width = 200, height = 48, pad = 4 } = {}) {
  const n = values.length
  const span = hi - lo || 1
  const x = (i) => (n > 1 ? (i / (n - 1)) * width : width / 2)
  const y = (v) => pad + (1 - (Math.min(hi, Math.max(lo, v)) - lo) / span) * (height - pad * 2)
  let line = ''
  let area = ''
  let start = null
  let last = null
  const close = () => {
    area += ` L${x(last).toFixed(1)} ${height} L${x(start).toFixed(1)} ${height} Z`
  }
  for (let i = 0; i < n; i++) {
    const v = values[i]
    if (v == null) {
      if (start != null) { close(); start = null }
      continue
    }
    const p = `${start == null ? ' M' : ' L'}${x(i).toFixed(1)} ${y(v).toFixed(1)}`
    line += p
    area += p
    if (start == null) start = i
    last = i
  }
  if (start != null) close()
  if (last == null) return { line: '', area: '', last: null, yFor: y }
  return {
    line: line.trim(),
    area: area.trim(),
    last: { xPct: (x(last) / width) * 100, y: y(values[last]) },
    yFor: y,
  }
}

/**
 * The three measure tiles. Each face is `{ value, sub, series, hi, guide }`;
 * `value === DASH` means "never measured in this window" and the tile draws its
 * ghost. The rate carries its denominator so a quiet agent's one failure reads
 * as "1 of 2 runs", not a bare 50%.
 */
export function measureTiles(stats, { never = false } = {}) {
  const none = never ? 'after the first run' : 'no runs in this window'
  const timeline = stats?.timeline || []
  const success = stats?.success_count || 0
  const terminal = success + (stats?.failed_count || 0)

  const finished = terminal > 0
    ? { value: `${Math.round((success / terminal) * 100)}%`, sub: `${success} of ${plural(terminal, 'run')}` }
    : { value: DASH, sub: hasRuns(stats) ? 'no run has finished yet' : none }

  const durAvg = stats?.duration_ms?.avg
  const durP95 = stats?.duration_ms?.p95
  const duration = durAvg != null
    ? { value: formatDuration(durAvg), sub: durP95 != null ? `95% finish within ${formatDuration(durP95)}` : 'average of finished runs' }
    : { value: DASH, sub: hasRuns(stats) ? 'no run has finished yet' : none }
  const durMax = timeline.reduce((m, d) => Math.max(m, d?.duration_avg_ms || 0), 0)

  // Percent of the run's own window when the API has it; tokens for rows that
  // predate the recorded window, so an older agent is not left with a dash.
  // Clamped: a run that overran its recorded window is "full", not 104%.
  const pct = stats?.context_pct_avg == null ? null : Math.min(100, stats.context_pct_avg)
  const pctSeries = timeline.map((d) => (d?.context_pct_avg == null ? null : Math.min(100, d.context_pct_avg)))
  const peak = pctSeries.reduce((m, v) => (v == null ? m : Math.max(m, v)), 0)
  let context
  if (pct != null) {
    context = { value: `${pct}%`, sub: `busiest day ${peak}% · line marks 80%`, series: pctSeries, hi: 100, guide: 80 }
  } else if (stats?.context_avg != null) {
    const tokens = timeline.map((d) => (d?.context_avg == null ? null : d.context_avg))
    context = {
      value: formatTokens(stats.context_avg), sub: 'tokens per run, average',
      series: tokens, hi: tokens.reduce((m, v) => Math.max(m, v || 0), 0) * 1.15 || 1, guide: null,
    }
  } else {
    context = { value: DASH, sub: hasRuns(stats) ? 'not recorded for these runs' : none, series: [], hi: 100, guide: null }
  }

  return [
    {
      key: 'finished', label: 'Finished', ...finished,
      series: timeline.map((d) => (d?.success_rate == null ? null : Math.round(d.success_rate * 100))),
      hi: 100, guide: null,
      aria: 'Share of runs that finished, per day',
    },
    {
      key: 'duration', label: 'Time per run', ...duration,
      series: timeline.map((d) => (d?.duration_avg_ms == null ? null : d.duration_avg_ms)),
      hi: durMax * 1.15 || 1, guide: null,
      aria: 'Average run duration, per day',
    },
    { key: 'context', label: 'Context used', ...context, aria: 'Average share of the context window used, per day' },
  ]
}

/** A prompt's first non-empty line — the nearest thing a run has to a title. */
export function firstLine(message) {
  for (const line of String(message || '').split(/\r?\n/)) {
    const text = line.trim()
    if (text) return text
  }
  return ''
}

/** The whole prompt on one line, capped, for hover text. */
export function hoverText(message, cap = 400) {
  const flat = String(message || '').replace(/\s+/g, ' ').trim()
  return flat.length > cap ? `${flat.slice(0, cap - 1)}…` : flat
}

/**
 * What to call a run. A scheduled run is known by its schedule's name (its
 * message is only the command, kept as `detail`); anything else by the first
 * line of what was asked.
 */
export function runTitle(run, schedules = []) {
  const line = firstLine(run?.message)
  const schedule = run?.schedule_id ? (schedules || []).find((s) => s.id === run.schedule_id) : null
  if (schedule) return { title: schedule.name, detail: line && line !== schedule.name ? line : '', full: hoverText(run.message) }
  return { title: line || '(no message)', detail: '', full: hoverText(run?.message) }
}

/** `in 48m`, `in 2h 10m`, `in 3d`; `due now` once the time has passed. */
export function formatUntil(ms) {
  if (ms == null || Number.isNaN(ms)) return DASH
  if (ms <= 0) return 'due now'
  const mins = Math.round(ms / 60000)
  if (mins < 1) return 'in under a minute'
  if (mins < 60) return `in ${mins}m`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return mins % 60 ? `in ${hours}h ${mins % 60}m` : `in ${hours}h`
  return `in ${Math.floor(hours / 24)}d`
}

/** `4m 12s`, `1h 03m` — the elapsed clock on a running task. */
export function formatElapsed(ms) {
  if (ms == null || Number.isNaN(ms) || ms < 0) return ''
  const total = Math.floor(ms / 1000)
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  if (h) return `${h}h ${String(m).padStart(2, '0')}m`
  return m ? `${m}m ${String(s).padStart(2, '0')}s` : `${s}s`
}

const ms = (iso) => {
  if (!iso) return null
  const t = parseUTC(iso)?.getTime?.()
  return Number.isFinite(t) ? t : null
}

/**
 * What the agent is doing right now.
 *
 *   stopped  — the container is not running
 *   working  — an execution is in flight (title from the recent list when the
 *              running row is on it; "Working" alone when it is not)
 *   queued   — nothing running, work waiting
 *   first    — running, idle, and nothing has ever run in the window
 *   idle     — running, idle
 *
 * `live` and `recent` may each be missing (their read failed or has not
 * landed); the state then rests on what is known and claims nothing more.
 */
export function nowState({ agentStatus, recent = [], live = null, stats = null, schedules = [], now = Date.now() } = {}) {
  if (agentStatus !== 'running') {
    return { kind: 'stopped', title: 'Stopped', detail: 'Start it from the header to give it work.' }
  }
  const running = recent.filter((r) => r?.status === 'running')
  const runningCount = Math.max(live?.running_count || 0, running.length)
  if (runningCount > 0) {
    const first = running[0]
    const started = ms(first?.started_at)
    const more = runningCount - 1
    const named = first ? runTitle(first, schedules) : null
    return {
      kind: 'working',
      title: first?.message ? `Working on “${named.title}”` : 'Working',
      full: named?.full || '',
      detail: [started != null ? formatElapsed(now - started) : '', more > 0 ? `+${more} more` : ''].filter(Boolean).join(' · '),
      executionId: first?.id || null,
    }
  }
  if ((live?.queued_count || 0) > 0) {
    return { kind: 'queued', title: `${plural(live.queued_count, 'task')} queued`, detail: 'Waiting for a free slot.' }
  }
  if (stats && !hasRuns(stats) && recent.length === 0) {
    return { kind: 'first', title: 'Idle, waiting for its first run', detail: '' }
  }
  const lastDone = recent.find((r) => r?.completed_at || r?.started_at)
  const at = ms(lastDone?.completed_at || lastDone?.started_at)
  return { kind: 'idle', title: 'Idle', detail: at != null ? `last ran ${agoLabel(now - at)}` : '' }
}

/** The soonest enabled schedule with a future (or just-due) run. */
export function nextRun(schedules = [], now = Date.now()) {
  let best = null
  for (const s of schedules || []) {
    if (!s?.enabled) continue
    const at = ms(s.next_run_at)
    if (at == null) continue
    if (!best || at < best.at) best = { at, name: s.name }
  }
  if (!best) return null
  return { ...best, label: formatUntil(best.at - now), absolute: new Date(best.at).toLocaleString() }
}

/** The last few runs, shaped for the list. */
export function recentRows(executions = [], now = Date.now(), schedules = []) {
  return (executions || []).slice(0, 5).map((r) => {
    const started = ms(r.started_at)
    const status = r.status === 'success' || r.status === 'completed' ? 'finished'
      : r.status === 'failed' || r.status === 'error' ? 'failed'
      : r.status === 'running' ? 'running'
      : r.status === 'queued' ? 'queued'
      : r.status || 'unknown'
    return {
      id: r.id,
      status,
      ...runTitle(r, schedules),
      when: started != null ? agoLabel(now - started) : DASH,
      absolute: started != null ? new Date(started).toLocaleString() : '',
      took: status === 'running' ? 'in progress' : formatDuration(r.duration_ms),
      via: r.triggered_by || '',
    }
  })
}

/**
 * One row per schedule: the window's rollup joined to the schedule's next run.
 * The outcome bar is a share of FINISHED runs in the window — the API has no
 * per-run history here, so the row says "9 of 10 in 14d", never "last ten".
 */
export function scheduleRows(summary, schedules = [], { now = Date.now(), windowLabel = '' } = {}) {
  const byId = new Map((schedules || []).map((s) => [s.id, s]))
  return (summary?.schedules || []).map((row) => {
    const sched = byId.get(row.schedule_id)
    const next = sched?.enabled ? ms(sched.next_run_at) : null
    const ok = row.success_count || 0
    const bad = row.failed_count || 0
    const terminal = ok + bad
    return {
      id: row.schedule_id,
      name: row.name,
      command: row.command || '',
      cron: row.cron_expression,
      enabled: row.enabled !== false,
      next: row.enabled === false ? 'paused' : next != null ? formatUntil(next - now) : DASH,
      nextAbsolute: next != null ? new Date(next).toLocaleString() : '',
      okPct: terminal ? (ok / terminal) * 100 : 0,
      failed: bad,
      hasOutcome: terminal > 0,
      score: terminal ? `${ok} of ${terminal} finished${windowLabel ? ` in ${windowLabel}` : ''}` : 'no finished runs yet',
      avg: formatDuration(row.avg_duration_ms),
    }
  })
}

/**
 * Reachability blocks over the span the checks actually cover.
 *
 * The history read returns the newest N checks, which at the default cadence
 * is hours, not a week — so the strip is as long as the data and says how long
 * that is, instead of stretching a few hours across a "7 days" axis.
 */
export function healthBlocks(checks = [], { blocks = 24, now = Date.now() } = {}) {
  const points = (checks || [])
    .map((c) => ({ at: ms(c.checked_at), ok: !!c.reachable }))
    .filter((p) => p.at != null)
    .sort((a, b) => a.at - b.at)
  if (!points.length) return null
  const from = points[0].at
  const describe = (ok, n, at) => {
    const share = n ? `${Math.round((ok / n) * 100)}% reachable (${plural(n, 'check')})` : 'no checks'
    return `${new Date(at).toLocaleString()} · ${share}`
  }
  // Monitoring has only just started: one block per check, packed against
  // "now", with the rest of the strip left as placeholders. Spreading three
  // checks across the whole strip would draw history that does not exist.
  if (points.length <= blocks) {
    const pad = Array.from({ length: blocks - points.length }, () => ({ state: 'none', title: 'no checks yet' }))
    return {
      spanLabel: agoLabel(now - from),
      blocks: [...pad, ...points.map((p) => ({ state: p.ok ? 'ok' : 'down', title: describe(p.ok ? 1 : 0, 1, p.at) }))],
    }
  }
  const to = points[points.length - 1].at
  const span = Math.max(1, to - from)
  const bins = Array.from({ length: blocks }, () => ({ n: 0, ok: 0 }))
  for (const p of points) {
    const i = Math.min(blocks - 1, Math.floor(((p.at - from) / span) * blocks))
    bins[i].n += 1
    if (p.ok) bins[i].ok += 1
  }
  const step = span / blocks
  return {
    spanLabel: agoLabel(now - from),
    blocks: bins.map((b, i) => {
      const state = b.n === 0 ? 'none' : b.ok === b.n ? 'ok' : b.ok === 0 ? 'down' : 'degraded'
      return { state, title: describe(b.ok, b.n, from + i * step) }
    }),
  }
}

/** Things waiting on a person: notifications, asks, failed syncs. */
export function attentionCount({ notifications = 0, asks = 0, syncFailures = 0 } = {}) {
  return (notifications || 0) + (asks || 0) + (syncFailures || 0)
}
