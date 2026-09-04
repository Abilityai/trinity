/**
 * Decidable logic behind the Finish-setup card's sections (ent#437).
 *
 * Split out of the SFC because `vitest.config.js` runs `environment: 'node'`
 * with no component-mount harness, so a decision left inside a component is one
 * no test can reach — the ent#392 / #2380 rule. The component is a dispatcher
 * over this module; every visibility term and every sentence of consent copy
 * lives here, where the spec can assert on it directly.
 */

// "Not now" is a per-browser SNOOZE, not a server marker: a one-shot ask at the
// coldest moment is ent#12's own "pure opt-in gets almost no data" trap. The
// server marker exists for the explicit "Don't ask again" and for consent.
export const TELEMETRY_SNOOZE_KEY = 'trinity_telemetry_ask_snoozed_until'
export const TELEMETRY_WARM_SHOWN_KEY = 'trinity_telemetry_warm_ask_shown'
export const SNOOZE_DAYS = 14

// The sign-in-email nudge (#2381) moved into the same card; its per-browser
// dismissal key is unchanged so an existing dismissal keeps holding.
export const EMAIL_NUDGE_DISMISS_KEY = 'trinity-admin-email-nudge-dismissed'

/**
 * Should the usage-sharing section render?
 *
 * `flagsLoaded` is a required term (the `stores/firstRun.js` `loaded`
 * rationale): every flag below starts in its HIDDEN value, and a hidden→shown
 * flip after the fetch is indistinguishable from a real one without it.
 *
 * `isAdmin` is a real gate: the actions are admin + human-only routes, and the
 * preview discloses fleet-wide counts.
 *
 * `dismissed` is the SERVER marker (consented, or "Don't ask again"); `snoozed`
 * is the per-browser one. The warm variant may override an active snooze exactly
 * once per browser (`warmShown`), then the normal rule applies.
 */
export function isTelemetryConsentVisible({
  flagsLoaded = false,
  profileVerified = false,
  isAdmin = false,
  enabled = false,
  hardDisabled = false,
  dismissed = true,
  firstValue = false,
  snoozed = false,
  warmShown = false,
} = {}) {
  if (!flagsLoaded) return false
  if (!profileVerified || !isAdmin) return false
  if (enabled || hardDisabled || dismissed) return false
  if (!snoozed) return true
  return firstValue && !warmShown
}

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
 * install fault: from the default URL it means the hosted service is not live
 * yet; from an override it means only that the receiver answered 404.
 */
export function receiverCopy(hint, shareUrl = '') {
  switch (hint) {
    case 'ok':
      return 'The receiving service acknowledged the last send.'
    case 'receiver_not_live':
      return 'The receiving service answered 404 — the hosted service is not live yet, so sends are recorded here and retried daily.'
    case 'receiver_404':
      return `The receiver at ${shareUrl} answered 404. Check TELEMETRY_SHARING_URL.`
    case 'failed':
      return 'The last send failed; it is recorded below and retried daily.'
    default:
      return 'Nothing has been sent yet.'
  }
}

/** Ordering the email section needs — moved verbatim from AdminEmailNudge (#2381). */
export function isEmailNudgeVisible({
  profileVerified = false,
  isAdmin = false,
  hasEmail = true,
  dismissed = false,
} = {}) {
  return profileVerified && isAdmin && !hasEmail && !dismissed
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

export function persistSnooze(days = SNOOZE_DAYS, now = Date.now()) {
  try {
    localStorage.setItem(TELEMETRY_SNOOZE_KEY, new Date(now + days * 86400000).toISOString())
    return true
  } catch (e) {
    console.warn('[telemetryConsent] could not persist snooze:', e?.message || e)
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

export function persistEmailNudgeDismissed() {
  try {
    localStorage.setItem(EMAIL_NUDGE_DISMISS_KEY, 'true')
    return true
  } catch {
    return false
  }
}
