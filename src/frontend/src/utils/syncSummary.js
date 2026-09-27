/**
 * The agent card's sync numbers (trinity-enterprise#707).
 *
 * Display only. `utils/syncHealth.js` maps the backend's `state` to a colour
 * and holds no threshold and no clock; this module formats the numbers the
 * backend measured — ahead / behind on the agent's own branch, the dirty-file
 * count, the age of the last successful push — and takes the chip kind and the
 * text colour from that same `state`. It decides nothing: the caller passes
 * `now`, and no number here changes a colour.
 *
 * Two entry shapes: the per-agent reads (`ahead_working` / `behind_working` —
 * `GET /api/agents/sync-health`, `GET /api/agents/{name}/git/sync-state`) and
 * the fleet block (`ahead` / `behind` — `GET /api/monitoring/status`).
 *
 *   formatSyncSummary(entry, now) → '↑7 ↓0 · 12 dirty · pushed 3h ago' | null
 *   syncChip(entry, now)          → { kind, icon, text, title } | null  (AgentTile)
 *   syncTextClass(entry)          → status text classes for a chrome fill (OverviewPanel)
 */
import { classifySyncHealth, syncHealthLabel } from './syncHealth'
import { parseUTC } from './timestamps'

const CHIP_KIND = { red: 'crit', yellow: 'warn', green: 'calm' }

// Status text on a CHROME fill (the Overview footprint chips sit on
// gray-100 / gray-700). #2201's 700-light / 400-dark tier is measured on a
// surface; on chrome it fails AA (warning-700 on gray-100 4.47:1, danger-400 on
// gray-700 3.73:1, measured in the browser), so it steps up one tier — the
// contract's chrome rule for tertiary ink, applied to status ink.
const TEXT_CLASS = {
  red: 'text-status-danger-800 dark:text-status-danger-300',
  yellow: 'text-status-warning-800 dark:text-status-warning-300',
  green: 'text-status-success-800 dark:text-status-success-300',
}

function count(value) {
  return Number.isInteger(value) && value >= 0 ? value : null
}

function first(...values) {
  return values.find((v) => v !== undefined && v !== null)
}

/** `just now`, `45m ago`, `26h ago`, `9d ago` — the backend reason's age shape. */
export function formatAge(ms) {
  const seconds = Math.floor(Math.max(ms, 0) / 1000)
  if (seconds < 60) return 'just now'
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 48 * 3600) return `${Math.floor(seconds / 3600)}h ago`
  return `${Math.floor(seconds / 86400)}d ago`
}

function pushedAt(entry) {
  const iso = entry.last_successful_push_at
  if (!iso) return null
  const at = parseUTC(iso)
  return Number.isNaN(at.getTime()) ? null : at
}

export function formatSyncSummary(entry, now) {
  if (classifySyncHealth(entry) === 'gray') return null
  const ahead = count(first(entry.ahead, entry.ahead_working))
  const behind = count(first(entry.behind, entry.behind_working))
  const parts = [`↑${ahead ?? '?'} ↓${behind ?? '?'}`]
  const dirty = count(entry.dirty_files)
  if (dirty) parts.push(`${dirty} dirty`)
  const at = pushedAt(entry)
  parts.push(at ? `pushed ${formatAge(now - at.getTime())}` : 'never pushed')
  return parts.join(' · ')
}

export function syncChip(entry, now) {
  const text = formatSyncSummary(entry, now)
  if (!text) return null
  const at = pushedAt(entry)
  const lines = [syncHealthLabel(entry)]
  // The dashboard batch and /git/sync-state say `freeze`; the fleet block `frozen`.
  if (entry.freeze || entry.frozen) lines.push('Scheduled runs are paused until it syncs')
  // Principle 22: relative on the chip, absolute + timezone on hover.
  if (at) {
    lines.push(`Last successful push: ${at.toLocaleString(undefined, { timeZoneName: 'short' })}`)
  }
  return {
    kind: CHIP_KIND[classifySyncHealth(entry)],
    icon: '⟳',
    text,
    title: lines.join('\n'),
  }
}

export function syncTextClass(entry) {
  return TEXT_CLASS[classifySyncHealth(entry)] || ''
}
