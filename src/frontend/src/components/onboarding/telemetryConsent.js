/**
 * Consent copy and per-browser state for the usage-sharing ask (ent#437) —
 * since ent#581 the `sharing` step of the first-run overlay, whose visibility is
 * decided by the registry in `firstRunSteps.js`.
 *
 * Split out of the SFC because `vitest.config.js` runs `environment: 'node'`
 * with no component-mount harness, so a decision left inside a component is one
 * no test can reach — the ent#392 / #2380 rule. Every sentence of consent copy
 * lives here, where the spec can assert on it directly.
 */

// The retired Finish-setup card's per-browser answers: its "Not now" snooze and
// the sign-in-email nudge's dismissal (#2381). Nothing writes them any more; the
// overlay reads them once so an upgrade does not re-ask
// (`firstRunSteps.js::legacySkips`).
export const TELEMETRY_SNOOZE_KEY = 'trinity_telemetry_ask_snoozed_until'
export const EMAIL_NUDGE_DISMISS_KEY = 'trinity-admin-email-nudge-dismissed'

export const TELEMETRY_WARM_SHOWN_KEY = 'trinity_telemetry_warm_ask_shown'

/** Which copy the section speaks: `warm` after the first autonomous success. */
export function consentVariant({ firstValue = false, warmShown = false } = {}) {
  return firstValue && !warmShown ? 'warm' : 'cold'
}

/**
 * Every sentence is asserted by the spec so a later edit cannot promote it to a
 * claim the payload does not earn: "anonymous" and "aggregate" are properties of
 * what is built; "not traceable" is NOT promised here, because unlinkability
 * also depends on the receiver keeping its streams apart.
 */
export const CONSENT_COPY = {
  cold: {
    title: 'Help improve Trinity',
    lead:
      'Share anonymous, aggregate usage so we can see whether the platform works outside our own instance — and so you can see how your setup compares to the fleet.',
  },
  warm: {
    title: 'Your first scheduled run just completed',
    lead:
      'Share anonymous, aggregate usage to see how your setup compares to the fleet. Coarse counts only — the same numbers you can inspect below.',
  },
  shared: {
    detail:
      'Off by default. Coarse counts and version info only — no prompts, no agent content, no emails, no agent names. Turning it on also shares the last 30 days of local counts so your benchmarks are accurate. Keyed by a random share id minted when you turn this on and discarded when you turn it off. Reversible any time in Settings → Usage sharing.',
    previewSummary: 'See what would be sent',
    share: 'Share anonymous usage',
    notNow: 'Not now',
    dontAsk: "Don't ask again",
    shared: 'Sharing is on. Each send shows in Settings → Usage sharing → Recent sends.',
  },
}

/**
 * The name of the receiver an attempt went to, as the panel prints it — the
 * recorded origin (scheme + host + port; never a path, query or userinfo), or
 * "an unknown receiver" for an attempt logged before the origin was recorded
 * (#2571) or carrying a corrupt value. One rule for the per-row "to …" text and
 * the "Last delivered … to …" line; never "null" or "undefined".
 */
export function receiverLabel(host) {
  return typeof host === 'string' && host.trim() ? host.trim() : 'an unknown receiver'
}

/**
 * The receiver line. Decided from what the newest attempt RECORDED — the origin
 * it was posted to — never from the URL configured now: the two differ after an
 * operator tests against a local sink and restores the default, and that is
 * exactly when the old sentence lied (#2571). A 404 is stated for what it is,
 * never dressed up as an install fault. The hosted receiver has been live since
 * 2026-09-04 (trinity-enterprise#190), so a 404 from the default address is an
 * anomaly to look at, not the expected state.
 *
 * `host` is the recorded origin (null when the entry predates the record);
 * `configuredHost` is where sends go now; `mismatch` is the backend's verdict
 * that both are known and differ — it appends the plain sentence naming both
 * and what happens next, which depends on `enabled` (with sharing off there is
 * no next send to promise). Only a real HTTP status may say "answered": a
 * `failed` attempt names where it was sent, because a refused payload or a
 * pre-POST failure never reached anyone.
 */
export function receiverCopy(hint, { host = null, configuredHost = null, mismatch = false, enabled = true } = {}) {
  const named = typeof host === 'string' && host.trim() ? host.trim() : ''
  let line
  switch (hint) {
    case 'ok':
      line = named
        ? `The receiving service at ${named} acknowledged the last send.`
        : 'The receiving service acknowledged the last send; which receiver answered was not recorded (sent before this version).'
      break
    case 'receiver_not_live':
      line = 'The receiving service answered 404 at the default address. The send is recorded here and retried automatically.'
      break
    case 'receiver_404':
      line = named
        ? `The receiver at ${named} answered 404. Check TELEMETRY_SHARING_URL.`
        : 'A receiver answered 404; which one was not recorded (sent before this version). Check TELEMETRY_SHARING_URL.'
      break
    case 'failed':
      line = named
        ? `The last send to ${named} failed; it is recorded below and retried automatically.`
        : 'The last send failed; it is recorded below and retried automatically.'
      break
    default:
      return 'Nothing has been sent yet.'
  }
  if (mismatch && named) {
    const now = typeof configuredHost === 'string' && configuredHost.trim() ? configuredHost.trim() : 'a different address'
    line += ` That send went to ${named}; sharing is now configured for ${now}, which has not seen it.`
    line += enabled ? ' The next scheduled send goes there.' : ' Sharing is off, so nothing further leaves the box.'
  }
  return line
}

// --- per-browser state, every read/write guarded (private mode, blocked storage)

export function readSnoozedUntil(now = Date.now()) {
  try {
    const raw = localStorage.getItem(TELEMETRY_SNOOZE_KEY)
    const until = raw ? Date.parse(raw) : NaN
    return Number.isFinite(until) && until > now
  } catch {
    return false
  }
}

export function readWarmShown() {
  try {
    return localStorage.getItem(TELEMETRY_WARM_SHOWN_KEY) === '1'
  } catch {
    return false
  }
}

export function persistWarmShown() {
  try {
    localStorage.setItem(TELEMETRY_WARM_SHOWN_KEY, '1')
    return true
  } catch {
    return false
  }
}

export function readEmailNudgeDismissed() {
  try {
    return localStorage.getItem(EMAIL_NUDGE_DISMISS_KEY) === 'true'
  } catch {
    return false
  }
}
