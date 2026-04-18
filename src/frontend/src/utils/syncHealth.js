/**
 * Sync health indicator helpers (#389 S1).
 *
 * Classifies a per-agent sync-health entry into one of four colors so the
 * dashboard can render a small dot next to each agent:
 *
 *   green  — last sync succeeded, recent, working branch in sync
 *   yellow — last sync succeeded but stale (>24h, <7d)
 *   red    — failed, very stale (>7d), OR behind_working > 0 (P6: peer wrote)
 *   gray   — no sync attempt yet, or auto-sync disabled and never manual-synced
 */

const DAY_MS = 24 * 60 * 60 * 1000
const WEEK_MS = 7 * DAY_MS

export function classifySyncHealth(entry) {
  if (!entry) return 'gray'
  const status = entry.last_sync_status || 'never'
  if (status === 'never') return 'gray'

  if (entry.behind_working && entry.behind_working > 0) return 'red'
  if (status === 'failed') return 'red'

  const lastAt = entry.last_sync_at ? new Date(entry.last_sync_at).getTime() : null
  if (!lastAt) return 'gray'
  const age = Date.now() - lastAt
  if (age >= WEEK_MS) return 'red'
  if (age >= DAY_MS) return 'yellow'
  return 'green'
}

export function syncHealthColor(entry) {
  // Tailwind utility classes for the dot.
  switch (classifySyncHealth(entry)) {
    case 'green': return 'bg-green-500'
    case 'yellow': return 'bg-yellow-500'
    case 'red': return 'bg-red-500'
    default: return 'bg-gray-400'
  }
}

export function syncHealthLabel(entry) {
  if (!entry) return 'Sync status unknown'
  const status = entry.last_sync_status || 'never'
  if (status === 'never') return 'No sync attempts yet'
  if (entry.behind_working && entry.behind_working > 0) {
    return `Working branch has ${entry.behind_working} unseen commit(s) from a peer`
  }
  if (status === 'failed') {
    const summary = entry.last_error_summary || 'unknown error'
    const failures = entry.consecutive_failures || 0
    return `Last sync failed (${failures} in a row): ${summary}`
  }
  if (entry.last_sync_at) {
    return `Last synced ${new Date(entry.last_sync_at).toLocaleString()}`
  }
  return 'Sync status: success'
}
