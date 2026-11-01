/**
 * The Assign skills dialog's draft rules (trinity-enterprise#539):
 * Select all / Deselect all, and whether Save must ask first.
 *
 * Pure, so `tests/unit/skillAssignDraft.spec.js` runs every row type and every
 * reason; `components/skills/SkillAssignModal.vue` wires them.
 *
 * The draft is the agent's INDIVIDUAL assignments (ent#530), and both buttons
 * act on the rows the dialog lists, nothing else:
 *   - a set's row is ticked and locked: never added to the draft, never removed;
 *   - an assignment whose skill left the library has no row, so it stays
 *     (operator ruling, 2026-10-22: its card's Unassign removes it);
 *   - Select all never ticks a deprecated skill, and leaves the tick a
 *     deprecated skill already has (operator ruling, 2026-10-22);
 *   - Select all never ticks a name the PUT would refuse: the library lists
 *     every directory with a SKILL.md, unvalidated, and one refused name fails
 *     the whole save.
 * A filter, if one arrives, narrows `rows`, and both buttons follow it.
 */
import { SKILL_NAME_RE } from './skillCards'

/** Why a button is disabled: each says what is missing, never just "unavailable". */
export const BULK_TITLES = Object.freeze({
  loading: 'The library is still loading',
  unconfigured: 'No skills library is configured',
  empty: 'The library has no skills yet',
  saving: 'Wait for the save to finish',
  allTicked: 'Every skill listed here is already ticked',
  deprecatedLeft: 'The skills left unticked are deprecated: tick one by hand to assign it',
  unassignableLeft: "The skills left unticked have names the platform can't assign",
  noneTicked: 'No skill listed here is ticked',
  onlySetTicked: 'Only skills a set assigns are ticked here: unassign the set to remove them',
})

export function selectAllTitle(count, { skipsUnassignable = false } = {}) {
  const except = skipsUnassignable ? "deprecated ones and names the platform can't assign" : 'deprecated ones'
  return `Tick every skill listed here except ${except} (${count} to tick)`
}

export function deselectAllTitle(count) {
  return `Untick every skill listed here except ones only a set assigns (${count} to untick). `
    + 'Saving then unassigns them, and asks first if that would drop an approval requirement.'
}

function selectReason({ loaded, emptyReason, saving, toAdd, unticked }) {
  if (!loaded) return BULK_TITLES.loading
  if (emptyReason === 'library_unconfigured') return BULK_TITLES.unconfigured
  if (emptyReason === 'library_empty') return BULK_TITLES.empty
  if (saving) return BULK_TITLES.saving
  if (toAdd.length) return null
  if (!unticked.length) return BULK_TITLES.allTicked
  return unticked.some((s) => s.deprecated) ? BULK_TITLES.deprecatedLeft : BULK_TITLES.unassignableLeft
}

function deselectReason({ loaded, saving, toRemove, hasLocked }) {
  if (!loaded) return BULK_TITLES.loading
  if (saving) return BULK_TITLES.saving
  if (toRemove.length) return null
  return hasLocked ? BULK_TITLES.onlySetTicked : BULK_TITLES.noneTicked
}

/**
 * What each button would change, and whether it may run now.
 * @param {object} p
 * @param {Array<{name: string, deprecated?: boolean}>} p.rows  the rows the dialog lists
 * @param {string[]} p.draft  the individual names ticked now
 * @param {Set<string>} p.locked  names present only through a set
 * @param {boolean} p.loaded  the agent's assignments and the library have answered
 * @param {string|null} p.emptyReason  `store.emptyReason`
 * @param {boolean} p.saving  a save is in flight
 */
export function bulkSelection({
  rows = [], draft = [], locked = new Set(), loaded = false, emptyReason = null, saving = false,
} = {}) {
  const ticked = new Set(draft)
  const editable = rows.filter((s) => !locked.has(s.name))
  const unticked = editable.filter((s) => !ticked.has(s.name))
  const toAdd = unticked.filter((s) => !s.deprecated && SKILL_NAME_RE.test(s.name)).map((s) => s.name)
  const toRemove = editable.filter((s) => ticked.has(s.name)).map((s) => s.name)
  const hasLocked = rows.some((s) => locked.has(s.name))
  const skipsUnassignable = unticked.some((s) => !s.deprecated && !SKILL_NAME_RE.test(s.name))

  const selectWhy = selectReason({ loaded, emptyReason, saving, toAdd, unticked })
  const deselectWhy = deselectReason({ loaded, saving, toRemove, hasLocked })
  return {
    toAdd,
    toRemove,
    select: { disabled: Boolean(selectWhy), title: selectWhy || selectAllTitle(toAdd.length, { skipsUnassignable }) },
    deselect: { disabled: Boolean(deselectWhy), title: deselectWhy || deselectAllTitle(toRemove.length) },
  }
}

export function withSelectAll(draft, toAdd) {
  return [...draft, ...toAdd]
}

export function withDeselectAll(draft, toRemove) {
  const drop = new Set(toRemove)
  return draft.filter((name) => !drop.has(name))
}

/**
 * The saved names this save would take off the agent. A name an assigned set
 * still holds is only demoted, so it stays; pass an empty `setHeld` when the
 * sets cannot be trusted (one is unresolved), because the server then keeps
 * only rows that are set-only.
 */
export function removedNames({ saved = [], draft = [], setHeld = new Set() } = {}) {
  const keep = new Set(draft)
  return [...saved].filter((name) => !keep.has(name) && !setHeld.has(name))
}

/**
 * The removed names whose approval requirement a save would put at risk: a
 * person's unassign drops it once the package has left (trinity-enterprise#753).
 * A name in `conflict` is the agent's own skill, which stays, and the server
 * keeps its requirement, so it never asks. With the approval map unread, every
 * other removal asks.
 * @returns {{names: string[], gatesKnown: boolean} | null}
 */
export function gatedRemovals({
  saved = [], draft = [], setHeld = new Set(), gated = new Set(), conflict = new Set(), gatesKnown = false,
} = {}) {
  const removed = removedNames({ saved, draft, setHeld }).filter((name) => !conflict.has(name))
  if (!removed.length) return null
  if (!gatesKnown) return { names: removed, gatesKnown: false }
  const names = removed.filter((name) => gated.has(name.toLowerCase()))
  return names.length ? { names, gatesKnown: true } : null
}

const LISTED_NAMES = 5

function nameList(names) {
  const shown = names.slice(0, LISTED_NAMES).map((name) => `/${name}`)
  const more = names.length - shown.length
  if (more > 0) return `${shown.join(', ')} and ${more} more`
  if (shown.length === 1) return shown[0]
  return `${shown.slice(0, -1).join(', ')} and ${shown[shown.length - 1]}`
}

export const REMOVAL_CONFIRM_TEXT = 'Save and unassign'

/** The confirm's title and message, in the future tense (the Unassign confirm's rule). */
export function removalConfirm({ names, gatesKnown }) {
  const one = names.length === 1
  const list = nameList(names)
  if (gatesKnown) {
    return {
      title: one ? `Unassign /${names[0]}?` : `Unassign ${names.length} skills that need approval?`,
      message: one
        ? `${list} needs approval. Saving will remove it from this agent, along with its approval requirement.`
        : `${list} need approval. Saving will remove them from this agent, along with their approval requirements.`,
    }
  }
  return {
    title: one ? `Unassign /${names[0]}?` : `Unassign ${names.length} skills?`,
    message: `Saving will remove ${list} from this agent. Which skills need approval couldn't be read, `
      + `so any approval requirement on ${one ? 'it' : 'them'} will go too.`,
  }
}
