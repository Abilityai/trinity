/**
 * Decidable rules for the agent canvas (ent#438, widened by ent#536).
 *
 * Pure and exported because `vitest` runs `environment: 'node'` with no
 * component-mount harness — a rule that lives inside an SFC is one no test can
 * reach (the ent#392 precedent, restated). The components below are
 * dispatchers over this file. That is also why every input an agent authors
 * (a chart series label, a colour, an image source, a fence body) is
 * normalised HERE before any component sees it.
 */
import { paletteColor } from '../../utils/canvasPalette'

// The five kinds the shared `components/reports/` dispatch already renders.
// Reused, never forked: those renderer keys are CI-pinned as the canonical
// contract (`test_1535_report_prompt_guidance.py`), and forking them is what
// the agent page and the deliverable card both refused.
export const REPORT_DELEGATED_KINDS = ['table', 'kpi', 'markdown', 'timeline', 'json']

// The kinds a canvas adds. The report `display_hint` enum is deliberately NOT
// widened to match — a canvas is a superset of a report's rendering, not a
// change to what a report is.
export const CANVAS_ONLY_KINDS = ['chart', 'html', 'image', 'diagram']

export const CANVAS_BLOCK_KINDS = [...REPORT_DELEGATED_KINDS, ...CANVAS_ONLY_KINDS]

/** The default canvas — the one the agent and its voice mode both write (ent#536). */
export const DEFAULT_CANVAS_ID = 'main'

export const CHART_TYPES = ['line', 'area', 'bar', 'stacked_bar', 'pie', 'donut']

/**
 * The Mermaid initialisation the diagram renderer uses, pinned here so a spec
 * can read it. `htmlLabels: false` is load-bearing, not style: mermaid 11 keeps
 * HTML labels on under `securityLevel: 'strict'` (only `loose` is
 * special-cased), and DOMPurify forbids the `<foreignObject>` they live in —
 * so without this every flowchart node renders as an empty box after
 * sanitisation. The theme is set at init time from the theme store.
 */
export const MERMAID_CONFIG = Object.freeze({
  startOnLoad: false,
  securityLevel: 'strict',
  htmlLabels: false,
  flowchart: Object.freeze({ htmlLabels: false }),
  // ent#537 /cso: an `%%{init: {themeCSS}}%%` directive in agent source is
  // honoured under `strict`. Mermaid namespaces every rule under the diagram
  // id, so it cannot reach the page — but `@keyframes` are deliberately left
  // global and could redefine an app animation name. Listing the keys here
  // makes `sanitize()` delete them from any directive, so the SVG's own
  // <style> (the one `sanitizeSvg` keeps) never carries agent-authored CSS.
  secure: Object.freeze(['themeCSS', 'fontFamily', 'altFontFamily']),
})

const LABEL_MAX = 80
const HEX_COLOR_RE = /^#(?:[0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})$/i

/**
 * Which renderer a block wants.
 *
 * An UNKNOWN kind resolves to `json`, never to nothing: a block whose kind we
 * do not recognise still holds data the reader is entitled to see, and a
 * silently dropped block is the one failure a canvas must not have — the
 * surface would look complete while missing content.
 */
export function blockRenderer(block) {
  const kind = block && typeof block.kind === 'string' ? block.kind : ''
  if (CANVAS_BLOCK_KINDS.includes(kind)) return kind
  return 'json'
}

// ent#537 — the slot charset the backend enforces (`CANVAS_SLOT_RE`); a
// stored value is re-checked rather than trusted, and a malformed one reads
// as "unslotted" so the block still renders.
const SLOT_RE = /^[a-z][a-z0-9-]{0,31}$/

/** Blocks that survive to the renderer, with their resolved kind attached. */
export function renderableBlocks(blocks) {
  if (!Array.isArray(blocks)) return []
  return blocks
    .filter((b) => b && typeof b === 'object')
    .map((b, i) => ({
      key: `${i}:${typeof b.id === 'string' ? b.id : ''}:${blockRenderer(b)}`,
      id: typeof b.id === 'string' ? b.id : null,
      kind: blockRenderer(b),
      title: typeof b.title === 'string' ? b.title : null,
      // Carried explicitly — this rebuild is a field allowlist, so a key it
      // does not name never reaches the layout (ent#537).
      slot: typeof b.slot === 'string' && SLOT_RE.test(b.slot) ? b.slot : null,
      payload: b.payload && typeof b.payload === 'object' ? b.payload : {},
    }))
}

