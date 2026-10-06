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
 * The section header's seat line. This cut knows one form: the agent serves
 * the seat its primary holds (the role id on the primary's row). "Holds the
 * seat" needs seat-holder storage that does not exist yet (ent#810 follow-up).
 *
 * @returns {{ form: 'serves', roleId: string, person: string } | { form: 'none' }}
 */
export function seatLine({ assignments = null, primary = null } = {}) {
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
 * new primary row created. The new primary keeps any other kinds they hold;
 * it is a new row, not a re-kinding of an existing one.
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
  const create = { op: 'create', body: { user_id: newUserId, role_id: roleId, kind: 'primary' } }
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
    rollback: [{ op: 'create', body: { user_id: old.user_id, role_id: old.role_id, kind: 'primary' } }],
  }
}

/**
 * Proactive-brief consent as the row can state it. The backend returns one
 * boolean today, so "not asked" and "declined" are one state; the label says
 * only what is known.
 */
export function consentLabel(row) {
  return row?.proactive_consent ? 'Proactive briefs on' : 'Proactive briefs off'
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
  if (state === 'changed') {
    return { tone: 'warning', text: 'The role file changed since this was assigned — review it' }
  }
  if (state === 'current') return { tone: 'ok', text: 'Role file unchanged' }
  return { tone: 'muted', text: 'Role file not checked yet' }
}

/** The backend's own words for a failed write, never a generic line first. */
export function writeError(err, fallback) {
  const detail = err?.response?.data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail.trim()
  if (Array.isArray(detail) && detail[0]?.msg) return detail[0].msg
  return fallback
}
