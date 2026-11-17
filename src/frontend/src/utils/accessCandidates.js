/**
 * The Access tab's add-field suggestions (trinity-enterprise#450): what the panel
 * decides, kept pure so a spec can execute it.
 */
import { PEOPLE_INPUT_MIN_QUERY, nextActive, personRows } from '../components/base/peopleInput'

// trinity-enterprise#1153: the field is BasePeopleInput now, so the row shape,
// the keyboard and the minimum are the primitive's. Re-exported here so the
// Access tab's own rules read as one list.
export const MIN_QUERY = PEOPLE_INPUT_MIN_QUERY
export { nextActive }
export const candidateRows = personRows

/** Would this text produce a query? The popup never opens on a shorter one. */
export function wantsSuggestions(text) {
  return String(text || '').trim().length >= MIN_QUERY
}

/** A query that matched no one says so, and how to invite anyway. */
export function noMatchMessage(text) {
  return /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(String(text || '').trim())
    ? 'No account matches. Add person will invite this address.'
    : 'No account matches. Type their full email to invite them.'
}
