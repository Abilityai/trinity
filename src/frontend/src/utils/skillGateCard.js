/**
 * trinity-enterprise#755 — the gated-skill approval card.
 *
 * An ask the skill gate raised carries `gate`, the platform's own view of the
 * request (`skill_gate_service.card_view`): the skills, who asked, who it is
 * waiting on, whether this viewer may decide, and the record's state. Only the
 * platform writes it, so its presence is what makes an ask a gate card — never
 * `proposal.kind`, which an agent can write.
 */
import { queueEnding } from './operatorQueue'

export const GATE_REJECT = 'Reject'

export function isGateCard(item) {
  return !!item && !!item.gate && typeof item.gate === 'object'
}

/** A gate card this viewer may not decide: the controls are not offered. */
export function gateReadOnly(item) {
  return isGateCard(item) && item.gate.can_decide !== true
}

/**
 * trinity-enterprise#980 — any platform-raised approval addressed to particular
 * people carries `addressed`: who it waits on, and whether THIS viewer may decide
 * it (`ask_service.may_end`'s own answer). A viewer who may not gets no controls.
 */
export function addressedReadOnly(item) {
  return !!item?.addressed && item.addressed.can_decide !== true
}

/** Who a still-pending approval waits on, for a viewer who cannot decide it. */
export function addressedWaitingOn(item) {
  if (!addressedReadOnly(item) || isGateCard(item) || item.status !== 'pending') return ''
  return (item.addressed.waiting_on || []).join(', ')
}

/** The note beside a picked Reject is the reason the requester is told. */
export function gateRejecting(item, option) {
  return isGateCard(item) && option === GATE_REJECT
}

// state → the outcome line. `variant` is a BaseBadge variant; `ran` marks the
// states with a run to open.
const OUTCOMES = Object.freeze({
  dispatching: { label: 'Approved', text: 'The run is starting.', variant: 'info', ran: true },
  dispatched: { label: 'Approved', text: 'The run is in progress.', variant: 'info', ran: true },
  succeeded: { label: 'Ran', text: 'The run finished successfully.', variant: 'success', ran: true },
  failed: { label: 'Ran', text: 'The run did not succeed.', variant: 'danger', ran: true },
  denied: { label: 'Rejected', text: 'Nothing was run.', variant: 'neutral' },
  expired: {
    label: 'Expired',
    text: 'Nobody decided in time. An expired approval counts as a rejection: nothing was run.',
    variant: 'warning',
  },
  cancelled: { label: 'Cancelled', text: 'Nothing was run.', variant: 'neutral' },
  stale: {
    label: 'Stale',
    text: 'The skill changed after this was asked, so nothing was run. Ask again to have the current version approved.',
    variant: 'warning',
  },
  not_run: { label: 'Not run', text: 'It was approved, but the run could not start.', variant: 'danger' },
  unknown: {
    label: 'Not confirmed',
    text: 'It was approved, but the run could not be confirmed to have started. It was not retried.',
    variant: 'warning',
  },
})

/**
 * How the request ended, or null while it waits. The record trails the ask by
 * one poll cycle, so an ask the clock already ended reads Expired before the
 * record does; an ask just answered has no outcome until the record moves.
 */
export function gateOutcome(item) {
  if (!isGateCard(item)) return null
  const state = item.gate.state
  if (state === 'pending') {
    return queueEnding(item)?.kind === 'expired' ? { state: 'expired', ...OUTCOMES.expired } : null
  }
  const known = OUTCOMES[state]
  return known ? { state, ...known } : null
}

export const GATE_INPUT_FOLD_CHARS = 600
export const GATE_INPUT_FOLD_LINES = 8

/** A frozen input long enough to open folded. */
export function gateInputLong(text) {
  const s = String(text || '')
  return s.length > GATE_INPUT_FOLD_CHARS || s.split('\n').length > GATE_INPUT_FOLD_LINES
}
