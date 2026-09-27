/**
 * Decidable logic behind FirstRunOverlay.vue (ent#581, epic ent#54).
 *
 * Every visibility term and every skip destination lives here, where the spec
 * can assert on it directly — the ent#392 / #2380 / ent#437 rule, because
 * `vitest.config.js` runs `environment: 'node'` with no component-mount
 * harness. The overlay is a dispatcher over this module. No `.vue` imports.
 */
import { DOMAIN_POSTURE, HARDENING_GUIDE_DISMISSED_KEY } from './hardeningGuide'
import { readEmailNudgeDismissed, readSnoozedUntil } from './telemetryConsent'

// Per-browser, matching the keys the surfaces this replaces already used.
export const FIRST_RUN_CLOSED_KEY = 'trinity_first_run_closed'
export const FIRST_RUN_SKIPPED_KEY = 'trinity_first_run_skipped' // JSON array of step keys

/**
 * Each step is `eligible` (who it is for — role, provenance, config) AND
 * `pending` (is there still something to do). `applies` is the conjunction.
 *
 * The split exists for re-opening: `?onboarding=1` and Settings → General →
 * Re-run setup show every ELIGIBLE step, so a completed one renders as done
 * instead of vanishing — a rail filtered on `applies` alone could never show a
 * check mark. An auto-open shows only the steps that apply.
 *
 * `ridesAlong` (ent#581 spec correction): a step with no derivable completion
 * never opens the overlay on its own, or every admin on an established fleet
 * would meet a blocking overlay on every new browser.
 */
const REGISTRY = [
  {
    key: 'secure',
    name: 'Secure this instance',
    tag: 'Recommended',
    required: false,
    minutes: 2,
    settingsPath: 'Settings → General',
    // Absorbs HardeningGuide.vue (#2380/#2564). `marketplaceInstall` is the
    // server-resolved `hardening_guide_eligible` PROVENANCE gate — never TLS
    // state, or the whole managed fleet behind Tailscale would qualify.
    eligible: (c) => !!c.isAdmin && !!c.marketplaceInstall,
    pending: (c) => c.tlsPosture !== DOMAIN_POSTURE,
  },
  {
    key: 'email',
    name: 'Sign-in email',
    tag: 'Optional',
    required: false,
    minutes: 1,
    settingsPath: 'Settings → General',
    // FALLBACK ONLY. A marketplace operator sets their email while claiming the
    // instance at /setup (ent#580), so this is false and the step never
    // renders — the "no duplicate asks" AC is this predicate. It survives for
    // the ADMIN_PASSWORD-provisioned install, where nobody was ever asked.
    // Absorbs FinishSetupCard section 1 (#2381).
    eligible: (c) => !!c.isAdmin,
    pending: (c) => !c.hasEmail,
  },
  {
    key: 'claude',
    name: 'Connect Claude',
    tag: 'Required',
    required: true, // the ONLY blocking step
    minutes: 2,
    settingsPath: 'Settings → Integrations',
    eligible: (c) => !!c.isAdmin,
    pending: (c) => !c.claudeAuthConfigured,
  },
  {
    key: 'keys',
    name: 'Other keys',
    tag: 'Optional',
    required: false,
    minutes: 2,
    settingsPath: 'Settings → Integrations',
    ridesAlong: true,
    eligible: (c) => !!c.isAdmin,
    // No state says "the optional keys are done"; it reads done only once the
    // step itself emits `complete` this session.
    pending: () => true,
  },
  {
    key: 'agent',
    name: 'Your first agent',
    tag: 'Optional',
    required: false,
    minutes: 2,
    settingsPath: 'the dashboard',
    // Absorbs FrontDeskPanel (ent#319) + the ent#52 wizard intro. The server's
    // first-run flag — no agent of your own yet — is both the auto-open signal
    // the front desk used and this step's completion: create one and it is done.
    eligible: () => true,
    pending: (c) => !!c.firstRun,
  },
  {
    key: 'sharing',
    name: 'Usage sharing',
    tag: 'Optional',
    required: false,
    minutes: 1,
    settingsPath: 'Settings → General',
    // Absorbs FinishSetupCard section 2 (ent#437 / ent#12). Same server terms:
    // consent on, or "Don't ask again", or disabled by config.
    eligible: (c) => !!c.isAdmin && !c.telemetryHardDisabled,
    pending: (c) => !c.telemetryEnabled && !c.telemetryDismissed,
  },
]

export const FIRST_RUN_STEPS = REGISTRY.map((s) => ({
  ...s,
  applies: (c = {}) => s.eligible(c) && s.pending(c),
}))

/** The steps THIS install actually has left. Order is fixed; membership is not. */
export function applicableSteps(ctx = {}) {
  return FIRST_RUN_STEPS.filter((s) => s.applies(ctx))
}

/** Every step this user could be shown — the re-open set. */
export function eligibleSteps(ctx = {}) {
  return FIRST_RUN_STEPS.filter((s) => s.eligible(ctx))
}

/**
 * The rail's membership, snapshotted by the chassis when the overlay OPENS so a
 * step that gets done mid-session stays in the rail with a check instead of
 * vanishing and renumbering everything after it.
 */
export function stepsForSession(ctx = {}) {
  return ctx.forced ? eligibleSteps(ctx) : applicableSteps(ctx)
}