/**
 * What the freshness line says.
 *
 * Two facts, never one: the timestamp is ALWAYS rendered, and the staleness
 * mark is an addition to it. That ordering is the honesty contract (AC 7) —
 * a mark that replaced the timestamp would leave a reader who disagrees with
 * our heuristic no way to judge for themselves.
 *
 * The `stale` flag is derived server-side ("the agent finished a run after
 * this canvas was written"), so the wording says what was observed rather
 * than asserting the content is wrong — we know the agent worked and did not
 * refresh this surface; we do not know that what is here is false.
 */
export function freshness(canvas, now = Date.now()) {
  const updatedAt = canvas?.updated_at || null
  const label = updatedAt ? `Updated ${relativeTime(updatedAt, now)}` : 'Never updated'
  if (!canvas?.stale) return { label, stale: false, note: null }
  return {
    label,
    stale: true,
    note: 'The agent has run since this was written — it may be out of date.',
  }
}

/** Compact relative time. Returns an absolute-ish fallback for a bad value. */
export function relativeTime(iso, now = Date.now()) {
  const then = Date.parse(iso)
  if (Number.isNaN(then)) return 'at an unknown time'
  const secs = Math.max(0, Math.round((now - then) / 1000))
  if (secs < 60) return 'just now'
  const mins = Math.round(secs / 60)
  if (mins < 60) return `${mins}m ago`
  const hours = Math.round(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.round(hours / 24)
  if (days < 30) return `${days}d ago`
  return new Date(then).toISOString().slice(0, 10)
}

/**
 * What an empty canvas surface should say (AC 6).
 *
 * Two audiences, two next actions, because a blank panel is the defect and a
 * WRONG next action is worse than none: an operator can make an agent write a
 * canvas, a Workspace client cannot — offering them a tool call would be an
 * instruction they cannot follow.
 */
export function emptyState(viewer) {
  if (viewer === 'client') {
    return {
      title: 'Nothing published here yet',
      body: 'This agent has not put anything on its canvas for you. Ask it in the chat — it can publish results here as it works.',
      action: 'chat',
    }
  }
  return {
    title: 'No canvas yet',
    body: 'A canvas is a surface your agent keeps current — a status board, a running tally, a chart, the latest version of an analysis. Ask it in chat to "put it on your canvas", or have it call set_canvas.',
    action: null,
  }
}

// ---------------------------------------------------------------------------
// Living with a lot of canvases (ent#553)
// ---------------------------------------------------------------------------

/**
 * Order a canvas list: pinned first, then most-recently-updated.
 *
 * The backend already returns this order. Re-deriving it here is deliberate,
 * not redundant: the list is mutated in place by an optimistic pin or delete,
 * and a client that only re-sorted on refetch would show a just-pinned canvas
 * still sitting in the middle. Same two keys, so the two cannot disagree.
 *
 * Sorts a COPY — sorting props in place mutates the store's array.
 */
export function sortCanvases(list) {
  return (Array.isArray(list) ? [...list] : []).sort((a, b) => {
    const pinned = Number(!!b?.pinned) - Number(!!a?.pinned)
    if (pinned) return pinned
    return String(b?.updated_at || '').localeCompare(String(a?.updated_at || ''))
  })
}

/**
 * Filter by title (AC 4). Matches the id too, because an agent may never set a
 * title and the id is then the only thing the person can see to search for.
 *
 * An empty query returns everything rather than nothing — the filter is a
 * narrowing of a list that is already correct, never a search that must be
 * satisfied before anything renders.
 */
export function filterCanvases(list, query) {
  const needle = String(query || '').trim().toLowerCase()
  const rows = Array.isArray(list) ? list : []
  if (!needle) return rows
  return rows.filter((c) => {
    const title = String(c?.title || '').toLowerCase()
    const id = String(c?.canvas_id || '').toLowerCase()
    return title.includes(needle) || id.includes(needle)
  })
}

/**
 * Should the chip strip render? (ent#553 review — the search-narrowing gap.)
 *
 * The strip exists "only when there is a choice to make", and the first cut
 * gated that on the FILTERED list: `visible.length > 1`. At exactly one match
 * that hides the strip, the no-match line (`!visible.length`) does not render
 * either, and the auto-select watcher keyed off the UNFILTERED list never
 * selects the match — so the user searched, hit one result, and the chips
 * vanished with the previous canvas still on screen. A query with a hit
 * always shows its hit; the "no choice" collapse applies only when NOT
 * searching.
 *
 * @param {{ visible: number, manage: boolean, query: string }} s
 */
export function canvasSelectorVisible({ visible, manage, query }) {
  if (manage) return true
  if (String(query || '').trim()) return visible > 0
  return visible > 1
}

/**
 * Which canvas the strip should select after the visible set changed.
 *
 * `null` = leave the selection alone. While a query is active the selection
 * must be one of the MATCHES: if it is not, the first match is selected (the
 * one-match case above needs this, or the strip shows a chip the panel is
 * not displaying). With no query the caller's own list-watcher already
 * handles "the selected canvas disappeared".
 *
 * @param {Array<{canvas_id: string}>} visible
 * @param {string|null} selectedId
 * @param {string} query
 * @returns {string|null}
 */
export function canvasAutoSelect(visible, selectedId, query) {
  if (!String(query || '').trim()) return null
  const rows = Array.isArray(visible) ? visible : []
  if (!rows.length) return null
  if (rows.some((c) => c && c.canvas_id === selectedId)) return null
  return rows[0].canvas_id
}

/**
 * The one confirmation a bulk delete shows, naming the count (AC 3).
 *
 * Singular and plural are spelled out rather than pluralised with an "(s)":
 * this is the last thing a person reads before destroying work.
 */
export function bulkDeletePrompt(count) {
  const n = Number(count) || 0
  if (n <= 0) return null
  if (n === 1) return 'Delete this canvas? The agent can create it again, but its current contents will be gone.'
  return `Delete ${n} canvases? The agent can create them again, but their current contents will be gone.`
}

/**
 * How full an agent's canvas allowance is, for the header line (AC 6).
 *
 * `atLimit` drives a message BEFORE the agent hits the refusal, because the
 * person who can act on it (retire one) is not the one who receives the error
 * (the agent, mid-run).
 */
export function canvasHeadroom(count, max) {
  const used = Number(count) || 0
  const limit = Number(max) || 0
  if (!limit) return { used, limit: 0, atLimit: false, label: null }
  const atLimit = used >= limit
  const near = used >= Math.floor(limit * 0.9)
  return {
    used,
    limit,
    atLimit,
    label: atLimit
      ? `${used} of ${limit} canvases — the agent cannot create another until one is removed`
      : near
        ? `${used} of ${limit} canvases`
        : null,
  }
}

/**
 * Selection state for the bulk bar, derived rather than stored.
 *
 * `selected` is filtered against the VISIBLE list, so ids left over from a
 * previous filter or a canvas someone else deleted cannot inflate the count
 * the confirmation names — the number a person is shown is the number that
 * will actually be sent.
 */
export function selectionState(selectedIds, visible) {
  const rows = Array.isArray(visible) ? visible : []
  const ids = new Set(Array.isArray(selectedIds) ? selectedIds : [])
  const present = rows.filter((c) => ids.has(c?.canvas_id)).map((c) => c.canvas_id)
  return {
    ids: present,
    count: present.length,
    all: rows.length > 0 && present.length === rows.length,
    any: present.length > 0,
  }
}

/** What a bulk delete actually did, said honestly (never "5 removed" for 3). */
export function bulkDeleteOutcome(requested, deleted) {
  const asked = Number(requested) || 0
  const got = Array.isArray(deleted) ? deleted.length : Number(deleted) || 0
  if (!asked) return null
  if (got === asked) return got === 1 ? 'Canvas deleted' : `${got} canvases deleted`
  if (got === 0) return 'Nothing was deleted — those canvases were already gone'
  return `${got} of ${asked} deleted — the rest were already gone`
}

// ---------------------------------------------------------------------------
// Agent-authored strings — normalised before any component reads them
// ---------------------------------------------------------------------------

/** A series label as plain text, length-capped; never markup. */
export function safeLabel(value, fallback = '') {
  const s = value == null ? '' : String(value)
  // eslint-disable-next-line no-control-regex -- control characters are the point
  const trimmed = s.replace(/[\x00-\x1f\x7f]/g, '').trim()
  if (!trimmed) return fallback
  return trimmed.length > LABEL_MAX ? `${trimmed.slice(0, LABEL_MAX - 1)}…` : trimmed
}

/**
 * A colour a chart may bind: a hex triplet the agent named, else the palette
 * entry for this series' position. Nothing else passes — a "colour" is
 * interpolated into a `style` attribute and a uPlot stroke, and an
 * unconstrained string there is markup injection waiting for a template.
 */
export function safeColor(value, index) {
  return typeof value === 'string' && HEX_COLOR_RE.test(value.trim())
    ? value.trim()
    : paletteColor(index)
}

// ---------------------------------------------------------------------------
// Charts — the metric series shape
// ---------------------------------------------------------------------------

function finiteOrNull(v) {
  if (typeof v === 'number') return Number.isFinite(v) ? v : null
  if (typeof v === 'string' && v.trim() !== '') {
    const n = Number(v)
    return Number.isFinite(n) ? n : null
  }
  return null
}

function tsKey(ts) {
  return ts == null ? '' : String(ts)
}

/** Points from the metric shape `{points:[{ts, value}]}`. */
function metricPoints(series) {
  if (!Array.isArray(series?.points)) return null
  const out = []
  for (const p of series.points) {
    if (!p || typeof p !== 'object') continue
    const ts = tsKey(p.ts)
    if (!ts) continue
    out.push({ ts, value: finiteOrNull(p.value) })
  }
  return out
}

/** Points from the legacy shape `{labels:[...], series:[{data:[...]}]}` (ent#438). */
function legacyPoints(labels, series) {
  if (!Array.isArray(series?.data)) return null
  return labels.map((label, i) => ({ ts: tsKey(label), value: finiteOrNull(series.data[i]) }))
    .filter((p) => p.ts)
}

/**
 * Normalise a `chart` payload into one model every chart renderer reads.
 *
 * The payload is the metric series shape (#478/#479, ruled for ent#536): one
 * series per line, stack segment or slice, each a list of `{ts, value}`
 * points. `ts` is a label — a date/time when it parses as one, otherwise a
 * category name shown verbatim. The pre-ent#536 `{labels, series[{data}]}`
 * shape is still accepted and normalised the same way, so canvases written
 * since ent#438 keep rendering.
 *
 * Returns null when the payload cannot make a chart, so the caller falls back
 * to the JSON renderer rather than mounting a chart over nothing — an empty
 * chart reads as "no data", which is a claim we have not earned.
 */
export function chartModel(payload) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null
  const rawSeries = Array.isArray(payload.series) ? payload.series : null
  if (!rawSeries || !rawSeries.length) return null
  const legacyLabels = Array.isArray(payload.labels) ? payload.labels : null

  const series = []
  rawSeries.forEach((s, i) => {
    if (!s || typeof s !== 'object') return
    const points = Array.isArray(s.points)
      ? metricPoints(s)
      : legacyLabels ? legacyPoints(legacyLabels, s) : null
    if (!points || !points.length) return
    series.push({
      label: safeLabel(s.label, `Series ${series.length + 1}`),
      unit: safeLabel(s.unit, ''),
      color: safeColor(s.color, i),
      stale: s.stale === true,
      lastPointAt: typeof s.last_point_at === 'string' ? s.last_point_at : null,
      points,
    })
  })
  if (!series.length) return null
  if (!series.some((s) => s.points.some((p) => p.value != null))) return null

  // The x positions are the union of every series' labels, in first-seen
  // order — or sorted, when every one of them parses as a date.
  const labels = []
  const seen = new Set()
  for (const s of series) for (const p of s.points) {
    if (!seen.has(p.ts)) { seen.add(p.ts); labels.push(p.ts) }
  }
  const allDates = labels.length > 0 && labels.every((l) => !Number.isNaN(Date.parse(l)))
  if (allDates) labels.sort((a, b) => Date.parse(a) - Date.parse(b))

  const type = CHART_TYPES.includes(payload.type) ? payload.type : 'line'
  return {
    type,
    axis: allDates ? 'time' : 'category',
    labels,
    series: series.map((s) => {
      const byTs = new Map(s.points.map((p) => [p.ts, p.value]))
      return { ...s, data: labels.map((l) => (byTs.has(l) ? byTs.get(l) : null)) }
    }),
    asOf: series.map((s) => s.lastPointAt).filter(Boolean).sort().pop() || null,
    stale: series.some((s) => s.stale),
  }
}

