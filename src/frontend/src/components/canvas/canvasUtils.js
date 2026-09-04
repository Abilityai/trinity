/**
 * Decidable rules for the agent canvas (ent#438).
 *
 * Pure and exported because `vitest` runs `environment: 'node'` with no
 * component-mount harness — a rule that lives inside an SFC is one no test can
 * reach (the ent#392 precedent, restated). The components below are
 * dispatchers over this file.
 */

// The five kinds the shared `components/reports/` dispatch already renders.
// Reused, never forked: those renderer keys are CI-pinned as the canonical
// contract (`test_1535_report_prompt_guidance.py`), and forking them is what
// the agent page and the deliverable card both refused.
export const REPORT_DELEGATED_KINDS = ['table', 'kpi', 'markdown', 'timeline', 'json']

// The two a canvas adds. The report `display_hint` enum is deliberately NOT
// widened to match — a canvas is a superset of a report's rendering, not a
// change to what a report is.
export const CANVAS_ONLY_KINDS = ['chart', 'html']

export const CANVAS_BLOCK_KINDS = [...REPORT_DELEGATED_KINDS, ...CANVAS_ONLY_KINDS]

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

/** Blocks that survive to the renderer, with their resolved kind attached. */
export function renderableBlocks(blocks) {
  if (!Array.isArray(blocks)) return []
  return blocks
    .filter((b) => b && typeof b === 'object')
    .map((b, i) => ({
      key: `${i}:${blockRenderer(b)}`,
      kind: blockRenderer(b),
      title: typeof b.title === 'string' ? b.title : null,
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
    body: 'A canvas is a surface your agent keeps current — a status board, a running tally, the latest version of an analysis. Ask the agent to call set_canvas, or give it a skill that does.',
    action: null,
  }
}

/**
 * Series for a `chart` block, in the shape `TrendLineChart` wants.
 *
 * Returns null when the payload cannot make a chart, so the caller can fall
 * back to the JSON renderer rather than mounting a chart over nothing — an
 * empty chart reads as "no data", which is a claim we have not earned.
 */
export function chartSeries(payload) {
  const labels = Array.isArray(payload?.labels) ? payload.labels : null
  const rawSeries = Array.isArray(payload?.series) ? payload.series : null
  if (!labels || !labels.length || !rawSeries || !rawSeries.length) return null
  const series = rawSeries
    .filter((s) => s && Array.isArray(s.data))
    .map((s, i) => ({
      label: typeof s.label === 'string' ? s.label : `Series ${i + 1}`,
      data: s.data.map((v) => (typeof v === 'number' && Number.isFinite(v) ? v : null)),
      color: typeof s.color === 'string' ? s.color : undefined,
    }))
  return series.length ? { labels, series } : null
}
