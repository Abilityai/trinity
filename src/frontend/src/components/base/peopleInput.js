/**
 * BasePeopleInput's decisions (trinity-enterprise#1153), kept pure so a spec can
 * execute them without mounting (the ent#392 pattern). The component holds only
 * the state; what a key does, what a row reads and when a must-pick field is
 * invalid are decided here.
 */
import { platformRoleLabel } from '../../utils/workspaceOnly'

/** Below this the field never asks: a picker, never a directory (the #450 bound). */
export const PEOPLE_INPUT_MIN_QUERY = 2
export const PEOPLE_INPUT_DEBOUNCE_MS = 200

/** A row reads the name first and the email second; the role only when the server sent one. */
export function personRows(people) {
  return (people || []).map((p) => ({
    key: p.email,
    primary: p.label || p.email,
    secondary: p.role ? `${p.email} · ${platformRoleLabel(p.role)}` : p.email,
  }))
}

/** Arrow keys over `count` rows, wrapping. -1 means none. */
export function nextActive(active, count, delta) {
  if (count <= 0) return -1
  if (active < 0) return delta > 0 ? 0 : count - 1
  return (active + delta + count) % count
}

/**
 * What a key does while the field has focus. Only an OPEN list reacts; Enter
 * picks only a highlighted row, so a plain Enter falls through to the form.
 */
export function keyAction(key, { open, active, count }) {
  if (!open) return { action: 'none' }
  if (key === 'Escape') return { action: 'close' }
  if (key === 'ArrowDown' || key === 'ArrowUp') {
    return { action: 'move', active: nextActive(active, count, key === 'ArrowDown' ? 1 : -1) }
  }
  if (key === 'Enter' && active >= 0 && active < count) return { action: 'pick', active }
  return { action: 'none' }
}

/** Is `text` exactly what picking `person` wrote? */
export function isPickedText(text, person) {
  return !!person && String(text || '').trim().toLowerCase() === String(person.email || '').toLowerCase()
}

export const CHOOSE_FROM_LIST = 'Choose a person from the list. Type part of their name or email, then pick them.'

export function defaultNoMatch(text) {
  return `No one on this instance matches “${String(text || '').trim()}”.`
}

/**
 * A must-pick field's named validation: '' when it is fine (suggest mode, empty,
 * accepted by the caller, or a person was chosen). Text the server said matches no one gets `noMatch`;
 * anything else unchosen is told to pick from the list.
 */
export function mustPickError({ mode, text, picked, matchedNone, noMatch = defaultNoMatch, accepted = false }) {
  if (mode !== 'pick') return ''
  const t = String(text || '').trim()
  // `accepted`: text the caller takes without a pick (trinity-enterprise#1155 —
  // a task owner written `role:<id>`, or the value the field opened with).
  if (!t || accepted || isPickedText(t, picked)) return ''
  return matchedNone ? noMatch(t) : CHOOSE_FROM_LIST
}
