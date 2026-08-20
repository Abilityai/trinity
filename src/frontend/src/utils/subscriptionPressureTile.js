// ent#259 — pure decision logic for the "Subscription pressure" grid tile.
//
// Separate from `subscriptionPressure.js` on purpose, mirroring how ent#96 kept
// `executionsTile.js` apart from `executionBuckets.js`: that module is the
// SHARED MEANING layer (what a reading means), consumed by the Settings panel,
// the AgentTile chip and the AgentListPanel badge. This module is one tile's
// PRESENTATION rules. Folding tile row-building into the shared module would
// put a fourth consumer's layout concerns in front of the other three.
//
// Everything decidable lives here because `vitest.config.js` pins
// `environment: 'node'` — pure modules only, no mount harness — so a decision
// left inside the SFC is a decision no test can reach.

import {
  failureKindLabel,
  formatResetTime,
  headroomIsFresh,
  usageSourceLabel,
} from './subscriptionPressure'

/**
 * Row tracks the tile renders. `InfoTile`'s body is `overflow: hidden` and
 * `TileRowList` has no scroll by design (FleetGrid's wheel handler zooms the
 * board unconditionally), so a row past this cap does not ellipsize or scroll —
 * it SILENTLY VANISHES. The count is therefore fixed, and anything beyond it is
 * disclosed through the stamp rather than dropped in silence.
 */
export const SUBSCRIPTION_TILE_MAX_ROWS = 4

/** Severity rank for sorting — higher sorts first. */
const RANK = { crit: 3, warn: 2, unknown: 1, ok: 0 }

/**
 * Colour bands for "how much of this limit is spent".
 *
 * Bands, not a gradient: the operator question is "do I need to act", which has
 * three answers. A continuous ramp would imply a precision the reading does not
 * have — the provider reports whole percents and the snapshot can be up to 30
 * min old.
 */
export const UTILIZATION_WARM_PCT = 60
export const UTILIZATION_HIGH_PCT = 85

/**
 * Bar fill width for a utilization percentage, clamped to 0..100.
 *
 * The provider can report **over 100%** on an overage plan, and an unclamped
 * `width: 137%` bleeds the fill past its track and shoves the row's siblings
 * sideways — inside a tile body that is `overflow: hidden`, that pushes content
 * out of view with no trace. The *number* beside the bar stays unclamped, so an
 * overage is still reported honestly; only the geometry is bounded.
 */
export function barWidthPct(pct) {
  const v = Number(pct)
  if (!Number.isFinite(v)) return 0
  return Math.max(0, Math.min(100, v))
}

/** `null` when there is no reading — the caller must not colour a guess. */
export function utilizationLevel(pct) {
  if (pct == null || !Number.isFinite(Number(pct))) return null
  const v = Number(pct)
  if (v >= UTILIZATION_HIGH_PCT) return 'high'
  if (v >= UTILIZATION_WARM_PCT) return 'warm'
  return 'ok'
}

/**
 * Both rolling limit windows as spend-against-limit, or `null` when no fresh
 * provider reading backs them.
 *
 * The windows are Anthropic's own **5-hour** and **7-day** rolling limits — not
 * calendar day/week. They are labelled `5h` / `7d` rather than "daily"/"weekly"
 * because a 5h rolling window resets on a clock the operator can see in
 * `resets_at`, and calling it "daily" would misdescribe when the quota comes
 * back.
 *
 * Returns `null` (not zeroes) unless `headroomIsFresh` — the DB-derived arm
 * carries consumption but no denominator, so there is no honest percentage to
 * show without a provider snapshot.
 */
export function windowReadings(usage) {
  if (isUnavailable(usage) || !headroomIsFresh(usage)) return null
  const h = usage.headroom || {}
  const out = []
  for (const [label, win] of [['5h', h.five_hour], ['7d', h.seven_day]]) {
    if (!win || win.utilization_pct == null) continue
    const pct = Math.round(Number(win.utilization_pct))
    out.push({
      label,
      pct,
      // `fill` is geometry (clamped); `pct` is the claim (not clamped).
      fill: barWidthPct(pct),
      level: utilizationLevel(pct),
      resetsAt: win.resets_at || null,
    })
  }
  return out.length ? out : null
}

