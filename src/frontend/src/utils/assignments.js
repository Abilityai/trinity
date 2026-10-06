/**
 * Agent assignments — who an agent serves (trinity-enterprise#810, over the
 * ent#500 endpoints). Pure: the store runs the steps these return, the section
 * renders what these decide, and the tests reach both without a mount.
 *
 * An assignment row is `{ id, user_id, display_name, role_id, kind, ... }`.
 * A person may hold several kinds on one agent (one row per kind); an agent
 * has at most ONE primary row — the backend enforces it with a partial unique
 * index, so the replace flow below never asks for a second.
 */

export const KINDS = ['primary', 'approver', 'collaborator', 'viewer']

export const KIND_LABELS = {
  primary: 'Primary',
  approver: 'Approver',
  collaborator: 'Collaborator',
  viewer: 'Viewer',
}

/** The non-primary kinds, in the order the pickers offer them. */
export const STAKEHOLDER_KINDS = KINDS.filter((k) => k !== 'primary')

export function primaryOf(assignments) {
  return (assignments || []).find((a) => a.kind === 'primary') || null
}

/** Every row but the primary, grouped person by person, in kind order. */
export function stakeholdersOf(assignments) {
  return (assignments || [])
    .filter((a) => a.kind !== 'primary')
    .slice()
    .sort((a, b) => (a.display_name || '').localeCompare(b.display_name || '')
      || KINDS.indexOf(a.kind) - KINDS.indexOf(b.kind))
}

/**
 * The section header's seat line, in the two forms ent#810 names: the agent
 * HOLDS a seat itself (R50a, `held_seat` from the backend — an autonomous
 * player), or it SERVES the seat its primary holds (the role on the primary's
 * row — a companion). Holds wins when both apply (operator ruling 2026-10-06).
 *
 * @returns {{ form: 'holds', roleId: string }
 *         | { form: 'serves', roleId: string, person: string }
 *         | { form: 'none' }}
 */
export function seatLine({ assignments = null, primary = null, held_seat: heldSeat = null } = {}) {
  if (heldSeat) return { form: 'holds', roleId: heldSeat }
  const row = primaryOf(assignments)
  if (row && row.role_id) return { form: 'serves', roleId: row.role_id, person: row.display_name }
  if (!assignments && primary) return { form: 'none', person: primary }
  return { form: 'none' }
}

/**
 * The steps that replace the primary without ever asking for two at once.
 *
 * The old primary row leaves FIRST — re-kinded to `oldBecomes`, or deleted when
 * the old person is to be removed or already holds that kind (a second row of
 * one kind for one person is refused by the backend) — and only then is the
 * new primary is written. A new primary who is already on the agent has their
 * existing row PROMOTED (re-kinded to primary, keeping their seat unless a new
 * one is given) — a second row would list one person twice and give them two
 * seats to choose between (ent#814: a run serves the person's own seat).
 *
 * `rollback` undoes step one, for the store to run if step two fails, so a
 * failed replacement leaves the agent with the primary it had.
 *
 * @param {object[]} assignments  the current rows
 * @param {object} opts
 * @param {number} opts.newUserId
 * @param {string} opts.oldBecomes  one of STAKEHOLDER_KINDS, or 'remove'
 * @param {string} opts.roleId      the seat the new primary holds
 */
export function replacePrimarySteps(assignments, { newUserId, oldBecomes, roleId }) {
  const old = primaryOf(assignments)
  // ent#811: the seat is optional — sent only when there is one.
  const existing = (assignments || []).find((a) => a.user_id === newUserId && a.kind !== 'primary')
  const create = existing
    ? { op: 'update', id: existing.id, body: withRole({ kind: 'primary' }, roleId) }
    : { op: 'create', body: withRole({ user_id: newUserId, kind: 'primary' }, roleId) }
  if (!old) return { steps: [create], rollback: [] }
  if (old.user_id === newUserId) return { steps: [], rollback: [] }

  const oldHoldsKind = (assignments || []).some(
    (a) => a.user_id === old.user_id && a.kind === oldBecomes)
  if (oldBecomes !== 'remove' && !oldHoldsKind) {
    return {
      steps: [{ op: 'update', id: old.id, body: { kind: oldBecomes } }, create],
      rollback: [{ op: 'update', id: old.id, body: { kind: 'primary' } }],
    }
  }
  return {
    steps: [{ op: 'delete', id: old.id }, create],
    rollback: [{ op: 'create', body: withRole({ user_id: old.user_id, kind: 'primary' }, old.role_id) }],
  }
}

/** A create body with `role_id` only when there is a seat to name (ent#811). */
export function withRole(body, roleId) {
  const role = typeof roleId === 'string' ? roleId.trim() : roleId
  return role ? { ...body, role_id: role } : { ...body }
}

/**
 * Proactive-brief consent in three states (ent#817): consented, declined, or
 * never asked. Falls back to the boolean for a backend that predates the state.
 */
export function consentLabel(row) {
  const state = row?.proactive_consent_state || (row?.proactive_consent ? 'consented' : null)
  if (state === 'consented') return 'Proactive briefs: consented'
  if (state === 'declined') return 'Proactive briefs: declined'
  return 'Proactive briefs: not asked'
}

const DRIFT_REASONS = {
  not_recorded: 'set before the check existed — set the seat again to start tracking it',
  no_updated_stamp: 'the role file has no `updated` stamp to compare',
  no_canon: 'this agent declares no canon',
  no_roles_dir: "the canon has no roles/ directory",
  agent_stopped: 'the agent is stopped',
  agent_missing: 'the agent has no container',
  agent_unreachable: 'the agent is not answering',
  rate_limited: 'too many reads just now — reload shortly',
  role_file_invalid: "the role file doesn't parse",
  role_file_unreadable: "the role file couldn't be read",
  canon_read_unavailable: 'this install cannot read canon files',
}

/** The title the canon gives a seat id, or null. */
export function seatTitle(canon, roleId) {
  return (canon?.roles || []).find((r) => r.id === roleId)?.title || null
}

/** The canon's `updated` stamp for a seat id, or null. */
export function seatUpdated(canon, roleId) {
  return (canon?.roles || []).find((r) => r.id === roleId)?.updated || null
}

/**
 * The role check on a row. Only `changed` is a warning; `unknown` is stated
 * with its reason rather than hidden, because "no check has run" and "the
 * role is fine" must not look alike.
 *
 * @returns {{ tone: 'warning'|'muted'|'ok', text: string }}
 */
export function driftNote(row) {
  const state = row?.drift_state || 'unknown'
  if (state === 'no_role') return null
  if (state === 'changed') {
    return { tone: 'warning', text: 'The role file changed since this was assigned — review it' }
  }
  if (state === 'missing') {
    return { tone: 'warning', text: 'No role file for this seat in the canon' }
  }
  if (state === 'current') return { tone: 'ok', text: 'Role file unchanged' }
  const why = DRIFT_REASONS[row?.drift_reason]
  return { tone: 'muted', text: why ? `Role file unknown: ${why}` : 'Role file not checked yet' }
}

/** The backend's own words for a failed write, never a generic line first. */
export function writeError(err, fallback) {
  const detail = err?.response?.data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail.trim()
  if (Array.isArray(detail) && detail[0]?.msg) return detail[0].msg
  return fallback
}
