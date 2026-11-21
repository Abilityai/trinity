/**
 * #4344 — words for the backend's enum values on the Workspace's agent sheet.
 *
 * A client reads "Delegated", not `L2`; "Dropped", not `killed`. Each map is a
 * frozen object with a humanised fallback, so a value the backend adds later
 * renders as a readable phrase rather than empty or raw (the pattern
 * `portalLoopUtils.js::loopStatusLabel` already uses).
 *
 * Pure module: `vitest` runs it in `environment: 'node'`.
 */

/** `kpi_snapshot` → "Kpi snapshot"; empty → "Unknown". */
export function humaniseEnum(value) {
  const text = typeof value === 'string' ? value.trim().replace(/[_-]+/g, ' ').replace(/\s+/g, ' ') : ''
  if (!text) return 'Unknown'
  return text.charAt(0).toUpperCase() + text.slice(1).toLowerCase()
}

function fromMap(map, value) {
  return (typeof value === 'string' && Object.prototype.hasOwnProperty.call(map, value)) ? map[value] : humaniseEnum(value)
}

// `services/autonomy_dial_service.py::LEVELS`. Nothing runs unprompted below
// L2, so L0 and L1 read the same to a client; the backend's `level_label`
// stays the subtitle for the detail.
const AUTONOMY_LEVELS = Object.freeze({ L0: 'On request', L1: 'On request', L2: 'Delegated', L3: 'Broad' })
export function autonomyLevelLabel(level) {
  if (typeof level === 'string' && level.trim() && !AUTONOMY_LEVELS[level]) return level.trim()
  return fromMap(AUTONOMY_LEVELS, level)
}

// `services/seat_decision_service.py::OUTCOMES`.
const DECISION_OUTCOMES = Object.freeze({ approved: 'Approved', deferred: 'Deferred', killed: 'Dropped' })
export const decisionOutcomeLabel = (v) => fromMap(DECISION_OUTCOMES, v)

const DECISION_STATUSES = Object.freeze({
  active: 'Active',
  routed: 'Routed',
  expired: 'Expired',
  superseded: 'Replaced',
  closed: 'Closed',
  reversed: 'Reversed',
})
export const decisionStatusLabel = (v) => fromMap(DECISION_STATUSES, v)

// The role file's own `status:` — free text in the file, so mostly the fallback.
const ROLE_STATUSES = Object.freeze({ active: 'Active', draft: 'Draft', retired: 'Retired' })
export const roleStatusLabel = (v) => fromMap(ROLE_STATUSES, v)

// `client_portal/role_card.py::READINESS_STATES`.
const READINESS = Object.freeze({ calibrating: 'Calibrating', ready: 'Ready' })
export const readinessLabel = (v) => fromMap(READINESS, v)

const REPORT_TYPES = Object.freeze({ kpi_snapshot: 'KPI snapshot' })
export const reportTypeLabel = (v) => fromMap(REPORT_TYPES, v)

/** A report's title, or "Report · Oct 21, 2026" — never the raw type. */
export function reportTitle(report) {
  const title = typeof report?.title === 'string' ? report.title.trim() : ''
  if (title) return title
  const at = report?.created_at ? new Date(report.created_at) : null
  if (!at || Number.isNaN(at.getTime())) return 'Report'
  return `Report · ${at.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })}`
}