/** A per-subscription usage payload that failed to load (store's honest shape). */
function isUnavailable(usage) {
  return !usage || usage.error === true
}

/**
 * 429s in the last 24h — the `rate_limit` kind ONLY.
 *
 * NOT `failure_events_24h`, which also counts `auth` failures and pre-#471
 * `unknown` rows. #471's migration 0040 exists precisely because the table
 * conflated the two; rendering the total under a "429" label would undo that
 * fix one layer up, in the UI. An unclassified row is never promoted to a 429 —
 * claiming an unknown failure was a rate limit is exactly the false precision
 * this surface must not invent.
 */
export function rateLimitEventCount(usage) {
  if (isUnavailable(usage)) return 0
  return Number(usage.failure_events_by_kind?.rate_limit || 0)
}

/**
 * One subscription's pressure severity.
 *
 * `crit` comes ONLY from the backend's one-gate `rate_limited_now` (its 2h
 * predicate OR a fresh provider verdict) — a 24h failure count alone never
 * claims "limited now", the same rule `pressureBadge` pins for the per-agent
 * chip. `unknown` is a real state, distinct from `ok`: a usage fetch that
 * failed must never render as a healthy subscription.
 */
export function subscriptionSeverity(usage) {
  if (isUnavailable(usage)) return 'unknown'
  if (usage.rate_limited_now) return 'crit'
  if ((usage.failure_events_24h || 0) > 0) return 'warn'
  // A window in the HIGH band is pressure even with a clean failure history —
  // it is the subscription about to stop working, and it is the only signal
  // available BEFORE the first 429. Without this it ranks as `ok` and can be
  // pushed into the overflow by a subscription whose only problem is that its
  // usage read failed, which is precisely backwards.
  const windows = windowReadings(usage)
  if (windows && windows.some((w) => w.level === 'high')) return 'warn'
  return 'ok'
}

/**
 * Why a row is `warn` — the chip has room for one word and they mean different
 * things: 429s already happened, `near` has not happened yet.
 */
export function warnReason(usage) {
  if (isUnavailable(usage) || !usage) return null
  if ((usage.failure_events_24h || 0) > 0) return 'events'
  const windows = windowReadings(usage)
  if (windows && windows.some((w) => w.level === 'high')) return 'utilization'
  return null
}

/** Compact token count: 0 · 940 · 12.3k · 1.2M. */
export function formatTokenCount(n) {
  const v = Number(n)
  if (!Number.isFinite(v) || v <= 0) return '0'
  if (v < 1000) return String(Math.round(v))
  if (v < 1_000_000) return `${(v / 1000).toFixed(v < 10_000 ? 1 : 0)}k`
  return `${(v / 1_000_000).toFixed(1)}M`
}

/**
 * Cost as API-EQUIVALENT, never a bill.
 *
 * A subscription is a flat fee; `cost_usd` is what the same consumption would
 * have cost at API prices. The `≈` is load-bearing and matches the Settings
 * panel's own wording ("Cost is API-equivalent … not a bill") — dropping it
 * would present a shadow price as an invoice.
 */
export function formatApproxCost(n) {
  const v = Number(n)
  if (!Number.isFinite(v) || v <= 0) return '≈$0'
  return v < 1 ? `≈$${v.toFixed(2)}` : `≈$${v.toFixed(v < 100 ? 2 : 0)}`
}

/**
 * The QUALITATIVE fallback, shown only when `windowReadings` has nothing.
 *
 * The percentages are the primary reading and live in `windowReadings`; this
 * runs when no fresh provider snapshot backs them. It deliberately shows no
 * number: the DB-derived arm records consumption but carries no denominator, so
 * any percentage synthesized here would be the fake precision the AC forbids.
 */
export function pressureHeadline(usage) {
  if (isUnavailable(usage)) return 'unavailable'
  if (usage.rate_limited_now) return 'rate-limited'
  const events = rateLimitEventCount(usage)
  if (events > 0) return `${events}× 429`
  if ((usage.failure_events_24h || 0) > 0) return 'failures'
  return 'ok'
}