/**
 * Should the overlay open?
 *
 * `flagsLoaded`, `profileVerified` and `firstRunLoaded` are REQUIRED terms, not
 * defensive ones: every flag starts in its hidden value and `role` answers
 * 'user' until /api/users/me lands, so without them the overlay flashes over an
 * established fleet on every load (the #2198 rationale, and the exact bug that
 * shipped #2380 and #2381 permanently hidden). `flagsLoaded` must also mean the
 * fetch SUCCEEDED: a failed read reports `claude_auth_configured` false, and a
 * required step must never block a working fleet on a network blip.
 *
 * Skipped steps do not re-open it: a skip is an answer.
 */
export function isFirstRunOverlayVisible(ctx = {}) {
  if (!ctx.flagsLoaded || !ctx.profileVerified || !ctx.firstRunLoaded) return false
  if (ctx.forced) return true // ?onboarding=1 / Settings → General → Re-run setup
  if (ctx.closed) return false
  const skipped = ctx.skipped || []
  return applicableSteps(ctx).some((s) => !s.ridesAlong && !skipped.includes(s.key))
}

/**
 * Does this open start the activation funnel (`setup_started`, ent#184 /
 * ent#437)? Keeps the ent#52 wizard's meaning — it auto-opened only for an
 * operator with nothing running yet — so: an open on its own (never `forced`)
 * over a Claude credential still to connect or no agent of your own. A re-run,
 * or a new browser opening for email / sharing / secure on an established
 * fleet, would inflate the top of the funnel.
 */
export function countsAsSetupStart(ctx = {}) {
  return !ctx.forced && FIRST_RUN_STEPS.some((s) => ['claude', 'agent'].includes(s.key) && s.applies(ctx))
}

/** Rail state for one step. Completion is DERIVED, never stored. */
export function stepState({ step, currentKey, ctx = {}, skipped = [], completed = [] }) {
  if (step.key === currentKey) return 'current'
  if (!step.applies(ctx) || completed.includes(step.key)) return 'done' // the predicate went false = it got done
  if (skipped.includes(step.key)) return 'skipped'
  return 'upcoming'
}

/**
 * Continue on a required step is live only once the step emitted `complete`
 * or, after the chassis refreshed the stores, no longer applies. Anything
 * softer makes the Required badge decoration (spec §3).
 */
export function canContinue({ step, ctx = {}, completed = [] }) {
  if (!step?.required) return true
  return completed.includes(step.key) || !step.applies(ctx)
}

/** "About N minutes" for the welcome step. */
export function estimateMinutes(steps = []) {
  return steps.reduce((sum, s) => sum + (s.minutes || 0), 0)
}

/**
 * The constellation's four outer nodes are the four capabilities this sequence
 * configures, in registry order. A node is lit when nothing is left for it in
 * this session: every one of its steps is done, or none is in the session.
 */
export const CONSTELLATION_NODES = [['secure'], ['email', 'keys'], ['claude'], ['sharing']]

export function litNodes(states = {}) {
  return CONSTELLATION_NODES.flatMap((keys, i) =>
    keys.every((k) => !(k in states) || states[k] === 'done') ? [i + 1] : []
  )
}

// --- per-browser state, every read/write guarded (private mode, blocked storage)

export function readFirstRunClosed() {
  try {
    return localStorage.getItem(FIRST_RUN_CLOSED_KEY) === '1'
  } catch {
    return false
  }
}

export function persistFirstRunClosed() {
  try {
    localStorage.setItem(FIRST_RUN_CLOSED_KEY, '1')
    return true
  } catch (e) {
    console.warn('[firstRun] could not persist close:', e?.message || e)
    return false
  }
}

/**
 * A "no" given to a card this overlay replaced is honoured as a skip of the
 * step that absorbed it — otherwise an upgrade re-asks every operator who
 * already declined. The telemetry snooze counts only while it lasts.
 */
function legacySkips(now) {
  const skips = []
  try {
    if (localStorage.getItem(HARDENING_GUIDE_DISMISSED_KEY)) skips.push('secure')
    if (localStorage.getItem('trinity_front_desk_dismissed')) skips.push('agent') // ent#319
    if (localStorage.getItem('trinity_onboarding_dismissed_v1')) skips.push('agent') // ent#52
  } catch {
    /* unreadable storage: nothing was declined */
  }
  if (readEmailNudgeDismissed()) skips.push('email')
  if (readSnoozedUntil(now)) skips.push('sharing')
  return skips
}

export function readFirstRunSkipped(now = Date.now()) {
  let stored = []
  try {
    const parsed = JSON.parse(localStorage.getItem(FIRST_RUN_SKIPPED_KEY) || '[]')
    if (Array.isArray(parsed)) stored = parsed.filter((k) => typeof k === 'string')
  } catch {
    /* garbage or unreadable storage reads as nothing skipped */
  }
  return [...new Set([...stored, ...legacySkips(now)])]
}

export function persistFirstRunSkipped(keys = []) {
  try {
    localStorage.setItem(FIRST_RUN_SKIPPED_KEY, JSON.stringify([...new Set(keys)]))
    return true
  } catch (e) {
    console.warn('[firstRun] could not persist skip:', e?.message || e)
    return false
  }
}
