/**
 * Loading-state decisions for data surfaces (#1927 — design-system p13/p14/p15).
 *
 * THE rule, in one place: **loading means "no data yet", never "fetch in
 * flight"**. A surface that already holds data is never "loading" again — a
 * background poll swaps values in place, and a poll that FAILS leaves the data
 * on screen and flags it stale ("showing data from 10:42 · Retry"). Three
 * issues in a month (#1634, #1926, #1927) were the same bug on different panels:
 * a template gated its spinner on the in-flight flag, so every refetch swapped
 * rendered content for a spinner. The four surfaces #1927 fixes, and the
 * operator-queue store, call these functions rather than re-deriving the chain
 * inline — `vitest.config.js` pins `environment: 'node'` (no component
 * mounting), so this module is the only unit-testable home for the rule, and a
 * helper nobody calls tests nothing (the spec asserts the wiring too).
 *
 * Companion ratchet: `scripts/scan-loading-gates.mjs` + `loading-gate-baseline.json`
 * freeze the remaining bare `v-if="loading"` gates at today's per-file counts.
 */

/**
 * Reduce a surface's fetch flags to one of four states plus a staleness bit.
 *
 *   !hasLoaded, no error  → 'loading'   (covers the pre-fetch frame too — never the empty copy)
 *   !hasLoaded, error     → 'failed'    (LoadFailed, never the empty copy — the #1926 lie)
 *   hasLoaded, count 0    → 'empty'     (a fetch SUCCEEDED and returned zero)
 *   hasLoaded, count > 0  → 'ready'
 *   stale = hasLoaded && !!error        (a refresh failed with data on screen → banner, data stays)
 *
 * `loading` (fetch in flight) is accepted for call-site readability but is
 * deliberately NOT a state input: a background refresh must be invisible, and a
 * retry with data on screen is "ready + stale", not "loading". Read the raw flag
 * for `:retrying` labels only.
 *
 * @param {{loading?: boolean, hasLoaded?: boolean, error?: unknown, count?: number}} flags
 * @returns {{state: 'loading'|'failed'|'empty'|'ready', stale: boolean}}
 */
export function viewState({ loading = false, hasLoaded = false, error = null, count = 1 } = {}) {
  void loading // see doc: in-flight never changes what is rendered
  const hasError = Boolean(error)
  if (!hasLoaded) return { state: hasError ? 'failed' : 'loading', stale: false }
  return { state: count === 0 ? 'empty' : 'ready', stale: hasError }
}

/**
 * The stale-refresh banner copy — the style guide's own treatment
 * (design-system.md §6: "Refresh failed — showing data from 10:42 · Retry").
 * Never prints "undefined": with no known load time it says what it shows.
 *
 * @param {string} subject            e.g. 'the queue', 'agents', 'template info'
 * @param {Date|number|string|null} lastLoadedAt
 * @param {{formatTime?: (d: Date) => string}} [opts]  injectable for tests (locale-free)
 */
export function staleBannerMessage(subject, lastLoadedAt, { formatTime = defaultFormatTime } = {}) {
  const at = toDate(lastLoadedAt)
  if (!at) return `Couldn't refresh ${subject} — showing the last data that loaded.`
  return `Couldn't refresh ${subject} — showing data from ${formatTime(at)}.`
}

/**
 * Normalize a list response that may arrive bare (`[…]`) or wrapped under a key
 * (`{items:[…], count}` from /api/operator-queue, `{agents:[…]}` from
 * /api/agents/execution-stats). Never returns a non-array — the /m fetchers
 * were doing `(res.data || []).filter` on an object and throwing on every poll.
 */
export function listFrom(data, key) {
  if (Array.isArray(data)) return data
  const inner = data && typeof data === 'object' ? data[key] : undefined
  return Array.isArray(inner) ? inner : []
}

/**
 * Operator-queue landing rule (principle 5: updates preserve expansion).
 * Returns the id to auto-expand, or null.
 *
 *   armed      — true until a human toggles any card; re-armed when the open set drains
 *   openIds    — ids of the currently open items, in display order
 *   expandedId — the store's current expansion (may be STALE: an item that was
 *                answered while the operator was away). Membership, not truthiness,
 *                decides whether "something is already expanded".
 */
export function decideAutoExpand({ armed = false, openIds, expandedId = null } = {}) {
  if (!armed || !Array.isArray(openIds) || openIds.length === 0) return null
  if (expandedId != null && openIds.includes(expandedId)) return null
  return openIds[0]
}

// ---------------------------------------------------------------- internals

function toDate(v) {
  if (v == null || v === '') return null
  const d = v instanceof Date ? v : new Date(v)
  return Number.isNaN(d.getTime()) ? null : d
}

function defaultFormatTime(d) {
  try {
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  } catch {
    return d.toISOString().slice(11, 16)
  }
}