/**
 * How to print one x label. A date prints as a day, or as day + time when the
 * labels are closer than a day apart (an hourly metric must not collapse into
 * "Sep 1, Sep 1, Sep 1"); anything else prints verbatim, capped.
 */
export function tsLabelFormatter(labels) {
  const parsed = (labels || []).map((l) => Date.parse(l))
  const allDates = parsed.length > 0 && parsed.every((t) => !Number.isNaN(t))
  if (!allDates) return (l) => safeLabel(l, '')
  let withTime = false
  for (let i = 1; i < parsed.length; i++) {
    if (Math.abs(parsed[i] - parsed[i - 1]) < 24 * 3600 * 1000) { withTime = true; break }
  }
  return (l) => {
    const t = Date.parse(l)
    if (Number.isNaN(t)) return safeLabel(l, '')
    const d = new Date(t)
    const day = d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' })
    if (!withTime) return day
    const time = d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'UTC' })
    return `${day} ${time}`
  }
}

/**
 * Minimum px between x-axis labels, from the longest label the formatter
 * will print: ~6px per character at the axis's 10px font plus a gutter,
 * clamped so a date axis keeps uPlot's default and a 30-category axis of
 * "Category number N" prints every fifth name instead of all thirty on top
 * of each other (#2583).
 */
