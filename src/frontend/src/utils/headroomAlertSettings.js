/**
 * Decidable rules for the weekly-headroom alert settings control (ent#434).
 *
 * These live here rather than inside `SubscriptionsPanel.vue` because vitest
 * runs `environment: 'node'` with no component-mount harness — a decision
 * expressed inside an SFC is one no test can reach (the ent#392 precedent that
 * put `portalUtils.js` beside its components). The panel is a dispatcher over
 * these; the rules are unit-tested.
 */

// Mirrors `subscription_headroom_alerts.MIN/MAX_THRESHOLD_PCT`. Used only as a
// fallback when the server has not answered yet — the payload's own bounds win,
// so a future server-side change does not need a matching frontend edit.
export const FALLBACK_MIN_PCT = 50
export const FALLBACK_MAX_PCT = 99

/**
 * Client-side pre-validation mirroring the endpoint's named 422.
 *
 * Returns '' when acceptable, else a message naming the problem, the fix, and
 * an example (design-system principle 17). The legal set is `{0} ∪ [min, max]`
 * — `0` is the documented "off" value (the `operator_queue_retention_days`
 * idiom), so a bare range message would be actively wrong about it.
 */
export function thresholdValidationError(raw, bounds = {}) {
  const text = String(raw ?? '').trim()
  if (text === '') return ''
  if (!/^\d+$/.test(text)) {
    return `Whole numbers only — "${text}" is not one. Example: 75.`
  }
  const value = Number(text)
  if (value === 0) return ''
  const min = bounds.min ?? FALLBACK_MIN_PCT
  const max = bounds.max ?? FALLBACK_MAX_PCT
  if (value < min || value > max) {
    return (
      `Must be 0 to switch the alerts off, or between ${min} and ${max}. ` +
      `${value} is outside that. Example: 75.`
    )
  }
  return ''
}

/** Has the operator actually changed the value away from what the server holds? */
export function thresholdChanged(draft, serverValue) {
  if (serverValue === undefined || serverValue === null) return false
  return String(draft ?? '').trim() !== String(serverValue)
}

/**
 * Every inactive path names ITSELF.
 *
 * AC #4 and the #2217 canary lesson: "no alerts" must be distinguishable from
 * "not checking". A boolean cannot say that, so an unrecognised reason falls
 * through to an honest "this backend did not say" rather than to silence or to
 * a fabricated explanation.
 */
export const INACTIVE_COPY = {
  no_subscriptions:
    'Inactive — no subscriptions are registered, so there is nothing to watch.',
  threshold_disabled:
    'Off — set a threshold above to start watching weekly limits.',
  auto_refresh_off:
    'Inactive — automatic quota checking is switched off above, so nothing is being measured.',
  redis_unavailable:
    'Inactive — the platform cache is unreachable, so the sampler is holding off rather than guessing.',
  count_unavailable:
    'Unknown — the subscription list could not be read, so this cannot be reported honestly.',
}

/**
 * The status line: `{ text, tone }` where tone is 'active' | 'muted'.
 *
 * `loaded` is the store's fetch latch, not a spinner flag: before the first
 * successful read there is no data yet, and claiming either "active" or any
 * specific inactive reason would be inventing one (principle 15 —
 * loading ≠ empty ≠ failed).
 */
export function alertStatusLine(weeklyAlert, loaded) {
  const alert = weeklyAlert || {}
  if (!loaded) return { text: 'Checking…', tone: 'muted' }
  if (alert.active) {
    const escalation = alert.escalation_pct
    const tail =
      escalation && escalation !== alert.threshold_pct
        ? `, escalating at ${escalation}%.`
        : '.'
    return { text: `Active — warning at ${alert.threshold_pct}%${tail}`, tone: 'active' }
  }
  return {
    text:
      INACTIVE_COPY[alert.inactive_reason] ||
      'Inactive — this backend did not report a reason.',
    tone: 'muted',
  }
}


/**
 * Turn a save failure into a message that claims only what was observed.
 *
 * The first version of this said "Check your connection and try again" for
 * ANY thrown value, which sent a real operator hunting a network problem when
 * the actual cause was a stale Pinia store after HMR (the action was
 * `undefined`, so the call threw a TypeError that never reached the network).
 * A message that diagnoses a cause it has not observed is worse than a vague
 * one — principle 25 wants what happened, what it means, what to do.
 */
export function describeSaveFailure(err) {
  const detail = err?.response?.data?.detail
  if (typeof detail === 'string' && detail.trim() !== '') {
    return detail
  }
  if (err?.response) {
    // The server answered and refused, but not in the shape we parse.
    return `The server refused the change (HTTP ${err.response.status}).`
  }
  if (err?.request) {
    // A request went out and nothing came back — this IS the connection case.
    return 'No response from the server. Check your connection and try again.'
  }
  // Never reached the network at all: a client-side bug, most often a stale
  // module after a hot reload. Say that, and name the reliable remedy.
  const kind = err?.name || 'Error'
  return `The change was not sent (${kind}). Reload the page and try again; ` +
    'if it persists this is a bug, not a connection problem.'
}