/**
 * The second line: only figures that are literally what they say.
 *
 * Deliberately OMITS the "input tokens" figure. `SubscriptionUsageWindow.
 * input_tokens` is `SUM(schedule_executions.context_used)` — context-window
 * OCCUPANCY per run, not input tokens consumed; summing it across runs
 * re-counts the whole conversation context on every turn. architecture.md
 * already made this exact ruling for the sibling ent#101 tile against
 * `/api/executions/timeline` ("reports context-window occupancy under that
 * name … must be labelled to match rather than presented as tokens consumed").
 * Output tokens and cost are real, so they lead; the context estimate is
 * available in the row tooltip, labelled as an estimate.
 */
export function usageLine(usage) {
  if (isUnavailable(usage)) return 'usage data unavailable'
  const w5 = usage.window_5h || {}
  const w7 = usage.window_7d || {}
  return [
    `5h ${formatApproxCost(w5.cost_usd)}`,
    `${formatTokenCount(w5.output_tokens)} out`,
    `7d ${formatApproxCost(w7.cost_usd)}`,
  ].join(' · ')
}

/** Full hover text — where the caveats live, since the row face has no room. */
export function rowTooltip(sub, usage) {
  if (isUnavailable(usage)) {
    return `${sub.name}\nUsage could not be read. The subscription itself is unaffected.`
  }
  const w5 = usage.window_5h || {}
  const w7 = usage.window_7d || {}
  const lines = [sub.name]

  // Both limit windows, each with the clock its quota comes back on — the row
  // face has room for the percentages but not the reset times.
  for (const [label, win] of [['5h', usage.headroom?.five_hour], ['7d', usage.headroom?.seven_day]]) {
    if (!win || win.utilization_pct == null) continue
    const reset = formatResetTime(win.resets_at)
    lines.push(
      `${win.utilization_pct}% of the ${label} limit used`
      + (reset ? ` · resets ${reset}` : ''),
    )
  }
  lines.push(usageSourceLabel(usage))

  // 5h is a SUBSET of 7d — spelled out so the two lines are never read as
  // additive.
  lines.push(
    `5h: ${formatTokenCount(w5.output_tokens)} out · ${formatApproxCost(w5.cost_usd)}`
    + ` · ${w5.message_count || 0} run${(w5.message_count || 0) === 1 ? '' : 's'}`,
  )
  lines.push(
    `7d (includes the 5h window): ${formatTokenCount(w7.output_tokens)} out`
    + ` · ${formatApproxCost(w7.cost_usd)}`,
  )
  lines.push(
    `Context estimate — 5h ${formatTokenCount(w5.input_tokens)},`
    + ` 7d ${formatTokenCount(w7.input_tokens)}`
    + ' (recorded context occupancy, not billed input tokens)',
  )
  lines.push('Cost is API-equivalent — what this consumption would cost at API prices, not a bill.')

  const kinds = failureKindLabel(usage.failure_events_by_kind)
  if (kinds) lines.push(`Failure events (24h): ${kinds}`)

  // The most actionable state the payload can carry, and it is otherwise
  // indistinguishable from "no provider data" on the row face.
  if (usage.headroom?.status === 'invalid_token') {
    lines.push('Provider token rejected — re-register this subscription in Settings.')
  }

  const agents = (usage.agents || []).length
  lines.push(agents === 0 ? 'No agents assigned' : `${agents} agent${agents === 1 ? '' : 's'} assigned`)
  return lines.join('\n')
}

/** Settings → Integrations. The ONLY surface where a subscription is acted on. */
export const SUBSCRIPTION_SETTINGS_ROUTE = { name: 'Settings', query: { tab: 'integrations' } }

/**
 * Build the tile's rows from the subscription roster + the usage map.
 *
 * The ROSTER drives the rows — never the usage map. A subscription whose usage
 * fetch failed, or which has simply been quiet, must still appear: dropping it
 * would render "not configured" for "not used", which is the opposite claim.
 *
 * Returns `{ rows, visibleRows, totalRows, hiddenRows }`. The counts are
 * returned rather than derived by the component because the overflow is
 * otherwise invisible: past the fixed track count a row does not scroll or
 * ellipsize, it disappears, and a node-environment suite structurally cannot
 * see that happen.
 */