export function axisLabelSpace(labels, format) {
  let longest = 0
  for (const l of labels || []) longest = Math.max(longest, String(format(l) ?? '').length)
  return Math.min(240, Math.max(50, longest * 6 + 12))
}

/** Props for `TrendLineChart` (line / area). */
export function trendChartProps(model) {
  const labelFormat = tsLabelFormatter(model.labels)
  return {
    dates: model.labels,
    series: model.series.map((s) => ({
      label: s.label,
      data: s.data,
      color: s.color,
      fill: model.type === 'area',
    })),
    labelFormat,
    labelSpace: axisLabelSpace(model.labels, labelFormat),
  }
}

/**
 * Props for `StackedBarChart` (bar / stacked_bar).
 *
 * Two layouts, decided by the data: when every series holds exactly one point
 * and there are several series, each series is a bar (a "leads by region"
 * chart — categories are series, as they are dims in the metric store); else
 * the columns are the x labels and the series stack inside them. Negative
 * values clamp to zero — a stacked bar has no meaning below the axis.
 */
export function stackedBarProps(model) {
  const oneEach = model.series.length > 1 && model.series.every((s) => s.points.length === 1)
  const colors = {}
  const labels = {}
  if (oneEach) {
    const bucket = model.series.map((s) => s.label)
    const data = model.series.map((s, i) => {
      const v = Math.max(0, s.points[0].value ?? 0)
      colors[bucketKey(s.label, i)] = s.color
      labels[bucketKey(s.label, i)] = s.label
      return { date: bucketKey(s.label, i), total: v, by_type: { [bucketKey(s.label, i)]: v } }
    })
    return {
      data,
      buckets: bucket.map((_, i) => bucketKey(model.series[i].label, i)),
      colors,
      labels,
      labelFormat: (k) => labels[k] ?? k,
    }
  }
  const buckets = model.series.map((s, i) => bucketKey(s.label, i))
  model.series.forEach((s, i) => { colors[buckets[i]] = s.color; labels[buckets[i]] = s.label })
  const data = model.labels.map((l, li) => {
    const byType = {}
    let total = 0
    model.series.forEach((s, i) => {
      const v = Math.max(0, s.data[li] ?? 0)
      if (v > 0) { byType[buckets[i]] = v; total += v }
    })
    return { date: l, total, by_type: byType }
  })
  return { data, buckets, colors, labels, labelFormat: tsLabelFormatter(model.labels) }
}

