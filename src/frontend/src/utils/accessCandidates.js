/**
 * The Access tab's add-field suggestions (trinity-enterprise#450): what the panel
 * decides, kept pure so a spec can execute it.
 */
import { platformRoleLabel } from './workspaceOnly'

export const MIN_QUERY = 2

/** Would this text produce a query? The popup never opens on a shorter one. */
export function wantsSuggestions(text) {
  return String(text || '').trim().length >= MIN_QUERY
}

export function candidateRows(candidates) {
  return (candidates || []).map((c) => ({
    key: c.email,
    primary: c.label,
    secondary: c.role ? `${c.email} · ${platformRoleLabel(c.role)}` : c.email,
  }))
}

/** Arrow keys over `count` rows, wrapping. -1 means none, so Enter submits the text. */
export function nextActive(active, count, delta) {
  if (count <= 0) return -1
  if (active < 0) return delta > 0 ? 0 : count - 1
  return (active + delta + count) % count
}

/** A query that matched no one says so, and how to invite anyway. */
export function noMatchMessage(text) {
  return /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(String(text || '').trim())
    ? 'No account matches. Add person will invite this address.'
    : 'No account matches. Type their full email to invite them.'
}
