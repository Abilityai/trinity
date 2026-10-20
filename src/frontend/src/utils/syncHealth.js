/**
 * Sync health indicator helpers (#389 S1, trinity-enterprise#706).
 *
 * The BACKEND owns the sync state. `GET /api/agents/sync-health` serves each
 * agent's `state` (green / yellow / red / unknown), an operator-readable
 * `reason` and a `recommendation`, all computed by one policy module
 * (src/backend/services/sync_freeze_policy.py — the same rule the scheduler's
 * freeze uses). This file only maps that state to a colour and shows the
 * reason; it holds no threshold, so the dot can never disagree with the
 * freeze.
 *
 *   green  — the agent and its repository agree
 *   yellow — diverged for 24 h or less (any divergence for a deployment)
 *   red    — a failed sync, diverged > 24 h, dirty > 24 h, or auto-sync on
 *            with no heartbeat for 7 days
 *   gray   — no observation yet (`unknown`), or no entry
 */

const STATE_COLOURS = new Set(['green', 'yellow', 'red'])

export function classifySyncHealth(entry) {
  const state = entry && entry.state
  return STATE_COLOURS.has(state) ? state : 'gray'
}

export function syncHealthColor(entry) {
  switch (classifySyncHealth(entry)) {
    case 'green': return 'bg-status-success-500'
    case 'yellow': return 'bg-status-warning-500'
    case 'red': return 'bg-status-danger-500'
    default: return 'bg-gray-400'
  }
}

export function syncHealthLabel(entry) {
  if (!entry || !entry.reason) return 'Sync status unknown'
  return entry.recommendation
    ? `${entry.reason} — ${entry.recommendation}`
    : entry.reason
}
