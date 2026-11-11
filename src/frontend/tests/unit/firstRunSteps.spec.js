/**
 * The first-run overlay's registry (ent#581) — every rule that decides what the
 * operator sees lives in `firstRunSteps.js`, because `vitest.config.js` runs
 * `environment: 'node'` with no mount harness (the ent#392 rule).
 *
 * Four rules carry the surface, none visible to a structural check:
 *   1. Never over a fleet that is already set up, and never before the answer
 *      arrives (flags + profile + first-run state, and the flags read SUCCEEDED).
 *   2. No duplicate asks: what /setup collected is never asked again.
 *   3. Completion is derived from state; only close and per-step skips persist.
 *   4. Exactly one blocking step, and it cannot be continued past unsatisfied.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { existsSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

vi.hoisted(() => {
  const mem = new Map()
  globalThis.localStorage = {
    getItem: (k) => (mem.has(k) ? mem.get(k) : null),
    setItem: (k, v) => mem.set(k, String(v)),
    removeItem: (k) => mem.delete(k),
    clear: () => mem.clear(),
  }
})

import {
  FIRST_RUN_CLOSED_KEY,
  FIRST_RUN_SKIPPED_KEY,
  FIRST_RUN_STEPS,
  applicableSteps,
  canContinue,
  countsAsSetupStart,
  eligibleSteps,
  estimateMinutes,
  isFirstRunOverlayVisible,
  litNodes,
  nextStepKey,
  persistFirstRunClosed,
  previousStepKey,
  persistFirstRunSkipped,
  readFirstRunClosed,
  readFirstRunSkipped,
  stepState,
  stepsForSession,
} from '@/components/onboarding/firstRunSteps'

const here = (rel) => fileURLToPath(new URL(rel, import.meta.url))
const read = (rel) => readFileSync(here(rel), 'utf8')
const keys = (steps) => steps.map((s) => s.key)
const step = (key) => FIRST_RUN_STEPS.find((s) => s.key === key)

const ready = { flagsLoaded: true, profileVerified: true, firstRunLoaded: true }

/** A fresh marketplace droplet, first admin login, nothing configured. */
const freshMarketplace = {
  ...ready,
  isAdmin: true,
  marketplaceInstall: true,
  tlsPosture: 'https-ip',
  hasEmail: false,
  claudeAuthConfigured: false,
  telemetryEnabled: false,
  telemetryHardDisabled: false,
  telemetryDismissed: false,
  firstRun: true,
}

/** The established-fleet case the ent#581 spec correction exists for. */
const established = {
  ...freshMarketplace,
  tlsPosture: 'https-domain',
  hasEmail: true,
  claudeAuthConfigured: true,
  telemetryDismissed: true,
  firstRun: false,
  profileDone: true,
  companyDone: true,
  auditChoice: 'start',
}

beforeEach(() => localStorage.clear())

describe('the registry', () => {
  it('has the steps in the fixed order', () => {
    expect(keys(FIRST_RUN_STEPS)).toEqual(['intro', 'secure', 'email', 'profile', 'claude', 'company', 'sharing', 'keys', 'audit', 'agent'])
  })

  it('has exactly one required step: claude', () => {
    expect(keys(FIRST_RUN_STEPS.filter((s) => s.required))).toEqual(['claude'])
  })

  it('names a real Settings tab (or the dashboard, or the Workspace) as every skip destination', () => {
    // The spec's "Settings → Privacy" / "Settings → Setup" do not exist.
    for (const s of FIRST_RUN_STEPS) {
      expect(['Settings → General', 'Settings → Integrations', 'the dashboard', 'the Workspace', 'Get help in the Workspace'], s.key).toContain(
        s.settingsPath
      )
    }
  })

  it('a fresh marketplace admin gets every step', () => {
    expect(keys(applicableSteps(freshMarketplace))).toEqual([
      'intro', 'secure', 'email', 'profile', 'claude', 'company', 'sharing', 'keys', 'audit', 'agent',
    ])
    expect(estimateMinutes(applicableSteps(freshMarketplace))).toBeGreaterThan(0)
  })

  it('never shows an admin step to a non-admin — only the agent step', () => {
    const user = { ...freshMarketplace, isAdmin: false }
    expect(keys(applicableSteps(user))).toEqual(['agent'])
    expect(keys(eligibleSteps(user))).toEqual(['agent'])
  })

  it('never shows `secure` off the marketplace provenance, whatever the posture', () => {
    // The managed fleet runs plain HTTP behind Tailscale: provenance is the only
    // signal that tells it from an unhardened droplet (#2380).
    for (const tlsPosture of ['unconfigured', 'http', 'https-ip']) {
      const managed = { ...freshMarketplace, marketplaceInstall: false, tlsPosture }
      expect(keys(applicableSteps(managed))).not.toContain('secure')
    }
  })

  it('never offers usage sharing when config hard-disables it', () => {
    const hard = { ...freshMarketplace, telemetryHardDisabled: true }
    expect(keys(applicableSteps(hard))).not.toContain('sharing')
    expect(keys(eligibleSteps({ ...hard, forced: true }))).not.toContain('sharing')
  })
})