export function subscriptionPressureRows(subscriptions, usageBySub, options = {}) {
  const maxRows = options.maxRows || SUBSCRIPTION_TILE_MAX_ROWS
  const list = Array.isArray(subscriptions) ? subscriptions : []
  const usageMap = usageBySub || {}

  const built = list.map((sub) => {
    const usage = usageMap[sub.id]
    const severity = subscriptionSeverity(usage)
    const windows = windowReadings(usage)
    // Sort on the FULLEST window, not the 5h one: a subscription at 30% of its
    // 5h limit but 95% of its weekly is the one about to stop working, and
    // ranking it below a busier-but-fine neighbour would bury the actual risk.
    const utilization = windows ? Math.max(...windows.map((x) => x.pct)) : null
    return {
      key: sub.id,
      id: sub.id,
      to: SUBSCRIPTION_SETTINGS_ROUTE,
      name: sub.name,
      severity,
      warnReason: warnReason(usage),
      windows,
      utilization,
      events: rateLimitEventCount(usage),
      primary: sub.name,
      // Rendered only when `windows` is null — the percentages are the reading.
      meta: pressureHeadline(usage),
      sub: usageLine(usage),
      title: rowTooltip(sub, usage),
    }
  })

  // Total order: severity, then 429 volume, then how full the window is, then
  // name. A partial comparator would let equal-severity rows reorder between
  // polls, which reads as the board flickering.
  built.sort((a, b) => {
    const rank = (RANK[b.severity] ?? 0) - (RANK[a.severity] ?? 0)
    if (rank !== 0) return rank
    if (b.events !== a.events) return b.events - a.events
    const util = (b.utilization ?? -1) - (a.utilization ?? -1)
    if (util !== 0) return util
    return String(a.name).localeCompare(String(b.name))
  })

  const totalRows = built.length
  if (totalRows <= maxRows) {
    return { rows: built, visibleRows: totalRows, totalRows, hiddenRows: 0 }
  }
  // Keep one track for the overflow link so the hidden rows are reachable
  // rather than merely counted.
  const shown = built.slice(0, maxRows - 1)
  // Counted BEFORE the overflow link is appended: the link is not a
  // subscription, so a stamp reading "4 of 9" when only 3 are legible would be
  // the same silent-clipping lie the stamp exists to prevent.
  const visibleRows = shown.length
  const hiddenRows = totalRows - visibleRows
  shown.push({
    key: '__overflow__',
    id: '__overflow__',
    to: SUBSCRIPTION_SETTINGS_ROUTE,
    name: '',
    severity: 'ok',
    windows: null,
    utilization: null,
    events: 0,
    overflow: true,
    primary: `+${hiddenRows} more`,
    meta: '',
    sub: 'Open Settings → Integrations to see every subscription',
    title: `${hiddenRows} more subscription${hiddenRows === 1 ? '' : 's'} not shown`,
  })
  return { rows: shown, visibleRows, totalRows, hiddenRows }
}

/**
 * `'loading' | 'error' | 'empty' | 'ready'`.
 *
 * `empty` requires a fetch that SUCCEEDED and returned zero — never
 * `rows.length === 0`, which is equally true before the first poll lands and
 * after a failed one. `loaded` latches true on first success and never back, so
 * a later failure keeps the last good rows on screen (stale-while-revalidate)
 * instead of replacing real data with an error panel.
 */
export function subscriptionPressureTileState({ loaded, error, totalRows }) {
  if (!loaded) return error ? 'error' : 'loading'
  if (!totalRows) return 'empty'
  return 'ready'
}

/**
 * Header stamp. Discloses the overflow — the only signal that rows exist beyond
 * the ones rendered, since they are clipped without a trace.
 */
export function subscriptionTileStamp({ visibleRows, totalRows, stale }) {
  if (!totalRows) return ''
  const base = totalRows > visibleRows
    ? `${visibleRows} of ${totalRows}`
    : `${totalRows} subscription${totalRows === 1 ? '' : 's'}`
  return stale ? `${base} · stale` : base
}