// Bucket keys must be unique even when two series share a label.
function bucketKey(label, i) {
  return `${i}:${label}`
}

/**
 * Slices for a pie/donut: one per series, the series' LAST non-null point.
 * Returns null when nothing is positive — a pie of zeros is not a chart.
 */
export function pieSlices(model) {
  const slices = []
  model.series.forEach((s) => {
    const last = [...s.points].reverse().find((p) => p.value != null)
    const v = last ? last.value : null
    if (v != null && v > 0) slices.push({ label: s.label, value: v, color: s.color, unit: s.unit })
  })
  const total = slices.reduce((a, s) => a + s.value, 0)
  if (!slices.length || total <= 0) return null
  let angle = 0
  return slices.map((s) => {
    const fraction = s.value / total
    const start = angle
    angle += fraction * 360
    return { ...s, fraction, startAngle: start, endAngle: angle }
  })
}

// ---------------------------------------------------------------------------
// Images
// ---------------------------------------------------------------------------

/**
 * What an `image` block may bind. The backend normalised the payload to
 * `{src, src_kind}` at write; the renderer trusts the kind AND rechecks the
 * prefix, because a stored block outlives the validator. Anything else → null,
 * and the block says so instead of mounting a broken `<img>`.
 */
export function imageSource(payload) {
  if (!payload || typeof payload !== 'object') return null
  const src = typeof payload.src === 'string' ? payload.src.trim() : ''
  if (!src) return null
  const kind = payload.src_kind
  const low = src.toLowerCase()
  let resolved = null
  if (kind === 'url' || kind == null) {
    if (/^https:\/\/\S+$/i.test(src)) resolved = 'url'
  }
  if (!resolved && (kind === 'data' || kind == null)) {
    if (/^data:image\/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/]+={0,2}$/.test(src)) resolved = 'data'
  }
  if (!resolved && kind === 'path') {
    if (low.startsWith('/home/developer/') && !src.includes('..')) resolved = 'path'
  }
  if (!resolved) return null
  return {
    kind: resolved,
    src,
    alt: safeLabel(payload.alt, ''),
    caption: typeof payload.caption === 'string' ? payload.caption.slice(0, 300) : '',
  }
}