describe('no duplicate asks after /setup (AC 6)', () => {
  it('an email bound at /setup is not asked for again: it is listed as already done', () => {
    const claimed = { ...freshMarketplace, hasEmail: true }
    expect(keys(applicableSteps(claimed))).not.toContain('email')
    const email = stepsForSession(claimed).find((s) => s.key === 'email')
    expect(stepState({ step: email, currentKey: 'welcome', ctx: claimed })).toBe('done')
  })

  it('a decided usage-sharing ask is not asked again', () => {
    expect(keys(applicableSteps({ ...freshMarketplace, telemetryDismissed: true }))).not.toContain('sharing')
    expect(keys(applicableSteps({ ...freshMarketplace, telemetryEnabled: true }))).not.toContain('sharing')
  })
})

describe('isFirstRunOverlayVisible', () => {
  it('opens on a fresh marketplace install', () => {
    expect(isFirstRunOverlayVisible(freshMarketplace)).toBe(true)
  })

  it('is hidden by default: an empty call is the hidden state', () => {
    expect(isFirstRunOverlayVisible()).toBe(false)
    expect(isFirstRunOverlayVisible({})).toBe(false)
  })

  it.each([
    ['the flags have not loaded (or the read failed)', { flagsLoaded: false }],
    ['the profile is not verified', { profileVerified: false }],
    ['the first-run state has not loaded', { firstRunLoaded: false }],
  ])('stays hidden while %s — even when forced', (_label, override) => {
    expect(isFirstRunOverlayVisible({ ...freshMarketplace, ...override })).toBe(false)
    expect(isFirstRunOverlayVisible({ ...freshMarketplace, ...override, forced: true })).toBe(false)
  })

  it('stays hidden on an established fleet (the spec correction)', () => {
    // `keys` still applies to every admin here, and would open the overlay on
    // every new browser if a ride-along step could open it on its own.
    expect(keys(applicableSteps(established))).toEqual(['intro', 'keys'])
    expect(isFirstRunOverlayVisible(established)).toBe(false)
  })

  it('opens for the server first-run flag alone — a non-admin on a seeded install', () => {
    const user = { ...established, isAdmin: false, firstRun: true }
    expect(isFirstRunOverlayVisible(user)).toBe(true)
    expect(isFirstRunOverlayVisible({ ...user, firstRun: false })).toBe(false)
  })

  it.each([
    ['secure', { tlsPosture: 'https-ip' }],
    ['email', { hasEmail: false }],
    ['claude', { claudeAuthConfigured: false }],
    ['sharing', { telemetryDismissed: false }],
    ['agent', { firstRun: true }],
  ])('each state-derived step opens it on its own: %s', (_key, override) => {
    expect(isFirstRunOverlayVisible({ ...established, ...override })).toBe(true)
  })

  it('stays closed after "Finish later" / "Done"', () => {
    expect(isFirstRunOverlayVisible({ ...freshMarketplace, closed: true })).toBe(false)
  })

  it('a skip is an answer: skipped steps do not reopen it', () => {
    const onlySharingLeft = { ...established, telemetryDismissed: false }
    expect(isFirstRunOverlayVisible({ ...onlySharingLeft, skipped: ['sharing'] })).toBe(false)
    // …but a different, unskipped step still does.
    expect(
      isFirstRunOverlayVisible({ ...onlySharingLeft, hasEmail: false, skipped: ['sharing'] })
    ).toBe(true)
  })

  it('forced reopen (?onboarding=1 / Re-run setup) opens even closed and established', () => {
    expect(isFirstRunOverlayVisible({ ...established, closed: true, forced: true })).toBe(true)
  })
})

