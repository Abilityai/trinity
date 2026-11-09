/**
 * #4281 — how the page says what tier a schedule's runs get, what a take-over
 * sends back, and whether a run ran restricted.
 *
 * The server decides every fact (slices A, C, D): `runs_as`, `runs_as_reason`,
 * `created_by` (only on an owner-tier read, D5), `created_by_you`,
 * `can_take_over`, and `turn_integrity.restricted_turn` on a run. This module
 * only words them, and builds the take-over body from the version the person
 * reviewed, never from a later read.
 */

const SHARED_PERSON = 'shared_person'
const RESTRICTED = 'no shell commands and no tools the owner added'

/**
 * The row's badge, reason and creator line, or null when there is nothing to
 * say. `runs_as` null means "not computed": no badge, and never read as owner.
 */
export function runTierView (schedule) {
  if (schedule?.runs_as !== SHARED_PERSON) return null
  const reason = schedule.runs_as_reason?.message || `Its runs have a shared person's access: ${RESTRICTED}.`
  let creatorLine = null
  if (schedule.created_by) creatorLine = `Created by ${schedule.created_by}`
  else if (schedule.created_by_you) creatorLine = 'You created this schedule'
  return { badge: 'Runs without shell', reason, creatorLine }
}

/**
 * D9: an owner-tier person's "Run now" on a shared person's schedule runs once
 * with THEIR access, not the schedule's. Only that case is warned about; a
 * share holder's own run has their own (shared) access.
 */
export function warnsOnRunNow (schedule, ownerTier) {
  return Boolean(ownerTier) && schedule?.runs_as === SHARED_PERSON
}

const normEmail = (v) => String(v ?? '').trim().toLowerCase()

/**
 * D8: `none` (no Workspace delivery), `own` (the viewer's own Workspace, left
 * alone) or `other` (another person's: the viewer must choose clear or keep).
 * An unknown viewer email is never taken as "own".
 */
export function deliveryDecision (schedule, viewerEmail) {
  const target = normEmail(schedule?.deliver_to_workspace_email)
  if (!target) return 'none'
  const me = normEmail(viewerEmail)
  return me && me === target ? 'own' : 'other'
}

/**
 * The take-over request for the version `snapshot` (the schedule read the
 * person reviewed). Every reviewed field is echoed verbatim, nulls included;
 * the server refuses the take-over if any differs from what it holds.
 */
export function takeOverBody (snapshot, { viewerEmail = null, delivery = null } = {}) {
  const body = {
    expected_updated_at: snapshot.updated_at,
    reviewed: {
      message: snapshot.message,
      allowed_tools: snapshot.allowed_tools ?? null,
      model: snapshot.model ?? null,
      validation_enabled: snapshot.validation_enabled,
      validation_prompt: snapshot.validation_prompt ?? null,
    },
  }
  if (deliveryDecision(snapshot, viewerEmail) !== 'other') return body
  if (delivery !== 'clear' && delivery !== 'keep') {
    throw new Error(`take-over of schedule ${snapshot.id}: another person's delivery needs "clear" or "keep", got ${delivery}`)
  }
  body.delivery = delivery
  if (delivery === 'keep') body.keep_delivery_to = snapshot.deliver_to_workspace_email
  return body
}

const REFUSALS = {
  schedule_changed_since_review:
    'This schedule changed after you opened this review. The current version is shown now: review it and take it over again.',
  schedule_run_in_progress:
    'A run of this schedule is in progress or waiting to retry. Take it over once that run has finished.',
  schedule_not_restricted:
    "This schedule's runs already have the owner's access, so there is nothing to take over.",
  schedule_delivers_to_another_person:
    "This schedule delivers to another person's Workspace. Choose whether to stop or keep that delivery, then take it over again.",
  schedule_not_found:
    'This schedule no longer exists, so there is nothing to take over.',
}

/** A take-over refusal in user words; an unknown code reads `fallback`. */
export function takeOverRefusalText (code, fallback) {
  return REFUSALS[code] || fallback
}

/**
 * Slice C's record on a run, or null. `turn_integrity` arrives as the JSON
 * text of the column (or an object); a run without the key, or a column that
 * does not parse, has nothing to show.
 */
export function restrictedTurnOf (turnIntegrity) {
  if (!turnIntegrity) return null
  let parsed = turnIntegrity
  if (typeof turnIntegrity === 'string') {
    try {
      parsed = JSON.parse(turnIntegrity)
    } catch {
      return null
    }
  }
  const record = parsed?.restricted_turn
  return record && typeof record === 'object' && !Array.isArray(record) ? record : null
}

const RUN_SENTENCES = {
  creator_not_owner_tier:
    `This run had the access of the person who created the schedule, who is not the agent's owner or an admin: ${RESTRICTED}.`,
  creator_missing:
    `The account that created the schedule no longer exists, so this run had a shared person's access: ${RESTRICTED}.`,
  agent_has_no_owner:
    `The agent had no owner on record, so this run had a shared person's access: ${RESTRICTED}.`,
  schedule_unreadable:
    `The schedule could not be read while this run was going (deleted or changed mid-run), so the run was limited: ${RESTRICTED}.`,
}

/** The run's restriction as one sentence, by cause. */
export function restrictedRunSentence (record) {
  return RUN_SENTENCES[record?.cause] || `This run had a shared person's access: ${RESTRICTED}.`
}