// ---------------------------------------------------------------------------
// Rich fences in markdown (ent#536)
// ---------------------------------------------------------------------------

export const RICH_FENCES = ['chart', 'kpi', 'table', 'mermaid']

const RICH_OPEN_RE = /^```([a-z]+)[ \t]*$/
const ANY_FENCE_OPEN_RE = /^(`{3,}|~{3,})/
const CLOSE_RE = /^```[ \t]*$/

function parseRichFence(lang, body) {
  if (lang === 'mermaid') {
    return body.trim() ? { type: 'diagram', payload: { mermaid: body } } : null
  }
  let obj
  try { obj = JSON.parse(body) } catch { return null }
  if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return null
  if (lang === 'chart') return chartModel(obj) ? { type: 'chart', payload: obj } : null
  if (lang === 'kpi') return Array.isArray(obj.tiles) ? { type: 'kpi', payload: obj } : null
  if (lang === 'table') {
    return Array.isArray(obj.columns) && Array.isArray(obj.rows) ? { type: 'table', payload: obj } : null
  }
  return null
}

/**
 * Split a markdown string into prose and the figures fenced inside it.
 *
 * Only a column-0 opener of exactly three backticks plus one of the rich
 * languages, closed by a column-0 line of exactly three backticks, becomes a
 * figure; and only when its body is usable (valid JSON in the shape the
 * renderer reads, or non-empty Mermaid source). Everything else — `~~~`
 * fences, four-backtick fences, indented or blockquoted fences, unterminated
 * fences, an info string with extras, JSON that will not parse or will not
 * make a chart — stays in the prose byte-for-byte and renders as the code
 * block it is. Any other fence is copied through to its own close untouched,
 * so a ```chart shown inside a ````markdown example is never extracted.
 *
 * The extracted JSON is handed to components as DATA; it is never joined
 * back into HTML, so nothing here widens the DOMPurify policy.
 */
export function splitRichFences(markdown) {
  const text = typeof markdown === 'string' ? markdown.replace(/\r\n?/g, '\n') : ''
  if (!text) return []
  const lines = text.split('\n')
  const segments = []
  let buf = []
  const flush = () => {
    if (buf.length && buf.join('\n').trim()) segments.push({ type: 'markdown', text: buf.join('\n') })
    buf = []
  }

  let i = 0
  while (i < lines.length) {
    const line = lines[i]
    const rich = RICH_OPEN_RE.exec(line)
    if (rich && RICH_FENCES.includes(rich[1])) {
      let j = i + 1
      while (j < lines.length && !CLOSE_RE.test(lines[j])) j++
      if (j >= lines.length) { buf.push(line); i++; continue } // unterminated → prose
      const seg = parseRichFence(rich[1], lines.slice(i + 1, j).join('\n'))
      if (seg) {
        flush()
        segments.push(seg)
      } else {
        for (let k = i; k <= j; k++) buf.push(lines[k])
      }
      i = j + 1
      continue
    }
    const other = ANY_FENCE_OPEN_RE.exec(line)
    if (other) {
      // Copy any other fence through to its close, untouched.
      const run = other[1]
      const ch = run[0]
      const closeRe = new RegExp(`^\\${ch}{${run.length},}[ \\t]*$`)
      buf.push(line)
      let j = i + 1
      while (j < lines.length && !closeRe.test(lines[j])) { buf.push(lines[j]); j++ }
      if (j < lines.length) buf.push(lines[j])
      i = j + 1
      continue
    }
    buf.push(line)
    i++
  }
  flush()
  return segments
}

/** True when a markdown block carries at least one renderable figure. */
export function hasRichFences(markdown) {
  return splitRichFences(markdown).some((s) => s.type !== 'markdown')
}
