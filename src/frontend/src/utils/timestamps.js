/**
 * Timezone-Aware Timestamp Utilities
 *
 * IMPORTANT: Backend timestamps are in UTC (with or without 'Z' suffix).
 * These utilities ensure consistent parsing regardless of user timezone.
 *
 * Usage:
 *   import { parseUTC, getTimestampMs } from '@/utils/timestamps'
 *
 *   // Parse backend timestamp
 *   const date = parseUTC("2026-01-15T10:30:00")  // Assumes UTC
 *
 *   // Get Unix ms for calculations
 *   const ms = getTimestampMs("2026-01-15T10:30:00")
 *
 *   // Display in user's local timezone
 *   date.toLocaleString()  // Automatically converts to local time
 */

/**
 * Parse a UTC timestamp from the backend.
 *
 * Handles timestamps with or without timezone indicator:
 * - "2026-01-15T10:30:00Z" -> parsed as UTC (correct)
 * - "2026-01-15T10:30:00+00:00" -> parsed as UTC (correct)
 * - "2026-01-15T10:30:00" -> ASSUMED to be UTC (adds Z suffix)
 *
 * @param {string} timestamp - ISO 8601 timestamp string
 * @returns {Date} Date object representing the correct instant in time
 */
export function parseUTC(timestamp) {
  if (!timestamp) return new Date()

  // Check if timestamp already has timezone info
  const hasTimezone = timestamp.endsWith('Z') ||
                     timestamp.includes('+') ||
                     (timestamp.length > 19 && timestamp.includes('-', 10))

  // If no timezone indicator, assume UTC by adding 'Z'
  if (!hasTimezone) {
    timestamp = timestamp + 'Z'
  }

  return new Date(timestamp)
}

/**
 * Get Unix timestamp (ms) from a UTC timestamp string.
 * Use this for timeline positioning and duration calculations.
 *
 * @param {string} timestamp - ISO 8601 timestamp string
 * @returns {number} Unix timestamp in milliseconds
 */
export function getTimestampMs(timestamp) {
  return parseUTC(timestamp).getTime()
}

/**
 * Format a UTC timestamp for display in user's local timezone.
 *
 * @param {string} timestamp - ISO 8601 timestamp string (UTC)
 * @param {object} options - Intl.DateTimeFormat options
 * @returns {string} Formatted date string in user's local timezone
 */
export function formatLocalTime(timestamp, options = {}) {
  if (!timestamp) return ''
  const date = parseUTC(timestamp)
  return date.toLocaleString(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    ...options
  })
}

/**
 * Format a UTC timestamp as local date and time.
 *
 * @param {string} timestamp - ISO 8601 timestamp string (UTC)
 * @returns {string} Formatted as "Jan 15, 2026 10:30:00 AM" in user's timezone
 */
export function formatLocalDateTime(timestamp) {
  if (!timestamp) return ''
  const date = parseUTC(timestamp)
  return date.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit'
  })
}

/**
 * Format relative time (e.g., "2 hours ago")
 *
 * @param {string} timestamp - ISO 8601 timestamp string (UTC)
 * @returns {string} Human-readable relative time
 */
export function formatRelativeTime(timestamp) {
  if (!timestamp) return 'Unknown'
  const date = parseUTC(timestamp)
  const now = new Date()
  const diffSeconds = Math.floor((now - date) / 1000)

  if (diffSeconds < 0) return 'in the future'
  if (diffSeconds < 60) return 'just now'
  if (diffSeconds < 3600) return `${Math.floor(diffSeconds / 60)} minutes ago`
  if (diffSeconds < 86400) return `${Math.floor(diffSeconds / 3600)} hours ago`
  if (diffSeconds < 604800) return `${Math.floor(diffSeconds / 86400)} days ago`
  return date.toLocaleDateString()
}

/**
 * Compact age for a dense row, e.g. a grid tile (ent#100): "42s" · "12m" ·
 * "3h" · "5d". One or two characters plus a unit, because the column it lives
 * in is ~40px wide next to an agent name that must not be the thing that gets
 * truncated.
 *
 * Deliberately NOT `formatRelativeTime`: that helper reads its own `new Date()`
 * and returns prose ("12 minutes ago"). This one takes an explicit elapsed
 * duration so the caller can drive it from the Grid's shared 1s tick — a tile
 * that read the clock itself would either need its own timer (a defect on this
 * chassis) or silently freeze.
 *
 * A NEGATIVE elapsed time renders "now", never "-3m". It is reachable without
 * any bug in the platform: the row's `started_at` comes from the server and
 * `now` from the browser, so a client clock running fast puts a fresh row in
 * the future. See `serverSkewMs` for the correction; this is the floor that
 * holds even when the correction is unavailable.
 *
 * @param {number} msSince elapsed milliseconds (server instant → now)
 * @returns {string}
 */
export function formatCompactAge(msSince) {
  if (!Number.isFinite(msSince)) return '—'
  const s = Math.floor(msSince / 1000)
  if (s < 1) return 'now'
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h`
  return `${Math.floor(h / 24)}d`
}

/**
 * Offset between the server's clock and this browser's, in milliseconds
 * (positive = the server is ahead, i.e. the browser is slow).
 *
 * Read from the HTTP `Date` RESPONSE header, which every response carries and
 * which needs no backend change at all. Same-origin, so the browser exposes it
 * to JS — `Date` is not on the CORS-safelisted response-header list, so this
 * would need `Access-Control-Expose-Headers` if the API ever moved to another
 * origin; it does not today (nginx proxies `/api/` on the same host and hides
 * no headers).
 *
 * Why bother for an AGE rather than a countdown: an uncorrected age is measured
 * between two different clocks, so a laptop three minutes fast renders every
 * failure three minutes younger than it is — and the freshest ones as "now".
 * The tile presents that as a statement about the platform, not about the
 * browser.
 *
 * Sub-2s offsets are treated as zero: that is inside the header's own
 * one-second granularity plus flight time, so "correcting" it would be noise.
 * There is deliberately NO upper clamp — a wildly wrong client clock is exactly
 * the case that needs correcting, and the header is the only authority either
 * way. Unparseable or absent ⇒ 0, i.e. today's uncorrected behaviour.
 *
 * @param {string|null|undefined} dateHeader value of the HTTP `Date` header
 * @param {number} clientNowMs `Date.now()` sampled when the response landed
 * @returns {number} milliseconds to ADD to a client instant to get server time
 */
export function serverSkewMs(dateHeader, clientNowMs) {
  if (typeof dateHeader !== 'string' || !dateHeader) return 0
  if (!Number.isFinite(clientNowMs)) return 0
  const serverMs = Date.parse(dateHeader)
  if (!Number.isFinite(serverMs)) return 0
  const skew = serverMs - clientNowMs
  return Math.abs(skew) < 2000 ? 0 : skew
}