describe('Workspace Build: about you, your company, the AI audit', () => {
  const unanswered = { ...established, profileDone: false, companyDone: false, auditChoice: undefined }

  it('never opens the overlay by itself on an established fleet', () => {
    expect(isFirstRunOverlayVisible(unanswered)).toBe(false)
    expect(keys(applicableSteps(unanswered))).toEqual(['intro', 'profile', 'company', 'keys', 'audit'])
  })

  it('stay skippable, but Continue waits for an answer', () => {
    for (const key of ['profile', 'company', 'audit']) {
      expect(step(key).required, key).toBe(false)
      expect(canContinue({ step: step(key), ctx: unanswered }), key).toBe(false)
      expect(canContinue({ step: step(key), ctx: established }), key).toBe(true)
    }
  })

  it('asks about the person, then Claude, then the company and the audit, which both use it', () => {
    const order = keys(FIRST_RUN_STEPS)
    expect(order.indexOf('profile')).toBeLessThan(order.indexOf('claude'))
    expect(order.indexOf('company')).toBeGreaterThan(order.indexOf('claude'))
    expect(order.indexOf('audit')).toBeGreaterThan(order.indexOf('company'))
    expect(order.indexOf('audit')).toBeLessThan(order.indexOf('agent'))
  })
})

describe('forced reopen keeps completed steps complete', () => {
  it('the session set is every ELIGIBLE step, and the done ones read done', () => {
    const ctx = { ...established, forced: true }
    const steps = stepsForSession(ctx)
    expect(keys(steps)).toEqual(['intro', 'secure', 'email', 'profile', 'claude', 'company', 'sharing', 'keys', 'audit', 'agent'])
    const states = Object.fromEntries(
      steps.map((s) => [s.key, stepState({ step: s, currentKey: 'welcome', ctx })])
    )
    expect(states).toEqual({
      intro: 'upcoming', secure: 'done', email: 'done', profile: 'done', claude: 'done', keys: 'upcoming', company: 'done', audit: 'done', agent: 'done', sharing: 'done',
    })
  })

  it('an auto-open lists every step too, with what was already done checked', () => {
    const ctx = { ...established, hasEmail: false }
    const steps = stepsForSession(ctx)
    expect(keys(steps)).toEqual(keys(stepsForSession({ ...ctx, forced: true })))
    const todo = steps.filter((s) => stepState({ step: s, currentKey: 'welcome', ctx }) !== 'done')
    expect(keys(todo)).toEqual(['intro', 'email', 'keys'])
  })
})

describe('setup_started — the top of the activation funnel (ent#184 / ent#437)', () => {
  it('counts an auto-open over Claude still to connect, or no agent of your own', () => {
    expect(countsAsSetupStart(freshMarketplace)).toBe(true)
    expect(countsAsSetupStart({ ...established, claudeAuthConfigured: false })).toBe(true)
    // A non-admin on a seeded install: the ent#52 wizard opened for them too.
    expect(countsAsSetupStart({ ...established, isAdmin: false, firstRun: true })).toBe(true)
  })

  it('never counts a forced re-run, even on a fresh install', () => {
    expect(countsAsSetupStart({ ...freshMarketplace, forced: true })).toBe(false)
  })

  it.each([
    ['email', { hasEmail: false }],
    ['sharing', { telemetryDismissed: false }],
    ['secure', { tlsPosture: 'https-ip' }],
  ])('never counts an established fleet reopening for %s alone', (_key, override) => {
    const ctx = { ...established, ...override }
    expect(isFirstRunOverlayVisible(ctx)).toBe(true)
    expect(countsAsSetupStart(ctx)).toBe(false)
  })

  it('never counts a non-admin who already has an agent', () => {
    expect(countsAsSetupStart({ ...freshMarketplace, isAdmin: false, firstRun: false })).toBe(false)
  })

  it('the overlay records it only through this predicate', () => {
    const overlay = read('../../src/components/onboarding/FirstRunOverlay.vue')
    expect(overlay).toContain("if (countsAsSetupStart(ctx.value)) telemetry.record('setup_started')")
    expect(overlay.match(/'setup_started'/g)).toHaveLength(1)
  })
})

