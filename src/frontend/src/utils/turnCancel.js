/**
 * ent#155 — stopping an in-flight chat turn, and getting the words back.
 *
 * Three surfaces cancel a turn (Agent Detail chat, the public link, the
 * Workspace) and each owns a different transport, a different auth credential
 * and a different composer. What they must NOT own separately is the two rules
 * a person actually notices: when Escape means "stop" and what happens to the
 * text they typed. Those live here, as pure functions, because
 * `vitest.config.js` runs `environment: 'node'` with no mount harness — a rule
 * decided inside an SFC is a rule no test can reach.
 */

/** Terminal statuses: a cancel that arrives now has already lost the race. */
export const TERMINAL_STATUSES = Object.freeze(['success', 'failed', 'cancelled', 'skipped'])

export function isTerminalStatus(status) {
  return TERMINAL_STATUSES.includes(status)
}

/**
 * Whether an Escape keydown should cancel the turn.
 *
 * Escape is heavily overloaded in this app — it closes modals, dismisses the
 * typeahead, exits the voice overlay — so the AC is explicit that cancelling
 * must never hijack those. The rule is therefore a conjunction of things the
 * caller can actually know, and it is deliberately CONSERVATIVE: when in doubt
 * Escape does nothing, because a missed cancel costs one click on the Stop
 * button while a wrong one destroys a turn the user is still waiting for.
 *
 * `overlays` is the caller's list of "something else owns Escape right now"
 * booleans — a menu open, a modal mounted, a picker showing.
 */
export function shouldCancelOnEscape(event, { inFlight, cancelling, overlays = [] } = {}) {
  if (!event || event.key !== 'Escape') return false
  // A composed IME session uses Escape to abandon a candidate; taking it here
  // would cancel the turn instead of the character.
  if (event.isComposing) return false
  if (event.defaultPrevented) return false
  if (!inFlight || cancelling) return false
  return !overlays.some(Boolean)
}

/**
 * Put the cancelled message back in the composer without destroying a draft.
 *
 * The AC's phrasing is "restored... if the user has already typed a new draft,
 * it is not silently destroyed (restore prepends or merges sensibly)". Prepend
 * is the sensible merge: the cancelled text is what the user is returning to
 * edit, so it belongs where the caret starts, and the draft they typed while
 * waiting follows it. Nothing is ever dropped.
 *
 * Idempotent by construction: restoring a message the composer already starts
 * with returns the draft unchanged, so a double-fire (Escape and Stop, or a
 * retried cancel) cannot stack two copies.
 */
export function restoreDraft(cancelledText, currentDraft) {
  const restored = (cancelledText || '').trim()
  const draft = currentDraft || ''
  if (!restored) return draft
  if (!draft.trim()) return restored
  // Already there — a second cancel must not duplicate it.
  if (draft === restored || draft.startsWith(`${restored}\n\n`)) return draft
  return `${restored}\n\n${draft}`
}

/**
 * What the user is told after a cancel attempt.
 *
 * `cancelled` is not an error and must not render as one (AC: "honest status");
 * a failed terminate is, because the turn is still running and still spending.
 * `already_terminal` is neither — the reply arrived, so there is nothing to
 * say at all.
 */
export function cancelOutcome({ ok, alreadyTerminal }) {
  if (alreadyTerminal) return { kind: 'noop', message: '' }
  if (ok) return { kind: 'cancelled', message: 'Stopped.' }
  return { kind: 'failed', message: "Couldn't stop the turn — it's still running." }
}
