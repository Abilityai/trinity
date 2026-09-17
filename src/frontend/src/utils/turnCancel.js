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
 * Cancel responses that mean "there was nothing left to stop".
 *
 * Review finding: the DB pre-check on the two new routes answers
 * `already_terminal`, but the AGENT answers `already_finished` — HTTP **200**,
 * from `agent_server/routers/chat.py`, when its process registry finds the turn
 * already gone — and `_proxy_terminate_and_finalize` passes that dict straight
 * through. Testing only for `already_terminal` therefore treated the
 * agent-level race, which is the one this feature actually races, as a
 * successful cancel: the reply arrived AND the message was prepended back into
 * the composer as though it had been stopped.
 *
 * One predicate, so a third spelling has one place to be added.
 */
export const NOOP_CANCEL_STATUSES = Object.freeze(['already_terminal', 'already_finished'])

export function isNoopCancel(status) {
  return NOOP_CANCEL_STATUSES.includes(status)
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
  if (!ownsEscape(event)) return false
  if (!inFlight || cancelling) return false
  return !overlays.some(Boolean)
}

/**
 * The three preconditions every Escape owner on these surfaces shares.
 *
 * Extracted in #2598 rather than left inline, because the bug was precisely
 * that a SECOND Escape owner appeared and re-decided them — badly. `ent#534`
 * gave the Workspace voice call the first branch of its handler, above
 * `shouldCancelOnEscape` and reading none of this, so an overlay that claimed
 * the keystroke in the capture phase with `preventDefault()` closed AND ended
 * the call on one press. `preventDefault()` cannot help where nothing reads it.
 *
 *   - not Escape → not ours;
 *   - a composed IME session uses Escape to abandon a candidate, so taking it
 *     here acts on the turn instead of the character;
 *   - `defaultPrevented` is the protocol on this surface: an overlay claims
 *     Escape in the capture phase, and every owner downstream must yield.
 */
function ownsEscape(event) {
  if (!event || event.key !== 'Escape') return false
  if (event.isComposing) return false
  if (event.defaultPrevented) return false
  return true
}

/**
 * Whether an Escape keydown should end an in-flight voice call (#2598).
 *
 * A sibling of `shouldCancelOnEscape`, not a special case of it: while a call
 * is up the composer and the picker are inert, so there is no turn to cancel
 * and no overlay list to consult — the only question is whether anything
 * NEARER the keystroke already claimed it.
 *
 * It is a rule here rather than a guard clause in the SFC for the reason this
 * whole module exists: `vitest.config.js` runs `environment: 'node'` with no
 * mount harness, so a branch decided inside a component is a branch no test can
 * reach — which is exactly how ent#534's version shipped able to ignore
 * `defaultPrevented` and `isComposing` at once.
 *
 * The caller stays responsible for ordering: a call is the loudest thing on the
 * surface, so this is asked FIRST and `shouldCancelOnEscape` only after it says
 * no. Both now start from the same `ownsEscape` preconditions, so the two
 * branches cannot drift apart again.
 */
export function shouldEndCallOnEscape(event, { callActive } = {}) {
  if (!ownsEscape(event)) return false
  return !!callActive
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