describe('Continue and Back pass over steps already done', () => {
  // The rail keeps a done step with its check (stepsForSession lists every eligible
  // step), but nobody is walked through a page with nothing on it.
  const order = ['welcome', 'intro', 'email', 'profile', 'claude', 'keys', 'company', 'done']
  const states = { intro: 'current', email: 'done', profile: 'upcoming', claude: 'done', keys: 'upcoming', company: 'upcoming' }

  it('Continue lands on the next step with something to do', () => {
    expect(nextStepKey(order, 'welcome', { ...states, intro: 'upcoming' })).toBe('intro')
    expect(nextStepKey(order, 'intro', states)).toBe('profile')
    expect(nextStepKey(order, 'profile', { ...states, intro: 'done', profile: 'current' })).toBe('keys')
  })

  it('always reaches the last panel, even past done steps', () => {
    expect(nextStepKey(order, 'keys', { ...states, company: 'done' })).toBe('done')
    expect(nextStepKey(order, 'done', states)).toBe('done')
    expect(nextStepKey(order, 'not-a-step', states)).toBe('done')
    expect(nextStepKey([], 'welcome', {})).toBeNull()
  })

  it('Back lands on the previous step with something to do, or the first panel', () => {
    expect(previousStepKey(order, 'profile', { ...states, intro: 'upcoming' })).toBe('intro')
    expect(previousStepKey(order, 'keys', { ...states, profile: 'done' })).toBe('intro')
    expect(previousStepKey(order, 'intro', states)).toBe('welcome')
    expect(previousStepKey(order, 'welcome', states)).toBe('welcome')
  })
})

describe('stepState — completion is derived, never stored', () => {
  it('current wins over everything', () => {
    expect(stepState({ step: step('claude'), currentKey: 'claude', ctx: established })).toBe('current')
  })

  it('a step reads done the moment its predicate goes false', () => {
    const ctx = { ...freshMarketplace }
    expect(stepState({ step: step('secure'), currentKey: 'welcome', ctx })).toBe('upcoming')
    // The operator saved a domain; the chassis refreshed the flags.
    expect(stepState({ step: step('secure'), currentKey: 'welcome', ctx: { ...ctx, tlsPosture: 'https-domain' } })).toBe('done')
  })

  it('done outranks an earlier skip', () => {
    const ctx = { ...freshMarketplace, hasEmail: true }
    expect(stepState({ step: step('email'), currentKey: 'welcome', ctx, skipped: ['email'] })).toBe('done')
  })

  it('a skipped step reads skipped; anything else upcoming', () => {
    const ctx = freshMarketplace
    expect(stepState({ step: step('email'), currentKey: 'welcome', ctx, skipped: ['email'] })).toBe('skipped')
    expect(stepState({ step: step('email'), currentKey: 'welcome', ctx })).toBe('upcoming')
  })

  it('a ride-along step reads done only once it emitted `complete` this session', () => {
    const ctx = established
    expect(stepState({ step: step('keys'), currentKey: 'welcome', ctx })).toBe('upcoming')
    expect(stepState({ step: step('keys'), currentKey: 'welcome', ctx, completed: ['keys'] })).toBe('done')
  })
})

describe('canContinue — the one blocking step', () => {
  it('blocks claude until the step completes or the credential is configured', () => {
    const ctx = freshMarketplace
    expect(canContinue({ step: step('claude'), ctx })).toBe(false)
    expect(canContinue({ step: step('claude'), ctx, completed: ['claude'] })).toBe(true)
    expect(canContinue({ step: step('claude'), ctx: { ...ctx, claudeAuthConfigured: true } })).toBe(true)
  })

  it('never blocks an optional step', () => {
    for (const key of ['secure', 'email', 'keys', 'agent', 'sharing']) {
      expect(canContinue({ step: step(key), ctx: freshMarketplace }), key).toBe(true)
    }
  })
})

describe('the constellation reports progress', () => {
  it('lights a node when nothing is left for it', () => {
    expect(litNodes({ secure: 'upcoming', email: 'done', keys: 'upcoming', claude: 'done', sharing: 'skipped' }))
      .toEqual([3])
    expect(litNodes({ secure: 'done', email: 'done', keys: 'done', claude: 'done', sharing: 'done' }))
      .toEqual([1, 2, 3, 4])
    // A capability not in this session has nothing left to do.
    expect(litNodes({ claude: 'upcoming' })).toEqual([1, 2, 4])
  })
})

