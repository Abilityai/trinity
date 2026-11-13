/**
 * Guardrail events — the copy and the state rules both surfaces share
 * (trinity-enterprise#165 S4, GUARD-005): the Operations › Health card
 * (`GuardrailStatsCard.vue`) and Settings › Guardrails › Recent events
 * (`agent-settings/GuardrailEventsSection.vue`).
 *
 * The point of this module is that silence never reads as healthy. An agent
 * with no events is in one of four distinct states, and each has its own words:
 *
 *   no_hooks  — its runtime has no guardrail hooks, so no event can ever arrive
 *   never     — no report has ever arrived (an image older than the forwarder)
 *   stale     — it reported before and has been silent since
 *   reporting — it reports, and had nothing to report in the window
 *
 * The backend decides `coverage` for the fleet read; the per-agent read carries
 * only `reporting`, so `agentCoverage` applies the same rule here, with the
 * same runtime names (`services/guardrail_event_service.py::HOOKED_RUNTIMES`).
 */
import { formatLocalDateTime, formatRelativeTime } from './timestamps'

/** The runtimes Trinity registers guardrail hooks on (mirrors the backend). */
export const HOOKED_RUNTIMES = Object.freeze(['claude-code', 'claude'])

export const NO_EVENTS_COPY = 'No guardrail events in the last 24 h'
export const NEVER_COPY = "This agent isn't reporting guardrail events (older image)"
export const NO_HOOKS_COPY = 'Guardrail hooks run on the Claude Code runtime only'

/** "Not reporting since <time>" — absolute, so it reads the same tomorrow. */
export function staleCopy(lastReportAt) {
  return lastReportAt
    ? `Not reporting since ${formatLocalDateTime(lastReportAt)}`
    : 'Not reporting'
}

/**
 * Whether guardrail hooks run on this runtime. An unknown runtime (no label, no
 * report) is the platform default, Claude Code — as on the backend.
 */
export function runtimeHasHooks(runtime) {
  if (!runtime || runtime === 'other') return true
  return HOOKED_RUNTIMES.includes(String(runtime).toLowerCase())
}

/**
 * One coverage word for one agent: `no_hooks` first (its keepalives may say
 * `reporting`, but no hook can fire), then the reporting state.
 */
export function agentCoverage({ runtime = null, reporting = null } = {}) {
  if (!runtimeHasHooks(runtime || reporting?.runtime)) return 'no_hooks'
  const state = reporting?.state
  return state === 'reporting' || state === 'stale' ? state : 'never'
}

/**
 * The notice an agent's coverage earns, or null when it is reporting.
 * `{ coverage, text, variant }` — the variant is a BaseBadge token family.
 */
export function coverageNotice(coverage, reporting = null) {
  switch (coverage) {
    case 'no_hooks':
      return { coverage, text: NO_HOOKS_COPY, variant: 'neutral' }
    case 'never':
      return { coverage, text: NEVER_COPY, variant: 'neutral' }
    case 'stale':
      return { coverage, text: staleCopy(reporting?.last_report_at), variant: 'warning' }
    default:
      return null
  }
}

/** Short label per coverage state, for a badge or a tally. */
export const COVERAGE_LABELS = Object.freeze({
  reporting: 'Reporting',
  stale: 'Stopped reporting',
  never: 'Not reporting',
  no_hooks: 'No hooks',
})

/** User vocabulary for each event kind; an unknown kind keeps its name. */
export const KIND_LABELS = Object.freeze({
  deny: 'Refused',
  credential_redacted: 'Credential hidden',
  credential_unredacted: 'Credential not hidden',
  redaction_writeback_refused: 'Placeholder write refused',
  scanner_error: 'Scanner error',
  hook_error: 'Hook error',
  config_error: 'Config error',
  other: 'Other',
})

export function kindLabel(kind) {
  return KIND_LABELS[kind] || kind || 'Other'
}

/** A BaseBadge variant per kind: refusals warn, failures to protect are danger. */
export function kindVariant(kind) {
  if (kind === 'credential_unredacted' || kind === 'scanner_error') return 'danger'
  if (kind === 'hook_error' || kind === 'config_error') return 'warning'
  if (kind === 'credential_redacted') return 'info'
  return 'neutral'
}

/** `[{kind, label, count}]`, largest first, for a per-kind breakdown. */
export function kindRows(byKind = {}) {
  return Object.entries(byKind || {})
    .filter(([, n]) => Number(n) > 0)
    .map(([kind, n]) => ({ kind, label: kindLabel(kind), count: Number(n) }))
    .sort((a, b) => b.count - a.count || a.label.localeCompare(b.label))
}

/** "turn_tier_guard · shell_owner_only" — the machine names, for mono text. */
export function sourceText(event) {
  return [event?.hook, event?.code].filter(Boolean).join(' · ')
}

/** Relative time for a row, absolute for its tooltip (principle 22). */
export function eventTimes(iso) {
  return { relative: formatRelativeTime(iso), absolute: formatLocalDateTime(iso) }
}

/** ISO cutoff `hours` before `now` — the per-agent read's window. */
export function sinceHours(hours, now = Date.now()) {
  return new Date(now - hours * 3600 * 1000).toISOString()
}
