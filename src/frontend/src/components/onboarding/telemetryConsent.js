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
 * The receiver line. A 404 is stated for what it is, never dressed up as an
 * install fault. The hosted receiver has been live since 2026-09-04
 * (trinity-enterprise#190), so a 404 from the default address is an anomaly
 * to look at, not the expected state; from an override it means only that the
 * receiver answered 404.
 */
export function receiverCopy(hint, shareUrl = '') {
  switch (hint) {
    case 'ok':
      return 'The receiving service acknowledged the last send.'
    case 'receiver_not_live':
      return 'The receiving service answered 404 at the default address. The send is recorded here and retried automatically.'
    case 'receiver_404':
      return `The receiver at ${shareUrl} answered 404. Check TELEMETRY_SHARING_URL.`
    case 'failed':
      return 'The last send failed; it is recorded below and retried automatically.'
    default:
      return 'Nothing has been sent yet.'
  }
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