describe('per-browser state', () => {
  it('persists close and reads it back', () => {
    expect(readFirstRunClosed()).toBe(false)
    expect(persistFirstRunClosed()).toBe(true)
    expect(localStorage.getItem(FIRST_RUN_CLOSED_KEY)).toBe('1')
    expect(readFirstRunClosed()).toBe(true)
  })

  it('persists skips as a JSON array and reads them back, de-duplicated', () => {
    expect(readFirstRunSkipped()).toEqual([])
    expect(persistFirstRunSkipped(['email', 'sharing', 'email'])).toBe(true)
    expect(JSON.parse(localStorage.getItem(FIRST_RUN_SKIPPED_KEY))).toEqual(['email', 'sharing'])
    expect(readFirstRunSkipped()).toEqual(['email', 'sharing'])
  })

  it('reads garbage as nothing skipped', () => {
    localStorage.setItem(FIRST_RUN_SKIPPED_KEY, '{not json')
    expect(readFirstRunSkipped()).toEqual([])
    localStorage.setItem(FIRST_RUN_SKIPPED_KEY, '"email"')
    expect(readFirstRunSkipped()).toEqual([])
  })

  it('honours a "no" given to a card this replaced, so an upgrade does not re-ask', () => {
    localStorage.setItem('trinity_hardening_guide_dismissed', '1')
    localStorage.setItem('trinity-admin-email-nudge-dismissed', 'true')
    localStorage.setItem('trinity_front_desk_dismissed', '1')
    expect(readFirstRunSkipped().sort()).toEqual(['agent', 'email', 'secure'])
  })

  it('counts the old telemetry snooze only while it lasts', () => {
    const now = Date.UTC(2026, 8, 11)
    localStorage.setItem('trinity_telemetry_ask_snoozed_until', new Date(now + 86400000).toISOString())
    expect(readFirstRunSkipped(now)).toEqual(['sharing'])
    expect(readFirstRunSkipped(now + 2 * 86400000)).toEqual([])
  })

  it('survives storage that throws', () => {
    const { getItem, setItem } = localStorage
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    localStorage.getItem = () => { throw new Error('private mode') }
    localStorage.setItem = () => { throw new Error('quota') }
    expect(readFirstRunClosed()).toBe(false)
    expect(readFirstRunSkipped()).toEqual([])
    expect(persistFirstRunClosed()).toBe(false)
    expect(persistFirstRunSkipped(['email'])).toBe(false)
    localStorage.getItem = getItem
    localStorage.setItem = setItem
    warn.mockRestore()
  })
})

describe('the chassis replaced the card ladder (structure)', () => {
  const DASHBOARD = read('../../src/views/Dashboard.vue')
  const OVERLAY = read('../../src/components/onboarding/FirstRunOverlay.vue')
  const SIDEBAR = read('../../src/components/SystemViewsSidebar.vue')
  const CHECKLIST = read('../../src/components/onboarding/ActivationChecklist.vue')

  it('deletes the absorbed surfaces and the one-card-at-a-time rule', () => {
    for (const gone of [
      '../../src/components/OnboardingWizard.vue',
      '../../src/components/onboarding/HardeningGuide.vue',
      '../../src/components/onboarding/FrontDeskPanel.vue',
      '../../src/components/onboarding/FinishSetupCard.vue',
    ]) {
      expect(existsSync(here(gone)), gone).toBe(false)
    }
    expect(DASHBOARD).not.toMatch(/onboarding-stack/)
    for (const name of ['OnboardingWizard', 'HardeningGuide', 'FrontDeskPanel', 'FinishSetupCard']) {
      expect(DASHBOARD, name).not.toContain(`<${name}`)
    }
  })

  it('keeps the checklist out of the page flow, and mounts the overlay once', () => {
    // ent#238's checklist STAYS (ruling on ent#581) — but not in the dashboard
    // flow. It fetches in its own `onMounted` and renders on the result, so
    // inline it could only ever appear one round-trip after paint, pushing the
    // fleet grid down by ~150px. Nothing could reserve that space, because the
    // entitlement answer is what decides whether a card exists at all, and
    // reserving unconditionally would leave a permanent gap on every OSS build
    // where the endpoint 404s.
    expect(DASHBOARD).not.toContain('<ActivationChecklist')
    // It lives in the systems rail — a fixed-width column outside the
    // dashboard's flow — so a late entitlement answer extends a scrolling list
    // instead of moving the fleet grid. It sits after the view rows (under the
    // labels, however many) and goes away with them when the rail collapses.
    expect(SIDEBAR).toContain('<ActivationChecklist v-if="!isCollapsed" />')
    expect(SIDEBAR.indexOf('<ActivationChecklist')).toBeGreaterThan(
      SIDEBAR.indexOf('v-for="view in sortedViews"'),
    )
    expect(SIDEBAR.indexOf('<ActivationChecklist')).toBeLessThan(
      SIDEBAR.indexOf('Create New View Button'),
    )
    // The checklist itself opens expanded on first start; a saved collapse wins.
    expect(CHECKLIST).toMatch(/getItem\(EXPANDED_KEY\) !== 'false'/)
    // Expanded by default: an unlabelled rail hides both the systems it lists
    // and the checklist. A saved preference still wins.
    expect(SIDEBAR).toMatch(/savedCollapsed === 'true' : false/)
    expect(DASHBOARD.match(/<FirstRunOverlay\b/g)).toHaveLength(1)
    // The Dashboard's hotkeys stand down while setup is open.
    expect(DASHBOARD).toMatch(/firstRunOpen\.value \|\| isEditorOpen\.value/)
  })

  it('is a teleported, labelled, modal dialog with no backdrop close and no X', () => {
    expect(OVERLAY).toContain('<Teleport to="body">')
    expect(OVERLAY).toContain('aria-modal="true"')
    expect(OVERLAY).toContain('aria-labelledby="first-run-title"')
    // The header owns the id, so exactly one is mounted at a time.
    expect(read('../../src/components/onboarding/FirstRunStepHeader.vue')).toContain('id="first-run-title"')
    const root = OVERLAY.slice(OVERLAY.indexOf('data-testid="first-run-overlay"') - 400, OVERLAY.indexOf('data-testid="first-run-overlay"'))
    expect(root).not.toMatch(/@click/)
    expect(OVERLAY).not.toMatch(/aria-label="Close/)
  })

  it('Esc raises the same confirm as "Finish later"', () => {
    const esc = OVERLAY.slice(OVERLAY.indexOf("if (e.key === 'Escape')"))
    expect(esc.slice(0, esc.indexOf("if (e.key === 'Tab')"))).toContain('requestClose()')
    expect(OVERLAY).toMatch(/@close="requestClose"/)
  })

  it('gates the required step through the registry, not in the component', () => {
    expect(OVERLAY).toMatch(/canContinue\(\{ step: currentStep\.value, ctx: ctx\.value, completed: completed\.value \}\)/)
    expect(OVERLAY).toMatch(/:disabled="!continueEnabled"/)
  })

  it('wires the ent#582 credential steps into the key → component map', () => {
    expect(OVERLAY).toMatch(/import StepClaude from '\.\/steps\/StepClaude\.vue'/)
    expect(OVERLAY).toMatch(/import StepKeys from '\.\/steps\/StepKeys\.vue'/)
    expect(OVERLAY).toMatch(/claude: StepClaude/)
    expect(OVERLAY).toMatch(/keys: StepKeys/)
  })

  it('refreshes every store ctx derives from after a step completes', () => {
    const refresh = OVERLAY.slice(OVERLAY.indexOf('async function refreshCtx()'))
    const body = refresh.slice(0, refresh.indexOf('\n}\n'))
    expect(body).toContain('sessions.loadFeatureFlags(true)')
    expect(body).toContain('auth.fetchUserProfile()')
    expect(body).toContain('firstRun.fetchState(true)')
  })

  it('reads a failed flags fetch as "not ready", never as "Claude is not configured"', () => {
    expect(OVERLAY).toMatch(/flagsLoaded: sessions\.featureFlagsLoaded && !sessions\.featureFlagsFailed/)
  })

  it('Settings → General carries the Re-run entry, which reopens via ?onboarding=1', () => {
    const settings = read('../../src/views/Settings.vue')
    expect(settings).toMatch(/v-if="activeTab === 'general'"[\s\S]{0,40}<FirstRunRerunPanel \/>/)
    const panel = read('../../src/components/settings/FirstRunRerunPanel.vue')
    expect(panel).toContain("query: { onboarding: '1' }")
  })
})

describe('Workspace Build: the audit in setup', () => {
  const audit = FIRST_RUN_STEPS.find((s) => s.key === 'audit')
  const ctx = { ...ready, isAdmin: true }

  it('is still to do when skipped, because skipping is not doing it', () => {
    const c = { ...ctx, auditChoice: 'skip' }
    expect(audit.applies(c)).toBe(true)
    expect(stepState({ step: audit, currentKey: 'agent', ctx: c, skipped: ['audit'] })).toBe('skipped')
  })

  it('reads as started rather than done once it has begun', () => {
    const c = { ...ctx, auditChoice: 'start' }
    expect(audit.applies(c)).toBe(false)
    expect(audit.doneLabel).toBe('Started')
  })

  it('comes after usage sharing, because starting it leaves setup', () => {
    const order = FIRST_RUN_STEPS.map((s) => s.key)
    expect(order.indexOf('sharing')).toBeLessThan(order.indexOf('audit'))
  })
})
